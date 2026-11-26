from decimal import Decimal
from importlib import import_module
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root / name) for name in ("scripts", "rag-service")]
from agent_judge import InvalidJudgeResponse
from transaction_risk_grade import openai_risk_grade, validate_grade

evaluation = import_module("evaluate-transaction-risk-grades")
INPUT = {"targetTxId": "T-2", "transactions": [{"txId": "T-1"}, {"txId": "T-2"}]}
ENV = {"OPENAI_API_KEY": "risk-grade-test-placeholder", "OPENAI_MODEL": "test-model"}


def answer(grade, ids):
    return {"riskGrade": grade, "evidenceTxIds": ids, "reason": "관련 거래 확인"}


def reply(body):
    return BytesIO(json.dumps({"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(body)},
    ]}]}).encode())


class TransactionRiskGradeTest(unittest.TestCase):
    def test_grades_use_only_original_ids_and_fixed_signals(self):
        for grade, ids, signal in (
                ("high", ["T-1", "T-2"], 1), ("medium", ["T-2"], 0.5),
                ("low", ["T-2"], 0), ("insufficient", [], None)):
            with self.subTest(grade=grade):
                graded = validate_grade(INPUT, answer(grade, ids))
                self.assertEqual(graded["rankSignal"], signal)
        for ids in (["T-2"], ["T-1"], ["T-2", "outside"], ["T-2", "T-2"]):
            with self.subTest(ids=ids), self.assertRaises(InvalidJudgeResponse):
                validate_grade(INPUT, answer("high", ids))

    def test_provider_gets_only_the_graph_and_constrained_evidence_ids(self):
        def transport(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(json.loads(body["input"][1]["content"]), INPUT)
            self.assertFalse(body["store"])
            schema = body["text"]["format"]["schema"]
            self.assertEqual(schema["properties"]["evidenceTxIds"]["items"]["enum"], ["T-1", "T-2"])
            self.assertEqual(set(schema["properties"]["riskGrade"]["enum"]),
                             {"high", "medium", "low", "insufficient"})
            return reply(answer("high", ["T-2", "T-1"]))

        with patch.dict("os.environ", ENV):
            result = openai_risk_grade(INPUT, transport)
        self.assertEqual(result["riskGrade"], "high")
        self.assertEqual(result["modelCallAttempts"], 1)

    def test_invalid_evidence_stays_separate_from_model_abstention(self):
        with patch.dict("os.environ", ENV):
            for proposed, expected in (
                    (answer("insufficient", []), "insufficient"),
                    (answer("high", ["T-2", "missing"]), "unscored"),
                    ({**answer("low", ["T-2"]), "reason": ENV["OPENAI_API_KEY"]}, "unscored")):
                with self.subTest(expected=expected):
                    result = openai_risk_grade(INPUT, lambda request, timeout: reply(proposed))
                    self.assertEqual(result["riskGrade"], expected)
                    self.assertIsNone(result["rankSignal"])
                    self.assertNotIn(ENV["OPENAI_API_KEY"], json.dumps(result))

    def test_missing_grades_keep_ml_priority_while_low_is_a_real_signal(self):
        base = [Decimal("0.6")] * 5
        rows = [{"rankSignal": value} for value in (0, 0.5, 1, None, None)]
        self.assertEqual(evaluation.blended_scores(base, rows, 25), [45, Decimal("57.5"), 70, 60, 60])
        self.assertEqual(evaluation.blended_scores(base, rows, 0), [60] * 5)

    def test_mock_result_cannot_tune_live_ranking(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mock.json"
            path.write_text(json.dumps({
                "suite": evaluation.RESULT_SUITE, "mode": "mock", "modelConfig": {}, "decisions": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "complete live"):
                evaluation.read_live_result({"cases": []}, path)

    def test_development_reuses_only_valid_pilot_results_in_original_order(self):
        second_input = {**INPUT, "targetTxId": "T-1"}
        case, other = {"input": INPUT}, {"input": second_input}
        packet = {"packetSha256": "development", "cases": [other, case]}
        pilot = {"packetSha256": "pilot", "cases": [case, other]}
        version = {"model": "test-model", "detectorVersion": evaluation.GRADE_VERSION}

        def saved_grade(payload, fields):
            return {
                **version, **fields, "modelCallAttempts": 1, "targetTxId": payload["targetTxId"],
                "inputSha256": evaluation.detector.digest(evaluation.detector.canonical(payload)),
            }

        rows = [
            saved_grade(INPUT, validate_grade(INPUT, answer("high", ["T-2", "T-1"]))),
            saved_grade(second_input, {"riskGrade": "unscored", "rankSignal": None, "reasonCode": "bad_id"}),
        ]
        pilot_result = {
            "suite": evaluation.RESULT_SUITE, "mode": "openai", "modelConfig": version,
            "caseCount": 2, "packetSha256": "pilot", "decisions": rows,
        }
        new_grade = saved_grade(second_input, validate_grade(second_input, answer("low", ["T-1"])))
        with TemporaryDirectory() as directory:
            pilot_path = Path(directory) / "pilot.json"
            pilot_path.write_text(json.dumps(pilot_result), encoding="utf-8")
            output = Path(directory) / "dev.json"
            with patch.object(evaluation, "prepare_pilot", return_value=pilot), patch.object(
                    evaluation, "openai_risk_grade", return_value=new_grade) as run:
                with self.assertRaisesRegex(ValueError, "same OpenAI model"):
                    evaluation.grade_packet(packet, "openai", "wrong-model", output, pilot_path)
                run.assert_not_called()
                result = evaluation.grade_packet(packet, "openai", "test-model", output, pilot_path)
                run.assert_called_once_with(second_input)
        self.assertEqual(result["decisions"], [new_grade, rows[0]])
        self.assertEqual(result["modelCallAttempts"], 2)
        self.assertEqual(result["reuse"]["reusedPilotDecisions"], 1)
        self.assertEqual(result["reuse"]["newModelCallAttempts"], 1)


if __name__ == "__main__":
    unittest.main()
