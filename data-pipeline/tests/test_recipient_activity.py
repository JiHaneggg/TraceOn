from contextlib import closing
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluation_index import FIELDS, SourceIndex
from recipient_activity import RecipientActivity


def transfer(id_, day, sender="bank:1", recipient="bank:2", hour="00:00:00"):
    return (id_, f"2022-09-{day:02}T{hour}", sender, recipient, "1.00", "Euro",
            "1.00", "Euro", "ACH", "0", "", "")


class RecipientActivityTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "sample.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE events (" + ",".join(FIELDS) + ")")
            rows = [
                transfer("source-bound", 1, recipient="other"), transfer("baseline-bound", 2),
                transfer("previous-2", 3), transfer("previous-3", 4), transfer("previous-last", 6),
                transfer("recent-bound", 7), transfer("repeat", 7, hour="05:00:00"),
                transfer("target", 8, sender="bank:9"), transfer("tie", 8, sender="bank:8"),
                transfer("future", 8, sender="bank:7", hour="00:01:00"),
                transfer("self-loop", 7, sender="bank:2"), transfer("unrelated", 7, recipient="other"),
                transfer("sparse", 8, recipient="sparse"),
            ]
            connection.executemany("INSERT INTO events VALUES (" + ",".join("?" for _ in FIELDS) + ")", rows)
            connection.commit()
        self.source = SourceIndex(path)
        self.addCleanup(self.source.close)

    def test_baseline_and_recent_boundaries_use_unique_senders_and_as_of_target(self):
        result = RecipientActivity(self.source).compare("target")
        self.assertEqual(result["status"], "comparable")
        self.assertEqual(result["baselineSenderCounts"], [1, 1, 1, 0, 1])
        self.assertEqual(result["recentSenderCount"], 2)
        self.assertEqual(result["recentTransactionCount"], 3)
        self.assertEqual(result["senderRatio"], 2.0)
        self.assertEqual(result["transactionRatio"], 3.0)

    def test_source_boundary_sparse_history_and_cap_do_not_produce_ratios(self):
        for target, limit, expected in [("baseline-bound", 50_000, "insufficient_coverage"),
                                        ("sparse", 50_000, "insufficient_activity"),
                                        ("target", 1, "row_limit")]:
            with self.subTest(target=target):
                result = RecipientActivity(self.source, max_rows=limit).compare(target)
                self.assertEqual(result["status"], expected)
                self.assertNotIn("senderRatio", result)

    def test_changed_expected_target_is_rejected(self):
        with self.assertRaises(ValueError):
            RecipientActivity(self.source).compare("target", expected_target={})
