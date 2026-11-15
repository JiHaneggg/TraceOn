"""전처리 규칙 단위 테스트. 실행: python -m unittest discover -s tests -v"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import preprocess as pp  # noqa: E402

HEADER = ",".join(pp.EXPECTED_HEADER)
TRANS = [
    "2022/09/01 00:20,010,8000EBD30,010,8000EBD30,3697.34,US Dollar,3697.34,US Dollar,Reinvestment,0",
    "2022/09/01 00:21,014,AAA,0014,BBB,85.34,Euro,100.00,US Dollar,ACH,0",
    "2022/09/01 00:22,0014,BBB,020,CCC,170.68,Euro,170.68,Euro,ACH,1",
    "2022/09/01 00:23,020,CCC,014,AAA,200.00,US Dollar,200.00,US Dollar,ACH,1",
    "2022/09/01 00:21,014,AAA,0014,BBB,85.34,Euro,100.00,US Dollar,ACH,0",  # 2번째 행과 완전히 같은 행
    "2022/09/02 09:00,030,DDD,014,AAA,50.00,US Dollar,50.00,US Dollar,Cash,1",
]
PATTERNS = """BEGIN LAUNDERING ATTEMPT - CYCLE:  Max 3 hops
2022/09/01 00:22,0014,BBB,020,CCC,170.68,Euro,170.68,Euro,ACH,1
2022/09/01 00:23,020,CCC,014,AAA,200.00,US Dollar,200.00,US Dollar,ACH,1
END LAUNDERING ATTEMPT - CYCLE

BEGIN LAUNDERING ATTEMPT - STACK
2022/09/02 09:00,030,DDD,014,AAA,50.00,US Dollar,50.00,US Dollar,Cash,1
END LAUNDERING ATTEMPT - STACK
"""
ACCOUNTS = """Bank Name,Bank ID,Account Number,Entity ID,Entity Name
Korea Bank #1,14,AAA,E1,Corporation #1
Korea Bank #1,14,BBB,E2,Partnership #2
"""


class RuleTest(unittest.TestCase):
    def test_bank_zero_padding_is_normalized(self):
        self.assertEqual(pp.account_key("014", "AAA"), pp.account_key("0014", "AAA"))
        self.assertEqual(pp.account_key("0014", "AAA"), "14:AAA")

    def test_same_account_number_in_different_banks_is_different(self):
        self.assertNotEqual(pp.account_key("221731", "X"), pp.account_key("27444", "X"))

    def test_timestamp(self):
        self.assertEqual(pp.to_iso_timestamp("2022/09/01 00:20"), "2022-09-01T00:20:00")

    def test_parse_patterns(self):
        patterns, rows = pp.parse_patterns(PATTERNS.splitlines())
        self.assertEqual([(p.pattern_id, p.pattern_type, p.detail, p.tx_count) for p in patterns],
                         [("P-00001", "CYCLE", "Max 3 hops", 2), ("P-00002", "STACK", "", 1)])
        self.assertEqual(len(rows), 3)

    def test_unclosed_pattern_is_rejected(self):
        with self.assertRaises(ValueError):
            pp.parse_patterns(PATTERNS.splitlines()[:3])

    def test_fx_rate_from_internal_exchange(self):
        rows = [line.split(",") for line in TRANS]
        rates = pp.compute_fx_rates(rows)
        self.assertAlmostEqual(rates["Euro"][0], 0.8534)
        self.assertEqual(pp.to_usd("170.68", "Euro", rates), "200.00")
        with self.assertRaises(ValueError):
            pp.to_usd("1", "Klingon Darsek", rates)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inp, self.out = root / "in", root / "out"
        self.inp.mkdir()
        (self.inp / "T_Trans.csv").write_text(HEADER + "\n" + "\n".join(TRANS) + "\n", encoding="utf-8")
        (self.inp / "T_Patterns.txt").write_text(PATTERNS, encoding="utf-8")
        (self.inp / "T_accounts.csv").write_text(ACCOUNTS, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        with (self.out / "T" / name).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_outputs(self):
        manifest = pp.run(self.inp, "T", self.out)
        txs = self.read("transactions.csv")
        self.assertEqual([t["txId"] for t in txs], [f"TX-{i:09d}" for i in range(1, 7)])
        self.assertEqual(txs[0]["isSelfLoop"], "1")
        self.assertEqual(txs[1]["fromAccountKey"], "14:AAA")
        self.assertEqual(txs[1]["toAccountKey"], "14:BBB")
        # 내용이 완전히 같은 2번째·5번째 행도 서로 다른 txId를 받는다
        self.assertEqual(txs[1]['timestamp'], txs[4]['timestamp'])
        self.assertNotEqual(txs[1]['txId'], txs[4]['txId'])
        self.assertEqual((txs[2]["patternType"], txs[5]["patternType"]), ("CYCLE", "STACK"))
        self.assertEqual(txs[2]["amountUsd"], "200.00")
        accounts = {a["accountKey"]: a for a in self.read("accounts.csv")}
        self.assertEqual(set(accounts), {"10:8000EBD30", "14:AAA", "14:BBB", "20:CCC", "30:DDD"})
        self.assertEqual(accounts["14:AAA"]["entityName"], "Corporation #1")
        self.assertEqual(manifest["counts"]["laundering"], 3)
        self.assertEqual(manifest["counts"]["patternMatched"], 3)
        self.assertEqual(manifest["counts"]["accountsWithoutInfo"], 3)
        self.assertTrue(json.loads((self.out / "T" / "manifest.json").read_text(encoding="utf-8")))

    def test_bad_header_is_rejected(self):
        (self.inp / "T_Trans.csv").write_text("a,b,c\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            pp.run(self.inp, "T", self.out)


if __name__ == "__main__":
    unittest.main()
