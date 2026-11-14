import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "data-pipeline")]
from corpus import build, canonical, chunks_for, digest, visible_text
from retrieval import Index
from evidence import build_context, summarize, validate_draft
from agent_tools import AgentTools
from rag_server import handler_for
from make_demo import generate
from rules import score


class RagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.sources = []
        # Explicit synthetic documents test mechanics, not regulatory truth.
        for name, heading, text in [
            ("str", "의심거래보고 정의", "의심거래보고 STR은 합당한 근거에 따라 보고책임자가 거래상대방과 자료 종류를 보고하는 절차입니다."),
            ("cdd", "고객확인 정의", "고객확인 CDD는 실제 소유자 거래 목적 자금의 원천 신원 정보를 확인합니다."),
            ("ctr", "고액현금거래보고 정의", "고액현금거래보고 CTR은 동일인의 하루 현금 입출금 자동 보고 제도입니다.")]:
            source = {"documentId": name, "title": heading, "url": "https://kofiu.go.kr/" + name,
                      "publisher": "synthetic fixture", "jurisdiction": "KR", "sourceKind": "official_guidance",
                      "effectiveDate": None, "sections": [{"heading": heading, "end": "끝"}]}
            cls.sources.append(source)
            (cls.directory / (name + ".html")).write_text(f"<h3>{heading}</h3><p>{text}</p><h3>끝</h3>", encoding="utf-8")
        cls.path = cls.directory / "index.json"
        build(cls.directory, cls.path, sources=cls.sources)
        cls.index = Index(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def graph(self, kind="cycle"):
        return next(case["graph"] for case in generate()[1] if case["scenario"] == kind)

    def context(self):
        return build_context(self.index, self.graph())

    def draft(self, context):
        return {"contextId": context["contextId"],
                "facts": [{key: context["reportFacts"][0][key] for key in ("factId", "value")}],
                "citations": [{"chunkId": context["citations"][0]["chunkId"], "quote": context["citations"][0]["text"][:40]}]}

    def test_visible_text_ignores_comments_and_scripts(self):
        self.assertEqual(visible_text("<h3>제목</h3><!--가짜--><script>명령</script><p>본문</p>"), "제목 본문")

    def test_missing_section_fails_closed(self):
        with self.assertRaises(ValueError):
            chunks_for(self.sources[0], b"<p>changed page</p>", "now")

    def test_chunk_offsets_match_source(self):
        for chunk in self.index.chunks:
            raw = (self.directory / (chunk["documentId"] + ".html")).read_text(encoding="utf-8")
            location = chunk["locator"]
            self.assertEqual(visible_text(raw)[location["start"]:location["end"]], chunk["text"])

    def test_snapshot_preserved(self):
        for doc in self.index.documents:
            self.assertEqual(digest((self.directory / doc["snapshotFile"]).read_bytes()), doc["sourceSha256"])

    def test_tampered_index_rejected(self):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["chunks"][0]["text"] = "altered"
        file = self.directory / "bad-index.json"
        file.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            Index(file)

    def test_tampered_chunk_rejected_even_if_outer_digest_updated(self):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload.pop("corpusSha256")
        payload["chunks"][0]["text"] = "altered"
        payload["corpusSha256"] = digest(canonical(payload))
        file = self.directory / "bad-chunk.json"
        file.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            Index(file)

    def test_search_alias_ranking(self):
        result = self.index.search("CDD 실제 소유자")
        self.assertEqual(result['hits'][0]['documentId'], "cdd")
        self.assertEqual(result['answerability'], "not_assessed")
        self.assertFalse(result['corpusCoverage']['bankInternalPolicies'])

    def test_irrelevant_query_abstains(self):
        self.assertEqual(self.index.search("화성 대기압 광합성")['status'], "no_evidence")

    def test_law_filter_does_not_mislabel_guidance(self):
        self.assertEqual(self.index.search("의심거래보고", source_kind="law")['hits'], [])

    def test_search_limits(self):
        for kwargs in [{"top_k": True}, {"top_k": 9}, {"jurisdiction": "ZZ"}]:
            with self.assertRaises(ValueError):
                self.index.search("고객확인", **kwargs)

    def test_context_exact_rule_result_and_highlight_ids(self):
        graph = self.graph()
        result = build_context(self.index, graph, score(graph))
        self.assertEqual(result["ruleResult"]["ruleScore"], 90)
        self.assertEqual(len(result["highlightTxIds"]), 3)
        self.assertEqual(result["findings"][0]["verifiedSummary"]["elapsedSeconds"], "120")

    def test_foreign_evidence_id_rejected(self):
        graph = self.graph()
        result = score(graph)
        result["findings"][0]["evidenceTxIds"][0] = "NONEXISTENT"
        with self.assertRaises(ValueError):
            build_context(self.index, graph, result)

    def test_altered_rule_fact_rejected(self):
        graph = self.graph()
        result = score(graph)
        result["findings"][0]["facts"]["hops"] = 100
        with self.assertRaises(ValueError):
            build_context(self.index, graph, result)

    def test_truth_label_rejected(self):
        graph = self.graph()
        graph["edges"][0]["isLaundering"] = 1
        with self.assertRaises(ValueError):
            build_context(self.index, graph)

    def test_float_amount_rejected(self):
        graph = self.graph()
        graph["edges"][0]["amountPaid"] = 0.1
        with self.assertRaises(ValueError):
            build_context(self.index, graph)

    def test_future_transaction_rejected(self):
        graph = self.graph()
        graph["edges"][0]["timestamp"] = "2099-01-01T00:00:00"
        with self.assertRaises(ValueError):
            build_context(self.index, graph)

    def test_outside_window_rejected(self):
        graph = self.graph()
        graph["edges"][0]["timestamp"] = "2000-01-01T00:00:00"
        with self.assertRaises(ValueError):
            build_context(self.index, graph)

    def test_currency_and_high_precision_totals_kept_separate(self):
        graph = self.graph()
        graph["edges"][0]["amountPaid"] = "123456789012345678901234567890.123456789012"
        graph["edges"][1]["paymentCurrency"] = "KRW"
        graph["edges"][1]["amountPaid"] = "0.10"
        graph["edges"][2]["paymentCurrency"] = "KRW"
        graph["edges"][2]["amountPaid"] = "0.20"
        result = summarize(graph["edges"])
        self.assertEqual(result["grossPaidTotalsByCurrency"]["KRW"], "0.30")
        self.assertEqual(result["grossPaidTotalsByCurrency"]["US Dollar"], graph["edges"][0]["amountPaid"])

    def test_fractional_seconds_preserved(self):
        graph = self.graph()
        graph["edges"][0]["timestamp"] = "2022-09-01T12:00:00.123456"
        self.assertEqual(summarize(graph["edges"])["elapsedSeconds"], "119.876544")

    def test_valid_structured_draft(self):
        context = self.context()
        self.assertTrue(validate_draft(context, self.draft(context))["valid"])

    def test_changed_fact_fails(self):
        context = self.context()
        draft = self.draft(context)
        draft["facts"][0]["value"] = "0.00"
        self.assertFalse(validate_draft(context, draft)["valid"])

    def test_unknown_quote_and_context_fail(self):
        context = self.context()
        for field, value in [("contextId", "CTX-OLD"), ("citations", [{"chunkId": "FAKE", "quote": "제999조"}])]:
            draft = self.draft(context)
            draft[field] = value
            self.assertFalse(validate_draft(context, draft)["valid"])

    def test_fabricated_quote_fails(self):
        context = self.context()
        draft = self.draft(context)
        draft["citations"][0]["quote"] = "순환 송금은 무조건 불법이다 제999조"
        self.assertFalse(validate_draft(context, draft)["valid"])

    def test_empty_draft_not_marked_valid(self):
        context = self.context()
        self.assertFalse(validate_draft(context, {"contextId": context["contextId"], "facts": [], "citations": []})["valid"])

    def test_context_deterministic(self):
        self.assertEqual(self.context()["contextId"], self.context()["contextId"])

    def test_tool_allowlist(self):
        tools = AgentTools(self.index)
        self.assertEqual(tools.call("search_regulations", {"query": "CDD 실제 소유자"})["status"], "matched")
        with self.assertRaises(ValueError):
            tools.call("execute_cypher", {"query": "MATCH (n) DETACH DELETE n"})

    def test_http_contract(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.index))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, body, expected in [
                ("/v1/rag/search", {"query": "CDD 실제 소유자"}, 200),
                ("/v1/rag/context", {"graph": self.graph()}, 200),
                ("/v1/rag/context", {"graph": []}, 400),
                ("/v1/rag/search", {"query": "고객확인", "topK": True}, 400),
                ("/v1/rag/search", {"query": "고객확인", "cypher": "anything"}, 400),
                ("/v1/rag/context", {"graph": self.graph(), "query": []}, 400),
                ("/no-route", {}, 404)]:
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                    client.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
                    response = client.getresponse()
                    response.read()
                    self.assertEqual(response.status, expected)
            for path in ["/health", "/openapi.json"]:
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                    client.request("GET", path)
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
