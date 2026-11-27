"""Node 3 · evidence_check (code gate, no LLM): where does this alert go next?

PPT Slide 8 2x2, with the team's rule+ML risk score in the similarity slot
(FastRP similarity showed no signal on validation; see README):

    risk high & signal facts  -> fast_track   (typology)
    risk high & no facts      -> investigate  (evidence conflict)
    risk low  & signal facts  -> investigate  (possible new type)
    risk low  & no facts      -> close        (false-positive closure, no LLM call)

Safety rules: an alert the trigger rule did not flag is closed as "not an alert";
without a risk score the gate never closes (facts -> investigate, none ->
human review as evidence_insufficient); a route that needs an LLM call goes to
human review as budget_exceeded when the LLM calls or the alert's time budget are
used up. Every closure records the fact IDs that were checked.

During an investigation the same node re-checks the PPT stop conditions after
every investigator step, in order: evidence sufficient -> typology; no suspicion
-> close; no progress -> human review; budget exceeded -> human review.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from investigation_state import RESERVED_LLM_CALLS, hold, remaining_llm_calls

NODE = "evidence_check"
CONFIG_PATH = Path(__file__).with_name("gate_config.json")
SIGNAL_HINTS = ("CYCLE_PATH", "PASS_THROUGH", "RAPID_BURST", "REPEATED_AMOUNTS", "JUST_BELOW_THRESHOLD")
SIGNAL_FACT_SUFFIXES = ("timing.maxTxInBurstWindow", "amounts.largestSimilarAmountGroup",
                        "amounts.justBelowThresholdCount", "forwarding.fastestForwardSeconds",
                        "forwarding.passThroughRatio")
LLM_ROUTES = {"fast_track", "investigate"}


def load_gate_config(path=CONFIG_PATH):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    cut = config.get("riskCut")
    if isinstance(cut, bool) or not isinstance(cut, (int, float)) or not 0 <= cut <= 1:
        raise ValueError("gate riskCut must be a number in 0..1 (run tune_gate.py)")
    if not set(config.get("signalHints", [])) <= set(SIGNAL_HINTS):
        raise ValueError("Unknown signal hint in gate config")
    return config


HOLD_REASONS = {"risk_unavailable_without_facts": "evidence_insufficient",
                "checklist_incomplete": "evidence_insufficient", "checklist_mixed": "evidence_insufficient",
                "closure_not_confirmed": "evidence_insufficient",
                "closure_without_investigation": "evidence_insufficient",
                "repeated_tool_call": "no_progress", "unconfirmed_not_decreasing": "no_progress",
                "llm_budget_exhausted": "budget_exceeded", "deadline_exceeded": "budget_exceeded"}
# PPT 종료 조건 1·2 decided by code from the checklist, never from the model's conclusion alone.
MIN_SUSPICIOUS_FOR_TYPOLOGY = 2   # 체크리스트 충족: two or more items confirmed suspicious by facts
MIN_BENIGN_FOR_CLOSURE = 2        # 혐의 없음: no suspicious item and two or more shown normal by facts
MIN_TOOL_CALLS_FOR_CLOSURE = 1    # never close on the gate's facts alone; investigate at least once
STALL_STEPS = 2                  # unconfirmed items not shrinking over this many tool steps -> no progress


def checklist_outcome(rows):
    """'typology', 'close', 'mixed' (resolved but neither), or None while items stay unconfirmed."""
    statuses = [row["status"] for row in rows]
    if "unconfirmed" in statuses:
        return None
    if statuses.count("suspicious") >= MIN_SUSPICIOUS_FOR_TYPOLOGY:
        return "typology"
    if "suspicious" not in statuses and statuses.count("benign") >= MIN_BENIGN_FOR_CLOSURE:
        return "close"
    return "mixed"


def loop_decide(investigation, budget, now=None):
    """Stop conditions after an investigator step, in PPT order; returns (route, reasonCode, details)."""
    last = investigation["decisions"][-1]
    rows = last["checklist"]

    def items(status):
        return [row["itemId"] for row in rows if row["status"] == status]

    def cited(status):
        return sorted({fact for row in rows if row["status"] == status for fact in row["factIds"]})

    # Outcomes are judged only when the investigator concludes: a complete-looking checklist
    # must not cut off an investigator that still wants to look further.
    if last["action"]["type"] == "conclude":
        outcome = checklist_outcome(rows)
        if outcome == "typology":
            return "typology", "evidence_sufficient", {"suspiciousItems": items("suspicious"),
                                                       "basisFactIds": cited("suspicious"),
                                                       "modelConclusion": last["conclusion"]}
        if outcome == "close" and last["conclusion"] == "no_suspicion":
            if len(investigation["toolCalls"]) >= MIN_TOOL_CALLS_FOR_CLOSURE:
                return "close", "no_suspicion", {"benignItems": items("benign"),
                                                 "closureBasisFactIds": cited("benign"),
                                                 "modelConclusion": last["conclusion"]}
            reason = "closure_without_investigation"
        elif outcome == "close":
            reason = "closure_not_confirmed"
        else:
            reason = "checklist_incomplete" if outcome is None else "checklist_mixed"
        return "human_review", reason, {"modelConclusion": last["conclusion"], "unconfirmedItems": items("unconfirmed"),
                                        "suspiciousItems": items("suspicious"), "benignItems": items("benign")}
    call = investigation["toolCalls"][-1]
    if call["refused"] == "repeated_call":
        return "human_review", "repeated_tool_call", {"callId": call["callId"]}
    counts = [sum(row["status"] == "unconfirmed" for row in decision["checklist"])
              for decision in investigation["decisions"]]
    if len(counts) > STALL_STEPS and all(counts[-step] >= counts[-step - 1] for step in range(1, STALL_STEPS + 1)):
        return "human_review", "unconfirmed_not_decreasing", {"unconfirmedCounts": counts}
    if remaining_llm_calls(budget) - RESERVED_LLM_CALLS <= 0:
        return "human_review", "llm_budget_exhausted", {}
    if (time.time() if now is None else now) >= budget["deadlineAt"]:
        return "human_review", "deadline_exceeded", {}
    return "investigate", "continue_investigation", {"toolCalls": len(investigation["toolCalls"])}


def decide(context, profile, budget, config, now=None):
    """Pure routing decision; returns (route, reasonCode, details)."""
    signals = [hint for hint in profile["hints"] if hint["kind"] in config["signalHints"]]
    risk = profile["riskScore"]
    details = {"signalHints": [hint["hintId"] + ":" + hint["kind"] for hint in signals],
               "riskStatus": risk["status"], "riskScore": risk.get("score"), "riskCut": config["riskCut"]}
    if not context["flagged"]:
        return "close", "rule_not_flagged", details
    if risk["status"] != "computed":
        route, reason = ("investigate", "risk_unavailable_with_facts") if signals else \
                        ("human_review", "risk_unavailable_without_facts")
    else:
        high = risk["score"] >= config["riskCut"]
        details["riskHigh"] = high
        route, reason = {(True, True): ("fast_track", "high_risk_with_facts"),
                         (True, False): ("investigate", "high_risk_without_facts"),
                         (False, True): ("investigate", "low_risk_with_facts"),
                         (False, False): ("close", "low_risk_without_facts")}[(high, bool(signals))]
    if route in LLM_ROUTES and budget["llmCalls"] >= budget["maxLlmCalls"]:
        return "human_review", "llm_budget_exhausted", details
    if route in LLM_ROUTES and (time.time() if now is None else now) >= budget["deadlineAt"]:
        return "human_review", "deadline_exceeded", details
    return route, reason, details


def make_evidence_check(config=None):
    config = config or load_gate_config()

    def evidence_check(state):
        investigation = state.get("investigation")
        if investigation and investigation["decisions"]:
            return loop_check(state, investigation)
        route, reason, details = decide(state["alertContext"], state["profile"], state["budget"], config)
        if route == "close":
            details["closureBasisFactIds"] = [row["factId"] for row in state["evidence"]
                                              if row["source"] == "basic_profile"
                                              and row["factId"].endswith(SIGNAL_FACT_SUFFIXES)]
        gate = {"configVersion": config["version"], "reasonCode": reason, **details}
        if route == "human_review":
            return {**hold(NODE, HOLD_REASONS[reason], reason), "gate": gate}
        update = {"route": route, "gate": gate,
                  "trace": [{"node": NODE, "outcome": route, "reasonCode": reason, "llmCalls": 0}]}
        if route == "close":
            update.update(status="closed", reasonCodes=[reason])
        return update
    return evidence_check


def loop_check(state, investigation):
    route, reason, details = loop_decide(investigation, state["budget"])
    gate = {**state["gate"], "loop": {"reasonCode": reason, **details}}
    if route == "human_review":
        return {**hold(NODE, HOLD_REASONS[reason], reason, **details), "gate": gate}
    update = {"route": route, "gate": gate,
              "trace": [{"node": NODE, "phase": "loop", "outcome": route, "reasonCode": reason, "llmCalls": 0}]}
    if route == "close":
        update.update(status="closed", reasonCodes=[reason])
    return update
