"""Prepare and evaluate held-out claims across independent synthetic graphs."""
import argparse
import json
from pathlib import Path
import tempfile

from agent_judge import judge_configuration, openai_judge
from agent_judge_demo import OUTCOMES, build_claim_payloads, evaluate_payloads, read_reviewed_labels
from agent_workflow_demo import FIXTURES, fixture_tools
from corpus import canonical, digest
from rules import score


SCENARIOS = FIXTURES / "judge-multigraph-cases.json"


def fixture_graph(scenario):
    graph_id = scenario["graphId"]
    date = scenario["date"]
    edges = []
    for i, tx in enumerate(scenario["transactions"], 1):
        edges.append({
            "txId": f"DEMO-MG-{graph_id}-{i}", "timestamp": f"{date}T{tx['time']}",
            "fromAccountKey": f"1:DEMO-MG-{graph_id}-{tx['sender']}",
            "toAccountKey": f"1:DEMO-MG-{graph_id}-{tx['recipient']}",
            "amountPaid": tx["amount"], "amountReceived": tx["amount"],
            "paymentCurrency": "US Dollar", "receivingCurrency": "US Dollar", "paymentFormat": tx["format"],
        })
    accounts = sorted({edge[field] for edge in edges for field in ("fromAccountKey", "toAccountKey")})
    return {
        "schemaVersion": "1.0", "targetTxId": edges[-1]["txId"], "asOf": edges[-1]["timestamp"],
        "windowStart": f"{date}T00:00:00", "nodes": [{"accountKey": account} for account in accounts],
        "edges": edges, "meta": {
            "hops": 2, "maxNeighbors": 50, "maxEdges": 1000, "truncated": False,
            "timeDirection": "past-only", "sourceScope": "synthetic_judge_multigraph",
            "sourceGraphComplete": False,
        },
    }


def prepare_packet(frozen):
    cases = []
    with tempfile.TemporaryDirectory() as directory:
        tools, _ = fixture_tools(directory)
        for scenario in json.loads(frozen):
            graph = fixture_graph(scenario)
            workflow = tools.call("run_review_workflow", {"graph": graph, "ruleResult": score(graph)})
            claims = [{**claim, "split": "holdout"} for claim in scenario["claims"]]
            cases.extend({"caseId": case["caseId"], "graphId": scenario["graphId"], "input": payload}
                         for case, payload in build_claim_payloads(workflow, claims, "holdout"))

    labels = {case["caseId"]: {"expected": None, "reason": ""} for case in cases}
    if not cases or len(cases) != len(labels):
        raise ValueError("Evaluation requires unique case IDs")
    return {
        "suite": "synthetic_multigraph_judge_label_packet", "packetSha256": digest(canonical(cases)),
        "labels": labels, "cases": cases,
    }


def evaluate_packet(frozen, packet, labels_path, judge, case_id=None):
    labels, label_hash = read_reviewed_labels(packet, labels_path)
    if set(labels.values()) != set(OUTCOMES):
        raise ValueError("The reviewed packet must contain passed, held, and rejected cases")
    cases = [case for case in packet["cases"] if case_id is None or case["caseId"] == case_id]
    if not cases:
        raise ValueError("No reviewed case matches --case-id")
    selected = [(case, case["input"], labels[case["caseId"]]) for case in cases]
    result = evaluate_payloads(judge, "holdout", 1, frozen, selected, "human_reviewed")
    for row, case in zip(result["cases"], cases, strict=True):
        row["graphId"] = case["graphId"]
    return {
        **result, "suite": "synthetic_multigraph_judge_benchmark",
        "packetSha256": packet["packetSha256"], "labelFileSha256": label_hash,
        "graphCount": len({case["graphId"] for case in cases}),
        "selectedCaseId": case_id, "packetCaseCount": len(packet["cases"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "evaluate"))
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--case-id", help="Evaluate one case from the fully reviewed packet")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output file")
    if (args.phase == "evaluate") != (args.labels is not None):
        parser.error("Pass --labels for evaluate only, with every case reviewed")
    if args.phase == "prepare" and args.case_id is not None:
        parser.error("--case-id requires evaluate")
    if args.phase == "evaluate" and not all(judge_configuration()):
        parser.error("Set OPENAI_API_KEY and OPENAI_MODEL in this PowerShell session")

    frozen = SCENARIOS.read_bytes()
    packet = prepare_packet(frozen)
    if args.phase == "evaluate":
        try:
            result = evaluate_packet(frozen, packet, args.labels, openai_judge, args.case_id)
        except (ValueError, OSError) as error:
            parser.error(str(error))
    else:
        result = packet

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    summary_keys = ("selectedCaseId", "packetCaseCount", "graphCount", "correctRuns", "totalRuns", "unscoredRuns",
                    "rejectRecall", "rejectF1", "confusionMatrix", "modelCallAttempts")
    case_count = len(result["cases"])
    print(json.dumps({"suite": result["suite"], "caseCount": case_count,
                      **{key: result[key] for key in summary_keys if key in result}}, ensure_ascii=False))
    return 2 if result.get("unscoredRuns") else 0


if __name__ == "__main__":
    raise SystemExit(main())
