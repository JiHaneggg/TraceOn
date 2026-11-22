"""Replay the frozen V2 graph retrieval against a dedicated local Neo4j store."""
import argparse
import base64
from collections import Counter
from contextlib import closing
import csv
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import perf_counter, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / part) for part in ("data-pipeline", "rule-service")]
from evaluation_index import SourceIndex
from experiment import digest, validate_file
from graph_contract import extract
from neo4j_mvp import NEIGHBOR_QUERY, TARGET_QUERY
from rules import RuleConfig, score

SPLITS = ("validation", "holdout")
GRAPH_POLICY = {"hops": 2, "max_neighbors": 50, "max_edges": 1000, "window_days": 7}
SCHEMA = (
    "CREATE CONSTRAINT account_key_unique IF NOT EXISTS FOR (a:Account) REQUIRE a.accountKey IS UNIQUE",
    "CREATE INDEX transfer_tx_id IF NOT EXISTS FOR ()-[t:TRANSFER]-() ON (t.txId)",
)
COUNT = "MATCH ()-[t:TRANSFER]->() RETURN count(t) AS n"
ACCOUNTS = "MATCH (a:Account) RETURN count(a) AS n"
READY = "RETURN 1 AS ready"
BATCH_SIZE = 32


def write_json(path, obj):
    with path.open("x", encoding="utf-8") as file:
        json.dump(obj, file, ensure_ascii=False, indent=2)
        file.write("\n")


def fingerprint(obj):
    return sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def frozen_inputs():
    folder = ROOT / "data/evaluation/HI-Small-v2"
    manifest = json.loads((folder / "manifest.json").read_bytes())
    frozen_path = ROOT / "artifacts/rule-v2-frozen.json"
    frozen = json.loads(frozen_path.read_bytes())
    if (digest(frozen_path) != frozen_path.with_suffix(".json.sha256").read_text(encoding="ascii").strip()
            or frozen["phase"] != "frozen_before_holdout"
            or digest(folder / "manifest.json") != frozen["manifestSha256"]
            or digest(ROOT / "rule-service/rules.py") != frozen["engineSha256"]
            or any(manifest["files"][split]["sha256"] != frozen[split + "Sha256"] for split in SPLITS)
            or manifest["graphPolicy"] != {"hops": 2, "maxNeighbors": 50, "maxEdges": 1000, "pastDays": 7}):
        raise ValueError("Frozen V2 data, policy or rule engine does not match its seal")
    cases = [(split, row["graph"]) for split in SPLITS for row in validate_file(folder, manifest, split)]
    if len(cases) != len({graph["targetTxId"] for _, graph in cases}):
        raise ValueError("Target IDs overlap between V2 splits")
    return manifest, frozen, cases


def comparable_graph(graph):
    # Compare all graph fields except source provenance. This is a scoped replay DB.
    return {**graph, "meta": {key: value for key, value in graph["meta"].items()
                            if key not in ("sourceScope", "sourceGraphComplete")}}


def prepare(output):
    manifest, frozen, cases = frozen_inputs()
    recorded_queries, source_rows, traces = {}, {}, {}
    with closing(SourceIndex(ROOT / "data/evaluation/HI-Small-source.sqlite3")) as source:
        metadata = dict(source.db.execute("SELECT key,value FROM metadata"))
        if metadata["sourceSha256"] != manifest["sourceSha256"]:
            raise ValueError("SQLite index points to another source dataset")
        for _, expected in cases:
            target = source.target(expected["targetTxId"])
            source_rows[target["txId"]] = target
            keys = []

            def lookup(account, since, as_of, limit):
                key = (account, since, as_of, limit)
                keys.append(key)
                if key not in recorded_queries:
                    rows = source.lookup(*key)
                    source_rows.update((row["txId"], row) for row in rows)
                    recorded_queries[key] = [row["txId"] for row in rows]
                return [source_rows[id_] for id_ in recorded_queries[key]]

            actual = extract(target, lookup, **GRAPH_POLICY)
            if comparable_graph(expected) != comparable_graph(actual):
                raise ValueError(f"Rebuilding from the frozen SQLite source changed {target['txId']}")
            traces[target["txId"]] = keys

    accounts = sorted({row[key] for row in source_rows.values() for key in ("fromAccountKey", "toAccountKey")})
    with (output / "accounts.csv").open("x", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("accountKey:ID(Account)", ":LABEL"))
        writer.writerows((account, "Account") for account in accounts)
    with (output / "transfers.csv").open("x", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("txId:string", "timestamp:localdatetime", ":START_ID(Account)", ":END_ID(Account)",
                         "amountPaidExact:string", "paymentCurrency:string", "amountReceivedExact:string",
                         "receivingCurrency:string", "paymentFormat:string", ":TYPE"))
        fields = ("txId", "timestamp", "fromAccountKey", "toAccountKey", "amountPaid", "paymentCurrency",
                  "amountReceived", "receivingCurrency", "paymentFormat")
        writer.writerows([*(row[field] for field in fields), "TRANSFER"] for _, row in sorted(source_rows.items()))
    bundle = {
        "manifestSha256": frozen["manifestSha256"], "sourceSha256": manifest["sourceSha256"],
        "ruleEngineSha256": frozen["engineSha256"], "accounts": len(accounts), "transactions": len(source_rows),
        "accountsSha256": digest(output / "accounts.csv"), "transfersSha256": digest(output / "transfers.csv"),
        "targetTraces": traces, "lookups": [[*key, ids] for key, ids in recorded_queries.items()],
        "sourceIndexTransactions": int(metadata["rows"]),
    }
    write_json(output / "replay.json", bundle)
    print(f"Prepared {len(cases)} targets, {len(recorded_queries):,} distinct account queries, "
          f"{len(source_rows):,} transactions.", flush=True)


class EvaluationDB:
    def __init__(self, port):
        if not 17474 <= port <= 17574:
            raise ValueError("Use a dedicated evaluation port from 17474 to 17574")
        self.url = f"http://127.0.0.1:{port}/db/neo4j/tx/commit"
        credential = "neo4j:" + os.environ["TRACEON_EVAL_PASSWORD"]
        self.authorization = "Basic " + base64.b64encode(credential.encode()).decode("ascii")
        self.opener = build_opener(ProxyHandler({}))

    def batch(self, requests):
        payload = {"statements": [{"statement": query, "parameters": params} for query, params in requests]}
        request = Request(self.url, json.dumps(payload).encode(), {
            "Authorization": self.authorization, "Content-Type": "application/json"
        })
        with self.opener.open(request, timeout=45) as response:
            result = json.loads(response.read(4_000_000))
        if errors := result["errors"]:
            raise RuntimeError("Neo4j returned " + ", ".join(error["code"] for error in errors))
        if len(result["results"]) != len(requests):
            raise ValueError("Neo4j response count differs from submitted query count")
        return [[dict(zip(table["columns"], item["row"])) for item in table["data"]] for table in result["results"]]

    def one(self, statement, **params):
        return self.batch([(statement, params)])[0]

    def wait_ready(self):
        for _ in range(45):
            try:
                self.one(READY)
                return
            except URLError:
                sleep(1)
        raise RuntimeError("Evaluation Neo4j did not become ready")


def compare(output, port):
    manifest, frozen, cases = frozen_inputs()
    bundle = json.loads((output / "replay.json").read_bytes())
    if (bundle["manifestSha256"] != frozen["manifestSha256"]
            or bundle["sourceSha256"] != manifest["sourceSha256"]
            or bundle["ruleEngineSha256"] != frozen["engineSha256"]
            or any(digest(output / (kind + ".csv")) != bundle[kind + "Sha256"] for kind in ("accounts", "transfers"))):
        raise ValueError("Prepared replay differs from frozen inputs")
    db = EvaluationDB(port)
    db.wait_ready()
    version = db.one("CALL dbms.components() YIELD versions RETURN versions[0] AS version")[0]["version"]
    if db.one(COUNT) != [{"n": bundle["transactions"]}] or db.one(ACCOUNTS) != [{"n": bundle["accounts"]}]:
        raise ValueError("Dedicated Neo4j counts differ from the prepared replay")
    for statement in SCHEMA:
        db.one(statement)
    db.one("CALL db.awaitIndexes(30)")

    start = perf_counter()
    requests = [(TARGET_QUERY, {"txId": graph["targetTxId"]}) for _, graph in cases]
    query_keys = []
    for account, since, as_of, limit, _ in bundle["lookups"]:
        query_keys.append((account, since, as_of, limit))
        requests.append((NEIGHBOR_QUERY, {"accountKey": account, "since": since, "asOf": as_of, "limit": limit}))
    actual_rows = []
    for pos in range(0, len(requests), BATCH_SIZE):
        actual_rows.extend(db.batch(requests[pos:pos + BATCH_SIZE]))
    query_seconds = perf_counter() - start
    targets = actual_rows[:len(cases)]
    lookups = {key: [row["edge"] for row in rows] for key, rows in zip(query_keys, actual_rows[len(cases):])}
    matched_queries = sum(
        [row["txId"] for row in lookups[tuple(query[:4])]] == query[4] for query in bundle["lookups"]
    )
    configs = {name: RuleConfig(**frozen[key]) for name, key in
               (("baseline", "baselineConfig"), ("candidate", "selectedConfig"))}
    by_split = {split: Counter() for split in SPLITS}
    failures = []
    case_hashes = {}
    for (split, expected), found in zip(cases, targets):
        target_id = expected["targetTxId"]
        permitted = {tuple(key) for key in bundle["targetTraces"][target_id]}
        counts = by_split[split]
        counts["cases"] += 1
        counts["expectedTruncated"] += bool(expected["meta"]["truncated"])

        def lookup(*key):
            if key not in permitted:
                raise KeyError("Unexpected account reached during Neo4j replay")
            return lookups[key]

        if len(found) != 1:
            failures.append({"targetTxId": target_id, "split": split, "failed": ["targetUnique"]})
            continue
        try:
            actual = extract(found[0]["edge"], lookup, **GRAPH_POLICY)
        except KeyError:
            failures.append({"targetTxId": target_id, "split": split, "failed": ["lookupScope"]})
            continue
        actual["meta"].update(sourceScope="neo4j-evaluation-replay", sourceGraphComplete=False)
        counts["neo4jTruncated"] += bool(actual["meta"]["truncated"])
        checks = {
            "graphExact": comparable_graph(actual) == comparable_graph(expected),
            "transactionValuesExact": actual["edges"] == expected["edges"],
            "truncationExact": actual["meta"]["truncated"] == expected["meta"]["truncated"],
        }
        for name, config in configs.items():
            old, new = score(expected, config), score(actual, config)
            # Preserve source completeness in each output. Normalize it for rule comparison only.
            old["limitations"].pop("sourceGraphComplete")
            new["limitations"].pop("sourceGraphComplete")
            checks[name + "RuleExact"] = old == new
        counts.update(key for key, passed in checks.items() if passed)
        if not all(checks.values()):
            failures.append({"targetTxId": target_id, "split": split, "failed": [k for k, v in checks.items() if not v]})
        case_hashes[target_id] = fingerprint(comparable_graph(actual))
    report = {
        "schemaVersion": "1.0", "scope": "frozen_v2_query_replay",
        "checkedAt": datetime.now().astimezone().isoformat(), "neo4jVersion": version,
        "manifestSha256": frozen["manifestSha256"], "sourceSha256": manifest["sourceSha256"],
        "ruleEngineSha256": frozen["engineSha256"], "oracleTransactions": bundle["sourceIndexTransactions"],
        "replayTransactions": bundle["transactions"], "replayAccounts": bundle["accounts"], "policy": GRAPH_POLICY,
        "queries": len(requests), "neighborQueries": len(query_keys), "neighborOrderedIdsExact": matched_queries,
        "querySeconds": round(query_seconds, 3), "splits": {name: dict(counts) for name, counts in by_split.items()},
        "caseGraphSha256": case_hashes, "failures": failures,
        "passed": not failures and matched_queries == len(query_keys),
    }
    write_json(output / "comparison.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"caseGraphSha256", "failures"}}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "compare"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=17474)
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.output)
        return 0
    return compare(args.output, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
