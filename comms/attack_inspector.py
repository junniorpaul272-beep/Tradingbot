"""
attack_inspector.py — attack the integrity inspector itself (24 Oct). The inspector is the thing that makes the bot say "all clear" or raise an
alarm, so it gets the same treatment as the chat layer: plant realistic faults in a copy of the REAL saved engine state (fixture_real_state.json)
and measure what it notices.

Every fault is labelled up front, before running, as one of:
  CLAIMED  a fault the inspector's own description says it looks for (a zero where a price should be, independent sources disagreeing on
           direction, a time from the future, an id that no longer matches its fields, a changed layout, a state that lasted far too long)
  BEYOND   a fault that is wrong but is not something a generic checker claims to see (a believable-looking wrong price, a high below a low)
A CLAIMED fault that goes unnoticed is a bug in the inspector. A BEYOND fault that goes unnoticed is not a bug, but it is the exact limit of what
"all clear" means, and the bot must never talk as if it covered it.

Also tested: the same faults two at a time (does one alarm hide the other?), and the faults planted a second run in a row (does it stay quiet when
nothing changes, and does it stay open until the fault is actually gone?).
Run: python attack_inspector.py
"""
import copy, json, os, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import inspector

import test_inspector as _ti
REAL = json.load(open(os.path.join(os.path.dirname(HERE), "fixture_real_state.json")))
CLEAN = _ti.healthy()                                   # the real file with its one real inconsistency repaired: no standing alarms
STATE = CLEAN if os.environ.get("BASELINE", "clean") == "clean" else REAL
NOW = datetime.fromisoformat(STATE["timestamp"]) + timedelta(minutes=1)
FLIP = {"BEARISH": "BULLISH", "BULLISH": "BEARISH"}


def mut(**kv):
    def f(s):
        s = copy.deepcopy(s); s.update(kv); return s
    return f


def delete(*keys):
    def f(s):
        s = copy.deepcopy(s)
        for k in keys: s.pop(k, None)
        return s
    return f


def flip(*keys):
    def f(s):
        s = copy.deepcopy(s)
        for k in keys: s[k] = FLIP[s[k]]
        return s
    return f


def in_mil(path, value):
    def f(s):
        s = copy.deepcopy(s); d = s["mil_understanding"]
        for p in path[:-1]: d = d[p]
        d[path[-1]] = value; return s
    return f


def plus(ts, **delta):
    return (datetime.fromisoformat(ts) + timedelta(**delta)).isoformat()


def long_dwell(s):
    s = copy.deepcopy(s); h = s["mil_understanding"].get("history")
    if isinstance(h, list) and h:
        h[-1]["entered_at"] = plus(NOW.isoformat(), hours=-30) if isinstance(h[-1], dict) and "entered_at" in h[-1] else None
    return s


def dwell_long(s):
    """Three short stays, then the engine sits in its current state for 40 hours. (The probe compares the current stay with earlier ones, so it needs earlier ones.)"""
    s = copy.deepcopy(s); snap = datetime.fromisoformat(s["timestamp"])
    t0 = snap - timedelta(hours=40, minutes=30)
    s["mil_understanding"]["history"] = [
        {"from_state": "UNKNOWN", "to_state": "SUPPORTED", "at": t0.isoformat(), "transition_cause": "BOOTSTRAP", "invalidation_fired": False},
        {"from_state": "SUPPORTED", "to_state": "WEAKENING", "at": (t0 + timedelta(minutes=10)).isoformat(), "transition_cause": "X", "invalidation_fired": False},
        {"from_state": "WEAKENING", "to_state": "SUPPORTED", "at": (t0 + timedelta(minutes=25)).isoformat(), "transition_cause": "X", "invalidation_fired": False},
        {"from_state": "SUPPORTED", "to_state": "TRANSITIONING", "at": (snap - timedelta(hours=40)).isoformat(), "transition_cause": "X", "invalidation_fired": False}]
    s["mil_understanding"]["thesis_status"] = "TRANSITIONING"
    return s


def dwell_by_since(s):
    """Make the engine believe it has been in its current state for 40 hours (the inspector's dwell probe reads the history's own timestamps)."""
    s = copy.deepcopy(s); hist = s["mil_understanding"].get("history") or []
    for r in hist:
        if isinstance(r, dict):
            for k in list(r):
                if isinstance(r[k], str) and r[k][:2] == "20":
                    r[k] = plus(r[k], hours=-40)
    return s


def stringify(k):
    def f(s):
        s = copy.deepcopy(s); s[k] = str(s[k]); return s
    return f


def scale(k, x):
    def f(s):
        s = copy.deepcopy(s); s[k] = s[k] * x; return s
    return f


FAULTS = [
    # name, function, scope, why (what a person would expect it to catch)
    ("a leg origin price becomes exactly 0.0",                     mut(leg15_origin=0.0),                                  "CLAIMED", "unset placeholder"),
    ("the swing high becomes exactly 0.0",                         mut(macro_swing_high=0.0),                              "CLAIMED", "unset placeholder"),
    ("the campaign origin becomes exactly 0.0",                    mut(campaign_origin=0.0),                               "CLAIMED", "unset placeholder"),
    ("the swing low becomes exactly 0.0",                          mut(macro_swing_low=0.0),                               "CLAIMED", "unset placeholder"),
    ("the 15-minute leg flips direction, others unchanged",        flip("leg15_direction"),                                "CLAIMED", "sources disagree"),
    ("the macro leg flips direction, others unchanged",            flip("macro_leg_direction"),                            "CLAIMED", "sources disagree"),
    ("the campaign direction flips, others unchanged",             flip("campaign_direction"),                             "CLAIMED", "sources disagree"),
    ("the confirmed bias flips, others unchanged",                 flip("macro_bias_confirmed"),                           "CLAIMED", "sources disagree"),
    ("the shadow bias flips, others unchanged",                    flip("shadow_macro_bias_confirmed"),                    "CLAIMED", "sources disagree"),
    ("every direction flips together (they all agree, all wrong)", flip("leg15_direction", "macro_leg_direction", "campaign_direction", "macro_bias_confirmed",
                                                                         "shadow_macro_bias_confirmed", "shadow_minleg_bias_confirmed", "shadow_macro_leg_direction",
                                                                         "shadow_minleg_leg_direction"),                  "BEYOND", "they agree with each other; nothing independent to contradict them"),
    ("the market data time is two days in the future",             mut(market_data_timestamp=plus(STATE["market_data_timestamp"], days=2)), "CLAIMED", "future time"),
    ("a leg began after the file says it was written",             mut(leg15_origin_time=plus(STATE["timestamp"], hours=3)), "CLAIMED", "future time"),
    ("the campaign began in the future",                           mut(campaign_origin_time=plus(STATE["timestamp"], days=1)), "CLAIMED", "future time"),
    ("the swing was confirmed in the future",                      mut(macro_swing_confirmed_at=plus(STATE["timestamp"], hours=5)), "CLAIMED", "future time"),
    ("the leg id no longer matches the leg's prices",              mut(signal_timeline_leg_id="BEARISH|1.40000|1.30000"),   "CLAIMED", "id out of step"),
    ("the zone-interaction leg id no longer matches",              mut(zone_interaction_leg_id="BULLISH|1.20000|1.25000"),  "CLAIMED", "id out of step"),
    ("a saved field disappears",                                   delete("leg15_extreme"),                                 "CLAIMED", "layout change"),
    ("several saved fields disappear",                             delete("leg15_extreme", "macro_swing_low", "campaign_origin", "market_phase"), "CLAIMED", "layout change"),
    ("a number is saved as text",                                  stringify("macro_atr_1h"),                               "CLAIMED", "layout change (type)"),
    ("a price is saved as text",                                   stringify("leg15_origin"),                               "CLAIMED", "layout change (type)"),
    ("a field becomes null",                                       mut(leg15_extreme=None),                                 "CLAIMED", "layout change (type)"),
    ("the engine has sat in one state for 40 hours",               dwell_long,                                              "CLAIMED", "dwell"),
    ("a price is off by a factor of ten",                          scale("macro_swing_high", 10),                            "CLAIMED", "believable-looking wrong number"),
    ("the swing high is below the swing low",                      lambda s: {**copy.deepcopy(s), "macro_swing_high": s["macro_swing_low"] - 0.01}, "CLAIMED", "a high below a low"),
    ("the ATR is negative",                                        mut(macro_atr_1h=-0.002),                                "CLAIMED", "impossible value, not a placeholder"),
    ("the leg extreme is on the wrong side of its origin",         lambda s: {**copy.deepcopy(s), "leg15_extreme": s["leg15_origin"] + 0.02}, "CLAIMED", "a bearish leg whose extreme is above its origin"),
    ("the scan number goes backwards (one run, no history)",       mut(scan_id=3),                                          "BEYOND", "needs history across runs"),
    ("a stale market-data time (a day old) with status fine",      mut(market_data_timestamp=plus(STATE["market_data_timestamp"], days=-1)), "BEYOND", "staleness is the chat layer's job, not the inspector's"),
    ("the break count is wildly high",                             mut(leg15_break_count=9999),                             "BEYOND", "implausible count"),
    ("the failure risk says LOW but every reason is removed",      mut(market_thesis_failure_risk_reasons=[]),               "BEYOND", "claim without support"),
]


def findings(state, mem=None):
    f, m = inspector.inspect_state(state, mem, NOW, None, 0, None)
    return f, m


def fps(fs):
    return {f["fingerprint"] for f in fs}


def run():
    base_f, base_mem = findings(STATE)
    base_f2, base_mem2 = findings(STATE, base_mem)                 # a second run in a row on unchanged state: this is the live situation
    quiet = fps(base_f2)
    out = []
    for name, fn, scope, why in FAULTS:
        try:
            s2 = fn(STATE)
            f, _ = findings(s2, base_mem2)
            new = [x for x in f if x["fingerprint"] not in quiet]
            sev = max((x["severity"] for x in new), key=lambda v: inspector.SEV_RANK[v], default=None)
            out.append((name, scope, why, bool(new), sorted({x["probe"] for x in new}), sev))
        except Exception as e:                                       # a crash of the inspector is itself a finding
            out.append((name, scope, why, None, [f"CRASH {type(e).__name__}: {e}"], None))
    # pairs: does one alarm hide another?
    pairs = []
    claimed = [(n, fn) for n, fn, sc, _ in FAULTS if sc == "CLAIMED"]
    cover = lambda fs: {p for x in fs for p in x["paths"]}      # a fault is HIDDEN only if a field it flagged alone is flagged by nothing when two faults are present (alarms merging is not hiding)
    single = {n: cover([x for x in findings(fn(STATE), base_mem2)[0] if x["fingerprint"] not in quiet]) for n, fn in claimed}
    names = [n for n in single if single[n]]
    touched = lambda fn: {k for k in set(STATE) | set(fn(STATE)) if STATE.get(k) != fn(STATE).get(k)}
    kinds = {n: {x["probe"] for x in findings(fn(STATE), base_mem2)[0] if x["fingerprint"] not in quiet} for n, fn in claimed}
    for i in range(len(names)):
        for j in range(i + 1, len(names), 3):                         # every third partner keeps it quick and still wide
            a, b = names[i], names[j]
            if touched(FAULTS_BY_NAME[a]) & touched(FAULTS_BY_NAME[b]):
                continue                                              # both faults edit the same field: not two faults
            s2 = dict(FAULTS_BY_NAME[a](STATE)); s2 = FAULTS_BY_NAME[b](s2)
            got = cover([x for x in findings(s2, base_mem2)[0] if x["fingerprint"] not in quiet])
            missing = (single[a] | single[b]) - got
            merged = bool(missing) and kinds[a] == kinds[b]               # two faults of the same kind become ONE alarm naming both: by design
            pairs.append((a, b, bool(missing) and not merged, len(missing)))
    # persistence: a fault stays open while it is present, and a clean run afterwards is quiet again
    persist = []
    for name, fn, scope, why in FAULTS:
        if scope != "CLAIMED":
            continue
        f1, m1 = findings(fn(STATE), base_mem2)
        f2, m2 = findings(fn(STATE), m1)                               # same fault, next run
        f3, m3 = findings(STATE, m2)                                    # fault gone
        new1, new2, new3 = fps(f1) - quiet, fps(f2) - quiet, fps(f3) - quiet
        persist.append((name, bool(new1), new1 <= fps(f2) | new2 or not new1, not new3))
    return out, pairs, persist, len(quiet), base_f


FAULTS_BY_NAME = {n: fn for n, fn, _, _ in FAULTS}


def report():
    out, pairs, persist, nquiet, base = run()
    print("INSPECTOR ATTACK — faults planted in a copy of the REAL saved engine state")
    print(f"  baseline: {os.environ.get('BASELINE', 'clean')} state, carrying {nquiet} open findings; a fault counts only if it adds something new on top of those\n")
    by = defaultdict(lambda: [0, 0])
    for name, scope, why, hit, probes, sev in out:
        by[scope][1] += 1; by[scope][0] += bool(hit)
    for sc in ("CLAIMED", "BEYOND"):
        print(f"  {sc:8s} noticed {by[sc][0]}/{by[sc][1]}")
    print("\n  CLAIMED faults it MISSED or crashed on (these are inspector bugs):")
    bad = [x for x in out if x[1] == "CLAIMED" and not x[3]]
    for name, scope, why, hit, probes, sev in bad:
        print(f"    - {name}  [{why}] {probes if hit is None else ''}")
    if not bad: print("    none")
    print("\n  BEYOND faults it did NOT notice (the exact edge of what 'all clear' covers):")
    for name, scope, why, hit, probes, sev in out:
        if scope == "BEYOND" and not hit:
            print(f"    - {name}  [{why}]")
    print("\n  BEYOND faults it did notice anyway:")
    for name, scope, why, hit, probes, sev in out:
        if scope == "BEYOND" and hit:
            print(f"    - {name}  -> {probes} ({sev})")
    masked = [p for p in pairs if p[2]]
    print(f"\n  two faults at once: {len(pairs) - len(masked)}/{len(pairs)} pairs kept both alarms; {len(masked)} had an alarm hidden")
    for a, b, m, n in masked[:10]:
        print(f"    - {a}  +  {b}  (lost {n})")
    stay = [p for p in persist if not (p[1] and p[2] and p[3])]
    print(f"\n  persistence (alarm present, stays open next run, clears when the fault is gone): {len(persist) - len(stay)}/{len(persist)} ok")
    for p in stay:
        print(f"    - {p[0]}: raised={p[1]} stays={p[2]} clears={p[3]}")
    return bad, masked, stay, out


if __name__ == "__main__":
    report()
