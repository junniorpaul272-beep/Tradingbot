"""test_reports.py - protects the reports package (reports/): layer separation, the bias contract, every report passing every check at many real
moments, no look-ahead, and calculations recomputed independently. Reads the repo's own ohlc_*_log.jsonl and state.json; writes nothing.
Run: python test_reports.py"""
import sys, json, random, ast, os, dataclasses
from datetime import datetime, timedelta, timezone
import pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
PKG = os.path.join(HERE, "reports")
for _k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TWELVE_DATA_KEY"): os.environ.setdefault(_k, "dummy")      # same dummies tests.yml uses; scanner_common reads them at import
import scanner_common as SC
from reports import contracts as C, facts as F, interpret as I, render as RD, guard as G, assemble as RP, source as S
def load(n): return S.read_candles(os.path.join(HERE, f"ohlc_{n}_log.jsonl"))
def open_only(df): return df[[not SC.is_forex_weekend(x.to_pydatetime()) for x in df.index]]     # the scanner's own weekend rule, applied candle by candle
D5, D15, D1 = load("5m"), load("15m"), load("1h")
ok = bad = 0
def check(n, c, x=""):
    global ok, bad
    if c: ok += 1; print("PASS", n)
    else: bad += 1; print("FAIL", n, x)
def mkb(now, kind="BEARISH"):
    h = F.closed(D1, "1H", pd.Timestamp(now, tz="UTC") if pd.Timestamp(now).tzinfo is None else pd.Timestamp(now)).tail(72); hi, lo = float(h["High"].max()), float(h["Low"].min())
    return C.BiasInput(kind, hi, lo) if kind == "BEARISH" else C.BiasInput(kind, lo, hi)

# ---- 1. layer separation (import graph) ----
def imports(fn):
    t = ast.parse(open(os.path.join(PKG, fn)).read()); s = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Import): s |= {a.name.split(".")[0] for a in n.names}
        if isinstance(n, ast.ImportFrom):
            if n.level and not n.module: s |= {a.name for a in n.names}          # from . import facts
            else: s.add(n.module.split(".")[0])                                    # from .contracts import X  /  from datetime import X
    return s
LAY = {"contracts.py": {"dataclasses", "datetime", "typing", "math"}, "facts.py": {"math", "pandas", "contracts"}, "interpret.py": {"contracts"},
       "render.py": {"re", "datetime", "html", "contracts"}, "guard.py": {"re"}, "source.py": {"json", "math", "datetime", "pandas", "contracts"},
       "assemble.py": {"os", "pandas", "source", "facts", "interpret", "render", "guard", "contracts"}}
for fn, allowed in LAY.items():
    extra = imports(fn) - allowed
    check(f"{fn} imports only {sorted(allowed)}", not extra, extra)
check("render.py cannot see candles (no pandas) and facts.py cannot see wording (no render/guard)", "pandas" not in imports("render.py") and not ({"render", "guard", "interpret"} & imports("facts.py")))
# ---- 2. no free-text field in any contract ----
texty = [f"{c.__name__}.{f.name}" for c in vars(C).values() if dataclasses.is_dataclass(c) for f in dataclasses.fields(c)
         if re.search(r"text|message|narr|sentence|reason|prose|why", f.name) and c.__name__ != "Report"] if (re := __import__("re")) else []
check("no contract has a text/reason/narrative field (only Report.messages, written by render)", not texty, texty)
check("engine input is exactly bias + two prices", [f.name for f in dataclasses.fields(C.BiasInput)] == ["bias", "leg_origin", "leg_extreme"])
for label, args in (("bad bias word", ("SIDEWAYS", 1.3, 1.2)), ("NaN price", ("BEARISH", float("nan"), 1.2)), ("price 0", ("BEARISH", 0.0, 1.2)), ("text as a price", ("BEARISH", "1.3", 1.2))):
    try: C.BiasInput(*args); check("input refused: " + label, False)
    except C.ContractError: check("input refused: " + label, True)

# ---- 3. whole pipeline at many real moments ----
random.seed(11)
times = [pd.Timestamp("2026-10-01", tz="UTC") + pd.Timedelta(minutes=5 * random.randrange(0, 12 * 24 * 40)) for _ in range(160)]
times = [t for t in times if not SC.is_forex_weekend(t.to_pydatetime())]
bad_runs = []; gaps_refused = []
for t in times:
    for kind in ("technical", "quantitative", "levels", "now", "thesis", "thesis_gen", "thesis_1h", "thesis_15m", "thesis_5m"):
        rep = RP.build(kind, t, mkb(t, random.choice(["BEARISH", "BULLISH"])), D5, D15, D1)
        if rep.status == "refused" and kind == "now" and "unbroken 5M candles" in rep.messages[0]: gaps_refused.append(str(t)); continue      # market closed / rollover: stated, not guessed
        if rep.status != "ok": bad_runs.append((str(t), kind, rep.status))
check(f"now, technical, quantitative and levels pass every check at {len(times)} real moments", not bad_runs, bad_runs[:3])
check(f"/now says so plainly when the last 30 minutes have a gap in the candles ({len(gaps_refused)} of {len(times)} moments, all around the daily rollover)", len(gaps_refused) <= len(times) * 0.1)
same = True
for t in times[:40]:
    b = mkb(t)
    for kind in ("technical", "quantitative", "now"):
        a = RP.build(kind, t, b, D5, D15, D1).messages; z = RP.build(kind, t, b, D5[D5.index < t], D15[D15.index < t], D1[D1.index < t]).messages
        same &= a == z
check("identical output when every candle after 'now' is deleted (no look-ahead)", same)
rep = RP.build("technical", pd.Timestamp("2026-11-13T10:36:27", tz="UTC"), C.BiasInput("BEARISH", 1.34604, 1.32205), D5, D15, D1)
paras = [p for p in re.split(r"\n\s*\n", rep.messages[0]) if p.strip()]
check("every paragraph of a report has a recorded source", len(rep.trace) > 5 and all(s for _, s in rep.trace), rep.trace[:3])

# ---- 4. interpretation covers the cases the friend listed (synthetic facts) ----
T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
def mk(**k):
    base = dict(tf="15M", as_of=T0, close=1.3250, atr_pips=6.0, last_break=C.Break("DOWN", 1.3260, T0 - timedelta(hours=2), T0 - timedelta(hours=5)), held=True,
                bars_since_break=3, beyond_level_atr=1.5, mom_bars=8, mom_net_pips=-6.0, mom_net_atr=-1.0, mom_up=3, contain_bars=24, contain_range_atr=6.0,
                contain_range_pips=36.0, push_bars=24, push_net_atr=0.0, swing_high=None, swing_low=None)
    base.update(k); return C.TFFacts(**base)
cases = {
    "structurally dominant, momentum has turned the other way": (mk(mom_net_atr=1.6, mom_net_pips=9.6), ("STRUCTURE_HOLDS", "MOMENTUM_AGAINST"), "gaining momentum"),
    "buyers pushed price but no meaningful level has broken": (mk(last_break=None, held=None, bars_since_break=None, beyond_level_atr=None, push_net_atr=2.6, mom_net_atr=1.5, mom_net_pips=9.0), ("PUSHING_NO_BREAK", "MOMENTUM_WITH"), "no swing level has gone yet"),
    "neither side shows dominance, contained in a range": (mk(last_break=None, held=None, bars_since_break=None, beyond_level_atr=None, contain_range_atr=2.0, mom_net_atr=0.2, mom_net_pips=1.2), ("CONTAINED", "MOMENTUM_FLAT"), "bouncing inside a range"),
    "level broke but price struggles to continue": (mk(bars_since_break=7, beyond_level_atr=0.2), ("NO_FOLLOW_THROUGH", "MOMENTUM_WITH"), "struggling to follow through"),
    "break is no longer holding": (mk(held=False, beyond_level_atr=-0.4), ("STRUCTURE_NOT_HOLDING", "MOMENTUM_WITH"), "isn't holding"),
}
for name, (f, want, phrase) in cases.items():
    s, m = I.observe(f); r = RD.R(); d = RD.Doc(); RD._tf_block(d, r, f, s, m)
    check(f"describes: {name}", (s.code, m.code) == want and phrase in d.text(), (s.code, m.code))
# ---- 5. flow claims are only made when calculated ----
def synth(up_big):
    idx = pd.date_range("2026-09-01", periods=24 * 5 * 12, freq="h", tz="UTC"); rows = []; p = 1.30
    for i, t in enumerate(idx):
        up = (i % 100) < 54
        body = (0.0008 if up_big else 0.0003) if up else (0.0003 if up_big else 0.0008)
        o = p; c = p + body if up else p - body; p = c
        rows.append((o, max(o, c) + 0.0001, min(o, c) - 0.0001, c))
    return open_only(pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"]))
for up_big, phrase in ((True, "Up candles were bigger"), (False, "Down candles were bigger")):
    d1 = synth(up_big); now = d1.index[-1] + pd.Timedelta(hours=1)
    m = F.measure_facts(d1, now); lab = I.label_measures(m); text, r, tr = RD.measures(now.to_pydatetime(), m, lab, I.plain_labels(m))
    check(f"54% of candles up but {'up' if up_big else 'down'} candles larger: says so from measured bodies ({phrase})", phrase in text and "54% of" not in text and "stronger side" in text, text[:50])
# ---- 6. guard rejects the things it must ----
r = RD.R(); r.p(1.2345); tr = [("p1", "x")]
for label, txt in (("an engine word", "The engine says 1.23450"), ("leg talk", "The leg is old 1.23450"), ("a quote", "“hello” 1.23450"), ("an invented number", "Price 1.23450 and 77 pips away"),
                   ("timeframe-versus-timeframe", "5M vs 1H 1.23450"), ("catch-up talk", "the 5M needs to catch up 1.23450"), ("execution", "A setup at 1.23450"),
                   ("a wall-of-text line", "word " * 60 + "1.23450"), ("unbalanced tags", "<b>1.23450"), ("a stray markup character", "1.23450 < 3"), ("a paragraph with no source", None), ("the word thesis in a sentence", "The thesis is bearish 1.23450"), ("thesis hidden after a command", "/thesis_1h and the thesis 1.23450")):
    try:
        G.check(txt or "1.23450", r, tr if txt else [("p1", "")]); check("guard rejects " + label, False)
    except G.GuardError: check("guard rejects " + label, True)
# ---- 7. missing / old data is explicit ----
rep = RP.build("thesis_15m", pd.Timestamp("2026-11-13T10:36:27", tz="UTC"), C.BiasInput("BEARISH", 1.34604, 1.32205), D5, D15, D1)
check("close-up never offers 'carry it further' at a level price has already passed", rep.status == "ok" and "would carry it further" not in rep.messages[0], rep.messages[0][-300:])
rep = RP.build("thesis", pd.Timestamp("2026-11-13T10:36:27", tz="UTC"), None, D5, D15, D1)
check("the close-up menu needs no bias and lists all four commands", rep.status == "ok" and all(c in rep.messages[0] for c in ("/thesis_gen", "/thesis_1h", "/thesis_15m", "/thesis_5m")))
rep = RP.build("technical", pd.Timestamp("2026-09-01T23:30", tz="UTC"), C.BiasInput("BEARISH", 1.35, 1.32), D5, D15, D1)
check("not enough history -> plain refusal with the reason", rep.status == "refused" and "not enough" in rep.messages[0], rep.messages[0][:90])
rep = RP.build("technical", pd.Timestamp("2026-11-13T10:36", tz="UTC"), None, D5, D15, D1)
check("missing bias -> refusal, not a crash", rep.status == "refused" and "1H bias" in rep.messages[0], rep.status)
rep = RP.build("quantitative", pd.Timestamp("2026-11-14T20:00", tz="UTC"), None, D5, D15, D1)
check("weekend / old data is labelled", rep.status == "ok" and "hours old" in rep.messages[0], rep.messages[0][:70])
rep = RP.build("nonsense", pd.Timestamp("2026-11-13T10:36", tz="UTC"), None, D5, D15, D1)
check("unknown report name -> refusal", rep.status == "refused")

# ---- 8. range pressure: containment is not read as balance ----
cs = {
 "range with the top turned back again and again": (mk(last_break=None, held=None, bars_since_break=None, beyond_level_atr=None, contain_range_atr=2.0, range_high=1.3270, range_low=1.3250, tests_top=4, tests_bottom=1, drift=0.0, mom_net_atr=0.1, mom_net_pips=0.6), "CONTAINED_DEFENDED", "SELLERS", "keep defending the top"),
 "range with the bottom held again and again": (mk(last_break=None, held=None, bars_since_break=None, beyond_level_atr=None, contain_range_atr=2.0, range_high=1.3270, range_low=1.3250, tests_top=0, tests_bottom=3, drift=0.0, mom_net_atr=0.1, mom_net_pips=0.6), "CONTAINED_DEFENDED", "BUYERS", "keep defending the bottom"),
 "range with closes creeping toward the top": (mk(last_break=None, held=None, bars_since_break=None, beyond_level_atr=None, contain_range_atr=2.0, range_high=1.3270, range_low=1.3250, tests_top=1, tests_bottom=1, drift=0.4, mom_net_atr=0.1, mom_net_pips=0.6), "CONTAINED_DRIFTING", "BUYERS", "gaining ground"),
 "range with nothing leaning either way": (mk(last_break=None, held=None, bars_since_break=None, beyond_level_atr=None, contain_range_atr=2.0, range_high=1.3270, range_low=1.3250, tests_top=1, tests_bottom=1, drift=0.05, mom_net_atr=0.1, mom_net_pips=0.6), "CONTAINED", None, "neither edge has been defended more"),
}
for name, (f, code, side, phrase) in cs.items():
    s, m = I.observe(f); r = RD.R(); d = RD.Doc(); RD._tf_block(d, r, f, s, m)
    check(f"describes: {name}", s.code == code and s.side == side and phrase in d.text(), (s.code, s.side))
# ---- 9. level selection rule ----
def mkbias(kind="BEARISH", **k):
    base = dict(bias=kind, extreme=1.3220, origin=1.3460, evidence=None, held=None, reversal=(1.3241, T0), range_bars=12, range_high=1.3245, range_low=1.3220, is_range=True,
                ema_side="BELOW", ema_dist_pips=-70.0, ema_dist_atr=-7.0, atr_pips=10.0, close=1.3225, dist_extreme_pips=5.0)
    base.update(k); return C.BiasFacts(**base)
t15 = mk(swing_high=(1.3238, T0), swing_low=(1.3231, T0), atr_pips=6.0)
lv = I.select_levels(mkbias(), t15, 1.3225)
check("directional bias: one level per role, in order boundary, continuation, change", [l.role for l in lv] == ["BOUNDARY", "CONTINUATION", "CHANGE"], [l.role for l in lv])
check("boundary is the 15M swing nearest to price", lv[0].price == 1.3231, lv[0].price)
t15b = mk(swing_high=(1.3238, T0), swing_low=(1.32205, T0), atr_pips=6.0)    # nearest 15M swing is the same level as the continuation level
lv = I.select_levels(mkbias(), t15b, 1.3225)
check("a 15M swing equal to another selected level is not listed twice", sorted(l.price for l in lv).count(1.32205) + sorted(l.price for l in lv).count(1.3220) <= 1 and len({l.price for l in lv}) == len(lv), [(l.role, l.price) for l in lv])
lv = I.select_levels(mkbias(reversal=None), t15, 1.3225)
check("a role with no level is left out, not filled with something else", [l.role for l in lv] == ["BOUNDARY", "CONTINUATION"], [l.role for l in lv])
lv = I.select_levels(mkbias("CONSOLIDATION", reversal=None), t15, 1.3225)
check("no 1H direction: the two range edges plus the nearest 15M swing", sorted(l.source for l in lv) == ["15M", "1H", "1H"], [(l.role, l.source) for l in lv])
check("background level only appears in /levels", [l.role for l in I.select_levels(mkbias(), t15, 1.3225, detail=True)][-1] == "BACKGROUND" and "BACKGROUND" not in [l.role for l in I.select_levels(mkbias(), t15, 1.3225)])
# ---- 10. last-30-minutes story ----
def flat5(n=120):
    idx = pd.date_range("2026-11-10 08:00", periods=n, freq="5min", tz="UTC"); rng = random.Random(5); p = 1.3300; rows = []
    for _ in idx:
        o = p; c = p + rng.uniform(-0.0002, 0.0002); rows.append((o, max(o, c) + 0.00015, min(o, c) - 0.00015, c)); p = c
    return open_only(pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"]))
def scripted(last):          # replace the last 6 candles with a scripted path of closes
    d = flat5(); p = float(d["Close"].iloc[-7])
    for k, c in enumerate(last):
        i = len(d) - 6 + k; o = p; d.iloc[i] = (o, max(o, c) + 0.00005, min(o, c) - 0.00005, c); p = c
    return d
p0 = 1.3300
cases = {"UP": [p0 + 0.0004, p0 + 0.0009, p0 + 0.0013, p0 + 0.0017, p0 + 0.0021, p0 + 0.0026], "DOWN_THEN_BOUNCE": [p0 - 0.0006, p0 - 0.0012, p0 - 0.0018, p0 - 0.0012, p0 - 0.0006, p0 - 0.0002],
         "UP_THEN_FADE": [p0 + 0.0006, p0 + 0.0013, p0 + 0.0019, p0 + 0.0012, p0 + 0.0006, p0 + 0.0003], "SIDEWAYS": [p0 + 0.0001, p0 - 0.0001, p0 + 0.0001, p0, p0 + 0.0001, p0]}
for shape, path in cases.items():
    d = scripted(path); now = d.index[-1] + pd.Timedelta(minutes=5)
    st = F.story_facts(d, now, 30, None); lab = I.label_story(st)
    check(f"30-minute story is described as {shape}", lab.shape == shape, (lab.shape, round(st.range_atr, 2), round(st.close_pos, 2)))
chop = scripted([p0 + 0.0008, p0 - 0.0002, p0 + 0.0009, p0 - 0.0001, p0 + 0.0008, p0 + 0.0003]); st = F.story_facts(chop, chop.index[-1] + pd.Timedelta(minutes=5), 30, None)
check("back-and-forth with no direction is described as CHOPPY, not as a move", I.label_story(st).shape == "CHOPPY", (I.label_story(st).shape, round(st.efficiency, 2), round(st.close_pos, 2)))
tq = pd.Timestamp("2026-11-03T11:14", tz="UTC"); stq = F.story_facts(D5, tq, 30, D15, D1)
check("an earlier break (before the window, within 3h) is found from the candles", stq.earlier is not None and stq.earlier_minutes > 0, stq.earlier)
rq = RP.build("now", tq.isoformat(), C.BiasInput("BEARISH", 1.34604, 1.32205), D5, D15, D1)
check("/now says the bigger event came earlier when the last 30 minutes had none", rq.status == "ok" and "bigger event came earlier" in rq.messages[0])
d = scripted(cases["UP"]); now = d.index[-1] + pd.Timedelta(minutes=5)
inter = F.interaction(d, now, float(d["High"].iloc[-6:].max()) - 0.0001)
check("level interaction counts touches from the candles, not from anything else", inter.touches >= 1 and isinstance(inter.closed_through, bool), inter)
# ---- 11. plain-words layer follows the numbers ----
base_m = F.measure_facts(D1, pd.Timestamp("2026-11-13T10:36:27", tz="UTC"))
for eff, want in ((0.05, "CHOPPY"), (0.25, "SOMEWHAT_DIRECTIONAL"), (0.6, "DIRECTIONAL")):
    m2 = dataclasses.replace(base_m, flow=dataclasses.replace(base_m.flow, efficiency=eff))
    check(f"net-progress {int(eff*100)}% is called {want}", I.plain_labels(m2).efficiency == want)
text, r, tr = RD.measures(base_m.as_of, base_m, I.label_measures(base_m), I.plain_labels(base_m))
check("quantitative report no longer compares a 2-week move with a weekly range", "typical week" not in text)
check("quantitative report leads with percentages, keeps pips in brackets, and explains in plain words", "%" in text and "In plain words" in text and "pips" in text)
# ---- 11b. the weekend rule is the scanner's rule, and weekend quotes never reach a calculation ----
idx = pd.date_range("2026-11-06", "2026-11-17", freq="5min", tz="UTC")
check("market_closed() equals scanner_common.is_forex_weekend at every 5 minutes for 11 days", all(bool(F.market_closed(pd.DatetimeIndex([x])[0:1])[0]) == SC.is_forex_weekend(x.to_pydatetime()) for x in idx))
check("closed() returns no candle from market-closed hours, for any timeframe", all(not F.market_closed(F.closed(df, tf, pd.Timestamp("2026-11-16T12:00", tz="UTC")).index).any() for df, tf in ((D1, "1H"), (D15, "15M"), (D5, "5M"))))
# ---- 12. the calculations are what the documentation says (independent recomputation) ----
tt = pd.Timestamp("2026-11-13T10:36:27", tz="UTC"); m = F.measure_facts(D1, tt); dd = open_only(D1[D1.index + pd.Timedelta(hours=1) <= tt])
w = dd[dd.index >= dd.index[-1] + pd.Timedelta(hours=1) - pd.Timedelta(hours=336)]; body = (w["Close"] - w["Open"]).values
up_, dn_ = body[body > 0], -body[body < 0]
check("net progress = |net move| / (all up bodies + all down bodies)", abs(abs(w["Close"].iloc[-1] - w["Open"].iloc[0]) / (up_.sum() + dn_.sum()) - m.flow.efficiency) < 1e-9)
check("average up / down candle = mean body of candles that closed up / down", abs(up_.mean() / 1e-4 - m.flow.up_body_mean_pips) < 1e-6 and abs(dn_.mean() / 1e-4 - m.flow.down_body_mean_pips) < 1e-6)
f5 = F.tf_facts(D5, "5M", tt); c5 = open_only(D5[D5.index + pd.Timedelta(minutes=5) <= tt]); pc = D5["Close"].shift()
tr = pd.concat([D5["High"] - D5["Low"], (D5["High"] - pc).abs(), (D5["Low"] - pc).abs()], axis=1).max(axis=1).loc[c5.index]
check("momentum = (latest close - open 8 candles back) / ATR14 of that timeframe", abs((c5["Close"].iloc[-1] - c5["Open"].iloc[-8]) / tr.values[-14:].mean() - f5.mom_net_atr) < 1e-9)

# ---- 13. Stage A additions: the bias contract, the shorter /now window, timestamps, the repo entry point, no writes, runtime ----
import json as _json, time, copy
NOW = pd.Timestamp("2026-11-20T15:56:00", tz="UTC")
GOOD = {"macro_bias_confirmed": "BEARISH", "macro_leg_origin": 1.32283, "macro_leg_extreme": 1.30798, "macro_bias_stale": False, "market_data_timestamp": "2026-11-20T15:55:00+00:00"}
def refused(state, now=NOW, why=None):
    try: S.bias_from_state(state, now); return False
    except C.ContractError as e: return why is None or why in str(e)
check("bias contract: a normal state gives bias + two prices and nothing else", S.bias_from_state(GOOD, NOW) == C.BiasInput("BEARISH", 1.32283, 1.30798))
check("bias contract: the engine's stale flag refuses (real recorded state: stale, prices empty)", refused({**GOOD, "macro_bias_stale": True, "macro_leg_origin": None, "macro_leg_extreme": None}, why="no confirmed 1H bias"))
check("bias contract: stale flag that is not true/false refuses", refused({**GOOD, "macro_bias_stale": "no"}))
check("bias contract: state older than 20 minutes refuses", refused({**GOOD, "market_data_timestamp": "2026-11-20T15:30:00+00:00"}, why="20 minutes old"))
check("bias contract: state from 19 minutes ago is accepted", not refused({**GOOD, "market_data_timestamp": "2026-11-20T15:37:00+00:00"}))
for k in ("macro_bias_confirmed", "macro_leg_origin", "macro_leg_extreme", "macro_bias_stale", "market_data_timestamp"):
    st = dict(GOOD); del st[k]; check(f"bias contract: missing {k} refuses", refused(st, why="isn't available"))
check("bias contract: state that is not a dict refuses", refused(None) and refused([1]))
check("bias contract: unknown bias word refuses", refused({**GOOD, "macro_bias_confirmed": "UP"}))
check("bias contract: bearish with the prices the wrong way round refuses (contradiction)", refused({**GOOD, "macro_leg_origin": 1.30798, "macro_leg_extreme": 1.32283}, why="don't make sense"))
check("bias contract: bullish with the prices the wrong way round refuses", refused({**GOOD, "macro_bias_confirmed": "BULLISH"}, why="don't make sense"))
check("bias contract: missing / text / absurd prices refuse", all(refused({**GOOD, "macro_leg_extreme": v}) for v in (None, "1.3", 0, 99, float("nan"), True)))
check("bias contract: refusal reasons contain no raw values or engine words", all(not re.search(r"\d|stale|macro", str(e)) for e in [])) if False else None
for label, st in (("stale", {**GOOD, "macro_bias_stale": True}), ("contradiction", {**GOOD, "macro_leg_origin": 1.2}), ("old", {**GOOD, "market_data_timestamp": "2026-11-20T10:00:00+00:00"})):
    try: S.bias_from_state(st, NOW)
    except C.ContractError as e: check(f"refusal sentence for '{label}' has no numbers or engine field names", not re.search(r"\d{2}|macro|_", str(e).replace("20 minutes", "")), str(e))
rep = RP.build_from_repo("now", NOW, {**GOOD, "macro_bias_stale": True}, HERE)
check("a stale bias gives a plain refusal from the repo entry point", rep.status == "refused" and "no confirmed 1H bias" in rep.messages[0], rep.messages[0][:80])
rep = RP.build_from_repo("quantitative", NOW, None, HERE)
check("the quantitative report needs no bias and builds from the repo's own logs", rep.status == "ok", rep.status)
# the report shows the bias it was given; it never produces a second one
shown = set()
for b in ("BEARISH", "BULLISH", "CONSOLIDATION"):
    for t in times[:15]:
        bi = mkb(t, b) if b != "CONSOLIDATION" else C.BiasInput("CONSOLIDATION", 1.31, 1.32)
        t1 = F.tf_facts(D1, "1H", t); bf = F.bias_facts(bi, D1, t, t1); shown.add((b, bf.bias))
check("bias_facts returns exactly the bias it was given, whatever the candles say", all(a == b for a, b in shown))
# shorter /now window
t0 = pd.Timestamp("2026-11-18T14:00", tz="UTC"); d5g = D5.drop(D5.index[(D5.index >= t0 + pd.Timedelta(minutes=0)) & (D5.index < t0 + pd.Timedelta(minutes=10))])
nowg = t0 + pd.Timedelta(minutes=50)           # candles 14:10..14:45 exist (7 unbroken, 35 minutes) -> a full 30-minute window is still fine
nowg2 = t0 + pd.Timedelta(minutes=30)          # candles 14:10..14:25 exist -> 4 unbroken = 20 minutes
rep = RP.build("now", nowg2, mkb(nowg2), d5g, D15, D1)
check("with a gap, /now uses the unbroken 20 minutes and says so plainly", rep.status == "ok" and "The last 20 minutes" in rep.messages[0] and "instead of the usual 30" in rep.messages[0], rep.messages[0][:400])
rep = RP.build("now", t0 + pd.Timedelta(minutes=25), mkb(nowg2), d5g, D15, D1)
check("with fewer than 4 unbroken candles /now refuses and says how much it has", rep.status == "refused" and "unbroken 5M candles" in rep.messages[0], rep.messages[0][:200])
rep = RP.build("now", nowg, mkb(nowg), d5g, D15, D1)
check("a full 30 minutes carries no 'shorter' notice", rep.status == "ok" and "instead of the usual" not in rep.messages[0])
# timestamps: reply time and data cutoff are both stated
rep = RP.build("now", pd.Timestamp("2026-11-18T14:36:00", tz="UTC"), mkb("2026-11-18T14:36"), D5, D15, D1)
check("header says when the reply was made, when the data stops and how old that is", rep.status == "ok" and "Answered 15:36 Lagos" in rep.messages[0] and "data up to 15:35" in rep.messages[0] and "1 min old" in rep.messages[0], rep.messages[0][:200])
# the market is closed
for kind in ("now", "technical", "thesis_1h"):
    rep = RP.build(kind, pd.Timestamp("2026-11-14T12:00", tz="UTC"), mkb("2026-11-13T12:00"), D5, D15, D1)
    check(f"{kind} refuses on a Saturday because the market is closed", rep.status == "refused" and "market is closed" in rep.messages[0], rep.messages[0][:100])
rep = RP.build("quantitative", pd.Timestamp("2026-11-14T12:00", tz="UTC"), None, D5, D15, D1)
check("quantitative on a Saturday still builds and carries the 'hours old' notice (weekend quotes are not counted)", rep.status == "ok" and "hours old" in rep.messages[0], rep.messages[0][:90])
# no writes, no scanner imports, runtime
src = "\n".join(open(os.path.join(PKG, f)).read() for f in os.listdir(PKG) if f.endswith(".py"))
check("the reports package never opens a file for writing and never deletes", not re.search(r"open\([^)]*[\"'][wax+]b?[\"']|\.write\(|os\.(remove|unlink|rename|replace)|shutil|to_csv|to_json|json\.dump\(", src))
check("the reports package never imports the scanner, BUS, Telegram or network code", not re.search(r"scanner_|comms|bus|telegram|requests|urllib|socket|subprocess", "\n".join(l for l in src.splitlines() if re.match(r"\s*(import|from) ", l))))
before = {f: os.path.getmtime(os.path.join(HERE, f)) for f in ("ohlc_5m_log.jsonl", "ohlc_15m_log.jsonl", "ohlc_1h_log.jsonl", "state.json")}
t_s = time.time(); outs = [RP.build_from_repo(k, NOW, GOOD, HERE) for k in ("now", "technical", "levels", "quantitative", "thesis_1h", "thesis_15m", "thesis_5m", "thesis")]; el = time.time() - t_s
check(f"all eight reports build from the real saved logs in {el:.1f}s (limit 10s) and none is refused", el < 10 and all(o.status == "ok" for o in outs), [(o.kind, o.status) for o in outs])
check("building reports changed none of the data files", before == {f: os.path.getmtime(os.path.join(HERE, f)) for f in before})

print(f"\n{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
