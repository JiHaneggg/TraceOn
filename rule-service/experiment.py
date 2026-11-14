"""Versioned validation-only tuning followed by one sealed holdout evaluation."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from evaluate import metrics
from rules import RuleConfig, apply_config, score


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_cases(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def weighted_metrics(cases, outputs):
    totals = {"tp": 0., "fp": 0., "fn": 0., "tn": 0.}
    for case, output in zip(cases, outputs):
        weight = float(case["samplingWeight"])
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Sampling weights must be positive and finite")
        truth, flagged = case["expectedLabel"], bool(output["flagged"])
        if type(truth) is not int or truth not in (0, 1):
            raise ValueError("Invalid expectedLabel")
        totals[("tp" if flagged else "fn") if truth else ("fp" if flagged else "tn")] += weight
    tp, fp, fn, tn = (totals[key] for key in ["tp", "fp", "fn", "tn"])
    return {**totals, "recall": tp / (tp + fn) if tp + fn else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "falsePositiveRate": fp / (fp + tn) if fp + tn else None,
            "alertRate": (tp + fp) / (tp + fp + fn + tn),
            "method": "inverse_sampling_weighted_totals_ratio_estimate"}


def summarize(cases, outputs):
    if len(cases) != len(outputs) or not cases:
        raise ValueError("Nonempty aligned cases and outputs required")
    sample = metrics([c["expectedLabel"] for c in cases], [o["flagged"] for o in outputs])
    by_type = defaultdict(list)
    by_period = defaultdict(list)
    for i, case in enumerate(cases):
        if case["expectedLabel"]:
            by_type[case["expectedPatternType"]].append(i)
        by_period[case["graph"]["asOf"][:10]].append(i)
    def subgroup(indices):
        return metrics([cases[i]["expectedLabel"] for i in indices], [outputs[i]["flagged"] for i in indices])
    return {"sample": sample, "weighted": weighted_metrics(cases, outputs),
            "byPattern": {kind: subgroup(indices) for kind, indices in sorted(by_type.items())},
            "byDay": {day: subgroup(indices) for day, indices in sorted(by_period.items())},
            "truncatedGraphs": sum(bool(c["graph"]["meta"]["truncated"]) for c in cases)}


def candidates():
    # A global cutoff above 65 disables both fan rules, so keep them enabled and
    # tune their own evidence thresholds independently. All choices are fixed
    # before opening the new holdout.
    for window, hops, fan_in, fan_out in itertools.product(
            [1440, 4320, 10080], [4, 6], [2, 3, 5, 8, 12, 20], [2, 3, 5, 8, 12, 20]):
        yield RuleConfig(window_minutes=window, max_cycle_hops=hops, threshold=65,
                         fan_in_min_counterparties=fan_in, fan_out_min_counterparties=fan_out)


def analyze(cases, config, cache):
    key = (config.window_minutes, config.max_cycle_hops)
    if key not in cache:
        start = time.perf_counter()
        permissive = RuleConfig(min_counterparties=2, window_minutes=key[0], max_cycle_hops=key[1], threshold=65)
        cache[key] = [score(c["graph"], permissive) for c in cases]
        print(f"Analyzed {len(cases)} cases: window={key[0]}min hops={key[1]} ({time.perf_counter()-start:.1f}s)", flush=True)
    return [apply_config(output, config) for output in cache[key]]


def validate_file(folder, manifest, split):
    path = folder / manifest["files"][split]["file"]
    if digest(path) != manifest["files"][split]["sha256"]:
        raise ValueError(f"{split} data changed after preparation")
    cases = read_cases(path)
    if len(cases) != manifest["files"][split]["cases"]:
        raise ValueError("Case count differs from manifest")
    if any(c["split"] != split for c in cases):
        raise ValueError("Unexpected split in case file")
    targets = [c["graph"]["targetTxId"] for c in cases]
    if len(targets) != len(set(targets)):
        raise ValueError("Repeated target ID")
    cutoff = manifest["timeCutoffs"]["holdoutStart"]
    if any((c["graph"]["asOf"] >= cutoff) != (split == "holdout") for c in cases):
        raise ValueError("Case is on the wrong side of chronological holdout cutoff")
    return cases


def tune(folder, baseline_path, output, target_recall=.9):
    seal = output.with_suffix(output.suffix + ".sha256")
    if output.exists() or seal.exists():
        raise ValueError("Frozen output already exists; choose a new experiment version")
    if not math.isfinite(target_recall) or not 0 < target_recall <= 1:
        raise ValueError("Target recall must be in (0,1]")
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Deliberately read validation only. Holdout content is not touched here.
    cases = validate_file(folder, manifest, "validation")
    baseline = RuleConfig(**json.loads(baseline_path.read_text(encoding="utf-8"))["selected"]["config"])
    cache, sweep = {}, []
    grid = list(candidates())
    for config in grid:
        result = summarize(cases, analyze(cases, config, cache))
        sweep.append({"config": asdict(config), "weighted": result["weighted"], "sample": result["sample"]})
    eligible = [r for r in sweep if r["weighted"]["recall"] >= target_recall]
    pool = eligible or sweep
    if eligible:
        best = max(pool, key=lambda r: (r["weighted"]["precision"] or 0,
                                       -r["weighted"]["alertRate"], r["weighted"]["recall"]))
    else:
        best = max(pool, key=lambda r: (r["weighted"]["recall"], r["weighted"]["precision"] or 0,
                                       -r["weighted"]["alertRate"]))
    selected = RuleConfig(**best["config"])
    frozen = {"experimentVersion": 2, "phase": "frozen_before_holdout", "targetRecall": target_recall,
              "validationTargetMet": bool(eligible), "deployable": False,
              "selectionPolicy": "precision_at_target_recall_else_maximum_recall_research_candidate",
              "manifestSha256": digest(manifest_path), "validationSha256": manifest["files"]["validation"]["sha256"],
              "holdoutSha256": manifest["files"]["holdout"]["sha256"],
              "baselineSourceSha256": digest(baseline_path), "baselineConfig": asdict(baseline),
              "engineSha256": digest(Path(__file__).with_name("rules.py")),
              "selectedConfig": asdict(selected), "grid": [asdict(c) for c in grid],
              "validation": {"baseline": summarize(cases, analyze(cases, baseline, cache)),
                             "selected": summarize(cases, analyze(cases, selected, cache))}, "sweep": sweep}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(frozen, indent=2), encoding="utf-8")
    seal.write_text(digest(output), encoding="ascii")
    print(json.dumps({"validationTargetMet": frozen["validationTargetMet"], "selectedConfig": frozen["selectedConfig"],
                      "validation": frozen["validation"]}, indent=2))
    return frozen


def holdout(folder, frozen_path, output):
    if output.exists():
        raise ValueError("Holdout report already exists; it will not be overwritten")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    seal = frozen_path.with_suffix(frozen_path.suffix + ".sha256")
    if digest(frozen_path) != seal.read_text(encoding="ascii").strip():
        raise ValueError("Frozen model changed after sealing")
    if digest(Path(__file__).with_name("rules.py")) != frozen["engineSha256"]:
        raise ValueError("Rule engine changed after freezing")
    if frozen["phase"] != "frozen_before_holdout" or digest(folder / "manifest.json") != frozen["manifestSha256"]:
        raise ValueError("Experiment/data changed since freezing")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest["files"]["holdout"]["sha256"] != frozen["holdoutSha256"]:
        raise ValueError("Holdout hash differs from frozen selection")
    cases = validate_file(folder, manifest, "holdout")
    cache, results = {}, {}
    for name, raw in [("baseline", frozen["baselineConfig"]), ("selected", frozen["selectedConfig"])]:
        outputs = analyze(cases, RuleConfig(**raw), cache)
        results[name] = {**summarize(cases, outputs),
                         "outputs": [{"txId": row["txId"], "score": row["ruleScore"], "flagged": row["flagged"],
                                      "rules": [f["ruleId"] for f in row["findings"]]} for row in outputs]}
    report = {"experimentVersion": 2, "phase": "holdout_evaluated", "frozenSha256": digest(frozen_path),
              "manifestSha256": frozen["manifestSha256"], "targetRecall": frozen["targetRecall"],
              "validationTargetMet": frozen["validationTargetMet"],
              "holdoutTargetMet": results["selected"]["weighted"]["recall"] >= frozen["targetRecall"],
              "scope": "chronological_case_control_with_prior_target_and_family_exclusions",
              "results": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({name: {"sample": r["sample"], "weighted": r["weighted"]} for name, r in results.items()}, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["tune", "holdout"])
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--target-recall", type=float, default=.9)
    args = parser.parse_args()
    if args.phase == "tune":
        if args.baseline is None or not 0 < args.target_recall <= 1:
            parser.error("tune requires --baseline and target recall in (0,1]")
        tune(args.data, args.baseline, args.output, args.target_recall)
    else:
        if args.frozen is None:
            parser.error("holdout requires --frozen")
        holdout(args.data, args.frozen, args.output)
