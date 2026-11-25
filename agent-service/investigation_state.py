"""Shared state for one alert investigation (PPT: 에이전트 조사 계층).

Every node returns a partial update. `evidence` and `trace` are append-only so
later nodes can cite earlier facts by ID and the run keeps an ordered log.

Final statuses: rejected (invalid input), closed (no suspicion, with evidence),
held (handed to a person, with a hold category), completed (draft ready for
human review).
"""
from __future__ import annotations

from copy import deepcopy
import operator
import time
from typing import Annotated, TypedDict

# Per-alert ceilings from the PPT (Slide 9, Project Charter: 60 seconds per alert).
BUDGET_LIMITS = {"maxLlmCalls": 8, "maxIterations": 3, "maxHops": 4, "maxSeconds": 60}
ALERT_FIELDS = {"graph", "ruleResult", "query"}
# Why an investigation went to a person instead of finishing automatically (PPT 종료 조건).
HOLD_CATEGORIES = {
    "evidence_insufficient": "근거 부족",   # facts or documents cannot support a conclusion
    "model_failure": "모델 실패",           # LLM unavailable, invalid or refused output
    "budget_exceeded": "예산 초과",         # LLM calls, iterations, hops or time
    "no_progress": "진전 없음",             # repeated tool calls without new facts
}
LLM_ERROR_CATEGORIES = {"budget": "budget_exceeded", "deadline": "budget_exceeded"}


class InvestigationState(TypedDict, total=False):
    alert: dict          # frozen input: backend graph + ruleResult (+ optional query)
    identity: dict       # hashes tying every later node to the same snapshot
    alertContext: dict   # alert_context output
    profile: dict        # basic_profile output: account profiles, hints, routing signals
    route: str           # fast_track | investigate | close | human_review
    gate: dict           # evidence_check decision details
    hold: dict           # why the run went to human review: category, node, reasonCode
    budget: dict         # ceilings, counters, LLM usage and the absolute deadline
    status: str          # running | rejected | closed | held | completed
    reasonCodes: list
    evidence: Annotated[list, operator.add]  # {factId, value, evidenceTxIds, source}
    trace: Annotated[list, operator.add]     # ordered node log


def make_budget(overrides=None, *, now=None):
    """Callers may tighten the PPT ceilings, never raise them."""
    budget = dict(BUDGET_LIMITS)
    for key, value in (overrides or {}).items():
        if key not in BUDGET_LIMITS:
            raise ValueError(f"Unknown budget field: {key}")
        if type(value) is not int or not 0 <= value <= BUDGET_LIMITS[key]:
            raise ValueError(f"{key} must be an integer in 0..{BUDGET_LIMITS[key]}")
        budget[key] = value
    started = time.time() if now is None else now
    return {**budget, "llmCalls": 0, "iterations": 0, "hopsExplored": 0,
            "costUsd": 0.0, "inputTokens": 0, "outputTokens": 0, "deadlineAt": started + budget["maxSeconds"]}


def initial_state(alert, budget=None, *, now=None):
    if not isinstance(alert, dict) or set(alert) - ALERT_FIELDS:
        raise ValueError("alert must contain only graph, ruleResult and query")
    if not isinstance(alert.get("graph"), dict) or not isinstance(alert.get("ruleResult"), dict):
        raise ValueError("alert.graph and alert.ruleResult must be backend objects")
    # Copy once so caller-side mutation cannot change the investigated snapshot.
    return {"alert": deepcopy(alert), "budget": make_budget(budget, now=now), "status": "running",
            "reasonCodes": [], "evidence": [], "trace": []}


def remaining_llm_calls(budget):
    return max(0, budget["maxLlmCalls"] - budget["llmCalls"])


def charge_llm(budget, result):
    """Add one LLMClient result (all attempts, including failed ones) to the alert budget."""
    usage = result["usage"]
    return {**budget, "llmCalls": budget["llmCalls"] + result["callsUsed"],
            "costUsd": round(budget["costUsd"] + (result["costUsd"] or 0.0), 6),
            "inputTokens": budget["inputTokens"] + usage["inputTokens"],
            "outputTokens": budget["outputTokens"] + usage["outputTokens"]}


def hold(node, category, reason_code, *, llm_calls=0, **details):
    """State update that sends the alert to human review with a categorized reason."""
    if category not in HOLD_CATEGORIES:
        raise ValueError(f"Unknown hold category: {category}")
    return {"status": "held", "route": "human_review", "reasonCodes": [reason_code],
            "hold": {"category": category, "label": HOLD_CATEGORIES[category], "node": node,
                     "reasonCode": reason_code, **details},
            "trace": [{"node": node, "outcome": "held", "holdCategory": category,
                       "reasonCode": reason_code, "llmCalls": llm_calls}]}


def llm_hold(node, result):
    """Hold after an LLMClient failure: budget/deadline vs model failure, never mixed up."""
    category = LLM_ERROR_CATEGORIES.get(result["errorClass"], "model_failure")
    return hold(node, category, result["reasonCode"], llm_calls=result["callsUsed"],
                errorClass=result["errorClass"], lastErrorCode=result["lastErrorCode"],
                responseIds=[row["responseId"] for row in result["attempts"] if row["responseId"]])
