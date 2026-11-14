import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "rule-service"), str(ROOT / "data-pipeline")]
from evaluate import benchmark, metrics
from graph_contract import MemoryGraph
from make_demo import generate
from rules import RuleConfig, score


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rows, self.cases = generate()

    def case(self, scenario):
        return copy.deepcopy(next(c["graph"] for c in self.cases if c["scenario"] == scenario))

    def test_cycle_is_found_with_real_evidence(self):
        graph = self.case("cycle")
        result = score(graph)
        cycle = next(f for f in result["findings"] if f["ruleId"] == "CYCLE")
        self.assertEqual(cycle["facts"]["hops"], 3)
        self.assertIn(graph["targetTxId"], cycle["evidenceTxIds"])

    def test_reverse_temporal_path_is_not_money_flow(self):
        graph = self.case("cycle")
        a, b, _ = graph["edges"]
        a["timestamp"], b["timestamp"] = b["timestamp"], a["timestamp"]
        self.assertNotIn("CYCLE", [f["ruleId"] for f in score(graph)["findings"]])

    def test_cycle_hop_limit(self):
        self.assertFalse(score(self.case("cycle"), RuleConfig(max_cycle_hops=2))["flagged"])

    def test_fan_in_exact_native_currency_total(self):
        graph = self.case("fan_in")
        for edge, amount in zip(graph["edges"], ["0.1", "0.2", "0.3"]):
            edge["amountReceived"] = amount
        facts = score(graph)["findings"][0]["facts"]
        self.assertEqual(facts["totalsByCurrency"]["US Dollar"], "0.6")
        self.assertEqual(facts["counterpartyCount"], 3)

    def test_distinct_counterparties_not_transaction_count(self):
        graph = self.case("fan_in")
        for edge in graph["edges"]:
            edge["fromAccountKey"] = "same-sender"
        self.assertFalse(score(graph)["flagged"])

    def test_normal_and_fan_out(self):
        self.assertFalse(score(self.case("normal"))["flagged"])
        self.assertTrue(score(self.case("fan_out"))["flagged"])

    def test_future_data_rejected(self):
        graph = self.case("cycle")
        graph["edges"][0]["timestamp"] = "2027-01-01T00:00:00"
        with self.assertRaisesRegex(ValueError, "Future"):
            score(graph)

    def test_labels_rejected_even_nested(self):
        graph = self.case("cycle")
        graph["meta"]["hidden"] = {"isLaundering": 1}
        with self.assertRaisesRegex(ValueError, "Ground-truth"):
            score(graph)

    def test_duplicate_id_rejected(self):
        graph = self.case("cycle")
        graph["edges"].append(graph["edges"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            score(graph)

    def test_old_edges_excluded(self):
        self.assertFalse(score(self.case("fan_in"), RuleConfig(window_minutes=1))["flagged"])


class GraphAndEvaluationTest(unittest.TestCase):
    def test_whitelist_and_past_only(self):
        rows, _ = generate()
        graph = MemoryGraph(rows)
        target = rows[1]
        result = graph.subgraph(target["txId"])
        self.assertTrue(all(e["timestamp"] <= result["asOf"] for e in result["edges"]))
        self.assertNotIn("isLaundering", str(result))

    def test_graph_caps_and_determinism(self):
        rows, _ = generate()
        graph = MemoryGraph(rows)
        target = next(r for r in rows if "payroll_control-3" in r["txId"])
        result = graph.subgraph(target["txId"], max_neighbors=1, max_edges=2)
        self.assertTrue(result["meta"]["truncated"])
        self.assertLessEqual(len(result["edges"]), 2)
        self.assertEqual(result, graph.subgraph(target["txId"], max_neighbors=1, max_edges=2))

    def test_confusion_matrix(self):
        result = metrics([1, 1, 0, 0], [True, False, True, False])
        self.assertEqual((result["tp"], result["fp"], result["fn"], result["tn"]), (1, 1, 1, 1))
        self.assertEqual(result["recall"], .5)
        self.assertIsNone(metrics([0], [False])["recall"])

    def test_validation_selection_does_not_use_test_labels(self):
        _, cases = generate()
        baseline = benchmark(cases, .9)
        changed = copy.deepcopy(cases)
        for case in changed:
            if case["split"] == "test":
                case["expectedLabel"] = 1 - case["expectedLabel"]
        self.assertEqual(baseline["selected"], benchmark(changed, .9)["selected"])

    def test_target_not_met_does_not_tune_on_test(self):
        _, cases = generate()
        for case in cases:
            if case["split"] == "validation" and case["expectedLabel"]:
                case["graph"]["edges"] = [e for e in case["graph"]["edges"]
                                          if e["txId"] == case["graph"]["targetTxId"]]
        result = benchmark(cases, 1)
        self.assertEqual(result["status"], "target_not_met")
        self.assertIsNone(result["test"])

    def test_overlapping_time_split_rejected(self):
        _, cases = generate()
        cases[0]["split"] = "test"
        with self.assertRaisesRegex(ValueError, "later"):
            benchmark(cases, .9)


if __name__ == "__main__":
    unittest.main()
