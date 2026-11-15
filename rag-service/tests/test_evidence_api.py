import copy
import http.client
import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import test_rag as fixtures
from corpus import build, canonical, digest
from retrieval import Index
from search_intent import prioritize
from evidence import build_context, validate_draft
from evidence_lookup import EvidenceRepository, EvidenceError, resolve_fact
from report_verification import verify_report
from reports import generate_report
from agent_tools import AgentTools
from rag_server import handler_for
from http.server import ThreadingHTTPServer


class EvidenceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RagTest.setUpClass()
        cls.base = fixtures.RagTest()
        cls.index = cls.base.index
        cls.repository = EvidenceRepository(cls.index, raw_root=cls.base.directory)

    @classmethod
    def tearDownClass(cls):
        fixtures.RagTest.tearDownClass()

    def context(self):
        return build_context(self.index, self.base.graph())

    def report(self):
        return generate_report(self.context())

    def rehash(self, report):
        report['reportId'] = 'RPT-' + digest(canonical({key:value for key,value in report.items() if key != 'reportId'}))[:24]
        return report

    def verify(self, report):
        return verify_report(self.repository, self.base.graph(), None, report)

    def test_definition_and_comparison_preserve_eligible_concepts(self):
        rows = [
            {'chunkId':'str-procedure','title':'의심거래보고제도','section':'보고 방법 및 절차','retrievalScore':20},
            {'chunkId':'ctr-def','title':'고액현금거래보고제도','section':'개념','retrievalScore':18},
            {'chunkId':'str-def','title':'의심거래보고제도','section':'정의','retrievalScore':16}]
        ranked, info = prioritize(rows, 'STR은 어떤 경우에 하는 보고인가요')
        self.assertEqual(ranked[0]['chunkId'],'str-def')
        self.assertEqual(ranked[0]['retrievalScore'],16)
        ranked, info = prioritize(rows, 'STR과 CTR의 차이는 무엇인가요')
        self.assertEqual({r['chunkId'] for r in ranked[:2]},{'str-def','ctr-def'})
        self.assertEqual(info['intent'],'comparison')

    def test_procedural_question_keeps_relevance_order(self):
        rows = [{'chunkId':'a','title':'의심거래보고제도','section':'보고 절차'},
                {'chunkId':'b','title':'의심거래보고제도','section':'정의'}]
        ranked, info = prioritize(rows, 'STR 보고서에 무엇을 기재하나요')
        self.assertEqual(ranked, rows)
        self.assertEqual(info['intent'],'lexical')

    def test_intent_does_not_create_ineligible_passages(self):
        rows=[{'chunkId':'a','title':'고액현금거래보고제도','section':'개념'}]
        ranked, _=prioritize(rows,'STR 정의를 알려주세요')
        self.assertEqual(ranked, rows)

    def test_citation_snapshot_and_highlight_match(self):
        row=self.index.chunks[0]
        result=self.repository.citation(row['chunkId'],self.index.corpus_hash)
        self.assertEqual(result['sourceVerification']['status'],'snapshot_checked')
        h=result['section']['highlight']
        self.assertEqual(result['section']['text'][h['start']:h['end']],row['text'])
        self.assertEqual(result['citation']['textSha256'],digest(row['text']))
        self.assertNotIn(str(self.base.directory),canonical(result))

    def test_citation_lookup_does_not_mutate_index(self):
        row=self.index.chunks[0]
        result=self.repository.citation(row['chunkId'])
        result['citation']['locator']['start']=-1
        self.assertGreaterEqual(row['locator']['start'],0)

    def test_missing_snapshot_is_explicit_index_only(self):
        repo=EvidenceRepository(self.index,raw_root=self.base.directory/'absent')
        result=repo.citation(self.index.chunks[0]['chunkId'])
        self.assertEqual(result['sourceVerification']['status'],'index_only')
        self.assertIsNone(result['section']['text'])

    def test_corrupt_snapshot_is_rejected(self):
        row=self.index.chunks[0]
        file=self.base.directory/'snapshots'/row['documentId']/(row['sourceSha256']+'.html')
        original=file.read_bytes()
        try:
            file.write_bytes(b'changed source')
            with self.assertRaises(EvidenceError) as caught:self.repository.citation(row['chunkId'])
            self.assertEqual(caught.exception.code,'snapshot_hash_mismatch')
        finally:file.write_bytes(original)

    def test_unavailable_version_never_uses_active_corpus(self):
        with self.assertRaises(EvidenceError) as caught:self.repository.citation(self.index.chunks[0]['chunkId'],'0'*64)
        self.assertEqual(caught.exception.status,409)

    def test_invalid_ids_and_unknown_citation(self):
        for value in ['../../.env','a/b','',[]]:
            with self.assertRaises(EvidenceError) as caught:self.repository.citation(value)
            self.assertEqual(caught.exception.status,400)
        with self.assertRaises(EvidenceError) as caught:self.repository.citation('missing:0000')
        self.assertEqual(caught.exception.status,404)

    def test_archived_version_returns_its_original_text(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);raw=root/'raw';raw.mkdir();archive=root/'versions';archive.mkdir()
            source=self.base.sources[0]
            def write(text):
                (raw/'str.html').write_text('<h3>의심거래보고 정의</h3><p>'+text+'</p><h3>끝</h3>',encoding='utf-8')
                build(raw,root/'index.json',sources=[source])
                return Index(root/'index.json')
            old=write('과거 버전의 합성 검증 본문')
            (archive/('v1-'+old.corpus_hash+'.json')).write_bytes((root/'index.json').read_bytes())
            current=write('새 버전의 합성 검증 본문')
            repo=EvidenceRepository(current,raw_root=raw,archive_dir=archive)
            result=repo.citation(old.chunks[0]['chunkId'],old.corpus_hash)
            self.assertEqual(result['citation']['text'],old.chunks[0]['text'])
            self.assertEqual(result['sourceVerification']['status'],'snapshot_checked')
            self.assertNotEqual(result['corpusSha256'],current.corpus_hash)
            with self.assertRaises(EvidenceError):repo.citation(old.chunks[0]['chunkId'])

    def test_fact_lookup_returns_exact_transactions(self):
        context=self.context()
        result=resolve_fact(context,'target.amountPaid',context['contextId'])
        self.assertEqual(result['fact']['value'],context['targetTransaction']['amountPaid'])
        self.assertEqual([row['txId'] for row in result['transactions']],[context['targetTxId']])
        self.assertFalse(result['verification']['externalLedgerChecked'])

    def test_fact_unknown_or_context_mismatch(self):
        with self.assertRaises(EvidenceError) as caught:resolve_fact(self.context(),'target.amountPaid','CTX-old')
        self.assertEqual(caught.exception.status,409)
        with self.assertRaises(EvidenceError) as caught:resolve_fact(self.context(),'not.real')
        self.assertEqual(caught.exception.status,404)

    def test_complete_report_verifies_against_sources(self):
        result=self.verify(self.report())
        self.assertTrue(result['valid'],result['errors'])
        self.assertTrue(all(result['checks'][key]['valid'] for key in ['amounts','times','counts','citations','sourceSnapshots','renderedContent']))
        self.assertTrue(result['humanReviewRequired'])
        self.assertFalse(result['semanticEntailmentVerified'])
        self.assertFalse(result['readyForSubmission'])

    def test_amount_time_and_count_tampering_fail_separate_checks(self):
        for fact_id,value,category in [('target.amountPaid','999999','amounts'),
                                       ('finding.0.elapsedSeconds','121','times'),
                                       ('finding.0.transactionCount',999,'counts')]:
            report=self.report()
            next(row for row in report['draft']['facts'] if row['factId']==fact_id)['value']=value
            result=self.verify(self.rehash(report))
            self.assertFalse(result['valid'])
            self.assertFalse(result['checks'][category]['valid'])

    def test_numeric_type_change_is_rejected(self):
        report=self.report()
        fact=next(row for row in report['draft']['facts'] if row['factId']=='finding.0.transactionCount')
        fact['value']=str(fact['value'])
        self.assertFalse(self.verify(self.rehash(report))['checks']['counts']['valid'])

    def test_partial_report_and_duplicate_facts_fail(self):
        report=self.report();report['draft']['facts'].pop()
        result=self.verify(self.rehash(report))
        self.assertFalse(result['checks']['coverage']['valid'])
        report=self.report();report['draft']['facts'].append(copy.deepcopy(report['draft']['facts'][0]))
        self.assertFalse(self.verify(self.rehash(report))['valid'])

    def test_invented_quote_fails(self):
        report=self.report();report['draft']['citations'][0]['quote']='만들어낸 법 조항 제999조'
        result=self.verify(self.rehash(report))
        self.assertFalse(result['checks']['citations']['valid'])

    def test_changed_prose_is_rejected_even_with_correct_facts_and_new_digest(self):
        report=self.report();report['markdown']+='\n범죄로 확정됨\n'
        result=self.verify(self.rehash(report))
        self.assertFalse(result['valid'])
        self.assertTrue(result['checks']['amounts']['valid'])
        self.assertFalse(result['checks']['renderedContent']['valid'])

    def test_changed_citation_url_and_extra_conclusion_are_rejected(self):
        report=self.report();report['citations'][0]['url']='https://example.invalid/invented'
        self.assertFalse(self.verify(self.rehash(report))['checks']['renderedContent']['valid'])
        report=self.report();report['finalVerdict']='criminal'
        self.assertFalse(self.verify(self.rehash(report))['checks']['renderedContent']['valid'])

    def test_missing_raw_snapshots_prevent_full_verification(self):
        repo=EvidenceRepository(self.index,raw_root=self.base.directory/'absent')
        result=verify_report(repo,self.base.graph(),None,self.report())
        self.assertTrue(result['checks']['amounts']['valid'])
        self.assertFalse(result['checks']['sourceSnapshots']['valid'])
        self.assertFalse(result['valid'])

    def test_legacy_subset_validation_remains_available(self):
        context=self.context()
        self.assertTrue(validate_draft(context,self.base.draft(context))['valid'])
        self.assertFalse(validate_draft(context,self.base.draft(context),require_all_facts=True)['valid'])

    def test_navigation_is_pinned_and_fact_lookup_contains_context(self):
        context=self.context();report=generate_report(context)
        self.assertTrue(all(self.index.corpus_hash in row['lookup']['path'] for row in report['citations']))
        self.assertTrue(all(row['contextId']==context['contextId'] for row in report['evidenceLinks']['facts']))

    def test_agent_evidence_and_prepare_tools(self):
        tools=AgentTools(self.index,raw_root=self.base.directory)
        prepared=tools.call('prepare_review_report',{'graph':self.base.graph()})
        self.assertTrue(prepared['verification']['valid'])
        citation=prepared['report']['citations'][0]
        self.assertEqual(tools.call('get_citation',{'chunkId':citation['chunkId'],'corpusSha256':self.index.corpus_hash})['sourceVerification']['status'],'snapshot_checked')
        self.assertTrue(tools.call('verify_review_report',{'graph':self.base.graph(),'report':prepared['report']})['valid'])
        fact=tools.call('resolve_report_fact',{'graph':self.base.graph(),'factId':'target.timestamp','contextId':prepared['context']['contextId']})
        self.assertEqual(fact['fact']['value'],prepared['context']['targetTransaction']['timestamp'])

    def test_http_evidence_prepare_verify_and_lookup(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.index,raw_root=self.base.directory))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,body=None):
            with closing(http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)) as client:
                client.request(method,path,None if body is None else json.dumps(body),{'Content-Type':'application/json'})
                response=client.getresponse();return response.status,json.loads(response.read())
        try:
            status,prepared=request('POST','/v1/rag/reports/prepare',{'graph':self.base.graph()})
            self.assertEqual(status,200);self.assertTrue(prepared['verification']['valid'])
            citation=prepared['report']['citations'][0]
            self.assertEqual(request('GET',citation['lookup']['path'])[0],200)
            self.assertEqual(request('GET','/v1/rag/citations/'+quote(citation['chunkId'],safe='')+'?corpusSha256=')[0],400)
            self.assertEqual(request('GET','/v1/rag/citations/'+quote(citation['chunkId'],safe='')+'?corpusSha256='+'0'*64)[0],409)
            self.assertEqual(request('GET','/v1/rag/citations/..%2F.env')[0],400)
            self.assertEqual(request('GET','/v1/rag/citations/not-found:1')[0],404)
            status,fact=request('POST','/v1/rag/facts/resolve',{'graph':self.base.graph(),'factId':'finding.0.transactionCount','contextId':prepared['context']['contextId']})
            self.assertEqual(status,200);self.assertEqual(fact['fact']['value'],3)
            self.assertEqual(request('POST','/v1/rag/facts/resolve',{'graph':self.base.graph(),'factId':'target.timestamp','contextId':'CTX-old'})[0],409)
            status,verified=request('POST','/v1/rag/reports/verify',{'graph':self.base.graph(),'report':prepared['report']})
            self.assertEqual(status,200);self.assertTrue(verified['valid'])
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
