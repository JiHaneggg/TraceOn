"""Score the installed demo snapshots with the team's frozen, offline model."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    for name in ("source-root", "freeze", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.username is not None
            or url.password is not None or url.path or url.query or url.fragment):
        parser.error("Use the loopback demo origin")
    if args.output.exists():
        parser.error("Choose a new output filename")
    root = args.source_root.resolve()
    sys.path[:0] = [str(root / name) for name in ("rule-service", "rag-service", "data-pipeline", "ml-service")]
    from corpus import canonical, digest
    from evaluation_index import SourceIndex
    from recipient_activity import RecipientActivity
    from recipient_hybrid import ML_WEIGHT, combined_score, features
    from rules import RuleConfig, score

    opener = build_opener(ProxyHandler({}))

    def request(endpoint, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(args.base_url + endpoint, data, {"Content-Type": "application/json"})
        with opener.open(req, timeout=25) as response:
            return json.load(response)

    locked = json.loads(args.freeze.read_bytes())
    rule_freeze = root / "artifacts/rule-v2-frozen.json"
    rules_checksum = digest(rule_freeze.read_bytes())
    if rules_checksum != locked["provenance"]["v2FrozenSha256"] or locked["mlWeight"] != ML_WEIGHT:
        raise ValueError("Frozen score model does not match the rule configuration")
    rules = RuleConfig(**json.loads(rule_freeze.read_bytes())["selectedConfig"])
    model = locked["variants"]["graphWithAvailabilityAndDelta"]["model"]
    targets = request("/api/cases")["items"]
    rows = []
    with closing(SourceIndex(root / "data/evaluation/HI-Small-source.sqlite3")) as source:
        if dict(source.db.execute("SELECT key,value FROM metadata"))["sourceSha256"] != locked["provenance"]["sourceSha256"]:
            raise ValueError("Recipient activity index belongs to a different source")
        activity = RecipientActivity(source)

        def add(payload):
            graph, target = payload["graph"], payload["target"]
            previous = activity.compare(target["txId"], expected_target=target)
            value = combined_score(model, features(graph, target, previous), score(graph, rules)["ruleScore"])
            rows.append({"targetTxId": target["txId"], "asOf": graph["asOf"],
                         "graphSha256": digest(canonical(graph)), "score": value})

        for row in targets:
            result = request("/api/graph?" + urlencode({"txId": row["txId"], "hops": 2, "windowDays": 7}))
            add(result)
            if row["txId"] == "TX-000619283":
                accounts = [row for row in result["accountExpansion"]["candidates"] if row["canExpand"]]
                if len(accounts) != 1:
                    raise ValueError("The shared demo should have one expandable account")
                add(request("/api/graph/expand", {"snapshotId": result["reviewSnapshotId"],
                                                  "accountKey": accounts[0]["accountKey"]}))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"schemaVersion": "traceon-demo-hybrid-v1", "modelSha256": digest(args.freeze.read_bytes()),
                   "ruleFreezeSha256": rules_checksum, "scores": rows}, stream, indent=2)
    print(f"Verified frozen scores written for {len(rows)} graph snapshots")


if __name__ == "__main__":
    main()
