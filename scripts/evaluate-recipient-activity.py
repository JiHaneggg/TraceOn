"""Evaluate a recipient's recent versus typical intake with a separately sampled check."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import timedelta
from itertools import product
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / directory) for directory in ("data-pipeline", "rule-service")]
from evaluation_index import SourceIndex
from experiment import digest, summarize, validate_file
from prepare_evaluation import SEED, old_exclusions, select_targets
from recipient_activity import BASELINE_DAYS, MIN_ACTIVE_DAYS, MAX_INBOUND_ROWS, RecipientActivity
from rules import RuleConfig, score

V2_DIR = ROOT / "data/evaluation/HI-Small-v2"
FROZEN_PATH = ROOT / "artifacts/rule-v2-frozen.json"
INDEX = ROOT / "data/evaluation/HI-Small-source.sqlite3"
PILOT = ROOT / "data/processed/HI-Small-MVP/transactions.csv"
RATIOS = (1.0, 1.25, 1.5, 2.0, 3.0)
DELTAS = (0, 1, 2)
METRICS = ("sender", "transaction")
PLAIN_WINDOW = {"metric": "recent", "minSenders": 2}
EXPLORATORY_FPR_CAP = 0.50


def inputs():
    manifest_path = V2_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    frozen = json.loads(FROZEN_PATH.read_bytes())
    frozen_hash = digest(FROZEN_PATH)
    if (frozen_hash != FROZEN_PATH.with_suffix(".json.sha256").read_text(encoding="ascii").strip()
            or frozen["phase"] != "frozen_before_holdout"
            or digest(manifest_path) != frozen["manifestSha256"]
            or any(manifest["files"][split]["sha256"] != frozen[split + "Sha256"]
                   for split in ("validation", "holdout"))
            or digest(ROOT / "rule-service/rules.py") != frozen["engineSha256"]):
        raise ValueError("Original V2 inputs differ from their frozen versions")
    provenance = {
        "v2FrozenSha256": frozen_hash,
        "sourceSha256": manifest["sourceSha256"],
        "runnerSha256": digest(Path(__file__)),
        "featureCodeSha256": digest(ROOT / "data-pipeline/recipient_activity.py"),
    }
    return manifest, frozen, provenance


def read_activity(source, manifest):
    metadata = dict(source.db.execute("SELECT key,value FROM metadata"))
    if metadata["sourceSha256"] != manifest["sourceSha256"]:
        raise ValueError("Source index belongs to another data version")
    return RecipientActivity(source)


def extract_rows(cases, activity, config):
    outputs = [score(case["graph"], config) for case in cases]
    rows = []
    for case, result in zip(cases, outputs):
        graph = case["graph"]
        target = next(row for row in graph["edges"] if row["txId"] == graph["targetTxId"])
        qualifies = [item["ruleId"] for item in result["findings"] if item["score"] >= config.threshold]
        rows.append({
            "profile": activity.compare(target["txId"], expected_target=target),
            "fanIn": "FAN_IN" in qualifies,
            "otherAlert": bool(set(qualifies) - {"FAN_IN"}),
        })
    return rows, summarize(cases, outputs)


def decisions(rows, policy):
    decisions_ = []
    for row in rows:
        profile = row["profile"]
        passes = True
        if row["fanIn"] and profile["status"] == "comparable":
            if policy["metric"] == "recent":
                passes = profile["recentSenderCount"] >= policy["minSenders"]
            else:
                metric = policy["metric"]
                passes = (profile["recentSenderCount"] >= policy["minSenders"]
                          and profile[metric + "Delta"] >= policy["minIncrease"]
                          and profile[metric + "Ratio"] >= policy["minRatio"])
        decisions_.append({"flagged": row["otherAlert"] or (row["fanIn"] and passes)})
    return decisions_


def evaluate_policy(cases, rows, policy):
    return {"policy": policy, **summarize(cases, decisions(rows, policy))}


def coverage(cases, rows):
    return {label: dict(Counter(row["profile"]["status"] for case, row in zip(cases, rows)
                                if case["expectedLabel"] == expected and (not alerts_only or row["fanIn"])))
            for label, expected, alerts_only in (("normal", 0, False), ("positive", 1, False),
                                                  ("normalAmongFanInAlerts", 0, True),
                                                  ("positiveAmongFanInAlerts", 1, True))}


def save(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def develop(output, source, manifest, frozen, provenance):
    cases = validate_file(V2_DIR, manifest, "validation")
    activity = read_activity(source, manifest)
    config = RuleConfig(**frozen["selectedConfig"])
    rows, reference = extract_rows(cases, activity, config)
    if reference != frozen["validation"]["selected"]:
        raise ValueError("Research baseline differs from V2")
    sweep = [evaluate_policy(cases, rows, {"metric": metric, "minSenders": 2,
                                         "minRatio": ratio, "minIncrease": increase})
             for metric, ratio, increase in product(METRICS, RATIOS, DELTAS)]
    best_recall_target = min(
        (r for r in sweep if r["weighted"]["recall"] >= frozen["targetRecall"]
         and r["sample"]["fp"] < reference["sample"]["fp"]),
        key=lambda r: (r["weighted"]["falsePositiveRate"], -r["weighted"]["recall"]), default=None)
    exploratory = max(
        (r for r in sweep if r["weighted"]["falsePositiveRate"] <= EXPLORATORY_FPR_CAP),
        key=lambda r: (r["weighted"]["recall"], -r["weighted"]["falsePositiveRate"]), default=None)
    strict_increase = max(
        (r for r in sweep if r["weighted"]["falsePositiveRate"] <= EXPLORATORY_FPR_CAP
         and r["policy"]["minIncrease"] >= 1),
        key=lambda r: (r["weighted"]["recall"], -r["weighted"]["falsePositiveRate"]), default=None)
    selected = {"absolute24h": PLAIN_WINDOW}
    if best_recall_target:
        selected["meetsRecallTarget"] = best_recall_target["policy"]
    if exploratory and exploratory != best_recall_target:
        selected["exploratoryAt50PercentFpr"] = exploratory["policy"]
    if strict_increase and strict_increase["policy"] not in selected.values():
        selected["strictIncreaseAt50PercentFpr"] = strict_increase["policy"]
    data = {"phase": "frozen_before_fresh_evaluation", "provenance": provenance,
            "policy": {"days": BASELINE_DAYS, "minActiveDays": MIN_ACTIVE_DAYS, "maxRows": MAX_INBOUND_ROWS,
                       "targetRecall": frozen["targetRecall"], "exploratoryFprCap": EXPLORATORY_FPR_CAP},
            "sampleSelection": {"start": manifest["timeCutoffs"]["holdoutStart"], "samplingSeed": SEED,
                                "positives": 100, "normals": 150, "excludesBothV2SplitsAndPilot": True},
            "validation": {"coverage": coverage(cases, rows), "v2": reference, "sweep": sweep,
                           "chosen": {name: evaluate_policy(cases, rows, policy) for name, policy in selected.items()}},
            "selected": selected}
    save(output, data)
    print(json.dumps({"scenarios": len(sweep), "coverage": data["validation"]["coverage"],
                      "selected": selected}, ensure_ascii=False), flush=True)


def evaluate(output, freeze_path, source, manifest, frozen, provenance):
    locked = json.loads(freeze_path.read_bytes())
    if locked["phase"] != "frozen_before_fresh_evaluation" or locked["provenance"] != provenance:
        raise ValueError("Selected experiment is incompatible with the frozen inputs or code")
    if digest(PILOT) != manifest["priorPilotSha256"]:
        raise ValueError("Original pilot exclusion file differs from V2")
    excluded_ids, excluded_families = old_exclusions(PILOT)
    previous = [case for split in ("validation", "holdout") for case in validate_file(V2_DIR, manifest, split)]
    excluded_ids.update(case["graph"]["targetTxId"] for case in previous)
    excluded_families.update(case["patternFamily"] for case in previous if case["patternFamily"])
    activity = read_activity(source, manifest)
    selection = locked["sampleSelection"]
    targets, population, sample_sizes = select_targets(
        source, selection["start"], (activity.source_end + timedelta(seconds=1)).isoformat(timespec="seconds"),
        excluded_ids, excluded_families, holdout=True, positives=selection["positives"], normals=selection["normals"],
    )
    if (len(targets) != selection["positives"] + selection["normals"]
            or any(row["txId"] in excluded_ids or row["patternId"] in excluded_families for row in targets)):
        raise ValueError("Fresh sample size or exclusion check failed")
    cases = [{"graph": source.subgraph(row["txId"]), "samplingWeight": row["samplingWeight"],
              "expectedLabel": int(row["isLaundering"]),
              "expectedPatternType": row["patternType"] or "UNMAPPED"} for row in targets]
    rows, reference = extract_rows(cases, activity, RuleConfig(**frozen["selectedConfig"]))
    data = {"phase": "fresh_evaluation_complete", "freezeSha256": digest(freeze_path), "provenance": provenance,
            "population": population, "sampleSizes": sample_sizes, "coverage": coverage(cases, rows),
            "targetIds": [row["txId"] for row in targets], "v2": reference,
            "results": {name: evaluate_policy(cases, rows, policy) for name, policy in locked["selected"].items()}}
    save(output, data)
    compact = {name: {"sample": result["sample"], "weighted": result["weighted"]}
               for name, result in {"v2": reference, **data["results"]}.items()}
    print(json.dumps({"coverage": data["coverage"], "metrics": compact}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("develop", "evaluate"))
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output file to preserve previous results")
    if args.phase == "evaluate" and args.freeze is None:
        parser.error("Evaluate requires the frozen development file")
    manifest, frozen, provenance = inputs()
    with closing(SourceIndex(INDEX)) as source:
        if args.phase == "develop":
            develop(args.output, source, manifest, frozen, provenance)
        else:
            evaluate(args.output, args.freeze, source, manifest, frozen, provenance)


if __name__ == "__main__":
    main()
