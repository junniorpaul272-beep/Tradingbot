"""
observatory/measure.py
======================
OBSERVATORY — measurement primitives. ISOLATED from the live bot.

Contains ONLY concept-free machinery:
  * atr()                     ATR(14) (simple mean of true range)
  * Series                    numpy view of clean candles (from observatory.clean)
  * first_touch()             +target before -stop, in event order (R1)
  * resolve_ambiguous()       settle same-bar target/stop using a lower timeframe
  * collapse_episodes()       independent-episode counting (R3)
  * check_no_lookahead()      as-of harness (gate A3)

No FVG / BOS / CHoCH / IFVG / premium-discount. No scanner imports.

CONVENTIONS (frozen here; change only by editing the catalog contract)
---------------------------------------------------------------------
  * An event is KNOWN at the CLOSE of bar `e`. Entry = OPEN of bar e+1.
  * R-unit = ATR(14) through bar e (inclusive; knowable at that close), in price.
  * Window = bars e+1 .. e+horizon (the entry bar itself counts: its range can
    hit target or stop).
  * Outcomes: TARGET_FIRST, STOP_FIRST, NEITHER, AMBIGUOUS, DATA_GAP.
      AMBIGUOUS = target and stop both touched inside the SAME bar.
      DATA_GAP  = candles are missing BEFORE the outcome is decided, or the window
                  runs past the end of data with no touch. A gap that comes after
                  the outcome was already decided does not censor it. DATA_GAP is
                  never a win/loss/neither and carries a reason so it can be
                  COUNTED, not hidden.
  * A weekend closure inside the window is NOT a gap; it sets crosses_closure.
  * Spread: a long pays +spread, a short receives -spread on entry (price units).
  * Episodes: within one key (e.g. type+direction), an event starts a NEW episode
    if it is more than K bars after the episode's FIRST event (anchored, not
    chained, so an episode can never grow past K bars).
"""
import numpy as np
import pandas as pd

TARGET_FIRST = "TARGET_FIRST"
STOP_FIRST = "STOP_FIRST"
NEITHER = "NEITHER"
AMBIGUOUS = "AMBIGUOUS"
DATA_GAP = "DATA_GAP"


def atr(df, n=14):
    """ATR through each bar inclusive. First n-1 values are NaN."""
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


class Series:
    """Numpy view of clean candles. df needs open/high/low/close/gap_before_missing/closure_before."""

    def __init__(self, df, step_minutes):
        self.df = df
        self.index = df.index
        self.step = pd.Timedelta(minutes=step_minutes)
        self.step_minutes = step_minutes
        self.o = df["open"].to_numpy(float)
        self.h = df["high"].to_numpy(float)
        self.l = df["low"].to_numpy(float)
        self.c = df["close"].to_numpy(float)
        self.gap = df["gap_before_missing"].to_numpy(int) if "gap_before_missing" in df else np.zeros(len(df), int)
        self.closure = df["closure_before"].to_numpy(bool) if "closure_before" in df else np.zeros(len(df), bool)
        self.atr = atr(df).to_numpy(float)
        self.n = len(df)


def _result(**kw):
    base = dict(status=None, reason=None, bar=None, offset=None, crosses_closure=False,
                entry=None, stop=None, target=None, r_unit=None, mfe_r=None, mae_r=None)
    base.update(kw)
    return base


def first_touch(s, e, direction, r_unit=None, target_r=1.0, stop_r=1.0, horizon=24, spread=0.0):
    """
    direction: +1 long, -1 short. Returns a dict (see _result).
    `bar` = index of the bar where the outcome was decided; `offset` = bar - e.
    """
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    if r_unit is None:
        r_unit = s.atr[e] if 0 <= e < s.n else np.nan
    if e < 0 or not np.isfinite(r_unit) or r_unit <= 0:
        return _result(status=DATA_GAP, reason="no_r_unit", r_unit=None)
    first, last = e + 1, e + horizon
    crosses = False
    entry = stop = target = None
    mfe = mae = 0.0
    stop_at = min(last, s.n - 1)
    for j in range(first, stop_at + 1):
        # A gap BEFORE bar j means candles are missing between j-1 and j. If the
        # outcome is not decided yet, it is unknowable -> DATA_GAP. (A gap that
        # comes AFTER the outcome was decided is irrelevant and does not censor.)
        if s.gap[j] > 0:
            return _result(status=DATA_GAP, reason="gap", r_unit=r_unit, crosses_closure=crosses)
        crosses = crosses or bool(s.closure[j])
        if j == first:
            entry = s.o[first] + direction * spread
            stop = entry - direction * stop_r * r_unit
            target = entry + direction * target_r * r_unit
        hi, lo = s.h[j], s.l[j]
        if direction == 1:
            hit_t, hit_s = hi >= target, lo <= stop
            fav, adv = (hi - entry) / r_unit, (entry - lo) / r_unit
        else:
            hit_t, hit_s = lo <= target, hi >= stop
            fav, adv = (entry - lo) / r_unit, (hi - entry) / r_unit
        mfe, mae = max(mfe, fav), max(mae, adv)
        if hit_t or hit_s:
            status = AMBIGUOUS if (hit_t and hit_s) else (TARGET_FIRST if hit_t else STOP_FIRST)
            return _result(status=status, bar=j, offset=j - e, crosses_closure=crosses, entry=entry,
                           stop=stop, target=target, r_unit=r_unit, mfe_r=mfe, mae_r=mae)
    if last >= s.n:
        return _result(status=DATA_GAP, reason="end_of_data", r_unit=r_unit, crosses_closure=crosses)
    return _result(status=NEITHER, crosses_closure=crosses, entry=entry, stop=stop, target=target,
                   r_unit=r_unit, mfe_r=mfe, mae_r=mae)


def resolve_ambiguous(s_hi, s_lo, res, direction):
    """
    Settle an AMBIGUOUS result using a lower-timeframe Series.
    Returns (status, reason). Status stays AMBIGUOUS if the lower timeframe
    cannot decide (incomplete coverage, or both levels inside one lower bar).
    """
    if res["status"] != AMBIGUOUS:
        return res["status"], "not_ambiguous"
    j = res["bar"]
    t0 = s_hi.index[j]
    t1 = t0 + s_hi.step
    ratio = int(round(s_hi.step / s_lo.step))
    a = s_lo.index.searchsorted(t0, side="left")
    b = s_lo.index.searchsorted(t1, side="left")
    if b - a != ratio:
        return AMBIGUOUS, "lower_tf_incomplete"
    if (s_lo.index[a:b][1:] - s_lo.index[a:b][:-1] != s_lo.step).any():
        return AMBIGUOUS, "lower_tf_incomplete"
    stop, target = res["stop"], res["target"]
    for k in range(a, b):
        hi, lo = s_lo.h[k], s_lo.l[k]
        hit_t = hi >= target if direction == 1 else lo <= target
        hit_s = lo <= stop if direction == 1 else hi >= stop
        if hit_t and hit_s:
            return AMBIGUOUS, "same_lower_bar"
        if hit_t:
            return TARGET_FIRST, "resolved_lower_tf"
        if hit_s:
            return STOP_FIRST, "resolved_lower_tf"
    return AMBIGUOUS, "lower_tf_no_touch"   # should not happen if data is consistent


def collapse_episodes(idx, keys=None, K=12):
    """
    idx : sorted array of bar indices of events.
    keys: optional same-length array of group keys (e.g. type+direction).
    Returns (episode_id array, kept_mask array). kept_mask is True for the FIRST
    event of each episode. Episode ids are unique across keys.
    """
    idx = np.asarray(idx)
    n = len(idx)
    if keys is None:
        keys = np.zeros(n, dtype=int)
    keys = np.asarray(keys)
    ep_id = np.full(n, -1, dtype=int)
    kept = np.zeros(n, dtype=bool)
    nxt = 0
    for key in pd.unique(keys):
        pos = np.where(keys == key)[0]
        pos = pos[np.argsort(idx[pos], kind="mergesort")]
        start = None
        for p in pos:
            if start is None or idx[p] - start > K:
                start = idx[p]
                nxt += 1
                kept[p] = True
            ep_id[p] = nxt - 1
    return ep_id, kept


def check_no_lookahead(detector, df, sample_points):
    """
    detector(df) -> set of (confirm_pos, direction), confirm_pos = POSITION (0-based) of the
    bar whose CLOSE confirms the event, relative to the df it was given.
    For every sample position t: events from detector(df[:t+1]) must equal events
    from detector(df) restricted to confirm_pos <= t. Returns list of mismatches.
    """
    full = detector(df)
    bad = []
    for t in sample_points:
        trunc = detector(df.iloc[:t + 1])
        want = {ev for ev in full if ev[0] <= t}
        if trunc != want:
            bad.append({"t": int(t), "only_in_truncated": sorted(trunc - want)[:3],
                        "only_in_full": sorted(want - trunc)[:3]})
    return bad


def last_closed_bar_pos(htf_index, htf_step, event_time):
    """
    Position of the latest HIGHER-timeframe bar that is fully CLOSED at event_time
    (bar_open + step <= event_time), or -1 if none. Event time of a lower-TF
    event = (confirming bar's open time + its step). Using any later bar is lookahead.
    """
    pos = htf_index.searchsorted(event_time - htf_step, side="right") - 1
    return int(pos)
