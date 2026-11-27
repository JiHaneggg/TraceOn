"""PPT 에이전트 조사 계층 as a LangGraph StateGraph.

    alert_context -> basic_profile -> evidence_check --investigate--> investigator
                                          ^                          |        |
                                          |<------ conclude ---------+        | call_tool
                                          |<------------- graph_tools <-------+

Edges between nodes are decided by code from the state; only LLM nodes choose
inside themselves. The recursion limit is the last-resort stop (PPT 종료 조건 5).
Routes whose nodes are not built yet (fast_track/typology) end the run with the
route recorded in the state.
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from alert_context import alert_context
from basic_profile import make_basic_profile
from evidence_check import make_evidence_check
from investigation_state import InvestigationState, initial_state
from investigator import make_graph_tools, make_investigator

RECURSION_LIMIT = 25


def stop_unless_running(state):
    return "continue" if state["status"] == "running" else "stop"


def after_gate(state):
    return "investigate" if state["status"] == "running" and state.get("route") == "investigate" else "stop"


def after_investigator(state):
    if state["status"] != "running":
        return "stop"
    return "tool" if state["investigation"]["pendingAction"] else "check"


def build_graph(similarity_source=None, *, risk_source=None, gate_config=None, llm_client=None,
                graph_source=None, checkpointer=None):
    """Routing signals, the LLM client and the transaction store are bound here.

    similarity_source: EmbeddingSimilarity or a feature reference set (recorded only).
    risk_source: the team's HybridRiskScore (rule+ML), the gate's main routing signal.
    gate_config: evidence_check settings; defaults to the tuned gate_config.json.
    llm_client: LLMClient for the investigator; defaults to one configured from the environment.
    graph_source: store for expand_account (IndexSource); defaults to subgraph only.
    """
    builder = StateGraph(InvestigationState)
    builder.add_node("alert_context", alert_context)
    builder.add_node("basic_profile", make_basic_profile(similarity_source, risk_source=risk_source))
    builder.add_node("evidence_check", make_evidence_check(gate_config))
    builder.add_node("investigator", make_investigator(llm_client))
    builder.add_node("graph_tools", make_graph_tools(graph_source))
    builder.add_edge(START, "alert_context")
    builder.add_conditional_edges("alert_context", stop_unless_running, {"stop": END, "continue": "basic_profile"})
    builder.add_edge("basic_profile", "evidence_check")
    builder.add_conditional_edges("evidence_check", after_gate, {"stop": END, "investigate": "investigator"})
    builder.add_conditional_edges("investigator", after_investigator,
                                  {"stop": END, "tool": "graph_tools", "check": "evidence_check"})
    builder.add_edge("graph_tools", "evidence_check")
    return builder.compile(checkpointer=checkpointer)


def run_investigation(alert, budget=None, *, similarity_source=None, risk_source=None, gate_config=None,
                      llm_client=None, graph_source=None, checkpointer=None, thread_id=None):
    config = {"recursion_limit": RECURSION_LIMIT}
    if checkpointer is not None:
        config["configurable"] = {"thread_id": thread_id or str(uuid4())}
    graph = build_graph(similarity_source, risk_source=risk_source, gate_config=gate_config,
                        llm_client=llm_client, graph_source=graph_source, checkpointer=checkpointer)
    return graph.invoke(initial_state(alert, budget), config)
