"""Exercise the shared TraceOn demo against the reference service or a local team API."""
import argparse
from contextlib import contextmanager, nullcontext
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ("graph-viewer", "data-pipeline")]
import server as viewer
from graph_contract import MemoryGraph, read_transactions
from graph_diagnostics import fingerprint, uncapped_reference
from history_store import HistoryStore
from review_bridge import ReviewBridge, graph_hash

TARGET = "TX-000619283"
CSV = ROOT / "data/processed/HI-Small-MVP/transactions.csv"
DEMO_SHA256 = "d88ae9888527d71134502602d1f3c9ec41fe0ef65323c4829425993f81653f34"
STAGES = ("graph", "expand", "review", "archive")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def client(base_url):
    origin = urlsplit(base_url)
    if (origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost"}
            or origin.username is not None or origin.password is not None or not origin.port
            or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("Use a local HTTP origin with a port")
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(path, *, params=None, body=None, expected=200):
        url = base_url.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = Request(url, data=data, headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with opener.open(request, timeout=45) as response:
                status, raw = response.status, response.read(9_000_001)
        except HTTPError as error:
            status, raw = error.code, error.read(9_000_001)
        require(status == expected, f"{path} returned HTTP {status}; expected HTTP {expected}")
        if expected >= 400:
            return None
        require(len(raw) <= 9_000_000, f"{path} returned too much data")
        return json.loads(raw)

    return call


def unavailable(*args, **kwargs):
    raise AssertionError("Archived evidence must be served from saved data")


class OfflineRag:
    call = staticmethod(unavailable)


@contextmanager
def reference_service(store, offline=False):
    bridge = ReviewBridge(OfflineRag()) if offline else ReviewBridge()
    database = unavailable if offline else viewer.ReadOnlyDatabase
    handler = viewer.handler_for([TARGET], database, review_bridge=bridge, history_store=store)

    class QuietHandler(handler):
        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield client(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            thread.join(5)


def check_graph(payload, expected, limit):
    graph = payload["graph"]
    for key in ("targetTxId", "asOf", "windowStart"):
        require(graph[key] == expected[key], f"Graph {key} differs from the demo source")
    require(graph["meta"]["hops"] == 2, "Expected a 2-hop view")
    require(graph["meta"]["truncated"] == expected["meta"]["truncated"], "Unexpected truncation flag")
    actual_edges = {row["txId"]: row for row in graph["edges"]}
    require(len(graph["edges"]) == len(actual_edges) == limit, f"Expected {limit} unique transactions")
    require(actual_edges == {row["txId"]: row for row in expected["edges"]}, "Original transaction values differ")
    accounts = {node["accountKey"] for node in graph["nodes"]}
    require(len(accounts) == len(graph["nodes"]) and accounts == {node["accountKey"] for node in expected["nodes"]},
            "Account list differs from the original graph")
    expected_rule = viewer.score(expected)
    rule = payload["ruleResult"]
    for key in ("ruleScore", "flagged", "findings"):
        require(rule[key] == expected_rule[key], f"Rule output {key} differs from the graph evidence")
    require(payload["target"] == actual_edges[TARGET], "Target transaction differs from the graph")
    require(payload["source"]["sourceGraphComplete"] is False, "Partial demo scope was lost")
    return actual_edges


def prepare(call, payload):
    index = next((i for i, item in enumerate(payload["ruleResult"]["findings"]) if item["ruleId"] == "CYCLE"), None)
    require(index is not None, "Expected a cycle finding in the selected demo")
    inputs = {"snapshotId": payload["reviewSnapshotId"], "findingIndex": index}
    review = call("/api/review/prepare", body=inputs)
    report = review["report"]
    require(review["snapshotId"] == inputs["snapshotId"] and review["focus"]["ruleId"] == "CYCLE",
            "Review is bound to another graph or finding")
    require(report["graphSha256"] == graph_hash(payload["graph"]), "Review graph digest mismatch")
    require(review["verification"]["valid"] is True and review["verification"]["readyForSubmission"] is False,
            "Expected a verified draft for human review")
    require(report["generation"]["llmCalled"] is False, "Use the deterministic demo template")
    return review, inputs


def live_evidence(call, review, rows):
    facts = {"target.amountPaid": rows[TARGET]["amountPaid"], "target.timestamp": rows[TARGET]["timestamp"]}
    for id_, value in facts.items():
        detail = call("/api/review/fact", body={"reviewId": review["reviewId"], "factId": id_})
        require(detail["fact"]["value"] == value and detail["transactions"] == [rows[TARGET]],
                f"{id_} does not match the source transaction")
    citations = {}
    for row in review["report"]["citations"]:
        chunk = row["chunkId"]
        result = call("/api/review/citation", params={"reviewId": review["reviewId"], "chunkId": chunk})
        section = result["section"]
        start, end = section["highlight"]["start"], section["highlight"]["end"]
        require(result["sourceVerification"]["status"] == "snapshot_checked" and section["text"][start:end] == row["text"],
                "Citation does not match its archived source passage")
        citations[chunk] = result
    require(bool(citations), "Expected citations in the demo report")
    return facts, citations


def run(call, stage, expected, expanded_reference):
    initial = call("/api/graph", params={"txId": TARGET, "hops": 2, "windowDays": 7})
    check_graph(initial, expected, 69)
    result = {"target": TARGET, "initialTransactions": 69}
    if stage == "graph":
        return result, None

    old = prepare(call, initial) if stage in {"review", "archive"} else None
    candidates = [row for row in initial["accountExpansion"]["candidates"]
                  if row["canExpand"] and row["reason"] == "more_history"]
    require(len(candidates) == 1, "The selected cycle demo should have one omitted account page")
    page_request = {"snapshotId": initial["reviewSnapshotId"], "accountKey": candidates[0]["accountKey"]}
    expanded = call("/api/graph/expand", body=page_request)
    rows = check_graph(expanded, expanded_reference, 72)
    page = expanded["accountExpansion"]["lastPage"]
    require((page["readRows"], page["addedTransactions"], page["duplicateTransactions"]) == (4, 3, 1),
            "Expansion must add three unique rows, with one duplicate")
    require(set(page["addedTxIds"]) == set(rows) - {row["txId"] for row in expected["edges"]},
            "New edge IDs differ from the original source")
    require(call("/api/graph/expand", body=page_request) == expanded, "Retry changed the graph or page budget")
    result.update(expandedTransactions=72, duplicateTransactions=1, expansionReplay=True)
    if stage == "expand":
        return result, None

    old_review, old_inputs = old
    call("/api/review/prepare", body=old_inputs, expected=409)
    call("/api/review/verify", body={"reviewId": old_review["reviewId"]}, expected=409)
    call("/api/review/fact", body={"reviewId": old_review["reviewId"], "factId": "target.amountPaid"}, expected=409)
    call("/api/review/download", params={"reviewId": old_review["reviewId"], "format": "json"}, expected=409)
    review, _ = prepare(call, expanded)
    facts, citations = live_evidence(call, review, rows)
    result.update(staleReviewOperationsRejected=4, factsChecked=len(facts), citationsChecked=len(citations))
    if stage == "review":
        return result, None

    save_request = {"snapshotId": expanded["reviewSnapshotId"], "reviewId": review["reviewId"]}
    saved = call("/api/history/save", body=save_request)["history"]
    repeated = call("/api/history/save", body=save_request)
    require(repeated["created"] is False and repeated["history"] == saved, "Repeated save created another history entry")
    archive = (saved, expanded, review, facts, citations, rows)
    check_archive(call, archive)
    result.update(archiveRestored=True, saveReplay=True)
    return result, archive


def check_archive(call, archive):
    saved, expanded, review, facts, citations, rows = archive
    params = {"historyId": saved["historyId"]}
    opened = call("/api/history/open", params=params)
    history = opened["history"]
    require(history == saved and history["integrity"]["currentSourcesChecked"] is False,
            "Archived data was replaced or displayed as live")
    require(opened["payload"]["graph"] == expanded["graph"] and opened["payload"]["ruleResult"] == expanded["ruleResult"],
            "Restored graph or findings differ")
    require(opened["review"]["context"] == review["context"] and opened["review"]["report"] == review["report"],
            "Restored draft or context differs")
    for fact, value in facts.items():
        resolved = call("/api/history/fact", params={**params, "factId": fact})
        require(resolved["fact"]["value"] == value and resolved["transactions"] == [rows[TARGET]],
                "Saved fact differs from the source transaction")
    for chunk, live in citations.items():
        resolved = call("/api/history/citation", params={**params, "chunkId": chunk})
        require(all(resolved[key] == live[key] for key in ("citation", "section", "sourceVerification")),
                "Saved citation differs from the originally verified source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=STAGES, default="archive")
    parser.add_argument("--base-url", help="Local test API origin; archive stage writes a demo history entry there")
    args = parser.parse_args()
    source = MemoryGraph(read_transactions(CSV))
    source_hash = fingerprint(source.transactions.values())
    require(source_hash == DEMO_SHA256, "The demo CSV belongs to a different source version")
    expected = source.subgraph(TARGET, hops=2, window_days=7)
    expanded = uncapped_reference(expected, source.transactions)
    with TemporaryDirectory() as temp:
        store = HistoryStore(Path(temp) / "team-integration.sqlite3")
        connection = nullcontext(client(args.base_url)) if args.base_url else reference_service(store)
        with connection as call:
            result, archive = run(call, args.stage, expected, expanded)
        if archive and not args.base_url:
            with reference_service(store, offline=True) as call:
                check_archive(call, archive)
            result["offlineRestartRestored"] = True
    print(json.dumps({"passed": True, "stage": args.stage, "mode": "team_api" if args.base_url else "local_reference",
                      "sourceSha256": source_hash, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
