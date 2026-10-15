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

import dataclasses
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import bus
import bus_adapters as ba
import bus_contract as bc
import ledger
import quiet_mode as qm
from user_catalog import Catalog

STATE_HISTORY_NAME = "state_history_log.jsonl"
HISTORY_WINDOW = timedelta(days=7)      # recent history only; older records are not read (stated, not hidden)
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


def _history_claims(root, state, now):
    """Past state from state_history_log.jsonl: only what was recorded by `now`, only the last
    HISTORY_WINDOW, and only snapshots STRICTLY older than the current state.json (the current scan is
    adapt_state's job; two sources must not describe the same instant). A missing/empty log adds nothing."""
    recs, _bad, _trunc = ledger.read_tail(os.path.join(root, STATE_HISTORY_NAME))
    if not recs:
        return ba.Adapted()
    visible = ledger.as_of(recs, now)
    try:
        cutoff = ledger.to_utc((state or {}).get("market_data_timestamp"))
    except ledger.LedgerError:
        cutoff = None
    lo = now - HISTORY_WINDOW
    keep = [r for r in visible if ledger.to_utc(r["ts"]) >= lo and (cutoff is None or ledger.to_utc(r["ts"]) < cutoff)]
    return ba.adapt_state_history(keep)


def collect(root, catalog, now):
    """Everything BUS may speak from, as Claims/Gaps: state (current-only), the
    append-only logs, and the catalog."""
    state = _read_json(os.path.join(root, "state.json")) or {}
    parts = [ba.adapt_state(state),
             ba.adapt_signal_events(_read_jsonl(os.path.join(root, "signal_event_log.jsonl"))),
             ba.adapt_market_events(_read_jsonl(os.path.join(root, "market_event_log.jsonl"))),
             ba.adapt_leg_outcomes(_read_jsonl(os.path.join(root, "leg_obs_log.jsonl")))]
    parts.append(_history_claims(root, state, now))
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
BUS_SIGNAL_ANNOUNCED = "BUS_SIGNAL_ANNOUNCED"      # a fired signal BUS has stated (once per event_id)
BUS_SIGNAL_SKIPPED = "BUS_SIGNAL_SKIPPED"          # a fired signal BUS chose NOT to state, with the reason (never a silent drop)
SIGNAL_FLAG_ENV = "BUS_SIGNALS"
SIGNAL_MAX_AGE = timedelta(minutes=20)             # an entry price is stale after this; older signals are logged, not announced
SIGNAL_MAX_PER_RUN = 3
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
    q = qm.state(recs, now)
    if q.active:                                            # quiet mode: hold (or, if critical, send) instead of announcing
        hold_integrity(catalog, recs, answer, open_now, loud_before, q, now, send, summary)
        return
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


# ----------------------------------------------------------------------
# QUIET MODE (the rules live in quiet_mode.py; this is its I/O edge)
# ----------------------------------------------------------------------
QUIET_NOTE = "Quiet mode is on, but this is a critical integrity finding, so I'm sending it now.\n"
LAGOS = "Africa/Lagos"


def _lagos(dt):
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(LAGOS)).strftime("%H:%M")


def handle_quiet(catalog, text, update_id, received_at, now):
    """/quiet <duration|until HH:MM> and /loud. Writes QUIET_SET / QUIET_CLEARED and returns the reply text."""
    recs, _ = ledger.read(catalog.path)
    st = qm.state(recs, now)
    p = qm.parse(text, received_at, now)
    if p.kind == "usage":
        cur = (f"Quiet mode is on until {qm.hhmm(st.until)} ({_lagos(st.until)} Lagos time).\n" if st.active
               else "Quiet mode is off.\n")
        return cur + qm.USAGE
    if p.kind == "error":
        return p.message
    if p.kind == "loud":
        if not st.active:
            return "Quiet mode isn't on, so there is nothing to end."
        ledger.append(catalog.path, qm.QUIET_CLEARED, {"update_id": update_id, "quiet_id": st.quiet_id}, now=now)
        return "Quiet mode is off. If I held anything while it was on, I'll send one summary now."
    if p.until <= now:
        return ("That quiet period had already ended by the time I read your message (I only read messages when the "
                "scanner runs), so nothing was muted.")
    sent = ledger.to_utc(received_at) if received_at is not None else now
    ledger.append(catalog.path, qm.QUIET_SET, {"until": ledger._iso(p.until), "set_at": ledger._iso(sent),
                                               "minutes": p.minutes, "update_id": update_id}, ts=sent, now=now)
    replaced = " This replaces the earlier end time." if st.active else ""
    return (f"Quiet mode is on until {qm.hhmm(p.until)} ({_lagos(p.until)} Lagos time).{replaced} "
            "I'll hold my automated signal and integrity messages and send ONE summary when it ends. "
            "Your replies, your own alerts and a critical integrity finding still come through.")


def _fp_claim(answer, findings):
    c = answer.items[0].claim
    return dataclasses.replace(c, value={"open": findings, "n_open": len(findings)})


def hold_integrity(catalog, recs, answer, open_now, loud_before, q, now, send, summary):
    """Quiet mode is active and the loud set changed. New critical findings are sent (once each, with a plain note);
    every other new loud finding is stored as held. Nothing is dropped and nothing is worded here."""
    loud = [f for f in open_now if f.get("severity") in ("HIGH", "MED") and f["fingerprint"] not in loud_before]
    held = {r["payload"].get("fingerprint") for r in recs if r["event_type"] == qm.BUS_HELD
            and r["payload"].get("kind") == "integrity" and r["payload"].get("quiet_id") == q.quiet_id}
    sent = {fp for r in recs if r["event_type"] == qm.BUS_CRITICAL_SENT and r["payload"].get("quiet_id") == q.quiet_id
            for fp in r["payload"].get("fingerprints", [])}
    crit = [f for f in loud if qm.is_critical(f) and f["fingerprint"] not in sent]
    for f in loud:
        if f["fingerprint"] in held or qm.is_critical(f):
            continue
        ledger.append(catalog.path, qm.BUS_HELD, {"kind": "integrity", "quiet_id": q.quiet_id, "fingerprint": f["fingerprint"],
                                                  "probe": f.get("probe"), "severity": f.get("severity"),
                                                  "first_seen": f.get("first_seen")}, now=now)
        summary["integrity_held"] = summary.get("integrity_held", 0) + 1
    if not crit:
        return
    ans = bc.resolve("integrity_alert", [_fp_claim(answer, crit)], now, gaps=[])
    reply = bus.finalize(ans)
    text = QUIET_NOTE + reply.text
    ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(ans), ts=now, now=now)
    rec = ledger.append(catalog.path, qm.BUS_CRITICAL_SENT, {"quiet_id": q.quiet_id, "fingerprints": sorted(f["fingerprint"] for f in crit),
                                                             "text": text}, now=now)
    uid = f"critical:{rec['event_id']}"
    ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": text, "kind": "critical_integrity",
                                            "used_fallback": reply.used_fallback, "violations": list(reply.violations)}, now=now)
    ok = bool(send(text)) if send else False
    ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
    summary["critical_sent"] = summary.get("critical_sent", 0) + 1


def _n(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def quiet_summary(root, catalog, now, send, summary):
    """When a quiet period has ended (expired or /loud), say ONE thing about it, once. Built from the stored held
    records: signal text was audited when it was composed; integrity wording is re-rendered from the findings that
    are STILL open; findings that have closed are named by probe only, never re-described."""
    recs, _ = ledger.read(catalog.path)
    p = qm.pending_summary(recs, now)
    if p is None:
        return
    by_user = qm.state(recs, now).reason == "cleared"
    sig = [r for r in p["held"] if r["payload"].get("kind") == "signal"]
    integ = [r for r in p["held"] if r["payload"].get("kind") == "integrity"]
    crit_fps = [fp for r in p["critical"] for fp in r["payload"].get("fingerprints", [])]
    if not sig and not integ and not crit_fps and by_user:
        ledger.append(catalog.path, qm.BUS_QUIET_SUMMARY, {"quiet_id": p["quiet_id"], "held_ids": [], "text": None, "silent": True}, now=now)
        return
    adapted, _st = collect(root, catalog, now)
    answer = bc.resolve("integrity_alert", adapted.claims, now, gaps=adapted.gaps)
    open_now = answer.items[0].claim.value.get("open") if answer.items else []
    open_by = {f["fingerprint"]: f for f in (open_now or [])}
    loud_now = {fp for fp, f in open_by.items() if f.get("severity") in ("HIGH", "MED")}
    announced = [r for r in recs if r["event_type"] == BUS_ANNOUNCED]
    before = set(announced[-1]["payload"].get("open", [])) if announced else set()
    held_fps = [r["payload"]["fingerprint"] for r in integ]
    still = [open_by[fp] for fp in dict.fromkeys(held_fps + crit_fps) if fp in open_by]
    closed = [r for r in integ if r["payload"]["fingerprint"] not in open_by]
    closed_crit = [fp for fp in dict.fromkeys(crit_fps) if fp not in open_by]

    head = f"Quiet mode ended. It was on from {qm.hhmm(p['started_at'])} to {qm.hhmm(p['ended_at'])}."
    counts = []
    if sig:
        counts.append(_n(len(sig), "signal"))
    if integ:
        counts.append(_n(len(integ), "integrity finding"))
    lines = [head, ("Held while it was on: " + " and ".join(counts) + ".") if counts else "Nothing was held."]
    if crit_fps:
        lines.append(f"{_n(len(set(crit_fps)), 'critical finding')} came through while it was on.")
    parts = []
    if sig:
        parts.append("Signals (entry prices are from when they fired, not now):\n\n" + "\n\n".join(r["payload"]["text"] for r in sig))
    if still:
        ans = bc.resolve("integrity_alert", [_fp_claim(answer, still)], now, gaps=[])
        parts.append("Integrity findings still open:\n" + bus.finalize(ans).text)
        ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(ans), ts=now, now=now)
    gone = [f"- {r['payload'].get('probe')} ({str(r['payload'].get('severity')).lower()}): opened and has closed since." for r in closed]
    gone += [f"- a critical finding sent earlier has closed since." for _ in closed_crit]
    if gone:
        parts.append("No longer open:\n" + "\n".join(gone))
    if before and not loud_now:
        parts.append("No integrity findings are open now.")
    text = "\n".join(lines) + ("\n\n" + "\n\n".join(parts) if parts else "")
    if len(text) > TELEGRAM_LIMIT - 200:                       # too long for one message: counts plus each signal's first line
        short = [r["payload"]["text"].split("\n", 1)[0] for r in sig[:5]]
        text = "\n".join(lines) + ("\n" + "\n".join(short) if short else "") + "\nThe full detail is in the ledger; /health shows the integrity state."
    for r in sig:                                               # what BUS said is only now said: log it as stated
        st = r["payload"].get("statement")
        if st:
            ledger.append(catalog.path, bc.BUS_STATEMENT, st, ts=now, now=now)
    rec = ledger.append(catalog.path, qm.BUS_QUIET_SUMMARY, {"quiet_id": p["quiet_id"], "held_ids": [r["event_id"] for r in p["held"]],
                                                             "text": text}, now=now)
    keep = (before & loud_now) | (set(held_fps + crit_fps) & loud_now)
    if keep != before:                                          # the summary now covers these: announce() must not repeat them
        ledger.append(catalog.path, BUS_ANNOUNCED, {"text": "(covered by the quiet-mode summary)", "open": sorted(keep),
                                                    "used_fallback": False, "via": "quiet_summary"}, now=now)
    uid = f"quiet:{rec['event_id']}"
    ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": text, "kind": "quiet_summary",
                                            "used_fallback": False, "violations": []}, now=now)
    ok = bool(send(text)) if send else False
    ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
    summary["quiet_summary"] = summary.get("quiet_summary", 0) + 1


def signals_flag(env=None):
    return str((env if env is not None else os.environ).get(SIGNAL_FLAG_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def announce_signals(root, catalog, now, send, summary):
    """FLAG-GATED, REPORT-ONLY. States each fired signal once, from its own signal_event_log.jsonl record.
    It reads the log and writes only to BUS's own catalog ledger: it never touches the engine's files, the
    legacy alert path or any trading decision. A signal older than SIGNAL_MAX_AGE is recorded as skipped (with
    the reason) rather than announced, so switching the flag on can never replay history."""
    adapted, _state = collect(root, catalog, now)
    sigs = sorted((c for c in adapted.claims if c.subject == "signal.undelivered"), key=lambda c: (c.recorded_at, c.value["event_id"]))
    if not sigs:
        return
    recs, _ = ledger.read(catalog.path)
    done = {r["payload"].get("event_id") for r in recs if r["event_type"] in (BUS_SIGNAL_ANNOUNCED, BUS_SIGNAL_SKIPPED)}
    done |= {r["payload"].get("event_id") for r in recs if r["event_type"] == qm.BUS_HELD and r["payload"].get("kind") == "signal"}
    q = qm.state(recs, now)
    if q.active:                                            # HOLD every new signal (stored, summarised when quiet ends)
        for c in sigs:
            eid = c.value["event_id"]
            if eid in done:
                continue
            answer = bc.resolve("signal_alert", [c], now, gaps=[])
            if not answer.items:
                ledger.append(catalog.path, BUS_SIGNAL_SKIPPED, {"event_id": eid, "reason": "no supported claim"}, now=now)
                continue
            reply = bus.finalize(answer)
            ledger.append(catalog.path, qm.BUS_HELD, {"kind": "signal", "quiet_id": q.quiet_id, "event_id": eid,
                                                      "text": reply.text, "used_fallback": reply.used_fallback,
                                                      "statement": bc.statement_payload(answer)}, now=now)
            summary["signals_held"] = summary.get("signals_held", 0) + 1
        return
    spoken = 0
    for c in sigs:
        eid = c.value["event_id"]
        if eid in done:
            continue
        age = now - c.recorded_at
        if age > SIGNAL_MAX_AGE:
            ledger.append(catalog.path, BUS_SIGNAL_SKIPPED, {"event_id": eid, "reason": "older than the announce window",
                                                              "age_s": int(age.total_seconds())}, now=now)
            summary["signals_skipped"] = summary.get("signals_skipped", 0) + 1
            continue
        if spoken >= SIGNAL_MAX_PER_RUN:
            break                                           # the rest wait for the next run (or age out and are recorded as skipped)
        answer = bc.resolve("signal_alert", [c], now, gaps=[])
        if not answer.items:
            ledger.append(catalog.path, BUS_SIGNAL_SKIPPED, {"event_id": eid, "reason": "no supported claim"}, now=now)
            continue
        reply = bus.finalize(answer)
        ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(answer), ts=now, now=now)
        rec = ledger.append(catalog.path, BUS_SIGNAL_ANNOUNCED, {"event_id": eid, "text": reply.text,
                                                                 "used_fallback": reply.used_fallback}, now=now)
        uid = f"signal:{eid}"
        ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": reply.text, "kind": "signal_alert",
                                                "used_fallback": reply.used_fallback, "violations": list(reply.violations)}, now=now)
        ok = bool(send(reply.text)) if send else False
        ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
        summary["signals_announced"] = summary.get("signals_announced", 0) + 1
        spoken += 1


def run(root=".", now=None, send=None, signals=False):
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
            if qm.command_of(text):                                   # /quiet, /loud: handled here, the catalog is not involved
                reply = bus.Reply(handle_quiet(catalog, text, uid, msg.get("received_at"), now), None)
            else:
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
        quiet_summary(root, catalog, now, send, summary)      # once, when a quiet period has ended; before announce() so it is not repeated
    except Exception as e:
        summary["errors"] += 1
        print(f"[BUS QUIET ERROR] {type(e).__name__}: {e}")
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
    if signals:                                                 # OFF unless BUS_SIGNALS is set: nothing below runs, nothing is written
        try:
            announce_signals(root, catalog, now, send, summary)
        except Exception as e:                                  # a signal-report problem must never fail the step
            summary["errors"] += 1
            print(f"[BUS SIGNAL ERROR] {type(e).__name__}: {e}")
    return summary


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    send = telegram_sender(token, chat) if token and chat else None
    if send is None:
        print("[BUS] no TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: replies are composed and logged, not sent")
    on = signals_flag()
    print(f"[BUS] signal reporting: {'ON' if on else 'off'} ({SIGNAL_FLAG_ENV})")
    print("[BUS]", json.dumps(run(root, send=send, signals=on)))


if __name__ == "__main__":
    main(sys.argv)
