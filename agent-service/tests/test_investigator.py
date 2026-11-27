"""Investigator loop with a scripted fake LLM: every PPT stop condition (no network)."""
import unittest

from support import HAS_LANGGRAPH

from evidence_check import checklist_outcome, loop_decide
from investigation_state import make_budget
from investigator import CHECKLIST, make_validator, response_schema
from llm_client import LLMOutputError
from synthetic_graphs import SCENARIOS

GATE = {"version": "test", "riskCut": 0.5, "signalHints": ["CYCLE_PATH", "PASS_THROUGH", "RAPID_BURST",
                                                           "REPEATED_AMOUNTS", "JUST_BELOW_THRESHOLD"]}


class FixedRisk:
    def __init__(self, value):
        self.value = value

    def score(self, alert, context=None):
        return {"status": "computed", "score": self.value}


def checklist(**statuses):
    return [{"itemId": item, "status": statuses.get(item, ("unconfirmed", []))[0],
             "factIds": statuses.get(item, ("unconfirmed", []))[1]} for item in CHECKLIST]


def tool(name, account, **arguments):
    return {"type": "call_tool", "tool": name, "accountKey": account, "direction": None, "hops": None,
            "windowHours": None, **arguments}


CONCLUDE = {"type": "conclude", "tool": None, "accountKey": None, "direction": None, "hops": None, "windowHours": None}


def step(action, conclusion=None, **statuses):
    return {"checklist": checklist(**statuses), "action": action, "conclusion": conclusion, "rationale": "근거를 확인했다."}


class FakeLLM:
    """Scripted outputs run through the caller's validator, like LLMClient (minus the retry)."""

    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        base = {"name": kwargs["name"], "model": "fake", "callsUsed": 1, "latencyMs": 1, "costUsd": 0.001,
                "usage": {"inputTokens": 1000, "cachedTokens": 0, "outputTokens": 100, "reasoningTokens": 0},
                "attempts": [{"responseId": f"resp_{len(self.calls)}"}]}
        if output == "transient_failure":
            return {**base, "status": "held", "reasonCode": "llm_http_503", "errorClass": "transient",
                    "lastErrorCode": "llm_http_503", "output": None, "responseId": None}
        try:
            kwargs["validate"](output)
        except LLMOutputError as error:
            code = "llm_output_rejected:" + str(error)
            return {**base, "status": "held", "reasonCode": code, "errorClass": "output", "lastErrorCode": code,
                    "output": None, "responseId": None}
        return {**base, "status": "ok", "reasonCode": "llm_ok", "errorClass": None, "lastErrorCode": None,
                "output": output, "responseId": f"resp_{len(self.calls)}"}


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validate = make_validator(["F1", "F2", "A1", "inv.T1.hints", "inv.T1.forwarding.passThroughRatio",
                                        "profile.receiver.flow.inCounterparties"],
                                       ["900:A"], final=False, alert_fact_ids=["A1"])

    def assert_rejects(self, output, code, validate=None):
        with self.assertRaises(LLMOutputError) as caught:
            (validate or self.validate)(output)
        self.assertEqual(str(caught.exception), code)

    def test_valid_tool_call_and_conclusion_pass(self):
        self.validate(step(tool("account_profile", "900:A")))
        self.validate(step(CONCLUDE, "no_suspicion", counterparty_role=("benign", ["F1"])))
        self.validate(step(CONCLUDE, "suspicious",
                           source_pattern=("suspicious", ["A1", "profile.receiver.flow.inCounterparties"])))

    def test_suspicion_needs_evidence_of_the_matching_kind(self):
        self.assert_rejects(step(CONCLUDE, "suspicious", counterparty_role=("suspicious", ["inv.T1.hints"])),
                            "suspicion_without_matching_evidence")
        self.validate(step(CONCLUDE, "suspicious", counterparty_role=(
            "suspicious", ["inv.T1.hints", "inv.T1.forwarding.passThroughRatio"])))

    def test_checked_status_must_cite_a_fact(self):
        self.assert_rejects(step(CONCLUDE, "undetermined", circularity=("benign", [])), "status_without_fact")
        self.assert_rejects(step(CONCLUDE, "undetermined", circularity=("benign", ["F9"])), "invalid_fact_reference")

    def test_rule_result_alone_cannot_confirm_suspicion(self):
        self.assert_rejects(step(CONCLUDE, "suspicious", source_pattern=("suspicious", ["A1"])),
                            "suspicion_from_alert_only")

    def test_conclusion_must_match_the_checklist(self):
        self.assert_rejects(step(CONCLUDE, "suspicious", circularity=("benign", ["F1"])),
                            "conclusion_contradicts_checklist")
        self.assert_rejects(step(CONCLUDE, "no_suspicion",
                                 onward_flow=("suspicious", ["inv.T1.forwarding.passThroughRatio"])),
                            "conclusion_contradicts_checklist")

    def test_checklist_must_cover_every_item_once(self):
        output = step(CONCLUDE, "undetermined")
        output["checklist"] = output["checklist"][1:]
        self.assert_rejects(output, "checklist_coverage")

    def test_tool_arguments_must_match_the_tool(self):
        self.assert_rejects(step(tool("account_profile", "900:A", hops=2)), "tool_arguments")
        self.assert_rejects(step(tool("trace_funds", "900:A", direction="forward", hops=3, windowHours=24)),
                            "tool_arguments")
        self.assert_rejects(step(tool("account_profile", "900:B")), "tool_arguments")

    def test_conclusion_and_tool_call_are_exclusive(self):
        self.assert_rejects(step(tool("account_profile", "900:A"), "undetermined"), "invalid_tool_call")
        self.assert_rejects(step(CONCLUDE, None), "invalid_conclusion")

    def test_final_step_cannot_call_a_tool(self):
        self.assert_rejects(step(tool("account_profile", "900:A")), "invalid_tool_call",
                            make_validator(["F1"], ["900:A"], final=True))


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict_everywhere(self):
        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(response_schema(["F1"], ["900:A"], final=False))

    def test_final_schema_only_allows_conclude(self):
        schema = response_schema(["F1"], ["900:A"], final=True)
        self.assertEqual(schema["properties"]["action"]["properties"]["type"]["enum"], ["conclude"])


RESOLVED_BENIGN = {"onward_flow": ("benign", ["F1"]), "source_pattern": ("benign", ["F2"]),
                   "circularity": ("not_applicable", ["F3"]), "counterparty_role": ("benign", ["F4"]),
                   "amount_pattern": ("not_applicable", ["F5"])}


class LoopDecisionTests(unittest.TestCase):
    def investigation(self, *decisions, refused=None):
        calls = [{"callId": f"T{n}", "refused": None} for n, d in enumerate(decisions, 1)
                 if d["action"]["type"] == "call_tool"]
        if refused and calls:
            calls[-1]["refused"] = refused
        return {"decisions": list(decisions), "toolCalls": calls}

    def decide(self, *decisions):
        return loop_decide(self.investigation(*decisions), make_budget(now=0), now=0)

    def test_checklist_outcomes_follow_the_ppt_stop_conditions(self):
        self.assertIsNone(checklist_outcome(checklist(onward_flow=("benign", ["F1"]))))
        self.assertEqual(checklist_outcome(checklist(**RESOLVED_BENIGN)), "close")
        two = {**RESOLVED_BENIGN, "onward_flow": ("suspicious", ["F1"]), "amount_pattern": ("suspicious", ["F5"])}
        self.assertEqual(checklist_outcome(checklist(**two)), "typology")
        one = {**RESOLVED_BENIGN, "onward_flow": ("suspicious", ["F1"])}
        self.assertEqual(checklist_outcome(checklist(**one)), "mixed")
        lone_benign = {item: ("not_applicable", ["F1"]) for item in CHECKLIST}
        lone_benign["source_pattern"] = ("benign", ["F2"])
        self.assertEqual(checklist_outcome(checklist(**lone_benign)), "mixed")

    def test_model_conclusion_is_not_trusted_without_a_complete_checklist(self):
        incomplete = self.decide(step(CONCLUDE, "no_suspicion", amount_pattern=("benign", ["F1"]),
                                      circularity=("benign", ["F2"])))
        self.assertEqual(incomplete[:2], ("human_review", "checklist_incomplete"))
        mixed = self.decide(step(CONCLUDE, "suspicious", **{**RESOLVED_BENIGN, "onward_flow": ("suspicious", ["F1"])}))
        self.assertEqual(mixed[:2], ("human_review", "checklist_mixed"))

    def test_a_complete_checklist_never_cuts_off_an_investigator_that_wants_to_look_further(self):
        continuing = self.decide(step(tool("account_profile", "900:A"), **RESOLVED_BENIGN))
        self.assertEqual(continuing[:2], ("investigate", "continue_investigation"))

    def test_closure_needs_the_model_conclusion_and_at_least_one_tool_call(self):
        closing = self.decide(step(tool("account_profile", "900:A")), step(CONCLUDE, "no_suspicion", **RESOLVED_BENIGN))
        self.assertEqual(closing[:2], ("close", "no_suspicion"))
        self.assertEqual(closing[2]["closureBasisFactIds"], ["F1", "F2", "F4"])
        unsure = self.decide(step(tool("account_profile", "900:A")), step(CONCLUDE, "undetermined", **RESOLVED_BENIGN))
        self.assertEqual(unsure[:2], ("human_review", "closure_not_confirmed"))
        hasty = self.decide(step(CONCLUDE, "no_suspicion", **RESOLVED_BENIGN))
        self.assertEqual(hasty[:2], ("human_review", "closure_without_investigation"))

    def test_unconfirmed_items_not_shrinking_over_two_tool_steps_is_no_progress(self):
        calls = [step(tool("account_profile", f"900:{name}")) for name in "ABC"]
        self.assertEqual(self.decide(*calls)[:2], ("human_review", "unconfirmed_not_decreasing"))

    def test_deadline_stops_the_loop(self):
        budget = make_budget(now=0)
        decision = loop_decide(self.investigation(step(tool("account_profile", "900:A"))), budget,
                               now=budget["deadlineAt"])
        self.assertEqual(decision[:2], ("human_review", "deadline_exceeded"))


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed")
class InvestigationLoopTests(unittest.TestCase):
    def run_loop(self, name, risk, llm, budget=None):
        from investigation_graph import run_investigation
        return run_investigation(SCENARIOS[name](), budget, risk_source=FixedRisk(risk), gate_config=GATE,
                                 llm_client=llm)

    def nodes(self, result):
        return [row["node"] for row in result["trace"]]

    def test_confirmed_suspicion_hands_over_to_typology(self):
        suspicious = {"source_pattern": ("suspicious", ["inv.T1.hop1Counterparties"]),
                      "amount_pattern": ("suspicious", ["profile.receiver.amounts.justBelowThresholdCount"]),
                      "onward_flow": ("benign", ["profile.receiver.flow.outCount"]),
                      "circularity": ("not_applicable", ["inv.T1.hop1Transactions"]),
                      "counterparty_role": ("not_applicable", ["profile.sender.flow.outCount"])}
        llm = FakeLLM(step(tool("trace_funds", "900:FANIN-MULE", direction="backward", hops=2, windowHours=168)),
                      step(CONCLUDE, "suspicious", **suspicious))
        result = self.run_loop("fan_in_mule", 0.2, llm)
        self.assertEqual((result["status"], result["route"]), ("running", "typology"))
        self.assertEqual(self.nodes(result), ["alert_context", "basic_profile", "evidence_check", "investigator",
                                              "graph_tools", "evidence_check", "investigator", "evidence_check"])
        self.assertEqual(result["gate"]["loop"]["suspiciousItems"], ["source_pattern", "amount_pattern"])
        self.assertEqual((result["budget"]["llmCalls"], result["budget"]["iterations"]), (2, 1))
        second = llm.calls[1]
        self.assertIn("inv.T1.hop1Counterparties", {row["factId"] for row in second["payload"]["facts"]})
        self.assertEqual(second["payload"]["toolHistory"][0]["tool"], "trace_funds")

    def test_normal_explanations_close_the_alert_with_their_facts(self):
        benign = {"onward_flow": ("benign", ["inv.T1.flow.outCount"]),
                  "source_pattern": ("benign", ["inv.T1.flow.topInCounterpartyShare"]),
                  "circularity": ("not_applicable", ["inv.T1.flow.outCount"]),
                  "counterparty_role": ("benign", ["inv.T1.hints"]),
                  "amount_pattern": ("benign", ["inv.T1.amounts.justBelowThresholdCount"])}
        llm = FakeLLM(step(tool("account_profile", "900:SHOP-STORE")), step(CONCLUDE, "no_suspicion", **benign))
        result = self.run_loop("normal_merchant", 0.9, llm)
        self.assertEqual((result["status"], result["reasonCodes"]), ("closed", ["no_suspicion"]))
        self.assertEqual(result["gate"]["loop"]["closureBasisFactIds"],
                         ["inv.T1.amounts.justBelowThresholdCount", "inv.T1.flow.outCount",
                          "inv.T1.flow.topInCounterpartyShare", "inv.T1.hints"])

    def test_repeated_tool_call_is_no_progress(self):
        same = tool("account_profile", "900:SHOP-STORE")
        result = self.run_loop("normal_merchant", 0.9, FakeLLM(step(same), step(same)))
        self.assertEqual((result["status"], result["hold"]["category"], result["hold"]["reasonCode"]),
                         ("held", "no_progress", "repeated_tool_call"))
        self.assertEqual(result["investigation"]["toolCalls"][-1]["refused"], "repeated_call")

    def test_after_the_last_tool_call_the_model_may_only_conclude(self):
        llm = FakeLLM(step(tool("account_profile", "900:SHOP-STORE")), step(CONCLUDE, "undetermined"))
        result = self.run_loop("normal_merchant", 0.9, llm, budget={"maxIterations": 1})
        self.assertEqual(llm.calls[1]["schema"]["properties"]["action"]["properties"]["type"]["enum"], ["conclude"])
        self.assertEqual((result["hold"]["category"], result["hold"]["reasonCode"]),
                         ("evidence_insufficient", "checklist_incomplete"))

    def test_model_failure_and_invalid_citation_are_model_failures(self):
        failed = self.run_loop("normal_merchant", 0.9, FakeLLM("transient_failure"))
        self.assertEqual((failed["status"], failed["hold"]["category"]), ("held", "model_failure"))
        bad = self.run_loop("normal_merchant", 0.9,
                            FakeLLM(step(CONCLUDE, "no_suspicion", amount_pattern=("benign", ["inv.T9.unknown"]))))
        self.assertEqual(bad["hold"]["reasonCode"], "llm_output_rejected:invalid_fact_reference")

    def test_reserved_calls_stop_the_investigation_before_any_call(self):
        llm = FakeLLM()
        result = self.run_loop("normal_merchant", 0.9, llm, budget={"maxLlmCalls": 2})
        self.assertEqual((result["hold"]["category"], llm.calls), ("budget_exceeded", []))

    def test_payload_never_carries_labels_and_lists_allowed_accounts(self):
        llm = FakeLLM(step(CONCLUDE, "undetermined"))
        self.run_loop("fan_in_mule", 0.2, llm)
        payload = llm.calls[0]["payload"]
        self.assertNotIn("isLaundering", str(payload))
        self.assertEqual(payload["accounts"][0]["role"], "sender")
        self.assertEqual(payload["remainingToolCalls"], 3)


if __name__ == "__main__":
    unittest.main()
