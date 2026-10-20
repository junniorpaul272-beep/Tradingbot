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
import informal
import ledger
import market_reconstruct as mr
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


def _market_text(root, catalog, adapted, start, end, now, assumptions=()):
    candles = _read_jsonl(os.path.join(root, CANDLE_ARCHIVES["5m"]))
    events = _read_jsonl(os.path.join(root, "market_event_log.jsonl"))
    hpath = os.path.join(root, STATE_HISTORY_NAME)
    history = ledger.read(hpath)[0] if os.path.exists(hpath) else []
    recs, _ = ledger.read(catalog.path)
    rep = mr.reconstruct_window(start, end, candles, events, history, recs, _signals_of(adapted), now)
    ctx = {"kind": "market", "start": ledger._iso(start), "end": ledger._iso(end)}
    return mr.render_window(rep, assumptions), ctx


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


def _ask_bus(root, catalog, canonical, now, uid, recs, sent_at=None):
    adapted, _state = collect(root, catalog, now)
    r = bus.route(canonical)
    if r.kind == "unknown":
        return bus.Reply(voice.unknown(uid), None)
    rep = bus.respond(canonical, adapted.claims, adapted.gaps, now, catalog=catalog, last_subject=_last_subject(recs),
                      candle_counts=candle_counts(root))
    if rep.answer is not None and r.kind == "intent":
        rep = bus.Reply(voice.wrap("answer", _late_note(sent_at, now, r.intent) + rep.text, uid), rep.answer, rep.used_fallback, rep.violations)
    return rep


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
        fresh = converse.interpret(text, now, slang)["kind"] in ("ask_bus", "market_recon", "convo_recon", "followup", "need_time", "act_offer", "act_missing", "alert_unsupported")
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
    if k == "followup":
        ctx = _last_context(recs, now)
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
    if k == "alert_unsupported":
        return bus.Reply("I can set price alerts, but I couldn't read that one. I can watch: price going above or below a level (alert me when price goes above 1.3400), "
                         "touching a level, a candle closing beyond a level, price moving a number of pips from now (alert me when price moves 10 pips), "
                         "a new change of character (any direction), the break-of-structure direction changing, or price against an EMA. Nothing was saved. Try one of those in your own words.", None), {}
    if k == "act_offer":
        return _offer_act(root, catalog, d, uid, text, now)
    if k == "offer_save":
        quote = cs.quote(text, 160)
        opt = [{"id": "1", "label": "save it as a note of yours",
                "action": {"type": "know", "text": text, "at": ledger._iso(sent_at) if sent_at is not None else None}}]
        return bus.Reply(f"{voice.lead('clarify', uid)} That sounds like something you noticed or expect: \"{quote}\". "
                         "Want me to save it as a note of yours, word for word? (yes or no)", None), {"pending": {"kind": "save", "options": opt}}
    return bus.Reply(voice.unknown(uid), None), {}


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


def _offer_alert(root, al, uid, now):
    def ask(said, cmds):
        act = {"type": "act", "commands": cmds} if len(cmds) > 1 else {"type": "act", "command": cmds[0]}
        return (bus.Reply(f"{voice.lead('clarify', uid)} Here's what I understood: {said}. Should I set it? (yes or no)", None),
                {"pending": {"kind": "act", "options": [{"id": "1", "label": said, "action": act}]}})
    if al["type"] == "level" and al.get("tf_asked"):
        # They asked for a timeframe the engine does not record this reading on: say so and offer the real one; never quietly swap it.
        words = {"5m": "5-minute", "1h": "1-hour"}[al["tf_asked"]]
        what = "change of character" if al["cmd"] == "/alert choch" else "break of structure"
        said = (f"the engine only saves its {what} reading on the 15-minute, not the {words}, so I can't watch the {words} one. "
                f"I can set the 15-minute {what} alert instead (any direction)")
        return (bus.Reply(f"{voice.lead('clarify', uid)} You asked for the {words}, but {said}. Want that? (yes or no)", None),
                {"pending": {"kind": "act", "options": [{"id": "1", "label": said, "action": {"type": "act", "command": al["cmd"]}}]}})
    if al["type"] == "level":
        if al.get("said"):
            return ask(al["said"] + (". It checks the engine's saved scans, which come about every 5 minutes, so it can't see a change that appears and disappears between two scans; "
                                      "and it only knows 'a change of character happened', not which direction (the engine doesn't record that), so it's any direction"
                                      if al["cmd"] == "/alert choch" else ""), [al["cmd"]])
        return ask("set the alert " + al["cmd"][len("/alert "):], [al["cmd"]])
    px, at = _latest_price(root, now)
    if px is None or (now - at).total_seconds() > ALERT_FRESH_S:
        when = f"from {timeref.hm(at)} Lagos time" if at else "at all"
        return (bus.Reply("I can't set a 'moves N pips' alert right now because I need today's price to measure from, and my newest saved candle is "
                          f"{'not available' if at is None else when}. Give me a level instead, like: alert me when price goes above 1.3400.", None), {})
    n = al["pips"] * 0.0001
    up, dn = round(px + n, 5), round(px - n, 5)
    pip_txt = f"{al['pips']:g}"
    cmds, parts = [], []
    if al["dir"] in ("any", "up"):
        cmds.append(f"/alert {up:.5f} above"); parts.append(f"above {up:.5f}")
    if al["dir"] in ("any", "down"):
        cmds.append(f"/alert {dn:.5f} below"); parts.append(f"below {dn:.5f}")
    how = " and ".join(parts)
    said = (f"price is {px:.5f} (from my {timeref.hm(at)} Lagos-time candle), so {pip_txt} pips is {how}. I'll set " +
            ("two alerts, and when one hits I'll message you; the other stays open until you cancel it" if len(cmds) == 2 else "one alert"))
    return ask(said, cmds)


def _offer_act(root, catalog, d, uid, text, now):
    """Turn a plain-language trade, outcome or reasoning into the exact command, show it in plain words, and wait for a yes."""
    a = d["act"]
    if a == "alert":
        return _offer_alert(root, d["alert"], uid, now)
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


def fire_alerts(root, catalog, now, send, summary):
    """User price alerts are catalog threads with a machine-checkable condition. Each scan:
    check open threads against the permanent 5m candle archive (only candles AFTER the thread was
    opened can resolve it — a level already crossed earlier does not fire), and tell the user ONCE.
    The resolution is written first; the message is stored and retried like any other reply."""
    by_tf = {tf: _read_jsonl(os.path.join(root, name)) for tf, name in CANDLE_ARCHIVES.items()}
    events = _read_jsonl(os.path.join(root, "market_event_log.jsonl"))
    digests = _read_jsonl(os.path.join(root, "structure_digest_log.jsonl"))
    if not any(by_tf.values()) and not events and not digests:
        return
    for rec in catalog.resolve_open_threads(candles_by_tf=by_tf, events=events, now=now, digests=digests):
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
# /taken and /skip: the user's answer to a fired signal (Phase A). The signal stays the engine's record;
# this stores only what the USER did about it, and logs a case whose bot-plan side is the signal as it FIRED.
# ----------------------------------------------------------------------
SIGNAL_COMMANDS = ("/taken", "/skip")
SIGNAL_REPLY_HINT = "\nSend /taken if you took it or /skip if you didn't."


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
            ledger.append(catalog.path, BUS_REPLY, {"update_id": uid, "text": reply.text, "kind": kind,
                                                    "used_fallback": reply.used_fallback,
                                                    "violations": list(reply.violations), **extra}, now=now)
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
