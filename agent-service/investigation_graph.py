"""PPT 에이전트 조사 계층 as a LangGraph StateGraph.

Edges between nodes are decided by code from the state; only LLM nodes choose
inside themselves. The recursion limit is the last-resort stop (PPT 종료 조건 5).
Routes whose nodes are not built yet (typology, investigator, human_review) end
the run with the route recorded in the state.
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from alert_context import alert_context
from basic_profile import make_basic_profile
from evidence_check import make_evidence_check
from investigation_state import InvestigationState, initial_state

RECURSION_LIMIT = 25


def stop_unless_running(state):
    return "continue" if state["status"] == "running" else "stop"


def build_graph(similarity_source=None, *, risk_source=None, gate_config=None, checkpointer=None):
    """Routing signals are bound here; None leaves them unavailable.

    similarity_source: EmbeddingSimilarity or a feature reference set.
    risk_source: the team's HybridRiskScore (rule+ML), the gate's main routing signal.
    gate_config: evidence_check settings; defaults to the tuned gate_config.json.
    """
    builder = StateGraph(InvestigationState)
    builder.add_node("alert_context", alert_context)
    builder.add_node("basic_profile", make_basic_profile(similarity_source, risk_source=risk_source))
    builder.add_node("evidence_check", make_evidence_check(gate_config))
    builder.add_edge(START, "alert_context")
    builder.add_conditional_edges("alert_context", stop_unless_running, {"stop": END, "continue": "basic_profile"})
    builder.add_edge("basic_profile", "evidence_check")
    builder.add_edge("evidence_check", END)
    return builder.compile(checkpointer=checkpointer)


def run_investigation(alert, budget=None, *, similarity_source=None, risk_source=None, gate_config=None,
                      checkpointer=None, thread_id=None):
    config = {"recursion_limit": RECURSION_LIMIT}
    if checkpointer is not None:
        config["configurable"] = {"thread_id": thread_id or str(uuid4())}
    graph = build_graph(similarity_source, risk_source=risk_source, gate_config=gate_config,
                        checkpointer=checkpointer)
    return graph.invoke(initial_state(alert, budget), config)
