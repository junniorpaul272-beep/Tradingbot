"""Pins the survival-check retrace bug (27 Oct 2026). Run next to scanner_observation.py."""
import os, unittest
os.environ.setdefault("TELEGRAM_TOKEN","x"); os.environ.setdefault("TELEGRAM_CHAT_ID","0"); os.environ.setdefault("TWELVE_DATA_API_KEY","x")
import pandas as pd, scanner_observation as so

def frame(rows, start="2026-01-05"):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, index=idx, columns=["Open","High","Low","Close"])

def bull(pull_low):
    # origin low 1.3000 (bar0), steady climb to 1.3200, then a pullback to pull_low
    rows=[(1.3000,1.3005,1.3000,1.3004)]
    p=1.3004
    for _ in range(20):
        rows.append((p,p+0.0010,p-0.0002,p+0.0009)); p+=0.0009   # lows stay close to the path, well above origin
    rows.append((p,1.3200,pull_low,pull_low+0.0003))
    return frame(rows)

class T(unittest.TestCase):
    def st(self, df):
        return {"macro_leg_direction":"BULLISH","macro_leg_origin":1.3000,"macro_leg_extreme":1.3200,
                "macro_leg_origin_time":df.index[0].isoformat()}
    def test_healthy_leg_with_shallow_pullback_survives(self):
        df=bull(1.3100)   # 50% pullback of a 200-pip leg
        ok,_=so.check_leg_anchor_survival(self.st(df),"macro_leg",df)
        self.assertTrue(ok)
    def test_deep_pullback_still_kills(self):
        df=bull(1.3030)   # 85% pullback
        ok,u=so.check_leg_anchor_survival(self.st(df),"macro_leg",df)
        self.assertFalse(ok); self.assertTrue(u["macro_leg_last_invalidation_retrace_violated"])
    def test_origin_touch_still_kills(self):
        df=bull(1.2995)
        ok,u=so.check_leg_anchor_survival(self.st(df),"macro_leg",df)
        self.assertFalse(ok); self.assertTrue(u["macro_leg_last_invalidation_origin_violated"])
    def test_bearish_mirror_survives_shallow_pullback(self):
        df=bull(1.3100)
        m=pd.DataFrame({"Open":2.6-df.Open,"High":2.6-df.Low,"Low":2.6-df.High,"Close":2.6-df.Close})
        st={"macro_leg_direction":"BEARISH","macro_leg_origin":2.6-1.3000,"macro_leg_extreme":2.6-1.3200,
            "macro_leg_origin_time":m.index[0].isoformat()}
        ok,_=so.check_leg_anchor_survival(st,"macro_leg",m)
        self.assertTrue(ok)
if __name__=="__main__": unittest.main()
