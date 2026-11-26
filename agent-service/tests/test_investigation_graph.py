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

    def test_full_run_reuses_draft_and_verification_then_requires_a_person(self):
        tool_calls = []

        class Model:
            def call(self, *, name, payload, validate, **kwargs):
                answer = {"choice": "finish" if name == "investigator" else "cycle",
                          "factIds": [payload["facts"][0]["factId"]]}
                if name == "typology":
                    answer["chunkIds"] = ["official:1"]
                return {"status": "ok", "output": validate(answer), "callsUsed": 1, "costUsd": 0.01,
                        "usage": {"inputTokens": 10, "outputTokens": 3}}

        def tools(name, arguments):
            tool_calls.append(name)
            if name == "build_evidence_context":
                return {"contextId": "context", "corpusSha256": "document-version",
                        "citations": [{"chunkId": "official:1", "section": "1", "text": "근거"}]}
            if name == "generate_review_report":
                self.assertEqual(arguments["corpusSha256"], "document-version")
                return {"reportId": "draft", "contextId": "context"}
            return {"reportId": arguments["report"]["reportId"], "valid": True}

        result = self.run(load_alert("cycle"), llm_client=Model(), call_tool=tools)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trace"][-1]["node"], "human_review")
        self.assertEqual(result["budget"]["llmCalls"], 2)
        self.assertTrue(result["humanReviewRequired"])
        self.assertFalse(result["readyForSubmission"])
        self.assertEqual(tool_calls, ["build_evidence_context", "generate_review_report", "verify_review_report"])

    def test_typology_checks_the_rule_evidence_id_and_closed_cycle_focus(self):
        from investigation_steps import check_typology_choice
        from llm_client import LLMOutputError
        observed = {"alert.F1.ruleId": "fan_in", "alert.F2.ruleId": "cycle"}
        with self.assertRaises(LLMOutputError):
            check_typology_choice({"choice": "cycle", "factIds": ["alert.F1.ruleId"]}, observed, "cycle")
        with self.assertRaises(LLMOutputError):
            check_typology_choice({"choice": "fan_in", "factIds": ["alert.F1.ruleId"]}, observed, "cycle")
        check_typology_choice({"choice": "cycle", "factIds": ["alert.F2.ruleId"]}, observed, "cycle")
        check_typology_choice({"choice": "uncertain", "factIds": []}, observed, "cycle")


if __name__ == "__main__":
    unittest.main()
