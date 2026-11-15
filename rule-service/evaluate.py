"""Tune on validation cases; evaluate the frozen configuration on test cases.

Case file: [{"split":"validation"|"test", "expectedLabel":0|1,
             "graph": <label-free common subgraph>}].
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict
from pathlib import Path

from rules import RuleConfig, score


def metrics(labels, predictions):
    if len(labels) != len(predictions) or not labels:
        raise ValueError("Nonempty aligned labels and predictions are required")
    if any(type(x) is not int or x not in (0, 1) for x in labels):
        raise ValueError("Labels must be integer 0/1")
    tp = sum(y == 1 and bool(p) for y, p in zip(labels, predictions))
    fp = sum(y == 0 and bool(p) for y, p in zip(labels, predictions))
    fn = sum(y == 1 and not p for y, p in zip(labels, predictions))
    tn = sum(y == 0 and not p for y, p in zip(labels, predictions))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "flagged": tp + fp, "total": len(labels)}


def evaluate(cases, config):
    outputs = [score(c["graph"], config) for c in cases]
    return metrics([c["expectedLabel"] for c in cases], [r["flagged"] for r in outputs]), outputs


def benchmark(cases, target_recall):
    if not 0 < target_recall <= 1:
        raise ValueError("target_recall must be in (0,1]")
    validation = [c for c in cases if c["split"] == "validation"]
    test = [c for c in cases if c["split"] == "test"]
    if not validation or not test:
        raise ValueError("Separate validation and test cases are required")
    if any(c["split"] not in {"train", "validation", "test"} for c in cases):
        raise ValueError("Unknown split")
    ids = [c["graph"]["targetTxId"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Repeated evaluation target")
    if max(c["graph"]["asOf"] for c in validation) >= min(c["graph"]["asOf"] for c in test):
        raise ValueError("Test targets must be strictly later than validation targets")
    if any(len({c["expectedLabel"] for c in split}) != 2 for split in [validation, test]):
        raise ValueError("Each evaluation split must contain both classes")
    sweep = []
    for n, window, threshold in itertools.product([2, 3, 4, 5], [60, 1440, 10080], [60, 65, 75, 90]):
        config = RuleConfig(n, window, 4, threshold)
        result, _ = evaluate(validation, config)
        sweep.append({"config": asdict(config), "validation": result})
    eligible = [r for r in sweep if r["validation"]["recall"] >= target_recall]
    if not eligible:
        return {"status": "target_not_met", "targetRecall": target_recall,
                "sweep": sweep, "test": None}
    # Precision first, then fewer candidate alerts, then recall. Stable tie order.
    chosen = max(eligible, key=lambda r: (r["validation"]["precision"] or 0,
                                        -r["validation"]["flagged"], r["validation"]["recall"]))
    test_metrics, outputs = evaluate(test, RuleConfig(**chosen["config"]))
    return {"status": "evaluated", "targetRecall": target_recall,
            "validationTargetMet": True, "testTargetMet": test_metrics["recall"] >= target_recall,
            "selected": chosen,
            "test": test_metrics, "testOutputs": outputs, "sweep": sweep}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--target-recall", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    report = benchmark(cases, args.target_recall)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "targetRecall", "test")}, indent=2))


if __name__ == "__main__":
    main()
