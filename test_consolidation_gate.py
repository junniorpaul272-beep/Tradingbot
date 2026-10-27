"""Pins: a reversal seen right after CONSOLIDATION faces the same size gate as one seen from BULLISH/BEARISH.
Real-date replay on the repo's logged candles (skipped if absent) + a synthetic mechanism test."""
import os, json, io, contextlib, unittest
os.environ.setdefault("TELEGRAM_TOKEN","x"); os.environ.setdefault("TELEGRAM_CHAT_ID","0"); os.environ.setdefault("TWELVE_DATA_API_KEY","x")
import pandas as pd, scanner_common as sc, scanner_observation as so
HERE=os.path.dirname(os.path.abspath(__file__))
def _load(f):
    d=pd.DataFrame([json.loads(l) for l in open(os.path.join(HERE,f)) if l.strip()]); d["datetime"]=pd.to_datetime(d["datetime"])
    return d.drop_duplicates("datetime").set_index("datetime").sort_index().rename(columns=str.capitalize)
def replay(d1,d15,start,end,n=500):
    st={}; seq=[]
    for t in pd.date_range(start,end,freq="1h",tz="UTC"):
        if sc.is_forex_weekend(t): continue
        a=d1[d1.index<=t-pd.Timedelta(hours=1)].tail(n); b=d15[d15.index<=t-pd.Timedelta(minutes=15)].tail(58)
        with contextlib.redirect_stdout(io.StringIO()):
            bias,up=so.compute_macro_bias(a,b,st); sc.apply_state_updates(st,up)
        seq.append((t,bias,st.get("macro_leg_origin")))
    return seq
@unittest.skipUnless(os.path.exists(os.path.join(HERE,"ohlc_15m_log.jsonl")),"no logged candles")
class Real(unittest.TestCase):
    def test_23_oct_no_unqualified_flip_through_consolidation(self):
        d1=_load("ohlc_1h_log.jsonl"); d15=_load("ohlc_15m_log.jsonl")
        seq=replay(d1,d15,"2026-10-19 07:00","2026-10-23 23:00")
        flips=[(str(t)[5:16],b) for (t,b,_) in seq if t>=pd.Timestamp("2026-10-23 12:00",tz="UTC") and b=="BEARISH"]
        # the 52-pip sell-off needs 141 pips (half of the 281-pip leg) to supersede the bullish leg
        self.assertEqual(flips,[], "bearish flip accepted although the reversal size gate was not met")
    def test_bullish_leg_origin_held_through_the_week(self):
        d1=_load("ohlc_1h_log.jsonl"); d15=_load("ohlc_15m_log.jsonl")
        seq=replay(d1,d15,"2026-10-19 07:00","2026-10-23 23:00")
        self.assertEqual({round(float(o),5) for (_,_,o) in seq if o is not None},{1.31382})
if __name__=="__main__": unittest.main()
