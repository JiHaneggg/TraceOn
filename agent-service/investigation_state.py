"""Shared state for one alert investigation (PPT: 에이전트 조사 계층).

Every node returns a partial update. `evidence` and `trace` are append-only so
later nodes can cite earlier facts by ID and the run keeps an ordered log.
"""
from __future__ import annotations

from copy import deepcopy
import operator
from typing import Annotated, TypedDict

# Per-alert ceilings from the PPT (Slide 9) and the pipeline notes (3-3).
BUDGET_LIMITS = {"maxLlmCalls": 8, "maxIterations": 3, "maxHops": 4}
ALERT_FIELDS = {"graph", "ruleResult", "query"}


class InvestigationState(TypedDict, total=False):
    alert: dict          # frozen input: backend graph + ruleResult (+ optional query)
    identity: dict       # hashes tying every later node to the same snapshot
    alertContext: dict   # alert_context output
    profile: dict        # basic_profile output: account profiles, hints, routing signals
    route: str           # evidence_check: fast_track | investigate | close | human_review
    gate: dict           # evidence_check decision details
    budget: dict       # ceilings and counters
    status: str          # running | rejected | held | closed | completed
    reasonCodes: list
    evidence: Annotated[list, operator.add]  # {factId, value, evidenceTxIds, source}
    trace: Annotated[list, operator.add]     # ordered node log


def make_budget(overrides=None):
    """Callers may tighten the PPT ceilings, never raise them."""
    budget = dict(BUDGET_LIMITS)
    for key, value in (overrides or {}).items():
        if key not in BUDGET_LIMITS:
            raise ValueError(f"Unknown budget field: {key}")
        if type(value) is not int or not 0 <= value <= BUDGET_LIMITS[key]:
            raise ValueError(f"{key} must be an integer in 0..{BUDGET_LIMITS[key]}")
        budget[key] = value
    return {**budget, "llmCalls": 0, "iterations": 0, "hopsExplored": 0}


def initial_state(alert, budget=None):
    if not isinstance(alert, dict) or set(alert) - ALERT_FIELDS:
        raise ValueError("alert must contain only graph, ruleResult and query")
    if not isinstance(alert.get("graph"), dict) or not isinstance(alert.get("ruleResult"), dict):
        raise ValueError("alert.graph and alert.ruleResult must be backend objects")
    # Copy once so caller-side mutation cannot change the investigated snapshot.
    return {"alert": deepcopy(alert), "budget": make_budget(budget), "status": "running",
            "reasonCodes": [], "evidence": [], "trace": []}
