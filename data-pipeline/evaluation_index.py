"""Disk-backed source index for offline evaluation preparation only.

This is not an application database. Production graph access stays in Neo4j
through Spring. Public lookups select no labels or pattern annotations.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from graph_contract import EDGE_FIELDS, extract
from preprocess import sha256_of

INDEX_VERSION = 1
FIELDS = EDGE_FIELDS + ["isLaundering", "patternId", "patternType"]


def build(source: Path, output: Path):
    digest = sha256_of(source)
    if output.exists():
        with closing(sqlite3.connect(output)) as db:
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("sourceSha256") != digest or metadata.get("version") != str(INDEX_VERSION):
            raise ValueError("Existing source index belongs to another source/version")
        return metadata
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".building.sqlite3")
    if temporary.exists():
        raise ValueError(f"Incomplete build exists; inspect it before retrying: {temporary}")
    db = sqlite3.connect(temporary)
    try:
        db.execute("PRAGMA journal_mode=OFF")  # unpublished cache, rebuilt after an interrupted build
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA cache_size=-65536")
        columns = ",".join(f'"{name}" TEXT NOT NULL' for name in FIELDS)
        db.execute(f"CREATE TABLE events ({columns}, PRIMARY KEY(txId)) WITHOUT ROWID")
        insert = f"INSERT INTO events VALUES ({','.join('?' for _ in FIELDS)})"
        batch, count, bounds = [], 0, {}
        with source.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                batch.append(tuple(row[key] for key in FIELDS))
                pid = row["patternId"]
                if pid:
                    if pid not in bounds:
                        bounds[pid] = [row["timestamp"], row["timestamp"]]
                    bounds[pid][0] = min(bounds[pid][0], row["timestamp"])
                    bounds[pid][1] = max(bounds[pid][1], row["timestamp"])
                if len(batch) == 10000:
                    db.executemany(insert, batch)
                    count += len(batch)
                    batch.clear()
                    if count % 1000000 == 0:
                        db.commit()
                        print(f"Indexed {count:,} source transactions", flush=True)
        if batch:
            db.executemany(insert, batch)
            count += len(batch)
        db.commit()
        for name, columns in [("source_time", "fromAccountKey,timestamp DESC,txId DESC"),
                              ("destination_time", "toAccountKey,timestamp DESC,txId DESC"),
                              ("event_time", "timestamp,txId")]:
            print(f"Building {name} index", flush=True)
            db.execute(f"CREATE INDEX {name} ON events({columns})")
            db.commit()
        db.execute("CREATE TABLE pattern_bounds(patternId TEXT PRIMARY KEY,firstTime TEXT,lastTime TEXT)")
        db.executemany("INSERT INTO pattern_bounds VALUES (?,?,?)", [(key, *value) for key, value in bounds.items()])
        metadata = {"version": str(INDEX_VERSION), "sourceSha256": digest, "rows": str(count)}
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        db.commit()
    finally:
        db.close()
    temporary.rename(output)
    return metadata


class SourceIndex:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    def target(self, tx_id):
        projection = ",".join(EDGE_FIELDS)
        row = self.db.execute(f"SELECT {projection} FROM events WHERE txId=?", (tx_id,)).fetchone()
        if row is None:
            raise ValueError(f"Missing source transaction {tx_id}")
        return dict(row)

    def lookup(self, account, since, as_of, limit):
        projection = ",".join(EDGE_FIELDS)
        rows = []
        for field in ("fromAccountKey", "toAccountKey"):
            rows.extend(dict(row) for row in self.db.execute(
                f"SELECT {projection} FROM events WHERE {field}=? AND timestamp>=? AND timestamp<=? "
                "AND fromAccountKey<>toAccountKey ORDER BY timestamp DESC,txId DESC LIMIT ?",
                (account, since, as_of, limit)))
        return sorted(rows, key=lambda e: (e["timestamp"], e["txId"]), reverse=True)[:limit]

    def subgraph(self, tx_id, **kwargs):
        graph = extract(self.target(tx_id), self.lookup, **kwargs)
        graph["meta"].update({"sourceScope": "HI-Small-full-offline-index",
                               "sourceGraphComplete": True})
        return graph


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))
