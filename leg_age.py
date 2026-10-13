"""leg_age.py -- engine-owned, pure classification of how mature the owned leg is.

Phase C item 1 (PROJECT_HANDOFF s10). It RELABELS atomic upstream fields; it adds no
new thresholds. The numbers (break count, EMA distance) are decided where the engine's
phase is classified; this function only names the result.

Input is world_state["phase"] -- never prose such as market_thesis_trend_health.
Absence discipline: returns None when the inputs cannot form the answer
(no phase, phase not expansion/exhaustion). None means "could not be formed", never "fresh".

Statuses:
  DEVELOPING            engine phase == expansion (no aging reason fired)
  MATURE                exhaustion, reason = break_count only   (a mature trend, NOT "exhausted")
  EXTENDED              exhaustion, reason = ema_distance only  (stretched from EMA, few breaks)
  MATURE_AND_EXTENDED   exhaustion, both reasons fired          (the stronger claim)
  AGING_UNSPECIFIED     exhaustion but the reason is missing/unrecognised -- reported, not guessed
"""
from __future__ import annotations
from typing import Optional

_REASON_TO_STATUS = {
    "break_count": "MATURE",
    "ema_distance": "EXTENDED",
    "break_count+ema_distance": "MATURE_AND_EXTENDED",
}


def classify_leg_age(world_state: Optional[dict]) -> Optional[dict]:
    phase = (world_state or {}).get("phase") or {}
    leg_phase = phase.get("phase")
    if leg_phase not in ("expansion", "exhaustion"):
        return None
    reason = phase.get("aging_reason")
    if leg_phase == "expansion":
        status = "DEVELOPING"
    else:
        status = _REASON_TO_STATUS.get(reason, "AGING_UNSPECIFIED")
    return {
        "status": status,
        "engine_phase": leg_phase,
        "aging_reason": reason,          # None stays None
        "break_count": phase.get("break_count"),
        "dist_in_atr": phase.get("dist_in_atr"),
    }
