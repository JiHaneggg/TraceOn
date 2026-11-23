"""Offline contract checks for role exchange, paired comparisons, and holdout labels."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_judge import openai_judge
from agent_judge_demo import (HOLDOUT_CASES, HOLDOUT_REQUEST, compare_cases, evaluate_cases,
                             label_packet, reviewed_labels)
from agent_workflow import run_review_workflow
from agent_workflow_demo import fixture_tools
from judge_debate import openai_debate
from test_agent_judge import model_response, provider_reply


def request_input(request):
    body = json.loads(request.data)
    return body, json.loads(body["input"][1]["content"])


class DebateTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.tools, self.request = fixture_tools(directory)
        self.enterContext(patch.dict("os.environ", {"OPENAI_API_KEY": "test-judge-key", "OPENAI_MODEL": "mock-model"}))

    def test_workflow_reviews_same_facts_and_challenges_first_validated_review(self):
        requests = []
        def provider(request, timeout):
            body, payload = request_input(request)
            requests.append((body, payload))
            return provider_reply(model_response(payload))

        with patch("agent_judge.urlopen", side_effect=provider):
            result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "debate"})
        self.assertTrue(result["verification"]["valid"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["judge"]["modelCallAttempts"], 2)
        self.assertEqual(result["judge"]["strategyVersion"], "traceon-debate-v2")
        self.assertTrue(result["humanReviewRequired"])
        self.assertFalse(result["readyForSubmission"])
        self.assertEqual(requests[0][1], requests[1][1])
        first, second = (row[0]["input"] for row in requests)
        self.assertIn("첫 검토 역할", first[0]["content"])
        self.assertIn("반론 검토 역할", second[0]["content"])
        self.assertEqual(len(first), 2)
        self.assertEqual(json.loads(second[2]["content"])["priorReview"], result["judge"]["rounds"][0]["items"])
        for body, payload in requests:
            output = body["text"]["format"]
            self.assertTrue(output["strict"])
            fields = output["schema"]["properties"]["items"]["items"]["properties"]
            self.assertEqual(fields["itemId"]["enum"], [row["itemId"] for row in payload["items"]])
            self.assertEqual(fields["factIds"]["items"]["enum"], [row["factId"] for row in payload["facts"]])
            self.assertEqual(fields["chunkIds"]["items"]["enum"], [row["chunkId"] for row in payload["sources"]])

    def test_second_round_unknown_id_is_still_held_if_provider_ignores_schema(self):
        attempts = []

        def provider(request, timeout):
            _, payload = request_input(request)
            attempts.append(payload)
            response = model_response(payload, {"document_support": "contradicted"})
            if len(attempts) == 2:
                item = next(item for item in response["items"] if item["itemId"] == "document_support")
                item["chunkIds"] = [item["chunkIds"][0] + "-typo"]
            return provider_reply(response)

        with patch("agent_judge.urlopen", side_effect=provider):
            result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "debate"})
        self.assertEqual(result["status"], "held")
        self.assertIsNone(result["judge"]["verificationScore"])
        self.assertEqual(result["judge"]["modelCallAttempts"], 2)
        self.assertEqual(result["judge"]["rounds"][1]["responseErrorCode"], "unknown_evidence_id")

    def test_oversized_id_allowlist_holds_without_a_model_call(self):
        payload = {
            "items": [{"itemId": "check"}],
            "facts": [{"factId": "long-id-" + "x" * 60 + str(index)} for index in range(251)],
            "sources": [{"chunkId": "source"}],
        }
        with patch("agent_judge.urlopen") as provider:
            result = openai_debate(payload)
        provider.assert_not_called()
        self.assertEqual(result["status"], "held")
        self.assertIsNone(result["verificationScore"])
        self.assertEqual(result["rounds"][0]["reasonCode"], "judge_id_schema_unavailable")

    def test_reconcile_requires_both_verdict_and_same_source_for_document_contradiction(self):
        sample = (
            ("contradicted", 0, "rejected", "agreement"),
            ("supported", 0, "held", "verdict_disagreement"),
            ("contradicted", 1, "held", "contradiction_basis_disagreement"),
        )
        for second_verdict, second_source, expected, resolution in sample:
            seen = []
            def provider(request, timeout):
                _, payload = request_input(request)
                seen.append(payload)
                verdict = "contradicted" if len(seen) == 1 else second_verdict
                chunk = payload["sources"][0 if len(seen) == 1 else second_source]["chunkId"]
                response = model_response(payload, {"document_support": verdict})
                row = next(row for row in response["items"] if row["itemId"] == "document_support")
                row["chunkIds"] = [chunk]
                return provider_reply(response)
            with self.subTest(expected=expected, resolution=resolution), patch("agent_judge.urlopen", side_effect=provider):
                result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "debate"})
                self.assertEqual(result["status"], expected)
                row = next(i for i in result["judge"]["items"] if i["itemId"] == "document_support")
                self.assertEqual(row["resolution"], resolution)

    def test_code_failure_skips_both_roles(self):
        def call(name, arguments):
            response = self.tools.call(name, arguments)
            if name == "generate_review_report":
                response["draft"]["citations"][0]["quote"] = "변조 인용"
            return response
        with patch("agent_judge.urlopen") as provider:
            result = run_review_workflow(call, self.request["graph"], self.request["ruleResult"],
                                         self.request["query"], judge=openai_debate)
        provider.assert_not_called()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["judge"]["modelCallAttempts"], 0)

    def test_either_model_failure_holds_without_a_score(self):
        for failure_round in (1, 2):
            with self.subTest(failure_round=failure_round):
                attempts = []
                def provider(request, timeout):
                    _, payload = request_input(request)
                    attempts.append(payload)
                    if len(attempts) == failure_round:
                        raise URLError("private transport error")
                    return provider_reply(model_response(payload))
                with patch("agent_judge.urlopen", side_effect=provider):
                    result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "debate"})
                self.assertEqual(len(attempts), failure_round)
                self.assertEqual(result["status"], "held")
                self.assertIsNone(result["judge"]["verificationScore"])
                self.assertEqual(result["judge"]["rounds"][-1]["reasonCode"], "judge_model_unavailable")
                self.assertNotIn("private transport error", json.dumps(result))

    def test_compare_pairs_identical_cases_with_call_totals(self):
        provider = mock_provider
        workflow = self.tools.call("run_review_workflow", self.request)
        report = compare_cases(workflow, {"single": lambda payload: openai_judge(payload, provider),
                                          "debate": lambda payload: openai_debate(payload, provider)},
                               "evaluation", 2)
        single, debate = (report["strategies"][name] for name in ("single", "debate"))
        self.assertEqual((single["modelCallAttempts"], debate["modelCallAttempts"]), (6, 12))
        self.assertEqual((single["correctRuns"], debate["correctRuns"]), (6, 6))
        self.assertTrue(all(r["single"] == r["debate"] == r["expected"] for r in report["pairedRuns"]))

    def test_single_and_debate_send_identical_evidence_id_schema(self):
        schemas = []
        def provider(request, timeout):
            body, payload = request_input(request)
            schemas.append(body["text"]["format"])
            return provider_reply(model_response(payload))

        with patch("agent_judge.urlopen", side_effect=provider):
            result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "openai"})
            debate = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "debate"})
        self.assertEqual(result["judge"]["strategy"], "single_judge")
        self.assertEqual(result["judge"]["strategyVersion"], "traceon-single-v2")
        self.assertEqual(result["judge"]["evidenceIdSchema"], debate["judge"]["evidenceIdSchema"])
        self.assertEqual(schemas, [schemas[0]] * 3)
        self.assertEqual(result["status"], "passed")

    def test_comparison_refuses_different_id_schema_versions(self):
        workflow = self.tools.call("run_review_workflow", self.request)
        with self.assertRaisesRegex(ValueError, "same evidence ID schema"):
            compare_cases(
                workflow,
                {"single": lambda p: {**openai_judge(p, mock_provider), "evidenceIdSchema": "legacy"},
                 "debate": lambda p: openai_debate(p, mock_provider)},
                "evaluation", 1, case_id="judge-eval-ownership",
            )

    def test_holdout_requires_complete_labels_and_pins_reviewed_inputs(self):
        workflow = self.tools.call("run_review_workflow", self.request)
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tools, request = fixture_tools(folder / "evidence", HOLDOUT_REQUEST)
        holdout = tools.call("run_review_workflow", request)
        self.assertNotEqual(holdout["identity"]["graphSha256"], workflow["identity"]["graphSha256"])
        packet = label_packet(holdout)
        self.assertEqual(len(packet["cases"]), 6)
        self.assertTrue(all(row["expected"] is None for row in packet["labels"].values()))
        judge = unittest.mock.Mock()
        with self.assertRaisesRegex(ValueError, "requires a valid label"):
            evaluate_cases(holdout, judge, "holdout", 1, case_file=HOLDOUT_CASES)
        judge.assert_not_called()

        label_file = folder / "reviewed.json"
        label_file.write_text(json.dumps(packet), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "requires a human verdict"):
            reviewed_labels(holdout, label_file)
        completed = deepcopy(packet)
        completed["labels"] = {k: {"expected": "held", "reason": "test label"}
                               for k in packet["labels"]}
        label_file.write_text(json.dumps(completed), encoding="utf-8")
        labels, _ = reviewed_labels(holdout, label_file)
        self.assertEqual(set(labels), set(packet["labels"]))
        completed["cases"][0]["input"]["draft"] = "변경된 평가 입력"
        label_file.write_text(json.dumps(completed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            reviewed_labels(holdout, label_file)


def mock_provider(request, timeout):
    _, payload = request_input(request)
    case_id = payload["identity"]["caseId"]
    verdict = {"judge-eval-unexplored": "insufficient",
               "judge-eval-ownership": "contradicted"}.get(case_id, "supported")
    return provider_reply(model_response(payload, {case_id: verdict}))


if __name__ == "__main__":
    unittest.main()
