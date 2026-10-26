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
import re
import sys
from datetime import datetime, timedelta, timezone

import bus
import bus_adapters as ba
import bus_contract as bc
import case_reconstruct as cr
import conversation_search as cs
import converse
import guard
import idea_review as ir
import informal
import ledger
import market_reconstruct as mr
import plainwords
import timeref
import voice
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
        with open(path, encoding="utf-8-sig", errors="replace") as f:          # a BOM or a stray bad byte must not hide a row
            for line in f:
                line = line.strip()
                if line:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):                                   # a bare number / list / null is not a record
                        rows.append(row)
    return rows


class Digests(list):
    """The per-scan records plus what could NOT be used: `unreadable` = [(time of the scan before, time of the scan after)] for each damaged line
    (either end None when the damage is at the start or end), `conflicts` = [time] where two saved scans share a timestamp but disagree."""
    unreadable = ()
    conflicts = ()


def _read_digests(root):
    """The per-scan structure records, oldest first, one per scan (a row written twice is counted once). Damaged lines and conflicting duplicates are
    kept as evidence of what is missing, so an answer can say when its evidence has a hole instead of looking complete."""
    path = os.path.join(root, "structure_digest_log.jsonl")
    out, seen, bad_after, conflicts = Digests(), {}, [], []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    r = None
                t = mr._t(r.get("scanned_at")) if isinstance(r, dict) else None
                if t is None:
                    bad_after.append(len(out)); continue
                k = r.get("scanned_at")
                if k in seen:
                    if seen[k] != json.dumps(r, sort_keys=True):
                        conflicts.append(t)
                    continue
                seen[k] = json.dumps(r, sort_keys=True); out.append(r)
    order = sorted(range(len(out)), key=lambda i: str(out[i].get("scanned_at")))
    ts = [mr._t(out[i].get("scanned_at")) for i in order]
    out2 = Digests(out[i] for i in order)
    # a damaged line sat between file positions n-1 and n; the neighbours by position give the stretch it could have covered
    out2.unreadable = [((mr._t(out[n - 1].get("scanned_at")) if n > 0 else None), (mr._t(out[n].get("scanned_at")) if n < len(out) else None)) for n in bad_after]
    out2.conflicts = conflicts
    return out2


def _evidence_note(digests, start, end):
    """A sentence about damaged or conflicting scan records that touch [start, end] (None = open ended); "" when none do."""
    parts = []
    for a_, b_ in getattr(digests, "unreadable", ()):
        lo, hi = a_ or datetime.min.replace(tzinfo=timezone.utc), b_ or datetime.max.replace(tzinfo=timezone.utc)
        if (end is None or lo <= end) and (start is None or hi >= start):
            parts.append(f"{timeref.hm(a_)} and {timeref.hm(b_)}" if a_ and b_ else ("after " + timeref.hm(a_) if a_ else "before " + timeref.hm(b_)))
    msg = ""
    if parts:
        msg += (f"One saved scan line between {parts[0]} couldn't be read" if len(parts) == 1 else
                f"{len(parts)} saved scan lines (first between {parts[0]}) couldn't be read") + ", so something from that stretch may be missing from what I just said. "
    cf = [t for t in getattr(digests, "conflicts", ()) if (start is None or t >= start - timedelta(minutes=1)) and (end is None or t <= end + timedelta(minutes=1))]
    if cf:
        msg += f"Two saved scans at {timeref.hm(cf[0])} disagree with each other; I used the first. "
    return msg.strip()


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


def _sent_at(msg, now):
    """When the user sent the message, if the inbox says so and it is not in the future; else None."""
    try:
        t = ledger.to_utc(msg.get("received_at"))
    except Exception:
        return None
    return t if t <= now else None


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



# ----------------------------------------------------------------------
# PLAIN MESSAGES, CLARIFYING QUESTIONS AND RECONSTRUCTION (Step 2e)
# The decisions are made by converse.py (pure). This is the I/O edge: it reads the files, asks the question, stores what
# is pending in the reply record, and runs the chosen action. The user's words are already stored verbatim by
# record_inbound before anything here runs; nothing below ever rewrites them.
# ----------------------------------------------------------------------
PENDING_TTL = timedelta(hours=6)
CONTEXT_EXCHANGES = 3
_TIMEWORDS = set("today yesterday tonight morning afternoon evening night last this monday tuesday wednesday thursday friday saturday "
                 "sunday earlier ago day week".split())


def _user_replies(recs):
    return [r for r in recs if r["event_type"] == BUS_REPLY and isinstance(r["payload"].get("update_id"), int)]


def _open_pending(recs, now):
    """The question the bot last asked, if it is still waiting: it was the newest reply to the user, nothing resolved it,
    and it is not stale. Any other message handled in between closes it."""
    rs = _user_replies(recs)
    if not rs:
        return None
    last = rs[-1]
    p = last["payload"].get("pending")
    if not p or not p.get("options"):
        return None
    if now - ledger.to_utc(last["recorded_at"]) > PENDING_TTL:
        return None
    return {"uid": last["payload"]["update_id"], **p}


def _last_context(recs, now):
    for r in reversed(_user_replies(recs)[-CONTEXT_EXCHANGES:]):
        ctx = r["payload"].get("context")
        if ctx and now - ledger.to_utc(r["recorded_at"]) <= PENDING_TTL:
            return ctx
    return None


LATE_NOTE_AFTER_S = 600


HISTORY_INTENTS = ("trade_reconstruction", "bot_vs_user", "thread_status")   # read saved records, not the live state


def _late_note(sent_at, now, intent=None):
    """A question read long after it was sent is answered with the picture NOW. Say so, so it isn't mistaken for the picture then."""
    if sent_at is None or intent in HISTORY_INTENTS or (now - sent_at).total_seconds() <= LATE_NOTE_AFTER_S:
        return ""
    return (f"(You sent this at {timeref.hm(sent_at)} Lagos time and I'm reading it at {timeref.hm(now)}, "
            f"so this is how things look as of {timeref.hm(now)}, not at {timeref.hm(sent_at)}.)\n")


def _day_label(w):
    return f"{timeref.lagos(w.start):%A %d %b}" if w.kind == "day" else f"{timeref.lagos(w.start):%a %d %b}, {timeref.hm(w.start)}-{timeref.hm(w.end)}"


def _signals_of(adapted):
    out = []
    for c in adapted.claims:
        if c.subject == "signal.undelivered" and isinstance(c.value, dict):
            out.append({"at": c.observed_at, "direction": c.value.get("direction"), "entry": c.value.get("entry")})
    return out


def _market_text(root, catalog, adapted, start, end, now, assumptions=(), brief=True):
    candles = _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["5m"]))
    events = _read_jsonl(os.path.join(root, "market_event_log.jsonl"))
    hpath = os.path.join(root, STATE_HISTORY_NAME)
    history = ledger.read(hpath)[0] if os.path.exists(hpath) else []
    recs, _ = ledger.read(catalog.path)
    c1h = _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["1h"]))
    digests = _read_digests(root)
    thesis = _read_jsonl(os.path.join(root, "thesis_context_log.jsonl"))
    rep = mr.reconstruct_window(start, end, candles, events, history, recs, _signals_of(adapted), now, c1h, digests, thesis)
    ctx = {"kind": "market", "start": ledger._iso(start), "end": ledger._iso(end)}
    ctx["brief"] = bool(brief)
    txt = mr.render_window_brief(rep, assumptions) if brief else mr.render_window(rep, assumptions)
    ev_note = _evidence_note(digests, start, end)
    return (txt.rstrip() + ("\n" + ev_note if ev_note else "")), ctx


def _replay_text(catalog, key, start, end, label, exclude, now):
    recs, _ = ledger.read(catalog.path)
    exs = cs.exchanges(recs, start, end, exclude=exclude)
    lines, total = cs.replay(exs, key)
    day = f"{timeref.lagos(start):%a %d %b}"
    if not lines:
        return f"I can't find anything stored for {label} on {day} any more.", None
    return cs.render_replay(label, lines, total, cs.coverage(recs), day), None


def _convo_reply(catalog, d, now, uid):
    recs, _ = ledger.read(catalog.path)
    w = d.get("window")
    if w is None:
        start, end, day_label = now - timedelta(days=7), now + timedelta(seconds=1), "the last week"
    else:
        start, end, day_label = w.start, w.end + (timedelta(seconds=1) if w.kind != "day" else timedelta(0)), _day_label(w)
    exs = cs.exchanges(recs, start, end, exclude={uid})
    cov = cs.coverage(recs)
    named = [t for t in re.findall(r"[a-z']{4,}", d["normalised"].lower()) if t not in converse._STOP and t not in _TIMEWORDS
             and t not in cs.STOP and t not in {"talk", "talked", "discuss", "discussed", "conversation", "thing", "chat", "remind", "thinking",
                                                 "earlier", "know", "what", "when", "were", "have", "said", "told"}]
    tops, note = cs.topics(exs), ""
    if named:
        hit = cs.find_by_words(exs, " ".join(named))
        if hit:
            tops = hit
        elif tops:
            note = f"I couldn't find anything matching \"{' '.join(named)}\", but this is what we did cover. "
    opts = cs.options(tops)
    if not opts:
        return cs.render_options([], cov, day_label), None
    options = [{"id": str(i), "label": f"{t['label']}" + (f" {t['example']}" if t["example"] else ""),
                "action": {"type": "convo", "key": t["key"], "label": t["label"], "start": ledger._iso(start), "end": ledger._iso(end)}}
               for i, t in enumerate(opts, 1)]
    return note + cs.render_options(opts, cov, day_label), {"pending": {"kind": "convo", "options": options}}


def _case_text(root, catalog, case_id, detail, seed, now):
    recs, bad = ledger.read(catalog.path)
    hpath = os.path.join(root, STATE_HISTORY_NAME)
    history = ledger.read(hpath)[0] if os.path.exists(hpath) else None
    rep = cr.reconstruct(case_id, recs, history, catalog_bad_lines=len(bad) if hasattr(bad, "__len__") else int(bad or 0))
    if detail:
        return cr.render(rep)
    text = cr.render_plain(rep, seed)
    if rep.get("status") == "OK":
        trade = next(r for r in recs if r["event_type"] == "TRADE_LOGGED" and r["payload"].get("case_id") == case_id)
        anchor = ledger.to_utc(rep["cutoffs"]["entry_anchor"])
        out_at = None
        o = [r for r in recs if r["event_type"] == "OUTCOME_RECORDED" and r["payload"].get("case_id") == case_id]
        if o and rep["outcome_status"] in ("known", "partial"):
            out_at = ledger.to_utc(o[-1]["ts"])
        ft = mr.follow_through(trade["payload"], anchor, _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["5m"])), out_at, now)
        o_txt = (o[-1]["payload"]["result"].lower() if o and rep["outcome_status"] in ("known", "partial") else None)
        head, _, tail = text.rpartition("\nWhat I can stand behind:")
        text = head + "\n\n" + mr.render_follow(ft, o_txt) + "\n\nWhat I can stand behind:" + tail
    return text


def _ask_bus(root, catalog, canonical, now, uid, recs, sent_at=None, lead=True):
    adapted, _state = collect(root, catalog, now)
    r = bus.route(canonical)
    if r.kind == "unknown":
        return bus.Reply(voice.unknown(uid), None)
    rep = bus.respond(canonical, adapted.claims, adapted.gaps, now, catalog=catalog, last_subject=_last_subject(recs),
                      candle_counts=candle_counts(root))
    if (rep.answer is not None and r.kind == "intent" and rep.answer.status != "ANSWER" and "too old to call current" in rep.text
            and r.intent in ("market_state", "current_thesis", "current_market_story", "watching", "what_changed")):
        stamps = [c.recorded_at for c in adapted.claims if getattr(c, "recorded_at", None) is not None]
        newest = max(stamps) if stamps else None
        age = f"{timeref.hm(newest)} Lagos time ({int((now - newest).total_seconds() // 3600)} hours ago)" if newest and now - newest >= timedelta(hours=1) else (f"{timeref.hm(newest)} Lagos time" if newest else None)
        plain = ("I can't give you a current read right now: " + (f"the newest engine reading I have is from {age}, and I won't pass old data off as current. " if age else
                 "the engine readings I have are too old to call current, and I won't pass them off as current. ")
                 + "The scanner only runs 08:00 to 16:55 Lagos time, Monday to Friday. You can still ask about a past stretch, like 'what happened between 9 and 10', or 'when was the last CHoCH'.")
        return bus.Reply(voice.wrap("answer", plain, uid) if lead else plain, rep.answer, rep.used_fallback, rep.violations)
    if rep.answer is not None and r.kind == "intent":
        body = _late_note(sent_at, now, r.intent) + rep.text
        rep = bus.Reply(voice.wrap("answer", body, uid) if lead else body, rep.answer, rep.used_fallback, rep.violations)
    return rep


# ----------------------------------------------------------------------
# "WHEN WAS THE LAST CHoCH?" and messages that ask for several things at once
# ----------------------------------------------------------------------
AFTER_EVENT_SPAN = timedelta(hours=6)


def _last_event_text(root, catalog, wants, now, mode="last", window=None, before=None):
    """-> (text, context). A lookup in the engine's saved records; says where its records begin and what each source cannot tell."""
    hpath = os.path.join(root, STATE_HISTORY_NAME)
    history = ledger.read(hpath)[0] if os.path.exists(hpath) else []
    digs = _read_digests(root)
    res = mr.last_structure_events(digs,
                                   _read_jsonl(os.path.join(root, "thesis_context_log.jsonl")),
                                   _read_jsonl(os.path.join(root, "zone_log.jsonl")), history,
                                   (before - timedelta(seconds=1)) if before is not None else now)
    if before is not None:                      # "the one before that": the same reader, as of just before the event we were looking at
        res["now"], res["asof_anchor"] = now, before
    if mode in ("first", "count", "list"):
        part = window if isinstance(window, str) else None
        start, end, label = mr.event_window(now, part, None if isinstance(window, str) else window)
        text = mr.render_event_list(res, wants, mode, start, end, label)
    else:
        text = mr.render_last_events(res, wants)
    anchor = mr.event_anchor(res, wants)
    # a damaged line AFTER the event we report could be hiding a newer one; with no event found, any damaged line in the searched range counts
    ev_note = _evidence_note(digs, anchor, (before - timedelta(seconds=1)) if before is not None else now)
    if ev_note:
        text = text.rstrip() + "\n" + ev_note
    ctx = {"kind": "event", "wants": list(wants), "at": ledger._iso(anchor) if anchor else None}
    for zk in ("fvg", "ob"):
        zh = res.get("zone_" + zk)
        if zk in wants and zh and zh.get("key"):
            ctx["zone"] = {"type": zk, "key": zh["key"], "first_seen": ledger._iso(zh["at"])}
    return text, ctx


def _after_text(root, catalog, anchor, now, assumptions=()):
    """What the market did from `anchor` onward: up to six hours, or until now. -> (text, context)."""
    end = min(anchor + AFTER_EVENT_SPAN, now)
    adapted, _ = collect(root, catalog, now)
    notes = list(assumptions)
    if anchor + AFTER_EVENT_SPAN < now:
        notes.append("I covered the first six hours after it; say 'and after that?' and I'll carry on")
    return _market_text(root, catalog, adapted, anchor, end, now, notes)



_WANT_TERM = {"choch": "choch", "bos": "bos", "sweep": "liquidity sweep", "fvg": "fvg", "ob": "order block"}
_TERM_CONCEPT = {"choch": "choch", "bos": "bos", "liquidity sweep": "sweep", "fvg": "fvg", "order block": "ob", "displacement": "displacement",
                 "protected level": "protected level", "retracement": "retracement", "htf": "htf", "bias": "bias", "phase": "phase"}


def _explain_text(words):
    """The beginner meaning of each trading term mentioned. Definitions only, from plainwords: no market facts."""
    keys = plainwords.terms_in(words)
    if not keys:
        return None
    return "\n".join(plainwords.term(k)[:1].upper() + plainwords.term(k)[1:] + "." for k in keys)


def _capability_text(words):
    """What the engine saves for the concept(s) asked about, from plainwords.RECORDED. Says plainly when it saves nothing."""
    keys = [k for k in plainwords.terms_in(words) if _TERM_CONCEPT.get(k)]
    if not keys:
        return None
    return "\n".join(plainwords.recorded(_TERM_CONCEPT[k]) for k in keys if plainwords.recorded(_TERM_CONCEPT[k]))


def _compare_text(root, catalog, wants, now):
    hpath = os.path.join(root, STATE_HISTORY_NAME)
    history = ledger.read(hpath)[0] if os.path.exists(hpath) else []
    res = mr.last_structure_events(_read_digests(root),
                                   _read_jsonl(os.path.join(root, "thesis_context_log.jsonl")),
                                   _read_jsonl(os.path.join(root, "zone_log.jsonl")), history, now)
    return mr.render_compare_prev(res, wants)


def _integrity_history(root, now):
    """-> [{probe, severity, summary, opened, closed or None}] oldest first, from the append-only integrity ledger, as of `now`."""
    recs, _ = ledger.read(os.path.join(root, "integrity_ledger.jsonl"))
    seen, order = {}, []
    for r in ledger.as_of(recs, now):
        p, et = r.get("payload") or {}, r.get("event_type")
        try:
            at = ledger.to_utc(r["recorded_at"])
        except (KeyError, ledger.LedgerError):
            continue
        if et == "INTEGRITY_FINDING":
            fp = p.get("fingerprint")
            seen[fp] = {"probe": p.get("probe"), "severity": p.get("severity"), "summary": p.get("summary"), "opened": at, "closed": None}
            order.append(fp)
        elif et == "INTEGRITY_RESOLVED" and p.get("fingerprint") in seen and seen[p["fingerprint"]]["closed"] is None:
            seen[p["fingerprint"]]["closed"] = at
    return [seen[fp] for fp in order]


def _inspector_last_run(root):
    m = _read_json(os.path.join(root, "inspector_state.json")) or {}
    try:
        return ledger.to_utc(m["last_run_at"]), m.get("runs")
    except (KeyError, TypeError, ledger.LedgerError):
        return None, m.get("runs")


def _integrity_text(root, ask, now, recs=None, uid=None, words=""):
    """Questions about the integrity inspector, answered from its own records and from plainwords' description of what it checks. Never a market fact,
    and never 'all clear' without being honest about what that covers. Conversational: each answer leads with the answer to THIS question, and a caveat
    or a fact already given earlier in the conversation is not repeated. -> (text, context)."""
    lc = _last_context(recs or [], now)
    said = set((lc or {}).get("said") or []) if lc and lc.get("kind") == "integrity" else set()
    hist = _integrity_history(root, now)
    openf = [h for h in hist if h["closed"] is None]
    last_run, _runs = _inspector_last_run(root)
    pick = (lambda opts: opts[(sum(map(ord, str(uid or ""))) + len(said)) % len(opts)])
    if last_run:
        mins = int((now - last_run).total_seconds() // 60)
        ran = f"The checks last ran {mr._when(last_run, now)}" + (f", {mins} minutes ago." if mins < 360 else ".")
        if (now - last_run).total_seconds() > 3 * 3600 and 7 <= now.hour < 16 and now.weekday() < 5:
            ran += " That is longer ago than it should be during the scanner's hours, so don't read 'clear' as current."
    else:
        ran = "I can't see when the checks last ran, so 'clear' can only mean clear as of whenever they last did."
    def one(h, with_close=True):
        gloss = plainwords.probe_gloss(h["probe"]) or h["probe"]
        sev = plainwords.severity(h["severity"])[1].lower()
        close = f"; closed {mr._when(h['closed'], now)} because the condition it checks was no longer true" if (with_close and h["closed"]) else ""
        return f"• {sev}: {gloss}, first seen {mr._when(h['opened'], now)}{close}. The inspector's words: ‹{h['summary']}›"
    limits = "\n".join("• " + x for x in plainwords.inspector_limits())
    short_limit = "It can't see a believable-looking wrong number, an impossible value that isn't a zero, or every source being wrong together."
    new_said = set(said) | {ask}
    out = []
    if ask == "checks":
        out.append(pick(["It reads the engine's saved state after each scan and looks for three kinds of trouble:",
                         "Any of these would set it off. It reads the engine's saved state after each scan and looks for:"]))
        out.append("• numbers that look never filled in (a price stuck at exactly 0.0, a field gone empty)\n"
                   "• things that don't line up (parts of the engine disagreeing on direction, a time in the future, an id that no longer matches its numbers, "
                   "the file written by different code than is running)\n"
                   "• the file changing shape, a state sitting far longer than any before it, or a list that only ever grows\n"
                   "It also runs a self-test on the belief logic. It only reads and reports; it never changes anything.")
        if "limits" not in said:
            out.append("Ask 'what can't it see' for its blind spots.")
    elif ask == "limits":
        lead = "No. " if re.search(r"\bsame\b|\bmean (?:nothing|everything|no)\b|\bequal", words or "", re.I) else ""
        out.append(f"{lead}'All clear' means none of the checks found a problem in the latest saved state. It doesn't mean every reading is right. What it can't see:\n{limits}")
        if openf:
            out.append(f"Also, it isn't clear right now: {len(openf)} finding(s) are open.")
        new_said |= {"limits"}
    elif ask == "history":
        if not hist:
            out.append("It hasn't raised anything in the records I can see." if "never" not in said else "Still nothing: it hasn't raised anything in the records I can see.")
            new_said |= {"never"}
        else:
            n_open = len(openf)
            out.append((f"It has raised {len(hist)} finding(s) so far, and {n_open} are still open." if n_open else
                        f"It has raised {len(hist)} finding(s) so far, none open now.") + " The latest:\n" + "\n".join(one(h) for h in reversed(hist[-3:])))
        if "ran" not in said:
            out.append(ran)
        new_said |= {"ran"}
    else:                                                          # why_clear / status / trust
        if openf:
            out.append(("It isn't saying clear. " if ask == "why_clear" else "") + (f"{len(openf)} findings are open right now:" if len(openf) != 1 else "One finding is open right now:"))
            out.append("\n".join(one(h, False) for h in openf[:5]) + ("\n• …and more in the integrity ledger." if len(openf) > 5 else ""))
            out.append("Whether the market reading itself is right is not something it checks.")
        else:
            if ask == "why_clear":
                out.append(pick(["Because the last run of the checks found nothing wrong in the engine's saved state.",
                                 "Nothing is open: the last run of the checks found nothing wrong in the saved state."]))
            elif ask == "trust":
                out.append(pick(["Partly. The saved state passes every check I run on it, which is narrower than 'everything is right'.",
                                 "I can vouch for less than 'everything is fine'. The saved state passes every check, and that's the extent of it."]))
            else:
                out.append("Nothing is open right now: the last run of the checks found nothing wrong in the saved state.")
            if hist and hist[-1]["closed"]:
                last = hist[-1]
                out.append(f"The last thing it raised was {plainwords.probe_gloss(last['probe']) or last['probe']} ({mr._when(last['opened'], now)}); it closed {mr._when(last['closed'], now)}.")
            elif not hist and "never" not in said:
                out.append("It hasn't raised anything in the records I can see.")
            if "ran" not in said:
                out.append(ran)
            if "limits" not in said:
                out.append(short_limit + " Ask 'what can't it see' for the full list.")
            if ask == "trust":
                out.append("What I can vouch for more firmly: I only tell you what the engine recorded, with the time it recorded it. If you meant the market itself, ask 'what's happening'.")
        new_said |= ({"ran", "limits"} if not openf else {"ran"}) | ({"never"} if not hist else set())
    return "\n".join(out), {"kind": "integrity", "said": sorted(new_said)}


def _concept_part(d, recs, now, root=None, catalog=None):
    """explain / explain_prev / capability / compare_prev as one answer. -> (text, context or None)."""
    k = d["kind"]
    lc = _last_context(recs, now)
    wants = (lc or {}).get("wants") if lc and lc.get("kind") in ("event", "market") else None
    if k == "explain":
        t = _explain_text(d["words"])
        return (t or "That's not a term I have a plain definition for. I'd rather not guess at it. Ask me about a CHoCH, BOS, sweep, FVG, order block, "
                "displacement, protected high or low, or the engine's bias or phase."), None
    if k == "explain_prev":
        if not wants:
            return "Which part? Tell me the word you want explained, like 'what does CHoCH mean'.", None
        t = _explain_text(" ".join(_WANT_TERM[w] for w in wants if w in _WANT_TERM))
        return t or "Tell me the word you want explained and I'll put it simply.", None
    if k == "capability":
        t = _capability_text(d["words"])
        return t or "I'm not sure which concept you mean, so I won't guess what the engine saves. Name it, like 'do you track sweeps'.", None
    if k == "cannot_say":
        if d.get("why") == "inject":
            return ("I can't skip my checks: everything I tell you has to come from what the engine actually recorded, so I won't say something just because "
                    "it was asked for. Ask me what it recorded and I'll tell you plainly."), None
        if d.get("why") == "predict":
            return ("I don't predict where price will go, and I can't tell you whether to buy or sell. What I can do is tell you what the engine reads right now "
                    "and what it says it is waiting for: ask 'what's happening' or send /watch."), None
        if d.get("why") == "cause":
            return _cause_text(root, d.get("words", ""), now), None
        return ("I won't call it real or fake. The engine saves what it detected, and when, but it doesn't save a verdict on whether a "
                "break or sweep was genuine, so any verdict from me would be a guess. I can tell you what it recorded and when."), None
    if k == "compare_prev":
        w = d.get("events") or wants
        if not w:
            return "Compare what with what? Ask about the last CHoCH, sweep, FVG or order block first, then ask me to compare it with the one before.", None
        return _compare_text(root, catalog, w, now), None
    return "", None



def _prev_event_text(root, catalog, d, recs, now):
    """'The one before that': the same reader, as of just before the event we were last looking at. -> (text, context)."""
    lc = _last_context(recs, now)
    wants = d.get("events") or ((lc or {}).get("wants") if lc else None)
    anchor = ledger.to_utc(lc["at"]) if lc and lc.get("at") else None
    if not wants or anchor is None:
        return "The one before which? Ask about the last CHoCH, sweep, FVG or order block first, then ask for the one before it.", None
    text, ctx = _last_event_text(root, catalog, wants, now, before=anchor)
    kind = {"choch": "change of character", "bos": "break", "sweep": "sweep check", "fvg": "fair value gap", "ob": "order block"}.get(wants[0], "event")
    return f"(Going back from the {kind} first seen {mr._when(anchor, now)}, the one we were just looking at.)\n" + text, ctx


def _cause_text(root, words, now):
    """'Did X cause Y?': the recorded ORDER of the two, and that order is not cause."""
    keys = plainwords.terms_in(words)
    wants = sorted({k2 for k2 in (_TERM_CONCEPT.get(k) for k in keys) if k2 in ("choch", "bos", "sweep", "fvg", "ob")})
    vague = bool(re.search(r"\b(?:the )?break\b", words, re.I)) and not ({"bos", "choch"} & set(wants))
    if vague:
        wants = sorted(set(wants) | {"bos", "choch"})
    hpath = os.path.join(root, STATE_HISTORY_NAME)
    history = ledger.read(hpath)[0] if os.path.exists(hpath) else []
    res = mr.last_structure_events(_read_digests(root), _read_jsonl(os.path.join(root, "thesis_context_log.jsonl")),
                                   _read_jsonl(os.path.join(root, "zone_log.jsonl")), history, now)
    ev = mr.event_times(res, wants) if wants else []
    head = "I can't say whether one thing caused another: the engine saves each of these separately and nothing in its records links them."
    if len(ev) >= 2:
        lines = "; then ".join(f"{lab} {mr._when(t, now)}" for lab, t in ev)
        which = " 'The break' could mean a break of structure or a change of character, so I've listed both." if vague else ""
        return (head + which + f" What I do have is when each was recorded, in order: {lines}. Coming before or after another event, or around the same time, "
                "is not evidence that it caused it.")
    return head + " I can show you when each was recorded: ask 'when was the last sweep' or 'when was the last CHoCH' and compare the times yourself."


def _zone_state_text(root, d, recs, now):
    """'Was price inside the FVG then?' answered from the saved scan at that moment. -> (text, context)."""
    kind = d.get("zone")
    if kind is None:
        lc = _last_context(recs, now)
        wz = [w for w in ((lc or {}).get("wants") or []) if w in ("fvg", "ob")]
        if not wz:
            return "Inside which one? Ask about the last fair value gap or order block first, or say 'was price inside the fvg at 9:30'.", None
        kind = wz[0]
    digests = _read_digests(root)
    zones = _read_jsonl(os.path.join(root, "zone_log.jsonl"))
    when, note = None, ""
    w = d.get("window")
    if w is not None:
        # the moment the user named, NOT the middle of a window that may have been cut off at "now"
        # ("at 14:30" asked at 14:40 gives the window 14:00-14:40, whose middle is 14:20: a wrong scan, silently)
        when = {"around": w.start + timedelta(minutes=30), "up_to": w.end, "after": w.start}.get(w.kind) or (w.start + (w.end - w.start) / 2)
        note = " ".join(w.assumptions[:1]) + ". " if w.assumptions else ""
    elif d.get("then"):
        lc = _last_context(recs, now)
        if lc and lc.get("at"):
            when = ledger.to_utc(lc["at"])
        else:
            return "Then when? Give me a time, like 'was price inside the fvg at 9:30', or ask about the last FVG first.", None
    else:
        ts = [mr._t(r.get("scanned_at")) for r in digests]
        ts = [t for t in ts if t is not None and t <= now]
        if not ts:
            return "I have no saved scans to look in.", None
        when = max(ts)
        note = "No time given, so this is my latest saved scan. "
    res = mr.zone_state_at(digests, zones, when, kind, now)
    ev_note = _evidence_note(digests, when - mr.SCAN_MAX_GAP, when + mr.SCAN_MAX_GAP)
    if ev_note:
        res = dict(res, text=res["text"] + " " + ev_note)
    pre = ""
    lc = _last_context(recs, now)
    zc = (lc or {}).get("zone") if lc else None
    if d.get("ref") and zc and zc.get("type") == kind:
        fs = ledger.to_utc(zc["first_seen"])
        if when < fs:
            pre = (f"That {'fair value gap' if kind == 'fvg' else 'order block'} wasn't logged until {mr._when(fs, now)}, after the time you asked about, "
                   "so I can't say whether price was inside it then. What the scan at that time shows about whatever the engine had in view: ")
    ctx = {"kind": "event", "wants": [kind], "at": ledger._iso(when)}
    if res.get("zone"):                        # "the one before that" then means the one before the zone we just discussed
        ctx["at"] = ledger._iso(res["zone"]["at"])
        ctx["zone"] = {"type": kind, "key": res["zone"]["key"], "first_seen": ledger._iso(res["zone"]["at"])}
    return note + pre + res["text"], ctx


_PART_LABEL = {"ask_bus": "Right now:", "after_prev": "After that:"}


def _run_multi(root, catalog, d, uid, now, recs, slang, sent_at):
    """Answer every part of a several-part message, in the order asked; a part that cannot be answered is SAID, never dropped.
    -> (bus.Reply, extra payload, parts for the guard)."""
    texts, parts, anchor, ctx_out, pending = [], [], None, None, None
    for i, p in enumerate(d["parts"]):
        k, body = p["kind"], ""
        if k == "last_event":
            body, c = _last_event_text(root, catalog, p["events"], now, p.get("mode", "last"), p.get("window"))
            ctx_out = c
            anchor = ledger.to_utc(c["at"]) if c.get("at") else None
        elif k == "prev_event":
            body, c = _prev_event_text(root, catalog, p, recs, now)
            if c:
                ctx_out = c
                anchor = ledger.to_utc(c["at"]) if c.get("at") else None
        elif k == "ask_bus":
            rep = _ask_bus(root, catalog, p["canonical"], now, uid, recs, sent_at, lead=False)
            body = rep.text
        elif k == "after_prev":
            prior = anchor
            if prior is None:
                lc = _last_context(recs, now)
                prior = ledger.to_utc(lc["at"]) if lc and lc.get("kind") == "event" and lc.get("at") else None
            if prior is None:
                body = ("For the 'after that' part I need something to count from: I found no event to go on. Tell me a time, like "
                        "'what happened after 8:30'.") if anchor is None and ctx_out is not None else \
                       "For the 'after that' part: after what? Give me an event or a time, like 'what happened after 8:30'."
            else:
                body, c2 = _after_text(root, catalog, prior, now)
                ctx_out = {"kind": "market", **{k2: c2[k2] for k2 in ("start", "end")}, **({"wants": ctx_out["wants"]} if ctx_out and ctx_out.get("wants") else {})}
        elif k == "market_recon":
            w = p["window"]
            adapted, _ = collect(root, catalog, now)
            body, c2 = _market_text(root, catalog, adapted, w.start, w.end, now, w.assumptions)
            ctx_out = c2
        elif k == "zone_state":
            body, c = _zone_state_text(root, p, recs, now)
            ctx_out = c or ctx_out
        elif k in ("explain", "explain_prev", "capability", "compare_prev", "cannot_say"):
            body, _c = _concept_part(p, recs, now, root, catalog)
            if k == "explain_prev" and ctx_out and ctx_out.get("wants"):
                body = _explain_text(" ".join(_WANT_TERM[w] for w in ctx_out["wants"] if w in _WANT_TERM)) or body
        elif k == "convo_recon":
            body = "I can go back over a conversation, but I need that on its own: ask 'what did we talk about' as a separate message."
        elif k == "need_time":
            body = "For that part I need a time: something like 'what happened between 8 and 8:30'."
        elif k == "offer_save":
            quote = cs.quote(p["clause"], 160)
            pending = {"kind": "save", "options": [{"id": "1", "label": "save it as a note of yours",
                                                    "action": {"type": "know", "text": p["clause"], "at": ledger._iso(sent_at) if sent_at is not None else None}}]}
            body = f"Also, \"{quote}\" sounds like something you noticed or expect. Want me to save it as a note of yours, word for word? (yes or no)"
        else:
            body = f"I didn't catch this part: \"{cs.quote(p['clause'], 120)}\". Say it another way and I'll take it separately."
        label = _PART_LABEL.get(k) if i > 0 else None
        texts.append((label + "\n" if label else "") + body)
        parts.append((k, body))
    extra = {}
    if ctx_out:
        extra["context"] = ctx_out
    if pending:
        extra["pending"] = pending
    return bus.Reply("\n\n".join(texts), None), extra, parts


def _compare_line(root, adapted, state, now):
    """Facts only, set beside a note the user just saved: what the engine's own saved reading and saved candles show. It never says who is
    right. Empty when the engine's data is too old to call 'right now'."""
    ts = state.get("market_data_timestamp")
    try:
        age = (now - ledger.to_utc(ts)).total_seconds()
    except Exception:
        return ""
    if age > PLAN_SNAPSHOT_MAX_AGE_S or age < -60:
        return "\n\nI didn't set it beside the engine's reading because the engine's saved data is too old to call 'right now'."
    bits = []
    bias = next((c.value for c in adapted.claims if c.subject == "htf.bias"), None)
    if bias:
        bits.append(f"its higher-timeframe bias is {str(bias).lower()}")
    candles = _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["5m"]))
    rep = mr.reconstruct_window(now - timedelta(hours=1), now, candles, [], [], [], [], now)
    pr = rep.get("price")
    if pr:
        d = "up" if pr["net"] > 0 else "down" if pr["net"] < 0 else "flat"
        bits.append("in its saved 5-minute candles price is " + ("flat" if d == "flat" else f"{d} {abs(pr['net_pips'])} pips") +
                    f" over the last hour (high {mr._p(pr['high'])}, low {mr._p(pr['low'])})")
    if not bits:
        return ""
    return ("\n\nFor comparison, facts only: the engine's own saved reading says " + " and ".join(bits) +
            ". That's the engine's chart, not your screen, and I'm not saying whether you or it is right.")


def _run_option(root, catalog, opt, now, uid, recs, slang, sent_at=None):
    a = opt["action"]
    t = a["type"]
    if t == "know":
        saved = bus.know_reply(catalog, a["text"], now, received_at=a.get("at"))
        try:
            adapted, state = collect(root, catalog, now)
            saved += _compare_line(root, adapted, state, now)
        except Exception as e:                                   # the comparison is a bonus; the note is already saved
            print(f"[BUS COMPARE] {type(e).__name__}: {e}")
        return bus.Reply(saved, None), None
    if t == "answer":
        return _ask_bus(root, catalog, a["canonical"], now, uid, recs, a.get("sent_at") and ledger.to_utc(a["sent_at"])), None
    if t == "act":
        cmds = a.get("commands") or [a["command"]]
        adapted, state = collect(root, catalog, now)
        waited = (now - sent_at).total_seconds() if sent_at is not None else None
        facts = _plan_facts(adapted, state, now) if (waited is not None and waited <= PLAN_SNAPSHOT_MAX_AGE_S) else None
        reps = [bus.respond(c, adapted.claims, adapted.gaps, now, catalog=catalog, last_subject=_last_subject(recs),
                            plan_facts=facts, candle_counts=candle_counts(root), received_at=sent_at) for c in cmds]
        if len(reps) == 1:
            return reps[0], None
        return bus.Reply("\n\n".join(r.text for r in reps), None), None
    if t == "ask_capture":
        return bus.Reply(bus.research_question_reply(catalog, "/ask", a["text"], now), None), None
    if t == "convo":
        text, ctx = _replay_text(catalog, a["key"], ledger.to_utc(a["start"]), ledger.to_utc(a["end"]), a["label"], {uid}, now)
        return bus.Reply(text, None), ctx
    if t == "market":
        adapted, _ = collect(root, catalog, now)
        text, ctx = _market_text(root, catalog, adapted, ledger.to_utc(a["start"]), ledger.to_utc(a["end"]), now)
        return bus.Reply(text, None), {"context": ctx}
    return bus.Reply(voice.unknown(uid), None), None


def handle_plain(root, catalog, text, now, uid, recs, sent_at=None):
    """A message that is not a /command. -> (bus.Reply, extra payload for the stored BUS_REPLY)."""
    slang = catalog.slang_map()
    pending = _open_pending(recs, now)
    if pending:
        fresh = converse.interpret(text, now, slang)["kind"] in ("ask_bus", "market_recon", "convo_recon", "followup", "need_time", "act_offer", "act_missing", "alert_unsupported", "last_event", "multi", "explain", "explain_prev", "capability", "compare_prev", "cannot_say", "zone_state", "details", "prev_event", "integrity")
        how, opt = converse.resolve(pending["options"], text, slang, strict=fresh)
        if how == "option":
            rep, ctx = _run_option(root, catalog, opt, now, uid, recs, slang, sent_at)
            return rep, {"resolves": pending["uid"], **({"context": ctx} if ctx and "context" not in ctx else (ctx or {}))}
        if how == "cancel":
            return bus.Reply("No problem, I've left it.", None), {"resolves": pending["uid"]}
        if how == "ambiguous":
            labels = "; ".join(f"{o['id']}. {o['label']}" for o in pending["options"])
            return bus.Reply(f"Which one do you mean? {labels}. Just say the number.", None), {"pending": {k: v for k, v in pending.items() if k != "uid"}}
    d = converse.interpret(text, now, slang)
    k = d["kind"]
    ictx = _last_context(recs, now)
    h0 = ictx.get("h") if isinstance(ictx, dict) and ictx.get("kind") == "idea" and isinstance(ictx.get("h"), dict) else None
    if h0 and _SAVE_IDEA.match(text or ""):
        return _idea_save(catalog, h0, now, sent_at)
    itext = informal.normalise(text, slang)["text"] or text          # the message itself, not the part left after a correction was stripped
    parts = re.split(r"(?<=[.?!])\s+", (text or "").strip())
    if len(parts) >= 2 and not (text or "").lstrip().startswith("/") and k not in ("command", "smalltalk"):
        lead = []
        for sent in parts:
            if ir.is_idea(sent, converse.interpret(sent, now, slang)["kind"], h0) and not ir._RECORDS_Q.search(sent.lower()):
                lead.append(sent)
            else:
                break
        rest = parts[len(lead):]
        if lead and rest and not ir._NEG_CMD.search((text or "").lower()):
            body, ctx = _idea_text(root, " ".join(lead), now, h0)
            while rest and ir.is_idea(rest[0], converse.interpret(rest[0], now, slang)["kind"], ctx["h"]) and not ir._RECORDS_Q.search(rest[0].lower()):      # a following sentence that continues the idea stays in the idea
                b2, ctx = _idea_text(root, rest[0], now, ctx["h"])
                body += "\n\n" + b2
                rest = rest[1:]
            if not rest:
                return bus.Reply(body, None), {"context": ctx}
            rep2, extra2 = handle_plain(root, catalog, " ".join(rest), now, uid, recs, sent_at)
            extra2 = dict(extra2 or {})
            extra2["context"] = ctx                               # the idea stays the thread of the conversation
            return bus.Reply(body + "\n\nAnd for the rest of your message:\n" + rep2.text, None), extra2
    if ir.is_idea(itext, k, h0):
        body, ctx = _idea_text(root, itext, now, h0)
        return bus.Reply(body, None), {"context": ctx}
    if k == "ask_bus":
        rep = _ask_bus(root, catalog, d["canonical"], now, uid, recs, sent_at)
        return rep, {}
    if k == "market_recon":
        w = d["window"]
        adapted, _ = collect(root, catalog, now)
        body, ctx = _market_text(root, catalog, adapted, w.start, w.end, now, w.assumptions)
        return bus.Reply(f"{voice.lead('reconstruct', uid)}\n{body}", None), {"context": ctx}
    if k == "convo_recon":
        body, extra = _convo_reply(catalog, d, now, uid)
        return bus.Reply(body, None), (extra or {})
    if k == "need_time":
        return bus.Reply("Happy to go back through it. Which stretch do you mean? You can say something like 'between 8 and 8:30', "
                         "'around 14:00 yesterday', or 'after the 9:15 move'. I use Lagos time.", None), {}
    if k == "multi":
        rep, extra, parts = _run_multi(root, catalog, d, uid, now, recs, slang, sent_at)
        extra["_parts"] = parts
        return rep, extra
    if k == "last_event":
        body, ctx = _last_event_text(root, catalog, d["events"], now, d.get("mode", "last"), d.get("window"))
        return bus.Reply(body, None), {"context": ctx}
    if k in ("explain", "explain_prev", "capability", "compare_prev", "cannot_say"):
        body, _c = _concept_part(d, recs, now, root, catalog)
        return bus.Reply(body, None), {}
    if k == "integrity":
        body, ctx = _integrity_text(root, d["ask"], now, recs, uid, d.get("words", ""))
        return bus.Reply(body, None), {"context": ctx}
    if k == "prev_event":
        body, ctx = _prev_event_text(root, catalog, d, recs, now)
        return bus.Reply(body, None), ({"context": ctx} if ctx else {})
    if k == "details":
        ctx = _last_context(recs, now)
        if not ctx or ctx.get("kind") != "market" or not ctx.get("start"):
            return bus.Reply("Details of what? Ask me to go back over a stretch first, like 'what happened between 8 and 8:30', and I'll give the short version, then the full timeline on request.", None), {}
        adapted, _ = collect(root, catalog, now)
        body, c2 = _market_text(root, catalog, adapted, ledger.to_utc(ctx["start"]), ledger.to_utc(ctx["end"]), now, brief=False)
        return bus.Reply(f"{voice.lead('reconstruct', uid)}\n{body}", None), {"context": c2}
    if k == "zone_state":
        body, ctx = _zone_state_text(root, d, recs, now)
        return bus.Reply(body, None), ({"context": ctx} if ctx else {})
    if k == "followup":
        ctx = _last_context(recs, now)
        if ctx and ctx.get("kind") == "event":
            if not ctx.get("at"):
                return bus.Reply("I found no event in my records to count from, so there's no 'after' to look at. Give me a time, like 'what happened after 8:30'.", None), {}
            body, c2 = _after_text(root, catalog, ledger.to_utc(ctx["at"]), now)
            return bus.Reply(f"{voice.lead('reconstruct', uid)}\n{body}", None), {"context": c2}
        if not ctx or ctx.get("kind") != "market":
            return bus.Reply("After what? Tell me the time, like 'what happened after 8:30', or ask me to go back over a stretch first.", None), {}
        start = ledger.to_utc(ctx["end"])
        end = min(start + timedelta(hours=1), now)
        adapted, _ = collect(root, catalog, now)
        body, c2 = _market_text(root, catalog, adapted, start, end, now, ["I carried on for an hour from where we stopped"])
        return bus.Reply(f"{voice.lead('reconstruct', uid)}\n{body}", None), {"context": c2}
    if k == "act_missing":
        what = " and ".join(d["missing"])
        return bus.Reply(f"{voice.lead('clarify', uid)} That sounds like a trade you took, but I'm missing {what}. "
                         "Send it again with all three, like: I sold GBPUSD at 1.3240, stop 1.3270, target 1.3180.", None), {}
    if k == "smalltalk":
        return bus.Reply(voice.greet(uid) if d["tone"] == "greet" else voice.thanks(uid), None), {}
    if k == "alert_unsupported" and d.get("why") == "direction":
        return bus.Reply("I can alert you on the next change of character, but not in a chosen direction: the engine's saved scans say a change of character "
                         "is showing, not whether it's bullish or bearish, so I'd only be guessing. Say 'alert me at the next change of character in any direction' "
                         "and I'll set that one. Nothing was saved.", None), {}
    if k == "alert_unsupported" and d.get("why") == "zone_entry":
        nz = "No zone has been fixed in this conversation yet, so I'd also need to know which one you mean. " if h0 else ""
        return bus.Reply(nz + "I can't yet alert you when price enters a fair value gap or order block: the engine saves each zone when it first sees it, but there's no saved "
                         "record of price entering one, so I'd be guessing. What I can do: alert you when a NEW one forms (say 'alert me at the next bullish FVG'), or, if you "
                         "give me the zone's prices, alert you when price trades into them (/alert zone 1.3380 1.3400). Nothing was saved.", None), {}
    if k == "alert_unsupported" and d.get("why") == "zone_tf":
        return bus.Reply(f"The engine only records fair value gaps and order blocks on the 15-minute, not the {d.get('tf')}, so I can't watch the {d.get('tf')} one. "
                         "I can alert you when a new 15-minute one forms. Nothing was saved.", None), {}
    if k == "alert_unsupported":
        return bus.Reply("I can set price alerts, but I couldn't read that one. I can watch: price going above or below a level (alert me when price goes above 1.3400), "
                         "touching a level, a candle closing beyond a level, price moving a number of pips from now (alert me when price moves 10 pips), "
                         "a new change of character (any direction), the break-of-structure direction changing, a new fair value gap or order block forming on the 15-minute, or price against an EMA. Nothing was saved. Try one of those in your own words.", None), {}
    if k == "act_offer":
        return _offer_act(root, catalog, d, uid, text, now, recs, sent_at)
    if k == "offer_save":
        quote = cs.quote(text, 160)
        opt = [{"id": "1", "label": "save it as a note of yours",
                "action": {"type": "know", "text": text, "at": ledger._iso(sent_at) if sent_at is not None else None}}]
        return bus.Reply(f"{voice.lead('clarify', uid)} That sounds like something you noticed or expect: \"{quote}\". "
                         "Want me to save it as a note of yours, word for word? (yes or no)", None), {"pending": {"kind": "save", "options": opt}}
    return bus.Reply(voice.unknown(uid), None), {}


_SAVE_IDEA = re.compile(r"\s*(?:please )?(?:yes,? )?(?:save|note|keep|log|record|remember) (?:it|that|this|the idea|my idea)(?: (?:as a note|for me|word for word))?\s*[.!]?\s*$", re.I)


def _idea_facts(root, now):
    """-> counts from the saved records for ir.review (never an opinion). Empty facts when the archives are missing or unreadable."""
    try:
        digs = _read_digests(root)
        unread, confl = getattr(digs, "unreadable", ()), getattr(digs, "conflicts", ())
        res = mr.last_structure_events(digs, _read_jsonl(os.path.join(root, "thesis_context_log.jsonl")),
                                       _read_jsonl(os.path.join(root, "zone_log.jsonl")),
                                       (ledger.read(os.path.join(root, STATE_HISTORY_NAME))[0] if os.path.exists(os.path.join(root, STATE_HISTORY_NAME)) else []), now)
        F = mr.idea_facts(res, digs, _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["15m"])), now, _read_jsonl(os.path.join(root, "thesis_context_log.jsonl")))
    except Exception:
        return {"scans": 0, "events": []}
    try:                                                       # how many times the engine's 1-hour leg changed direction in the saved history
        hp = os.path.join(root, STATE_HISTORY_NAME)
        hist = ledger.read(hp)[0] if os.path.exists(hp) else []
        dirs = [((r.get("payload") or {}).get("macro_leg") or {}).get("direction") for r in sorted(hist, key=lambda r: r.get("ts", "")) if r.get("event_type") == "STATE_SNAPSHOT"]
        dirs = [d for d in dirs if d]
        F["flips_1h"] = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b) if dirs else None
        F["hist_from"] = sorted(r.get("ts", "") for r in hist)[0] if hist else None
    except Exception:
        F["flips_1h"] = None
    F["unreadable"], F["conflicts"] = list(unread), list(confl)
    F["when"] = lambda t: timeref.lagos(t).strftime("%a %d %b %H:%M") + " Lagos time"
    return F


def _idea_text(root, text, now, h0):
    """One turn of talking an idea through. -> (reply text, context). The idea is carried in the context so the next message continues it."""
    ttype = ir.classify(text, h0)
    h = ir.update(h0, text, ttype, at=now.isoformat()) if h0 else ir.start(text)
    if h0 is None and h.get("log"):
        h["log"][0]["at"] = now.isoformat()
    if h0 is None and ttype == "neg_command":
        h = ir.start(text)
        h["conds"] = h["conds"] or ir.terms(text)
    F = _idea_facts(root, now)
    body, _ask = ir.review(h, ttype, text, F, h.get("said"))
    return body, {"kind": "idea", "h": h}


def _idea_save(catalog, h, now, sent_at):
    """Saved only because you said so: your own sentences about the idea, word for word, as a note of yours."""
    saved = bus.know_reply(catalog, " ".join(h.get("texts") or [h.get("text", "")]), now, received_at=ledger._iso(sent_at) if sent_at is not None else None)
    return bus.Reply(saved, None), {"context": {"kind": "idea", "h": h}}


def _latest_price(root, now):
    """-> (close, candle time) of the newest saved 5-minute candle, or (None, None)."""
    best = None
    for r in _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["5m"])):
        try:
            t = ledger.to_utc(r.get("datetime") or r.get("time") or r.get("ts"))
            c = float(r["close"])
        except Exception:
            continue
        if t <= now and (best is None or t > best[0]):
            best = (t, c)
    return (best[1], best[0]) if best else (None, None)


ALERT_FRESH_S = 1800        # a reference price older than this is not "the price now"


def _offer_alert(root, al, uid, now, catalog=None, recs=None, sent_at=None):
    """A clear alert request is set at once (an alert is reversible: /alert cancel N) and said in one short line. Only a request the engine
    cannot honour as asked (another timeframe) is turned into a question, so it is never silently swapped."""
    def ask(text, said, cmds):
        act = {"type": "act", "commands": cmds} if len(cmds) > 1 else {"type": "act", "command": cmds[0]}
        return (bus.Reply(f"{voice.lead('clarify', uid)} {text} Want that? (yes or no)", None),
                {"pending": {"kind": "act", "options": [{"id": "1", "label": said, "action": act}]}})

    def now_set(cmds, lead=""):
        reps = [_run_option(root, catalog, {"action": {"type": "act", "command": c}}, now, uid, recs or [], {}, sent_at)[0] for c in cmds]
        texts = [r.text for r in reps]
        texts = [t.replace(" " + bus.ALERT_COVERAGE_NOTE, "") for t in texts[:-1]] + texts[-1:]        # the hours note once, not per alert
        body = "\n".join(texts)
        return bus.Reply((lead + body).strip(), None), {}

    if al["type"] == "level" and al.get("tf_asked"):
        t = al["tf_asked"]
        words = ({"5m": "5-minute", "1h": "1-hour", "4h": "4-hour", "1d": "daily", "1w": "weekly"}.get(t) or
                 (t[:-1] + "-minute" if t.endswith("m") else t[:-1] + "-hour"))
        what = "change of character" if al["cmd"] == "/alert choch" else "break of structure"
        said = f"set the 15-minute {what} alert (any direction)"
        return ask(f"You asked for the {words}, but the engine only saves its {what} on the 15-minute, so I can't watch the {words} one. "
                   f"I can set the 15-minute {what} alert instead.", said, [al["cmd"]])
    if al["type"] == "level":
        return now_set([al["cmd"]])
    px, at = _latest_price(root, now)
    if px is None or (now - at).total_seconds() > ALERT_FRESH_S:
        when = f"from {timeref.hm(at)} Lagos time" if at else "at all"
        return (bus.Reply("I can't set a 'moves N pips' alert right now because I need today's price to measure from, and my newest saved candle is "
                          f"{'not available' if at is None else when}. Give me a level instead, like: alert me when price goes above 1.3400.", None), {})
    n = al["pips"] * 0.0001
    up, dn = round(px + n, 5), round(px - n, 5)
    cmds, parts = [], []
    if al["dir"] in ("any", "up"):
        cmds.append(f"/alert {up:.5f} above"); parts.append(f"above {up:.5f}")
    if al["dir"] in ("any", "down"):
        cmds.append(f"/alert {dn:.5f} below"); parts.append(f"below {dn:.5f}")
    lead = (f"Price is {px:.5f} (my {timeref.hm(at)} Lagos-time candle), so {al['pips']:g} pips is " + " and ".join(parts) + ". " +
            ("When one hits I'll message you; the other stays open until you cancel it.\n" if len(cmds) == 2 else ""))
    return now_set(cmds, lead)


def _offer_act(root, catalog, d, uid, text, now, recs=None, sent_at=None):
    """Turn a plain-language trade, outcome or reasoning into the exact command, show it in plain words, and wait for a yes."""
    a = d["act"]
    if a == "alert":
        return _offer_alert(root, d["alert"], uid, now, catalog, recs, sent_at)
    if a == "logtrade":
        cmd = f"/logtrade {d['pair']} {d['side']} {d['entry']} sl {d['sl']} tp {d['tp']}"
        said = f"log a {d['side']} on {d['pair']} at {d['entry']}, stop {d['sl']}, target {d['tp']}"
    else:
        no = d.get("case_no")
        if no is None:
            latest = catalog.latest_case_id()
            if latest is None:
                return bus.Reply("You haven't logged a trade yet, so there's nothing to attach that to. Tell me the trade first, like: "
                                 "I sold GBPUSD at 1.3240, stop 1.3270, target 1.3180.", None), {}
            no = int(latest.split("-")[1])
        cid = f"CASE-{no:04d}"
        if a == "outcome":
            r = d.get("r")
            cmd = f"/outcome {no} {d['result']}" + (f" {r}" if r else "")
            said = f"record {cid} as a {'breakeven' if d['result'] == 'be' else d['result']}" + (f" ({r}R)" if r else "")
        else:
            cmd = f"/reason {no} {d['text']}"
            said = f"save this as your reasoning on {cid}, word for word"
    opt = [{"id": "1", "label": said, "action": {"type": "act", "command": cmd}}]
    return (bus.Reply(f"{voice.lead('clarify', uid)} Here's what I understood: {said}. Should I do that? (yes or no)", None),
            {"pending": {"kind": "act", "options": opt}})


def handle_ask_clarify(text, uid, slang, sent_at=None):
    """/ask <a 'right now' question>: ask which it is instead of silently filing it as research."""
    rest = text.strip().partition(" ")[2].strip()
    canon = converse.present_question(rest, slang) if rest else None
    if not canon:
        return None
    opts = [{"id": "1", "label": "answer it now", "action": {"type": "answer", "canonical": canon,
                                                              "sent_at": ledger._iso(sent_at) if sent_at is not None else None}},
            {"id": "2", "label": "save it as a research question", "action": {"type": "ask_capture", "text": rest}}]
    return (bus.Reply(f"{voice.lead('clarify', uid)} That reads like you want an answer right now, but /ask saves research questions to study later. "
                      "Which do you want?\n1. Answer it now\n2. Save it as a research question", None),
            {"pending": {"kind": "ask", "options": opts}})


def handle_reconstruct(root, catalog, text, now, uid, recs, sent_at=None):
    slang = catalog.slang_map()
    rest = text.strip().partition(" ")[2].strip()
    m = re.match(r"^#?(?:case-)?(\d+)(\s+detail)?\s*$", rest, re.I)
    if m:
        return bus.Reply(_case_text(root, catalog, f"CASE-{int(m.group(1)):04d}", bool(m.group(2)), uid, now), None), {}
    if not rest:
        return bus.Reply("What do you want me to go back over? A trade (/reconstruct followed by its number), a stretch of the market "
                         "('between 8 and 8:30'), or a conversation ('what we talked about Tuesday').", None), {}
    if re.search(r"\b(talk|talked|discuss|discussed|said|conversation|chat|spoke)\b", rest, re.I):
        tail = re.sub(r"^(what )?(we |i |you and i )?(talked|talk|discussed|discuss|said|spoke)( about)?\s*", "", rest, flags=re.I)
        asked = "what did we talk about " + (tail or rest)
    elif re.search(r"\bhappen", rest, re.I):
        asked = rest
    else:
        asked = "what happened " + rest
    return handle_plain(root, catalog, asked, now, uid, recs, sent_at)


def _alert_hit_text(v, ev, tid):
    """The event first, in one or two lines. Every number and word comes from the stored condition and its evidence record."""
    cond = v.get("condition") or {}
    at = ledger.to_utc(ev["at"])
    when = f"{timeref.hm(at)} Lagos time"
    if cond.get("type") == "structure_flip":
        sc = f" (scan {ev['scan_id']})" if ev.get("scan_id") is not None else ""
        extra = (f" The break-of-structure direction went from {str(ev['from']).lower()} to {str(ev['to']).lower()}." if ev.get("from") else "")
        return (f"\U0001F514 *Alert hit:* {bus.describe_condition(cond)}\n\U0001F552 First seen in the scan at {when}{sc}.{extra}\n"
                "_The scan record has no candle time, so this is when I saw it, not necessarily when it formed. A detection, not a trade signal._")
    if cond.get("type") == "new_zone":
        ft = ledger.to_utc(ev["formation_time"]) if ev.get("formation_time") else None
        form = f" It formed on the 15-minute candle at {timeref.hm(ft)} Lagos time." if ft else ""
        return (f"\U0001F514 *Alert hit:* {bus.describe_hit(cond, ev)}\n\U0001F552 First logged by the scan at {when}.{form}\n"
                "_Where price is now relative to it is not checked here. A detection, not a trade signal._")
    try:
        return f"\U0001F514 *Alert hit:* {bus.describe_hit(cond, ev)}\n\U0001F552 Seen at {when}."
    except KeyError:
        return f"\U0001F514 *Alert hit:* {tid}\n\U0001F552 At {when}."


def fire_alerts(root, catalog, now, send, summary):
    """User price alerts are catalog threads with a machine-checkable condition. Each scan:
    check open threads against the permanent 5m candle archive (only candles AFTER the thread was
    opened can resolve it — a level already crossed earlier does not fire), and tell the user ONCE.
    The resolution is written first; the message is stored and retried like any other reply."""
    by_tf = {tf: _read_jsonl(os.path.join(root, name)) for tf, name in CANDLE_ARCHIVES.items()}
    events = _read_jsonl(os.path.join(root, "market_event_log.jsonl"))
    digests = _read_digests(root)
    zones = _read_jsonl(os.path.join(root, "zone_log.jsonl"))
    if not any(by_tf.values()) and not events and not digests:
        _stale_notice(catalog, now, send, summary, by_tf, digests)
        return
    for rec in catalog.resolve_open_threads(candles_by_tf=by_tf, events=events, now=now, digests=digests, zones=zones):
        tid = rec["payload"]["thread_id"]
        adapted, _st = collect(root, catalog, now)
        answer = bc.resolve("thread_status", adapted.claims, now, params={"thread_id": tid}, gaps=adapted.gaps)
        reply = bus.finalize(answer)
        ev = rec["payload"]["evidence"]
        text = (_alert_hit_text(answer.items[0].claim.value, ev, tid) if (answer.items and not reply.used_fallback)
                else f"Alert hit: {reply.text}" if answer.items else f"Alert {tid} resolved at {ev.get('at')}.")
        ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(answer), ts=now, now=now)
        uid = f"alert:{rec['event_id']}"
        ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": text, "kind": "alert",
                                                "used_fallback": reply.used_fallback, "violations": list(reply.violations)}, now=now)
        ok = bool(send(text)) if send else False
        ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
        summary["alerts"] = summary.get("alerts", 0) + 1
    _stale_notice(catalog, now, send, summary, by_tf, digests)


ALERT_STALE_S = 1800            # an alert's data older than this, during scanner hours, is "stale": it cannot be said to have been checked


def _newest(rows, key):
    ts = [x for x in (ledger.to_utc(r[key]) if isinstance(r, dict) and r.get(key) else None for r in rows or []) if x]
    return max(ts) if ts else None


def _stale_notice(catalog, now, send, summary, by_tf, digests):
    """If there are open alerts but the data they are checked against has stopped arriving DURING scanner hours, say so once (per stale
    reading), instead of staying silent as if nothing had happened. It never claims an event did or did not occur. It can only run when the
    bot itself runs: if the scanner is not triggered at all, nothing here can speak."""
    n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    n = n.astimezone(timezone.utc)
    if not (n.weekday() < 5 and (7, 0) <= (n.hour, n.minute) < (15, 56)):
        return
    stale = []
    for t in catalog.open_threads():
        cond = t["payload"]["condition"]
        if cond.get("type") in ("structure_flip", "new_zone"):            # zone_log only grows when a zone is new, so scan freshness is read from the per-scan digests
            newest = _newest(digests, "scanned_at")
        else:
            newest = _newest(by_tf.get(cond.get("timeframe", "5m")), "datetime")
        if newest is None or (n - newest).total_seconds() > ALERT_STALE_S:
            stale.append((t["payload"]["thread_id"], newest))
    if not stale:
        return
    oldest = min((x[1] for x in stale if x[1]), default=None)
    uid = f"stale:{oldest.isoformat() if oldest else 'none'}"
    recs, _ = ledger.read(catalog.path)
    if any(r["event_type"] == BUS_REPLY and r["payload"].get("update_id") == uid for r in recs):
        return
    ids = ", ".join(str(int(x[0].split("-")[1])) for x in stale)
    when = f"from {timeref.hm(oldest)} Lagos time" if oldest else "missing"
    text = (f"\u23F3 *Heads up:* alert {ids} is still open, but the newest data I have to check it against is {when}, so I can't confirm whether it has "
            "happened since. I'll check it as soon as fresh data arrives.") if len(stale) == 1 else (
            f"\u23F3 *Heads up:* alerts {ids} are still open, but the newest data I have to check them against is {when}, so I can't confirm whether any "
            "has happened since. I'll check them as soon as fresh data arrives.")
    ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": text, "kind": "alert_stale", "used_fallback": False, "violations": []}, now=now)
    ok = bool(send(text)) if send else False
    ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
    summary["stale_notice"] = 1


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
    text = ("\U0001F6E1 *Integrity alert*\n" + reply.text) if loud_now else "\u2705 Integrity inspector: all clear \u2014 the findings I reported earlier are no longer open."
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
# /taken and /skip: the user's answer to a fired signal (Phase A). The signal stays the engine's record;
# this stores only what the USER did about it, and logs a case whose bot-plan side is the signal as it FIRED.
# ----------------------------------------------------------------------
SIGNAL_COMMANDS = ("/taken", "/skip")
SIGNAL_REPLY_HINT = "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\u2753 Reply /taken if you took it, or /skip if you didn't."


def reconstruct_command_of(text):
    first = (text or "").strip().split(" ", 1)[0].lower().split("@")[0]
    return first == "/reconstruct"


def signal_command_of(text):
    first = (text or "").strip().split(" ", 1)[0].lower().split("@")[0]
    return first if first in SIGNAL_COMMANDS else None


def _price(x):
    return f"{x:.5f}"


def handle_signal_response(catalog, adapted, text, received_at, now):
    """/taken [fill price]   /skip [why]. Applies to the newest fired signal that has no answer yet."""
    cmd = signal_command_of(text)
    rest = (text or "").strip().partition(" ")[2].strip()
    answered = catalog.signal_responses()
    sigs = sorted((c for c in adapted.claims if c.subject == "signal.undelivered"),
                  key=lambda c: (c.observed_at, c.value["event_id"]))
    open_ = [c for c in sigs if c.value["event_id"] not in answered]
    if not open_:
        return "There is no fired signal waiting for an answer."
    c = open_[-1]
    v = c.value
    when = f"{ledger.to_utc(c.observed_at):%H:%M UTC}"
    others = f" ({len(open_) - 1} older unanswered one(s) remain; they are not touched.)" if len(open_) > 1 else ""
    if cmd == "/skip":
        catalog.respond_to_signal(v["event_id"], "SKIPPED", note=rest, signal_fired_at=ledger._iso(ledger.to_utc(c.observed_at)), now=now)
        return f"Noted: you skipped the {v.get('direction', '').lower()} signal from {when}." + (" Your note is stored word for word." if rest else "") + others
    # /taken
    if v.get("entry") is None or v.get("sl") is None or v.get("tp") is None:
        return ("That signal's record doesn't carry an entry, stop and target, so I can't log it for you. "
                "Use /logtrade with your own numbers, then /skip to close this one.")
    entry = v["entry"]
    if rest:
        try:
            entry = float(rest.split()[0])
        except ValueError:
            return "Format: /taken (logs the signal's recorded entry) or /taken 1.31250 (the price you actually got)"
    taken_at = ledger.to_utc(received_at) if received_at is not None else now
    fired = ledger.to_utc(c.observed_at)
    if taken_at < fired:
        return "Your message is timestamped before that signal fired, so I won't log it as taken."
    # Safe to retry: if an earlier attempt wrote the case and was interrupted before the rest, complete it
    # instead of logging a second case. The case carries the signal's id, so there is exactly one per signal.
    case_id = catalog.case_for_signal(v["event_id"])
    if case_id is None:
        try:
            case_id = catalog.log_trade(v.get("pair") or "GBPUSD", v["direction"], entry, v["sl"], v["tp"], taken_at,
                                        now=now, signal_event_id=v["event_id"])
        except ledger.LedgerError as e:
            return f"I didn't log it: {e}"
    else:
        entry = catalog.get_case(case_id)["trade"]["payload"]["entry"]
    if not catalog.has_bot_plan(case_id):
        catalog.attach_bot_plan(case_id, fired, "TRADE", dict(v), now=now)
    catalog.respond_to_signal(v["event_id"], "TAKEN", case_id=case_id, note=("" if entry == v["entry"] else f"filled at {entry} (signal entry {v['entry']})"),
                              signal_fired_at=ledger._iso(fired), now=now)
    fill = "" if entry == v["entry"] else f" You filled at {_price(entry)}; the signal's own entry {_price(v['entry'])} is kept on the bot's side."
    return (f"Logged {case_id} from the {v['direction'].lower()} signal of {when}: entry {_price(entry)}, stop {_price(v['sl'])}, "
            f"target {_price(v['tp'])}.{fill} The bot's side is the signal as it fired, not today's market.{others}")


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
        out_text = reply.text + SIGNAL_REPLY_HINT
        ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": out_text, "kind": "signal_alert",
                                                "used_fallback": reply.used_fallback, "violations": list(reply.violations)}, now=now)
        ok = bool(send(out_text)) if send else False
        ledger.append(catalog.path, BUS_DELIVERED if ok else BUS_DELIVERY_FAILED, {"update_id": uid}, now=now)
        summary["signals_announced"] = summary.get("signals_announced", 0) + 1
        spoken += 1


def _from_owner(msg):
    """True only when the message's chat id is exactly the configured owner's. Anything unreadable, missing or different is NOT the owner (fail closed),
    and with no owner configured nobody is. The transport (scanner_live) filters first; this is the second lock."""
    owner = str(os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    cid = msg.get("chat_id") if isinstance(msg, dict) else None
    if not owner or cid is None or isinstance(cid, (list, dict, bool, float)):
        return False
    return str(cid) == owner


def run(root=".", now=None, send=None, signals=False):
    now = ledger.to_utc(now) if now is not None else datetime.now(timezone.utc)
    catalog = Catalog(os.path.join(root, LEDGER_NAME))
    summary = {"answered": 0, "resent": 0, "delivered": 0, "failed": 0, "errors": 0}
    inbox = _read_jsonl(os.path.join(root, INBOX_NAME))
    seen_this_run = set()

    def deliver(update_id, text):
        text = guard.final(text).text                              # the last gate for EVERYTHING that leaves: no raw codes, no placeholders, no dumps
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
        if not _from_owner(msg):
            print(f"[BUS] message {uid} is not from the configured chat: ignored, not answered, not recorded")
            continue
        recs, _ = ledger.read(catalog.path)
        if uid in _handled_updates(recs):
            continue
        text = (msg.get("text") or "").strip()
        try:
            catalog.record_inbound(uid, text, ts=msg.get("received_at"), now=now)
            extra = {}
            first = text.strip().split(" ", 1)[0].lower().split("@")[0]
            if qm.command_of(text):                                   # /quiet, /loud: handled here, the catalog is not involved
                reply = bus.Reply(handle_quiet(catalog, text, uid, msg.get("received_at"), now), None)
            elif reconstruct_command_of(text):                        # /reconstruct: read-only (a trade, a stretch of market, or a conversation)
                reply, extra = handle_reconstruct(root, catalog, text, now, uid, recs, _sent_at(msg, now))
            elif signal_command_of(text):                             # /taken, /skip: the user's answer to a fired signal
                adapted, _st = collect(root, catalog, now)
                reply = bus.Reply(handle_signal_response(catalog, adapted, text, msg.get("received_at"), now), None)
            elif first == "/ask" and handle_ask_clarify(text, uid, catalog.slang_map(), _sent_at(msg, now)):
                reply, extra = handle_ask_clarify(text, uid, catalog.slang_map(), _sent_at(msg, now))
            elif not text.startswith("/"):                            # plain talk: slang understood, questions answered, doubts asked
                reply, extra = handle_plain(root, catalog, text, now, uid, recs, _sent_at(msg, now))
            else:
                adapted, state = collect(root, catalog, now)
                sent_at = _sent_at(msg, now)
                # A command read long after it was sent must not get "what the engine had when you took it": the engine's
                # state now is not the state then. No snapshot is attached rather than a misleading one.
                waited = (now - sent_at).total_seconds() if sent_at is not None else None
                facts = _plan_facts(adapted, state, now) if (waited is not None and waited <= PLAN_SNAPSHOT_MAX_AGE_S) else None
                reply = bus.respond(text, adapted.claims, adapted.gaps, now, catalog=catalog,
                                    last_subject=_last_subject(recs),
                                    plan_facts=facts, candle_counts=candle_counts(root), received_at=sent_at)
                if reply.answer is not None and bus.route(text).kind == "intent":
                    reply = bus.Reply(voice.wrap("answer", _late_note(sent_at, now, bus.route(text).intent) + reply.text, uid), reply.answer, reply.used_fallback, reply.violations)
            kind = "answer" if reply.answer is not None else "other"
            if reply.answer is not None:
                ledger.append(catalog.path, bc.BUS_STATEMENT, bc.statement_payload(reply.answer), ts=now, now=now)
            parts = extra.pop("_parts", None) if isinstance(extra, dict) else None
            g = guard.review(reply.text, "report" if (parts or text.startswith("/") or "context" in (extra or {})) else "answer", parts)
            if g.blocked or g.repaired:
                print(f"[BUS GUARD] update {uid}: {list(g.violations)}{' (blocked)' if g.blocked else ''}")
            payload = {"update_id": uid, "text": g.text, "kind": kind, "used_fallback": reply.used_fallback or g.blocked,
                       "violations": list(reply.violations), **(extra or {})}
            if g.violations:
                payload["guard"] = list(g.violations)
                if g.repaired or g.blocked:
                    payload["guard_original"] = reply.text[:600]          # what the handler actually built, kept as evidence for the inspector
            ledger.append(catalog.path, BUS_REPLY, payload, now=now)
            reply = bus.Reply(g.text, reply.answer, reply.used_fallback or g.blocked, reply.violations)
            summary["answered"] += 1
            if reply.used_fallback:
                print(f"[BUS] audit fallback for update {uid}: {list(reply.violations)}")
            deliver(uid, reply.text)
        except Exception as e:
            summary["errors"] += 1
            err = "Something went wrong on my side while handling that, and nothing was changed. It's logged so it can be fixed. Try again, or ask it another way."
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
