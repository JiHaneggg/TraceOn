from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_judge import InvalidJudgeResponse
from transaction_detector import openai_detect, validate_decision


INPUT = {"targetTxId": "T-2", "transactions": [{"txId": "T-1"}, {"txId": "T-2"}]}


class TransactionDetectorTest(unittest.TestCase):
    def test_positive_requires_target_and_only_unique_source_ids(self):
        invalid = (["T-1"], ["T-2", "outside"], ["T-2", "T-2"])
        for ids in invalid:
            with self.subTest(ids=ids), self.assertRaises(InvalidJudgeResponse):
                validate_decision(INPUT, {"decision": "candidate", "evidenceTxIds": ids, "reason": "근거"})

    def test_models_use_enum_for_original_ids_and_return_distinct_decisions(self):
        for verdict, expected in (("candidate", True), ("not_candidate", False), ("insufficient", None)):
            def provider(request, timeout):
                payload = json.loads(request.data)
                self.assertEqual(json.loads(payload["input"][1]["content"]), INPUT)
                self.assertFalse(payload["store"])
                ids = payload["text"]["format"]["schema"]["properties"]["evidenceTxIds"]["items"]["enum"]
                self.assertEqual(ids, ["T-1", "T-2"])
                response = {"decision": verdict, "evidenceTxIds": ["T-2"], "reason": "근거 확인"}
                return BytesIO(json.dumps({"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(response)},
                ]}]}).encode())

            with self.subTest(verdict=verdict), patch.dict("os.environ", {
                "OPENAI_API_KEY": "detector-test-placeholder", "OPENAI_MODEL": "test-model",
            }):
                result = openai_detect(INPUT, provider)
                self.assertIs(result["flagged"], expected)
                self.assertEqual(result["decision"], verdict)
                self.assertEqual(result["modelCallAttempts"], 1)

    def test_transport_failure_has_no_negative_prediction(self):
        def provider(request, timeout):
            raise OSError("synthetic failure body")

        with patch.dict("os.environ", {"OPENAI_API_KEY": "detector-test-placeholder", "OPENAI_MODEL": "test-model"}):
            result = openai_detect(INPUT, provider)
        self.assertEqual(result["decision"], "unscored")
        self.assertIsNone(result["flagged"])
        self.assertEqual(result["modelCallAttempts"], 1)
        self.assertNotIn("synthetic failure body", json.dumps(result))
