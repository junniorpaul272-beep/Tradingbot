import os, datetime, json
os.environ['TELEGRAM_TOKEN']='x'; os.environ['TELEGRAM_CHAT_ID']='x'; os.environ['TWELVE_DATA_KEY']='x'
import pandas as pd
import scanner as s

FAILURES = []
def check(label, cond):
    status = 'PASS' if cond else 'FAIL'
    print(f'{status}: {label}')
    if not cond: FAILURES.append(label)

# 1. Weekend gate
check('Fri 20:00 open', s.is_forex_weekend(datetime.datetime(2026,7,17,20,0,tzinfo=datetime.timezone.utc)) == False)
check('Sun 20:00 closed', s.is_forex_weekend(datetime.datetime(2026,7,19,20,0,tzinfo=datetime.timezone.utc)) == True)
check('Sun 23:00 open (reopen fix)', s.is_forex_weekend(datetime.datetime(2026,7,19,23,0,tzinfo=datetime.timezone.utc)) == False)

# 2. Evidence Engine: min-N gate + exact-match + tier isolation
recs = []
for i in range(55):
    recs.append({'tier_number':1,'experiment':'EXP7_TIER_ATR','r_achieved': 2.0 if i%3 else -1.0,
                 'tags':{'order_block':True,'rejection_candle':True,'fresh_bos_aligned':True,'choch':True}})
with open('shadow_trade_log.jsonl','w') as f:
    for r in recs: f.write(json.dumps(r)+'\n')
ev = s.compute_evidence('TIER_1_POI', {'order_block':True,'rejection_candle':True,'fresh_bos_aligned':True,'choch':True})
check('Evidence Engine surfaces at n=55', ev is not None and ev['n']==55)
ev_mismatch = s.compute_evidence('TIER_1_POI', {'order_block':True,'rejection_candle':True,'fresh_bos_aligned':True,'choch':False})
check('Evidence Engine rejects single-fact mismatch', ev_mismatch is None)
os.remove('shadow_trade_log.jsonl')

# 3. Trade ID persistence
_ORIGINAL_LOG_FILE = s.SHADOW_TRADE_LOG_FILE
setup = s.build_shadow_setup('EXP7_TIER_ATR','BUY',1.1000,1.0980, datetime.datetime.now(datetime.timezone.utc), variant='TIER_1_POI', tags={'x':1}, tier_number=1)
s.SHADOW_TRADE_LOG_FILE = 'test_log.jsonl'
if os.path.exists('test_log.jsonl'): os.remove('test_log.jsonl')
s._append_shadow_trade_log(setup, 'WIN', 2.0, datetime.datetime.now(datetime.timezone.utc))
with open('test_log.jsonl') as f: rec = json.loads(f.readline())
check('trade_id persists to permanent log', rec.get('trade_id') == setup['id'])
os.remove('test_log.jsonl')
s.SHADOW_TRADE_LOG_FILE = _ORIGINAL_LOG_FILE  # restore -- every later test depends on this

# 4. fetch_ohlc retry/fallback
calls = {'n':0}
class FakeResp:
    def json(self): return {'values':[{'datetime':'2026-07-18 00:00:00','open':'1.27','high':'1.28','low':'1.26','close':'1.275'}]*3}
def flaky(*a,**kw):
    calls['n']+=1
    if calls['n']<3: raise Exception('timeout')
    return FakeResp()
s.requests.get = flaky
s.time.sleep = lambda x: None
df = s.fetch_ohlc('5min')
check('fetch_ohlc recovers after retries', df is not None and calls['n']==3)

# 5. OB mitigation
def make_df(rows, freq='15min'):
    idx = pd.date_range('2026-07-01', periods=len(rows), freq=freq, tz='UTC')
    return pd.DataFrame(rows, index=idx, columns=['Open','High','Low','Close'])
rows = [
    [1.005,1.02,1.00,1.01],[1.010,1.01,0.99,1.00],[1.000,1.00,0.95,0.98],
    [0.980,1.03,0.98,1.02],[1.020,1.05,1.00,1.04],[1.040,1.06,1.02,1.05],
    [1.050,1.10,1.03,1.06],[1.060,1.08,1.04,1.05],[1.050,1.07,1.03,1.04],
    [1.040,1.09,1.02,1.06],[1.060,1.12,1.05,1.11],
]
bos = {'direction':'BULLISH','impulse_start':0.95,'impulse_end':1.12,'origin_idx':2}
df_a = make_df(rows)
ob_a = s.detect_order_block(df_a, bos, pd.Series([0.0008]*len(df_a), index=df_a.index))
check('OB mitigation: no re-entry -> False', ob_a['mitigated'] == False)

# 6. Sweep distance
rows_5m = [[1.1005,1.1008,1.1002,1.1004]]*15 + [[1.1002,1.1005,1.0985,1.1003]] + [[1.1003,1.1006,1.1001,1.1004]]*4
df_5m = make_df(rows_5m, freq='5min')
df_15m = make_df([[1.1000,1.1010,1.0985,1.1008]]*3, freq='15min')
swept, label, dist = s.detect_liquidity_sweep(df_5m, df_15m, 1.1000, 'BULLISH')
check('Sweep distance calc', swept and dist == 15.0)

# 7. Market Intelligence Network rename didn't break the dashboard
summary = s.format_shadow_summary({'EXP1_STRUCTURE': {**s._empty_experiment_stat(), 'logged':5,'resolved':5,'wins':3,'sum_r':4.0}})
check('Dashboard renamed + still renders', summary is not None and 'Market Intelligence Network' in summary)

# 8. Regime classification (pure tagging, no decision logic)
class _FakeFacts:
    def __init__(self, atr_pct, hour):
        self._atr_pct = atr_pct
        self.now_utc = datetime.datetime(2026, 7, 20, hour, 0, tzinfo=datetime.timezone.utc)
    def atr_percentile_15m(self):
        return self._atr_pct
class _FakeCtx:
    def __init__(self, post_spike): self.post_spike_active = post_spike

r1 = s.classify_regime(_FakeFacts(10, 9), _FakeCtx(False), {"macro_bias_stale": False})
check('Regime: low_vol + london_early + fresh + normal',
      r1 == {"atr_bucket": "low_vol", "session": "london_early", "bias_state": "fresh", "spike_state": "normal"})
r2 = s.classify_regime(_FakeFacts(90, 14), _FakeCtx(True), {"macro_bias_stale": True})
check('Regime: high_vol + overlap + stale + post_spike',
      r2 == {"atr_bucket": "high_vol", "session": "london_ny_overlap", "bias_state": "stale", "spike_state": "post_spike"})

# 9. IC correlation (known-correlation synthetic dataset)
ic_records = []
for i in range(50):
    ic_records.append({'experiment': 'EXP7_TIER_ATR', 'variant': 'TIER_1_POI',
                        'r_achieved': -1.0 + (i / 49) * 4.0,
                        'tags': {'leg_length_pips': 20 + i}})
with open('shadow_trade_log.jsonl', 'w') as f:
    for r in ic_records: f.write(json.dumps(r) + '\n')
ic_result = s.compute_ic('TIER_1_POI', 'leg_length_pips')
check('IC detects strong positive correlation', ic_result is not None and ic_result['ic'] > 0.95)
check('IC withholds below min_n', s.compute_ic('TIER_1_POI', 'break_count') is None)
os.remove('shadow_trade_log.jsonl')

# 10. Failure Investigation Bureau
fib_records = [{'experiment': 'EXP7_TIER_ATR', 'variant': 'TIER_2_FIB', 'trade_id': f'w{i}',
                'r_achieved': 2.0, 'tags': {'leg_length_pips': 80.0}} for i in range(15)]
fib_records.append({'experiment': 'EXP7_TIER_ATR', 'variant': 'TIER_2_FIB', 'trade_id': 'the_loser',
                     'r_achieved': -1.0, 'tags': {'leg_length_pips': 15.0}})
with open('shadow_trade_log.jsonl', 'w') as f:
    for r in fib_records: f.write(json.dumps(r) + '\n')
fib_report = s.format_failure_investigation('the_loser')
check('Failure Investigation compares vs tier winners', 'the_loser' in fib_report and '15.0' in fib_report and '80.0' in fib_report)
check('Failure Investigation handles missing id', 'No resolved EXP7 trade found' in s.format_failure_investigation('nonexistent'))
os.remove('shadow_trade_log.jsonl')

# 11. was_choch + compute_market_state
quiet = [[1.1000, 1.1005, 1.0998, 1.1002]] * 30
expanding = []
price = 1.1002
for i in range(10):
    price += 0.0015
    expanding.append([price - 0.0015, price + 0.0010, price - 0.0025, price])
df_15m_ms = make_df(quiet + expanding, freq='15min')
df_5m_ms = make_df([[1.1000, 1.1005, 1.0998, 1.1002]] * 50, freq='5min')
df_1h_ms = make_df([[1.1000, 1.1010, 1.0990, 1.1005]] * 30, freq='1h')
facts_ms = s.MarketFacts(df_15m=df_15m_ms, df_5m=df_5m_ms, df_1h=df_1h_ms, macro_bias="BULLISH",
                          swing_high=1.15, swing_low=0.95, now_utc=df_15m_ms.index[-1].to_pydatetime())
bos_ms = {"direction": "BULLISH", "impulse_start": 1.10, "impulse_end": 1.12, "was_choch": True}
state_ms = s.compute_market_state(facts_ms, bos_ms)
check('Market state detects expanding volatility', state_ms['volatility_state'] == 'expanding')
check('Market state handles bos=None without crashing',
      s.compute_market_state(facts_ms, None)['trend_strength_atr_mult'] is None)

print()
print('TOTAL FAILURES:', len(FAILURES))
if FAILURES:
    raise SystemExit(1)
