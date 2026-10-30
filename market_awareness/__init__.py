"""
market_awareness - the independent GBPUSD market-awareness research body (hourly candles).

INDEPENDENCE RULE (same as observatory): nothing in the live bot imports this folder, and this folder imports nothing from the live bot (stdlib only,
plus pandas/numpy in ema_window_comparison.py). Read-only research: no trade decisions, signals, alerts, bias or BUS use. It reads candle files and writes
reports under market_awareness/results/. See SPEC_MARKET_AWARENESS.md and PLAN_DEVELOPMENT_VALIDATION.md.

Run from the repo root:
  python -m unittest market_awareness.test_awareness market_awareness.test_overlap_check market_awareness.test_compare_repair
  python -m market_awareness.awareness <1h file>                          latest observation
  python -m market_awareness.audit_research_dataset . market_awareness/results/audit_result.json
  python -m market_awareness.overlap_check observatory_data/raw/GBPUSD_1h.jsonl 2020-01-01 2022-12-31 market_awareness/results/overlap_result.json
  python -m market_awareness.compare_repair <orig 1h> <repair 1h> 2021-07-09 2021-10-02
"""
