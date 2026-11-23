"""Offline, reproducible workflow verification; every document is synthetic."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from agent_tools import AgentTools
from agent_workflow import run_review_workflow
from corpus import build, canonical, digest
from retrieval import Index

FIXTURES = Path(__file__).with_name("examples") / "agent-workflow"
CAPTURE_TIME = 1704067200  # 2024-01-01 UTC, independent of checkout mtimes.


def fixture_tools(directory, request_file=FIXTURES / "request.json"):
    directory = Path(directory)
    raw = directory / "raw"
    raw.mkdir(parents=True)
    sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    for source in sources:
        name = source["documentId"] + ".html"
        path = raw / name
        # Normalize Git CRLF/LF checkouts before hashing the fixed documents.
        path.write_bytes((FIXTURES / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
        os.utime(path, (CAPTURE_TIME, CAPTURE_TIME))
    index_path = directory / "index.json"
    build(raw, index_path, sources=sources)
    request = json.loads(request_file.read_text(encoding="utf-8"))
    return AgentTools(Index(index_path), raw_root=raw), request


def run_cases(directory):
    tools, request = fixture_tools(directory)
    results = {}
    for case in ("normal", "no_evidence", "wrong_amount", "wrong_citation"):
        arguments = deepcopy(request)
        if case == "no_evidence":
            arguments["query"] = "화성 대기압 광합성"

        def call(name, fields):
            response = tools.call(name, fields)
            if name == "generate_review_report" and case in {"wrong_amount", "wrong_citation"}:
                response = deepcopy(response)
                if case == "wrong_amount":
                    fact = next(row for row in response["draft"]["facts"] if row["factId"] == "target.amountPaid")
                    fact["value"] = "999999.99"
                else:
                    response["draft"]["citations"][0]["quote"] = "원문에 존재하지 않는 합성 인용입니다."
                # Rehash the bad draft, proving a self-consistent digest is
                # insufficient to bypass source/fact/rendering verification.
                response["reportId"] = "RPT-" + digest(canonical({
                    k: v for k, v in response.items() if k != "reportId"}))[:24]
            return response

        results[case] = run_review_workflow(call, arguments["graph"], arguments["ruleResult"],
                                           arguments.get("query"), mode="template")
    expected = {"normal": "passed", "no_evidence": "held",
                "wrong_amount": "rejected", "wrong_citation": "rejected"}
    checks = {case: result["status"] == expected[case] for case, result in results.items()}
    normal = results["normal"]
    checks["normal_all_checks"] = all(row["valid"] for row in normal["verification"]["checks"].values())
    checks["no_evidence_no_draft"] = results["no_evidence"]["report"] is None
    checks["supplementary_evidence_does_not_answer"] = bool(results["no_evidence"]["evidence"]["citations"])
    checks["amount_check_rejects"] = not results["wrong_amount"]["verification"]["checks"]["amounts"]["valid"]
    checks["citation_check_rejects"] = not results["wrong_citation"]["verification"]["checks"]["citations"]["valid"]
    checks["same_query_for_altered_drafts"] = all(
        results[case]["identity"] == normal["identity"] for case in ("wrong_amount", "wrong_citation"))
    checks["always_human_review"] = all(r["humanReviewRequired"] and not r["readyForSubmission"] for r in results.values())
    checks["no_model_calls"] = all(r["report"] is None or not r["report"]["generation"]["llmCalled"] for r in results.values())
    fixture_hashes = {path.name: digest(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
                      for path in sorted(FIXTURES.iterdir()) if path.is_file()}
    return {"schemaVersion": "1.0", "kind": "synthetic_workflow_mechanics",
            "fixtureSha256": fixture_hashes, "valid": all(checks.values()),
            "checks": checks, "cases": results, "actualModelCalls": 0, "databaseCalls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; use a new path to preserve verification history")
    with tempfile.TemporaryDirectory() as temporary:
        outcome = run_cases(temporary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(outcome, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"valid": outcome["valid"], "checks": outcome["checks"],
                      "output": str(args.output.resolve())}, ensure_ascii=False))
    raise SystemExit(0 if outcome["valid"] else 1)
