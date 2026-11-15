"""Shared, label-free graph projection used by DB validation tools.

Production extraction belongs in the Spring backend (ADR-001).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

EDGE_FIELDS = ["txId", "timestamp", "fromAccountKey", "toAccountKey", "amountPaid",
               "paymentCurrency", "amountReceived", "receivingCurrency", "paymentFormat"]


def public_edge(row):
    return {key: row[key] for key in EDGE_FIELDS}


def extract(target, lookup, hops=2, max_neighbors=50, max_edges=1000, window_days=7):
    if not 1 <= hops <= 3 or not 1 <= max_neighbors <= 100 or not 1 <= max_edges <= 2000:
        raise ValueError("Invalid extraction limits")
    if not 1 <= window_days <= 7:
        raise ValueError("window_days must be in 1..7")
    as_of = datetime.fromisoformat(target["timestamp"])
    since = (as_of - timedelta(days=window_days)).isoformat(timespec="seconds")
    selected = {target["txId"]: public_edge(target)}
    frontier = {target["fromAccountKey"], target["toAccountKey"]}
    visited = set()
    truncated = False
    for _ in range(hops):
        next_frontier = set()
        for account in sorted(frontier - visited):
            visited.add(account)
            rows = lookup(account, since, target["timestamp"], max_neighbors + 1)
            truncated |= len(rows) > max_neighbors
            for edge in rows[:max_neighbors]:
                if edge["txId"] not in selected and len(selected) >= max_edges:
                    truncated = True
                    continue
                selected[edge["txId"]] = public_edge(edge)
                next_frontier.update([edge["fromAccountKey"], edge["toAccountKey"]])
        frontier = next_frontier - visited
    edges = sorted(selected.values(), key=lambda e: (e["timestamp"], e["txId"]))
    return {"schemaVersion": "1.0", "targetTxId": target["txId"], "asOf": target["timestamp"],
            "windowStart": since,
            "nodes": [{"accountKey": key} for key in sorted({e[k] for e in edges
                         for k in ["fromAccountKey", "toAccountKey"]})],
            "edges": edges,
            "meta": {"hops": hops, "maxNeighbors": max_neighbors, "maxEdges": max_edges,
                     "truncated": truncated, "timeDirection": "past-only"}}


class MemoryGraph:
    """Only for small fixtures/MVP subsets, not millions of raw transactions."""
    def __init__(self, rows):
        self.transactions = {}
        self.adjacency = defaultdict(list)
        for row in rows:
            if row["txId"] in self.transactions:
                raise ValueError("Duplicate transaction ID")
            self.transactions[row["txId"]] = public_edge(row)
            if row["fromAccountKey"] != row["toAccountKey"]:
                self.adjacency[row["fromAccountKey"]].append(public_edge(row))
                self.adjacency[row["toAccountKey"]].append(public_edge(row))

    def lookup(self, account, since, as_of, limit):
        rows = [e for e in self.adjacency[account] if since <= e["timestamp"] <= as_of]
        return sorted(rows, key=lambda e: (e["timestamp"], e["txId"]), reverse=True)[:limit]

    def subgraph(self, tx_id, **kwargs):
        return extract(self.transactions[tx_id], self.lookup, **kwargs)


def read_transactions(path: Path):
    with path.open(newline="", encoding="utf-8") as stream:
        yield from csv.DictReader(stream)
