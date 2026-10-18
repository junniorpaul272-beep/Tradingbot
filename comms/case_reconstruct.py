"""
case_reconstruct.py — Step 2d: an EVIDENCE AUDIT of one logged case. Pure: ledger + stdlib. It reads records it is
handed, writes nothing, has no clock and no candle/price input, and nothing but bus_runner may import it.

WHAT IT IS NOT: a story generator and not an authority on the market. It reports what was RECORDED as known at the
time, what is missing, and which conclusions each gap blocks. Any research conclusion belongs to the research
process; a reconstructed case is never promoted to evidence for anything by being reconstructed.

THE RULES
  * Reconstruct the past, not the present. Every input is a record list; the only historical market evidence is a
    STATE_SNAPSHOT from state_history_log.jsonl whose event time AND knowledge time were both <= the entry anchor.
    There is no argument through which today's state.json could enter, so a missing snapshot stays missing.
  * happened_at (`ts`) and recorded_at are kept apart. `known_at` (optional) rewinds the KNOWLEDGE side: records
    written after it do not exist for this reconstruction.
  * Four things stay distinct and are never merged: the bot's original plan, your decision, the outcome, and the
    investigation (your notes/questions/statements about it).
  * Outcome status, evidence coverage and historical reconstruction are three separate answers. A missing reasoning
    note never invalidates a known outcome; a known outcome never makes an incomplete investigation look complete.
  * Absence is never False, zero, or "did not happen": every gap says what was expected, why it is missing, what
    it blocks, and whether it can still be resolved.
  * An outcome is never inferred from price. There is no price input to infer it from.
  * Conflicting or corrupted records are FLAGGED, never silently resolved; the affected item becomes unusable.
  * Every report carries the cutoffs used, the source records, and a fingerprint of those inputs, so another
    person can reproduce it.
"""
import hashlib
import json
import math

import ledger
import voice

# Event-type names are written out (not imported) because this module may not import the catalog or the history
# writer; a test pins each of them to its owner.
TRADE_LOGGED = "TRADE_LOGGED"
REASONING_LOGGED = "REASONING_LOGGED"
BOT_PLAN_ATTACHED = "BOT_PLAN_ATTACHED"
OUTCOME_RECORDED = "OUTCOME_RECORDED"
INVESTIGATION_ADDED = "INVESTIGATION_ADDED"
INVESTIGATION_OPENED = "INVESTIGATION_OPENED"
KNOWLEDGE_STATED = "KNOWLEDGE_STATED"
SNAPSHOT = "STATE_SNAPSHOT"
SNAPSHOT_SCHEMA = 1
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "OPEN")

STALE_AFTER_S = 1800          # a snapshot older than this at the entry anchor no longer describes "then"
ANCHOR_SLACK_S = 3600         # a command processed more than this after it was sent is not an exact entry time

# Fields shown from a snapshot, verbatim. Not interpreted, not summarised.
STATE_FIELDS = ("htf_bias", "market_phase", "thesis_state", "failure_risk", "mil_status", "leg_owner", "leg_age_status",
                "macro_leg")

# gap states
NEVER_RECORDED, UNAVAILABLE, STALE, TIMESTAMP, CONFLICT, EMPTY, LATE = (
    "never_recorded", "unavailable", "stale", "timestamp_problem", "conflict", "empty", "recorded_late")
# can the gap still be resolved?
PERMANENT = "permanent: it can no longer be recorded as it was"
ADD_OUTCOME = "yes: /outcome records it (nothing is inferred from price)"
ADD_LATE = "only as an after-the-fact record, which stays marked as recorded late"
CHECK_FILE = "unknown: the file may exist elsewhere; check it before concluding anything"


def _dt(value):
    try:
        return ledger.to_utc(value)
    except Exception:
        return None


def _iso(dt):
    return ledger._iso(dt) if dt is not None else None


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _clean(records, case_id):
    """-> (usable records, problems). A record that cannot be placed in time is excluded, and mentioned only if it
    names this case (or is of unknown ownership)."""
    ok, problems = [], []
    for r in records or []:
        if not isinstance(r, dict) or not isinstance(r.get("event_type"), str) or not isinstance(r.get("payload"), dict):
            problems.append("a record is not in the expected shape and was set aside")
            continue
        if _dt(r.get("ts")) is None or _dt(r.get("recorded_at")) is None:
            if r["payload"].get("case_id") in (case_id, None):
                problems.append(f"record {r.get('event_id')} ({r['event_type']}) has an unreadable timestamp and was set aside")
            continue
        ok.append(r)
    return ok, problems


def _ref(r):
    return {"event_id": r.get("event_id"), "event_type": r["event_type"], "ts": _iso(_dt(r["ts"])),
            "recorded_at": _iso(_dt(r["recorded_at"]))}


def _gap(item, expected, state, prevents, resolvable):
    return {"item": item, "expected": expected, "state": state, "prevents": list(prevents), "resolvable": resolvable}


def _entry_anchor(trade):
    """-> (anchor datetime, basis). The most exact time the record gives for 'when you took it'."""
    p, ts = trade["payload"], _dt(trade["ts"])
    rep = _dt(p.get("reported_at")) if p.get("reported_at") else None
    if p.get("signal_event_id") is not None:
        return ts, "the time of your /taken message"
    if rep is not None and rep <= ts:
        return rep, "the time of your /logtrade message"
    return ts, "processing_time"


def _case_records(view, case_id):
    return [r for r in view if r["payload"].get("case_id") == case_id]


def _live(rs, etype):
    return [r for r in rs if r["event_type"] == etype and r["superseded_by"] is None]


def _classify_reasoning(rs, trade, outcome):
    """pre_entry / in_trade / post_outcome counts. Reasoning recorded at or after the outcome's closing time, or
    flagged so when it was written, is post-outcome and is never offered as pre-trade thinking."""
    T = _dt(trade["ts"])
    anchor, _b = _entry_anchor(trade)
    close = _dt(outcome["ts"]) if outcome else None
    out = {"pre_entry": [], "in_trade": [], "post_outcome": [], "notes": []}
    for r in rs:
        at = _dt(r["recorded_at"])
        flagged = bool(r["payload"].get("written_after_outcome"))
        late = flagged or (close is not None and at >= close)
        if late:
            out["post_outcome"].append(r)
            if close is not None and at >= close and not flagged:
                out["notes"].append(f"{r.get('event_id')} was written after the trade's recorded close but before the outcome "
                                    "was recorded; it is counted as after the outcome")
        elif at <= min(anchor, T):
            out["pre_entry"].append(r)
        else:
            out["in_trade"].append(r)
    return out


def _snapshot_for(history, T, known_at, anchor_exact):
    """-> dict describing the historical market evidence. `history` is None when the log could not be read."""
    res = {"status": None, "state": None, "snapshot": None, "values": None, "missing_fields": [], "flags": [],
           "age_s": None, "late_recorded": [], "usable": False}
    if history is None:
        res.update(status="UNAVAILABLE", state=UNAVAILABLE)
        return res
    recs = [r for r in history if r["event_type"] == SNAPSHOT]
    if known_at is not None:
        recs = [r for r in recs if _dt(r["recorded_at"]) <= known_at]
    bad = {pid for k, pid in ledger.verify(recs)} if recs else set()
    view = ledger.view_as_of(recs, T)
    cands = [r for r in view if r["superseded_by"] is None]
    res["late_recorded"] = [_ref(r) for r in recs if _dt(r["ts"]) <= T and _dt(r["recorded_at"]) > T]
    if not cands:
        res.update(status="MISSING", state=NEVER_RECORDED)
        if not recs:
            res["flags"].append("the history log holds no snapshot that was known at that time")
        elif min(_dt(r["ts"]) for r in recs) > T:
            res["flags"].append("history recording began after this entry")
        return res
    best = max(cands, key=lambda r: (_dt(r["ts"]), _dt(r["recorded_at"])))
    same = [r for r in cands if _dt(r["ts"]) == _dt(best["ts"]) and r["payload"].get("state_hash") != best["payload"].get("state_hash")]
    res["snapshot"] = _ref(best)
    res["age_s"] = int((T - _dt(best["ts"])).total_seconds())
    if best.get("event_id") in bad:
        res.update(status="CONFLICT", state=CONFLICT)
        res["flags"].append("that snapshot's stored hash does not match its content, so it is not trusted")
        return res
    if same:
        res.update(status="CONFLICT", state=CONFLICT)
        res["flags"].append("two snapshots for the same moment disagree, so neither is used")
        return res
    p = best["payload"]
    if p.get("payload_schema") != SNAPSHOT_SCHEMA:
        res.update(status="UNAVAILABLE", state=UNAVAILABLE)
        res["flags"].append("that snapshot is in a format this reader does not know")
        return res
    if not anchor_exact:
        res.update(status="PARTIAL", state=TIMESTAMP)
        res["flags"].append("the entry time here is when your command was processed, which can be later than when you sent it, "
                            "so I can't say which market moment applies; values withheld")
        return res
    res["values"] = {k: p.get(k) for k in STATE_FIELDS}
    res["missing_fields"] = [k for k in STATE_FIELDS if p.get(k) is None]
    res["code_version"] = p.get("code_version")
    if p.get("regime_digest_current") is False:
        res["flags"].append("the 1H-bias digest in that snapshot came from a different scan than the rest of it")
    if p.get("htf_bias_stale") is True:
        res["flags"].append("that snapshot marks the 1H bias as stale")
    if res["age_s"] > STALE_AFTER_S:
        res.update(status="STALE", state=STALE)
        res["flags"].append(f"the newest snapshot known then was {res['age_s'] // 60} min old at entry (limit {STALE_AFTER_S // 60})")
        return res
    res["usable"] = True
    res["status"] = "PARTIAL" if res["missing_fields"] else "PRESENT"
    return res


def reconstruct(case_id, catalog_records, history_records, known_at=None, catalog_bad_lines=0):
    """-> report dict. `history_records=None` means the history log could not be read; [] means it is empty."""
    known = _dt(known_at) if known_at is not None else None
    cat, problems = _clean(catalog_records, case_id)
    hist = None
    if history_records is not None:
        hist, hp = _clean(history_records, None)
        problems += [x.replace("a record", "a history record") for x in hp]
    integrity = list(dict.fromkeys(problems))
    if catalog_bad_lines:
        integrity.append(f"{catalog_bad_lines} unreadable line(s) in the user ledger; I can't tell whether any belonged to this case")

    if known is not None:
        cat = [r for r in cat if _dt(r["recorded_at"]) <= known]
    view = ledger.view_as_of(cat, known) if known is not None else ledger.view_as_of(cat, None)
    mine = _case_records(view, case_id)
    trades = _live(mine, TRADE_LOGGED)
    base = {"case_id": case_id, "cutoffs": {"known_at": _iso(known)}, "integrity": integrity}
    if not trades:
        return {**base, "status": "NO_SUCH_CASE",
                "reason": "no trade with that id was recorded" + (" by that time" if known is not None else "")}
    if len(trades) > 1:
        integrity.append(f"{len(trades)} trade records claim to be {case_id}; none is used")
        return {**base, "status": "CONFLICT", "reason": "more than one trade record for this case"}
    trade = trades[0]
    tp = trade["payload"]
    anchor, basis = _entry_anchor(trade)
    exact = basis != "processing_time"
    base["cutoffs"].update(entry_anchor=_iso(anchor), entry_anchor_basis=basis, entry_recorded_at=_iso(_dt(trade["recorded_at"])))
    used = [trade]
    gaps = []

    # ---- integrity of the case's own records
    ids = {r.get("event_id") for r in mine}
    for kind, eid in ledger.verify(cat):
        if eid in ids:
            integrity.append(f"{kind.lower().replace('_', ' ')}: record {eid} of this case does not match its stored hash")
    tainted = {eid for k, eid in ledger.verify(cat) if k == "EDITED"}

    # ---- outcome (the third record)
    outs = _live(mine, OUTCOME_RECORDED)
    outcome, o_status, o_detail = None, "pending", "no outcome has been recorded; none is inferred from price"
    if len(outs) > 1:
        o_status, o_detail = "unavailable", f"{len(outs)} outcome records disagree; none is used"
        integrity.append(o_detail)
    elif outs:
        o = outs[0]
        op = o["payload"]
        if o.get("event_id") in tainted or op.get("result") not in OUTCOMES:
            o_status, o_detail = "unavailable", "the outcome record is corrupted"
            integrity.append("the outcome record is corrupted or unreadable")
        elif _dt(o["ts"]) < _dt(trade["ts"]):
            o_status, o_detail = "unavailable", "the outcome is dated before the entry"
            integrity.append("the outcome is dated before the entry; it is not used")
        elif op["result"] == "OPEN":
            o_status, o_detail, outcome = "pending", "recorded as still open", o
            used.append(o)
        else:
            outcome = o
            used.append(o)
            if _num(op.get("r")):
                o_status, o_detail = "known", f"{op['result'].lower()}, {op['r']}R, as you recorded it"
            else:
                o_status, o_detail = "partial", f"{op['result'].lower()} recorded without an R figure"
    if o_status == "pending" and not outs:
        gaps.append(_gap("recorded outcome", "an /outcome record", NEVER_RECORDED,
                         ["any comparison of the plan with what happened"], ADD_OUTCOME))
    if outcome is not None:
        o_detail += ("; the record says when it was written, not where the result came from, "
                     "and its time is when it was recorded, not necessarily when the trade closed")
    anchor_outcome = outcome if o_status in ("known", "partial") else None

    # ---- the bot's original plan (the first record)
    plans = _live(mine, BOT_PLAN_ATTACHED)
    plan, plan_usable, p_status, p_detail = None, False, "MISSING", "no bot plan was attached to this case"
    if len(plans) > 1:
        p_status, p_detail = "CONFLICT", f"{len(plans)} bot plans are attached; none is used"
        integrity.append(p_detail)
        gaps.append(_gap("bot plan", "one plan attached when the trade was taken", CONFLICT, ["any statement of what the bot had"], CHECK_FILE))
    elif plans:
        plan = plans[0]
        pp = plan["payload"]
        used.append(plan)
        as_of = _dt(pp.get("plan_as_of"))
        facts = pp.get("facts")
        if plan.get("event_id") in tainted or as_of is None:
            p_status, p_detail = "CONFLICT", "the plan record is corrupted or has no readable time"
            integrity.append(p_detail)
            gaps.append(_gap("bot plan", "a readable plan dated at or before entry", TIMESTAMP, ["any statement of what the bot had"], CHECK_FILE))
        elif as_of > _dt(trade["ts"]):
            p_status, p_detail = "CONFLICT", "the plan is dated after the entry"
            integrity.append(p_detail + "; it is not used")
            gaps.append(_gap("bot plan", "a plan dated at or before entry", TIMESTAMP, ["any statement of what the bot had"], PERMANENT))
        elif anchor_outcome is not None and _dt(plan["recorded_at"]) >= _dt(anchor_outcome["ts"]):
            p_status, p_detail = "PARTIAL", "the plan was recorded after the trade closed, so it may carry hindsight"
            gaps.append(_gap("bot plan recorded before the outcome", "a plan written before the result was known", LATE,
                             ["treating the plan as what the bot had before the result"], PERMANENT))
        elif not isinstance(facts, dict) or not facts:
            p_status, p_detail = "PARTIAL", f"a plan with result {str(pp.get('result')).lower().replace('_', ' ')} is attached but carries no facts"
            gaps.append(_gap("facts in the bot plan", "the engine's claims at that time", EMPTY, ["any statement of what the bot saw"], PERMANENT))
        else:
            plan_usable, p_status = True, "PRESENT"
            p_detail = (f"result {str(pp.get('result')).lower().replace('_', ' ')}, as of {_iso(as_of)}; "
                        f"{len(facts)} recorded fact(s)")
            if _dt(plan["recorded_at"]) > _dt(trade["ts"]):
                p_detail += f"; written down at {_iso(_dt(plan['recorded_at']))}"
    else:
        gaps.append(_gap("bot plan", "the engine's view attached when the trade was taken", NEVER_RECORDED,
                         ["any statement of what the bot had at entry"], ADD_LATE))

    # ---- your decision (the second record)
    fill = None
    if plan is not None and isinstance(plan["payload"].get("facts"), dict):
        pe = plan["payload"]["facts"].get("entry")
        if _num(pe) and _num(tp.get("entry")):
            fill = {"signal_entry": pe, "your_entry": tp["entry"], "differs": abs(pe - tp["entry"]) > 1e-9}
    u_status, u_detail = "PRESENT", (f"{tp.get('direction', '?').lower()} at {tp.get('entry')}, stop {tp.get('sl')}, "
                                     f"target {tp.get('tp')} (recorded {_iso(_dt(trade['recorded_at']))})")
    if tp.get("signal_event_id") is not None:
        u_detail += "; taken from a fired signal"
    if fill:
        u_detail += ("; your fill differs from the signal's entry" if fill["differs"] else "; your entry equals the signal's entry")
    else:
        u_detail += "; planned and filled prices are not stored separately, so what you typed is all there is"
    if not exact:
        u_status = "PARTIAL"
        u_detail += "; the entry time is when the scanner processed your command (your send time was not stored)"
        gaps.append(_gap("exact entry time", "the time you sent /logtrade", TIMESTAMP,
                         ["anchoring the market state to the moment of entry"], PERMANENT))
    rs = _live(mine, REASONING_LOGGED)
    rc = _classify_reasoning(rs, trade, anchor_outcome)
    used += rs
    pre, mid, post = len(rc["pre_entry"]), len(rc["in_trade"]), len(rc["post_outcome"])
    reasoning_usable = (pre + mid) > 0
    if pre:
        r_status = "PRESENT"
    elif mid:
        r_status = "PARTIAL"
    else:
        r_status = "MISSING"
    r_detail = (f"{pre} written before entry, {mid} written after entry but before the outcome, "
                f"{post} written after the outcome")
    if not reasoning_usable:
        gaps.append(_gap("reasoning before the outcome", "your thinking recorded before the result was known",
                         NEVER_RECORDED if not post else LATE,
                         ["why you took it", "whether the reasoning held", "any bot-vs-you comparison before the result"],
                         PERMANENT if post else ADD_LATE))
    elif not pre:
        gaps.append(_gap("reasoning before entry", "your thinking recorded before you entered", NEVER_RECORDED,
                         ["describing it as pre-trade reasoning (it was written after entry)"], PERMANENT))
    integrity += rc["notes"]

    # ---- historical market state
    ms = _snapshot_for(hist, anchor, known, exact)
    if ms["snapshot"]:
        used_snap = ms["snapshot"]
    else:
        used_snap = None
    if not ms["usable"]:
        why = ms["state"]
        prevents = ["what the machine's market state was at entry", "any claim about the 1H bias, phase, thesis or leg at entry"]
        res = PERMANENT if why in (NEVER_RECORDED, STALE, TIMESTAMP) else CHECK_FILE
        exp = "a snapshot known at entry and no more than %d min old" % (STALE_AFTER_S // 60)
        gaps.append(_gap("historical market state", exp, why, prevents, res))
    if ms["late_recorded"]:
        gaps.append(_gap("snapshot recorded in time", "a snapshot written before entry", LATE,
                         ["using a snapshot that was only written down after entry as pre-entry knowledge"], PERMANENT))
    if ms["values"] and ms["missing_fields"]:
        for k in ms["missing_fields"]:
            gaps.append(_gap(f"snapshot field {k}", "a recorded value", NEVER_RECORDED,
                             [f"any statement about {k.replace('_', ' ')} at entry"], PERMANENT))

    # ---- investigation (the fourth record: yours, never evidence for the other three)
    notes = _live(mine, INVESTIGATION_ADDED)
    opened = _live(mine, INVESTIGATION_OPENED)
    stated = [r for r in view if r["event_type"] == KNOWLEDGE_STATED and r["superseded_by"] is None
              and r["payload"].get("case_id") == case_id]
    for n in notes:
        if anchor_outcome is not None and _dt(n["recorded_at"]) < _dt(anchor_outcome["ts"]):
            integrity.append(f"investigation note {n.get('event_id')} is dated before the outcome it should follow")
    used += notes + stated
    inv = {"notes": len(notes), "open_questions": sum(1 for r in opened if r["payload"].get("question")),
           "statements": len(stated)}
    i_status = "PRESENT" if (notes or opened or stated) else "MISSING"
    i_detail = (f"{inv['notes']} note(s), {inv['open_questions']} question(s), {inv['statements']} statement(s) of yours; "
                "these are your words, kept apart from the evidence above")

    # ---- the three answers
    usable = [plan_usable, reasoning_usable, ms["usable"]]
    n_ok = sum(usable)
    coverage = "complete" if n_ok == 3 else ("partial" if n_ok == 2 else "insufficient")
    case_issues = [x for x in integrity if "unreadable line" not in x]
    if case_issues and coverage == "complete":
        coverage = "partial"
    if ms["usable"] and not case_issues:
        history = "trustworthy"
    elif ms["state"] in (NEVER_RECORDED, UNAVAILABLE, CONFLICT):
        history = "unavailable"
    elif ms["state"] == TIMESTAMP:
        history = "unavailable"
    else:
        history = "partial"

    established, not_established = [], []
    established.append("your recorded trade, as you gave it")
    if plan_usable:
        established.append("what the bot's recorded plan said when it was attached")
    if o_status in ("known", "partial"):
        established.append("the outcome as you recorded it")
    if reasoning_usable:
        established.append("your reasoning recorded before the outcome" + ("" if pre else " (written after entry)"))
    if ms["usable"]:
        established.append("the machine's recorded market state at entry, as stored, uninterpreted")
    if not plan_usable:
        not_established.append("what the bot had at entry")
    if not reasoning_usable:
        not_established.append("why you took the trade, as you saw it then")
    if not ms["usable"]:
        not_established.append("the machine's market state at entry (today's state is never substituted)")
    if o_status in ("pending", "unavailable"):
        not_established.append("how it turned out")
    not_established.append("whether anyone's reasoning was sound: an outcome alone cannot show that")

    sources = [_ref(r) for r in used] + ([used_snap] if used_snap else [])
    fp_rows = sorted((r.get("event_id") or "", r.get("hash") or "") for r in used)
    if used_snap:
        sr = next((r for r in hist if r.get("event_id") == used_snap["event_id"]), None)
        fp_rows.append((used_snap["event_id"] or "", (sr or {}).get("hash") or ""))
    fingerprint = hashlib.sha256(json.dumps([fp_rows, _iso(anchor), _iso(known), STALE_AFTER_S], sort_keys=True).encode()).hexdigest()[:16]

    inventory = [
        {"key": "bot_plan", "label": "Bot's original plan", "status": p_status, "detail": p_detail},
        {"key": "user_decision", "label": "Your decision and entry", "status": u_status, "detail": u_detail},
        {"key": "reasoning", "label": "Your reasoning before the outcome", "status": r_status, "detail": r_detail},
        {"key": "outcome", "label": "Recorded outcome", "status": {"known": "PRESENT", "partial": "PARTIAL", "pending": "MISSING",
                                                                  "unavailable": "CONFLICT"}[o_status], "detail": o_detail},
        {"key": "market_state", "label": "Historical market state", "status": ms["status"],
         "detail": "; ".join(ms["flags"]) if ms["flags"] else
         (f"snapshot {ms['snapshot']['event_id']} from {ms['snapshot']['ts']}, {ms['age_s'] // 60} min before entry" if ms["snapshot"] else "none")},
        {"key": "investigation", "label": "Investigation", "status": i_status, "detail": i_detail},
    ]
    return {**base, "status": "OK", "outcome_status": o_status, "evidence_coverage": coverage,
            "historical_reconstruction": history, "inventory": inventory,
            "four_records": {"bot_plan": {"status": p_status, "ref": _ref(plan) if plan else None},
                             "user_decision": {"status": u_status, "ref": _ref(trade), "fill": fill,
                                               "reasoning": {"pre_entry": pre, "in_trade": mid, "post_outcome": post}},
                             "outcome": {"status": o_status, "ref": _ref(outcome) if outcome else None},
                             "investigation": {**inv, "status": i_status}},
            "market_state": {"status": ms["status"], "snapshot": ms["snapshot"], "values": ms["values"],
                             "missing_fields": ms["missing_fields"], "flags": ms["flags"], "age_s": ms["age_s"],
                             "late_recorded": ms["late_recorded"]},
            "gaps": gaps, "established": established, "not_established": not_established,
            "sources": sources, "inputs_fingerprint": fingerprint}


# ----------------------------------------------------------------------
# Plain rendering: fixed templates over the report. No engine prose, no interpretation.
# ----------------------------------------------------------------------
_WORD = {"PRESENT": "Present", "PARTIAL": "Partial", "MISSING": "Missing", "STALE": "Stale", "CONFLICT": "Conflict",
         "UNAVAILABLE": "Unavailable"}


def render(rep, max_gaps=6):
    if rep.get("status") == "NO_SUCH_CASE":
        return f"I have no {rep['case_id']}" + (" as of that time." if rep["cutoffs"].get("known_at") else ". /review N lists a logged trade.")
    if rep.get("status") == "CONFLICT":
        return f"{rep['case_id']}: I can't reconstruct it. {rep['reason'].capitalize()}, so I use none of them."
    c = rep["cutoffs"]
    out = [f"Reconstruction of {rep['case_id']}: coverage {rep['evidence_coverage'].upper()}; outcome {rep['outcome_status'].upper()}; "
           f"history {rep['historical_reconstruction'].upper()}.",
           f"Built only from records known by the entry ({c['entry_anchor'][:16].replace('T', ' ')} UTC, anchored to {c['entry_anchor_basis']})"
           + (f", and by {c['known_at'][:16].replace('T', ' ')} UTC" if c.get("known_at") else "") + ". Today's state is never used."]
    for it in rep["inventory"]:
        out.append(f"- {it['label']}: {_WORD[it['status']]} — {it['detail']}")
    ms = rep["market_state"]
    if ms.get("values"):
        shown = ", ".join(f"{k}={v}" for k, v in ms["values"].items() if v is not None and not isinstance(v, (dict, list)))
        out.append(f"Recorded market state (as stored, not interpreted): {shown or 'nothing readable'}.")
    if rep["gaps"]:
        out.append("Missing or uncertain:")
        for g in rep["gaps"][:max_gaps]:
            out.append(f"  * {g['item']} — expected {g['expected']}; {g['state'].replace('_', ' ')}; blocks: "
                       f"{'; '.join(g['prevents'])}. Resolvable: {g['resolvable']}.")
        if len(rep["gaps"]) > max_gaps:
            out.append(f"  * and {len(rep['gaps']) - max_gaps} more gap(s).")
    if rep["integrity"]:
        out.append("Flagged, not resolved: " + "; ".join(rep["integrity"][:4]) + ".")
    out.append("Can be established: " + "; ".join(rep["established"]) + ".")
    out.append("Cannot be established: " + "; ".join(rep["not_established"]) + ".")
    out.append(f"Sources: {len(rep['sources'])} record(s); fingerprint {rep['inputs_fingerprint']}. "
               "This lists what was recorded. It does not judge the trade, and it doesn't add the case to any research.")
    return "\n".join(out)


# ----------------------------------------------------------------------
# The same audit in plain words. Every fact below comes from the report; the connecting words hold none.
# ----------------------------------------------------------------------
_RESOLVE_WORDS = {PERMANENT: "That can't be fixed now.", ADD_OUTCOME: "You can add it with /outcome.",
                  ADD_LATE: "You can still add it, but it will be marked as written after the fact.",
                  CHECK_FILE: "The record may exist somewhere else, so check before concluding anything."}


def _lagos(iso):
    import timeref
    return timeref.lagos(ledger.to_utc(iso)).strftime("%a %d %b, %H:%M")


_GAP_SENTENCE = {
    "recorded outcome": "There's no result recorded yet, so until there is I can't compare the plan with what happened.",
    "bot plan": "I never attached a bot plan, so I can't say what the bot had at that moment.",
    "reasoning before the outcome": "I don't have your reasoning from before the result was known, so I can't say why you took it or whether it held up.",
    "reasoning before entry": "Your reasoning was written after you entered, not before, so I won't call it pre-trade thinking.",
    "historical market state": "I don't have a saved reading of the market from that moment, so I can't say what the bot was seeing.",
    "snapshot recorded in time": "The reading closest to your entry only got written down after you entered, so I didn't treat it as something known then.",
    "exact entry time": "I don't know the exact moment you sent it, so I can't tie it to one specific market moment.",
    "bot plan recorded before the outcome": "The bot's plan was only written down after the trade had closed, so it may carry hindsight and I won't treat it as what the bot had before the result.",
    "facts in the bot plan": "A bot plan is attached but it's empty, so I can't say what the bot saw.",
}


def _gap_plain(g):
    base = _GAP_SENTENCE.get(g["item"])
    if base is None and g["item"].startswith("snapshot field "):
        field = g["item"][len("snapshot field "):].replace("_", " ")
        base = f"My saved reading had no value for {field}, so I can't say anything about it at that moment."
    if base is None:
        base = f"{g['item'][0].upper() + g['item'][1:]} {voice.GAP_WORDS.get(g['state'], g['state'])}, so I can't say {' or '.join(g['prevents'][:2])}."
    return f"{base} {_RESOLVE_WORDS.get(g['resolvable'], '')}".strip()


def render_plain(rep, seed=0):
    if rep.get("status") == "NO_SUCH_CASE":
        return f"I don't have {rep['case_id']}" + (" as of that time." if rep["cutoffs"].get("known_at") else ".")
    if rep.get("status") == "CONFLICT":
        return f"I can't piece {rep['case_id']} together: {rep['reason']}, so I'm not using any of them."
    c = rep["cutoffs"]
    inv = {i["key"]: i for i in rep["inventory"]}
    fr = rep["four_records"]
    out = [f"{voice.lead('reconstruct', seed)} This is {rep['case_id']} as it stood when you took it ({_lagos(c['entry_anchor'])} Lagos time), "
           "using only what was recorded by then, never today's market."]
    if c["entry_anchor_basis"] == "processing_time":
        out.append("One catch: I only know when the scanner processed your command, not when you sent it, so I can't pin the exact moment.")
    out.append("")
    # the bot's side
    out.append("The bot's side: " + {"PRESENT": "I have its plan from that moment on record.",
                                    "PARTIAL": inv["bot_plan"]["detail"][0].upper() + inv["bot_plan"]["detail"][1:] + ".",
                                    "MISSING": "I never attached a bot plan to this trade.",
                                    "CONFLICT": "its plan is recorded in conflicting ways, so I'm not using it."}[inv["bot_plan"]["status"]])
    # yours
    r = fr["user_decision"]["reasoning"]
    bits = []
    if r["pre_entry"]:
        bits.append(f"{r['pre_entry']} written before you entered")
    if r["in_trade"]:
        bits.append(f"{r['in_trade']} written after you entered but before it finished")
    if r["post_outcome"]:
        bits.append(f"{r['post_outcome']} written after the result was known (I don't count those as your thinking at the time)")
    import re as _re
    out.append("Your side: " + _re.sub(r"\s*\(recorded [^)]*\)", "", inv["user_decision"]["detail"].split("; ")[0]) + ". " +
               ("Your reasoning: " + ", ".join(bits) + "." if bits else "You haven't given me any reasoning for it yet."))
    # market
    ms = rep["market_state"]
    if ms.get("values"):
        shown = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in ms["values"].items() if v is not None and not isinstance(v, (dict, list)))
        out.append(f"The machine's saved reading from {ms['age_s'] // 60} minutes before (stored as it was, not interpreted): {shown}.")
    else:
        out.append("The machine's saved reading at that moment: I don't have one I can stand behind (details below).")
    # outcome
    out.append("How it ended: " + {"known": "you recorded it as " + inv["outcome"]["detail"].split(", as you recorded")[0].split("; ")[0] + ".",
                                   "partial": "you recorded a result but no R figure.",
                                   "pending": "no result is recorded" + (" (you marked it still open)" if "still open" in inv["outcome"]["detail"] else "") +
                                              ", and I won't guess one from price.",
                                   "unavailable": "the outcome is recorded in conflicting ways, so I'm not using it."}[rep["outcome_status"]])
    if rep["gaps"]:
        out.append("")
        out.append("Where I'm short:")
        for g in rep["gaps"][:6]:
            out.append("- " + _gap_plain(g))
    if rep["integrity"]:
        out.append("Things that look off in the records, which I'm flagging and not fixing: " + "; ".join(rep["integrity"][:3]) + ".")
    out.append("")
    out.append("What I can stand behind: " + "; ".join(rep["established"]) + ". What I can't: " + "; ".join(rep["not_established"][:-1] or ["nothing more"]) + ".")
    out.append("An outcome alone can't show whether anyone's reasoning was sound, so I'm not judging the trade.")
    out.append(f"(Sources: {len(rep['sources'])} record(s), reference {rep['inputs_fingerprint']}. Send /reconstruct {rep['case_id'].split('-')[1].lstrip('0') or '0'} detail for the technical breakdown.)")
    return "\n".join(out)
