"""Run: python test_leg_age.py -- Phase C item 1: leg maturity label, its wiring, and mutation tests."""
import ast, json, os, re, sys, unittest
from datetime import datetime, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.append(os.path.join(HERE, "comms"))   # bus / adapters live in comms/
import leg_age, bus, bus_adapters as ba, bus_contract as bc
from leg_age import classify_leg_age as c


def ws(phase, reason=None, bc_=None, d=None):
    return {"phase": {"phase": phase, "aging_reason": reason, "break_count": bc_, "dist_in_atr": d}}


class Classifier(unittest.TestCase):
    def test_each_status(self):
        self.assertEqual(c(ws("expansion"))["status"], "DEVELOPING")
        self.assertEqual(c(ws("exhaustion", "break_count", 4))["status"], "MATURE")
        self.assertEqual(c(ws("exhaustion", "ema_distance", 1, 2.7))["status"], "EXTENDED")
        self.assertEqual(c(ws("exhaustion", "break_count+ema_distance", 4, 2.7))["status"], "MATURE_AND_EXTENDED")

    def test_absence_is_none_not_fresh(self):
        for w in (None, {}, {"phase": None}, ws("transition"), ws("manipulation"), ws(None), ws("EXHAUSTION")):
            self.assertIsNone(c(w), w)       # uppercase must not silently classify (the 2026-08-26 bug)

    def test_unknown_reason_is_reported_not_guessed(self):
        self.assertEqual(c(ws("exhaustion", None))["status"], "AGING_UNSPECIFIED")
        self.assertEqual(c(ws("exhaustion", "something_new"))["status"], "AGING_UNSPECIFIED")

    def test_expansion_keeps_null_reason_and_never_reads_prose(self):
        w = ws("expansion"); w["market_thesis_trend_health"] = "Aging - 4 break(s)"
        r = c(w); self.assertEqual((r["status"], r["aging_reason"]), ("DEVELOPING", None))

    def test_agrees_with_the_engines_own_phase_values(self):
        import scanner_observation as so
        self.assertEqual({so.Phase.EXPANSION.value, so.Phase.EXHAUSTION.value}, {"expansion", "exhaustion"})

    def test_mutations_are_detected(self):
        orig = leg_age._REASON_TO_STATUS
        muts = [{**orig, "break_count": "EXTENDED"}, {**orig, "break_count+ema_distance": "MATURE"},
                {k: v for k, v in orig.items() if k != "ema_distance"}]
        try:
            for m in muts:
                leg_age._REASON_TO_STATUS = m
                with self.assertRaises(AssertionError, msg=f"mutant survived: {m}"):
                    self.test_each_status()
        finally:
            leg_age._REASON_TO_STATUS = orig


def base_state(**over):
    s = {"market_data_timestamp": "2026-10-01T12:00:00+00:00", "market_thesis_updated_at": "2026-10-01T12:00:00+00:00",
         "market_thesis_trend_health": "Aging - 4 break(s)", "market_thesis_aging_reason": "break_count",
         "market_thesis_break_count": 4}
    s.update(over); return s


class Adapter(unittest.TestCase):
    def claim(self, s):
        res = ba.adapt_state(s)
        return {x.subject: x for x in res.claims}.get("leg.age_status"), {g.subject: g for g in res.gaps}

    def test_status_passes_through_when_the_engine_wrote_it(self):
        cl, _ = self.claim(base_state(market_leg_age_status=c(ws("exhaustion", "break_count", 4))))
        self.assertEqual(cl.value["status"], "MATURE"); self.assertEqual(cl.value["break_count"], 4)

    def test_no_status_key_is_invented_when_the_engine_wrote_none(self):
        cl, _ = self.claim(base_state())
        self.assertNotIn("status", cl.value)
        cl, _ = self.claim(base_state(market_leg_age_status=None))
        self.assertNotIn("status", cl.value)

    def test_status_alone_is_enough_and_nothing_at_all_is_a_gap(self):
        s = base_state(market_leg_age_status=c(ws("expansion")))
        for k in ("market_thesis_trend_health", "market_thesis_aging_reason", "market_thesis_break_count"): s.pop(k)
        cl, _ = self.claim(s); self.assertEqual(cl.value, {"status": "DEVELOPING"})
        s.pop("market_leg_age_status"); cl, gaps = self.claim(s)
        self.assertIsNone(cl); self.assertEqual(gaps["leg.age_status"].state, "NOT_PROVIDED")

    def test_bus_renders_status_with_and_without_the_prose(self):
        T = datetime(2026, 10, 1, 12, 0, 30, tzinfo=timezone.utc)
        for s in (base_state(market_leg_age_status=c(ws("exhaustion", "break_count", 4))),
                  base_state(market_leg_age_status=c(ws("exhaustion", "break_count", 4)), market_thesis_trend_health=None)):
            res = ba.adapt_state(s)
            r = bus.respond("/now", res.claims, res.gaps, T)
            self.assertFalse(r.used_fallback, r.violations)
            self.assertIn("Leg maturity (engine label): mature.", r.text)


class Wiring(unittest.TestCase):
    def test_scanner_live_persists_it_from_the_engines_own_phase_atoms(self):
        src = open(os.path.join(HERE, "scanner_live.py")).read()
        self.assertIn("from leg_age import classify_leg_age", src)
        m = re.search(r'"market_leg_age_status":\s*classify_leg_age\(\{"phase": \{(.*?)\}\}\)', src, re.S)
        self.assertTrue(m, "state write missing")
        for k in ("phase_result.phase.value", "phase_result.aging_reason", "phase_result.break_count", "phase_result.dist_in_atr"):
            self.assertIn(k, m.group(1))

    def test_leg_age_is_pure(self):
        t = ast.parse(open(os.path.join(HERE, "leg_age.py")).read())
        found = {a.name.split(".")[0] for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names}
        found |= {n.module.split(".")[0] for n in ast.walk(t) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(found, {"__future__", "typing"}, found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
