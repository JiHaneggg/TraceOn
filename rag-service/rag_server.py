"""Local RAG API, standard-library only. Production Neo4j stays in Spring."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from corpus import DEFAULT_INDEX
from evidence import build_context, validate_draft
from retrieval import Index
from reports import generate_report, ReportUnavailable


def handler_for(index):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"service": "traceon-rag", "status": "ok",
                                       "documents": len(index.documents), "chunks": len(index.chunks),
                                       "retrievalPolicy": index.selection_policy,
                                       "reportModes": {"template": True, "openaiConfigured": bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"))},
                                       "corpusSha256": index.corpus_hash})
            if self.path == "/openapi.json":
                return self.reply(200, json.loads(Path(__file__).with_name("openapi.json").read_text(encoding="utf-8")))
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            if self.path not in {"/v1/rag/search", "/v1/rag/context", "/v1/rag/validate", "/v1/rag/report"}:
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
                    result = index.search(body["query"], body.get("topK", 3), body.get("jurisdiction", "KR"), body.get("sourceKind"), body.get("legalAsOf"))
                else:
                    allowed = {"graph", "ruleResult", "query"} | ({"draft"} if self.path.endswith("validate") else set()) | ({"mode"} if self.path.endswith("report") else set())
                    if set(body) - allowed:
                        raise ValueError("Unknown context field")
                    result = build_context(index, body["graph"], body.get("ruleResult"), body.get("query"))
                    if self.path.endswith("validate"):
                        result = validate_draft(result, body["draft"])
                    elif self.path.endswith("report"):
                        result = generate_report(result, body.get("mode", "template"))
                self.reply(200, result)
            except ReportUnavailable as error:
                self.reply(503, {"error": str(error), "reportGenerated": False})
            except (ValueError, KeyError, TypeError, ArithmeticError, OverflowError) as error:
                self.reply(400, {"error": str(error)})
    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(Index(args.index))).serve_forever()
