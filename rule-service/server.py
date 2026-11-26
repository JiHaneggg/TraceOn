"""Small local stateless mock/integration endpoint, no Neo4j credentials."""
import argparse
import json
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rules import RuleConfig, score


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404,
                   {"service": "traceon-rule", "status": "ok"} if self.path == "/health" else {"error": "not_found"})

    def do_POST(self):
        if self.path != "/v1/rules/score":
            return self.reply(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                return self.reply(413, {"error": "body_size"})
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
            config = body.get("config", {})
            if set(config) - {f.name for f in fields(RuleConfig)}:
                raise ValueError("Unknown configuration field")
            result = score(body["graph"], RuleConfig(**config))
            self.reply(200, result)
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            self.reply(400, {"error": str(error)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8091)
    # 기본값은 로컬 전용(127.0.0.1). 컨테이너에서는 --host 0.0.0.0으로 실행한다.
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
