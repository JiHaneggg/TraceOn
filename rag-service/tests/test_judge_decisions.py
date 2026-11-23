"""Mock the Decisions endpoint contract and re-use the fixed comparison inputs."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_judge import openai_judge
from agent_judge_demo import SMOKE_CASES, benchmark_payloads, compare_cases
from agent_workflow_demo import fixture_tools
from judge_debate import openai_debate
from judge_decisions import openai_decisions
from test_judge_debate import mock_provider


KEY = "synthetic-private-unit-key"


def choices_response(request, choice):
    body = json.loads(request.data)
    return {"answers": [{"type": "choice", "name": question["name"], "choice": choice, "confidence": 0.8}
                        for question in body["questions"]]}


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        tools, request = fixture_tools(directory)
        self.workflow = tools.call("run_review_workflow", request)
        self.cases = {case["caseId"]: payload for case, payload in
                      benchmark_payloads(self.workflow, SMOKE_CASES, "evaluation")}
        self.enterContext(patch.dict("os.environ", {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "gpt-6-luna"}))

    def test_choice_categories_use_same_input_and_code_scores(self):
        sample = [
            ("judge-eval-alternative", "supported", "passed", 100),
            ("judge-eval-unexplored", "insufficient", "held", 50),
            ("judge-eval-ownership", "contradicted", "rejected", 0),
        ]
        for case_id, choice, expected, score in sample:
            payload = self.cases[case_id]
            def verify_request(request, timeout):
                self.assertEqual(request.full_url, "https://api.openai.com/v1/decisions")
                self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
                body = json.loads(request.data)
                self.assertNotIn(KEY, request.data.decode())
                self.assertEqual(json.loads(body["input"][1]["content"]), payload)
                self.assertEqual([q["value"] for q in body["questions"][0]["choices"]],
                                 ["supported", "insufficient", "contradicted"])
                return io.BytesIO(json.dumps(choices_response(request, choice)).encode())
            with self.subTest(case=case_id):
                result = openai_decisions(payload, verify_request)
                self.assertEqual((result["status"], result["verificationScore"]), (expected, score))
                self.assertEqual(result["modelCallAttempts"], 1)
                self.assertFalse(result["evidenceAttributionProvided"])
                self.assertEqual(result["items"][0]["decisionConfidence"], 0.8)
                self.assertNotIn("reason", result["items"][0])

    def test_api_failure_or_invalid_choice_never_returns_score(self):
        payload = self.cases["judge-eval-ownership"]
        invalid = openai_decisions(
            payload, lambda request, timeout: io.BytesIO(json.dumps(choices_response(request, KEY)).encode())
        )
        self.assertEqual(invalid["responseErrorCode"], "invalid_decisions_choice")
        def denied(request, timeout):
            raise HTTPError(request.full_url, 403, KEY, {}, io.BytesIO(b"{}"))
        refused = openai_decisions(payload, denied)
        self.assertEqual(refused["providerHttpStatus"], 403)
        for result in (invalid, refused):
            self.assertEqual(result["status"], "held")
            self.assertIsNone(result["verificationScore"])
            self.assertNotIn(KEY, json.dumps(result))

    def test_restricts_decisions_to_synthetic_cases(self):
        payload = self.cases["judge-eval-ownership"]
        payload["sources"][0]["sourceKind"] = "official_guidance"
        transport = Mock()
        result = openai_decisions(payload, transport)
        transport.assert_not_called()
        self.assertEqual(result["reasonCode"], "decisions_requires_synthetic_case")

    def test_optional_third_strategy_matches_same_cases(self):
        def decisions_provider(request, timeout):
            body = json.loads(request.data)
            case_id = json.loads(body["input"][1]["content"])["identity"]["caseId"]
            choice = {"judge-eval-unexplored": "insufficient",
                      "judge-eval-ownership": "contradicted"}.get(case_id, "supported")
            return io.BytesIO(json.dumps(choices_response(request, choice)).encode())
        report = compare_cases(
            self.workflow,
            {"single": lambda p: openai_judge(p, mock_provider), "debate": lambda p: openai_debate(p, mock_provider),
             "decisions": lambda p: openai_decisions(p, decisions_provider)},
            "evaluation", 2,
        )
        self.assertEqual([r["modelCallAttempts"] for r in report["strategies"].values()], [6, 12, 6])
        self.assertTrue(all(row["single"] == row["debate"] == row["decisions"] == row["expected"]
                            for row in report["pairedRuns"]))
