"""Run the investigation loop with the live LLM on a seeded V2 validation sample.

Development check, not the benchmark: only the validation split is read, risk
scores are out-of-fold, and labels are used only to report results. Alerts are
those the gate sends to `investigate`. expand_account reads the offline source
index (past-only). The API key comes from the environment or --env-file and is
never written.

    python agent-service/run_investigations.py --env-file .env --per-label 4
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date
import json
import random
import time

import _paths
from evaluation_index import SourceIndex
from evidence_check import decide, load_gate_config
from graph_tools import IndexSource
from investigation_graph import run_investigation
from llm_client import LLMClient, configuration, read_env_file
from risk_score import DEFAULT_INDEX
from tune_gate import alerts_with_signals
from validation_scores import OutOfFoldRisk, validation_scores

OUTPUT_DIR = _paths.ROOT / "artifacts" / "agent-investigation"


def summarize(result, label, seconds):
    investigation = result.get("investigation") or {}
    budget = result["budget"]
    return {
        "targetTxId": result["alertContext"]["targetTxId"], "label": label,
        "gateReason": result["gate"]["reasonCode"], "riskScore": result["gate"].get("riskScore"),
        "status": result["status"], "route": result.get("route"), "reasonCodes": result["reasonCodes"],
        "hold": result.get("hold"), "loop": result["gate"].get("loop"),
        "steps": [{"step": d["step"], "action": {k: v for k, v in d["action"].items() if v is not None},
                   "conclusion": d["conclusion"], "rationale": d["rationale"],
                   "checklist": {row["itemId"]: [row["status"], row["factIds"]] for row in d["checklist"]}}
                  for d in investigation.get("decisions", [])],
        "tools": [{k: call[k] for k in ("callId", "tool", "arguments", "summary", "refused", "addedTransactions")}
                  for call in investigation.get("toolCalls", [])],
        "llmCalls": budget["llmCalls"], "costUsd": budget["costUsd"], "inputTokens": budget["inputTokens"],
        "outputTokens": budget["outputTokens"], "hopsExplored": budget["hopsExplored"], "seconds": round(seconds, 1),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file")
    parser.add_argument("--per-label", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--output")
    args = parser.parse_args()
    env = {}
    if args.env_file:
        env = read_env_file(args.env_file)
    key, model = configuration({**__import__("os").environ, **env})
    client = LLMClient(key, model or "gpt-6-luna")
    gate_config = load_gate_config()
    data = validation_scores()
    locked = json.loads((_paths.ROOT / "artifacts" / "recipient-hybrid-frozen-next.json").read_bytes())
    risk = OutOfFoldRisk(data, locked["variants"]["graphWithAvailabilityAndDelta"]["thresholds"])
    candidates = {0: [], 1: []}
    for row in alerts_with_signals():
        profile = {**row["profile"], "riskScore": {"status": "computed", "score": row["risk"]}}
        if decide(row["context"], profile, row["budget"], gate_config, now=float("-inf"))[0] == "investigate":
            candidates[row["case"]["expectedLabel"]].append(row["case"])
    rng = random.Random(args.seed)
    chosen = [(case, label) for label in (1, 0)
              for case in rng.sample(candidates[label], min(args.per_label, len(candidates[label])))]
    from alert_rules import alert_rule_result
    rows = []
    with closing(SourceIndex(DEFAULT_INDEX)) as source:
        store = IndexSource(source)
        for case, label in chosen:
            alert = {"graph": case["graph"], "ruleResult": alert_rule_result(case["graph"])}
            started = time.perf_counter()
            result = run_investigation(alert, risk_source=risk, gate_config=gate_config, llm_client=client,
                                       graph_source=store)
            rows.append(summarize(result, label, time.perf_counter() - started))
            last = rows[-1]
            print(f"{last['targetTxId']} label {label} {last['gateReason']:24s} -> {last['status']:7s} "
                  f"{(last['hold'] or {}).get('category') or last['route']:22s} tools "
                  f"{[t['tool'] for t in last['tools']]} calls {last['llmCalls']} ${last['costUsd']:.4f} "
                  f"{last['seconds']}s", flush=True)
    report = {"kind": "validation_sample_development_check_not_benchmark", "model": client.model,
              "seed": args.seed, "candidates": {"laundering": len(candidates[1]), "normal": len(candidates[0])},
              "gateConfig": gate_config["version"], "results": rows,
              "totals": {"llmCalls": sum(r["llmCalls"] for r in rows),
                         "costUsd": round(sum(r["costUsd"] for r in rows), 6)}}
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if key and key in text:
        raise SystemExit("refusing to write output that contains the credential")
    path = _paths.ROOT / args.output if args.output else OUTPUT_DIR / f"validation-sample-{date.today():%Y%m%d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text + "\n")
    print(json.dumps({"output": str(path), **report["totals"], "candidates": report["candidates"]}))


if __name__ == "__main__":
    main()
