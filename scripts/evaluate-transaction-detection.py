"""Compare bounded graph scopes and four AML candidate detection methods."""
import argparse
from collections import Counter
from contextlib import closing
from importlib import import_module
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ("data-pipeline", "rule-service", "graph-viewer", "rag-service")]
from agent_judge import judge_configuration
from corpus import atomic_json, canonical, digest
from detection_graph import GRAPH_VARIANTS, PATTERN_KINDS, agent_input, describe, selective_expand
from transaction_detector import DETECTOR_VERSION, openai_detect, validate_decision


replay_module = import_module("evaluate-recall-judge")
baseline = replay_module.baseline
SCHEMA_VERSION = "1.0"


def metrics(cases, predictions):
    summary = baseline.summarize(cases, [{"flagged": value} for value in predictions])
    return {
        "sample": summary["sample"], "weighted": summary["weighted"],
        "byPattern": summary["byPattern"], "reviewCount": sum(predictions),
    }


def graph_variants(source, base):
    expanded = source.subgraph(base["targetTxId"], hops=3, max_neighbors=base["meta"]["maxNeighbors"],
                               max_edges=base["meta"]["maxEdges"])
    selective = selective_expand(base, source.lookup) if base["meta"]["truncated"] else base
    variants = dict(zip(GRAPH_VARIANTS, (base, expanded, selective), strict=True))
    anchor = {edge["txId"]: edge for edge in base["edges"]}
    for graph in variants.values():
        if any(graph[key] != base[key] for key in ("targetTxId", "asOf", "windowStart")):
            raise ValueError("Graph variants changed the target or time window")
        edges = {edge["txId"]: edge for edge in graph["edges"]}
        if any(edges.get(key) != value for key, value in anchor.items()):
            raise ValueError("The expanded graph lost or changed an existing transaction")
    return variants


def describe_variant(graph, rules, seconds):
    return {
        "graphSha256": digest(canonical(graph)), "edgeCount": len(graph["edges"]),
        "truncated": graph["meta"]["truncated"], "ruleFlagged": rules["flagged"],
        "additionalReads": graph["meta"].get("evaluationAdditionalReads", 0),
        "additionalPages": graph["meta"].get("evaluationAdditionalPages", 0),
        "ruleScoringMs": round(seconds * 1000, 3), **describe(graph, rules),
    }


def summarize_scopes(cases):
    result = {}
    for variant in GRAPH_VARIANTS:
        rows = [case["graphScopes"][variant] for case in cases]
        rules = [row["ruleFlagged"] for row in rows]
        core = [row["targetInTwoCore"] for row in rows]
        patterns = [
            row["ruleFlagged"] or bool({"SCATTER_GATHER", "GATHER_SCATTER"} & row["patterns"].keys())
            for row in rows
        ]
        result[variant] = {
            "edges": sum(row["edgeCount"] for row in rows),
            "truncatedGraphs": sum(row["truncated"] for row in rows),
            "additionalReads": sum(row["additionalReads"] for row in rows),
            "additionalPages": sum(row["additionalPages"] for row in rows),
            "patternWitnessCounts": {kind: sum(kind in row["patterns"] for row in rows) for kind in PATTERN_KINDS},
            "newPatternWitnessCounts": {
                kind: sum(kind in row["patterns"] and kind not in case["graphScopes"]["2hop"]["patterns"]
                          for case, row in zip(cases, rows, strict=True)) for kind in PATTERN_KINDS
            },
            "candidateFilters": {
                "rules": metrics(cases, rules), "twoCoreOnly": metrics(cases, core),
                "rulesOrCompositePatterns": metrics(cases, patterns),
                "rulesOrCompositePatternsOrCore": metrics(cases, [p or c for p, c in zip(patterns, core, strict=True)]),
            },
        }
    return result


def prepare(freeze, evaluation, model_scope):
    cases, rules, old_ml_scores, _, _, target_recall, cutoff = replay_module.replay(freeze, evaluation)
    lock = json.loads(freeze.read_bytes())
    ml = lock["variants"][replay_module.ML_VARIANT]["model"]
    config = baseline.RuleConfig(**rules[0]["config"])
    prepared = []
    with closing(baseline.SourceIndex(baseline.INDEX)) as source:
        source.db.execute("BEGIN")
        metadata = dict(source.db.execute("SELECT key,value FROM metadata"))
        if metadata["sourceSha256"] != lock["provenance"]["sourceSha256"]:
            raise ValueError("Graph comparison index differs from the frozen source")
        activity = baseline.RecipientActivity(source)
        for case, first_rule, old_ml_score in zip(cases, rules, old_ml_scores, strict=True):
            started = perf_counter()
            graphs = graph_variants(source, case["graph"])
            scopes, scores = {}, {}
            for name, graph in graphs.items():
                measured_at = perf_counter()
                assessed = first_rule if name == "2hop" else baseline.score(graph, config)
                scores[name] = assessed
                scopes[name] = describe_variant(graph, assessed, perf_counter() - measured_at)
            selected = graphs[model_scope]
            observed = scopes[model_scope]
            features = {key: observed[key] for key in ("patterns", "twoCoreAccountCount", "targetInTwoCore",
                                                       "distinctIncomingSenders", "distinctOutgoingRecipients")}
            if model_scope == "2hop":
                ml_score = old_ml_score
            else:
                target = next(row for row in selected["edges"] if row["txId"] == selected["targetTxId"])
                profile = activity.compare(target["txId"], expected_target=target)
                ml_score = baseline.combined_score(ml, baseline.features(selected, target, profile),
                                                   scores[model_scope]["ruleScore"])
            prepared.append({
                "graph": {"targetTxId": selected["targetTxId"], "asOf": selected["asOf"],
                          "meta": {"truncated": selected["meta"]["truncated"]}},
                **{key: case[key] for key in ("expectedLabel", "expectedPatternType", "samplingWeight")},
                "graphScopes": scopes, "input": agent_input(selected, features),
                "ruleFlagged": observed["ruleFlagged"], "ruleMlFlagged": ml_score >= cutoff,
                "preparationMs": round((perf_counter() - started) * 1000, 3),
            })
    packet = {
        "schemaVersion": SCHEMA_VERSION, "suite": "ibm_aml_frozen_detection_packet",
        "modelGraphScope": model_scope, "targetRecall": target_recall,
        "baselineFreezeSha256": baseline.digest(freeze), "evaluationSha256": baseline.digest(evaluation),
        "cases": prepared, "scopeStudy": summarize_scopes(prepared),
    }
    return {**packet, "packetSha256": digest(canonical(packet))}


def load_packet(path):
    packet = json.loads(path.read_bytes())
    checksum = packet.pop("packetSha256")
    if (packet["suite"] != "ibm_aml_frozen_detection_packet" or packet["schemaVersion"] != SCHEMA_VERSION
            or checksum != digest(canonical(packet))):
        raise ValueError("Detection packet does not match its frozen content")
    return {**packet, "packetSha256": checksum}


def mock_detect(payload):
    patterns = payload["observations"]["patterns"]
    evidence = next((patterns[kind] for kind in ("CYCLE", "SCATTER_GATHER", "GATHER_SCATTER")
                     if kind in patterns), [])
    decision = "candidate" if evidence else "not_candidate"
    return {
        **validate_decision(payload, {"decision": decision, "evidenceTxIds": evidence,
                                      "reason": "모의 실행용 구조 응답입니다."}),
        "modelCallAttempts": 0, "latencyMs": None, "detectorVersion": DETECTOR_VERSION,
    }


def matches_case_order(rows, cases):
    return len(rows) <= len(cases) and all(
        row["targetTxId"] == case["input"]["targetTxId"]
        and row["inputSha256"] == digest(canonical(case["input"]))
        for row, case in zip(rows, cases)
    )


def collect(packet, mode, model, output, *, cases=None, prior_sha=None, model_run=None,
            detector_version=DETECTOR_VERSION):
    progress_path = output.with_suffix(".progress.json")
    lock_path = output.with_suffix(".lock")
    output.parent.mkdir(parents=True, exist_ok=True)
    stamp = {"packetSha256": packet["packetSha256"], "mode": mode, "model": model,
             "detectorVersion": detector_version}
    if prior_sha is not None:
        stamp["retryFromSha256"] = prior_sha
    cases = packet["cases"] if cases is None else cases
    if model_run is None:
        model_run = mock_detect if mode == "mock" else openai_detect
    with lock_path.open("x", encoding="ascii") as run_lock:
        run_lock.write("Detection benchmark in progress\n")
    try:
        progress = json.loads(progress_path.read_bytes()) if progress_path.exists() else {
            **stamp, "rows": [], "interruptedModelCallAttempts": 0,
        }
        if any(progress.get(key) != value for key, value in stamp.items()):
            raise ValueError("Existing progress belongs to a different packet, mode or model")
        rows = progress["rows"]
        if progress.get("retryFromSha256") != prior_sha or not matches_case_order(rows, cases):
            raise ValueError("Existing progress does not match the evaluated case order")
        print(f"{len(rows)}/{len(cases)} transactions evaluated", file=sys.stderr, flush=True)
        for case in cases[len(rows):]:
            payload = case["input"]
            decision = model_run(payload)
            if mode == "openai" and (
                    decision.get("reasonCode") == "judge_model_unavailable"
                    or decision.get("providerHttpStatus") in (401, 403, 429, 500, 502, 503, 504)
                    or decision.get("providerErrorCode") in {"model_not_found", "account_deactivated",
                                                            "billing_hard_limit_reached", "insufficient_quota"}):
                progress["interruptedModelCallAttempts"] += decision["modelCallAttempts"]
                atomic_json(progress_path, progress)
                print("OpenAI was unavailable; completed cases are saved. Resume with the same command.",
                      file=sys.stderr, flush=True)
                raise SystemExit(2)
            rows.append({**decision, "targetTxId": payload["targetTxId"],
                         "inputSha256": digest(canonical(payload))})
            atomic_json(progress_path, progress)
            if len(rows) % 25 == 0:
                print(f"{len(rows)}/{len(cases)} transactions evaluated", file=sys.stderr, flush=True)
        return rows, progress["interruptedModelCallAttempts"]
    finally:
        lock_path.unlink()


def compare(packet, decisions, mode, interrupted_calls):
    cases = packet["cases"]
    if len(decisions) != len(cases):
        raise ValueError("Every frozen transaction must be represented in the comparison")
    agent = [row["flagged"] is True for row in decisions]
    rule = [case["ruleFlagged"] for case in cases]
    rule_ml = [case["ruleMlFlagged"] for case in cases]
    combined = [r or a for r, a in zip(rule, agent, strict=True)]
    model_calls = sum(row["modelCallAttempts"] for row in decisions) + interrupted_calls
    return {
        "suite": "ibm_aml_candidate_comparison", "packetSha256": packet["packetSha256"],
        "modelGraphScope": packet["modelGraphScope"], "mode": mode, "caseCount": len(cases),
        "methods": {name: {**metrics(cases, method), "modelCallAttempts": calls}
                    for name, method, calls in (
                        ("rules", rule, 0), ("rulesPlusMl", rule_ml, 0),
                        ("agent", agent, model_calls), ("rulesOrAgent", combined, model_calls),
                    )},
        "agentDecisionCounts": dict(Counter(row["decision"] for row in decisions)),
        "agentModelCallAttempts": model_calls, "interruptedModelCallAttempts": interrupted_calls,
        "agentMissedWithoutDecision": sum(case["expectedLabel"] and row["flagged"] is None
                                         for case, row in zip(cases, decisions, strict=True)),
        "agentHitsMissedByRule": sum(case["expectedLabel"] and a and not r
                                    for case, a, r in zip(cases, agent, rule, strict=True)),
        "decisions": decisions,
    }


def retry_unscored(packet, previous_path, model, output):
    raw = previous_path.read_bytes()
    previous = json.loads(raw)
    previous_rows = previous["decisions"]
    cases = packet["cases"]
    if (previous.get("suite") != "ibm_aml_candidate_comparison"
            or previous.get("mode") != "openai" or previous.get("packetSha256") != packet["packetSha256"]
            or len(previous_rows) != len(cases) or not matches_case_order(previous_rows, cases)
            or any(row["model"] != model or row["detectorVersion"] != DETECTOR_VERSION for row in previous_rows)):
        raise ValueError("Retry requires the same frozen packet, model and detector version")
    positions = [index for index, row in enumerate(previous_rows) if row["decision"] == "unscored"]
    if not positions:
        raise ValueError("The previous result contains no unscored cases to retry")
    prior_sha = digest(raw)
    retried, interrupted = collect(
        packet, "openai", model, output, cases=[cases[index] for index in positions], prior_sha=prior_sha,
    )
    merged = previous_rows.copy()
    for index, row in zip(positions, retried, strict=True):
        merged[index] = {**row, "modelCallAttempts": row["modelCallAttempts"] + merged[index]["modelCallAttempts"]}
    combined = compare(packet, merged, "openai", previous["interruptedModelCallAttempts"] + interrupted)
    return {**combined, "retry": {
        "previousResultSha256": prior_sha, "previousModelCallAttempts": previous["agentModelCallAttempts"],
        "reusedDecisions": len(cases) - len(positions), "retriedCaseCount": len(positions),
        "retryModelCallAttempts": sum(row["modelCallAttempts"] for row in retried) + interrupted,
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    tasks = parser.add_subparsers(dest="phase", required=True)
    build = tasks.add_parser("prepare")
    build.add_argument("--freeze", type=Path, required=True)
    build.add_argument("--evaluation", type=Path, required=True)
    build.add_argument("--graph-scope", choices=GRAPH_VARIANTS, default="2hop")
    build.add_argument("--output", type=Path, required=True)
    run = tasks.add_parser("compare")
    run.add_argument("--packet", type=Path, required=True)
    run.add_argument("--mode", choices=("mock", "openai"), default="mock")
    run.add_argument("--retry-unscored-from", type=Path, help="Keep scored decisions and retry the rest")
    run.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output name; an existing result stays intact")
    if args.phase == "prepare":
        result = prepare(args.freeze, args.evaluation, args.graph_scope)
        report = {"caseCount": len(result["cases"]), "modelGraphScope": result["modelGraphScope"],
                  "scopes": {name: {k: scope[k] for k in ("edges", "truncatedGraphs", "newPatternWitnessCounts")}
                             for name, scope in result["scopeStudy"].items()}}
    else:
        if args.retry_unscored_from is not None and args.mode != "openai":
            parser.error("--retry-unscored-from requires --mode openai")
        key, model = judge_configuration() if args.mode == "openai" else (None, None)
        if args.mode == "openai" and not (key and model):
            parser.error("Set OPENAI_API_KEY and OPENAI_MODEL in the PowerShell session running compare")
        packet = load_packet(args.packet)
        if args.retry_unscored_from is None:
            decisions, interrupted_calls = collect(packet, args.mode, model, args.output)
            result = compare(packet, decisions, args.mode, interrupted_calls)
        else:
            result = retry_unscored(packet, args.retry_unscored_from, model, args.output)
        report = {"caseCount": result["caseCount"], "mode": args.mode,
                  "agentModelCallAttempts": result["agentModelCallAttempts"],
                  "agentDecisionCounts": result["agentDecisionCounts"],
                  **({"retry": result["retry"]} if "retry" in result else {})}
    baseline.save(args.output, result)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
