"""Actual tool-chain tests on versioned synthetic files; no paid calls or DB."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_workflow import run_review_workflow
from agent_workflow_demo import fixture_tools, run_cases
from corpus import canonical, digest
from reports import ReportUnavailable, default_plan


class AgentWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            cls.outcome = run_cases(directory)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.tools, self.request = fixture_tools(self.directory)

    def run_flow(self, call=None, **options):
        request = self.request
        return run_review_workflow(call or self.tools.call, request["graph"], request["ruleResult"],
                                   request["query"], **options)

    def test_normal_passes_every_full_verification_check(self):
        result = self.outcome["cases"]["normal"]
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["verification"]["performed"])
        self.assertTrue(all(check["valid"] for check in result["verification"]["checks"].values()))
        self.assertEqual(result["identification"]["ruleScore"], 90)
        values = {fact["factId"]: fact["value"] for fact in result["report"]["draft"]["facts"]}
        self.assertEqual(values["target.amountPaid"], "1002.10")
        self.assertEqual(values["finding.0.grossPaidTotalsByCurrency.US Dollar"], "3003.30")
        self.assertEqual(values["finding.0.transactionCount"], 3)

    def test_tools_run_in_requested_order_and_resolve_every_reference(self):
        result = self.outcome["cases"]["normal"]
        steps = [row["step"] for row in result["execution"]["toolCalls"]]
        self.assertEqual(list(dict.fromkeys(steps)), ["identify", "collect_evidence", "draft", "verify"])
        self.assertEqual(len(result["evidence"]["facts"]), len(result["context"]["reportFacts"]))
        for resolved in result["evidence"]["facts"]:
            self.assertTrue(resolved["verification"]["recomputed"])
            self.assertEqual({row["txId"] for row in resolved["transactions"]}, set(resolved["fact"]["evidenceTxIds"]))
        for resolved in result["evidence"]["citations"]:
            highlight = resolved["section"]["highlight"]
            self.assertEqual(resolved["sourceVerification"]["status"], "snapshot_checked")
            self.assertEqual(resolved["section"]["text"][highlight["start"]:highlight["end"]], resolved["citation"]["text"])

    def test_original_question_is_held_despite_supplementary_cdd(self):
        result = self.outcome["cases"]["no_evidence"]
        self.assertEqual(result["status"], "held")
        self.assertTrue(result["evidence"]["citations"])
        self.assertIsNone(result["report"])
        self.assertFalse(result["verification"]["performed"])
        self.assertEqual(result["execution"]["generationAttempts"], 0)
        self.assertEqual(result["execution"]["retryCount"], 0)

    def test_wrong_amount_rejects_even_after_rehash_and_without_retry(self):
        result = self.outcome["cases"]["wrong_amount"]
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["verification"]["checks"]["amounts"]["valid"])
        self.assertTrue(result["verification"]["checks"]["reportDigest"]["valid"])
        self.assertEqual(result["execution"]["generationAttempts"], 1)
        self.assertEqual(result["execution"]["retryCount"], 0)

    def test_non_verbatim_quote_rejects_even_after_rehash(self):
        result = self.outcome["cases"]["wrong_citation"]
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["verification"]["checks"]["citations"]["valid"])
        self.assertTrue(result["verification"]["checks"]["reportDigest"]["valid"])

    def test_template_never_calls_network_and_is_exposed_as_a_tool(self):
        with patch("reports.urlopen", side_effect=AssertionError("Unexpected network")):
            result = self.tools.call("run_review_workflow", self.request)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["report"]["generation"]["llmCalled"])
        self.assertEqual(result["execution"]["retryCount"], 0)

    def test_every_disposition_requires_human_review(self):
        for result in self.outcome["cases"].values():
            self.assertTrue(result["humanReviewRequired"])
            self.assertFalse(result["readyForSubmission"])

    def test_identical_fixture_runs_are_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            repeated = run_cases(directory)
        self.assertEqual(canonical(repeated), canonical(self.outcome))

    def test_input_mutation_cannot_switch_query_graph_or_score_mid_flow(self):
        original_graph = deepcopy(self.request["graph"])
        observed = []
        def call(name, fields):
            if "graph" in fields:
                observed.append(deepcopy(fields))
            response = self.tools.call(name, fields)
            if name == "build_evidence_context":
                self.request["graph"]["edges"][0]["amountPaid"] = "999.00"
                self.request["ruleResult"]["ruleScore"] = -1
                self.request["query"] = "화성 대기압"
            return response
        result = self.run_flow(call)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(row["graph"] == original_graph for row in observed))
        self.assertTrue(all(row["ruleResult"]["ruleScore"] == 90 for row in observed))
        self.assertEqual(result["identity"]["graphSha256"], digest(canonical(original_graph)))
        self.assertTrue(all(row["corpusSha256"] == result["identity"]["corpusSha256"]
                            for row in observed[1:] if "corpusSha256" in row))

    def test_forged_rule_score_is_rejected_before_generation(self):
        self.request["ruleResult"]["ruleScore"] += 1
        with patch("reports.urlopen", side_effect=AssertionError("Unexpected network")):
            with self.assertRaises(ValueError):
                self.run_flow()

    def test_missing_snapshot_holds_without_generation(self):
        snapshot = next((self.directory / "raw" / "snapshots").rglob("*.html"))
        snapshot.unlink()
        result = self.run_flow()
        self.assertEqual(result["status"], "held")
        self.assertIn("source_snapshot_unverified", result["reasonCodes"])
        self.assertEqual(result["execution"]["generationAttempts"], 0)

    def test_unavailable_corpus_is_held_without_using_current_version(self):
        result = self.run_flow(corpus_sha256="0" * 64)
        self.assertEqual(result["status"], "held")
        self.assertIn("corpus_unavailable", result["reasonCodes"])
        self.assertIsNone(result["report"])

    def test_malformed_candidate_is_rejected_and_retained(self):
        def call(name, fields):
            result = self.tools.call(name, fields)
            if name == "generate_review_report":
                result["draft"] = None
            return result
        result = self.run_flow(call, max_retries=2)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("invalid_report_shape", result["reasonCodes"])
        self.assertIsNone(result["report"]["draft"])
        self.assertEqual(result["execution"]["retryCount"], 0)

    def test_retries_are_bounded_and_do_not_leak_provider_errors(self):
        generations = []
        def call(name, fields):
            if name == "generate_review_report":
                generations.append(deepcopy(fields))
                raise ReportUnavailable("unit-secret-provider-detail")
            return self.tools.call(name, fields)
        result = self.run_flow(call, mode="openai", max_retries=1)
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["execution"]["generationAttempts"], 2)
        self.assertEqual(result["execution"]["retryCount"], 1)
        self.assertEqual(generations[0], generations[1])
        self.assertEqual(generations[0]["mode"], "openai")
        self.assertNotIn("unit-secret", canonical(result))
        self.assertIsNone(result["report"])

    def test_actual_model_adapter_stub_can_only_select_tool_ids_and_retry(self):
        context = self.tools.call("build_evidence_context", self.request)
        plan = default_plan(context)
        invalid = deepcopy(plan)
        invalid["observationIds"].append("model-invented-score-100")
        requests = []
        def provider(request, timeout):
            requests.append(json.loads(request.data))
            selected = invalid if len(requests) == 1 else plan
            return io.BytesIO(json.dumps({"status": "completed", "id": "stub-response",
                "output": [{"content": [{"type": "output_text", "text": json.dumps(selected)}]}]}).encode())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-token", "OPENAI_MODEL": "unit-model"}), patch("reports.urlopen", provider):
            result = self.run_flow(mode="openai", max_retries=1)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["execution"]["generationAttempts"], 2)
        self.assertEqual(result["execution"]["retryCount"], 1)
        self.assertEqual(requests[0], requests[1])
        self.assertTrue(result["report"]["generation"]["llmCalled"])
        self.assertFalse(result["verification"]["providerCallAuthenticated"])
        self.assertNotIn("unit-token", canonical(result))
        self.assertEqual(set(requests[0]["text"]["format"]["schema"]["properties"]),
                         {"contextId", "observationIds", "citationIds", "reviewItemIds"})
        expected = [{"factId": f["factId"], "value": f["value"]} for f in context["reportFacts"]]
        self.assertEqual(result["report"]["draft"]["facts"], expected)

    def test_retry_limit_and_tool_input_allowlist(self):
        for value in (-1, 3, True, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_flow(max_retries=value)
        with self.assertRaises(ValueError):
            self.tools.call("run_review_workflow", {**self.request, "ruleScore": 100})
        specs = json.loads((ROOT / "agent_tools.json").read_text(encoding="utf-8"))
        spec = next(row for row in specs if row["name"] == "run_review_workflow")
        self.assertEqual(spec["parameters"]["required"], ["graph", "ruleResult"])
        self.assertFalse(spec["parameters"]["additionalProperties"])

    def test_verification_failure_does_not_consume_generation_retries(self):
        def call(name, fields):
            result = self.tools.call(name, fields)
            if name == "generate_review_report":
                next(f for f in result["draft"]["facts"] if f["factId"] == "rule.score")["value"] = 100
            return result
        result = self.run_flow(call, max_retries=2)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["execution"]["generationAttempts"], 1)
        self.assertEqual(result["execution"]["retryCount"], 0)

    def test_caller_mutation_cannot_change_frozen_query(self):
        before = deepcopy(self.request)
        def call(name, fields):
            response = self.tools.call(name, fields)
            if name == "build_evidence_context":
                self.request["graph"]["edges"][0]["amountPaid"] = "777777.77"
                self.request["ruleResult"]["ruleScore"] = 0
                self.request["query"] = "화성 대기압 광합성"
            return response
        result = self.run_flow(call)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["identity"]["graphSha256"], digest(canonical(before["graph"])))
        self.assertEqual(result["identification"]["ruleScore"], 90)

    def test_fixture_ids_are_independent_of_checkout_time(self):
        normal = self.outcome["cases"]["normal"]
        repeated = self.run_flow()
        self.assertEqual(repeated["identity"], normal["identity"])
        self.assertEqual(repeated["report"], normal["report"])
        self.assertEqual(repeated["verification"], normal["verification"])


if __name__ == "__main__":
    unittest.main()
