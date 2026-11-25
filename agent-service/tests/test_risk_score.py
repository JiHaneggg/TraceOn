"""Rule+ML risk score wiring on a tiny source index built with the team's own indexer."""
from contextlib import closing
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401

from basic_profile import make_basic_profile
from alert_context import alert_context
from evaluation_index import FIELDS, SourceIndex, build
from investigation_state import initial_state
from recipient_activity import RecipientActivity
from recipient_hybrid import FEATURE_SETS, combined_score, features
from risk_score import VARIANT, HybridRiskScore
from rules import RuleConfig, score

V2_RULES = RuleConfig(min_counterparties=3, window_minutes=10080, fan_in_min_counterparties=2,
                      fan_out_min_counterparties=12)
MODEL = {"features": list(FEATURE_SETS[VARIANT]), "means": [0.0] * 5, "scales": [1.0] * 5,
         "coefficients": [0.2, 0.1, 0.1, 0.3, 0.4], "intercept": -1.0}
TARGET = "T-TARGET"


def transactions():
    """Recipient R: one sender a day for five days, then four senders within hours (target last)."""
    rows = []

    def tx(tx_id, stamp, source, target):
        rows.append({"txId": tx_id, "timestamp": stamp, "fromAccountKey": source, "toAccountKey": target,
                     "amountPaid": "500.00", "paymentCurrency": "US Dollar", "amountReceived": "500.00",
                     "receivingCurrency": "US Dollar", "paymentFormat": "ACH", "isLaundering": "0",
                     "patternId": "", "patternType": ""})

    tx("T-EARLY", "2022-08-30T00:00:00", "X", "Y")
    for day in range(1, 6):
        tx(f"T-A{day}", f"2022-09-0{day}T00:00:00", f"A{day}", "R")
    for number, hour in enumerate((8, 9, 10), 1):
        tx(f"T-B{number}", f"2022-09-06T{hour:02d}:00:00", f"B{number}", "R")
    tx(TARGET, "2022-09-06T11:00:00", "B4", "R")
    tx("T-LATE", "2022-09-07T00:00:00", "Y", "X")
    return rows


class RiskScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.temp.name)
        with open(root / "transactions.csv", "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(transactions())
        build(root / "transactions.csv", root / "index.sqlite3")
        cls.rule_freeze = root / "rule-v2-frozen.json"
        cls.rule_freeze.write_text(json.dumps({"selectedConfig": asdict(V2_RULES)}), encoding="utf-8")
        rule_digest = hashlib.sha256(cls.rule_freeze.read_bytes()).hexdigest()
        cls.rule_freeze.with_suffix(".json.sha256").write_text(rule_digest, encoding="ascii")
        cls.freeze = root / "hybrid-frozen.json"
        cls.freeze.write_text(json.dumps({
            "phase": "frozen_before_hybrid_fresh_sample", "provenance": {"v2FrozenSha256": rule_digest},
            "mlWeight": 0.5, "variants": {VARIANT: {"model": MODEL,
                                                     "thresholds": {"recallTarget": 0.3, "fprBudget": 0.9}}}}),
            encoding="utf-8")
        (root / "validation.jsonl").write_text(json.dumps({"graph": {"targetTxId": TARGET}}) + "\n", encoding="utf-8")
        with closing(SourceIndex(root / "index.sqlite3")) as source:
            graph = source.subgraph(TARGET)
        cls.alert = {"graph": graph, "ruleResult": score(graph, RuleConfig())}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def scorer(self, **overrides):
        options = {"freeze": self.freeze, "rule_freeze": self.rule_freeze, "index": self.root / "index.sqlite3",
                   "training_cases": self.root / "validation.jsonl", **overrides}
        return HybridRiskScore(**options)

    def test_score_equals_team_functions_on_the_same_history(self):
        scorer = self.scorer()
        try:
            result = scorer.score(self.alert)
        finally:
            scorer.close()
        graph = self.alert["graph"]
        target = next(edge for edge in graph["edges"] if edge["txId"] == TARGET)
        with closing(SourceIndex(self.root / "index.sqlite3")) as source:
            history = RecipientActivity(source).compare(TARGET, expected_target=target)
        row = features(graph, target, history)
        expected = combined_score(MODEL, row, score(graph, V2_RULES)["ruleScore"])
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["score"], round(expected, 4))
        self.assertEqual((result["features"]["inboundSenders24h"], result["features"]["senderDelta"]), (4, 3))
        self.assertEqual(result["historyStatus"], "comparable")
        self.assertEqual(result["aboveRecallTarget"], expected >= 0.3)
        self.assertTrue(result["inModelTrainingSet"])

    def test_target_missing_from_source_index_is_unavailable_not_guessed(self):
        from synthetic_graphs import SCENARIOS
        scorer = self.scorer()
        try:
            result = scorer.score(SCENARIOS["fan_in_mule"]())
        finally:
            scorer.close()
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "target_not_in_source_index"))

    def test_modified_rule_freeze_or_foreign_model_is_refused(self):
        tampered = self.root / "tampered-rules.json"
        tampered.write_text(json.dumps({"selectedConfig": asdict(RuleConfig())}), encoding="utf-8")
        tampered.with_suffix(".json.sha256").write_text("0" * 64, encoding="ascii")
        with self.assertRaises(ValueError):
            self.scorer(rule_freeze=tampered)
        foreign = self.root / "foreign-rules.json"
        foreign.write_text(json.dumps({"selectedConfig": asdict(RuleConfig())}), encoding="utf-8")
        foreign.with_suffix(".json.sha256").write_text(hashlib.sha256(foreign.read_bytes()).hexdigest(), encoding="ascii")
        with self.assertRaises(ValueError):
            self.scorer(rule_freeze=foreign)

    def test_basic_profile_records_risk_as_routing_signal_not_evidence(self):
        scorer = self.scorer()
        try:
            state = initial_state(self.alert)
            state = {**state, **alert_context(state)}
            update = make_basic_profile(risk_source=scorer)(state)
        finally:
            scorer.close()
        self.assertEqual(update["profile"]["riskScore"]["status"], "computed")
        self.assertEqual(update["trace"][0]["riskScore"]["status"], "computed")
        self.assertFalse([row for row in update["evidence"] if "risk" in row["factId"].lower()])

    def test_without_risk_source_the_signal_is_unavailable(self):
        state = initial_state(self.alert)
        state = {**state, **alert_context(state)}
        update = make_basic_profile()(state)
        self.assertEqual(update["profile"]["riskScore"], {"status": "unavailable", "reason": "risk_source_not_loaded"})


if __name__ == "__main__":
    unittest.main()
