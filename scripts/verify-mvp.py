"""Read-only integration check: real Neo4j -> public JSON -> rule evaluation.

Run after loading the HI-Small-MVP dataset. Does not mutate DB or tune on test.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "data-pipeline"), str(ROOT / "rule-service")]
from evaluate import benchmark
from graph_contract import MemoryGraph, read_transactions
from neo4j_mvp import Database
from rules import check_no_truth


def main():
    folder = ROOT / "data/processed/HI-Small-MVP"
    cases = json.loads((folder / "cases.json").read_text(encoding="utf-8"))
    memory = MemoryGraph(read_transactions(folder / "transactions.csv"))
    db = Database()
    exported = []
    for case in cases:
        target = case["graph"]["targetTxId"]
        actual = db.subgraph(target)
        expected = memory.subgraph(target)
        if actual["edges"] != expected["edges"] or actual["nodes"] != expected["nodes"]:
            raise AssertionError(f"Database and source graph differ: {target}")
        check_no_truth(actual)
        exported.append({**case, "graph": actual})
        print(f"Verified {target}: {len(actual['edges'])} edges", flush=True)
    report = benchmark(exported, .9)
    report["scope"] = "selected_case_pilot_not_population_benchmark"
    report["integration"] = {"database": "Neo4j 5.26", "matchedCases": len(exported),
                              "labelFree": True, "exactAmountsMatched": True}
    out = ROOT / "artifacts/neo4j-rule-pilot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"integration": report["integration"], "test": report["test"]}, indent=2))


if __name__ == "__main__":
    main()
