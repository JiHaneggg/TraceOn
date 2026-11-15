"""Bounded scope handling with synthetic documents and general-question controls."""
import copy
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT.parent/'data-pipeline')]
import test_domestic_sources as fixtures
from corpus import canonical,digest
from corpus_manager import CorpusManager
from retrieval import Index
from query_scope import assess_scope
from evidence import build_context
from evidence_lookup import EvidenceRepository
from evidence_lookup import EvidenceError
from reports import generate_report
from report_verification import verify_report
from agent_tools import AgentTools
from rag_server import handler_for


class QueryScopeTest(unittest.TestCase):
    def setUp(self):
        fixtures.DomesticSourcesTest.setUp(self)
        self.old=self.index
        self.manager=CorpusManager(self.path)
        self.manager.init(self.sources)
        self.manager.set_query_scope_policy('1.0',self.index.corpus_hash)
        self.index=Index(self.path)

    def graph(self):return fixtures.DomesticSourcesTest.graph(self)

    def abstains(self,query,code=None,**kwargs):
        result=self.index.search(query,**kwargs)
        self.assertEqual(result['status'],'insufficient_scope',query)
        self.assertEqual(result['answerability'],'insufficient_scope')
        self.assertEqual(result['hits'],[])
        self.assertEqual(result['scopeAssessment']['decision'],'abstain')
        self.assertTrue(result['abstentionReason'])
        if code:self.assertIn(code,result['scopeAssessment']['reasonCodes'])
        return result

    def allows(self,query,**kwargs):
        result=self.index.search(query,**kwargs)
        self.assertEqual(result['scopeAssessment']['decision'],'scope_supported',query)
        self.assertNotEqual(result['status'],'insufficient_scope',query)
        return result

    def test_bank_manual_deadlines_and_mixed_requests_abstain(self):
        for query in ['KB 내부 지침의 의심거래 처리 기한을 알려줘',
                      'STR 개념 설명과 국민은행 내부 승인 담당자 이름을 함께 알려줘',
                      '국민은행의 비공개 매뉴얼을 그대로 인용해줘']:
            with self.subTest(query=query):self.abstains(query,'internal_policy_unavailable')

    def test_generic_internal_reporting_and_public_obligations_are_allowed(self):
        for query in ['STR 내부 보고 절차와 보고책임자',
                      '금융회사가 내부 업무지침을 작성해야 한다는 법률 근거']:
            with self.subTest(query=query):self.allows(query)

    def test_fatf_primary_source_is_not_substituted_by_other_material(self):
        self.abstains('FATF 권고 제10호 원문 고객확인 조항을 찾아줘','requested_publisher_unavailable')
        self.allows('FATF를 언급한 국내 안내를 찾아줘')

    def test_registered_fatf_reference_can_satisfy_publisher_but_not_another_number(self):
        source={'documentId':'synthetic-fatf','title':'FATF Recommendations synthetic fixture',
                'publisher':'FATF synthetic fixture','url':'https://www.fincen.gov/synthetic-fixture',
                'jurisdiction':'US','sourceKind':'foreign_typology','effectiveDate':None,
                'sections':[{'heading':'Recommendation 10','end':'End'}]}
        raw=b'<h3>Recommendation 10</h3><p>FATF customer due diligence synthetic fixture only</p><h3>End</h3>'
        r=self.manager.register(source,raw);self.manager.publish(source['documentId'],r['revisionId'],self.index.corpus_hash)
        self.index=Index(self.path)
        result=self.allows('FATF Recommendation 10 customer due diligence original',jurisdiction='ALL')
        self.assertTrue(result['hits'])
        self.assertTrue(all(r['documentId']=='synthetic-fatf' for r in result['hits']))
        self.abstains('FATF Recommendation 11 customer due diligence original',
                      'requested_section_unavailable',jurisdiction='ALL')

    def test_eu_statute_request_does_not_use_korean_guidance(self):
        self.abstains('EU 자금세탁 규정의 최신 고객확인 조항을 알려줘','requested_jurisdiction_unavailable',jurisdiction='ALL')
        self.allows('한국 고객확인 제도의 개념을 설명해줘')

    def test_specific_customer_values_and_request_variants_abstain(self):
        for query in ['AC-7788 실질 계좌주 성명과 실제 소유자를 알아봐 주세요',
                      '이번 화면에서 고른 사례의 최종 수익자가 누구인지 설명해줘',
                      '박민수 고객의 직업과 실제 소유자를 알려줘']:
            with self.subTest(query=query):self.abstains(query,'customer_facts_unavailable')

    def test_customer_procedure_questions_with_identifiers_are_allowed(self):
        for query in ['AC-7788 고객의 실제 소유자를 확인할 때 필요한 서류를 알려줘',
                      '영리법인 고객의 주소와 연락처 확인 항목을 알려줘',
                      '개인 고객의 신원 확인 항목을 알려줘',
                      'AC-7788의 실제 소유자가 누구인지 확인하는 절차에 필요한 서류를 설명해줘']:
            with self.subTest(query=query):self.allows(query)

    def test_mixed_customer_value_and_procedure_request_still_abstains(self):
        self.abstains('AC-7788의 실제 이름을 알려주고 확인에 필요한 서류도 설명해줘','customer_facts_unavailable')

    def test_crime_assertion_and_score_to_decision_requests_abstain(self):
        self.abstains('사람이 검토하지 않아도 되는 자금세탁 범죄 확정 문장을 만들어 주세요','automatic_decision_not_supported')
        self.abstains('ML 확률 0.81을 넘으면 무조건 신고해야 하는 기준을 알려줘','score_decision_policy_unavailable')

    def test_caution_about_crime_assertions_is_not_a_decision_request(self):
        self.allows('자동으로 범죄 확정하면 안 되는 이유를 설명해줘')
        self.allows('범죄 확정 없이 사람의 검토가 필요한 이유를 알려줘')

    def test_policy_version_changes_only_corpus_metadata_and_is_reversible(self):
        self.assertEqual(self.index.chunks,self.old.chunks)
        self.assertNotEqual(self.index.corpus_hash,self.old.corpus_hash)
        same=self.manager.set_query_scope_policy('1.0',self.index.corpus_hash)
        self.assertEqual(same['status'],'no_change')
        restored=self.manager.set_query_scope_policy('legacy',self.index.corpus_hash)
        self.assertEqual(restored['corpusSha256'],self.old.corpus_hash)
        with self.assertRaises(ValueError):self.manager.set_query_scope_policy('9.9',restored['corpusSha256'])
        with self.assertRaises(ValueError):self.manager.set_query_scope_policy('1.0','0'*64)

    def test_prior_report_and_search_representation_stay_pinned(self):
        graph=self.graph();report=generate_report(build_context(self.old,graph))
        self.assertTrue(verify_report(EvidenceRepository(self.index),graph,None,report)['valid'])
        self.assertNotIn('scopeAssessment',self.old.search('STR 정의'))
        self.assertNotIn('answerDisposition',report)

    def test_legacy_policy_cannot_generate_new_reports_but_stored_evidence_is_readable(self):
        graph=self.graph();context=build_context(self.old,graph)
        tools=AgentTools(self.index)
        for name in ['build_evidence_context','generate_review_report','prepare_review_report','assess_evidence_fit']:
            with self.subTest(name=name),self.assertRaises(EvidenceError) as caught:
                tools.call(name,{'graph':graph,'corpusSha256':self.old.corpus_hash})
            self.assertEqual(caught.exception.code,'legacy_corpus_read_only')
        fact=tools.call('resolve_report_fact',{'graph':graph,'factId':'target.amountPaid',
                        'contextId':context['contextId'],'corpusSha256':self.old.corpus_hash})
        self.assertEqual(fact['contextId'],context['contextId'])
        citation=self.old.chunks[0]
        self.assertEqual(tools.call('get_citation',{'chunkId':citation['chunkId'],
                          'corpusSha256':self.old.corpus_hash})['sourceVerification']['status'],'snapshot_checked')

    def test_report_explains_abstention_even_when_other_reference_citations_exist(self):
        graph=self.graph();query='KB 내부 지침의 STR 처리 기한을 알려줘'
        context=build_context(self.index,graph,query=query)
        self.assertTrue(context['citations'])
        self.assertEqual(context['contextVersion'],'evidence-scope-v4')
        self.assertEqual(context['answerDisposition']['status'],'abstained')
        report=generate_report(context)
        self.assertIn('## 원질문 답변 보류',report['markdown'])
        self.assertIn('내부 지침',report['markdown'])
        self.assertTrue(verify_report(EvidenceRepository(self.index),graph,None,report,query)['valid'])

    def test_self_rehashed_removal_of_abstention_state_is_rejected(self):
        graph=self.graph();query='FATF 권고 10 원문을 찾아줘'
        report=generate_report(build_context(self.index,graph,query=query))
        report['answerDisposition']['status']='answered'
        report['reportId']='RPT-'+digest(canonical({k:v for k,v in report.items() if k!='reportId'}))[:24]
        self.assertFalse(verify_report(EvidenceRepository(self.index),graph,None,report,query)['valid'])

    def test_future_registered_bank_document_does_not_authorize_other_banks(self):
        source={'documentId':'synthetic-private','title':'신한은행 내부 지침 synthetic fixture',
                'publisher':'신한은행 synthetic fixture','url':'https://kofiu.go.kr/synthetic-fixture',
                'jurisdiction':'KR','sourceKind':'bank_internal_policy','effectiveDate':None,
                'sections':[{'heading':'내부 지침','end':'끝'}]}
        r=self.manager.register(source,'<h3>내부 지침</h3><p>처리 기한 합성 문서</p><h3>끝</h3>'.encode())
        self.manager.publish(source['documentId'],r['revisionId'],self.index.corpus_hash);self.index=Index(self.path)
        self.abstains('KB 내부 지침 처리 기한','internal_policy_unavailable')
        self.allows('신한은행 내부 지침 처리 기한')

    def test_http_and_agent_return_explicit_reasons(self):
        query='KB 내부 지침 STR 처리 기한'
        tools=AgentTools(self.index)
        result=tools.call('search_regulations',{'query':query})
        self.assertEqual(result['scopeAssessment']['decision'],'abstain')
        assessment=tools.call('assess_evidence_fit',{'graph':self.graph(),'query':query})
        self.assertEqual(assessment['answerDisposition']['status'],'abstained')
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.index))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=http.client.HTTPConnection(*server.server_address,timeout=10)
            client.request('POST','/v1/rag/search',json.dumps({'query':query}),{'Content-Type':'application/json'})
            response=client.getresponse();body=json.loads(response.read());client.close()
            self.assertEqual(response.status,200)
            self.assertIn('internal_policy_unavailable',body['scopeAssessment']['reasonCodes'])
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
