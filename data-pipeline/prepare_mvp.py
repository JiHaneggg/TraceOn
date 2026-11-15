"""Select two typologies + sampled controls without loading the full graph.

Preserves global source txIds. Balanced selected cases are a pilot, not a
population benchmark. Pattern labels are used only by this evaluation builder.
"""
import argparse
import csv
import heapq
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from graph_contract import MemoryGraph, read_transactions
from preprocess import TX_FIELDS, sha256_of

ROOT = Path(__file__).resolve().parent.parent


def prepare(source, output):
    source_file = source / "transactions.csv"
    latest = {}
    with (source / "patterns.csv").open(newline="", encoding="utf-8") as stream:
        sizes = {r["patternId"]: int(r["txCount"]) for r in csv.DictReader(stream)}
    print("[1/3] Select CYCLE/FAN-IN targets and normal controls", flush=True)
    for row in read_transactions(source_file):
        if (row["patternType"] == "CYCLE" and 2 <= sizes.get(row["patternId"], 0) <= 4
                or row["patternType"] == "FAN-IN" and 3 <= sizes.get(row["patternId"], 0) <= 10):
            key = row["patternId"]
            if key not in latest or (row["timestamp"], row["txId"]) > (latest[key]["timestamp"], latest[key]["txId"]):
                latest[key] = row
    positive = []
    for kind in ["CYCLE", "FAN-IN"]:
        candidates = sorted([r for r in latest.values() if r["patternType"] == kind],
                            key=lambda r: (r["timestamp"], r["txId"]))
        if len(candidates) < 2:
            raise ValueError(f"Need two distinct pattern instances for {kind}")
        positive.extend([candidates[0], candidates[-1]])
    positive.sort(key=lambda r: (r["timestamp"], r["txId"]))
    cutoff = positive[len(positive)//2]["timestamp"]
    targets = list(positive)
    # Sample each time split separately: rare late-period normals are otherwise
    # lost in a global reservoir dominated by the early high-volume period.
    controls, seen = [[], []], [0, 0]
    randomizer = random.Random(20261114)
    for row in read_transactions(source_file):
        if row["isLaundering"] != "0" or row["isSelfLoop"] != "0":
            continue
        bucket = int(row["timestamp"] >= cutoff)
        seen[bucket] += 1
        if len(controls[bucket]) < 4:
            controls[bucket].append(row)
        else:
            index = randomizer.randrange(seen[bucket])
            if index < 4:
                controls[bucket][index] = row
    for pool in controls:
        if not pool:
            raise ValueError("Insufficient normal controls in chronological split")
        targets.extend(sorted(pool, key=lambda r: (r["timestamp"], r["txId"])))

    selected = [{r["txId"]: r} for r in targets]
    frontiers = [{r["fromAccountKey"], r["toAccountKey"]} for r in targets]
    visited = [set() for _ in targets]
    truncations = [False for _ in targets]
    for hop in range(2):
        print(f"[{hop+2}/3] Bounded neighborhood scan (hop {hop+1})", flush=True)
        by_account = defaultdict(list)
        for i, frontier in enumerate(frontiers):
            for account in frontier - visited[i]:
                by_account[account].append(i)
                visited[i].add(account)
        heaps = defaultdict(list)
        since = [(datetime.fromisoformat(t["timestamp"]) - timedelta(days=7)).isoformat() for t in targets]
        for row in read_transactions(source_file):
            if row["isSelfLoop"] == "1":
                continue
            for account in [row["fromAccountKey"], row["toAccountKey"]]:
                for i in by_account.get(account, []):
                    if since[i] <= row["timestamp"] <= targets[i]["timestamp"]:
                        heap = heaps[i, account]
                        item = (row["timestamp"], row["txId"], row)
                        if len(heap) < 51:
                            heapq.heappush(heap, item)
                        elif item[:2] > heap[0][:2]:
                            heapq.heapreplace(heap, item)
        frontiers = [set() for _ in targets]
        for (i, account), heap in sorted(heaps.items()):
            truncations[i] |= len(heap) > 50
            for _, txid, row in sorted(heap, reverse=True)[:50]:
                if txid not in selected[i] and len(selected[i]) >= 1000:
                    truncations[i] = True
                    continue
                selected[i][txid] = row
                frontiers[i].update([row["fromAccountKey"], row["toAccountKey"]])

    union = {txid: row for case in selected for txid, row in case.items()}
    graph = MemoryGraph(union.values())
    cases = []
    for i, target in enumerate(targets):
        public = graph.subgraph(target["txId"])
        public["meta"].update({"sourceScope": "HI-Small-MVP", "sourceGraphComplete": False,
                                "truncated": truncations[i] or public["meta"]["truncated"]})
        cases.append({"split": "validation" if target["timestamp"] < cutoff else "test",
                      "expectedLabel": int(target["isLaundering"]), "graph": public})
    output.mkdir(parents=True, exist_ok=True)
    with (output / "transactions.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TX_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(union.values(), key=lambda r: r["txId"]))
    (output / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
    manifest = {"source": str(source_file), "sourceSha256": sha256_of(source_file),
                "scope": "selected_case_pilot_not_population_benchmark", "seed": 20261114,
                "typologies": ["CYCLE", "FAN-IN"], "cutoff": cutoff,
                "patternSizeSelection": {"CYCLE": "2..4 transactions", "FAN-IN": "3..10 transactions"},
                "transactions": len(union), "targets": [t["txId"] for t in targets],
                "cases": len(cases), "sourceGraphComplete": False}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "data/processed/HI-Small")
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed/HI-Small-MVP")
    args = parser.parse_args()
    prepare(args.source, args.output)
