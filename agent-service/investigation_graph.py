"""PPT 에이전트 조사 계층 as a LangGraph StateGraph.

Edges between nodes are decided by code from the state. Without the optional
model and RAG adapters, return the gate result for offline evaluation.
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from alert_context import alert_context
from basic_profile import make_basic_profile
from evidence_check import make_evidence_check
from investigation_state import InvestigationState, initial_state
from investigation_steps import human_review, make_investigator, make_report_judge, make_typology, make_verifier, make_writer

RECURSION_LIMIT = 25


def stop_unless_running(state):
    return "continue" if state["status"] == "running" else "stop"


def build_graph(similarity_source=None, *, risk_source=None, gate_config=None, checkpointer=None,
                llm_client=None, call_tool=None, judge_enabled=False):
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
    if (llm_client is None) != (call_tool is None) or (judge_enabled and llm_client is None):
        raise ValueError("Use both the model and RAG tool adapters for full investigation")
    if llm_client is None:
        builder.add_edge("evidence_check", END)
    else:
        builder.add_node("investigator", make_investigator(llm_client))
        builder.add_node("typology", make_typology(llm_client, call_tool))
        builder.add_node("str_writer", make_writer(call_tool))
        builder.add_node("verifier", make_verifier(call_tool))
        builder.add_node("human_review", human_review)
        builder.add_conditional_edges("evidence_check", lambda state: state["route"], {
            "close": END, "human_review": "human_review", "investigate": "investigator", "fast_track": "typology",
        })
        builder.add_conditional_edges("investigator", lambda state: state["route"], {
            "investigate": "investigator", "classify": "typology", "human_review": "human_review",
        })
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
                      checkpointer=None, thread_id=None, llm_client=None, call_tool=None, judge_enabled=False):
    config = {"recursion_limit": RECURSION_LIMIT}
    if checkpointer is not None:
        config["configurable"] = {"thread_id": thread_id or str(uuid4())}
    graph = build_graph(similarity_source, risk_source=risk_source, gate_config=gate_config,
                        checkpointer=checkpointer, llm_client=llm_client, call_tool=call_tool, judge_enabled=judge_enabled)
    return graph.invoke(initial_state(alert, budget), config)
