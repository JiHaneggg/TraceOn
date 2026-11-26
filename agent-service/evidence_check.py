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
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from investigation_state import hold

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
                "truncated_graph_without_confirmation": "evidence_insufficient",
                "llm_budget_exhausted": "budget_exceeded", "deadline_exceeded": "budget_exceeded"}


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
        if route == "close" and context.get("scope", {}).get("subgraphTruncated") is True:
            return "human_review", "truncated_graph_without_confirmation", details
    if route in LLM_ROUTES and budget["llmCalls"] >= budget["maxLlmCalls"]:
        return "human_review", "llm_budget_exhausted", details
    if route in LLM_ROUTES and (time.time() if now is None else now) >= budget["deadlineAt"]:
        return "human_review", "deadline_exceeded", details
    return route, reason, details


def make_evidence_check(config=None):
    config = config or load_gate_config()

    def evidence_check(state):
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
