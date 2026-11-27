"""Compare the V2 alert, its gate, and optional investigation on a sealed sample."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import json
from pathlib import Path
import sys
from time import perf_counter

from alert_rules import alert_rule_result
from agent_tools import AgentTools
from corpus import atomic_json, canonical, digest
from evidence_check import CONFIG_PATH
from experiment import weighted_metrics
from investigation_graph import build_graph, RECURSION_LIMIT
from investigation_state import initial_state
from llm_client import configuration, LLMClient
from retrieval import Index
from risk_score import HybridRiskScore, sha256_file
from evaluation_index import SourceIndex
from threading import local


def load_sample(path):
    packet = json.loads(path.read_bytes())
    seal = packet.pop("packetSha256")
    if packet["suite"] != "ibm_aml_investigation_holdout_manifest" or seal != digest(canonical(packet)):
        raise ValueError("Investigation sample seal does not match")
    ids = [case["targetTxId"] for case in packet["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Investigation sample repeats target IDs")
    return {**packet, "packetSha256": seal}


def summarize(cases, flags):
    labels = [case["expectedLabel"] for case in cases]
    cells = Counter(zip(labels, flags, strict=True))
    return {"weighted": weighted_metrics(cases, [{"flagged": flag} for flag in flags]),
            "sample": {"tp": cells[1, True], "fp": cells[0, True], "fn": cells[1, False], "tn": cells[0, False]},
            "reviewCount": sum(flags)}


class ThreadIndexSource:
    """expand_account store for worker threads: a sqlite connection cannot cross threads."""
    name = "offline_source_index"

    def __init__(self, path):
        self.path, self.local = path, local()

    def lookup(self, account, since, as_of, limit):
        if not hasattr(self.local, "source"):
            self.local.source = SourceIndex(self.path)
        return self.local.source.lookup(account, since, as_of, limit)


class BatchRiskScores:
    def __init__(self):
        self.values = {}

    def score(self, alert, context=None):
        return self.values[alert["graph"]["targetTxId"]]


def evaluate_case(graph_runner, case, alert):
    started = perf_counter()
    state = graph_runner.invoke(initial_state(alert), {"recursion_limit": RECURSION_LIMIT})
    if "gate" not in state:
        raise ValueError("The frozen alert did not pass input verification")
    held = state.get("hold", {})
    budget, judge = state["budget"], state.get("judge", {})
    investigation = state.get("investigation") or {}
    gate_candidate = alert["ruleResult"]["flagged"] and state["gate"]["reasonCode"] != "low_risk_without_facts"
    return {
        "targetTxId": case["targetTxId"], "ruleAlert": alert["ruleResult"]["flagged"],
        "gateCandidate": gate_candidate, "needsReview": gate_candidate and state["status"] != "closed",
        "status": state["status"], "gateReason": state["gate"]["reasonCode"],
        "modelCalls": budget["llmCalls"], "costUsd": budget["costUsd"], "inputTokens": budget["inputTokens"],
        "outputTokens": budget["outputTokens"], "holdCategory": held.get("category"),
        "holdErrorClass": held.get("errorClass"), "holdReason": held.get("reasonCode"),
        "investigatorToolCalls": len(investigation.get("toolCalls", [])),
        "loopReason": state["gate"].get("loop", {}).get("reasonCode"),
        "typology": state.get("typology", {}).get("choice"),
        "validatedDraft": state.get("verification", {}).get("valid") is True,
        "judgeStatus": judge.get("status"), "latencyMs": round((perf_counter() - started) * 1000),
    }


def unavailable(row):
    return row["holdErrorClass"] in {"auth", "configuration", "quota", "transient"} or row["holdReason"] == "llm_model_not_found"


def main(client_override=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("packet", "freeze", "rule-freeze", "source-index", "training", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--mode", choices=("gate", "openai"), default="gate")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=1)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output name")
    packet = load_sample(args.packet)
    live = args.mode == "openai"
    if live and (not args.corpus or (client_override is None and not all(configuration()))):
        parser.error("Live evaluation requires a corpus and configured OpenAI environment")
    if not args.training.is_file() or sha256_file(args.freeze) != packet["baselineFreezeSha256"]:
        parser.error("Supply the sealed score model and its training IDs")
    tools = AgentTools(Index(args.corpus)) if live else None
    client = (client_override or LLMClient()) if live else None
    service = Path(__file__).resolve().parent
    implementation = {name: sha256_file(service / name) for name in (
        "alert_rules.py", "basic_profile.py", "evidence_check.py", "graph_tools.py", "investigation_graph.py",
        "investigation_steps.py", "investigator.py", "llm_client.py", "risk_score.py",
    )}
    stamp = {"suite": "traceon-investigation-evaluation-v2", "mode": args.mode,
             "packetSha256": packet["packetSha256"], "model": client.model if live else None,
             "corpusSha256": tools.index.corpus_hash if live else None,
             "implementationSha256": digest(canonical(implementation)),
             "gatePolicySha256": sha256_file(CONFIG_PATH)}
    checkpoint = args.output.with_suffix(".progress.json")
    lock_path = args.output.with_suffix(".lock")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("x", encoding="ascii"):
        pass
    try:
        progress = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else {
            **stamp, "rows": [], "interruptedCalls": 0, "interruptedCostUsd": 0.0,
        }
        if any(progress.get(key) != value for key, value in stamp.items()):
            raise ValueError("Progress belongs to a different evaluation")
        rows, cases = progress["rows"], packet["cases"]
        done = {row["targetTxId"] for row in rows}
        if len(done) != len(rows) or not done <= {case["targetTxId"] for case in cases}:
            raise ValueError("Existing progress does not match the sealed cases")
        pending = [case for case in cases if case["targetTxId"] not in done]
        print(f"{len(rows)}/{len(cases)} transactions evaluated", file=sys.stderr, flush=True)
        with closing(HybridRiskScore(args.freeze, args.rule_freeze, args.source_index, args.training)) as risk:
            if dict(risk.source.db.execute("SELECT key,value FROM metadata"))["sourceSha256"] != packet["sourceSha256"]:
                raise ValueError("Source differs from the manifest")
            if set(risk.training_ids).intersection(case["targetTxId"] for case in cases):
                raise ValueError("A training target leaked into the evaluation")
            batch_scores = BatchRiskScores()
            graph_runner = build_graph(risk_source=batch_scores, llm_client=client,
                                       graph_source=ThreadIndexSource(args.source_index) if live else None,
                                       call_tool=tools.call if tools else None, judge_enabled=live)
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                for offset in range(0, len(pending), args.workers):
                    batch = []
                    for case in pending[offset:offset + args.workers]:
                        graph = risk.source.subgraph(case["targetTxId"], hops=packet["graphScope"]["hops"],
                                                      window_days=packet["graphScope"]["windowDays"])
                        if graph["asOf"] != case["asOf"]:
                            raise ValueError("Source graph time differs from the sealed target")
                        alert = {"graph": graph, "ruleResult": alert_rule_result(graph)}
                        batch_scores.values[case["targetTxId"]] = risk.score(alert)
                        batch.append((case, alert))
                    futures = [executor.submit(evaluate_case, graph_runner, case, alert) for case, alert in batch]
                    paused = False
                    for future in as_completed(futures):
                        row = future.result()
                        if live and unavailable(row):
                            paused = True
                            progress["interruptedCalls"] += row["modelCalls"]
                            progress["interruptedCostUsd"] += row["costUsd"]
                        else:
                            rows.append(row)
                        atomic_json(checkpoint, progress)
                    batch_scores.values.clear()
                    if paused:
                        print("Model access or transport failed; completed cases are saved. Run the same command to resume.",
                              file=sys.stderr, flush=True)
                        raise SystemExit(2)
                    if (len(rows) % 24 < args.workers or len(rows) == len(cases)):
                        print(f"{len(rows)}/{len(cases)} transactions evaluated", file=sys.stderr, flush=True)
        by_target = {row["targetTxId"]: row for row in rows}
        rows = [by_target[case["targetTxId"]] for case in cases]
        methods = {name: summarize(cases, [row[name] for row in rows]) for name in ("ruleAlert", "gateCandidate")}
        if live:
            methods["fullInvestigation"] = summarize(cases, [row["needsReview"] for row in rows])
        result = {**stamp, "caseCount": len(rows), "methods": methods, "states": dict(Counter(row["status"] for row in rows)),
                  "holdCounts": dict(Counter(row["holdCategory"] for row in rows if row["holdCategory"])),
                  "holdReasons": dict(Counter(row["holdReason"] for row in rows if row["holdReason"])),
                  "validatedDrafts": sum(row["validatedDraft"] for row in rows),
                  "judgeOutcomes": dict(Counter(row["judgeStatus"] for row in rows if row["judgeStatus"])),
                  "modelCalls": sum(row["modelCalls"] for row in rows) + progress["interruptedCalls"],
                  "interruptedCalls": progress["interruptedCalls"],
                  "inputTokens": sum(row["inputTokens"] for row in rows), "outputTokens": sum(row["outputTokens"] for row in rows),
                  "costUsd": round(sum(row["costUsd"] for row in rows) + progress["interruptedCostUsd"], 6)}
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, ensure_ascii=False)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        lock_path.unlink()


if __name__ == "__main__":
    main()
