"""Neo4j import 파일 변환 테스트."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import neo4j_export as ne  # noqa: E402
import preprocess as pp  # noqa: E402
from test_preprocess import ACCOUNTS, HEADER, PATTERNS, TRANS  # noqa: E402


class Neo4jExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        inp = root / "in"
        inp.mkdir()
        (inp / "T_Trans.csv").write_text(HEADER + "\n" + "\n".join(TRANS) + "\n", encoding="utf-8")
        (inp / "T_Patterns.txt").write_text(PATTERNS, encoding="utf-8")
        (inp / "T_accounts.csv").write_text(ACCOUNTS, encoding="utf-8")
        pp.run(inp, "T", root / "processed")
        self.out = root / "import" / "T"
        self.counts = ne.run(root / "processed" / "T", self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, name):
        with (self.out / name).open(newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def test_headers_use_neo4j_types(self):
        self.assertEqual(self.rows("accounts.csv")[0], ne.ACCOUNT_HEADER)
        self.assertEqual(self.rows("transfers.csv")[0], ne.TRANSFER_HEADER)
        self.assertEqual(self.counts, (5, 6))

    def test_flags_become_true_false_and_empty_pattern_stays_empty(self):
        header, *data = self.rows("transfers.csv")
        col = {name.split(":")[0]: i for i, name in enumerate(header)}
        self.assertEqual(data[0][col["isSelfLoop"]], "true")
        self.assertEqual(data[0][col["isLaundering"]], "false")
        self.assertEqual(data[0][col["patternId"]], "")
        self.assertEqual(data[2][col["isLaundering"]], "true")
        self.assertEqual(data[2][col["patternType"]], "CYCLE")
        self.assertTrue(all(r[-1] == "TRANSFER" for r in data))

    def test_invalid_flag_is_rejected(self):
        with self.assertRaises(ValueError):
            ne.to_bool("yes")


if __name__ == "__main__":
    unittest.main()
