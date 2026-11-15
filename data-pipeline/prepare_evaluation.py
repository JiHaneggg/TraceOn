"""Freeze a prospective, weighted case-control evaluation from the full source.

No model/rule output is used for target sampling. The old pilot's targets and
pattern families are excluded. Holdout pattern families start after the cutoff.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from evaluation_index import SourceIndex
from preprocess import sha256_of

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261114


def allocations(population, requested):
    """At least one per available positive stratum, then proportional capacity."""
    counts = {key: 1 for key, n in population.items() if n > 0}
    if requested < len(counts):
        raise ValueError("Requested sample cannot represent all available strata")
    goal = min(requested, sum(population.values()))
    while sum(counts.values()) < goal:
        available = [key for key in counts if counts[key] < population[key]]
        key = max(available, key=lambda key: (population[key] / (counts[key] + 1), key))
        counts[key] += 1
    return counts


def old_exclusions(path):
    ids, patterns = set(), set()
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            ids.add(row["txId"])
            if row["patternId"]:
                patterns.add(row["patternId"])
    return ids, patterns


def select_targets(index, start, end, excluded_ids, excluded_patterns, *, holdout, positives, normals):
    query = """SELECT e.txId,e.timestamp,e.isLaundering,e.patternId,e.patternType,
                       p.firstTime FROM events e LEFT JOIN pattern_bounds p ON p.patternId=e.patternId
                WHERE e.timestamp>=? AND e.timestamp<? ORDER BY e.txId"""
    # Streaming passes keep memory bounded even when a split contains millions.
    def eligible():
        for raw in index.db.execute(query, (start, end)):
            row = dict(raw)
            if row["txId"] in excluded_ids or row["patternId"] in excluded_patterns:
                continue
            if holdout and row["patternId"] and row["firstTime"] < start:
                continue
            row["stratum"] = "normal" if row["isLaundering"] == "0" else "positive:" + (row["patternType"] or "UNMAPPED")
            yield row
    population = Counter(row["stratum"] for row in eligible())
    positive_pop = {key: n for key, n in population.items() if key != "normal"}
    if not positive_pop or not population["normal"]:
        raise ValueError("Each time split needs both classes")
    requested = allocations(positive_pop, positives)
    requested["normal"] = min(normals, population["normal"])
    generators = {key: random.Random(SEED + int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")) for key in requested}
    selected, seen = defaultdict(list), Counter()
    for row in eligible():
        key = row["stratum"]
        seen[key] += 1
        if len(selected[key]) < requested[key]:
            selected[key].append(row)
        else:
            replacement = generators[key].randrange(seen[key])
            if replacement < requested[key]:
                selected[key][replacement] = row
    rows = []
    for key, sample in selected.items():
        for row in sample:
            rows.append({**row, "samplingWeight": population[key] / len(sample)})
    return sorted(rows, key=lambda r: (r["timestamp"], r["txId"])), dict(population), requested


def prepare(index_path, output, old_pilot, positives=100, normals=150):
    if output.exists():
        raise ValueError("Evaluation folder already exists; choose a new version instead of overwriting")
    excluded_ids, excluded_patterns = old_exclusions(old_pilot)
    index = SourceIndex(index_path)
    try:
        metadata = dict(index.db.execute("SELECT key,value FROM metadata"))
        count = int(metadata["rows"])
        cutoffs = [index.db.execute("SELECT timestamp FROM events ORDER BY timestamp,txId LIMIT 1 OFFSET ?",
                                    (int(count * fraction),)).fetchone()[0] for fraction in [.6, .8]]
        if cutoffs[0] >= cutoffs[1]:
            raise ValueError("Source does not support distinct chronological cutoffs")
        selected = {}
        populations, sample_sizes = {}, {}
        for split, start, end in [("validation", cutoffs[0], cutoffs[1]),
                                  ("holdout", cutoffs[1], "9999-12-31T23:59:59")]:
            selected[split], populations[split], sample_sizes[split] = select_targets(
                index, start, end, excluded_ids, excluded_patterns, holdout=split == "holdout",
                positives=positives, normals=normals)
        validation_families = {r["patternId"] for r in selected["validation"] if r["patternId"]}
        holdout_families = {r["patternId"] for r in selected["holdout"] if r["patternId"]}
        if validation_families & holdout_families:
            raise AssertionError("Pattern family overlap")
        output.mkdir(parents=True)
        files = {}
        for split, targets in selected.items():
            path = output / f"{split}.jsonl"
            with path.open("w", encoding="utf-8") as stream:
                for number, target in enumerate(targets, 1):
                    graph = index.subgraph(target["txId"])
                    case = {"split": split, "expectedLabel": int(target["isLaundering"]),
                            "expectedPatternType": target["patternType"] or "UNMAPPED",
                            "patternFamily": target["patternId"], "samplingWeight": target["samplingWeight"],
                            "stratum": target["stratum"], "graph": graph}
                    stream.write(json.dumps(case, separators=(",", ":")) + "\n")
                    if number % 50 == 0:
                        print(f"Prepared {split} {number}/{len(targets)}", flush=True)
            files[split] = {"file": path.name, "sha256": sha256_of(path), "cases": len(targets)}
        manifest = {"version": 2, "seed": SEED, "sourceSha256": metadata["sourceSha256"],
                    "timeCutoffs": {"validationStart": cutoffs[0], "holdoutStart": cutoffs[1]},
                    "selection": "stratified_case_control_with_inverse_sampling_weights",
                    "excludedPriorTransactions": len(excluded_ids), "excludedPriorPatternFamilies": len(excluded_patterns),
                    "priorPilotSha256": sha256_of(old_pilot),
                    "holdoutPolicy": "pattern families must start on or after holdout cutoff",
                    "patternFamilyOverlap": 0, "population": populations, "sampleSizes": sample_sizes,
                    "graphPolicy": {"hops": 2, "maxNeighbors": 50, "maxEdges": 1000, "pastDays": 7},
                    "files": files}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest, indent=2))
        return manifest
    finally:
        index.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--old-pilot", type=Path, default=ROOT / "data/processed/HI-Small-MVP/transactions.csv")
    parser.add_argument("--positives", type=int, default=100)
    parser.add_argument("--normals", type=int, default=150)
    args = parser.parse_args()
    prepare(args.index, args.output, args.old_pilot, args.positives, args.normals)
