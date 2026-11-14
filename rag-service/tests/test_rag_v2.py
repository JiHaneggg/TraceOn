import copy
import io
import json
import os
import http.client
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_rag as fixtures
from corpus import build, chunks_for, canonical, digest
from retrieval import Index
from evidence import build_context
from reports import generate_report, default_plan, validate_plan, ReportUnavailable
from agent_tools import AgentTools
from rag_server import handler_for


class RagV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RagTest.setUpClass()
        cls.base = fixtures.RagTest()
        cls.index, cls.directory = cls.base.index, cls.base.directory

    @classmethod
    def tearDownClass(cls):
        fixtures.RagTest.tearDownClass()

    def context(self):
        return build_context(self.index, self.base.graph())

    def provider(self, plan=None, status="completed", refusal=False):
        self.requests = []
        def send(request, timeout):
            self.requests.append((request, json.loads(request.data)))
            part = {"type": "refusal", "refusal": "no"} if refusal else {
                "type": "output_text", "text": json.dumps(plan or default_plan(self.context()))}
            return io.BytesIO(json.dumps({"id": "unit-response", "status": status,
                                         "output": [{"type": "message", "content": [part]}]}).encode())
        return send

    def test_bank_internal_scope_abstains(self):
        result = self.index.search("KB국민은행 내부 룰 점수 70점의 의심거래보고 기준")
        self.assertEqual(result["status"], "insufficient_scope")
        self.assertEqual(result["hits"], [])

    def test_legal_article_scope_abstains(self):
        self.assertEqual(self.index.search("특정금융정보법 제4조 조문")['status'], "insufficient_scope")

    def test_unknown_score_policy_abstains(self):
        self.assertEqual(self.index.search("룰 점수 90점이면 자금세탁으로 확정 보고해야 하나")['status'], "insufficient_scope")

    def test_historical_law_scope_abstains(self):
        self.assertEqual(self.index.search("STR", legal_as_of="2022-09-01")['status'], "insufficient_scope")

    def test_invalid_legal_date_rejected(self):
        for value in ("2026-02-30", "2026-1-1", 2026):
            with self.assertRaises(ValueError):
                self.index.search("STR", legal_as_of=value)

    def test_internal_reporting_procedure_not_blocked(self):
        self.assertEqual(self.index.search("STR 내부 보고 절차 보고책임자")['status'], "matched")

    def test_maintenance_page_rejected(self):
        with self.assertRaises(ValueError):
            chunks_for(self.base.sources[0], "<p>시스템 점검</p>".encode(), "now")

    def test_source_version_marker_required(self):
        source = {**self.base.sources[0], "requiredText": ["시행 2026. 10. 2."]}
        with self.assertRaises(ValueError):
            chunks_for(source, (self.directory / 'str.html').read_bytes(), "now")
        source = {**self.base.sources[0], 'validUntil':'2027-01-01'}
        rows = chunks_for(source, (self.directory / 'str.html').read_bytes(), "now")
        self.assertEqual(rows[0]['validUntil'],'2027-01-01')

    def test_last_occurrence_excludes_toc(self):
        source = copy.deepcopy(self.base.sources[0])
        source['sections'] = [{"heading": "제목", "end": "끝", "occurrence": "last"}]
        rows = chunks_for(source, "<nav>제목 끝</nav><h3>제목</h3><p>실제 본문</p><h3>끝</h3>".encode(), "now")
        self.assertEqual(rows[0]['text'], "제목 실제 본문 ")

    def test_failed_build_preserves_index(self):
        before = self.base.path.read_bytes()
        source = {**self.base.sources[0], 'url': 'https://unapproved.example/document'}
        with self.assertRaises(ValueError):
            build(self.directory, self.base.path, sources=[source])
        self.assertEqual(self.base.path.read_bytes(), before)

    def test_foreign_filter_does_not_return_korean_guidance(self):
        self.assertEqual(self.index.search("wire transfers", jurisdiction="US", source_kind="foreign_typology")['hits'], [])

    def test_legal_validity_interval_filters_individual_rows(self):
        payload = json.loads(self.base.path.read_text(encoding='utf-8'))
        payload.pop('corpusSha256')
        rows = []
        for suffix, start, end in [('past','2020-01-01','2021-01-01'),('future','2025-01-01','2027-01-01')]:
            row = copy.deepcopy(payload['chunks'][0])
            row.update({'chunkId':suffix,'sourceKind':'law','effectiveDate':start,'validUntil':end})
            rows.append(row)
        payload['chunks'] = rows
        payload['corpusSha256'] = digest(canonical(payload))
        path = self.directory / 'law-fixture.json'
        path.write_text(json.dumps(payload),encoding='utf-8')
        result = Index(path).search("의심거래보고", legal_as_of="2020-06-01")
        self.assertEqual([row['chunkId'] for row in result['hits']], ['past'])

    def test_template_report_never_calls_llm(self):
        context = self.context()
        with patch.dict(os.environ, {'OPENAI_API_KEY':'unit-token','OPENAI_MODEL':'unit-model'}):
            report = generate_report(context, transport=lambda *a, **k: self.fail('Unexpected network call'))
        self.assertFalse(report['generation']['llmCalled'])
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(report['status'], 'human_review_required')
        self.assertIn(context['targetTransaction']['amountPaid'], report['markdown'])

    def test_plan_cannot_invent_statement_or_number(self):
        context = self.context();plan = default_plan(context)
        plan['observationIds'].append('illegal-999999')
        with self.assertRaises(ValueError):validate_plan(context, plan)

    def test_plan_cannot_add_free_prose(self):
        context = self.context();plan = default_plan(context);plan['conclusion'] = 'confirmed crime'
        with self.assertRaises(ValueError):validate_plan(context, plan)

    def test_plan_cannot_omit_required_observations(self):
        context = self.context();plan = default_plan(context);plan['observationIds'].pop()
        with self.assertRaises(ValueError):validate_plan(context, plan)

    def test_human_review_cannot_be_removed(self):
        context = self.context();plan = default_plan(context);plan['reviewItemIds'] = ['purpose']
        with self.assertRaises(ValueError):validate_plan(context, plan)

    def test_openai_missing_key_does_not_fallback(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            with self.assertRaises(ReportUnavailable):generate_report(self.context(), 'openai')

    def test_openai_adapter_payload_and_output(self):
        context = self.context()
        with patch.dict(os.environ, {'OPENAI_API_KEY':'unit-token','OPENAI_MODEL':'unit-model'}):
            report = generate_report(context, 'openai', self.provider(default_plan(context)))
        request, body = self.requests[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
        self.assertFalse(body['store']);self.assertTrue(body['text']['format']['strict'])
        self.assertEqual(body['model'], 'unit-model')
        self.assertTrue(report['generation']['llmCalled'])
        self.assertNotIn('unit-token', json.dumps(report))

    def test_incomplete_provider_response_fails(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'unit-token','OPENAI_MODEL':'unit-model'}):
            with self.assertRaises(ReportUnavailable):
                generate_report(self.context(), 'openai', self.provider(status='incomplete'))

    def test_provider_refusal_fails(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'unit-token','OPENAI_MODEL':'unit-model'}):
            with self.assertRaises(ReportUnavailable):
                generate_report(self.context(), 'openai', self.provider(refusal=True))

    def test_provider_unknown_id_fails(self):
        context = self.context();plan = default_plan(context);plan['citationIds'].append('FAKE')
        with patch.dict(os.environ, {'OPENAI_API_KEY':'unit-token','OPENAI_MODEL':'unit-model'}):
            with self.assertRaises(ReportUnavailable):generate_report(context, 'openai', self.provider(plan))

    def test_no_citations_not_marked_fully_valid(self):
        context = self.context();context['citations'] = []
        report = generate_report(context)
        self.assertFalse(report['validation']['valid'])
        self.assertTrue(report['humanReviewRequired'])

    def test_report_does_not_mutate_context(self):
        context = self.context();before = canonical(context)
        generate_report(context)
        self.assertEqual(canonical(context), before)

    def test_agent_report_tool(self):
        report = AgentTools(self.index).call('generate_review_report', {'graph':self.base.graph()})
        self.assertFalse(report['generation']['llmCalled'])

    def test_report_http_contract_and_missing_provider(self):
        server = ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.index))
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict(os.environ, {'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
                for mode, expected in [('template',200),('openai',503),('invalid',400)]:
                    with closing(http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)) as client:
                        client.request('POST','/v1/rag/report',json.dumps({'graph':self.base.graph(),'mode':mode}),{'Content-Type':'application/json'})
                        response = client.getresponse();payload = json.loads(response.read())
                        self.assertEqual(response.status,expected)
                        if expected == 503:self.assertFalse(payload['reportGenerated'])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':unittest.main()
