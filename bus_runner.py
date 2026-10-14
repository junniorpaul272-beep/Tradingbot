"""
bus_runner.py — the I/O edge of BUS. Reads queued Telegram messages, answers them
through bus.py, and records everything. Runs as a workflow step AFTER the scanner
(so state.json is fresh) and never inside it: the engine does not know BUS exists.

FILES
  inbox.jsonl        written by scanner_live (transport) — one line per inbound
                     message it did not handle itself. APPEND-ONLY, not owned here.
  user_ledger.jsonl  OWNED HERE (single writer): MESSAGE_RECEIVED, BUS_REPLY,
                     BUS_DELIVERED, BUS_STATEMENT, plus every catalog event.
  state.json, signal_event_log.jsonl, market_event_log.jsonl, leg_obs_log.jsonl
                     READ ONLY.

GUARANTEES (each has a test)
  * A message is answered once. The reply TEXT is written to the ledger before it
    is sent; a failed send is retried from that stored text on a later run and never
    re-executes the command (so /logtrade cannot double-log).
  * A message that crashes the handler gets a plain error reply and is marked
    handled — one bad message cannot jam the queue.
  * A missing token or a Telegram outage never fails the workflow (the step runs
    with continue-on-error) and never loses a reply.
  * as_of for every answer is the wall-clock "now": the question is about now.
"""

import json
import os
import sys
from datetime import datetime, timezone

import bus
import bus_adapters as ba
import bus_contract as bc
import ledger
from user_catalog import Catalog

LEDGER_NAME = "user_ledger.jsonl"
INBOX_NAME = "inbox.jsonl"
BUS_REPLY, BUS_DELIVERED, BUS_DELIVERY_FAILED = "BUS_REPLY", "BUS_DELIVERED", "BUS_DELIVERY_FAILED"
MAX_DELIVERY_ATTEMPTS = 3
PLAN_SNAPSHOT_MAX_AGE_S = 600       # only attach "what the engine had" to a logged trade if the state is this fresh
TELEGRAM_LIMIT = 4000


def _read_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    return rows


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def telegram_sender(token, chat_id):
    """Returns send(text) -> bool. `requests` is imported here so everything else
    in this module stays testable without it."""
    def send(text):
        import requests
        ok = True
        for i in range(0, max(len(text), 1), TELEGRAM_LIMIT):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_LIMIT],
                                        "disable_web_page_preview": True}, timeout=20)
                ok = ok and r.status_code == 200 and bool(r.json().get("ok"))
            except Exception as e:                                 # network weather, not a bug
                print(f"[BUS SEND ERROR] {type(e).__name__}: {e}")
                ok = False
        return ok
    return send


def collect(root, catalog, now):
    """Everything BUS may speak from, as Claims/Gaps: state (current-only), the
    append-only logs, and the catalog."""
    state = _read_json(os.path.join(root, "state.json")) or {}
    parts = [ba.adapt_state(state),
             ba.adapt_signal_events(_read_jsonl(os.path.join(root, "signal_event_log.jsonl"))),
             ba.adapt_market_events(_read_jsonl(os.path.join(root, "market_event_log.jsonl"))),
             ba.adapt_leg_outcomes(_read_jsonl(os.path.join(root, "leg_obs_log.jsonl")))]
    integ_recs, _ = ledger.read(os.path.join(root, "integrity_ledger.jsonl"))
    parts.append(ba.adapt_findings(ledger.as_of(integ_recs, now), now))
    recs, bad = ledger.read(catalog.path)
    visible = ledger.as_of(recs, now)
    parts += [ba.adapt_threads(visible), ba.adapt_knowledge(visible), ba.adapt_statements(visible), ba.adapt_terms(visible), ba.adapt_research(visible), ba.adapt_alerts(visible),
              ba.adapt_integrity(recs, bad, now, path=os.path.basename(catalog.path))]
    for r in visible:
        if r["event_type"] == "TRADE_LOGGED":
            parts.append(ba.adapt_case(catalog.get_case(r["payload"]["case_id"], as_of=now)))
    return ba.merge(*parts), state


def _plan_facts(adapted, state, now):
    """The engine's claims as of now, for attaching to a trade being logged — only if the
    snapshot is fresh enough to honestly be 'what the engine had when you took it'."""
    ts = state.get("market_data_timestamp")
    try:
        age = (now - ledger.to_utc(ts)).total_seconds()
    except Exception:
        return None
    if age > PLAN_SNAPSHOT_MAX_AGE_S or age < -60:
        return None
    return {c.subject: c.value for c in adapted.claims if c.authority.startswith("engine.")}


def _handled_updates(records):
    return {r["payload"]["update_id"] for r in records if r["event_type"] == BUS_REPLY}


def _delivery_state(records):
    delivered = {r["payload"]["update_id"] for r in records if r["event_type"] == BUS_DELIVERED}
    failures = {}
    for r in records:
        if r["event_type"] == BUS_DELIVERY_FAILED:
            failures[r["payload"]["update_id"]] = failures.get(r["payload"]["update_id"], 0) + 1
    return delivered, failures


def _last_subject(records):
    for r in reversed([r for r in records if r["event_type"] == bc.BUS_STATEMENT]):
        items = r["payload"].get("items") or []
        if items:
            return items[0]["subject"]
    return None


BUS_ANNOUNCED = "BUS_ANNOUNCED"
CANDLE_ARCHIVES = {"5m": "ohlc_5m_log.jsonl", "15m": "ohlc_15m_log.jsonl", "1h": "ohlc_1h_log.jsonl"}   # written by scanner_live (append-only)


def candle_counts(root):
    n = {}
    for tf, name in CANDLE_ARCHIVES.items():
        p = os.path.join(root, name)
        try:
            with open(p, "rb") as f:
                n[tf] = sum(1 for _ in f)
        except OSError:
            n[tf] = 0
    return n


def fire_alerts(root, catalog, now, send, summary):
    """User price alerts are catalog threads with a machine-checkable condition. Each scan:
    check open threads against the permanent 5m candle archive (only candles AFTER the thread was
    opened can resolve it — a level already crossed earlier does not fire), and tell the user ONCE.
    The resolution is written first; the message is stored and retried like any other reply."""
    by_tf = {tf: _read_jsonl(os.path.join(root, name)) for tf, name in CANDLE_ARCHIVES.items()}
    events = _read_jsonl(os.path.join(root, "market_event_log.jsonl"))
    if not any(by_tf.values()) and not events:
        return
    for rec in catalog.resolve_open_threads(candles_by_tf=by_tf, events=events, now=now):
        tid = rec["payload"]["thread_id"]
        adapted, _st = collect(root, catalog, now)
        answer = bc.resolve("thread_status", adapted.claims, now, params={"thread_id": tid}, gaps=adapted.gaps)
        reply = bus.finalize(answer)
        ev = rec["payload"]["evidence"]
        text = f"Alert hit: {reply.text}" if answer.items else f"Alert {tid} resolved at {ev.get('at')}."
        ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(answer), ts=now, now=now)
        uid = f"alert:{rec['event_id']}"
        ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": text, "kind": "alert",
                                                "used_fallback": reply.used_fallback, "violations": list(reply.violations)}, now=now)
        ok = bool(send(text)) if send else False
        ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
        summary["alerts"] = summary.get("alerts", 0) + 1


def announce(root, catalog, now, send, summary):
    """PROACTIVE speech under the silence contract. Speaks only when the SET of open integrity
    findings has changed since BUS last said anything about it (a new finding, or all-clear after
    open ones) — an unchanged set is ALREADY_KNOWN and stays silent. A fresh install with nothing
    ever open says nothing at all."""
    adapted, _state = collect(root, catalog, now)
    answer = bc.resolve("integrity_alert", adapted.claims, now, gaps=adapted.gaps)
    if not answer.items:
        return
    recs, _ = ledger.read(catalog.path)
    open_now = answer.items[0].claim.value.get("open") or []
    # Only MED/HIGH findings are worth interrupting for; LOW (e.g. schema drift) is visible in /health
    # but never pings. The decision is on the SET of loud fingerprints versus what was last announced.
    loud_now = {f["fingerprint"] for f in open_now if f.get("severity") in ("HIGH", "MED")}
    announced = [r for r in recs if r["event_type"] == BUS_ANNOUNCED]
    loud_before = set(announced[-1]["payload"].get("open", [])) if announced else set()
    if loud_now == loud_before:
        return                                              # ALREADY_KNOWN (or nothing has ever been wrong)
    reply = bus.finalize(answer)
    text = reply.text if loud_now else "Integrity inspector: all clear — the findings I reported earlier are no longer open."
    ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(answer), ts=now, now=now)
    rec = ledger.append(catalog.path, BUS_ANNOUNCED, {"text": text, "open": sorted(loud_now),
                                                      "used_fallback": reply.used_fallback}, now=now)
    # an announcement is delivered like a reply: stored first, retried from the stored text if it fails
    uid = f"announce:{rec['event_id']}"
    ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": text, "kind": "announcement",
                                            "used_fallback": reply.used_fallback, "violations": list(reply.violations)}, now=now)
    ok = bool(send(text)) if send else False
    ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
    summary["announced"] = summary.get("announced", 0) + 1


def run(root=".", now=None, send=None):
    now = ledger.to_utc(now) if now is not None else datetime.now(timezone.utc)
    catalog = Catalog(os.path.join(root, LEDGER_NAME))
    summary = {"answered": 0, "resent": 0, "delivered": 0, "failed": 0, "errors": 0}
    inbox = _read_jsonl(os.path.join(root, INBOX_NAME))
    seen_this_run = set()

    def deliver(update_id, text):
        ok = bool(send(text)) if send else False
        if ok:
            ledger.append(catalog.path, BUS_DELIVERED, {"update_id": update_id}, now=now)
            summary["delivered"] += 1
        else:
            ledger.append(catalog.path, BUS_DELIVERY_FAILED, {"update_id": update_id}, now=now)
            summary["failed"] += 1
        return ok

    # 1) retry replies that were composed but never delivered — from the STORED text
    recs, _ = ledger.read(catalog.path)
    delivered, failures = _delivery_state(recs)
    for r in recs:
        if r["event_type"] == BUS_REPLY:
            uid = r["payload"]["update_id"]
            if uid not in delivered and failures.get(uid, 0) < MAX_DELIVERY_ATTEMPTS and send:
                summary["resent"] += 1
                deliver(uid, r["payload"]["text"])

    # 2) answer new messages, oldest first
    for msg in sorted(inbox, key=lambda m: m.get("update_id", 0)):
        uid = msg.get("update_id")
        if uid is None or uid in seen_this_run:
            continue
        seen_this_run.add(uid)
        recs, _ = ledger.read(catalog.path)
        if uid in _handled_updates(recs):
            continue
        text = (msg.get("text") or "").strip()
        try:
            catalog.record_inbound(uid, text, ts=msg.get("received_at"), now=now)
            adapted, state = collect(root, catalog, now)
            reply = bus.respond(text, adapted.claims, adapted.gaps, now, catalog=catalog,
                                last_subject=_last_subject(recs),
                                plan_facts=_plan_facts(adapted, state, now), candle_counts=candle_counts(root))
            kind = "answer" if reply.answer is not None else "other"
            if reply.answer is not None:
                ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(reply.answer), ts=now, now=now)
            ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": reply.text, "kind": kind,
                                                    "used_fallback": reply.used_fallback,
                                                    "violations": list(reply.violations)}, now=now)
            summary["answered"] += 1
            if reply.used_fallback:
                print(f"[BUS] audit fallback for update {uid}: {list(reply.violations)}")
            deliver(uid, reply.text)
        except Exception as e:
            summary["errors"] += 1
            err = f"I hit an error handling that ({type(e).__name__}). It's logged; nothing was changed by this reply."
            print(f"[BUS ERROR] update {uid}: {type(e).__name__}: {e}")
            try:
                ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": err, "kind": "error",
                                                        "used_fallback": False, "violations": [str(e)[:200]]}, now=now)
                deliver(uid, err)
            except Exception as e2:                                   # never raise out of the step
                print(f"[BUS ERROR] could not even record the failure: {e2}")
    try:
        fire_alerts(root, catalog, now, send, summary)
    except Exception as e:
        summary["errors"] += 1
        print(f"[BUS ALERT ERROR] {type(e).__name__}: {e}")
    try:
        announce(root, catalog, now, send, summary)
    except Exception as e:                                      # an announcement problem must never fail the step
        summary["errors"] += 1
        print(f"[BUS ANNOUNCE ERROR] {type(e).__name__}: {e}")
    return summary


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    send = telegram_sender(token, chat) if token and chat else None
    if send is None:
        print("[BUS] no TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: replies are composed and logged, not sent")
    print("[BUS]", json.dumps(run(root, send=send)))


if __name__ == "__main__":
    main(sys.argv)
