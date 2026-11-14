"""Generate clearly marked synthetic smoke cases, not an IBM benchmark."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from graph_contract import MemoryGraph
from preprocess import TX_FIELDS

ROOT = Path(__file__).resolve().parent.parent


def generate():
    rows, targets = [], []
    for split, day in [("validation", 1), ("test", 15)]:
        for kind in ["cycle", "fan_in", "fan_out", "normal", "payroll_control"]:
            prefix = f"DEMO-{split}-{kind}"
            base = datetime(2022, 9, day, 12)
            pairs = {"cycle": [("A", "B"), ("B", "C"), ("C", "A")],
                     "fan_in": [("A", "D"), ("B", "D"), ("C", "D")],
                     "fan_out": [("D", "A"), ("D", "B"), ("D", "C")],
                     "normal": [("A", "B"), ("A", "C")],
                     "payroll_control": [("D", "A"), ("D", "B"), ("D", "C"), ("D", "E")]}[kind]
            label = int(kind in {"cycle", "fan_in", "fan_out"})
            for i, (src, dst) in enumerate(pairs):
                amount = f"{1000+i}.10"
                rows.append({"txId": f"{prefix}-{i}",
                             "timestamp": (base + timedelta(minutes=i)).isoformat(),
                             "fromAccountKey": f"1:{prefix}-{src}",
                             "toAccountKey": f"1:{prefix}-{dst}",
                             "amountPaid": amount, "paymentCurrency": "US Dollar",
                             "amountReceived": amount, "receivingCurrency": "US Dollar",
                             "amountUsd": amount, "paymentFormat": "ACH", "isSelfLoop": "0",
                             "isLaundering": str(label), "patternId": "", "patternType": ""})
            targets.append({"split": split, "expectedLabel": label,
                            "targetTxId": rows[-1]["txId"], "scenario": kind})
    graph = MemoryGraph(rows)
    cases = [{"split": t["split"], "expectedLabel": t["expectedLabel"],
              "scenario": t["scenario"], "graph": graph.subgraph(t["targetTxId"])} for t in targets]
    return rows, cases


def main():
    out = ROOT / "data" / "demo"
    out.mkdir(parents=True, exist_ok=True)
    rows, cases = generate()
    with (out / "transactions.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (out / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"kind": "synthetic_smoke_only",
        "transactions": len(rows), "cases": len(cases),
        "note": "Not IBM data. Metrics verify wiring only; not generalization performance."}, indent=2), encoding="utf-8")
    print(f"Created {len(rows)} synthetic transactions, {len(cases)} smoke cases in {out}")


if __name__ == "__main__":
    main()
