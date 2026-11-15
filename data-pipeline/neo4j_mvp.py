"""Neo4j administrator smoke tool. Runtime application access stays in Spring.

Loads a bounded subset non-destructively and exports the common public graph.
Uses the Neo4j 5.26 local transactional HTTP endpoint, with credentials in .env.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from itertools import islice
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from graph_contract import EDGE_FIELDS, extract, read_transactions

ROOT = Path(__file__).resolve().parent.parent
PROJECTION = """{txId:t.txId, timestamp:toString(t.timestamp),
fromAccountKey:s.accountKey, toAccountKey:d.accountKey,
amountPaid:t.amountPaidExact, paymentCurrency:t.paymentCurrency,
amountReceived:t.amountReceivedExact, receivingCurrency:t.receivingCurrency,
paymentFormat:t.paymentFormat}"""
TARGET_QUERY = f"""MATCH (s:Account)-[t:TRANSFER {{txId:$txId}}]->(d:Account)
RETURN {PROJECTION} AS edge"""
NEIGHBOR_QUERY = f"""MATCH (a:Account {{accountKey:$accountKey}})-[t:TRANSFER]-(b:Account)
WHERE t.timestamp >= localdatetime($since) AND t.timestamp <= localdatetime($asOf)
AND a <> b
WITH t ORDER BY t.timestamp DESC, t.txId DESC LIMIT $limit
MATCH (s:Account)-[t]->(d:Account)
RETURN {PROJECTION} AS edge"""


def credentials():
    values = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    values.update(os.environ)
    password = values.get("NEO4J_PASSWORD")
    if not password:
        raise ValueError("Set NEO4J_PASSWORD in .env or environment")
    return values.get("NEO4J_USER", "neo4j"), password


class Database:
    def __init__(self, url="http://127.0.0.1:7474", database="neo4j"):
        if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("MVP administrator tool only accepts loopback database URLs")
        user, password = credentials()
        self.authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.url = f"{url}/db/{database}/tx/commit"

    def query(self, statement, parameters=None):
        payload = json.dumps({"statements": [{"statement": statement,
                                             "parameters": parameters or {}}]}).encode()
        request = Request(self.url, data=payload, headers={"Content-Type": "application/json",
                                                         "Authorization": self.authorization})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result["errors"]:
            raise RuntimeError(result["errors"])
        block = result["results"][0]
        return [dict(zip(block["columns"], row["row"])) for row in block["data"]]

    def schema(self):
        self.query("CREATE CONSTRAINT account_key_unique IF NOT EXISTS FOR (a:Account) REQUIRE a.accountKey IS UNIQUE")
        self.query("CREATE CONSTRAINT dataset_name_unique IF NOT EXISTS FOR (m:TraceOnDataset) REQUIRE m.name IS UNIQUE")
        self.query("CREATE INDEX transfer_tx_id IF NOT EXISTS FOR ()-[t:TRANSFER]-() ON (t.txId)")
        self.query("CREATE INDEX transfer_timestamp IF NOT EXISTS FOR ()-[t:TRANSFER]-() ON (t.timestamp)")
        self.query("CALL db.awaitIndexes(60)")

    def load(self, path: Path, dataset: str):
        # This path deliberately cannot overwrite/import an existing unrelated DB.
        from preprocess import sha256_of
        digest = sha256_of(path)
        existing = self.query("MATCH (m:TraceOnDataset) RETURN m.name AS name, m.sha256 AS sha256")
        if existing and any(m["name"] != dataset or m["sha256"] != digest for m in existing):
            raise ValueError("Database contains another dataset/version; use a separate database instance")
        if not existing and self.query("MATCH (n) RETURN count(n) AS n")[0]["n"]:
            raise ValueError("Refusing to load into a nonempty unmanaged database")
        rows = list(islice(read_transactions(path), 20001))
        if not rows:
            raise ValueError("Empty input")
        if len(rows) > 20000:
            raise ValueError("MVP loader supports at most 20,000 rows; prepare a subset first")
        if len({r["txId"] for r in rows}) != len(rows):
            raise ValueError("Duplicate txId in input")
        self.schema()
        self.query("MERGE (m:TraceOnDataset {name:$name}) SET m.sha256=$sha256",
                   {"name": dataset, "sha256": digest})
        statement = """UNWIND $rows AS row
MERGE (s:Account {accountKey:row.fromAccountKey})
MERGE (d:Account {accountKey:row.toAccountKey})
MERGE (s)-[t:TRANSFER {txId:row.txId}]->(d)
SET s.bankId=toInteger(split(row.fromAccountKey,':')[0]),
s.accountNumber=split(row.fromAccountKey,':')[1],
d.bankId=toInteger(split(row.toAccountKey,':')[0]),
d.accountNumber=split(row.toAccountKey,':')[1],
t.timestamp=localdatetime(row.timestamp),
t.amountPaidExact=row.amountPaid, t.amountReceivedExact=row.amountReceived,
t.amountPaid=toFloat(row.amountPaid), t.amountReceived=toFloat(row.amountReceived),
t.paymentCurrency=row.paymentCurrency, t.receivingCurrency=row.receivingCurrency,
t.paymentFormat=row.paymentFormat
RETURN count(t) AS loaded"""
        for i in range(0, len(rows), 500):
            safe = [{key: row[key] for key in EDGE_FIELDS} for row in rows[i:i+500]]
            self.query(statement, {"rows": safe})
        counts = self.query("MATCH ()-[t:TRANSFER]->() RETURN count(t) AS transactions")[0]
        if counts["transactions"] != len(rows):
            raise ValueError("Database/source transaction counts differ")
        return {**counts, "sourceSha256": digest,
                "accounts": self.query("MATCH (a:Account) RETURN count(a) AS n")[0]["n"]}

    def subgraph(self, tx_id, **kwargs):
        found = self.query(TARGET_QUERY, {"txId": tx_id})
        if len(found) != 1:
            raise ValueError("Target txId must match exactly one transaction")
        def lookup(account, since, as_of, limit):
            return [row["edge"] for row in self.query(NEIGHBOR_QUERY,
                    {"accountKey": account, "since": since, "asOf": as_of, "limit": limit})]
        graph = extract(found[0]["edge"], lookup, **kwargs)
        marker = self.query("MATCH (m:TraceOnDataset) RETURN m.name AS name")
        graph["meta"].update({"sourceScope": marker[0]["name"] if marker else "unknown",
                              "sourceGraphComplete": False})
        return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:7474")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("load")
    load.add_argument("--transactions", required=True, type=Path)
    load.add_argument("--dataset", required=True)
    graph = commands.add_parser("subgraph")
    graph.add_argument("--tx-id", required=True)
    graph.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    db = Database(args.url)
    if args.command == "load":
        print(json.dumps(db.load(args.transactions, args.dataset), indent=2))
    else:
        result = db.subgraph(args.tx_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Exported {len(result['edges'])} edges to {args.output}")


if __name__ == "__main__":
    main()
