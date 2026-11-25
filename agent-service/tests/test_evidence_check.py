"""Node 3 · evidence_check gate: every branch of the 2x2 plus the safety rules."""
import json
from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401

from alert_context import alert_context
from basic_profile import make_basic_profile
from evidence_check import SIGNAL_HINTS, decide, load_gate_config, make_evidence_check
from investigation_state import initial_state, make_budget
from synthetic_graphs import SCENARIOS

CONFIG = {"version": "test", "riskCut": 0.5, "signalHints": list(SIGNAL_HINTS)}


class FixedRisk:
    def __init__(self, value):
        self.value = value

    def score(self, alert, context=None):
        if self.value is None:
            return {"status": "unavailable", "reason": "target_not_in_source_index"}
        return {"status": "computed", "score": self.value}


def profiled_state(name, risk=None, budget=None):
    state = initial_state(SCENARIOS[name](), budget)
    first = alert_context(state)
    state = {**state, **first, "evidence": first["evidence"]}
    second = make_basic_profile(risk_source=FixedRisk(risk))(state)
    return {**state, **second, "evidence": state["evidence"] + second["evidence"]}


def gate(state, config=CONFIG):
    return make_evidence_check(config)(state)


class GateBranchTests(unittest.TestCase):
    def test_high_risk_with_signal_facts_goes_to_fast_track(self):
        update = gate(profiled_state("fan_in_mule", 0.8))
        self.assertEqual((update["route"], update["gate"]["reasonCode"]), ("fast_track", "high_risk_with_facts"))
        self.assertNotIn("status", update)

    def test_high_risk_without_facts_is_an_evidence_conflict_to_investigate(self):
        update = gate(profiled_state("normal_merchant", 0.8))
        self.assertEqual((update["route"], update["gate"]["reasonCode"]), ("investigate", "high_risk_without_facts"))

    def test_low_risk_with_facts_is_a_possible_new_type_to_investigate(self):
        update = gate(profiled_state("cycle_layering", 0.2))
        self.assertEqual((update["route"], update["gate"]["reasonCode"]), ("investigate", "low_risk_with_facts"))

    def test_low_risk_without_facts_closes_with_checked_fact_ids(self):
        state = profiled_state("normal_merchant", 0.2)
        update = gate(state)
        self.assertEqual((update["route"], update["status"], update["reasonCodes"]),
                         ("close", "closed", ["low_risk_without_facts"]))
        basis = update["gate"]["closureBasisFactIds"]
        self.assertTrue(basis)
        self.assertLessEqual(set(basis), {row["factId"] for row in state["evidence"]})
        self.assertEqual(update["trace"][0]["llmCalls"], 0)

    def test_shape_hints_alone_are_not_signal_facts(self):
        state = profiled_state("normal_merchant", 0.2)
        self.assertEqual([hint["kind"] for hint in state["profile"]["hints"]], ["FAN_IN_SHAPE"])
        self.assertEqual(gate(state)["gate"]["signalHints"], [])


class GateSafetyTests(unittest.TestCase):
    def test_missing_risk_never_closes(self):
        with_facts = gate(profiled_state("fan_in_mule", None))
        without_facts = gate(profiled_state("normal_merchant", None))
        self.assertEqual(with_facts["route"], "investigate")
        self.assertEqual((without_facts["route"], without_facts["gate"]["reasonCode"]),
                         ("human_review", "risk_unavailable_without_facts"))
        self.assertNotIn("status", without_facts)

    def test_alert_not_flagged_by_trigger_is_closed_as_not_an_alert(self):
        state = profiled_state("fan_in_mule", 0.9)
        state["alertContext"] = {**state["alertContext"], "flagged": False}
        update = gate(state)
        self.assertEqual((update["route"], update["reasonCodes"]), ("close", ["rule_not_flagged"]))

    def test_llm_routes_fall_back_to_human_review_when_llm_budget_is_zero(self):
        update = gate(profiled_state("fan_in_mule", 0.8, budget={"maxLlmCalls": 0}))
        self.assertEqual((update["route"], update["gate"]["reasonCode"]), ("human_review", "llm_budget_exhausted"))

    def test_cut_is_inclusive_on_the_high_side(self):
        context = {"flagged": True}
        profile = {"hints": [], "riskScore": {"status": "computed", "score": 0.5}}
        self.assertEqual(decide(context, profile, make_budget(), CONFIG)[0], "investigate")


class GateConfigTests(unittest.TestCase):
    def test_repository_config_is_tuned_and_valid(self):
        config = load_gate_config()
        self.assertEqual(config["version"], "gate-v1")
        self.assertEqual(config["tuning"]["riskScores"], "out-of-fold")
        self.assertLessEqual(config["tuning"]["validation"]["recallLoss"], 0.02)

    def test_invalid_config_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gate.json"
            for bad in ({"riskCut": 1.5, "signalHints": []}, {"riskCut": True, "signalHints": []},
                        {"riskCut": 0.5, "signalHints": ["FAN_IN_SHAPE"]}):
                path.write_text(json.dumps({"version": "x", **bad}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_gate_config(path)


if __name__ == "__main__":
    unittest.main()
