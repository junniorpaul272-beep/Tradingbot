"""source.py - the only door into reports from outside: the candle logs and the bias values in state.json.
It reads and validates. It writes nothing, has no wording and no analysis, and imports only contracts.

THE BIAS CONTRACT (one authority, never recomputed here)
  Read from state.json:
    macro_bias_confirmed    the bias. The engine's value is shown as given; reports never calculate a second one.
    macro_leg_origin        where the bias move began
    macro_leg_extreme       the far end of the bias move
    macro_bias_stale        True when the engine has no confirmed bias right now (its prices are then empty)
    market_data_timestamp   when state.json was last written
  Refuse, with a plain reason, when: the state or a key is missing / the wrong type; the bias is not BULLISH/BEARISH/CONSOLIDATION;
  macro_bias_stale is True (or not a true/false value); state.json is older than 20 minutes at the moment of the request;
  a price is missing, not a number, or outside 0.5-5; the two prices contradict the direction (BEARISH needs origin above extreme,
  BULLISH needs origin below).
  Only bias + origin + extreme go on to the report (BiasInput). The stale flag and the data time are used here and stop here.
"""
import json
import math
from datetime import datetime, timedelta, timezone
import pandas as pd
from .contracts import BiasInput, ContractError

MAX_STATE_AGE = timedelta(minutes=20)      # same freshness rule BUS already uses for current-state answers
KEYS = ("macro_bias_confirmed", "macro_leg_origin", "macro_leg_extreme", "macro_bias_stale", "market_data_timestamp")


def read_candles(path):
    """ohlc_*_log.jsonl -> DataFrame (UTC index, columns Open/High/Low/Close). Bad lines are skipped; duplicates keep the first."""
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            try: r = json.loads(line)
            except ValueError: continue
            if all(k in r for k in ("datetime", "open", "high", "low", "close")): rows.append(r)
    if not rows: raise ContractError("the candle log is empty")
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime"]).drop_duplicates("datetime").set_index("datetime").sort_index()
    df.columns = [c.capitalize() for c in df.columns]
    return df[["Open", "High", "Low", "Close"]].astype(float)


def bias_from_state(state, now):
    """state.json contents (a dict) + the time of the request -> BiasInput, or ContractError with a plain reason."""
    if not isinstance(state, dict): raise ContractError("the 1H bias isn't available")
    missing = [k for k in KEYS if k not in state]
    if missing: raise ContractError("the 1H bias isn't available")
    stale = state["macro_bias_stale"]
    if not isinstance(stale, bool): raise ContractError("the 1H bias isn't available")
    if stale: raise ContractError("there is no confirmed 1H bias right now")
    ts = pd.to_datetime(state["market_data_timestamp"], utc=True, errors="coerce")
    if pd.isna(ts): raise ContractError("the 1H bias isn't available")
    now = pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    if now - ts > MAX_STATE_AGE: raise ContractError("the stored 1H bias is more than 20 minutes old")
    try:
        return BiasInput(state["macro_bias_confirmed"], state["macro_leg_origin"], state["macro_leg_extreme"])   # validates words, prices and direction
    except ContractError:
        raise ContractError("the stored 1H bias values don't make sense")
