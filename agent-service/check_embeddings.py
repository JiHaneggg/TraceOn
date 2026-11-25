"""Validation-split sanity check for routing similarity (developer tool, not the benchmark).

Reads only transactions in [validation start, holdout start). Accounts of
laundering transactions in that period are positives; a seeded sample of other
accounts active in the same period are negatives. Reports ROC AUC of the
embedding similarity, separately for accounts that were or were not already
training-period reference accounts. The holdout period is never read.

    python agent-service/check_embeddings.py
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import random

import numpy as np
from scipy.stats import rankdata

import _paths
from embedding_similarity import EmbeddingSimilarity
from fastrp_embeddings import OUTPUT, SOURCE

VALIDATION_START = "2022-09-06T13:36:00"   # docs/db-rule-v2.md
HOLDOUT_START = "2022-09-08T16:12:00"


def auc(positive, negative):
    """Mann-Whitney ROC AUC with tie handling; None when a class is empty."""
    if not positive or not negative:
        return None
    ranks = rankdata(np.concatenate([positive, negative]))
    return round(float((ranks[:len(positive)].sum() - len(positive) * (len(positive) + 1) / 2)
                       / (len(positive) * len(negative))), 4)


def validation_accounts(transactions, negatives, seed):
    positive, active = set(), set()
    with open(transactions, newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            if not VALIDATION_START <= row["timestamp"] < HOLDOUT_START or row["isSelfLoop"] == "1":
                continue
            pair = (row["fromAccountKey"], row["toAccountKey"])
            active.update(pair)
            if row["isLaundering"] == "1":
                positive.update(pair)
    pool = sorted(active - positive)
    return sorted(positive), random.Random(seed).sample(pool, min(negatives, len(pool)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--embeddings", type=Path, default=OUTPUT)
    parser.add_argument("--transactions", type=Path, default=SOURCE / "transactions.csv")
    parser.add_argument("--negatives", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    scorer = EmbeddingSimilarity(args.embeddings)
    if scorer.train_end > VALIDATION_START:
        parser.error("Embeddings were trained past the validation start; the check would leak")
    positives, negatives = validation_accounts(args.transactions, args.negatives, args.seed)
    groups = {"all": ([], []), "notInReferenceSet": ([], []), "inReferenceSet": ([], [])}
    coverage = {"positive": [0, len(positives)], "negative": [0, len(negatives)]}
    for label, accounts in (("positive", positives), ("negative", negatives)):
        for key in accounts:
            result = scorer.account_score(key)
            if result["status"] != "computed":
                continue
            coverage[label][0] += 1
            slot = 0 if label == "positive" else 1
            groups["all"][slot].append(result["score"])
            groups["inReferenceSet" if result["accountInReferenceSet"] else "notInReferenceSet"][slot].append(result["score"])
    report = {
        "kind": "validation_split_sanity_check_not_benchmark", "embedding": scorer.base,
        "period": {"from": VALIDATION_START, "toExclusive": HOLDOUT_START},
        "coverage": {label: {"scored": scored, "total": total} for label, (scored, total) in coverage.items()},
        "auc": {name: auc(pos, neg) for name, (pos, neg) in groups.items()},
        "counts": {name: {"positive": len(pos), "negative": len(neg)} for name, (pos, neg) in groups.items()},
        "medianScore": {name: {"positive": round(float(np.median(pos)), 4) if pos else None,
                               "negative": round(float(np.median(neg)), 4) if neg else None}
                        for name, (pos, neg) in groups.items()},
        "negativeSample": {"size": len(negatives), "seed": args.seed},
    }
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
