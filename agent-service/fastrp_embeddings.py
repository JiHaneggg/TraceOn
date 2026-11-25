"""Offline FastRP account embeddings for routing similarity (PPT 데이터 계층: 노드 임베딩).

Same algorithm as Neo4j GDS `fastRP` (Chen et al., 2019, "Fast and Accurate
Network Embeddings via Very Sparse Random Projection"): each account starts from
a very sparse random vector, part of which is a random projection of its own
in/out activity (GDS `propertyRatio`), then neighbour averages are taken
repeatedly and the iterations are summed with fixed weights.

Only transactions strictly before --train-end are read, so validation and test
structure never leaks into the vectors. Reference accounts are the accounts of
laundering transactions in that same training period. Requires numpy and scipy.

    python agent-service/fastrp_embeddings.py --train-end 2022-09-06T13:36:00
"""
from __future__ import annotations

import argparse
from array import array
import csv
import hashlib
import json
from pathlib import Path
import re
import time

import numpy as np
from scipy import sparse

import _paths

VERSION = "fastrp-v1"
PARAMETERS = {"dimension": 64, "propertyRatio": 0.25, "iterationWeights": [0.0, 1.0, 1.0],
              "sparsity": 3, "seed": 20261125, "orientation": "undirected", "relationshipWeight": None}
NODE_FEATURES = ("outCounterparties", "inCounterparties", "outTxCount", "inTxCount", "outUsd", "inUsd")
SOURCE = _paths.ROOT / "data" / "processed" / "HI-Small"
OUTPUT = _paths.ROOT / "data" / "embeddings" / "HI-Small-fastrp-v1"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def read_training_graph(transactions, train_end):
    """Directed training edges as integer arrays; labels only select reference accounts."""
    index, keys = {}, []
    sources, targets, usd = array("i"), array("i"), array("d")
    references, rows = set(), 0

    def node(key):
        position = index.get(key)
        if position is None:
            position = index[key] = len(keys)
            keys.append(key)
        return position

    with open(transactions, newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        header = next(reader)
        col = {name: header.index(name) for name in
               ("timestamp", "fromAccountKey", "toAccountKey", "amountUsd", "isSelfLoop", "isLaundering")}
        for row in reader:
            if not TIMESTAMP.match(row[col["timestamp"]]):
                raise ValueError("Unexpected timestamp format in transactions file")
            if row[col["timestamp"]] >= train_end or row[col["isSelfLoop"]] == "1":
                continue
            source, target = node(row[col["fromAccountKey"]]), node(row[col["toAccountKey"]])
            sources.append(source)
            targets.append(target)
            usd.append(float(row[col["amountUsd"]] or 0))
            rows += 1
            if row[col["isLaundering"]] == "1":
                references.update((source, target))
    return (keys, np.frombuffer(sources, np.int32).astype(np.int64),
            np.frombuffer(targets, np.int32).astype(np.int64), np.frombuffer(usd, np.float64),
            sorted(references), rows)


def node_features(count, sources, targets, usd):
    pairs = np.unique(sources * count + targets)
    columns = [np.bincount(pairs // count, minlength=count), np.bincount(pairs % count, minlength=count),
               np.bincount(sources, minlength=count), np.bincount(targets, minlength=count),
               np.bincount(sources, weights=usd, minlength=count),
               np.bincount(targets, weights=usd, minlength=count)]
    features = np.log1p(np.column_stack(columns).astype(np.float64))
    return (features - features.mean(axis=0)) / (features.std(axis=0) + 1e-12)


def normalize(matrix):
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)


def fastrp(count, sources, targets, features, parameters=PARAMETERS):
    low, high = np.minimum(sources, targets), np.maximum(sources, targets)
    undirected = np.unique(low * count + high)
    left, right = undirected // count, undirected % count
    adjacency = sparse.csr_matrix((np.ones(2 * len(undirected), np.float32),
                                   (np.concatenate([left, right]), np.concatenate([right, left]))),
                                  shape=(count, count))
    degree = np.asarray(adjacency.sum(axis=1)).ravel()
    averaging = sparse.diags(np.divide(1.0, degree, out=np.zeros_like(degree), where=degree > 0)) @ adjacency

    rng = np.random.default_rng(parameters["seed"])
    property_dims = round(parameters["dimension"] * parameters["propertyRatio"])
    random_dims = parameters["dimension"] - property_dims
    s = parameters["sparsity"]
    scale = np.sqrt(s)
    initial = rng.choice(np.array([scale, 0.0, -scale], np.float32), size=(count, random_dims),
                         p=[1 / (2 * s), 1 - 1 / s, 1 / (2 * s)])
    projection = rng.standard_normal((features.shape[1], property_dims))
    current = normalize(np.hstack([initial, (features @ projection).astype(np.float32)]))
    embedding = np.zeros_like(current)
    for weight in parameters["iterationWeights"]:
        current = normalize(np.asarray(averaging @ current, dtype=np.float32))
        embedding += np.float32(weight) * current
    return normalize(embedding).astype(np.float32), len(undirected)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build(transactions, train_end, output, parameters=PARAMETERS, source_manifest=None):
    if not TIMESTAMP.match(train_end):
        raise ValueError("train_end must look like 2022-09-06T13:36:00")
    output = Path(output)
    if output.exists():
        raise ValueError("Output already exists; use a new version folder instead of overwriting")
    started = time.perf_counter()
    keys, sources, targets, usd, references, rows = read_training_graph(transactions, train_end)
    if not rows:
        raise ValueError("No training transactions before train_end")
    vectors, edges = fastrp(len(keys), sources, targets, node_features(len(keys), sources, targets, usd), parameters)
    output.mkdir(parents=True)
    np.save(output / "embeddings.npy", vectors)
    (output / "accounts.json").write_text(json.dumps(keys), encoding="utf-8", newline="\n")
    (output / "references.json").write_text(json.dumps(
        {"source": "accounts in laundering transactions before trainEnd",
         "accountKeys": [keys[i] for i in references]}), encoding="utf-8", newline="\n")
    manifest = {
        "version": VERSION, "algorithm": "FastRP (Chen et al., 2019), same as Neo4j GDS fastRP",
        "parameters": parameters, "nodeFeatures": list(NODE_FEATURES), "trainEnd": train_end,
        "trainTransactions": rows, "nodes": len(keys), "undirectedEdges": edges,
        "referenceAccounts": len(references),
        "sourceManifestSha256": sha256_file(source_manifest) if source_manifest else None,
        "files": {name: sha256_file(output / name) for name in ("embeddings.npy", "accounts.json", "references.json")},
        "buildSeconds": round(time.perf_counter() - started, 1),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transactions", type=Path, default=SOURCE / "transactions.csv")
    parser.add_argument("--train-end", required=True, help="exclusive training cutoff, e.g. 2022-09-06T13:36:00")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    manifest_path = args.transactions.parent / "manifest.json"
    try:
        result = build(args.transactions, args.train_end, args.output,
                       source_manifest=manifest_path if manifest_path.exists() else None)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ("version", "trainEnd", "trainTransactions", "nodes",
                                                   "undirectedEdges", "referenceAccounts", "buildSeconds")}))


if __name__ == "__main__":
    main()
