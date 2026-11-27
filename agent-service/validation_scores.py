"""Development-only scores on the V2 validation split (never the holdout or fresh sample).

Reproduces the team's out-of-fold procedure from `scripts/evaluate-recipient-hybrid.py
develop`: grouped folds by recipient and pattern family, the team's trainer, and the
team's combined score. Each validation case is scored by a model that did not see
it, so thresholds tuned here are not inflated by training on the same cases.

    python agent-service/validation_scores.py      # compare alert-trigger breadth
"""
from __future__ import annotations

from contextlib import closing
from importlib import import_module
import json
import sys

import _paths
from evaluation_index import SourceIndex
from experiment import summarize, validate_file
from recipient_activity import RecipientActivity
from recipient_hybrid import FEATURE_SETS, combined_score, grouped_folds, train
from rules import RuleConfig, score
from risk_score import DEFAULT_INDEX, VARIANT

V2 = _paths.ROOT / "data" / "evaluation" / "HI-Small-v2"


def team_script():
    scripts = str(_paths.ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    return import_module("evaluate-recipient-hybrid")


def validation_scores(variant=VARIANT):
    """Return validation cases with V2 rule, default rule and out-of-fold hybrid scores."""
    script = team_script()
    manifest, frozen, _, _ = script.runs()   # same input checks as the team script
    cases = validate_file(V2, manifest, "validation")
    v2_config = RuleConfig(**frozen["selectedConfig"])
    with closing(SourceIndex(DEFAULT_INDEX)) as source:
        rows, recipients, v2_rules = script.extract_features(cases, RecipientActivity(source), v2_config)
    out_of_fold = [None] * len(cases)
    every = set(range(len(cases)))
    for fold in grouped_folds(cases, recipients):
        fit = sorted(every - set(fold))
        model = train([rows[i] for i in fit], [cases[i] for i in fit], FEATURE_SETS[variant])
        for index in fold:
            out_of_fold[index] = combined_score(model, rows[index], v2_rules[index]["ruleScore"])
    default_rules = [score(case["graph"], RuleConfig()) for case in cases]
    return {"cases": cases, "features": rows, "v2Rules": v2_rules, "defaultRules": default_rules,
            "hybridOutOfFold": out_of_fold, "v2Config": frozen["selectedConfig"]}


class OutOfFoldRisk:
    """Risk source for validation alerts: each case scored by a model that did not see it."""

    def __init__(self, data, thresholds=None):
        self.scores = {case["graph"]["targetTxId"]: value
                       for case, value in zip(data["cases"], data["hybridOutOfFold"])}
        self.thresholds = thresholds or {}

    def score(self, alert, context=None):
        value = self.scores.get(alert["graph"]["targetTxId"])
        if value is None:
            return {"status": "unavailable", "reason": "not_a_validation_case"}
        return {"status": "computed", "method": "rule_ml_hybrid_v1_out_of_fold", "score": round(value, 4),
                "thresholds": self.thresholds}


def trigger_report(data, hybrid_cut):
    cases = data["cases"]
    default = [row["flagged"] for row in data["defaultRules"]]
    v2 = [row["flagged"] for row in data["v2Rules"]]
    hybrid = [value >= hybrid_cut for value in data["hybridOutOfFold"]]
    options = {
        "default rule (24h)": default,
        "V2 rule (7d)": v2,
        "hybrid >= cut": hybrid,
        "default OR V2": [a or b for a, b in zip(default, v2)],
        "V2 OR hybrid": [a or b for a, b in zip(v2, hybrid)],
        "default OR V2 OR hybrid": [a or b or c for a, b, c in zip(default, v2, hybrid)],
    }
    report = {}
    for name, flags in options.items():
        result = summarize(cases, [{"flagged": flag} for flag in flags])
        sample, weighted = result["sample"], result["weighted"]
        missed = sorted({cases[i]["expectedPatternType"] for i, flag in enumerate(flags)
                         if cases[i]["expectedLabel"] and not flag})
        report[name] = {"tp": sample["tp"], "fn": sample["fn"], "fp": sample["fp"],
                        "weightedRecall": round(weighted["recall"], 4),
                        "weightedFalsePositiveRate": round(weighted["falsePositiveRate"], 4),
                        "missedTypes": missed}
    return report


def main():
    data = validation_scores()
    locked = json.loads((_paths.ROOT / "artifacts" / "recipient-hybrid-frozen-next.json").read_bytes())
    cut = locked["variants"][VARIANT]["thresholds"]["recallTarget"]
    print(json.dumps({"split": "V2 validation (development)", "cases": len(data["cases"]),
                      "hybridCut": cut, "hybridScores": "out-of-fold",
                      "triggers": trigger_report(data, cut)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
