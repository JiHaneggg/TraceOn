"""Node 1 · alert_context: pure-Python checks that need no LangGraph install."""
import unittest

from support import load_alert

from alert_context import alert_context
from investigation_state import BUDGET_LIMITS, initial_state, make_budget


def run(alert):
    state = initial_state(alert)
    return state, alert_context(state)


def facts(update):
    return {row["factId"]: row for row in update["evidence"]}


class AlertContextTests(unittest.TestCase):
    def test_cycle_alert_reads_rule_reason_score_and_target(self):
        alert = load_alert("cycle")
        _, update = run(alert)
        context = update["alertContext"]
        target = next(e for e in alert["graph"]["edges"] if e["txId"] == alert["graph"]["targetTxId"])
        self.assertNotIn("status", update)  # still running; nothing rejected
        self.assertEqual(context["ruleIds"], ["CYCLE"])
        self.assertEqual((context["ruleScore"], context["flagged"]), (90, True))
        self.assertEqual(context["target"]["amountPaid"], target["amountPaid"])
        self.assertEqual(facts(update)["alert.target.amountPaid"]["value"], target["amountPaid"])
        self.assertEqual(facts(update)["alert.F1.hops"]["value"], 3)
        self.assertEqual(update["trace"][0]["outcome"], "ok")
        self.assertEqual(update["trace"][0]["llmCalls"], 0)

    def test_fan_in_totals_become_citable_facts(self):
        _, update = run(load_alert("fan_in"))
        rows = facts(update)
        self.assertEqual(update["alertContext"]["ruleIds"], ["FAN_IN"])
        self.assertEqual(rows["alert.F1.counterpartyCount"]["value"], 3)
        self.assertEqual(rows["alert.F1.totalsByCurrency.US Dollar"]["value"], "3003.30")

    def test_every_fact_has_unique_id_and_points_to_real_transactions(self):
        for name in ("cycle", "fan_in"):
            alert = load_alert(name)
            _, update = run(alert)
            tx_ids = {edge["txId"] for edge in alert["graph"]["edges"]}
            ids = [row["factId"] for row in update["evidence"]]
            self.assertEqual(len(ids), len(set(ids)))
            for row in update["evidence"]:
                self.assertTrue(row["evidenceTxIds"])
                self.assertLessEqual(set(row["evidenceTxIds"]), tx_ids)
                self.assertEqual(row["source"], "alert_context")

    def test_forged_rule_score_is_rejected_without_context(self):
        alert = load_alert("cycle")
        alert["ruleResult"]["ruleScore"] = 99
        _, update = run(alert)
        self.assertEqual((update["status"], update["reasonCodes"]), ("rejected", ["rule_result_mismatch"]))
        self.assertNotIn("alertContext", update)
        self.assertNotIn("evidence", update)

    def test_dropped_evidence_transaction_is_rejected(self):
        alert = load_alert("fan_in")
        alert["ruleResult"]["findings"][0]["evidenceTxIds"].pop()
        _, update = run(alert)
        self.assertEqual(update["reasonCodes"], ["rule_result_mismatch"])

    def test_ground_truth_label_never_reaches_the_agent(self):
        alert = load_alert("cycle")
        alert["graph"]["edges"][0]["isLaundering"] = 1
        _, update = run(alert)
        self.assertEqual((update["status"], update["reasonCodes"]), ("rejected", ["ground_truth_in_input"]))

    def test_malformed_alert_is_rejected_with_detail(self):
        missing_target = load_alert("cycle")
        missing_target["graph"]["targetTxId"] = "DOES-NOT-EXIST"
        missing_config = load_alert("cycle")
        del missing_config["ruleResult"]["config"]
        for alert in (missing_target, missing_config):
            _, update = run(alert)
            self.assertEqual(update["reasonCodes"], ["invalid_alert_input"])
            self.assertTrue(update["trace"][0]["detail"])

    def test_identity_is_reproducible_and_query_is_part_of_snapshot(self):
        _, first = run(load_alert("cycle"))
        _, second = run(load_alert("cycle"))
        other_query = load_alert("cycle")
        other_query["query"] = "다른 질문"
        _, third = run(other_query)
        self.assertEqual(first["identity"], second["identity"])
        self.assertNotEqual(first["identity"]["snapshotId"], third["identity"]["snapshotId"])
        self.assertEqual(first["identity"]["graphSha256"], third["identity"]["graphSha256"])

    def test_hops_already_explored_are_recorded_in_budget(self):
        state, update = run(load_alert("cycle"))
        self.assertEqual(update["budget"]["hopsExplored"], 2)
        self.assertEqual(update["budget"]["maxLlmCalls"], state["budget"]["maxLlmCalls"])


class InitialStateTests(unittest.TestCase):
    def test_input_is_copied_so_caller_mutation_cannot_change_snapshot(self):
        alert = load_alert("cycle")
        state = initial_state(alert)
        alert["ruleResult"]["ruleScore"] = 0
        self.assertEqual(state["alert"]["ruleResult"]["ruleScore"], 90)

    def test_unknown_alert_fields_are_refused(self):
        alert = load_alert("cycle")
        alert["modelAnswer"] = "suspicious"
        with self.assertRaises(ValueError):
            initial_state(alert)

    def test_budget_can_be_tightened_but_never_raised(self):
        self.assertEqual(make_budget({"maxLlmCalls": 4})["maxLlmCalls"], 4)
        self.assertEqual(make_budget()["maxLlmCalls"], BUDGET_LIMITS["maxLlmCalls"])
        for overrides in ({"maxLlmCalls": 9}, {"maxHops": 5}, {"maxIterations": -1},
                          {"maxLlmCalls": True}, {"tokens": 1}):
            with self.assertRaises(ValueError):
                make_budget(overrides)


if __name__ == "__main__":
    unittest.main()
