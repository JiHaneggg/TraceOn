"""Shared LLM caller for the agent's LLM nodes (PPT 예외 처리 표).

One structured-output request to the OpenAI Responses API, with:
- error classes: configuration, auth, quota and request errors are not retried;
  transient errors (network, timeout, truncated body, 408/409/429/5xx) get bounded
  retries with exponential backoff and Retry-After; output-format or validation
  errors get one retry; refusals are not retried;
- the alert's limits: remaining LLM calls and an absolute deadline;
- no exception reaches the caller, and results never contain the API key or
  provider error messages (only whitelisted error codes);
- token usage, cost and response IDs for every attempt.

Standard library only, like the rest of the repository. The key is read from the
process environment, or from an .env file only when a caller asks for it.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = "https://api.openai.com/v1/responses"
# USD per 1M tokens, developers.openai.com/docs/models/gpt-6-luna (checked 2026-11-25).
PRICES = {"gpt-6-luna": {"input": 0.10, "cachedInput": 0.01, "output": 0.50}}
SETTINGS = {"maxTransientRetries": 2, "maxFormatRetries": 1, "requestTimeoutSeconds": 45.0,
            "minimumTimeoutSeconds": 5.0, "backoffBaseSeconds": 1.0, "backoffCapSeconds": 8.0,
            "maxOutputTokens": 4000}
MAX_REQUEST_BYTES = 512_000
MAX_RESPONSE_BYTES = 1_000_000
QUOTA_CODES = {"insufficient_quota", "billing_hard_limit_reached", "account_deactivated"}
PUBLIC_ERROR_CODES = QUOTA_CODES | {"invalid_api_key", "invalid_json_schema", "model_not_found",
                                    "rate_limit_exceeded", "unsupported_parameter", "unsupported_value",
                                    "context_length_exceeded", "server_error"}
INCOMPLETE_REASONS = {"max_output_tokens", "content_filter"}


class LLMOutputError(ValueError):
    """Raised by a caller's validator; the message must be a short public code."""


def read_env_file(path):
    """OPENAI_* values from a .env file, without touching os.environ."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if separator and name in {"OPENAI_API_KEY", "OPENAI_MODEL"}:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
            values[name] = value
    return values


def configuration(env=None):
    env = os.environ if env is None else env
    key = (env.get("OPENAI_API_KEY") or "").strip()
    model = (env.get("OPENAI_MODEL") or "").strip()
    if len(model) >= 2 and model[0] == model[-1] and model[0] in "\"'":
        model = model[1:-1].strip()
    if key and key in model:  # a key pasted into the model field must never be sent as a model name
        model = ""
    return key, model


class _Attempt:
    def __init__(self, kind, code, *, http_status=None, provider_code=None, retry_after=None,
                 usage=None, response_id=None, output=None):
        self.kind, self.code = kind, code
        self.http_status, self.provider_code, self.retry_after = http_status, provider_code, retry_after
        self.usage, self.response_id, self.output = usage, response_id, output


class LLMClient:
    def __init__(self, api_key=None, model=None, *, env=None, transport=None, sleep=time.sleep,
                 clock=time.time, settings=None, rng=None):
        env_key, env_model = configuration(env)
        self._key = env_key if api_key is None else api_key.strip()
        self.model = model or env_model
        self._transport = transport or urlopen
        self._sleep, self._clock = sleep, clock
        self.settings = {**SETTINGS, **(settings or {})}
        self._rng = rng or random.Random()

    def __repr__(self):
        return f"LLMClient(model={self.model!r}, configured={bool(self._key and self.model)})"

    def call(self, *, name, instructions, payload, schema, max_calls, deadline=None, validate=None,
             reasoning_effort=None):
        """Return a result dict with status "ok" (validated output) or "held"; never raises."""
        attempts, started = [], time.perf_counter()

        def finish(status, reason, error_class=None, output=None, response_id=None):
            usage = _sum_usage(attempts)
            return {"status": status, "reasonCode": reason, "errorClass": error_class, "name": name,
                    "lastErrorCode": next((row["errorCode"] for row in reversed(attempts)
                                           if row["errorCode"] != "llm_ok"), None),
                    "model": self.model or None, "output": output, "responseId": response_id,
                    "callsUsed": len(attempts), "attempts": attempts, "usage": usage,
                    "costUsd": _cost(self.model, usage) if attempts else 0.0,
                    "latencyMs": round((time.perf_counter() - started) * 1000)}

        if not self._key or not self.model:
            return finish("held", "llm_not_configured", "configuration")
        body = {"model": self.model, "store": False, "max_output_tokens": self.settings["maxOutputTokens"],
                "input": [{"role": "developer", "content": instructions},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
                "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}}}
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            return finish("held", "llm_input_too_large", "request")
        transient_left, format_left = self.settings["maxTransientRetries"], self.settings["maxFormatRetries"]
        while True:
            if len(attempts) >= max_calls:
                return finish("held", "llm_budget_exhausted", "budget")
            timeout = self.settings["requestTimeoutSeconds"]
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining < self.settings["minimumTimeoutSeconds"]:
                    return finish("held", "llm_deadline_exceeded", "deadline")
                timeout = min(timeout, remaining)
            tried = time.perf_counter()
            attempt = self._attempt(data, timeout, validate)
            attempts.append({"attempt": len(attempts) + 1, "outcome": attempt.kind, "errorCode": attempt.code,
                             "httpStatus": attempt.http_status, "providerErrorCode": attempt.provider_code,
                             "responseId": attempt.response_id, "usage": attempt.usage,
                             "latencyMs": round((time.perf_counter() - tried) * 1000)})
            if attempt.kind == "ok":
                return finish("ok", "llm_ok", output=attempt.output, response_id=attempt.response_id)
            if attempt.kind in {"configuration", "auth", "quota", "request"}:
                return finish("held", attempt.code, attempt.kind)
            if attempt.kind == "refusal":
                return finish("held", attempt.code, "refusal")
            if attempt.kind == "output":
                if format_left:
                    format_left -= 1
                    continue
                return finish("held", attempt.code, "output")
            # transient
            if not transient_left:
                return finish("held", attempt.code, "transient")
            transient_left -= 1
            retry_number = self.settings["maxTransientRetries"] - transient_left
            wait = min(self.settings["backoffCapSeconds"],
                       self.settings["backoffBaseSeconds"] * 2 ** (retry_number - 1) * self._rng.uniform(0.5, 1.0))
            if attempt.retry_after is not None:
                wait = min(self.settings["backoffCapSeconds"], max(wait, attempt.retry_after))
            if deadline is not None and self._clock() + wait + self.settings["minimumTimeoutSeconds"] > deadline:
                return finish("held", "llm_deadline_exceeded", "deadline")
            attempts[-1]["waitSeconds"] = round(wait, 2)
            self._sleep(wait)

    def _attempt(self, data, timeout, validate):
        request = Request(ENDPOINT, data, {"Content-Type": "application/json",
                                           "Authorization": "Bearer " + self._key})
        try:
            with self._transport(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            return _http_error(error)
        except TimeoutError:
            return _Attempt("transient", "llm_timeout")
        except (URLError, OSError, http.client.HTTPException):
            # Covers refused/reset connections, RemoteDisconnected and IncompleteRead.
            return _Attempt("transient", "llm_network_error")
        if len(raw) > MAX_RESPONSE_BYTES:
            return _Attempt("output", "llm_response_too_large")
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return _Attempt("transient", "llm_invalid_envelope")
        usage, response_id = _usage(envelope), envelope.get("id") if isinstance(envelope.get("id"), str) else None
        found = dict(usage=usage, response_id=response_id)
        if envelope.get("status") != "completed":
            reason = (envelope.get("incomplete_details") or {}).get("reason")
            code = f"llm_incomplete_{reason}" if reason in INCOMPLETE_REASONS else "llm_incomplete"
            return _Attempt("output", code, **found)
        try:
            parts = [part for item in envelope.get("output", []) if item.get("type") == "message"
                     for part in item.get("content", [])]
        except (AttributeError, TypeError):
            return _Attempt("output", "llm_invalid_output_shape", **found)
        if any(isinstance(part, dict) and part.get("type") == "refusal" for part in parts):
            return _Attempt("refusal", "llm_refused", **found)
        text = "".join(part.get("text", "") for part in parts
                       if isinstance(part, dict) and part.get("type") == "output_text")
        if not text.strip():
            return _Attempt("output", "llm_missing_output", **found)
        try:
            output = json.loads(text)
        except ValueError:
            return _Attempt("output", "llm_output_not_json", **found)
        if self._key in text:
            return _Attempt("output", "llm_credential_in_output", **found)
        if validate is not None:
            try:
                validate(output)
            except LLMOutputError as error:
                return _Attempt("output", "llm_output_rejected:" + str(error)[:64], **found)
            except (ValueError, KeyError, TypeError):
                return _Attempt("output", "llm_output_rejected", **found)
        return _Attempt("ok", "llm_ok", output=output, **found)


def _http_error(error):
    status, code, retry_after = error.code, None, None
    try:
        with error:
            body = error.read(16_385)
            header = error.headers.get("Retry-After") if error.headers else None
        if header is not None:
            retry_after = max(0.0, float(header))
        if len(body) <= 16_384:
            candidate = json.loads(body)["error"]["code"]
            if isinstance(candidate, str) and candidate in PUBLIC_ERROR_CODES:
                code = candidate
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        pass
    details = dict(http_status=status, provider_code=code, retry_after=retry_after)
    if status == 429:
        if code in QUOTA_CODES:
            return _Attempt("quota", "llm_quota_exhausted", **details)
        return _Attempt("transient", "llm_rate_limited", **details)
    if status in (408, 409) or status >= 500:
        return _Attempt("transient", f"llm_http_{status}", **details)
    if status in (401, 403):
        return _Attempt("auth", "llm_auth_failed", **details)
    if code == "model_not_found":
        return _Attempt("request", "llm_model_not_found", **details)
    return _Attempt("request", f"llm_http_{status}", **details)


def _usage(envelope):
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None
    details_in = usage.get("input_tokens_details") or {}
    details_out = usage.get("output_tokens_details") or {}
    values = {"inputTokens": usage.get("input_tokens"), "cachedTokens": details_in.get("cached_tokens") or 0,
              "outputTokens": usage.get("output_tokens"), "reasoningTokens": details_out.get("reasoning_tokens") or 0}
    return values if all(isinstance(v, int) for v in values.values()) else None


def _sum_usage(attempts):
    totals = {"inputTokens": 0, "cachedTokens": 0, "outputTokens": 0, "reasoningTokens": 0}
    for attempt in attempts:
        for key, value in (attempt["usage"] or {}).items():
            totals[key] += value
    return totals


def _cost(model, usage):
    price = PRICES.get(model)
    if price is None:
        return None
    fresh = usage["inputTokens"] - usage["cachedTokens"]
    return round((fresh * price["input"] + usage["cachedTokens"] * price["cachedInput"]
                  + usage["outputTokens"] * price["output"]) / 1_000_000, 6)
