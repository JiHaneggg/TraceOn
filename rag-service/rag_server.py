"""Local RAG API, standard-library only. Production Neo4j stays in Spring."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

from corpus import DEFAULT_INDEX
from evidence import build_context, validate_draft
from retrieval import Index
from reports import generate_report, ReportUnavailable
from evidence_lookup import EvidenceRepository, EvidenceError, resolve_fact
from report_verification import verify_report
from evidence_fit import assessment_response


def handler_for(index, raw_root=None, archive_dir=None):
    repository = EvidenceRepository(index, raw_root, archive_dir)
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/v1/rag/citations/"):
                try:
                    parsed = urlsplit(self.path)
                    params = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True, max_num_fields=2)
                    if set(params) - {"corpusSha256"} or any(len(values) != 1 for values in params.values()):
                        raise ValueError("Unknown/duplicate citation query parameter")
                    chunk_id = unquote(parsed.path[len("/v1/rag/citations/"):])
                    result = repository.citation(chunk_id, params.get("corpusSha256", [None])[0])
                    return self.reply(200, result)
                except EvidenceError as error:
                    return self.reply(error.status, {"error": error.code, "message": str(error)})
                except (ValueError, TypeError):
                    return self.reply(400, {"error": "invalid_citation_request"})
            if self.path == "/health":
                try:
                    current = repository.index_for()
                except EvidenceError as error:
                    return self.reply(error.status, {"error": error.code, "message": str(error)})
                return self.reply(200, {"service": "traceon-rag", "status": "ok",
                                       "documents": len(current.documents), "chunks": len(current.chunks),
                                       "retrievalPolicy": current.selection_policy,
                                       "queryScopePolicyVersion": current.query_scope_policy_version,
                                       "evidenceFitPolicySha256": current.evidence_fit_policy["policySha256"] if current.evidence_fit_policy else None,
                                       "reportModes": {"template": True, "openaiConfigured": bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"))},
                                       "corpusSha256": current.corpus_hash})
            if self.path == "/openapi.json":
                return self.reply(200, json.loads(Path(__file__).with_name("openapi.json").read_text(encoding="utf-8")))
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            if self.path not in {"/v1/rag/search", "/v1/rag/context", "/v1/rag/validate", "/v1/rag/report",
                                  "/v1/rag/facts/resolve", "/v1/rag/reports/verify", "/v1/rag/reports/prepare", "/v1/rag/evidence/assess"}:
                return self.reply(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    return self.reply(413, {"error": "body_size"})
                self.connection.settimeout(10)
                def invalid_constant(_):
                    raise ValueError("Non-finite JSON number")
                body = json.loads(self.rfile.read(length), parse_constant=invalid_constant)
                if not isinstance(body, dict):
                    raise ValueError("Body must be an object")
                if self.path == "/v1/rag/search":
                    if set(body) - {"query", "topK", "jurisdiction", "sourceKind", "legalAsOf"}:
                        raise ValueError("Unknown search field")
                    result = repository.index_for().search(body["query"], body.get("topK", 3), body.get("jurisdiction", "KR"), body.get("sourceKind"), body.get("legalAsOf"))
                elif self.path == "/v1/rag/reports/verify":
                    if set(body) - {"graph", "ruleResult", "query", "report"}:
                        raise ValueError("Unknown report verification field")
                    result = verify_report(repository, body["graph"], body.get("ruleResult"), body["report"], body.get("query"))
                else:
                    allowed = {"graph", "ruleResult", "query", "corpusSha256"} | ({"draft"} if self.path.endswith("validate") else set()) | ({"mode"} if self.path.endswith(("report", "prepare")) else set())
                    if self.path == "/v1/rag/facts/resolve":
                        allowed |= {"factId", "contextId"}
                    if set(body) - allowed:
                        raise ValueError("Unknown context field")
                    selected = (repository.index_for(body.get("corpusSha256")) if self.path == "/v1/rag/facts/resolve"
                                else repository.index_for_generation(body.get("corpusSha256")))
                    result = build_context(selected, body["graph"], body.get("ruleResult"), body.get("query"))
                    if self.path.endswith("validate"):
                        result = validate_draft(result, body["draft"])
                    elif self.path.endswith("report"):
                        result = generate_report(result, body.get("mode", "template"))
                    elif self.path == "/v1/rag/facts/resolve":
                        result = resolve_fact(result, body["factId"], body.get("contextId"))
                    elif self.path.endswith("prepare"):
                        report = generate_report(result, body.get("mode", "template"))
                        verified = verify_report(repository, body["graph"], body.get("ruleResult"), report, body.get("query"))
                        result = {"context": result, "report": report, "verification": verified}
                    elif self.path == "/v1/rag/evidence/assess":
                        result = assessment_response(result)
                self.reply(200, result)
            except EvidenceError as error:
                self.reply(error.status, {"error": error.code, "message": str(error)})
            except ReportUnavailable as error:
                self.reply(503, {"error": str(error), "reportGenerated": False})
            except (ValueError, KeyError, TypeError, ArithmeticError, OverflowError) as error:
                self.reply(400, {"error": str(error)})
    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    # 기본값은 로컬 전용(127.0.0.1). 컨테이너에서는 --host 0.0.0.0으로 실행한다.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), handler_for(Index(args.index), args.raw_dir, args.archive_dir)).serve_forever()
