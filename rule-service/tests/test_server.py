import http.client
import json
import sys
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "rule-service"), str(ROOT / "data-pipeline")]
from make_demo import generate
from server import Handler


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def send(self, path, body):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())

    def test_score_contract(self):
        _, cases = generate()
        status, result = self.send("/v1/rules/score", {"graph": cases[0]["graph"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["ruleScore"], 90)
        self.assertTrue(result["findings"][0]["evidenceTxIds"])

    def test_bad_body_is_client_error(self):
        for body in [[], {"graph": []}, {"graph": {"isLaundering": 1}}]:
            self.assertEqual(self.send("/v1/rules/score", body)[0], 400)

    def test_unknown_route(self):
        self.assertEqual(self.send("/not-found", {})[0], 404)

    def test_per_rule_configuration_contract(self):
        _, cases = generate()
        graph = next(c["graph"] for c in cases if c["scenario"] == "fan_in")
        status, result = self.send("/v1/rules/score", {"graph": graph,
            "config": {"fan_in_min_counterparties": 4, "fan_out_min_counterparties": 3}})
        self.assertEqual(status, 200)
        self.assertFalse(result["flagged"])


if __name__ == "__main__":
    unittest.main()
