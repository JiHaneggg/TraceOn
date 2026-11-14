import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "data-pipeline")]
from evaluation_index import SourceIndex, build
from graph_contract import MemoryGraph
from make_demo import generate
from prepare_evaluation import allocations, select_targets
from preprocess import TX_FIELDS


class EvaluationPreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rows, _ = generate()
        # Deliberately create a pattern family crossing the chronological cutoff.
        for row in self.rows:
            if "fan_in" in row["txId"]:
                row["patternId"] = "P-shared"
                row["patternType"] = "FAN-IN"
        self.source = self.root / "transactions.csv"
        with self.source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=TX_FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)
        self.path = self.root / "source.sqlite3"
        build(self.source, self.path)
        self.index = SourceIndex(self.path)

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_public_graph_matches_full_memory_and_has_no_labels(self):
        target = self.rows[2]["txId"]
        actual = self.index.subgraph(target)
        expected = MemoryGraph(self.rows).subgraph(target)
        self.assertEqual(actual["edges"], expected["edges"])
        self.assertNotIn("isLaundering", str(actual))
        self.assertNotIn("patternType", str(actual))

    def test_index_reuse_checks_source_hash(self):
        self.assertEqual(build(self.source, self.path)["rows"], "30")
        with self.source.open("a") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "another source"):
            build(self.source, self.path)

    def test_holdout_excludes_old_targets_and_crossing_pattern_family(self):
        excluded = next(row["txId"] for row in self.rows if "test-normal-1" in row["txId"])
        rows, population, sizes = select_targets(self.index, "2022-09-10", "2022-09-20",
            {excluded}, set(), holdout=True, positives=3, normals=3)
        self.assertNotIn(excluded, [row["txId"] for row in rows])
        self.assertNotIn("P-shared", [row["patternId"] for row in rows])
        self.assertEqual(population["positive:UNMAPPED"], 6)
        for row in rows:
            self.assertEqual(row["samplingWeight"], population[row["stratum"]] / sizes[row["stratum"]])

    def test_allocations_respect_rare_strata_and_capacity(self):
        allocated = allocations({"rare": 1, "common": 100, "medium": 9}, 20)
        self.assertEqual(sum(allocated.values()), 20)
        self.assertEqual(allocated["rare"], 1)
        self.assertLessEqual(allocated["medium"], 9)
        with self.assertRaises(ValueError):
            allocations({"a": 2, "b": 2}, 1)


if __name__ == "__main__":
    unittest.main()
