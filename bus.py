"""
bus.py — the first BUS wording layer. Pure: text in, text out. No Telegram, no
files, no clock, no engine imports. It imports bus_contract, bus_adapters,
user_catalog and ledger, and nothing else.

HOW A REPLY IS MADE (every step is a rule, not a habit)
  1. ROUTE      message text -> one registered intent (deterministic; no model).
  2. COLLECT    adapters turn persisted state/logs/catalog into Claims + Gaps.
  3. RESOLVE    bus_contract.resolve() -> an Answer: claims that exist, plus
                explicit Missing/Conflict/notes. Nothing is computed here.
  4. RENDER     each intent has one renderer. A renderer may ONLY pick from the
                Answer's items and may ONLY use numbers/words that came from
                them. Anything missing is said as missing ("the engine hasn't
                set that yet"), never covered with plausible prose.
  5. AUDIT      audit_text() runs on the finished text. If it finds an
                unsupported number or term, or an unacknowledged note, the
                prose is DISCARDED and a plain claim listing is sent instead.
                An unaudited sentence never leaves this module.
  6. REMEMBER   statement_payload() is what the caller logs, so a later "didn't
                you say bullish?" is answered from what was actually said.

TONE: casual and direct. Engine-authored text (cautions, trend health, the
engine's own sentences) is quoted as the engine's, not adopted as BUS's voice.
Wording is deliberately plain: nothing here claims more than a claim says.
"""

import json
import re
from dataclasses import dataclass
from typing import Optional

import bus_adapters as ba
import bus_contract as bc
import ledger
from bus_contract import INTERPRETATION, FACT, EXPECTATION

# ----------------------------------------------------------------------
# 1. ROUTING
# ----------------------------------------------------------------------
COMMAND_INTENTS = {
    "/now": "market_state", "/state": "market_state",
    "/thesis": "current_thesis",
    "/watch": "watching",
    "/changed": "what_changed", "/change": "what_changed",
    "/flip": "what_would_change_it",
    "/story": "current_market_story",
    "/why": "why",
    "/review": "trade_reconstruction", "/botvsme": "bot_vs_user",
    "/thread": "thread_status", "/terms": "my_terms", "/alerts": "my_alerts", "/research": "research_cases",
    "/health": "integrity",
}
ACT_COMMANDS = ("/logtrade", "/reason", "/outcome", "/alert", "/term")
HELP_COMMANDS = ("/bushelp", "/menu")

_KEYWORDS = (   # (regex, intent) — first match wins; deliberately small and literal
    (re.compile(r"\bwhy\b", re.I), "why"),
    (re.compile(r"\b(what('?s| is) (the )?(story|read))\b", re.I), "current_market_story"),
    (re.compile(r"\b(what changed|anything change|what'?s new|changed)\b", re.I), "what_changed"),
    (re.compile(r"\b(invalidat\w*|flip|what would change|break(s)? (it|this)|wrong)\b", re.I), "what_would_change_it"),
    (re.compile(r"\b(watch(ing)?|waiting for|next)\b", re.I), "watching"),
    (re.compile(r"\b(thesis|bias|direction|bullish|bearish)\b", re.I), "current_thesis"),
    (re.compile(r"\b(what'?s happening|status|right now|now|market)\b", re.I), "market_state"),
)


@dataclass(frozen=True)
class Routed:
    kind: str                  # "intent" | "act" | "help" | "unknown"
    intent: Optional[str] = None
    command: Optional[str] = None
    params: Optional[dict] = None
    rest: str = ""


def route(text, last_subject=None):
    """Deterministic. `last_subject` is the first subject of BUS's own previous
    statement (conversation continuity for 'why'). Returns a Routed."""
    raw = (text or "").strip()
    if not raw:
        return Routed("unknown")
    first, _, rest = raw.partition(" ")
    cmd = first.lower().split("@")[0]
    rest = rest.strip()
    if cmd in HELP_COMMANDS:
        return Routed("help")
    if cmd in ACT_COMMANDS:
        return Routed("act", command=cmd, rest=rest)
    if cmd in COMMAND_INTENTS:
        intent = COMMAND_INTENTS[cmd]
        return Routed("intent", intent=intent, params=_params_for(intent, rest, last_subject), rest=rest)
    if cmd.startswith("/"):
        return Routed("unknown", command=cmd)
    for rx, intent in _KEYWORDS:
        if rx.search(raw):
            return Routed("intent", intent=intent, params=_params_for(intent, rest, last_subject), rest=raw)
    return Routed("unknown")


def _params_for(intent, rest, last_subject):
    p = {}
    if intent == "why":
        m = re.search(r"\b(htf\.bias|thesis\.[a-z_]+|leg\.[a-z_]+|cautions|watch\.zones)\b", rest or "")
        if m:
            p["about"] = m.group(1)
        elif last_subject:
            p["about"] = last_subject
    if intent in ("trade_reconstruction", "bot_vs_user"):
        m = re.search(r"(\d+)", rest or "")
        if m:
            p["case_id"] = f"CASE-{int(m.group(1)):04d}"
    if intent == "thread_status":
        m = re.search(r"(\d+)", rest or "")
        if m:
            p["thread_id"] = f"THREAD-{int(m.group(1)):04d}"
    return p


# ----------------------------------------------------------------------
# 4. RENDERING — claim-bound
# ----------------------------------------------------------------------
_REASON_PHRASE = {
    "no_evidence": "nothing recorded for it",
    "stale": "what I have is too old to call current",
    "wrong_kind": "I only have it as a different kind of statement than this needs",
    "no_basis": "the engine gave no supporting evidence for it",
    "engine_does_not_provide": "the engine doesn't compute that",
    "unknown_referent": "I'm not sure what you're asking about",
    "unset": "the engine hasn't set it yet",
    "unknown": "the engine says it doesn't know yet",
    "not_provided": "it isn't in the data I have",
    "flagged_stale": "the engine flags it as stale right now",
}
_SUBJECT_LABEL = {
    "htf.bias": "the higher-timeframe bias", "thesis.primary": "MIL's primary thesis",
    "thesis.status": "the thesis status", "thesis.failure_condition": "the failure condition",
    "thesis.invalidation": "the invalidation", "thesis.phase": "the phase",
    "leg.age_status": "the leg's age/health", "thesis.failure_risk": "failure risk",
    "volatility.state": "volatility", "leg.macro": "the 1H leg", "thesis.counter": "the counter-thesis",
    "cautions": "cautions", "watch.zones": "the watch zones", "leg.owner": "the leg owner",
    "story.controller": "who controlled the move", "story.counter_attempt": "where the other side fought back",
    "momentum.state": "momentum", "thesis.expected_next_event": "the expected next event",
    "thesis.state": "the thesis state", "thesis.conflicting_evidence": "conflicting evidence",
    "market.data_timestamp": "data freshness", "system.ledger_integrity": "ledger integrity",
    "system.integrity": "the integrity inspector", "user.terms": "your terms", "research.cases": "the research index", "alerts.open": "your alerts",
}


def describe_condition(c):
    """Words for a stored alert condition. Every number comes from the condition itself."""
    t, tf = c["type"], c.get("timeframe")
    on = f" on the {tf}" if tf and tf != "5m" else ""
    lv = _price(c["level"]) if "level" in c else None
    if t == "price_trade_above":  return f"price trades up through {lv}"
    if t == "price_trade_below":  return f"price trades down through {lv}"
    if t == "price_close_above":  return f"a candle closes above {lv}{on}"
    if t == "price_close_below":  return f"a candle closes below {lv}{on}"
    if t == "price_in_zone":
        return (f"price touches {_price(c['low'])}" if c["low"] == c["high"]
                else f"price trades into the zone {_price(c['low'])}–{_price(c['high'])}")
    if t == "ema_close_above":    return f"a {tf} candle closes above the EMA {c['period']}"
    if t == "ema_close_below":    return f"a {tf} candle closes below the EMA {c['period']}"
    if t == "ema_touch":          return f"price touches the {tf} EMA {c['period']}"
    if t == "event_code":         return f"the engine records event {c['code']}"
    return t


def describe_hit(c, ev):
    s = describe_condition(c)
    if "ema" in ev:
        s += f" (EMA was {_price(ev['ema'])}, the candle ran {_price(ev['low'])}–{_price(ev['high'])}, closed {_price(ev['close'])})"
    elif c["type"].endswith("above") and "high" in ev:
        s += f" (high {_price(ev['high'])})"
    elif c["type"].endswith("below") and "low" in ev:
        s += f" (low {_price(ev['low'])})"
    elif "close" in ev:
        s += f" (close {_price(ev['close'])})"
    elif "low" in ev and "high" in ev:
        s += f" (the candle ran {_price(ev['low'])}–{_price(ev['high'])})"
    return s


ALERT_COVERAGE_NOTE = ("Alerts are checked on each scan against the candle archive, and the scanner only runs "
                       "07:00–15:55 UTC Mon–Fri, so a move outside those hours can't trigger one (it would fire at the next scan "
                       "only if price is still beyond the level at a later candle).")
_SIDE = {"above": "above", "over": "above", "up": "above", "break": "above", "breaks": "above", "higher": "above",
         "below": "below", "under": "below", "down": "below", "breakdown": "below", "lower": "below"}
_NUM_ = r"(\d+(?:\.\d+)?)"


def parse_alert(rest):
    """-> ("list"|"cancel"|"set", payload) or (None, error text). Pure; the condition it returns is exactly what gets stored."""
    r = (rest or "").strip()
    low = r.lower()
    if low in ("", "help"):
        return None, ("Alert forms:\n/alert 1.3400 above (or below) — price trades through a level\n"
                      "/alert touch 1.3400 — price reaches a level from either side\n"
                      "/alert zone 1.3380 1.3400 — price trades into a zone\n"
                      "/alert close above 1.3400 15m — a candle CLOSES beyond a level (5m|15m|1h)\n"
                      "/alert ema 50 15m above|below|touch — price vs an EMA\n/alerts — list   /alert cancel N")
    if low in ("list", "ls"):
        return "list", None
    m = re.match(r"^cancel\s+(?:thread-?0*)?(\d+)$", low)
    if m:
        return "cancel", f"THREAD-{int(m.group(1)):04d}"
    m = re.match(r"^ema\s+(\d+)\s+(5m|15m|1h)\s+(above|below|touch)$", low)
    if m:
        period, tf, how = int(m.group(1)), m.group(2), m.group(3)
        if not 2 <= period <= 500:
            return None, "EMA period must be between 2 and 500."
        return "set", {"type": "ema_touch" if how == "touch" else f"ema_close_{how}", "period": period, "timeframe": tf}
    m = re.match(rf"^zone\s+{_NUM_}\s*(?:-|–|—|to|\s)\s*{_NUM_}$", low)
    if m:
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        if lo == hi:
            return None, "A zone needs two different prices (use /alert touch for one)."
        return "set", {"type": "price_in_zone", "low": lo, "high": hi}
    m = re.match(rf"^touch\s+{_NUM_}$", low)
    if m:
        lv = float(m.group(1)); return "set", {"type": "price_in_zone", "low": lv, "high": lv}
    m = re.match(rf"^close\s+(above|below|over|under)\s+{_NUM_}(?:\s+(5m|15m|1h))?$", low)
    if m:
        side = _SIDE[m.group(1)]
        cond = {"type": f"price_close_{side}", "level": float(m.group(2))}
        if m.group(3) and m.group(3) != "5m":
            cond["timeframe"] = m.group(3)
        return "set", cond
    m = re.match(rf"^{_NUM_}\s+({'|'.join(_SIDE)})$", low) or re.match(rf"^({'|'.join(_SIDE)})\s+{_NUM_}$", low)
    if m:
        g = m.groups()
        num, word = (g[0], g[1]) if g[0][0].isdigit() else (g[1], g[0])
        return "set", {"type": f"price_trade_{_SIDE[word]}", "level": float(num)}
    return None, "I couldn't read that alert. Send /alert for the forms I understand."


def _label(subject):
    return _SUBJECT_LABEL.get(subject, subject)


def _cap(s):
    """Upper-case the first letter only (str.capitalize() would lower-case 'MIL')."""
    return s[:1].upper() + s[1:]


def E(text):
    """Mark text as QUOTED from the engine. audit_text verifies it is verbatim in a claim."""
    return f"{bc.ENGINE_OPEN}{text}{bc.ENGINE_CLOSE}"


def A(label):
    """Mark a concept named ONLY to say it is missing (see bus_contract.ABSENT_OPEN)."""
    return f"{bc.ABSENT_OPEN}{label}{bc.ABSENT_CLOSE}"


def _display(text):
    return (text.replace(bc.ENGINE_OPEN, "\u201c").replace(bc.ENGINE_CLOSE, "\u201d")
                .replace(bc.ABSENT_OPEN, "").replace(bc.ABSENT_CLOSE, ""))


def _by(answer, subject, label=None):
    for i in answer.items:
        if i.claim.subject == subject and (label is None or i.label == label):
            return i.claim
    return None


def _dir(v):
    return (v.get("direction") if isinstance(v, dict) else v)


def _price(x):
    return f"{x:.5f}" if isinstance(x, (int, float)) and not isinstance(x, bool) else str(x)


def _integrity_lines(a):
    """Open inspector findings, always shown when present — an alarm is never hidden under a tidy answer."""
    c = _by(a, "system.integrity")
    if c is None:
        return []
    open_ = c.value.get("open") or []
    if not open_:
        return ["Integrity inspector: nothing open."]
    L = [f"Integrity alarm — the inspector has {c.value['n_open']} open finding(s):"]
    for f in open_[:5]:
        tag = " (suspected origin)" if f.get("first_divergence") else ""
        L.append(f"• {f['severity']} {f['probe']}{tag}: {E(f['summary'])}")
    if c.value["n_open"] > len(open_[:5]):
        L.append("• …and more (they are all in the integrity ledger).")
    return L


def _missing_lines(answer, only_required=False):
    out = []
    for m in answer.missing:
        if only_required and not m.required:
            continue
        out.append(f"• {A(_label(m.subject))}: {_REASON_PHRASE.get(m.reason, m.reason)}")
    return out


def _disagreement_line(answer):
    if "DIRECTION_DISAGREEMENT" not in answer.notes:
        return None
    b, p = _by(answer, "htf.bias"), _by(answer, "thesis.primary")
    st = _by(answer, "thesis.status")
    extra = f" (status: {st.value}, so the thesis layer is mid-transition)" if st and st.value == "TRANSITIONING" else ""
    return (f"Heads up — these disagree: the bias is {str(b.value).lower()} but MIL's primary thesis is "
            f"{str(_dir(p.value)).lower()}{extra}. I'm showing both rather than picking one.")


def _r_market_state(a):
    L = []
    d = _disagreement_line(a)
    bias, phase = _by(a, "htf.bias"), _by(a, "thesis.phase")
    if bias:
        line = f"HTF bias is {str(bias.value).lower()}"
        if phase:
            line += f", phase: {phase.value['phase']}"
            if phase.value.get("age_bars") is not None:
                line += f" ({phase.value['age_bars']} bars old)"
        L.append(line + ".")
    if d:
        L.append(d)
    ts = _by(a, "thesis.state")
    if ts:
        L.append(f"The engine's thesis state reads {E(ts.value)}.")
    leg = _by(a, "leg.macro")
    if leg:
        v = leg.value
        L.append(f"The 1H leg is {str(v['direction']).lower()}, origin {_price(v['origin'])}, extreme {_price(v['extreme'])}.")
    age = _by(a, "leg.age_status")
    if age:
        if "engine_trend_health" in age.value:
            L.append(f"Leg health (engine): {E(age.value['engine_trend_health'])}")
        if age.value.get("status"):
            L.append(f"Leg maturity (engine label): {str(age.value['status']).lower().replace('_', ' ')}.")
    fr = _by(a, "thesis.failure_risk")
    if fr:
        L.append(f"Failure risk: {str(fr.value['level']).lower()}.")
    vol = _by(a, "volatility.state")
    if vol:
        L.append(f"Volatility: {vol.value}.")
    c = _by(a, "cautions")
    if c is not None:
        if c.value:
            L.append("Cautions — " + "; ".join(f"{x['code']}: {E(x['engine_sentence'])}" for x in c.value))
        else:
            L.append("Cautions: the engine checked and listed none.")
    lo = _by(a, "leg.owner")
    if lo:
        L.append(f"Leg owner: {lo.value['tier']} ({str(lo.value['status']).lower()}).")
    ts_ = _by(a, "market.data_timestamp")
    if ts_:
        L.append(f"Data as of {ts_.observed_at:%H:%M} UTC.")
    st = _by(a, "thesis.status")
    if st and not d:
        L.append(f"Thesis status: {str(st.value).lower()}.")
    return L


def _r_current_thesis(a):
    L = []
    d = _disagreement_line(a)
    p, st = _by(a, "thesis.primary"), _by(a, "thesis.status")
    if p:
        L.append(f"MIL's primary thesis: {str(_dir(p.value)).lower()}"
                 + (f", status {str(st.value).lower()}" if st else "") + ".")
        if p.basis:
            L.append("Supporting evidence (engine): " + "; ".join(E(x) for x in p.basis) + ".")
    elif st:
        L.append(f"Thesis status: {str(st.value).lower()}.")
    if d:
        L.append(d)
    c = _by(a, "thesis.counter")
    if c:
        L.append(f"Counter-thesis: {str(_dir(c.value)).lower()} ({str(c.value.get('status', '')).replace('_', ' ')}).")
    f = _by(a, "thesis.failure_condition")
    if f:
        L.append(_failure_sentence(f))
    return L


def _failure_sentence(f):
    c, v = f.condition, f.value
    word = "above" if c["type"].endswith("above") else "below"
    s = f"Failure condition: price trading {word} {_price(c['level'])}"
    if v.get("retrace_level") is not None:
        s += f" (the retrace level is {_price(v['retrace_level'])})"
    return s + "."


def _r_watching(a):
    L = []
    z = _by(a, "watch.zones")
    if z is not None:
        if z.value:
            for w in z.value:
                bits = f"{w['code']} ({str(w['role']).lower()})"
                if w.get("zone_low") is not None and w.get("zone_high") is not None:
                    bits += f" zone {_price(w['zone_low'])}–{_price(w['zone_high'])}"
                L.append(f"Watching: {bits}. Engine: {E(w['engine_sentence'])}")
        else:
            L.append("The engine has nothing on its watch list right now.")
    e = _by(a, "thesis.expected_next_event")
    if e:
        L.append(f"Expected next event: {e.value}.")
    inv = _by(a, "thesis.invalidation")
    if inv:
        L.append(f"Invalidation (engine): {E(inv.value['engine_text'])}.")
    f = _by(a, "thesis.failure_condition")
    if f:
        L.append(_failure_sentence(f))
    return L


def _r_what_would_change_it(a):
    L = []
    f, inv = _by(a, "thesis.failure_condition"), _by(a, "thesis.invalidation")
    if inv:
        L.append(f"What breaks it (engine): {E(inv.value['engine_text'])}.")
    if f:
        L.append(_failure_sentence(f))
    ce = _by(a, "thesis.conflicting_evidence")
    if ce is not None:
        L.append("Conflicting evidence: " + ("; ".join(E(x) for x in ce.value) + "." if ce.value else "none listed."))
    return L


def _r_what_changed(a):
    L = []
    for subject in ("htf.bias", "thesis.status", "thesis.primary", "leg.owner"):
        prev, cur, same = _by(a, subject, "PREVIOUS"), _by(a, subject, "CURRENT"), _by(a, subject, "UNCHANGED")
        if prev and cur:
            since = next(i.since for i in a.items if i.claim is cur)
            L.append(f"{_cap(_label(subject))} went from {_short(prev.value)} to {_short(cur.value)}"
                     f" (the new value first shows at {since:%H:%M} UTC; the old one was last recorded at {prev.observed_at:%H:%M} UTC).")
        elif same:
            L.append(f"{_cap(_label(subject))}: unchanged at {_short(same.value)} in what I can see.")
    d = _disagreement_line(a)
    if d:
        L.append(d)
    return L


def _short(v):
    if isinstance(v, dict):
        d = v.get("direction") or v.get("status") or v.get("tier")
        return str(d).lower() if d else json.dumps(v, sort_keys=True)
    return str(v).lower()


def _r_current_market_story(a):
    L = _r_market_state(a)
    inv = _by(a, "thesis.invalidation")
    if inv:
        L.append(f"What would break it (engine): {E(inv.value['engine_text'])}.")
    return L


def _r_why(a):
    L = []
    for it in a.items:
        c = it.claim
        if it.claim.subject != (a.params or {}).get("about"):
            continue
        L.append(f"{_cap(_label(c.subject))} is {_short(c.value)}. The engine's evidence for that:")
        L += [f"• {E(x)}" for x in c.basis]
    d = _disagreement_line(a)
    if d:
        L.append(d)
    return L


def _r_case(a):
    L = []
    t = next((i.claim for i in a.items if i.claim.subject.endswith(".trade")), None)
    if t:
        v = t.value
        L.append(f"{v['pair']} {str(v['direction']).lower()} at {_price(v['entry'])}, stop {_price(v['sl'])}, "
                 f"target {_price(v['tp'])} (planned {v['planned_r']}R).")
    for i in a.items:
        c, sec = i.claim, i.claim.subject.rsplit(".", 1)[-1]
        if sec == "bot_plan":
            L.append(f"What the bot had at the time: {str(c.value['result']).replace('_', ' ').lower()}.")
        elif sec == "user_reasoning":
            for e in c.value["entries"]:
                flag = " (written after the outcome was known)" if e.get("written_after_outcome") else ""
                L.append(f"Your note{flag}: {E(e['text'])}")
        elif sec == "outcome":
            L.append(f"Outcome: {str(c.value['result']).lower()}" + (f", {c.value['r']}R." if c.value.get("r") is not None else "."))
        elif sec == "investigation":
            for e in c.value["entries"]:
                L.append(f"Later analysis: {E(e['text'])}")
    return L


def _r_thread(a):
    c = a.items[0].claim if a.items else None
    if not c:
        return []
    v = c.value
    L = [f"Thread ({v['status'].lower()}): {E(v['topic'])} — you asked: {E(v['concern'])} (bot position then: {E(v['bot_position'])})."]
    if v.get("evidence"):
        ev = v["evidence"]
        cond = v.get("condition") or {}
        try:
            L.append(f"It resolved when {describe_hit(cond, ev)} at {ev['at']}.")
        except KeyError:
            L.append("It resolved when " + ", ".join(f"{k} {x}" for k, x in ev.items() if k not in ("at", "kind")) + f" at {ev['at']}.")
    return L


def _r_integrity(a):
    L = []
    li = _by(a, "system.ledger_integrity")
    if li:
        v = li.value
        L.append(f"Ledger: {v['records']} records, " + ("untouched." if v["ok"] else f"PROBLEMS — {v['problems']}, bad lines {v['bad_lines']}."))
    ts = _by(a, "market.data_timestamp")
    if ts:
        L.append(f"Last good market data: {ts.value}.")
    L += _integrity_lines(a)
    return L


def _r_my_terms(a):
    c = _by(a, "user.terms")
    if not c:
        return []
    L = ["Your own terms so far (I count them; I don't define them):"]
    for t in c.value["terms"][:15]:
        flag = "the engine has a definition for this" if t["status"] == "recognised_by_engine" else "the engine has no definition for this"
        var = f", typed as {', '.join(t['variants'])}" if len(t["variants"]) > 1 else ""
        L.append(f"• #{t['term']}: {t['uses']} use(s) in {t['n_cases']} case(s){var} — {flag}.")
    return L


def _r_research_cases(a):
    c = _by(a, "research.cases")
    if not c:
        return []
    cases = c.value["cases"]
    if not cases:
        return ["No hypotheses recorded yet."]
    L = ["Research index (statuses describe an experiment, never the strategy):"]
    for h in cases[:10]:
        ex = h["experiments"]
        L.append(f"• {h['hypothesis_id']}: {E(h['question'])} — {h['n_specs']} spec(s), {h['n_frozen']} frozen; "
                 f"{h['family_searches']} search(es) in family {E(h['research_family_id'])}.")
        for e in ex[:4]:
            tags = [] + (["invalid draft"] if not e["valid"] else []) + (["final test read more than once"] if e.get("final_test_peeked") else [])
            L.append(f"   - {e['spec_id']}: {str(e['status']).replace('_', ' ').lower()}" + (f" ({', '.join(tags)})" if tags else ""))
    return L


def _r_my_alerts(a):
    c = _by(a, "alerts.open")
    if not c:
        return []
    al = c.value["alerts"]
    if not al:
        return ["You have no open alerts."]
    L = [f"You have {c.value['n_open']} open alert(s):"]
    for x in al[:15]:
        L.append(f"• {x['thread_id']}: tell you when {describe_condition(x['condition'])}.")
    L.append(ALERT_COVERAGE_NOTE)
    return L


def _r_integrity_alert(a):
    return _integrity_lines(a)


RENDERERS = {
    "market_state": _r_market_state, "current_thesis": _r_current_thesis, "watching": _r_watching,
    "what_would_change_it": _r_what_would_change_it, "what_changed": _r_what_changed,
    "current_market_story": _r_current_market_story, "why": _r_why,
    "trade_reconstruction": _r_case, "bot_vs_user": _r_case, "thread_status": _r_thread,
    "integrity": _r_integrity, "integrity_alert": _r_integrity_alert, "my_terms": _r_my_terms, "research_cases": _r_research_cases, "my_alerts": _r_my_alerts,
}


ALWAYS_SHOW_MISSING = {"current_market_story"}


def render_prose(answer):
    body = RENDERERS[answer.intent](answer) if answer.items else []
    if answer.intent in ("market_state", "current_thesis", "current_market_story") and _by(answer, "system.integrity") is not None:
        body = _integrity_lines(answer) + body
    if not body and not answer.items:
        body = []
    # The "fuller read" must say what it cannot provide (who controlled the move, momentum...) — that
    # honesty IS the content. Other intents hide optional gaps when the answer is complete.
    gaps = _missing_lines(answer, only_required=(answer.status == "ANSWER" and answer.intent not in ALWAYS_SHOW_MISSING))
    conflict = [f"• {A(_label(c.subject))}: my sources disagree at the same instant, so I'm not asserting either"
                for c in answer.conflicts]
    out = list(body)
    if answer.status == "NO_EVIDENCE":
        out.insert(0, "I can't answer that from what's recorded:")
    if gaps and answer.status != "ANSWER":
        out.append("What I don't have:")
        out += gaps
    elif gaps:
        out += gaps
    out += conflict
    return "\n".join(out)


def render_plain(answer):
    """The fallback: no prose at all, just the claims as they are. Used whenever
    audit_text() objects to the prose."""
    L = [f"[{answer.intent}] status {answer.status}"]
    for i in answer.items:
        c = i.claim
        L.append(f"- {i.label} {c.subject} ({c.kind}): {json.dumps(c.value, sort_keys=True, default=str)}")
    for m in answer.missing:
        L.append(f"- MISSING {m.subject}: {m.reason}")
    for c in answer.conflicts:
        L.append(f"- CONFLICT {c.subject}")
    for n in answer.notes:
        L.append(f"- NOTE {n}")
    return "\n".join(L)


# ----------------------------------------------------------------------
# 5. THE GATE
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Reply:
    text: str
    answer: Optional[bc.Answer]
    used_fallback: bool = False
    violations: tuple = ()


def finalize(answer):
    """Render, audit, and fall back to the plain listing if the audit objects."""
    prose = render_prose(answer)
    absent = {_label(m.subject) for m in answer.missing} | {_label(c.subject) for c in answer.conflicts}
    v = bc.audit_text(prose, answer, absent_labels=absent)      # audited WITH the marks; displayed without
    if v:
        return Reply(render_plain(answer), answer, True, tuple(v))
    return Reply(_display(prose), answer, False, ())


HELP_TEXT = (
    "I only say what the engine and your ledger actually recorded.\n"
    "/now — what's happening\n/thesis — MIL's read\n/watch — what I'm watching\n"
    "/changed — what changed\n/flip — what would break it\n/story — the fuller read\n"
    "/why [subject] — the engine's evidence\n/review N — a logged trade\n"
    "/botvsme N — bot plan vs your reasoning\n/thread N — an open question\n/health — data + ledger check\n"
    "/alert 1.3400 above — price alerts: a level, touch, zone, a close, or an EMA (send /alert for every form); /alerts lists them\n"
    "/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490 — log a trade\n"
    "/reason N <text> — add your reasoning to trade N (use #your_own_term to tag words that are yours)\n"
    "/terms — your own vocabulary and how often you use it\n/research — hypotheses, experiments and their statuses\n/term suport = support — treat two spellings as one term\n"
    "/outcome N win|loss|be [R] — record how it ended"
)


# ----------------------------------------------------------------------
# ACT commands: the catalog is the only thing written; the reply echoes what
# was STORED, not what was typed.
# ----------------------------------------------------------------------
def catalog_terms(catalog, reasoning_event_id):
    """The TERMS_OBSERVED record the catalog wrote for a reasoning entry (read back, not recomputed)."""
    for r in reversed(ledger.read(catalog.path)[0]):
        if r["event_type"] == "TERMS_OBSERVED" and r["payload"].get("reasoning_event_id") == reasoning_event_id:
            return r["payload"]
    return {"recognised": [], "user_terms": []}


EMA_WARMUP_FACTOR = 3


def candle_needs(period):
    """Mirrors user_catalog.candles_needed_for_ema (bus does not import the catalog; a test pins the two)."""
    return EMA_WARMUP_FACTOR * period + 1


def handle_act(routed, catalog, now, plan_facts=None, candle_counts=None):
    """Returns (text, answer_or_None). `plan_facts`, if given, is a dict of the
    engine's claims at `now` (from the adapters) to attach as the bot's plan when
    the trade is being logged at (about) the time it was taken."""
    cmd, rest = routed.command, routed.rest
    try:
        if cmd == "/logtrade":
            m = re.match(r"^\s*([A-Za-z]{6})\s+(buy|sell)\s+([\d.]+)\s+sl\s+([\d.]+)\s+tp\s+([\d.]+)\s*$", rest, re.I)
            if not m:
                return "Format: /logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490", None
            pair, side, entry, sl, tp = m.group(1).upper(), m.group(2).upper(), float(m.group(3)), float(m.group(4)), float(m.group(5))
            cid = catalog.log_trade(pair, side, entry, sl, tp, now, now=now)
            note = "I did not attach a bot snapshot."
            if plan_facts is not None:
                catalog.attach_bot_plan(cid, now, "UNKNOWN", plan_facts, now=now)
                note = "I attached what the engine had right now as the bot's side of the record (marked UNKNOWN: I'm not judging whether it was a valid setup)."
            case = catalog.get_case(cid)
            v = case["trade"]["payload"]
            return (f"Logged {cid}: {v['pair']} {v['direction'].lower()} at {_price(v['entry'])}, "
                    f"stop {_price(v['sl'])}, target {_price(v['tp'])} (planned {v['planned_r']}R). {note}"), None
        if cmd == "/term":
            m = re.match(r"^\s*#?([A-Za-z][\w\-]*)\s*(?:=|means|is)\s*#?([A-Za-z][\w\-]*)\s*$", rest, re.I)
            if not m:
                return "Format: /term suport = support   (says two spellings are the same term of yours)", None
            rec = catalog.alias_term(m.group(1), m.group(2), now=now)
            p = rec["payload"]                                  # the catalog's normalised keys, not a second normaliser here
            return f"Noted: I'll count #{p['variant']} as #{p['canonical']} from now on. Your original spellings are untouched.", None
        if cmd == "/alert":
            kind, payload = parse_alert(rest)
            if kind is None:
                return payload, None
            if kind == "list":
                return "Use /alerts to list your open alerts.", None
            if kind == "cancel":
                catalog.cancel_thread(payload, now=now)
                return f"{payload} cancelled. It stays in your history; I just stop checking it.", None
            cond = payload
            if cond["type"].startswith("ema_"):
                need = candle_needs(cond["period"])
                have = (candle_counts or {}).get(cond["timeframe"], 0)
                if have < need:
                    return (f"I can't set that yet: an EMA {cond['period']} on the {cond['timeframe']} needs at least {need} candles "
                            f"in the archive and I have {have}. It would be a number I can't trust, so I'm not guessing."), None
            tid = catalog.open_thread("price alert", describe_condition(cond), "none (user alert)", cond, opened_at=now, now=now)
            return f"Alert {tid} is set: I'll message you once when {describe_condition(cond)}. {ALERT_COVERAGE_NOTE}", None
        m = re.match(r"^\s*(\d+)\s+(.*)$", rest, re.S)
        if cmd == "/reason":
            if not m:
                return "Format: /reason 3 I thought the bounce was a reversal", None
            cid = f"CASE-{int(m.group(1)):04d}"
            rec = catalog.add_reasoning(cid, m.group(2).strip(), now=now)
            late = " I've marked it as written after the outcome." if rec["payload"]["written_after_outcome"] else ""
            found = catalog_terms(catalog, rec["event_id"])
            extra = ""
            if found["recognised"]:
                extra += " The engine has definitions for: " + ", ".join(x["concept"].replace("_", " ") for x in found["recognised"]) + "."
            unk = [t["raw"] for t in found["user_terms"] if t["status"] == "unrecognised_by_engine"]
            if unk:
                extra += " Your own terms (the engine has no definition, so they stay yours): " + ", ".join("#" + x for x in unk) + "."
            return f"Saved your reasoning on {cid}, word for word.{late}{extra}", None
        if cmd == "/outcome":
            m = re.match(r"^\s*(\d+)\s+(win|loss|be|breakeven)(?:\s+(-?[\d.]+))?\s*$", rest, re.I)
            if not m:
                return "Format: /outcome 3 loss -1", None
            cid = f"CASE-{int(m.group(1)):04d}"
            res = {"win": "WIN", "loss": "LOSS", "be": "BREAKEVEN", "breakeven": "BREAKEVEN"}[m.group(2).lower()]
            r = float(m.group(3)) if m.group(3) else None
            catalog.record_outcome(cid, res, closed_at=now, r=r, now=now)
            return f"Recorded {cid} as {res.lower()}" + (f" ({r}R)." if r is not None else "."), None
    except ledger.LedgerError as e:
        return f"I didn't record that: {e}", None
    return "I don't know that command. Send /bushelp.", None


# ----------------------------------------------------------------------
# One call: message in, Reply out.
# ----------------------------------------------------------------------
def respond(text, pool, gaps, as_of, catalog=None, last_subject=None, plan_facts=None, candle_counts=None):
    """`pool`/`gaps` come from bus_adapters; `as_of` is the time the answer is
    FOR. Returns a Reply (answer is None for help/act/unknown)."""
    r = route(text, last_subject=last_subject)
    if r.kind == "help":
        return Reply(HELP_TEXT, None)
    if r.kind == "unknown":
        return Reply("I didn't catch that — I answer a fixed set of questions about the engine's recorded state. /bushelp lists them.", None)
    if r.kind == "act":
        if catalog is None:
            return Reply("The ledger isn't available right now, so I can't record that.", None)
        msg, _ = handle_act(r, catalog, as_of, plan_facts=plan_facts, candle_counts=candle_counts)
        return Reply(msg, None)
    answer = bc.resolve(r.intent, pool, as_of, params=r.params, gaps=gaps)
    return finalize(answer)
