"""compare_repair.py - READ-ONLY. Compares a separately fetched 1h repair file with the original Research Lab 1h file for a date range.
Reports: hours recovered, hours still missing, Fridays still truncated, and any overlapping candle whose prices differ. Never writes to either input.
Usage: python3 compare_repair.py <original_1h.jsonl> <repair_1h.jsonl> <start YYYY-MM-DD> <end YYYY-MM-DD> [out.json]"""
import sys, json, collections, datetime as dt
from . import audit_research_dataset as au

def compare(orig, rep, start, end):
    inr = lambda t: start <= t[:10] <= end
    O = {t: v for t, v in orig.items() if inr(t)}; R = {t: v for t, v in rep.items() if inr(t)}
    new = sorted(t for t in R if t not in O)
    conflicts = [(t, O[t], R[t]) for t in R if t in O and max(abs(a - b) for a, b in zip(O[t], R[t])) > 1e-9]
    same = sum(1 for t in R if t in O) - len(conflicts)
    fridays = {}
    d = dt.date.fromisoformat(start)
    while d.isoformat() <= end:
        if d.weekday() == 4:
            k = d.isoformat(); hrs_o = sorted(int(t[11:13]) for t in O if t[:10] == k); hrs_r = sorted(int(t[11:13]) for t in R if t[:10] == k)
            union = sorted(set(hrs_o) | set(hrs_r))
            fridays[k] = {"original_last_hour": hrs_o[-1] if hrs_o else None, "repair_last_hour": hrs_r[-1] if hrs_r else None,
                          "combined_contiguous_to_19": union == list(range(union[-1] + 1)) and union[-1] >= 19 if union else False}
        d += dt.timedelta(days=1)
    return {"range": [start, end], "original_candles": len(O), "repair_candles": len(R), "identical_overlap": same,
            "conflicting_overlap": len(conflicts), "conflicts": conflicts[:20], "recovered_new_hours": len(new),
            "recovered_examples": new[:5], "fridays": fridays,
            "fridays_repaired": sum(1 for f in fridays.values() if f["combined_contiguous_to_19"]), "fridays_total": len(fridays),
            "verdict": ("NOTHING RECOVERED - record the gap as unresolved; do not retry" if not new else
                        "RECOVERED HOURS - review conflicts, then decide on a merge (not done here)")}

if __name__ == "__main__":
    o, _ = au.load(sys.argv[1]); r, _ = au.load(sys.argv[2]); res = compare(o, r, sys.argv[3], sys.argv[4])
    print(json.dumps(res, indent=1, default=str))
    if len(sys.argv) > 5: json.dump(res, open(sys.argv[5], "w"), indent=1, default=str)
