"""LangGraph wiring; skipped when `pip install -r agent-service/requirements.txt` was not run."""
import unittest

from support import HAS_LANGGRAPH, load_alert


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed: pip install -r agent-service/requirements.txt")
class InvestigationGraphTests(unittest.TestCase):
    def setUp(self):
        from investigation_graph import RECURSION_LIMIT, build_graph, run_investigation
        self.build_graph, self.run, self.limit = build_graph, run_investigation, RECURSION_LIMIT

    def test_valid_alert_runs_alert_context_profile_and_gate(self):
        result = self.run(load_alert("cycle"))
        self.assertEqual(result["status"], "running")
        self.assertEqual([row["node"] for row in result["trace"]], ["alert_context", "basic_profile", "evidence_check"])
        self.assertEqual(result["alertContext"]["ruleIds"], ["CYCLE"])
        self.assertEqual({row["source"] for row in result["evidence"]}, {"alert_context", "basic_profile"})
        self.assertEqual(result["profile"]["similarity"]["status"], "unavailable")
        # No risk source: a cycle alert with facts is investigated, never closed blind.
        self.assertEqual((result["route"], result["gate"]["reasonCode"]), ("investigate", "risk_unavailable_with_facts"))
        self.assertEqual(result["budget"]["llmCalls"], 0)

    def test_gate_closure_ends_the_run_as_closed(self):
        from synthetic_graphs import SCENARIOS

        class LowRisk:
            def score(self, alert, context=None):
                return {"status": "computed", "score": 0.1}

        result = self.run(SCENARIOS["normal_merchant"](), risk_source=LowRisk())
        self.assertEqual((result["status"], result["route"]), ("closed", "close"))
        self.assertTrue(result["gate"]["closureBasisFactIds"])

    def test_reference_set_is_bound_when_the_graph_is_built(self):
        from synthetic_graphs import SCENARIOS, dev_reference_set
        result = self.run(SCENARIOS["fan_in_mule"](), similarity_source=dev_reference_set())
        self.assertEqual(result["profile"]["similarity"]["status"], "computed")
        self.assertIn("JUST_BELOW_THRESHOLD", [hint["kind"] for hint in result["profile"]["hints"]])

    def test_rejected_alert_stops_the_graph(self):
        alert = load_alert("cycle")
        alert["ruleResult"]["ruleScore"] = 99
        result = self.run(alert)
        self.assertEqual((result["status"], result["reasonCodes"]), ("rejected", ["rule_result_mismatch"]))
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["evidence"], [])

    def test_checkpointed_run_can_be_inspected_by_thread(self):
        from langgraph.checkpoint.memory import InMemorySaver
        saver = InMemorySaver()
        self.run(load_alert("fan_in"), checkpointer=saver, thread_id="alert-1")
        snapshot = self.build_graph(checkpointer=saver).get_state({"configurable": {"thread_id": "alert-1"}})
        self.assertEqual(snapshot.values["alertContext"]["ruleIds"], ["FAN_IN"])

    def test_graph_exposes_ppt_node_names_and_a_recursion_limit(self):
        nodes = set(self.build_graph().get_graph().nodes)
        self.assertLessEqual({"alert_context", "basic_profile", "evidence_check"}, nodes)
        self.assertGreater(self.limit, 0)


if __name__ == "__main__":
    unittest.main()
