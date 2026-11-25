"""Synthetic development alerts must pass the same checks as backend alerts."""
import unittest

import support  # noqa: F401

from alert_context import alert_context
from investigation_state import initial_state
from similarity import validate_reference_set
from synthetic_graphs import SCENARIOS, dev_reference_set

EXPECTED_RULES = {"fan_in_mule": ["FAN_IN"], "cycle_layering": ["CYCLE"], "fan_out_burst": ["FAN_OUT"],
                  "normal_merchant": ["FAN_IN"], "normal_payroll": ["FAN_OUT"]}


class SyntheticGraphTests(unittest.TestCase):
    def test_every_scenario_is_accepted_by_alert_context_with_expected_rule(self):
        for name, make in SCENARIOS.items():
            with self.subTest(name):
                update = alert_context(initial_state(make()))
                self.assertNotIn("status", update)
                self.assertEqual(update["alertContext"]["ruleIds"], EXPECTED_RULES[name])
                self.assertTrue(update["alertContext"]["flagged"])

    def test_scenarios_are_marked_synthetic_and_large_enough_to_profile(self):
        for name, make in SCENARIOS.items():
            graph = make()["graph"]
            self.assertEqual(graph["meta"]["sourceScope"], "synthetic_agent_dev")
            self.assertGreaterEqual(len(graph["edges"]), 10, name)
            self.assertTrue(all(edge["txId"].startswith("SYN-") for edge in graph["edges"]))

    def test_scenarios_are_deterministic(self):
        for make in SCENARIOS.values():
            self.assertEqual(make(), make())

    def test_dev_reference_set_uses_variants_not_the_test_alerts(self):
        reference = dev_reference_set()
        validate_reference_set(reference)
        test_ids = {make()["graph"]["targetTxId"] for make in SCENARIOS.values()}
        reference_ids = {row["referenceId"].split(":")[0] for row in reference["profiles"]}
        self.assertEqual(len(reference["profiles"]), 4)
        self.assertFalse(test_ids & reference_ids)


if __name__ == "__main__":
    unittest.main()
