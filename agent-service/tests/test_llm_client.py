"""Common LLM caller: every error class, the alert limits and secret hygiene (no network)."""
from email.message import Message
import http.client
import io
import json
from pathlib import Path
import random
import socket
import tempfile
import unittest
from urllib.error import HTTPError, URLError

import support  # noqa: F401

from llm_client import ENDPOINT, LLMClient, LLMOutputError, configuration, read_env_file

KEY = "sk-test-secret-key"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
          "properties": {"answer": {"type": "string", "enum": ["A", "B"]}}}


class Response:
    def __init__(self, body=b"", error=None):
        self.body, self.error = body, error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if self.error:
            raise self.error
        return self.body if size < 0 else self.body[:size]


class Script:
    """Fake urlopen: each step is a body, an exception to raise, or a Response."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def __call__(self, request, timeout=None):
        self.requests.append({"request": request, "timeout": timeout})
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, Response) else Response(step)


def completed(output, *, tokens=(1000, 200), response_id="resp_ok", text=None):
    return json.dumps({
        "id": response_id, "status": "completed",
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 50}},
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(output) if text is None else text}]}]}).encode()


def http_error(status, code=None, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = json.dumps({"error": {"code": code, "message": f"provider detail mentioning {KEY}"}}).encode()
    return HTTPError(ENDPOINT, status, "provider error", headers, io.BytesIO(body))


class Harness:
    def __init__(self, *steps, settings=None, model="gpt-6-luna"):
        self.now, self.sleeps = 1000.0, []
        self.script = Script(*steps)
        self.client = LLMClient(KEY, model, transport=self.script, sleep=self.sleep, clock=lambda: self.now,
                                settings=settings, rng=random.Random(0))

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def call(self, **overrides):
        options = {"name": "test_call", "instructions": "Answer A or B.", "payload": {"question": "q"},
                   "schema": SCHEMA, "max_calls": 8, "deadline": self.now + 60, **overrides}
        return self.client.call(**options)


class SuccessTests(unittest.TestCase):
    def test_structured_output_usage_cost_and_request_shape(self):
        harness = Harness(completed({"answer": "A"}))
        result = harness.call(reasoning_effort="low")
        self.assertEqual((result["status"], result["output"], result["responseId"]), ("ok", {"answer": "A"}, "resp_ok"))
        self.assertEqual((result["callsUsed"], result["errorClass"]), (1, None))
        self.assertEqual(result["usage"]["inputTokens"], 1000)
        self.assertAlmostEqual(result["costUsd"], (1000 * 0.10 + 200 * 0.50) / 1e6)
        sent = json.loads(harness.script.requests[0]["request"].data)
        self.assertEqual((sent["store"], sent["text"]["format"]["strict"]), (False, True))
        self.assertEqual(sent["reasoning"], {"effort": "low"})
        self.assertLessEqual(harness.script.requests[0]["timeout"], 45)

    def test_unknown_model_price_reports_cost_as_unknown(self):
        result = Harness(completed({"answer": "B"}), model="other-model").call()
        self.assertEqual((result["status"], result["costUsd"]), ("ok", None))


class NoRetryTests(unittest.TestCase):
    def test_missing_configuration_makes_no_call(self):
        client = LLMClient(env={}, transport=Script())
        result = client.call(name="x", instructions="i", payload={}, schema=SCHEMA, max_calls=8)
        self.assertEqual((result["status"], result["reasonCode"], result["callsUsed"]),
                         ("held", "llm_not_configured", 0))

    def test_auth_quota_request_and_model_errors_are_not_retried(self):
        cases = [(http_error(401, "invalid_api_key"), "auth", "llm_auth_failed"),
                 (http_error(429, "insufficient_quota"), "quota", "llm_quota_exhausted"),
                 (http_error(400, "invalid_json_schema"), "request", "llm_http_400"),
                 (http_error(404, "model_not_found"), "request", "llm_model_not_found")]
        for error, error_class, reason in cases:
            with self.subTest(reason):
                harness = Harness(error)
                result = harness.call()
                self.assertEqual((result["status"], result["errorClass"], result["reasonCode"], result["callsUsed"]),
                                 ("held", error_class, reason, 1))
                self.assertEqual(harness.sleeps, [])

    def test_refusal_is_not_retried(self):
        body = json.dumps({"id": "r", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}).encode()
        result = Harness(body).call()
        self.assertEqual((result["reasonCode"], result["callsUsed"]), ("llm_refused", 1))

    def test_oversized_input_makes_no_call(self):
        result = Harness().call(payload={"blob": "x" * 600_000})
        self.assertEqual((result["reasonCode"], result["callsUsed"]), ("llm_input_too_large", 0))


class TransientRetryTests(unittest.TestCase):
    def test_rate_limit_waits_for_retry_after_then_succeeds(self):
        harness = Harness(http_error(429, "rate_limit_exceeded", retry_after=3), completed({"answer": "A"}))
        result = harness.call()
        self.assertEqual((result["status"], result["callsUsed"]), ("ok", 2))
        self.assertEqual(harness.sleeps, [3.0])
        self.assertEqual(result["attempts"][0]["providerErrorCode"], "rate_limit_exceeded")

    def test_server_errors_retry_with_capped_exponential_backoff_then_hold(self):
        harness = Harness(http_error(500), http_error(503), http_error(502))
        result = harness.call()
        self.assertEqual((result["status"], result["errorClass"], result["callsUsed"]), ("held", "transient", 3))
        self.assertEqual(len(harness.sleeps), 2)
        self.assertTrue(0.5 <= harness.sleeps[0] <= 1.0 and 1.0 <= harness.sleeps[1] <= 2.0)

    def test_network_failures_never_raise(self):
        failures = [URLError("refused"), TimeoutError(), socket.timeout(), ConnectionResetError(),
                    http.client.RemoteDisconnected("closed"),
                    Response(error=http.client.IncompleteRead(b"")), Response(b"not json")]
        for failure in failures:
            with self.subTest(type(failure).__name__):
                harness = Harness(failure, completed({"answer": "A"}))
                result = harness.call()
                self.assertEqual((result["status"], result["callsUsed"]), ("ok", 2))
                self.assertEqual(result["attempts"][0]["outcome"], "transient")


class OutputRetryTests(unittest.TestCase):
    def test_invalid_output_gets_exactly_one_retry(self):
        recovered = Harness(completed(None, text="{broken"), completed({"answer": "B"})).call()
        self.assertEqual((recovered["status"], recovered["callsUsed"]), ("ok", 2))
        failed = Harness(completed(None, text="{broken"), completed(None, text="")).call()
        self.assertEqual((failed["status"], failed["errorClass"], failed["reasonCode"], failed["callsUsed"]),
                         ("held", "output", "llm_missing_output", 2))

    def test_validator_rejection_is_retried_once_with_its_code(self):
        def validate(output):
            if output["answer"] != "B":
                raise LLMOutputError("unknown_evidence_id")
        result = Harness(completed({"answer": "A"}), completed({"answer": "A"})).call(validate=validate)
        self.assertEqual((result["reasonCode"], result["callsUsed"]), ("llm_output_rejected:unknown_evidence_id", 2))
        ok = Harness(completed({"answer": "A"}), completed({"answer": "B"})).call(validate=validate)
        self.assertEqual(ok["status"], "ok")

    def test_incomplete_response_and_failed_attempt_usage_are_counted(self):
        incomplete = json.dumps({"id": "r1", "status": "incomplete",
                                 "incomplete_details": {"reason": "max_output_tokens"},
                                 "usage": {"input_tokens": 500, "output_tokens": 4000}}).encode()
        result = Harness(incomplete, completed({"answer": "A"})).call()
        self.assertEqual(result["attempts"][0]["errorCode"], "llm_incomplete_max_output_tokens")
        self.assertEqual(result["usage"]["inputTokens"], 1500)  # the failed attempt cost money too


class LimitTests(unittest.TestCase):
    def test_remaining_call_budget_stops_retries(self):
        none_left = Harness().call(max_calls=0)
        self.assertEqual((none_left["reasonCode"], none_left["callsUsed"]), ("llm_budget_exhausted", 0))
        one_left = Harness(http_error(503), completed({"answer": "A"})).call(max_calls=1)
        self.assertEqual((one_left["errorClass"], one_left["callsUsed"], one_left["lastErrorCode"]),
                         ("budget", 1, "llm_http_503"))

    def test_deadline_bounds_calls_waits_and_timeouts(self):
        harness = Harness()
        expired = harness.call(deadline=harness.now + 2)
        self.assertEqual((expired["reasonCode"], expired["callsUsed"]), ("llm_deadline_exceeded", 0))
        short = Harness(completed({"answer": "A"}))
        short.call(deadline=short.now + 20)
        self.assertLessEqual(short.script.requests[0]["timeout"], 20)
        waiting = Harness(http_error(429, "rate_limit_exceeded", retry_after=8))
        result = waiting.call(deadline=waiting.now + 10)
        self.assertEqual((result["reasonCode"], waiting.sleeps), ("llm_deadline_exceeded", []))


class SecretHygieneTests(unittest.TestCase):
    def test_results_never_contain_the_key_or_provider_messages(self):
        for steps in ([http_error(401, "invalid_api_key")], [http_error(500)] * 3, [completed({"answer": "A"})]):
            result = Harness(*steps).call()
            text = json.dumps(result)
            self.assertNotIn(KEY, text)
            self.assertNotIn("provider detail", text)
        self.assertNotIn(KEY, repr(Harness().client))

    def test_output_echoing_the_key_is_rejected(self):
        result = Harness(completed(None, text=json.dumps({"answer": KEY})),
                         completed(None, text=json.dumps({"answer": KEY}))).call()
        self.assertEqual(result["reasonCode"], "llm_credential_in_output")


class ConfigurationTests(unittest.TestCase):
    def test_env_file_is_read_without_touching_the_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text('OPENAI_API_KEY="sk-from-file"\nOPENAI_MODEL=\'gpt-6-luna\'\nOTHER=x\n', encoding="utf-8")
            self.assertEqual(read_env_file(path), {"OPENAI_API_KEY": "sk-from-file", "OPENAI_MODEL": "gpt-6-luna"})

    def test_key_pasted_into_model_field_is_never_sent_as_model(self):
        self.assertEqual(configuration({"OPENAI_API_KEY": "sk-abc", "OPENAI_MODEL": "sk-abc"}), ("sk-abc", ""))


if __name__ == "__main__":
    unittest.main()
