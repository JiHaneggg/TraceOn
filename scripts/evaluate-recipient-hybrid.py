"""Fit hybrid comparisons on grouped V2 development cases, then assess a fresh sample."""
import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / directory) for directory in ("data-pipeline", "rule-service", "ml-service")]
from evaluation_index import SourceIndex
from experiment import digest, summarize, validate_file, weighted_metrics
from prepare_evaluation import SEED, old_exclusions, select_targets
from recipient_activity import RecipientActivity
from recipient_hybrid import FEATURE_SETS, ML_WEIGHT, combined_score, features, grouped_folds, train
from rules import RuleConfig, score

V2 = ROOT / "data/evaluation/HI-Small-v2"
INDEX = ROOT / "data/evaluation/HI-Small-source.sqlite3"
FROZEN_V2 = ROOT / "artifacts/rule-v2-frozen.json"
PILOT = ROOT / "data/processed/HI-Small-MVP/transactions.csv"
PREVIOUS = ROOT / "artifacts/recipient-activity-fresh-20261123.json"
EXPLORATORY_FPR_CAP = 0.50


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def runs():
    manifest = json.loads((V2 / "manifest.json").read_bytes())
    frozen = json.loads(FROZEN_V2.read_bytes())
    previous = json.loads(PREVIOUS.read_bytes())
    if (digest(FROZEN_V2) != FROZEN_V2.with_suffix(".json.sha256").read_text(encoding="ascii").strip()
            or digest(V2 / "manifest.json") != frozen["manifestSha256"]
            or any(manifest["files"][name]["sha256"] != frozen[name + "Sha256"]
                   for name in ("validation", "holdout"))
            or any(previous["provenance"][key] != value for key, value in
                   (("v2FrozenSha256", digest(FROZEN_V2)), ("sourceSha256", manifest["sourceSha256"])))
            or len(set(previous["targetIds"])) != len(previous["targetIds"]) or len(previous["targetIds"]) != 250):
        raise ValueError("Prior evaluation inputs do not match V2 and the recipient activity run")
    fingerprints = {name: digest(ROOT / name) for name in (
        "scripts/evaluate-recipient-hybrid.py", "ml-service/recipient_hybrid.py",
        "data-pipeline/recipient_activity.py", "data-pipeline/prepare_evaluation.py", "rule-service/rules.py",
    )}
    if fingerprints["rule-service/rules.py"] != frozen["engineSha256"]:
        raise ValueError("Rule engine differs from the V2 seal")
    provenance = {"v2FrozenSha256": digest(FROZEN_V2), "sourceSha256": manifest["sourceSha256"],
                  "previousEvaluationSha256": digest(PREVIOUS), "codeSha256": fingerprints}
    return manifest, frozen, previous["targetIds"], provenance


def extract_features(cases, activity, config):
    feature_rows, recipients, rules = [], [], []
    for case in cases:
        graph = case["graph"]
        target = next(row for row in graph["edges"] if row["txId"] == graph["targetTxId"])
        profile = activity.compare(target["txId"], expected_target=target)
        feature_rows.append(features(graph, target, profile))
        recipients.append(target["toAccountKey"])
        rules.append(score(graph, config))
    return feature_rows, recipients, rules


def outcomes(values, threshold):
    return [{"flagged": value >= threshold} for value in values]


def threshold_candidates(cases, scores, target_recall):
    possible = [(cut, weighted_metrics(cases, outcomes(scores, cut))) for cut in sorted(set(scores))]
    recall_cut, _ = min((row for row in possible if row[1]["recall"] >= target_recall),
                       key=lambda row: (row[1]["falsePositiveRate"], -row[1]["recall"], -row[0]))
    budget = max((row for row in possible if row[1]["falsePositiveRate"] <= EXPLORATORY_FPR_CAP),
                 key=lambda row: (row[1]["recall"], -row[1]["falsePositiveRate"], row[0]), default=None)
    return {"recallTarget": recall_cut, **({"fprBudget": budget[0]} if budget else {})}


def summarize_with_history(cases, predictions, feature_rows):
    result = summarize(cases, predictions)
    result["byHistoryAvailability"] = {}
    for flag, name in ((True, "available"), (False, "unavailable")):
        positions = [i for i, row in enumerate(feature_rows) if row["historyAvailable"] is flag]
        if positions:
            subset = summarize([cases[i] for i in positions], [predictions[i] for i in positions])
            result["byHistoryAvailability"][name] = {"sample": subset["sample"], "weighted": subset["weighted"]}
    return result


def develop(output, manifest, frozen, provenance, source, activity):
    cases = validate_file(V2, manifest, "validation")
    config = RuleConfig(**frozen["selectedConfig"])
    rows, recipients, rules = extract_features(cases, activity, config)
    reference = summarize(cases, rules)
    if reference != frozen["validation"]["selected"]:
        raise ValueError("Development rule predictions differ from V2")
    folds = grouped_folds(cases, recipients)
    all_indices = set(range(len(cases)))
    variants = {}
    for name, feature_names in FEATURE_SETS.items():
        out_of_fold = [None] * len(cases)
        for fold in folds:
            fit = sorted(all_indices - set(fold))
            model = train([rows[i] for i in fit], [cases[i] for i in fit], feature_names)
            for index in fold:
                out_of_fold[index] = combined_score(model, rows[index], rules[index]["ruleScore"])
        cuts = threshold_candidates(cases, out_of_fold, frozen["targetRecall"])
        variants[name] = {"model": train(rows, cases, feature_names), "thresholds": cuts, "outOfFold": {
            strategy: summarize_with_history(cases, outcomes(out_of_fold, cut), rows)
            for strategy, cut in cuts.items()
        }}
    locked = {
        "phase": "frozen_before_hybrid_fresh_sample", "provenance": provenance, "mlWeight": ML_WEIGHT,
        "targetRecall": frozen["targetRecall"], "exploratoryFprCap": EXPLORATORY_FPR_CAP,
        "foldSizes": [len(group) for group in folds],
        "foldPositiveCounts": [sum(cases[i]["expectedLabel"] for i in group) for group in folds],
        "samplePolicy": {"from": manifest["timeCutoffs"]["holdoutStart"], "positives": 100, "normals": 150,
                         "seed": SEED, "exclusions": ["pilot", "v2_validation", "v2_holdout", "recipient_activity"]},
        "v2": summarize_with_history(cases, rules, rows), "variants": variants,
    }
    save(output, locked)
    print(json.dumps({"trainCases": len(cases), "foldSizes": locked["foldSizes"],
                      "variants": {name: v["thresholds"] for name, v in variants.items()}}, ensure_ascii=False), flush=True)


def excluded_targets(manifest, previous_ids, source):
    if digest(PILOT) != manifest["priorPilotSha256"]:
        raise ValueError("Pilot data differs from the V2 source")
    ids, families = old_exclusions(PILOT)
    for name in ("validation", "holdout"):
        for case in validate_file(V2, manifest, name):
            ids.add(case["graph"]["targetTxId"])
            if case["patternFamily"]:
                families.add(case["patternFamily"])
    placeholders = ",".join("?" for _ in previous_ids)
    prior = list(source.db.execute(f"SELECT txId,patternId FROM events WHERE txId IN ({placeholders})", previous_ids))
    if len(prior) != len(previous_ids):
        raise ValueError("Previously evaluated target is missing from the source index")
    ids.update(id_ for id_, _ in prior)
    families.update(family for _, family in prior if family)
    return ids, families


def evaluate(output, freeze, manifest, frozen, previous_ids, provenance, source, activity):
    locked = json.loads(freeze.read_bytes())
    if locked["phase"] != "frozen_before_hybrid_fresh_sample" or locked["provenance"] != provenance:
        raise ValueError("Model settings belong to different source, rules or feature code")
    ids, families = excluded_targets(manifest, previous_ids, source)
    policy = locked["samplePolicy"]
    targets, population, sample_sizes = select_targets(
        source, policy["from"], (activity.source_end + timedelta(seconds=1)).isoformat(timespec="seconds"),
        ids, families, holdout=True, positives=policy["positives"], normals=policy["normals"],
    )
    if (len(targets) != policy["positives"] + policy["normals"]
            or any(row["txId"] in ids or row["patternId"] in families for row in targets)):
        raise ValueError("Fresh evaluation target or family exclusion failed")
    cases = [{"graph": source.subgraph(row["txId"]), "expectedLabel": int(row["isLaundering"]),
              "expectedPatternType": row["patternType"] or "UNMAPPED", "samplingWeight": row["samplingWeight"]}
             for row in targets]
    rows, _, rules = extract_features(cases, activity, RuleConfig(**frozen["selectedConfig"]))
    metrics = {"ruleOnly": summarize_with_history(cases, rules, rows)}
    for name, info in locked["variants"].items():
        scores = [combined_score(info["model"], row, rule["ruleScore"]) for row, rule in zip(rows, rules)]
        for strategy, cut in info["thresholds"].items():
            metrics[name + ":" + strategy] = summarize_with_history(cases, outcomes(scores, cut), rows)
    result = {"phase": "recipient_hybrid_evaluated", "freezeSha256": digest(freeze), "provenance": provenance,
              "population": population, "sampleSizes": sample_sizes, "targetIds": [row["txId"] for row in targets],
              "excludedTargetCount": len(ids), "excludedFamilyCount": len(families), "metrics": metrics}
    save(output, result)
    print(json.dumps({name: {"tp": stats["sample"]["tp"], "fp": stats["sample"]["fp"],
                            "recall": round(stats["weighted"]["recall"], 5),
                            "falsePositiveRate": round(stats["weighted"]["falsePositiveRate"], 5)}
                      for name, stats in metrics.items()}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("develop", "evaluate"))
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or (args.phase == "evaluate" and args.freeze is None):
        parser.error("Use a new output file, and supply a frozen model file for evaluation")
    manifest, frozen, previous_ids, provenance = runs()
    with closing(SourceIndex(INDEX)) as source:
        if dict(source.db.execute("SELECT key,value FROM metadata"))["sourceSha256"] != manifest["sourceSha256"]:
            raise ValueError("Source index differs from the prepared V2 data")
        activity = RecipientActivity(source)
        if args.phase == "develop":
            develop(args.output, manifest, frozen, provenance, source, activity)
        else:
            evaluate(args.output, args.freeze, manifest, frozen, previous_ids, provenance, source, activity)


if __name__ == "__main__":
    main()
