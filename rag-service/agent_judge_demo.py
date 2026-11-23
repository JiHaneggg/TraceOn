"""Run a live Judge smoke or compare strategies on labeled synthetic cases."""
import argparse
from copy import deepcopy
from itertools import combinations
import json
from pathlib import Path
from statistics import mean
import tempfile

from agent_judge import build_judge_input, openai_judge, review_item
from agent_workflow_demo import FIXTURES, fixture_tools
from corpus import canonical, digest
from judge_debate import openai_debate
from judge_decisions import openai_decisions


OUTCOMES = ("passed", "held", "rejected")
SMOKE_CASES = FIXTURES / "judge-cases.json"
HOLDOUT_CASES = FIXTURES / "judge-holdout-cases.json"
HOLDOUT_REQUEST = FIXTURES / "request-judge-holdout.json"


def benchmark_payloads(workflow, case_file, split, case_id=None):
    return build_claim_payloads(workflow, json.loads(case_file.read_bytes()), split, case_id)


def build_claim_payloads(workflow, cases, split, case_id=None):
    if not workflow["verification"]["valid"]:
        raise ValueError("Synthetic reference draft failed the code checks")
    reference = build_judge_input(workflow["identity"], workflow["context"],
                                  workflow["evidence"], workflow["report"])
    for case in cases:
        if case["split"] != split or (case_id is not None and case["caseId"] != case_id):
            continue
        payload = deepcopy(reference)
        payload["identity"].pop("reportId")
        payload["identity"]["caseId"] = case["caseId"]
        payload["draft"] = case["claim"]
        payload["items"] = [review_item(case["caseId"], case["criterion"], case["claim"])]
        yield case, payload


def evaluate_cases(workflow, judge, split, repeats, case_file=SMOKE_CASES, case_id=None, labels=None):
    """Compare strategies by replacing judge while keeping cases and scoring fixed."""
    frozen = case_file.read_bytes()
    selected = _labeled_payloads(workflow, frozen, split, case_id, labels)
    return _evaluate(workflow, judge, split, repeats, frozen, selected, labels)


def _labeled_payloads(workflow, frozen, split, case_id, labels):
    selected = [
        (case, payload, labels.get(case["caseId"]) if labels is not None else case.get("expected"))
        for case, payload in build_claim_payloads(workflow, json.loads(frozen), split, case_id)
    ]
    if not selected:
        raise ValueError("No benchmark cases match the requested split and case ID")
    if not all(label in OUTCOMES for _, _, label in selected):
        raise ValueError("Every selected case requires a valid label before any model call")
    return selected


def _evaluate(workflow, judge, split, repeats, frozen, selected, labels):
    label_source = "human_reviewed" if labels is not None else "developer_authored_synthetic"
    result = evaluate_payloads(judge, split, repeats, frozen, selected, label_source)
    identity = workflow["identity"]
    result["evidenceIdentity"] = {key: identity[key] for key in ("graphSha256", "ruleResultSha256",
                                                                "contextVersion", "contextId", "corpusSha256")}
    return result


def evaluate_payloads(judge, split, repeats, frozen, selected, label_source):
    """Measure the same outcomes for fixed payloads from one or more graphs."""
    rows = []
    confusion = {expected: dict.fromkeys((*OUTCOMES, "unscored"), 0) for expected in OUTCOMES}
    agreements, scored_pairs, total_pairs = 0, 0, 0
    for case, payload, expected in selected:
        runs = [judge(deepcopy(payload)) for _ in range(repeats)]
        scored = [run for run in runs if run["verificationScore"] is not None]
        counts = confusion[expected]
        counts["unscored"] += len(runs) - len(scored)
        for run in scored:
            counts[run["status"]] += 1
        total_pairs += len(runs) * (len(runs) - 1) // 2
        for left, right in combinations(scored, 2):
            agreements += left["status"] == right["status"]
            scored_pairs += 1
        scores = [run["verificationScore"] for run in scored]
        rows.append({"caseId": case["caseId"], "expected": expected,
                     "inputSha256": digest(canonical(payload)), "runs": runs,
                     "scoreRange": [min(scores), max(scores)] if scores else None})
    true_positive = confusion["rejected"]["rejected"]
    predicted = sum(row["rejected"] for row in confusion.values())
    expected = sum(confusion["rejected"].values())
    runs = [run for row in rows for run in row["runs"]]
    latency = [run["latencyMs"] for run in runs if run["latencyMs"] is not None]
    return {
        "schemaVersion": "1.2", "suite": "synthetic_semantic_benchmark", "split": split,
        "labelSource": label_source,
        "repeats": repeats,
        "caseFileSha256": digest(frozen), "cases": rows,
        "confusionMatrix": confusion, "rejectPrecision": true_positive / predicted if predicted else None,
        "rejectRecall": true_positive / expected if expected else None,
        "rejectF1": 2 * true_positive / (predicted + expected) if (predicted + expected) else None,
        "repeatAgreement": agreements / scored_pairs if scored_pairs else None,
        "scoredRepeatPairs": scored_pairs, "totalRepeatPairs": total_pairs,
        "unscoredRuns": sum(row["unscored"] for row in confusion.values()),
        "falseRejectPassedRuns": confusion["passed"]["rejected"],
        "correctRuns": sum(confusion[outcome][outcome] for outcome in OUTCOMES),
        "totalRuns": len(runs), "modelCallAttempts": sum(run["modelCallAttempts"] for run in runs),
        "meanLatencyMs": round(mean(latency), 1) if latency else None,
    }


def compare_cases(workflow, judges, split, repeats, case_file=SMOKE_CASES, case_id=None, labels=None):
    """Run both strategies against the same evidence and match results by case ID and repeat."""
    frozen = case_file.read_bytes()
    selected = _labeled_payloads(workflow, frozen, split, case_id, labels)
    names = ["single", "debate"] + (["decisions"] if "decisions" in judges else [])
    reports = {name: _evaluate(workflow, judges[name], split, repeats, frozen, selected, labels) for name in names}
    pairs = []
    for cases in zip(*(reports[name]["cases"] for name in names), strict=True):
        reference = cases[0]
        if any(case[key] != reference[key] for case in cases[1:] for key in ("caseId", "inputSha256", "expected")):
            raise ValueError("Strategy comparisons require identical cases and evidence")
        for repeat, runs in enumerate(zip(*(case["runs"] for case in cases), strict=True), 1):
            if len({run["model"] for run in runs}) != 1:
                raise ValueError("Strategy comparisons require the same OpenAI model")
            single, debate = runs[:2]
            if not single.get("evidenceIdSchema") or single.get("evidenceIdSchema") != debate.get("evidenceIdSchema"):
                raise ValueError("Single and debate comparisons require the same evidence ID schema")
            pairs.append({
                "caseId": reference["caseId"], "repeat": repeat, "expected": reference["expected"],
                **{name: run["status"] if run["verificationScore"] is not None else "unscored"
                   for name, run in zip(names, runs, strict=True)},
            })
    return {"schemaVersion": "1.1", "suite": "synthetic_judge_comparison",
            "strategies": reports, "pairedRuns": pairs}


def label_packet(workflow):
    cases = [{"caseId": case["caseId"], "input": payload}
             for case, payload in benchmark_payloads(workflow, HOLDOUT_CASES, "holdout")]
    packet_hash = digest(canonical(cases))
    return {"suite": "synthetic_judge_label_packet", "packetSha256": packet_hash,
            "labels": {row["caseId"]: {"expected": None, "reason": ""} for row in cases}, "cases": cases}


def reviewed_labels(workflow, path):
    return read_reviewed_labels(label_packet(workflow), path)


def read_reviewed_labels(expected, path):
    content = path.read_bytes()
    saved = json.loads(content)
    if (saved.get("packetSha256") != expected["packetSha256"]
            or canonical(saved.get("cases")) != canonical(expected["cases"])):
        raise ValueError("Label packet does not match the current holdout evidence")
    labels = saved.get("labels")
    if not isinstance(labels, dict) or set(labels) != set(expected["labels"]):
        raise ValueError("Complete the label packet for every holdout case")
    for label in labels.values():
        if (not isinstance(label, dict) or label.get("expected") not in OUTCOMES
                or not isinstance(label.get("reason"), str) or not label["reason"].strip()):
            raise ValueError("Each holdout case requires a human verdict and a reason")
    return {case_id: label["expected"] for case_id, label in labels.items()}, digest(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("workflow", "benchmark", "compare", "label-packet"), default="workflow")
    parser.add_argument("--strategy", choices=("single", "debate", "decisions"), default="single")
    parser.add_argument("--include-decisions", action="store_true", help="Include the Decisions API in --suite compare")
    parser.add_argument("--split", choices=("development", "evaluation", "holdout"), default="development")
    parser.add_argument("--case-id", help="Run one case in the selected benchmark split")
    parser.add_argument("--labels", type=Path, help="Holdout label packet completed by a human")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output exists; choose a different path")
    if args.include_decisions and args.suite != "compare":
        parser.error("--include-decisions requires --suite compare")
    if args.strategy == "decisions" and args.suite != "benchmark":
        parser.error("--strategy decisions requires --suite benchmark. Use --include-decisions for a comparison")
    if args.case_id is not None and args.suite not in {"benchmark", "compare"}:
        parser.error("--case-id requires --suite benchmark or compare")
    if args.suite == "label-packet" and args.split != "holdout":
        parser.error("--suite label-packet requires --split holdout")
    if args.split == "holdout" and args.suite in {"benchmark", "compare"} and args.labels is None:
        parser.error("Holdout evaluation requires a completed --labels packet")
    if args.labels is not None and (args.split != "holdout" or args.suite not in {"benchmark", "compare"}):
        parser.error("--labels is for holdout benchmark or compare runs")
    if args.split == "holdout" and args.suite == "workflow":
        parser.error("--split holdout requires a benchmark, compare or label-packet suite")
    judges = {"single": openai_judge, "debate": openai_debate}
    if args.strategy == "decisions" or args.include_decisions:
        judges["decisions"] = openai_decisions
    with tempfile.TemporaryDirectory() as temp:
        request_file = HOLDOUT_REQUEST if args.split == "holdout" else FIXTURES / "request.json"
        tools, request = fixture_tools(temp, request_file)
        if args.suite == "workflow":
            judge_mode = "debate" if args.strategy == "debate" else "openai"
            runs = [tools.call("run_review_workflow", {**request, "judgeMode": judge_mode})
                    for _ in range(args.repeats)]
            outcome = {"suite": "synthetic_workflow", "runs": runs}
            reviews = [run.get("judge", {}) for run in runs]
        else:
            workflow = tools.call("run_review_workflow", request)
            try:
                if args.suite == "label-packet":
                    outcome, reviews = label_packet(workflow), []
                else:
                    labels, label_hash = reviewed_labels(workflow, args.labels) if args.labels else (None, None)
                    options = {"case_file": HOLDOUT_CASES if labels is not None else SMOKE_CASES,
                               "case_id": args.case_id, "labels": labels}
                    if args.suite == "compare":
                        outcome = compare_cases(workflow, judges, args.split, args.repeats, **options)
                        reports = outcome["strategies"].values()
                    else:
                        outcome = evaluate_cases(workflow, judges[args.strategy], args.split, args.repeats, **options)
                        reports = [outcome]
                    if label_hash is not None:
                        outcome["labelFileSha256"] = label_hash
                    reviews = [{"caseId": case["caseId"], **run} for report in reports
                               for case in report["cases"] for run in case["runs"]]
            except (ValueError, OSError) as error:
                parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(outcome, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({
        "suite": outcome["suite"], "output": str(args.output.resolve()),
        "results": [{key: review[key] for key in
                     ("caseId", "strategy", "strategyVersion", "status", "reasonCode", "verificationScore", "modelCallAttempts",
                      "providerHttpStatus", "providerErrorCode", "responseErrorCode") if key in review} for review in reviews],
    }, ensure_ascii=False))
    completed = reviews and all(review.get("verificationScore") is not None for review in reviews)
    return 0 if completed or args.suite == "label-packet" else 2


if __name__ == "__main__":
    raise SystemExit(main())
