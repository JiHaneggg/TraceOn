"""FastRP builder and runtime similarity on a tiny synthetic transactions file."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401

try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    HAS_NUMERIC = True
except ImportError:
    HAS_NUMERIC = False

FIELDS = ["txId", "timestamp", "fromAccountKey", "toAccountKey", "amountPaid", "paymentCurrency",
          "amountReceived", "receivingCurrency", "amountUsd", "paymentFormat", "isSelfLoop",
          "isLaundering", "patternId", "patternType"]
TRAIN_END = "2022-09-05T00:00:00"


def write_transactions(path):
    """Two dense rings (one laundering, one normal) before the cutoff, plus later-only activity."""
    rows = []

    def tx(day, hour, source, target, laundering=0, self_loop=0):
        rows.append({"txId": f"T{len(rows)}", "timestamp": f"2022-09-0{day}T{hour:02d}:00:00",
                     "fromAccountKey": source, "toAccountKey": target, "amountPaid": "100.00",
                     "paymentCurrency": "US Dollar", "amountReceived": "100.00", "receivingCurrency": "US Dollar",
                     "amountUsd": "100.00", "paymentFormat": "ACH", "isSelfLoop": str(self_loop),
                     "isLaundering": str(laundering), "patternId": "", "patternType": ""})

    bad = [f"L{i}" for i in range(6)]
    good = [f"N{i}" for i in range(6)]
    for ring, laundering in ((bad, 1), (good, 0)):
        for i, account in enumerate(ring):
            for step in (1, 2):
                tx(1 + i % 3, i + step, account, ring[(i + step) % len(ring)], laundering if step == 1 else 0)
    tx(2, 12, "L0", "N0")                        # one bridge between the rings
    tx(3, 9, "N5", "N5", self_loop=1)            # self-loops never enter the graph
    tx(6, 9, "LATE", "L1", laundering=1)          # after cutoff: no node, no reference
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def context(as_of):
    return {"scope": {"asOf": as_of}}


def profile(alias, key):
    return {"alias": alias, "accountKey": key}


@unittest.skipUnless(HAS_NUMERIC, "numpy/scipy not installed: pip install -r agent-service/requirements.txt")
class FastRPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastrp_embeddings import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_transactions(root / "transactions.csv")
        cls.output = root / "embeddings"
        cls.manifest = build(root / "transactions.csv", TRAIN_END, cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def similarity(self):
        from embedding_similarity import EmbeddingSimilarity
        return EmbeddingSimilarity(self.output)

    def test_only_training_period_and_non_self_loops_become_nodes(self):
        accounts = json.loads((self.output / "accounts.json").read_text(encoding="utf-8"))
        self.assertNotIn("LATE", accounts)
        self.assertEqual(len(accounts), 12)
        self.assertEqual(self.manifest["trainEnd"], TRAIN_END)

    def test_references_are_training_period_laundering_accounts_only(self):
        references = json.loads((self.output / "references.json").read_text(encoding="utf-8"))["accountKeys"]
        self.assertEqual(sorted(references), [f"L{i}" for i in range(6)])

    def test_vectors_are_unit_length_and_build_is_reproducible(self):
        import numpy as np
        from fastrp_embeddings import build
        vectors = np.load(self.output / "embeddings.npy")
        self.assertEqual(vectors.shape, (12, 64))
        self.assertTrue(np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5))
        again = Path(self.temp.name) / "again"
        build(Path(self.temp.name) / "transactions.csv", TRAIN_END, again)
        self.assertEqual(self.manifest["files"], json.loads((again / "manifest.json").read_text())["files"])

    def test_existing_output_is_never_overwritten(self):
        from fastrp_embeddings import build
        with self.assertRaises(ValueError):
            build(Path(self.temp.name) / "transactions.csv", TRAIN_END, self.output)

    def test_ring_members_are_closer_to_laundering_ring_than_normal_ring(self):
        scorer = self.similarity()
        laundering = scorer.account_score("L3")
        normal = scorer.account_score("N3")
        self.assertTrue(laundering["accountInReferenceSet"])
        self.assertNotEqual(laundering["nearestReferenceId"], "L3")
        self.assertFalse(normal["accountInReferenceSet"])
        self.assertGreater(laundering["score"], normal["score"])

    def test_alert_inside_training_period_gets_no_similarity(self):
        result = self.similarity().score([profile("sender", "L3")], context("2022-09-04T23:59:00"))
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "alert_inside_embedding_training_period"))

    def test_unseen_account_is_skipped_like_a_new_account(self):
        result = self.similarity().score([profile("sender", "NEW"), profile("receiver", "N2")],
                                         context("2022-09-07T00:00:00"))
        self.assertEqual(result["accounts"][0]["reason"], "account_not_in_training_graph")
        self.assertEqual((result["status"], result["alias"]), ("computed", "receiver"))

    def test_tampered_embedding_file_is_refused(self):
        from embedding_similarity import EmbeddingSimilarity
        import shutil
        copy = Path(self.temp.name) / "tampered"
        shutil.copytree(self.output, copy)
        (copy / "references.json").write_text(json.dumps({"accountKeys": ["N1"]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            EmbeddingSimilarity(copy)


if __name__ == "__main__":
    unittest.main()
