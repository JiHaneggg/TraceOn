"""Tune candidate rankings under equal review budgets and freeze a new evaluation."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
import sys


detector = import_module("evaluate-transaction-detection")
baseline = detector.baseline
POLICY = "traceon-equal-review-budget-v1"
BUDGETS = (125, 150, 175, 198)
PRIMARY_BUDGET = 175
WEIGHTS = tuple(range(0, 101, 5))
BASES = ("rule", "ruleMl")
TIE_SEED = "traceon-detection-weights-v1"


def read_results(packet, result_file):
    result = json.loads(result_file.read_bytes())
    rows = result["decisions"]
    cases = packet["cases"]
    if (result.get("suite") != "ibm_aml_candidate_comparison" or result.get("mode") != "openai"
            or result.get("packetSha256") != packet["packetSha256"] or len(rows) != len(cases)
            or not detector.matches_case_order(rows, cases)):
        raise ValueError("A real result for the identical complete packet is required")
    model_versions = {(row["model"], row["detectorVersion"]) for row in rows}
    if len(model_versions) != 1:
        raise ValueError("Rankings require one pinned model and detector version")
    model, version = model_versions.pop()
    for case, row in zip(cases, rows, strict=True):
        if row["decision"] != "unscored":
            checked = detector.validate_decision(case["input"], {
                key: row[key] for key in ("decision", "reason", "evidenceTxIds")
            })
            if row["flagged"] is not checked["flagged"]:
                raise ValueError("Saved candidate flag conflicts with the verified decision")
        elif row["flagged"] is not None:
            raise ValueError("An unscored decision must not carry a candidate flag")
    return rows, {"model": model, "detectorVersion": version}


def development_signals(packet, freeze, evaluation):
    if (packet["baselineFreezeSha256"] != baseline.digest(freeze)
            or packet["evaluationSha256"] != baseline.digest(evaluation) or packet["modelGraphScope"] != "2hop"):
        raise ValueError("The development packet must match the original 2-hop comparison")
    cases, rules, ml_values, predictions, _, _, _ = detector.replay_module.replay(freeze, evaluation)
    for index, (live, case, rule) in enumerate(zip(cases, packet["cases"], rules, strict=True)):
        if (live["graph"]["targetTxId"] != case["graph"]["targetTxId"]
                or detector.digest(detector.canonical(live["graph"])) != case["graphScopes"]["2hop"]["graphSha256"]
                or any(live[key] != case[key] for key in ("expectedLabel", "expectedPatternType", "samplingWeight"))
                or rule["flagged"] != case["ruleFlagged"]
                or predictions["rule-ml"][index]["flagged"] != case["ruleMlFlagged"]):
            raise ValueError("Frozen scores or source graph differ from development input")
    return [{"rule": row["ruleScore"], "ruleMl": value} for row, value in zip(rules, ml_values, strict=True)]


def rank_inputs(cases, signals, decisions):
    tie = [detector.digest(TIE_SEED + ":" + case["graph"]["targetTxId"]) for case in cases]
    if len(set(tie)) != len(tie):
        raise ValueError("Repeated target transaction")
    rule = [Decimal(str(row["rule"])) / 100 for row in signals]
    rule_ml = [Decimal(str(row["ruleMl"])) for row in signals]
    votes = [int(row["flagged"] is True) for row in decisions]
    union = [int(case["ruleFlagged"] or vote) for case, vote in zip(cases, votes, strict=True)]
    if not all(value.is_finite() and 0 <= value <= 1 for value in rule + rule_ml):
        raise ValueError("Detection priority input is out of range")
    return tie, {"rule": rule, "ruleMl": rule_ml, "agent": votes, "ruleOrAgent": union}


def top_k(cases, scores, tie, budget):
    if not 1 <= budget <= len(cases) or len(cases) != len(scores) or len(cases) != len(tie):
        raise ValueError("Review budget and rankings must match the full evaluation sample")
    order = sorted(range(len(scores)), key=lambda position: (-scores[position], tie[position]))
    picked = set(order[:budget])
    stats = detector.metrics(cases, [position in picked for position in range(len(cases))])
    cutoff = scores[order[budget - 1]]
    return {
        "recall": stats["weighted"]["recall"], "falsePositiveRate": stats["weighted"]["falsePositiveRate"],
        "tp": stats["sample"]["tp"], "fp": stats["sample"]["fp"], "fn": stats["sample"]["fn"],
        "reviewCount": stats["reviewCount"], "cutoffTieCount": scores.count(cutoff),
    }


def blend(base, votes, rule_weight):
    """Mix scores in [0,1] with integer percent weights."""
    return [rule_weight * value + (100 - rule_weight) * vote
            for value, vote in zip(base, votes, strict=True)]


def choice_key(row):
    return (-row["recall"], row["falsePositiveRate"], abs(row["ruleWeight"] - 50),
            row["base"], row["ruleWeight"])


def tune(packet, decisions, signals, model):
    cases = packet["cases"]
    tie, inputs = rank_inputs(cases, signals, decisions)
    comparisons = {}
    for budget in BUDGETS:
        rows = [
            {"base": base, "ruleWeight": weight,
             **top_k(cases, blend(inputs[base], inputs["agent"], weight), tie, budget)}
            for base in BASES for weight in WEIGHTS
        ]
        mixed = [row for row in rows if 0 < row["ruleWeight"] < 100]
        comparisons[str(budget)] = {
            "baselines": {name: top_k(cases, score, tie, budget) for name, score in inputs.items()},
            "grid": rows, "selectedHybrid": min(mixed, key=choice_key),
        }
    return {
        "suite": "ibm_aml_weight_exploration", "rankingPolicy": POLICY, "tieSeed": TIE_SEED,
        "modelConfig": model, "primaryBudget": PRIMARY_BUDGET,
        "baselineFreezeSha256": packet["baselineFreezeSha256"],
        "developmentPacketSha256": packet["packetSha256"], "caseCount": len(cases),
        "developmentTargetIds": [case["graph"]["targetTxId"] for case in cases],
        "selected": comparisons[str(PRIMARY_BUDGET)]["selectedHybrid"], "budgets": comparisons,
    }


def sample_ranked_holdout(freeze_path, development_ids):
    """Sample and score holdout transactions, excluding targets and known pattern families."""
    freeze = json.loads(freeze_path.read_bytes())
    manifest, old_rule, old_ids, provenance = baseline.runs()
    if freeze["provenance"] != provenance:
        raise ValueError("Frozen score model belongs to a different source")
    policy = freeze["samplePolicy"]
    config = baseline.RuleConfig(**old_rule["selectedConfig"])
    ml_variant = freeze["variants"][detector.replay_module.ML_VARIANT]
    with closing(baseline.SourceIndex(baseline.INDEX)) as source:
        source.db.execute("BEGIN")
        if dict(source.db.execute("SELECT key,value FROM metadata"))["sourceSha256"] != provenance["sourceSha256"]:
            raise ValueError("Evaluation index has a different source")
        activity = baseline.RecipientActivity(source)
        excluded_ids, excluded_families = baseline.excluded_targets(manifest, old_ids, source)
        slots = ",".join("?" for _ in development_ids)
        prior = list(source.db.execute(f"SELECT txId,patternId FROM events WHERE txId IN ({slots})", development_ids))
        if len(prior) != len(development_ids):
            raise ValueError("Development target is missing from the evaluation source")
        excluded_ids.update(tx_id for tx_id, _ in prior)
        excluded_families.update(family for _, family in prior if family)
        targets, population, strata = baseline.select_targets(
            source, policy["from"], (activity.source_end + timedelta(seconds=1)).isoformat(timespec="seconds"),
            excluded_ids, excluded_families, holdout=True, positives=policy["positives"], normals=policy["normals"],
        )
        if (len(targets) != policy["positives"] + policy["normals"]
                or any(target["txId"] in excluded_ids or target["patternId"] in excluded_families for target in targets)):
            raise ValueError("Independent evaluation sample was not fully separated")
        cases = []
        for target in targets:
            graph = source.subgraph(target["txId"])
            rules = baseline.score(graph, config)
            edge = next(row for row in graph["edges"] if row["txId"] == graph["targetTxId"])
            profile = activity.compare(edge["txId"], expected_target=edge)
            value = baseline.combined_score(ml_variant["model"], baseline.features(graph, edge, profile),
                                            rules["ruleScore"])
            observations = detector.describe(graph, rules)
            cases.append({
                "graph": {"targetTxId": graph["targetTxId"], "asOf": graph["asOf"],
                          "meta": {"truncated": graph["meta"]["truncated"]}},
                "expectedLabel": int(target["isLaundering"]), "expectedPatternType": target["patternType"] or "UNMAPPED",
                "samplingWeight": target["samplingWeight"], "input": detector.agent_input(graph, observations),
                "ruleFlagged": rules["flagged"], "ruleMlFlagged": value >= ml_variant["thresholds"]["recallTarget"],
                "rankSignals": {"rule": rules["ruleScore"], "ruleMl": value},
            })
    return {"sampleSizes": strata, "population": population, "excludedTargetCount": len(excluded_ids),
            "excludedFamilyCount": len(excluded_families), "cases": cases}


def prepare_evaluation(lock_path, freeze_path):
    lock = json.loads(lock_path.read_bytes())
    if lock["rankingPolicy"] != POLICY or lock["baselineFreezeSha256"] != baseline.digest(freeze_path):
        raise ValueError("Weight selection does not match the frozen candidate model")
    development_ids = lock["developmentTargetIds"]
    if len(development_ids) != lock["caseCount"] or len(set(development_ids)) != len(development_ids):
        raise ValueError("The development target list changed")
    sample = sample_ranked_holdout(freeze_path, development_ids)
    packet = {
        "schemaVersion": detector.SCHEMA_VERSION, "suite": "ibm_aml_frozen_detection_packet", "modelGraphScope": "2hop",
        "baselineFreezeSha256": lock["baselineFreezeSha256"], "weightLockSha256": baseline.digest(lock_path),
        "developmentPacketSha256": lock["developmentPacketSha256"], **sample,
    }
    return {**packet, "packetSha256": detector.digest(detector.canonical(packet))}


def evaluate(packet, decisions, model, lock_path):
    lock = json.loads(lock_path.read_bytes())
    expected = {
        "weightLockSha256": baseline.digest(lock_path), "baselineFreezeSha256": lock["baselineFreezeSha256"],
        "developmentPacketSha256": lock["developmentPacketSha256"],
    }
    cases = packet["cases"]
    if (any(packet.get(key) != value for key, value in expected.items()) or model != lock["modelConfig"]
            or lock["rankingPolicy"] != POLICY or detector.DETECTOR_VERSION != model["detectorVersion"]
            or {case["graph"]["targetTxId"] for case in cases}.intersection(lock["developmentTargetIds"])):
        raise ValueError("Evaluation does not match the same model and independent weight lock")
    tie, inputs = rank_inputs(cases, [case["rankSignals"] for case in cases], decisions)
    results = {}
    for budget in BUDGETS:
        policy = lock["budgets"][str(budget)]["selectedHybrid"]
        scores = blend(inputs[policy["base"]], inputs["agent"], policy["ruleWeight"])
        results[str(budget)] = {"baselines": {key: top_k(cases, values, tie, budget) for key, values in inputs.items()},
                                "lockedHybrid": {"base": policy["base"], "ruleWeight": policy["ruleWeight"],
                                                 **top_k(cases, scores, tie, budget)}}
    return {
        "suite": "ibm_aml_weight_holdout", "caseCount": len(cases), "rankingPolicy": POLICY,
        "graphScope": packet["modelGraphScope"], "packetSha256": packet["packetSha256"], **expected,
        "modelConfig": model, "decisionCounts": dict(Counter(row["decision"] for row in decisions)),
        "primaryBudget": lock["primaryBudget"], "budgets": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    calibration = phases.add_parser("tune")
    holdout = phases.add_parser("prepare-evaluation")
    final = phases.add_parser("evaluate")
    for phase in (calibration, holdout, final):
        phase.add_argument("--output", type=Path, required=True)
    for phase in (calibration, final):
        phase.add_argument("--packet", type=Path, required=True)
        phase.add_argument("--result", type=Path, required=True)
    for phase in (calibration, holdout):
        phase.add_argument("--freeze", type=Path, required=True)
    for phase in (holdout, final):
        phase.add_argument("--lock", type=Path, required=True)
    calibration.add_argument("--evaluation", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output name")
    if args.phase == "prepare-evaluation":
        result = prepare_evaluation(args.lock, args.freeze)
        summary = {"caseCount": len(result["cases"]), "sampleSizes": result["sampleSizes"]}
    else:
        packet = detector.load_packet(args.packet)
        rows, model = read_results(packet, args.result)
        if args.phase == "tune":
            signals = development_signals(packet, args.freeze, args.evaluation)
            result = tune(packet, rows, signals, model)
            summary = {key: result[key] for key in ("caseCount", "primaryBudget", "selected")}
        else:
            result = evaluate(packet, rows, model, args.lock)
            summary = {"caseCount": result["caseCount"], "results": result["budgets"][str(result["primaryBudget"])]}
    baseline.save(args.output, result)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
