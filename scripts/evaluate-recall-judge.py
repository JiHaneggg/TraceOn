"""Replay frozen candidate scores and review fixed AML examples with the existing Judge."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import timedelta
from importlib import import_module
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag-service"))
from agent_judge import decide, judge_configuration, openai_judge
from agent_tools import AgentTools
from agent_workflow import run_review_workflow
from corpus import ALLOWED_HOSTS, DEFAULT_INDEX
from retrieval import Index

# Reuse the evaluator in place so its hash in the frozen model stays valid.
baseline = import_module("evaluate-recipient-hybrid")
ML_VARIANT = "graphWithAvailabilityAndDelta"
GATES = ("rule", "rule-ml")
PANEL = (("CYCLE", "supported"), ("FAN-IN", "insufficient"), ("normal", "contradicted"))


def replay(freeze_path, evaluation_path):
    manifest, frozen, prior_ids, provenance = baseline.runs()
    freeze = json.loads(freeze_path.read_bytes())
    evaluation = json.loads(evaluation_path.read_bytes())
    if (freeze["phase"] != "frozen_before_hybrid_fresh_sample"
            or evaluation["phase"] != "recipient_hybrid_evaluated"
            or baseline.digest(freeze_path) != evaluation["freezeSha256"]
            or freeze["provenance"] != provenance or evaluation["provenance"] != provenance
            or freeze["targetRecall"] != frozen["targetRecall"]):
        raise ValueError("Evaluation does not match the frozen candidate models and source")

    with closing(baseline.SourceIndex(baseline.INDEX)) as source:
        metadata = dict(source.db.execute("SELECT key,value FROM metadata"))
        if metadata["sourceSha256"] != manifest["sourceSha256"]:
            raise ValueError("Source index differs from the frozen evaluation")
        activity = baseline.RecipientActivity(source)
        exclusions, families = baseline.excluded_targets(manifest, prior_ids, source)
        policy = freeze["samplePolicy"]
        targets, population, sizes = baseline.select_targets(
            source, policy["from"],
            (activity.source_end + timedelta(seconds=1)).isoformat(timespec="seconds"),
            exclusions, families, holdout=True, positives=policy["positives"], normals=policy["normals"],
        )
        ids = [target["txId"] for target in targets]
        if (ids != evaluation["targetIds"] or len(set(ids)) != policy["positives"] + policy["normals"]
                or population != evaluation["population"] or sizes != evaluation["sampleSizes"]):
            raise ValueError("Evaluation sample or weights changed")
        cases = [
            {"graph": source.subgraph(target["txId"]), "expectedLabel": int(target["isLaundering"]),
             "expectedPatternType": target["patternType"] or "UNMAPPED",
             "samplingWeight": target["samplingWeight"]}
            for target in targets
        ]
        config = baseline.RuleConfig(**frozen["selectedConfig"])
        rows, _, rules = baseline.extract_features(cases, activity, config)

    variant = freeze["variants"][ML_VARIANT]
    cutoff = variant["thresholds"]["recallTarget"]
    ml_scores = [
        baseline.combined_score(variant["model"], row, rule["ruleScore"])
        for row, rule in zip(rows, rules, strict=True)
    ]
    predictions = {"rule": rules, "rule-ml": baseline.outcomes(ml_scores, cutoff)}
    metrics = {gate: baseline.summarize_with_history(cases, pred, rows)
               for gate, pred in predictions.items()}
    for gate, saved_key in (("rule", "ruleOnly"), ("rule-ml", ML_VARIANT + ":recallTarget")):
        if metrics[gate] != evaluation["metrics"][saved_key]:
            raise ValueError("Candidate metrics differ from the frozen replay")
    return cases, rules, ml_scores, predictions, metrics, freeze["targetRecall"], cutoff


def mock_judge(payload, scenario):
    """Inject one controlled response and run the normal evidence-ID validator."""
    items = []
    for item in payload["items"]:
        verdict = "supported"
        if ((scenario == "insufficient" and item["criterion"] == "normal_trade_consideration")
                or (scenario == "contradicted" and item["criterion"] == "document_support")):
            verdict = scenario
        items.append({
            "itemId": item["itemId"], "verdict": verdict,
            "factIds": [payload["facts"][0]["factId"]],
            "chunkIds": [payload["sources"][0]["chunkId"]],
            "reason": "모의 응답으로 판정 분기를 검증합니다.",
        })
    return {**decide(payload, {"items": items}), "modelCallAttempts": 0}


def review_panel(cases, rules, ml_scores, predictions, judge_mode):
    # Select the first admitted candidate of each type in the frozen sample order.
    representatives = {}
    for position, (case, prediction) in enumerate(zip(cases, predictions, strict=True)):
        if prediction["flagged"]:
            kind = case["expectedPatternType"] if case["expectedLabel"] else "normal"
            representatives.setdefault(kind, position)
    if any(kind not in representatives for kind, _ in PANEL):
        raise ValueError("The selected gate has no candidate for a required panel type")

    index = Index(DEFAULT_INDEX)
    if judge_mode == "openai" and any(
            urlsplit(row["url"]).scheme != "https" or urlsplit(row["url"]).hostname not in ALLOWED_HOSTS
            for row in index.chunks):
        raise ValueError("Live review requires a public-source corpus")
    tools = AgentTools(index)
    reviews = []
    for kind, scenario in PANEL:
        position = representatives[kind]
        graph = cases[position]["graph"]
        judge = openai_judge if judge_mode == "openai" else lambda payload: mock_judge(payload, scenario)
        start = perf_counter()
        review = run_review_workflow(
            tools.call, graph, rules[position], corpus_sha256=index.corpus_hash, judge=judge,
        )
        judged = review["judge"]
        reviews.append({
            "targetTxId": graph["targetTxId"], "panelType": kind,
            "ruleScore": rules[position]["ruleScore"], "ruleMlScore": ml_scores[position],
            "workflowStatus": review["status"], "reasonCodes": review["reasonCodes"],
            "codeVerificationPassed": review["verification"]["valid"],
            "identity": review["identity"], "judgeStatus": judged["status"],
            "judgeReasonCode": judged["reasonCode"], "verificationScore": judged["verificationScore"],
            "providerHttpStatus": judged.get("providerHttpStatus"),
            "providerErrorCode": judged.get("providerErrorCode"),
            "responseErrorCode": judged.get("responseErrorCode"),
            "modelCallAttempts": judged["modelCallAttempts"], "rubricVersion": judged.get("rubricVersion"),
            "strategyVersion": judged.get("strategyVersion"), "model": judged.get("model"),
            "itemEvidence": [{key: item[key] for key in ("itemId", "verdict", "factIds", "chunkIds", "reason")}
                             for item in judged["items"]],
            "humanReviewRequired": review["humanReviewRequired"],
            "readyForSubmission": review["readyForSubmission"],
            "workflowLatencyMs": round((perf_counter() - start) * 1000, 1),
        })
    return reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate", choices=GATES, default="rule")
    parser.add_argument("--judge-mode", choices=("mock", "openai"), default="mock")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output file")
    if args.judge_mode == "openai" and not all(judge_configuration()):
        parser.error("Set OPENAI_API_KEY and OPENAI_MODEL in this PowerShell session before live review")

    cases, rules, scores, predictions, metrics, target, cutoff = replay(args.freeze, args.evaluation)
    reviews = review_panel(cases, rules, scores, predictions[args.gate], args.judge_mode)
    detected = metrics[args.gate]["sample"]
    review_counts = Counter(row["judgeStatus"] for row in reviews)
    model_calls = sum(row["modelCallAttempts"] for row in reviews)
    scored_responses = sum(row["verificationScore"] is not None for row in reviews)
    result = {
        "candidateGate": args.gate, "targetRecall": target, "ruleMlThreshold": cutoff,
        "freezeSha256": baseline.digest(args.freeze), "evaluationSha256": baseline.digest(args.evaluation),
        "evaluatedTransactions": len(cases), "candidateMetrics": metrics,
        "notSelected": detected["tn"] + detected["fn"], "missedPositives": detected["fn"],
        "candidatesNotReviewedInPanel": detected["tp"] + detected["fp"] - len(reviews),
        "reviewPanel": {
            "mode": args.judge_mode, "counts": dict(review_counts),
            "modelCallAttempts": model_calls, "scoredResponses": scored_responses, "cases": reviews,
        },
    }
    baseline.save(args.output, result)
    print(json.dumps({
        "gate": args.gate, "evaluated": len(cases), "candidates": detected["tp"] + detected["fp"],
        "missedPositives": detected["fn"], "weightedRecall": metrics[args.gate]["weighted"]["recall"],
        "judgeMode": args.judge_mode, "judgePanel": dict(review_counts),
        "modelCallAttempts": model_calls, "scoredResponses": scored_responses,
    }, ensure_ascii=False))
    if args.judge_mode == "openai" and scored_responses != len(reviews):
        raise SystemExit("Live Judge did not return a valid decision for every case. See the saved evaluation.")


if __name__ == "__main__":
    main()
