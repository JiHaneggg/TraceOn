"""Compare the V2 alert, its gate, and optional investigation on a sealed sample."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path

from alert_rules import alert_rule_result
from agent_tools import AgentTools
from corpus import atomic_json, canonical, digest
from experiment import weighted_metrics
from investigation_graph import build_graph, RECURSION_LIMIT
from investigation_state import initial_state
from llm_client import configuration, LLMClient
from retrieval import Index
from risk_score import HybridRiskScore, sha256_file


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("packet", "freeze", "rule-freeze", "source-index", "training", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--mode", choices=("gate", "openai"), default="gate")
    parser.add_argument("--corpus", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output name")
    packet = load_sample(args.packet)
    live = args.mode == "openai"
    if live and (not args.corpus or not all(configuration())):
        parser.error("Live evaluation requires a corpus and configured OpenAI environment")
    if not args.training.is_file() or sha256_file(args.freeze) != packet["baselineFreezeSha256"]:
        parser.error("Supply the sealed score model and its training IDs")
    tools = AgentTools(Index(args.corpus)) if live else None
    client = LLMClient() if live else None
    stamp = {"suite": "traceon-investigation-evaluation-v1", "mode": args.mode,
             "packetSha256": packet["packetSha256"], "model": client.model if live else None,
             "corpusSha256": tools.index.corpus_hash if live else None}
    checkpoint = args.output.with_suffix(".progress.json")
    lock_path = args.output.with_suffix(".lock")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("x", encoding="ascii"):
        pass
    try:
        progress = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else {**stamp, "rows": []}
        if any(progress.get(key) != value for key, value in stamp.items()):
            raise ValueError("Progress belongs to a different evaluation")
        rows, cases = progress["rows"], packet["cases"]
        if len(rows) > len(cases) or any(row["targetTxId"] != case["targetTxId"] for row, case in zip(rows, cases)):
            raise ValueError("Existing progress does not match the target order")
        with closing(HybridRiskScore(args.freeze, args.rule_freeze, args.source_index, args.training)) as risk:
            if dict(risk.source.db.execute("SELECT key,value FROM metadata"))["sourceSha256"] != packet["sourceSha256"]:
                raise ValueError("Source differs from the manifest")
            if set(risk.training_ids).intersection(case["targetTxId"] for case in cases):
                raise ValueError("A training target leaked into the evaluation")
            graph_runner = build_graph(risk_source=risk, llm_client=client,
                                       call_tool=tools.call if tools else None, judge_enabled=live)
            for case in cases[len(rows):]:
                graph = risk.source.subgraph(case["targetTxId"], hops=packet["graphScope"]["hops"],
                                             window_days=packet["graphScope"]["windowDays"])
                if graph["asOf"] != case["asOf"]:
                    raise ValueError("Source graph time differs from the sealed target")
                rule = alert_rule_result(graph)
                state = graph_runner.invoke(initial_state({"graph": graph, "ruleResult": rule}),
                                             {"recursion_limit": RECURSION_LIMIT})
                if "gate" not in state:
                    raise ValueError("The frozen alert did not pass input verification")
                gate_candidate = rule["flagged"] and state["gate"]["reasonCode"] != "low_risk_without_facts"
                rows.append({
                    "targetTxId": case["targetTxId"], "ruleAlert": rule["flagged"], "gateCandidate": gate_candidate,
                    "needsReview": gate_candidate and state["status"] != "closed",
                    "status": state["status"], "gateReason": state["gate"]["reasonCode"],
                    "modelCalls": state["budget"]["llmCalls"], "costUsd": state["budget"]["costUsd"],
                    "holdCategory": state.get("hold", {}).get("category"),
                })
                atomic_json(checkpoint, progress)
        methods = {name: summarize(cases, [row[name] for row in rows]) for name in ("ruleAlert", "gateCandidate")}
        if live:
            methods["fullInvestigation"] = summarize(cases, [row["needsReview"] for row in rows])
        result = {**stamp, "caseCount": len(rows), "methods": methods, "states": dict(Counter(row["status"] for row in rows)),
                  "holdCounts": dict(Counter(row["holdCategory"] for row in rows if row["holdCategory"])),
                  "modelCalls": sum(row["modelCalls"] for row in rows),
                  "costUsd": round(sum(row["costUsd"] for row in rows), 6)}
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, ensure_ascii=False)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        lock_path.unlink()


if __name__ == "__main__":
    main()
