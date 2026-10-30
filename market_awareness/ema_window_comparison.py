"""ema_window_comparison.py - RESEARCH ONLY. How different is an EMA(100) of the hourly close computed on a short trailing window (as the live engine
does: 120 candles) from the same EMA computed on the full history, or on 500 candles?
Method (so it can be checked): research file GBPUSD_1h.jsonl, cleaned with awareness.clean_hourly (UTC, weekend rows dropped). For every 97th cleaned
candle from index 2000 onward and BEFORE the 18 Jun 2026 holdout: full = EMA from the first candle; short = EMA over the trailing N candles only
(pandas ewm(span=100, adjust=False), seeded with the first close of the window). Difference in pips of the last value. Prints counts, quantiles and worst dates.
This does NOT test whether 500 candles suits the strategy; it only measures start-value sensitivity. Usage: python3 ema_window_comparison.py <1h file>"""
import sys, numpy as np, pandas as pd
from . import awareness as aw
c, _ = aw.clean_hourly(aw.read_jsonl(sys.argv[1])); s = pd.Series([x[4] for x in c], index=[x[0] for x in c])
full = s.ewm(span=100, adjust=False).mean(); pts = [i for i in range(2000, len(s), 97) if s.index[i][:10] < "2026-06-18"]
print("samples:", len(pts), "from", s.index[pts[0]], "to", s.index[pts[-1]])
for n in (120, 200, 300, 500):
    d = np.array([abs(s.iloc[i - n + 1:i + 1].ewm(span=100, adjust=False).mean().iloc[-1] - full.iloc[i]) / 0.0001 for i in pts])
    worst = [(s.index[pts[j]][:16], round(float(d[j]), 1)) for j in np.argsort(-d)[:3]]
    print(f"trailing {n:>3} candles: median {np.median(d):5.2f}  p90 {np.percentile(d,90):5.2f}  max {d.max():5.2f} pips   worst {worst}")
