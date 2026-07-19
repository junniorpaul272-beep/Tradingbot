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
setup = s.build_shadow_setup('EXP7_TIER_ATR','BUY',1.1000,1.0980, datetime.datetime.now(datetime.timezone.utc), variant='TIER_1_POI', tags={'x':1}, tier_number=1)
s.SHADOW_TRADE_LOG_FILE = 'test_log.jsonl'
if os.path.exists('test_log.jsonl'): os.remove('test_log.jsonl')
s._append_shadow_trade_log(setup, 'WIN', 2.0, datetime.datetime.now(datetime.timezone.utc))
with open('test_log.jsonl') as f: rec = json.loads(f.readline())
check('trade_id persists to permanent log', rec.get('trade_id') == setup['id'])
os.remove('test_log.jsonl')

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

print()
print('TOTAL FAILURES:', len(FAILURES))
if FAILURES:
    raise SystemExit(1)
