"""Exercise the code gate, Judge transport, and reusable synthetic cases offline."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_judge import openai_judge
from agent_judge_demo import evaluate_cases
from agent_workflow import run_review_workflow
from agent_workflow_demo import FIXTURES, fixture_tools
from corpus import build


KEY = "unit-test-secret-never-persist"


def model_response(payload, overrides=None):
    overrides = overrides or {}
    return {"items": [{
        "itemId": item["itemId"],
        "verdict": overrides.get(item["itemId"], "supported"),
        "factIds": [payload["facts"][0]["factId"]],
        "chunkIds": [payload["sources"][0]["chunkId"]],
        "reason": "합성 근거를 사용한 모의 판정",
    } for item in payload["items"]]}


def provider_reply(result):
    return io.BytesIO(json.dumps({
        "status": "completed",
        "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]},
        ],
    }).encode())


class AgentJudgeTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.tools, self.request = fixture_tools(directory)
        self.enterContext(patch.dict(os.environ, {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "fixture-model"}))

    def tool_flow(self, provider):
        with patch("agent_judge.urlopen", side_effect=provider) as transport:
            result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "openai"})
        transport.assert_called_once()
        return result

    def test_supported_claims_pass_with_original_evidence_and_human_review(self):
        def provider(request, timeout):
            self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
            body = json.loads(request.data)
            self.assertFalse(body["store"])
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertNotIn(KEY, request.data.decode())
            payload = json.loads(body["input"][1]["content"])
            self.assertEqual(len(payload["transactions"]), 3)
            self.assertEqual({row["factId"]: row["value"] for row in payload["facts"]}["target.amountPaid"], "1002.10")
            self.assertTrue(any("합당한 근거" in row["originalSection"] for row in payload["sources"]))
            self.assertTrue(all(row["sourceKind"] == "synthetic_fixture" for row in payload["sources"]))
            self.assertIn("### 합성 테스트 자료", payload["draft"])
            self.assertNotIn("공식 안내", payload["draft"])
            self.assertEqual({item["criterion"] for item in payload["items"]},
                             {"claim_support", "document_support", "normal_trade_consideration"})
            return provider_reply(model_response(payload))
        result = self.tool_flow(provider)
        self.assertTrue(result["verification"]["valid"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["judge"]["verificationScore"], 100)
        self.assertEqual(result["identification"]["ruleScore"], 90)
        self.assertEqual(result["judge"]["modelCallAttempts"], 1)
        self.assertTrue(result["humanReviewRequired"])
        self.assertFalse(result["readyForSubmission"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_synthetic_source_missing_locally_does_not_fetch(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
        with patch("corpus.urlopen") as fetch:
            with self.assertRaisesRegex(ValueError, "local HTML snapshot"):
                build(directory / "raw", directory / "index.json", sources=sources)
        fetch.assert_not_called()

    def test_missing_claim_support_or_unexplored_normal_trade_holds(self):
        for item_id in ("claim:finding.0", "normal_trade_consideration"):
            with self.subTest(item_id=item_id):
                def provider(request, timeout):
                    payload = json.loads(json.loads(request.data)["input"][1]["content"])
                    return provider_reply(model_response(payload, {item_id: "insufficient"}))
                result = self.tool_flow(provider)
                self.assertTrue(result["verification"]["valid"])
                self.assertEqual(result["status"], "held")
                self.assertEqual(result["judge"]["verificationScore"], 50)
                self.assertEqual(next(i for i in result["judge"]["items"] if i["itemId"] == item_id)["verdict"],
                                 "insufficient")

    def test_source_contradiction_takes_priority_over_insufficient(self):
        def provider(request, timeout):
            payload = json.loads(json.loads(request.data)["input"][1]["content"])
            return provider_reply(model_response(payload, {
                "document_support": "contradicted", "normal_trade_consideration": "insufficient",
            }))
        result = self.tool_flow(provider)
        self.assertTrue(result["verification"]["valid"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["judge"]["verificationScore"], 0)
        self.assertIn("judge_contradicted", result["reasonCodes"])

    def test_code_rejects_wrong_amount_time_count_and_citation_without_judge(self):
        for field, value in (("target.amountPaid", "999999.99"), ("target.timestamp", "2022-09-01T12:01:00"),
                             ("finding.0.transactionCount", 9), ("citation", "없는 인용")):
            with self.subTest(field=field), patch("agent_judge.urlopen") as transport:
                def call(name, fields):
                    response = self.tools.call(name, fields)
                    if name == "generate_review_report":
                        if field == "citation":
                            response["draft"]["citations"][0]["quote"] = value
                        else:
                            next(f for f in response["draft"]["facts"] if f["factId"] == field)["value"] = value
                    return response
                result = run_review_workflow(call, self.request["graph"], self.request["ruleResult"],
                                             self.request["query"], judge=openai_judge)
                self.assertEqual(result["status"], "rejected")
                self.assertFalse(result["verification"]["valid"])
                self.assertEqual(result["judge"]["status"], "skipped")
                self.assertEqual(result["judge"]["modelCallAttempts"], 0)
                transport.assert_not_called()

    def test_preverification_abstention_does_not_call_judge(self):
        with patch("agent_judge.urlopen") as transport:
            result = self.tools.call("run_review_workflow", {
                **self.request, "judgeMode": "openai", "query": "화성 대기압 광합성",
            })
        transport.assert_not_called()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["judge"]["status"], "skipped")

    def test_provider_failure_holds_and_does_not_leak_error(self):
        result = self.tool_flow(URLError(KEY))
        self.assertTrue(result["verification"]["valid"])
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["judge"]["reasonCode"], "judge_model_unavailable")
        self.assertIsNone(result["judge"]["verificationScore"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_literal_quotes_in_model_name_are_removed(self):
        def provider(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(body["model"], "gpt-6-luna")
            return provider_reply(model_response(json.loads(body["input"][1]["content"])))
        with patch.dict(os.environ, {"OPENAI_MODEL": "'gpt-6-luna'"}):
            result = self.tool_flow(provider)
        self.assertEqual(result["judge"]["model"], "gpt-6-luna")
        self.assertEqual(result["status"], "passed")

    def test_http_failure_exposes_only_status_and_known_code(self):
        for code, expected in (("model_not_found", "model_not_found"), (KEY, None)):
            with self.subTest(expected=expected):
                body = io.BytesIO(json.dumps({"error": {"code": code, "message": KEY}}).encode())
                error = HTTPError("https://api.openai.com/v1/responses", 404, KEY, {}, body)
                result = self.tool_flow(error)
                self.assertEqual(result["status"], "held")
                self.assertEqual(result["judge"]["reasonCode"], "judge_api_error")
                self.assertEqual(result["judge"]["providerHttpStatus"], 404)
                self.assertEqual(result["judge"]["providerErrorCode"], expected)
                self.assertIsNone(result["judge"]["verificationScore"])
                self.assertNotIn(KEY, json.dumps(result))

    def test_missing_settings_hold_without_network(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("agent_judge.urlopen") as transport:
            result = self.tools.call("run_review_workflow", {**self.request, "judgeMode": "openai"})
        transport.assert_not_called()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["judge"]["reasonCode"], "judge_not_configured")

    def test_unknown_evidence_or_missing_item_is_not_accepted(self):
        defects = {
            "missing_item": "incomplete_judgment_coverage",
            "invented_evidence": "unknown_evidence_id",
            "document_without_source": "missing_document_reference",
        }
        for defect, expected in defects.items():
            with self.subTest(defect=defect):
                def provider(request, timeout):
                    payload = json.loads(json.loads(request.data)["input"][1]["content"])
                    response = model_response(payload)
                    document = next(item for item in response["items"] if item["itemId"] == "document_support")
                    if defect == "missing_item":
                        response["items"].pop()
                    else:
                        document["chunkIds"] = ["invented-chunk"] if defect == "invented_evidence" else []
                    return provider_reply(response)
                result = self.tool_flow(provider)
                self.assertEqual(result["status"], "held")
                self.assertEqual(result["judge"]["reasonCode"], "judge_invalid_response")
                self.assertEqual(result["judge"]["responseErrorCode"], expected)

    def test_incomplete_or_refused_response_holds(self):
        responses = [
            ({"status": "incomplete"}, "judge_model_incomplete"),
            ({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
             "judge_model_refused"),
        ]
        for provider_body, reason in responses:
            with self.subTest(reason=reason):
                result = self.tool_flow(lambda request, timeout: io.BytesIO(json.dumps(provider_body).encode()))
                self.assertEqual(result["status"], "held")
                self.assertEqual(result["judge"]["reasonCode"], reason)

    def test_reusable_benchmark_records_same_inputs_and_metrics(self):
        workflow = self.tools.call("run_review_workflow", self.request)
        def judge(payload):
            case_id = payload["identity"]["caseId"]
            verdict = {"judge-dev-funds": "insufficient", "judge-dev-source": "contradicted"}.get(case_id, "supported")
            self.assertNotIn("expected", payload)
            def provider(request, timeout):
                return provider_reply(model_response(payload, {case_id: verdict}))
            return openai_judge(payload, provider)
        report = evaluate_cases(workflow, judge, "development", 2)
        self.assertEqual(report["rejectF1"], 1)
        self.assertEqual(report["repeatAgreement"], 1)
        self.assertEqual(len(report["cases"]), 3)
        for case in report["cases"]:
            self.assertEqual({run["inputSha256"] for run in case["runs"]}, {case["inputSha256"]})
            self.assertEqual({run["status"] for run in case["runs"]}, {case["expected"]})

    def test_benchmark_separates_unscored_attempts_from_verdicts(self):
        workflow = self.tools.call("run_review_workflow", self.request)
        attempted = []

        def judge(payload):
            attempted.append(payload["identity"]["caseId"])
            response = model_response(payload, {attempted[-1]: "contradicted"})
            if len(attempted) == 1:
                response["items"][0]["chunkIds"] = []
            return openai_judge(payload, lambda request, timeout: provider_reply(response))

        case_id = "judge-eval-ownership"
        report = evaluate_cases(workflow, judge, "evaluation", 2, case_id=case_id)
        self.assertEqual(attempted, [case_id, case_id])
        self.assertEqual(report["confusionMatrix"]["rejected"], {
            "passed": 0, "held": 0, "rejected": 1, "unscored": 1,
        })
        self.assertEqual(report["unscoredRuns"], 1)
        self.assertEqual(report["rejectRecall"], 0.5)
        self.assertEqual(report["scoredRepeatPairs"], 0)
        self.assertEqual(report["totalRepeatPairs"], 1)
        self.assertIsNone(report["repeatAgreement"])


if __name__ == "__main__":
    unittest.main()
