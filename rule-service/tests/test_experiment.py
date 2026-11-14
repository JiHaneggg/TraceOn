import copy
import json
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "rule-service"), str(ROOT / "data-pipeline")]
import experiment
from make_demo import generate
from rules import RuleConfig, apply_config, score


class ExperimentTest(unittest.TestCase):
    def test_cached_analysis_matches_direct_per_rule_configuration(self):
        _, cases = generate()
        for case in cases:
            for window in [60, 1440]:
                candidate = score(case["graph"], RuleConfig(min_counterparties=2, window_minutes=window))
                config = RuleConfig(window_minutes=window, fan_in_min_counterparties=4,
                                    fan_out_min_counterparties=3)
                self.assertEqual(apply_config(candidate, config), score(case["graph"], config))
        with self.assertRaisesRegex(ValueError, "Cached"):
            apply_config(candidate, RuleConfig(window_minutes=10080))

    def test_per_rule_thresholds_are_independent(self):
        _, cases = generate()
        config = RuleConfig(fan_in_min_counterparties=4, fan_out_min_counterparties=3)
        fan_in = next(c for c in cases if c["scenario"] == "fan_in")
        fan_out = next(c for c in cases if c["scenario"] == "fan_out")
        self.assertFalse(score(fan_in["graph"], config)["flagged"])
        self.assertTrue(score(fan_out["graph"], config)["flagged"])

    def test_weighted_precision_corrects_case_control_prevalence(self):
        cases = [{"expectedLabel": 1, "samplingWeight": 1},
                 {"expectedLabel": 0, "samplingWeight": 999}]
        result = experiment.weighted_metrics(cases, [{"flagged": True}, {"flagged": True}])
        self.assertAlmostEqual(result["precision"], .001)
        self.assertEqual(result["recall"], 1)

    def test_tune_never_opens_holdout_and_seal_detects_changed_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, fixtures = generate()
            validation = []
            for case in fixtures:
                if case["split"] == "validation":
                    validation.append({**case, "samplingWeight": 1,
                        "expectedPatternType": case["scenario"], "patternFamily": ""})
            path = root / "validation.jsonl"
            path.write_text("\n".join(json.dumps(c) for c in validation), encoding="utf-8")
            manifest = {"timeCutoffs": {"holdoutStart": "2022-09-10"},
                "files": {"validation": {"file": path.name, "sha256": experiment.digest(path), "cases": len(validation)},
                          "holdout": {"file": "does-not-exist.jsonl", "sha256": "not-opened", "cases": 5}}}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps({"selected": {"config": asdict(RuleConfig())}}), encoding="utf-8")
            frozen = root / "frozen.json"
            with patch.object(experiment, "candidates", return_value=iter([RuleConfig()])), redirect_stdout(io.StringIO()):
                experiment.tune(root, baseline, frozen, .9)
            self.assertTrue(frozen.exists())  # absent holdout file proves no holdout read
            changed = json.loads(frozen.read_text())
            changed["selectedConfig"]["threshold"] = 0
            frozen.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed after sealing"):
                experiment.holdout(root, frozen, root / "holdout-report.json")


if __name__ == "__main__":
    unittest.main()
