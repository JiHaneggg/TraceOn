"""PPT 에이전트 조사 계층 as a LangGraph StateGraph.

    alert_context -> basic_profile -> evidence_check --investigate--> investigator
                                        ^   |   ^                     |        |
                                        |   |   |<---- conclude ------+        | call_tool
                                        |   |   |<----------- graph_tools <----+
                                        |   +--fast_track / typology--> typology -> str_writer -> verifier
                                        |                                   (report_judge) -> human_review

Edges between nodes are decided by code from the state; only LLM nodes choose
inside themselves. The recursion limit is the last-resort stop (PPT 종료 조건 5).
Adapters decide how far a run goes:
- no llm_client: the gate result only (offline evaluation).
- llm_client only: the investigation loop; a typology handoff ends the run with
  the route recorded (development runs without the RAG index).
- llm_client and call_tool: typology, draft, verification, optional Judge and
  human review.
Held runs go to human_review whenever a model is attached.
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from alert_context import alert_context
from basic_profile import make_basic_profile
from evidence_check import make_evidence_check
from investigation_state import InvestigationState, initial_state
from investigation_steps import human_review, make_report_judge, make_typology, make_verifier, make_writer
from investigator import make_graph_tools, make_investigator

RECURSION_LIMIT = 25


def stop_unless_running(state):
    return "continue" if state["status"] == "running" else "stop"


def after_check(state):
    """Gate and loop decisions: closed and rejected runs stop, held runs go to a person."""
    if state["status"] == "held":
        return "human_review"
    if state["status"] != "running":
        return "stop"
    return state["route"]   # investigate | fast_track | typology


def after_investigator(state):
    if state["status"] == "held":
        return "human_review"
    if state["status"] != "running":
        return "stop"
    return "tool" if state["investigation"]["pendingAction"] else "check"


def build_graph(similarity_source=None, *, risk_source=None, gate_config=None, llm_client=None,
                graph_source=None, call_tool=None, judge_enabled=False, checkpointer=None):
    """Routing signals, the LLM client, the transaction store and the RAG tools are bound here.

    similarity_source: EmbeddingSimilarity or a feature reference set (recorded only).
    risk_source: the team's HybridRiskScore (rule+ML), the gate's main routing signal.
    gate_config: evidence_check settings; defaults to the tuned gate_config.json.
    llm_client: LLMClient for the investigator, typology and Judge; None runs the gate only.
    graph_source: store for expand_account (IndexSource); defaults to subgraph only.
    call_tool: rag-service AgentTools.call for typology, draft, verification and Judge.
    judge_enabled: run the report Judge after a verified draft.
    """
    if call_tool is not None and llm_client is None or judge_enabled and call_tool is None:
        raise ValueError("Typology and the Judge need both the model and RAG tool adapters")
    builder = StateGraph(InvestigationState)
    builder.add_node("alert_context", alert_context)
    builder.add_node("basic_profile", make_basic_profile(similarity_source, risk_source=risk_source))
    builder.add_node("evidence_check", make_evidence_check(gate_config))
    builder.add_edge(START, "alert_context")
    builder.add_conditional_edges("alert_context", stop_unless_running, {"stop": END, "continue": "basic_profile"})
    builder.add_edge("basic_profile", "evidence_check")
    if llm_client is None:
        builder.add_edge("evidence_check", END)
        return builder.compile(checkpointer=checkpointer)

    builder.add_node("investigator", make_investigator(llm_client))
    builder.add_node("graph_tools", make_graph_tools(graph_source))
    builder.add_node("human_review", human_review)
    classify = "typology" if call_tool is not None else END
    builder.add_conditional_edges("evidence_check", after_check, {
        "stop": END, "human_review": "human_review", "investigate": "investigator",
        "fast_track": classify, "typology": classify,
    })
    builder.add_conditional_edges("investigator", after_investigator,
                                  {"human_review": "human_review", "stop": END, "tool": "graph_tools",
                                   "check": "evidence_check"})
    builder.add_edge("graph_tools", "evidence_check")
    if call_tool is not None:
        builder.add_node("typology", make_typology(llm_client, call_tool))
        builder.add_node("str_writer", make_writer(call_tool))
        builder.add_node("verifier", make_verifier(call_tool))
        builder.add_conditional_edges("typology", stop_unless_running, {"continue": "str_writer", "stop": "human_review"})
        builder.add_conditional_edges("str_writer", stop_unless_running, {"continue": "verifier", "stop": "human_review"})
        if judge_enabled:
            builder.add_node("report_judge", make_report_judge(llm_client, call_tool))
            builder.add_conditional_edges("verifier", lambda state: state["status"] == "completed",
                                          {True: "report_judge", False: "human_review"})
            builder.add_edge("report_judge", "human_review")
        else:
            builder.add_edge("verifier", "human_review")
    builder.add_edge("human_review", END)
    return builder.compile(checkpointer=checkpointer)


def run_investigation(alert, budget=None, *, similarity_source=None, risk_source=None, gate_config=None,
                      llm_client=None, graph_source=None, call_tool=None, judge_enabled=False,
                      checkpointer=None, thread_id=None):
    config = {"recursion_limit": RECURSION_LIMIT}
    if checkpointer is not None:
        config["configurable"] = {"thread_id": thread_id or str(uuid4())}
    graph = build_graph(similarity_source, risk_source=risk_source, gate_config=gate_config,
                        llm_client=llm_client, graph_source=graph_source, call_tool=call_tool,
                        judge_enabled=judge_enabled, checkpointer=checkpointer)
    return graph.invoke(initial_state(alert, budget), config)
