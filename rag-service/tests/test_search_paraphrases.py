"""Synthetic vocabulary, ranking and citation checks, independent of V4 gold.

These fixtures test mechanics; their contents are not regulatory advice.
"""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / 'data-pipeline')]
from corpus import build, canonical, digest
from retrieval import Index
from agent_tools import AgentTools
from evidence import build_context, validate_draft
from evidence_lookup import EvidenceRepository
from reports import generate_report
from report_verification import verify_report
from rag_server import handler_for
from make_demo import generate


class SearchParaphrasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.raw = cls.root / 'raw'
        cls.raw.mkdir()
        sources = []
        def add(name, title, heading, text, kind='official_guidance', jurisdiction='KR'):
            sources.append({'documentId': name, 'title': title, 'publisher': 'synthetic fixture',
                            'url': 'https://www.kofiu.go.kr/' + name, 'jurisdiction': jurisdiction,
                            'sourceKind': kind, 'effectiveDate': None,
                            'sections': [{'heading': heading, 'end': '끝'}]})
            (cls.raw / (name + '.html')).write_text('<h3>' + heading + '</h3><p>' + text + '</p><h3>끝</h3>', encoding='utf-8')
        add('destination', '의심거래보고 안내', '보고 대상 기관',
            '불법재산이 의심되는 합당한 근거가 있으면 금융정보분석원장에게 보고합니다. 의심거래보고 제도입니다.')
        add('procedure', '의심거래보고 안내', '직원 보고 절차',
            '영업점 직원은 고객의 평소 거래상황, 직업, 사업내용을 고려합니다. 의심거래라면 보고책임자에게 보고합니다. '
            '보고서에는 의심스러운 합당한 근거와 보존하는 자료의 종류를 기재합니다. '
            '긴급한 경우 우선 전화나 FAX로 보고하고 추후 보완합니다.')
        add('distraction', '기관 업무 개요', '고객확인 개념',
            '금융회사 기관은 재산 거래를 취급한 담당자에게 금융정보분석원장 자료를 설명합니다. '
            '최종적으로 알리는 기관의 명칭과 금융회사가 확인한 재산 거래 기관 자료. '
            '고객확인 CDD는 실제 소유자 거래 목적 자금의 원천을 확인합니다.')
        add('wire', 'Financial indicators', 'Wire transfers',
            'Large and frequent wire transfers between senders and receivers with no apparent relationship. '
            'Multiple clients send wire transfers overseas. Further assessment is required.', 'foreign_typology', 'CA')
        add('activity', 'Financial indicators', 'Change in account activity',
            'An inactive account begins to see financial activity such as deposits, wire transfers and withdrawals. '
            'Changes in account activity may require further assessment.', 'foreign_typology', 'CA')
        cls.path = cls.root / 'index.json'
        build(cls.raw, cls.path, sources=sources)
        payload = json.loads(cls.path.read_bytes())
        payload.pop('corpusSha256')
        payload['queryScopePolicyVersion'] = '1.0'
        payload['corpusSha256'] = digest(canonical(payload))
        cls.path.write_text(json.dumps(payload), encoding='utf-8')
        cls.index = Index(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def top(self, query, document, **kwargs):
        result = self.index.search(query, **kwargs)
        self.assertEqual(result['status'], 'matched', query)
        self.assertEqual(result['hits'][0]['documentId'], document, query)
        return result

    def test_recipient_relation_beats_institution_mentions(self):
        result = self.top('수상한 재산 거래를 최종 보고하는 기관은 어디인가요', 'destination')
        self.assertEqual(result['ranking']['intent'], 'report_recipient')
        self.assertEqual(result['ranking']['prioritizedChunkIds'], [result['hits'][0]['chunkId']])

    def test_mentioning_reporting_institution_is_not_destination_question(self):
        result = self.top('의심스러운 거래보고서에 보고기관과 거래상대방 외에 무엇을 기재하나요', 'procedure')
        self.assertNotEqual(result['ranking']['intent'], 'report_recipient')

    def test_customer_history_and_occupation_paraphrases(self):
        for query in ['창구 직원은 수상한 거래 판단에 손님의 생업과 기존 이용 모습을 참고하나요',
                      '영업점 담당자가 이상한 거래를 살필 때 평소 거래 이력과 사업 활동을 보는가']:
            with self.subTest(query=query): self.top(query, 'procedure')

    def test_internal_reporting_role_is_not_private_manual(self):
        result = self.top('수상한 자금 흐름을 찾은 지점 직원이 어느 책임자에게 전달하나요', 'procedure')
        self.assertEqual(result['scopeAssessment']['decision'], 'scope_supported')

    def test_urgent_telephone_and_fax_paraphrases(self):
        for query in ['긴급한 보고를 유선으로 먼저 하고 서류를 나중에 보완할 수 있나요',
                      '급한 보고는 팩스로 먼저 알리고 추후 정리해도 되나요']:
            with self.subTest(query=query): self.top(query, 'procedure')

    def test_report_reason_and_preserved_records_together(self):
        result = self.top('의심 보고에 사유를 적고 보관 중인 증빙 자료 종류를 제출하나요', 'procedure')
        self.assertIn('보존하는 자료의 종류', result['hits'][0]['text'])

    def test_specific_grounds_rank_above_general_overview(self):
        result = self.index.search('의심 보고에 구체적인 이유를 명시해야 하나요')
        self.assertIn('procedure', [r['documentId'] for r in result['hits']])

    def test_unrelated_wire_parties_bridge_korean_to_english(self):
        for query in ['서로 모르는 사람들이 반복 송금하는 해외 징후',
                      '송금 당사자 사이 관계가 드러나지 않는 해외 사례']:
            with self.subTest(query=query):
                result = self.top(query, 'wire', jurisdiction='ALL', source_kind='foreign_typology')
                self.assertIn('no apparent relationship', result['hits'][0]['text'])

    def test_inactive_account_reactivation_bridge(self):
        for query in ['쓰이지 않던 계좌가 다시 송금하는 해외 사례',
                      '잠자던 계좌에서 다시 입금 활동이 시작되는 사례']:
            with self.subTest(query=query):
                self.top(query, 'activity', jurisdiction='ALL', source_kind='foreign_typology')

    def test_non_financial_single_words_do_not_trigger_retrieval(self):
        for query in ['팩스 기기 수리 견적', '게임 계정 비활성화 오류', '생업 관련 소설 작가 인터뷰']:
            with self.subTest(query=query):
                result = self.index.search(query)
                self.assertFalse(result['hits'])
                self.assertNotIn('queryExpansion', result)

    def test_canonical_report_queries_keep_legacy_search_shape(self):
        # Existing contexts hash the full retrieval response, including metadata.
        # New diagnostics should not invalidate already canonical report queries.
        for query in ['의심거래보고 합당한 근거 보고 방법 절차',
                      '법률 제4조 불법재산 의심거래 보고 합당한 근거',
                      '고객확인 실제 소유자 거래 목적 자금의 원천']:
            with self.subTest(query=query):
                result = self.index.search(query)
                self.assertNotIn('queryExpansion', result)
                self.assertEqual(result['ranking']['notice'], 'Eligible concept sections are prioritized; retrieval does not establish semantic entailment.')

    def test_synonyms_do_not_bypass_unsupported_scope(self):
        for query in ['수상한 송금을 유선으로 먼저 알리는 국민은행 내부 지침의 실제 기한',
                      'STR 보고 사유와 FATF 권고 제10호 원문을 함께 찾아줘',
                      '이상한 거래의 AC-7799의 생업과 실제 소유자 이름을 알려줘',
                      '수상한 거래의 EU 규정 원문을 직접 인용해줘',
                      '의심 거래 룰 점수로 자동 보고 의무를 확정해줘']:
            with self.subTest(query=query):
                result = self.index.search(query)
                self.assertEqual(result['status'], 'insufficient_scope')
                self.assertFalse(result['hits'])
                self.assertEqual(result['scopeAssessment']['decision'], 'abstain')

    def test_source_jurisdiction_and_date_filters_remain_mandatory(self):
        query = '쓰이지 않던 계좌에서 다시 송금하는 해외 징후'
        for options in [{}, {'jurisdiction':'US', 'source_kind':'foreign_typology'},
                        {'jurisdiction':'ALL', 'source_kind':'law'},
                        {'jurisdiction':'ALL', 'legal_as_of':'2020-01-01'}]:
            with self.subTest(options=options): self.assertFalse(self.index.search(query, **options)['hits'])

    def test_source_bytes_ids_and_exact_quotes_survive_expansion(self):
        before = canonical(self.index.chunks)
        for query, options in [('의심 보고 자료에 보관한 증빙 종류도 기재하나요', {}),
                               ('서로 모르는 사람 간 반복 송금', {'jurisdiction':'ALL','source_kind':'foreign_typology'})]:
            result = self.index.search(query, **options)
            self.assertTrue(result['hits'])
            for hit in result['hits']:
                for row in [hit, *hit['supportingChunks']]:
                    original = self.index.by_id[row['chunkId']]
                    self.assertEqual(row['text'], original['text'])
                    self.assertEqual(row['textSha256'], digest(original['text']))
                    self.assertEqual(row['locator'], original['locator'])
                    self.assertTrue(self.index.verify_citation(row['chunkId'], row['text'][:80]))
                    self.assertFalse(self.index.verify_citation(row['chunkId'], '존재하지 않는 인용문'))
        self.assertEqual(canonical(self.index.chunks), before)

    def test_report_verifies_and_rejects_fabricated_quote_after_expansion(self):
        graph = next(c['graph'] for c in generate()[1] if c['scenario'] == 'cycle')
        query = '긴급 보고를 유선으로 먼저 알리고 서류는 나중에 보완하나요'
        context = build_context(self.index, graph, query=query)
        report = generate_report(context)
        repository = EvidenceRepository(self.index, self.raw)
        self.assertTrue(verify_report(repository, graph, None, report, query)['valid'])
        tampered = copy.deepcopy(report['draft'])
        tampered['citations'][0]['quote'] = '이 문장은 원문에 존재하지 않습니다.'
        self.assertFalse(validate_draft(context, tampered)['valid'])
        self.assertFalse(report['generation']['llmCalled'])

    def test_http_and_agent_return_same_ranking_and_abstention(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.index, self.raw))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tools = AgentTools(self.index, self.raw)
            for query in ['수상한 재산 거래를 최종 보고하는 기관은 어디인가요',
                          '긴급한 보고를 팩스로 먼저 보내고 추후 보완하나요',
                          '국민은행 내부 지침의 의심 보고 처리 기한을 알려줘']:
                body = {'query': query}
                direct = self.index.search(query)
                agent = tools.call('search_regulations', body)
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    connection.request('POST', '/v1/rag/search', json.dumps(body), {'Content-Type':'application/json'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), direct)
                    self.assertEqual(agent, direct)
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
