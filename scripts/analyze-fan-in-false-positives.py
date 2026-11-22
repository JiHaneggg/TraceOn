"""Diagnose V2 FAN_IN alerts and compare prospective gates on validation only."""
import argparse
from collections import Counter
from datetime import timedelta
from itertools import product
import json
from pathlib import Path
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rule-service"))
from experiment import digest, summarize, validate_file
from rules import RuleConfig, parse_time, score

WINDOWS = (60, 360, 720, 1440, 4320, 10080)
MIN_SENDERS = (2, 3, 4, 5, 6, 8, 12)
MODES = ("all", "target_currency", "not_observed_earlier")


def load_validation():
    folder = ROOT / "data/evaluation/HI-Small-v2"
    manifest_path = folder / "manifest.json"
    frozen_path = ROOT / "artifacts/rule-v2-frozen.json"
    manifest = json.loads(manifest_path.read_bytes())
    frozen = json.loads(frozen_path.read_bytes())
    frozen_hash = digest(frozen_path)
    if (frozen_hash != frozen_path.with_suffix(".json.sha256").read_text(encoding="ascii").strip()
            or frozen["phase"] != "frozen_before_holdout"
            or digest(manifest_path) != frozen["manifestSha256"]
            or manifest["files"]["validation"]["sha256"] != frozen["validationSha256"]
            or digest(ROOT / "rule-service/rules.py") != frozen["engineSha256"]):
        raise ValueError("Validation inputs differ from the frozen V2 record")
    return validate_file(folder, manifest, "validation"), frozen, frozen_hash


def sender_counts(inbound, target, as_of):
    dated = [(parse_time(edge["timestamp"]), edge) for edge in inbound]
    counts = {}
    for window in WINDOWS:
        start = as_of - timedelta(minutes=window)
        recent = [edge for when, edge in dated if when >= start]
        earlier = {edge["fromAccountKey"] for when, edge in dated if when < start}
        counts[("all", window)] = len({edge["fromAccountKey"] for edge in recent})
        counts[("target_currency", window)] = len({
            edge["fromAccountKey"] for edge in recent if edge["receivingCurrency"] == target["receivingCurrency"]
        })
        # This is observed history inside the bounded graph. Preserve the rule's target membership.
        counts[("not_observed_earlier", window)] = (
            len({edge["fromAccountKey"] for edge in recent} - earlier)
            if target["fromAccountKey"] not in earlier else 0
        )
    return counts


def analyze_case(case, output):
    graph = case["graph"]
    edges = {edge["txId"]: edge for edge in graph["edges"]}
    target = edges[graph["targetTxId"]]
    fan_in = next((item for item in output["findings"] if item["ruleId"] == "FAN_IN"), None)
    rules = sorted(item["ruleId"] for item in output["findings"])
    result = {"rules": rules, "flagged": output["flagged"],
              "otherFlagged": any(item["ruleId"] != "FAN_IN" and item["score"] >= output["config"]["threshold"]
                                  for item in output["findings"]),
              "recipient": target["toAccountKey"], "graphTruncated": graph["meta"]["truncated"]}
    if fan_in:
        inbound = [edges[id_] for id_ in fan_in["evidenceTxIds"]]
        as_of = parse_time(graph["asOf"])
        result.update(
            counts=sender_counts(inbound, target, as_of),
            spanMinutes=(as_of - min(parse_time(edge["timestamp"]) for edge in inbound)).total_seconds() / 60,
            inboundTransactions=len(inbound),
            currencies=len({edge["receivingCurrency"] for edge in inbound}),
        )
        if result["counts"][("all", 10080)] != fan_in["facts"]["counterpartyCount"]:
            raise ValueError("Features differ from the real seven-day rule evidence")
    return result


def fan_in_profile(rows):
    relevant = [row for row in rows if "FAN_IN" in row["rules"]]
    if not relevant:
        return {"cases": 0}
    recipients = Counter(row["recipient"] for row in relevant)
    return {
        "cases": len(relevant),
        "fanInOnly": sum(row["rules"] == ["FAN_IN"] for row in relevant),
        "ruleCombinations": dict(sorted(Counter("+".join(row["rules"]) for row in relevant).items())),
        "graphTruncated": sum(row["graphTruncated"] for row in relevant),
        "distinctRecipients": len(recipients),
        "mostCasesForOneRecipient": max(recipients.values()),
        "multiCurrency": sum(row["currencies"] > 1 for row in relevant),
        "medianSpanMinutes": median(row["spanMinutes"] for row in relevant),
        "medianInboundTransactions": median(row["inboundTransactions"] for row in relevant),
        "sevenDaySenderHistogram": dict(sorted(Counter(row["counts"][("all", 10080)] for row in relevant).items())),
        "belowTwoSendersByWindow": {str(window): sum(row["counts"][("all", window)] < 2 for row in relevant)
                                   for window in WINDOWS},
    }


def predictions(rows, mode, window, minimum):
    return [{"flagged": row["otherFlagged"] or row.get("counts", {}).get((mode, window), 0) >= minimum}
            for row in rows]


def best(rows, *, recall=None, false_positive_rate=None):
    eligible = [r for r in rows if (recall is None or r["weighted"]["recall"] >= recall)
                and (false_positive_rate is None or r["weighted"]["falsePositiveRate"] <= false_positive_rate)]
    if not eligible:
        return []
    if recall is not None:
        criterion = lambda row: (row["weighted"]["falsePositiveRate"], -row["weighted"]["recall"])
    else:
        criterion = lambda row: (-row["weighted"]["recall"], row["weighted"]["falsePositiveRate"])
    rank = min(criterion(row) for row in eligible)
    return [row["settings"] for row in eligible if criterion(row) == rank]


def run(output):
    cases, frozen, frozen_hash = load_validation()
    config = RuleConfig(**frozen["selectedConfig"])
    if config.window_minutes != 10080 or config.minimum("FAN_IN") != 2 or config.threshold != 65:
        raise ValueError("Expected the frozen V2 FAN_IN research candidate")
    outputs = [score(case["graph"], config) for case in cases]
    rows = [analyze_case(case, out) for case, out in zip(cases, outputs)]
    reference = summarize(cases, outputs)
    if reference != frozen["validation"]["selected"] or [row["flagged"] for row in predictions(rows, "all", 10080, 2)] != [out["flagged"] for out in outputs]:
        raise ValueError("Reference/identity gate must reproduce the frozen validation results")

    series = []
    for mode, window, minimum in product(MODES, WINDOWS, MIN_SENDERS):
        if mode == "not_observed_earlier" and window == 10080:
            continue
        simulated = predictions(rows, mode, window, minimum)
        if any(new["flagged"] and not old["flagged"] for new, old in zip(simulated, outputs)):
            raise ValueError("A stricter FAN_IN gate cannot introduce a new alert")
        series.append({"settings": {"mode": mode, "minutes": window, "minSenders": minimum},
                       **summarize(cases, simulated)})

    normals = [row for case, row in zip(cases, rows) if case["expectedLabel"] == 0 and row["flagged"]]
    positives = [row for case, row in zip(cases, rows) if case["expectedLabel"] == 1 and row["flagged"]]
    report = {
        "scope": "validation_only_exploratory_diagnostics",
        "source": {"frozenSha256": frozen_hash, "validationSha256": frozen["validationSha256"],
                   "ruleEngineSha256": frozen["engineSha256"]},
        "reference": {**reference, "settings": frozen["selectedConfig"]},
        "falsePositivesWithFanIn": fan_in_profile(normals),
        "truePositivesWithFanIn": fan_in_profile(positives),
        "selection": {
            "atV2RecallTarget": best(series, recall=frozen["targetRecall"]),
            "atFpr20Percent": best(series, false_positive_rate=0.20),
            "atFpr30Percent": best(series, false_positive_rate=0.30),
            "atFpr50Percent": best(series, false_positive_rate=0.50),
        },
        "sweep": series,
        "holdoutCasesUsed": 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
    print(json.dumps({
        "scenarios": len(series), "referenceTP": reference["sample"]["tp"], "referenceFP": reference["sample"]["fp"],
        "falsePositivesWithFanIn": report["falsePositivesWithFanIn"]["cases"], "selection": report["selection"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
