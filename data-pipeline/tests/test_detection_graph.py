from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / name) for name in ("data-pipeline", "graph-viewer", "rule-service")]
from detection_graph import agent_input, describe, selective_expand
from graph_contract import MemoryGraph
from rules import score


def edge(tx_id, sender, receiver, minute):
    return {
        "txId": tx_id, "timestamp": f"2022-09-01T09:{minute:02d}:00",
        "fromAccountKey": sender, "toAccountKey": receiver, "amountPaid": "10.00",
        "amountReceived": "10.00", "paymentCurrency": "US Dollar",
        "receivingCurrency": "US Dollar", "paymentFormat": "ACH",
    }


class DetectionGraphTest(unittest.TestCase):
    def test_crowded_recipient_can_still_fall_outside_two_core(self):
        graph = MemoryGraph([edge("T1", "A", "D", 1), edge("T2", "B", "D", 2),
                             edge("T3", "C", "D", 3)]).subgraph("T3")
        found = describe(graph, score(graph))
        self.assertEqual(found["patterns"]["FAN_IN"], ["T1", "T2", "T3"])
        self.assertFalse(found["targetInTwoCore"])

    def test_scatter_gather_requires_preceding_source_transfers(self):
        for first_minute, expected in ((1, True), (3, False)):
            rows = [edge("T1", "A", "B", first_minute), edge("T2", "A", "C", 2),
                    edge("T3", "B", "D", 3), edge("T4", "C", "D", 4)]
            graph = MemoryGraph(rows).subgraph("T4")
            found = describe(graph, score(graph))
            with self.subTest(first_minute=first_minute):
                self.assertEqual("SCATTER_GATHER" in found["patterns"], expected)
                if expected:
                    self.assertEqual(found["patterns"]["SCATTER_GATHER"], ["T1", "T2", "T3", "T4"])
                payload = agent_input(graph, found)
                self.assertEqual({row["txId"] for row in payload["transactions"]}, {"T1", "T2", "T3", "T4"})
                self.assertNotIn("ruleScore", payload)
                self.assertNotIn("expectedLabel", payload)

    def test_gather_scatter_uses_distinct_ordered_counterparties(self):
        graph = MemoryGraph([edge("T1", "A", "C", 1), edge("T2", "B", "C", 2),
                             edge("T3", "C", "D", 3), edge("T4", "C", "E", 4)]).subgraph("T4")
        found = describe(graph, score(graph))
        self.assertEqual(found["patterns"]["GATHER_SCATTER"], ["T1", "T2", "T3", "T4"])

    def test_selective_expansion_recovers_older_rows_with_target_scope_fixed(self):
        source = MemoryGraph([edge("T1", "E", "A", 1), edge("T2", "C", "A", 2), edge("T3", "A", "D", 3),
                              edge("T4", "A", "F", 4)])
        initial = source.subgraph("T3", max_neighbors=2)
        expanded = selective_expand(initial, source.lookup)
        self.assertEqual([row["txId"] for row in initial["edges"]], ["T2", "T3"])
        self.assertEqual([row["txId"] for row in expanded["edges"]], ["T1", "T2", "T3"])
        self.assertEqual(expanded["asOf"], initial["asOf"])
        self.assertEqual(expanded["meta"]["evaluationAdditionalPages"], 1)
