from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recipient_hybrid import FEATURE_SETS, features, grouped_folds, model_score, train, vector


class HybridFeatureTests(unittest.TestCase):
    def test_point_in_time_counts_and_missing_history_encoding(self):
        def edge(id_, time, sender, receiver):
            return {"txId": id_, "timestamp": time, "fromAccountKey": sender, "toAccountKey": receiver}

        target = edge("target", "2022-09-09T12:00:00", "sender", "recipient")
        graph = {"asOf": target["timestamp"], "edges": [
            target,
            edge("previous", "2022-09-08T12:00:00", "other", "recipient"),
            edge("send", "2022-09-09T11:59:00", "recipient", "other"),
            edge("tie", target["timestamp"], "tie_sender", "recipient"),
            edge("future", "2022-09-09T12:01:00", "future_sender", "recipient"),
            edge("self", "2022-09-09T11:59:00", "recipient", "recipient"),
        ]}
        missing = features(graph, target, {"status": "insufficient_activity"})
        observed = features(graph, target, {"status": "comparable", "senderDelta": 0})
        self.assertEqual((missing["inboundSenders24h"], missing["inboundTransfers24h"],
                          missing["outboundTransfers24h"]), (2, 2, 1))
        self.assertIsNone(missing["senderDelta"])
        names = FEATURE_SETS["graphWithAvailabilityAndDelta"]
        self.assertEqual(vector(missing, names)[-2:], [0.0, 0.0])
        self.assertEqual(vector(observed, names)[-2:], [1.0, 0.0])

    def test_grouping_keeps_transitively_linked_families_and_accounts_together(self):
        labels = [1, 1, 0, 0, 0, 1, 0, 1, 0]
        families = ["family", "family", "", "", "", "", "", "", ""]
        cases = [{"expectedLabel": label, "patternFamily": family} for label, family in zip(labels, families)]
        groups = grouped_folds(cases, ["a", "b", "b", "c", "d", "e", "f", "g", "h"], count=2)
        self.assertEqual(sorted(index for group in groups for index in group), list(range(len(cases))))
        self.assertTrue(any({0, 1, 2} <= set(group) for group in groups))
        self.assertTrue(all({labels[i] for i in group} == {0, 1} for group in groups))

    def test_model_learns_order_from_weighted_validation_only(self):
        cases = [{"expectedLabel": label, "samplingWeight": 1.0} for label in [0, 0, 1, 1]]
        rows = [{"historyAvailable": True, "senderDelta": delta} for delta in [-2, -1, 1, 2]]
        fitted = train(rows, cases, ["historyAvailable", "senderDelta"])
        self.assertLess(model_score(fitted, rows[0]), model_score(fitted, rows[-1]))
