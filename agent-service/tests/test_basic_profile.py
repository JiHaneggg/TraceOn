"""Node 2 · basic_profile on synthetic alerts (pure Python, no LangGraph)."""
from copy import deepcopy
import unittest

from support import load_alert

from alert_context import alert_context
from basic_profile import PROFILE_CONFIG, make_basic_profile
from investigation_state import initial_state
from synthetic_graphs import SCENARIOS, dev_reference_set

REFERENCE = dev_reference_set()


def profiled(alert, reference_set=None, config=PROFILE_CONFIG):
    state = initial_state(alert)
    first = alert_context(state)
    state = {**state, **first}
    second = make_basic_profile(reference_set, config)(state)
    return first, second


def account(update, alias):
    return next(row for row in update["profile"]["accounts"] if row["alias"] == alias)


def hints(update):
    return [(row["kind"], row["accountAlias"]) for row in update["profile"]["hints"]]


class BasicProfileTests(unittest.TestCase):
    def test_mule_receiver_shows_collection_pass_through_and_structuring(self):
        _, update = profiled(SCENARIOS["fan_in_mule"]())
        mule = account(update, "receiver")
        self.assertEqual((mule["flow"]["inCount"], mule["flow"]["inCounterparties"]), (12, 12))
        self.assertIn("center:F1", mule["roles"])
        self.assertEqual(mule["forwarding"]["passThrough"]["ratio"], "0.9397")
        self.assertEqual(mule["forwarding"]["fastestForwardSeconds"], 1800)
        self.assertEqual(mule["amounts"]["justBelowThresholdCount"], 12)
        self.assertEqual(mule["amounts"]["largestSimilarAmountGroup"], 12)
        self.assertEqual(hints(update), [("FAN_IN_SHAPE", "receiver"), ("PASS_THROUGH", "receiver"),
                                         ("REPEATED_AMOUNTS", "receiver"), ("JUST_BELOW_THRESHOLD", "receiver")])

    def test_cycle_hint_comes_from_the_rule_and_both_ends_pass_money_through(self):
        _, update = profiled(SCENARIOS["cycle_layering"]())
        self.assertEqual(update["profile"]["hints"][0],
                         {"hintId": "H1", "kind": "CYCLE_PATH", "accountAlias": None, "factIds": ["alert.F1.hops"]})
        self.assertIn(("PASS_THROUGH", "sender"), hints(update))
        self.assertIn(("PASS_THROUGH", "receiver"), hints(update))

    def test_fan_out_source_splits_a_deposit_quickly(self):
        _, update = profiled(SCENARIOS["fan_out_burst"]())
        source = account(update, "sender")
        self.assertEqual(source["timing"]["maxTxInBurstWindow"], 5)
        self.assertEqual(source["forwarding"]["fastestForwardSeconds"], 900)
        self.assertEqual(hints(update), [("FAN_OUT_SHAPE", "sender"), ("PASS_THROUGH", "sender"),
                                         ("RAPID_BURST", "sender"), ("REPEATED_AMOUNTS", "sender"),
                                         ("JUST_BELOW_THRESHOLD", "sender")])

    def test_normal_look_alikes_keep_the_shape_but_not_the_laundering_signals(self):
        _, merchant = profiled(SCENARIOS["normal_merchant"]())
        _, payroll = profiled(SCENARIOS["normal_payroll"]())
        self.assertEqual(hints(merchant), [("FAN_IN_SHAPE", "receiver")])
        self.assertEqual(hints(payroll), [("FAN_OUT_SHAPE", "sender"), ("RAPID_BURST", "sender")])
        for update, alias in ((merchant, "receiver"), (payroll, "sender")):
            self.assertIsNone(account(update, alias)["forwarding"]["passThrough"])
            self.assertFalse(account(update, alias)["forwarding"]["quickForward"])

    def test_currencies_are_never_mixed(self):
        _, update = profiled(SCENARIOS["cycle_layering"]())
        receiver = account(update, "receiver")["flow"]
        self.assertEqual(receiver["outTotals"], {"Euro": "1200.00", "US Dollar": "50000.00"})
        self.assertEqual(set(receiver["inOutRatio"]), {"US Dollar"})

    def test_every_fact_and_hint_is_traceable_to_graph_transactions(self):
        for name, make in SCENARIOS.items():
            with self.subTest(name):
                alert = make()
                first, second = profiled(alert)
                tx_ids = {edge["txId"] for edge in alert["graph"]["edges"]}
                rows = first["evidence"] + second["evidence"]
                ids = [row["factId"] for row in rows]
                self.assertEqual(len(ids), len(set(ids)))
                for row in rows:
                    self.assertTrue(row["evidenceTxIds"])
                    self.assertLessEqual(set(row["evidenceTxIds"]), tx_ids)
                for hint in second["profile"]["hints"]:
                    self.assertLessEqual(set(hint["factIds"]), set(ids))
                self.assertTrue(all(row["source"] == "basic_profile" for row in second["evidence"]))

    def test_tiny_repository_fixtures_still_profile(self):
        _, update = profiled(load_alert("fan_in"))
        self.assertEqual(account(update, "receiver")["flow"]["inCounterparties"], 3)
        self.assertEqual(update["trace"][0]["llmCalls"], 0)

    def test_hint_cut_offs_are_configuration_not_code(self):
        config = deepcopy(PROFILE_CONFIG)
        config["hints"]["justBelowCount"] = 50
        _, update = profiled(SCENARIOS["fan_in_mule"](), config=config)
        self.assertNotIn(("JUST_BELOW_THRESHOLD", "receiver"), hints(update))
        self.assertNotEqual(update["profile"]["configSha256"], profiled(SCENARIOS["fan_in_mule"]())[1]["profile"]["configSha256"])


class SimilarityTests(unittest.TestCase):
    def test_without_similarity_source_similarity_is_unavailable_not_guessed(self):
        _, update = profiled(SCENARIOS["fan_in_mule"]())
        self.assertEqual(update["profile"]["similarity"],
                         {"status": "unavailable", "reason": "similarity_source_not_loaded"})

    def test_laundering_scenarios_route_closer_than_normal_look_alikes(self):
        score = {name: profiled(make(), REFERENCE)[1]["profile"]["similarity"]["score"]
                 for name, make in SCENARIOS.items()}
        self.assertGreater(score["fan_in_mule"], score["normal_merchant"])
        self.assertGreater(score["fan_out_burst"], score["normal_payroll"])

    def test_accounts_with_too_few_transactions_are_skipped(self):
        _, update = profiled(SCENARIOS["fan_in_mule"](), REFERENCE)
        routing = update["profile"]["similarity"]
        sender = next(row for row in routing["accounts"] if row["alias"] == "sender")
        self.assertEqual(sender, {"alias": "sender", "status": "skipped", "reason": "too_few_transactions"})
        self.assertEqual((routing["status"], routing["alias"]), ("computed", "receiver"))

    def test_similarity_never_becomes_citable_evidence(self):
        _, update = profiled(SCENARIOS["fan_in_mule"](), REFERENCE)
        self.assertFalse([row for row in update["evidence"] if "similar" in row["factId"].lower()
                          and "Amount" not in row["factId"]])

    def test_modified_reference_set_is_refused(self):
        tampered = deepcopy(REFERENCE)
        tampered["profiles"][0]["vector"][0] = 0.0
        with self.assertRaises(ValueError):
            profiled(SCENARIOS["fan_in_mule"](), tampered)


if __name__ == "__main__":
    unittest.main()
