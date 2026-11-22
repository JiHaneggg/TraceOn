"""Real local RAG functions over synthetic documents; no network/model/real DB."""
from copy import deepcopy
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'graph-viewer'),str(ROOT/'rag-service'),str(ROOT/'rag-service/tests')]
from review_bridge import BoundedStore,LocalRagClient,ReviewBridge,ReviewError,graph_hash
import test_rag as fixtures
from evidence import build_context
from evidence_lookup import EvidenceRepository,resolve_fact
from report_verification import verify_report
from reports import generate_report
from rules import score
spec=importlib.util.spec_from_file_location('traceon_review_server',ROOT/'graph-viewer/server.py')
server_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(server_module)


class RagFunctions:
    def __init__(self,index,repository):self.index=index;self.repository=repository;self.calls=[];self.modify=None
    def call(self,method,path,body=None):
        self.calls.append((method,path,deepcopy(body)))
        if path.endswith('/prepare'):
            assert body['mode']=='template'
            context=build_context(self.index,body['graph'],body['ruleResult'])
            report=generate_report(context)
            result={'context':context,'report':report,'verification':verify_report(self.repository,body['graph'],body['ruleResult'],report)}
        elif path.endswith('/verify'):result=verify_report(self.repository,body['graph'],body['ruleResult'],body['report'])
        elif path.endswith('/resolve'):result=resolve_fact(build_context(self.index,body['graph'],body['ruleResult']),body['factId'],body['contextId'])
        else:
            parsed=urlsplit(path)
            result=self.repository.citation(unquote(parsed.path.split('/')[-1]),parse_qs(parsed.query)['corpusSha256'][0])
        if self.modify:self.modify(result)
        return result


class ReviewBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RagTest.setUpClass();cls.fixture=fixtures.RagTest();cls.repository=EvidenceRepository(cls.fixture.index,cls.fixture.directory)
    @classmethod
    def tearDownClass(cls):fixtures.RagTest.tearDownClass()
    def setUp(self):
        self.client=RagFunctions(self.fixture.index,self.repository);self.bridge=ReviewBridge(self.client)
        self.payload={'graph':self.fixture.graph(),'ruleResult':score(self.fixture.graph()),'source':{'queriedAt':'2026-11-16T00:00:00Z'}}
        self.graph=self.bridge.remember_graph(self.payload)
    def prepare(self):return self.bridge.prepare(self.graph['reviewSnapshotId'],0)
    def test_prepare_uses_server_snapshot_and_explicit_template_only(self):
        self.payload['graph']['edges'][0]['amountPaid']='999999.00'
        result=self.prepare();sent=self.client.calls[0][2]
        self.assertNotEqual(sent['graph']['edges'][0]['amountPaid'],'999999.00')
        self.assertEqual(sent['mode'],'template');self.assertTrue(result['verification']['valid']);self.assertFalse(result['report']['generation']['llmCalled'])
        self.assertEqual(result['source']['reportScope'],'complete_queried_graph')
    def test_fact_lookup_returns_original_transactions_and_same_context(self):
        result=self.prepare();fact=self.bridge.fact(result['reviewId'],'target.amountPaid')
        self.assertEqual(fact['contextId'],result['context']['contextId'])
        self.assertEqual(fact['transactions'][0]['txId'],self.payload['graph']['targetTxId'])
        self.assertEqual(fact['fact']['value'],result['context']['targetTransaction']['amountPaid'])
    def test_citation_lookup_pins_version_and_checks_original_snapshot(self):
        result=self.prepare();citation=result['report']['citations'][0]
        resolved=self.bridge.citation(result['reviewId'],citation['chunkId'])
        self.assertEqual(resolved['corpusSha256'],result['report']['corpusSha256'])
        self.assertEqual(resolved['sourceVerification']['status'],'snapshot_checked')
    def test_unknown_fact_or_citation_is_rejected_before_upstream_call(self):
        result=self.prepare();before=len(self.client.calls)
        for method,id in [(self.bridge.fact,'finding.999.amount'),(self.bridge.citation,'unknown:chunk')]:
            with self.assertRaises(ReviewError) as caught:method(result['reviewId'],id)
            self.assertEqual(caught.exception.status,404)
        self.assertEqual(len(self.client.calls),before)
    def test_forged_graph_identity_is_rejected(self):
        self.client.modify=lambda result:result['context'].update(graphSha256='0'*64)
        with self.assertRaises(ReviewError) as caught:self.prepare()
        self.assertEqual(caught.exception.status,502)
    def test_invalidated_graph_blocks_all_existing_review_operations(self):
        result=self.prepare();self.bridge.invalidate_snapshot(self.graph['reviewSnapshotId'])
        before=len(self.client.calls)
        for operation in [lambda:self.prepare(),lambda:self.bridge.verify(result['reviewId']),
                          lambda:self.bridge.fact(result['reviewId'],'target.amountPaid'),
                          lambda:self.bridge.citation(result['reviewId'],result['report']['citations'][0]['chunkId']),
                          lambda:self.bridge.download(result['reviewId'],'json')]:
            with self.assertRaises(ReviewError) as caught:operation()
            self.assertEqual(caught.exception.status,409)
        self.assertEqual(len(self.client.calls),before)
    def test_graph_invalidated_during_prepare_does_not_return_a_review(self):
        self.client.modify=lambda response:self.bridge.invalidate_snapshot(self.graph['reviewSnapshotId'])
        with self.assertRaises(ReviewError) as caught:self.prepare()
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(len(self.bridge.reviews.items),0)
    def test_graph_invalidated_during_verify_does_not_return_a_pass(self):
        result=self.prepare()
        self.client.modify=lambda response:self.bridge.invalidate_snapshot(self.graph['reviewSnapshotId'])
        with self.assertRaises(ReviewError) as caught:self.bridge.verify(result['reviewId'])
        self.assertEqual(caught.exception.status,409)
    def test_wrong_corpus_citation_is_rejected(self):
        result=self.prepare();self.client.modify=lambda result:result.update(corpusSha256='0'*64)
        with self.assertRaises(ReviewError):self.bridge.citation(result['reviewId'],result['report']['citations'][0]['chunkId'])
    def test_altered_transaction_evidence_is_rejected(self):
        result=self.prepare()
        self.client.modify=lambda response:response['transactions'][0].update(amountPaid='0.01')
        with self.assertRaises(ReviewError):self.bridge.fact(result['reviewId'],'target.amountPaid')
    def test_reverification_keeps_a_negative_result_negative(self):
        result=self.prepare()
        def fail(response):response['valid']=False;response['checks']['sourceSnapshots']['valid']=False
        self.client.modify=fail
        self.assertFalse(self.bridge.verify(result['reviewId'])['verification']['valid'])
        with self.assertRaises(ReviewError):self.bridge.download(result['reviewId'],'json')
    def test_download_rechecks_and_returns_the_canonical_report(self):
        result=self.prepare();name,raw,mime=self.bridge.download(result['reviewId'],'json')
        self.assertTrue(name.endswith('-review.json'));self.assertEqual(mime,'application/json')
        self.assertEqual(json.loads(raw)['report'],result['report'])
        _,raw,mime=self.bridge.download(result['reviewId'],'markdown')
        self.assertEqual(raw.decode(),result['report']['markdown']);self.assertEqual(mime,'text/markdown')
        with self.assertRaises(ReviewError):self.bridge.download(result['reviewId'],'html')
    def test_contradictory_positive_verification_is_rejected(self):
        def fail(response):response['verification']['checks']['amounts']['valid']=False
        self.client.modify=fail
        with self.assertRaises(ReviewError):self.prepare()
    def test_finding_index_cannot_address_another_graph(self):
        for value in [True,-1,999,None,'0']:
            with self.subTest(value=value),self.assertRaises(ReviewError):self.bridge.prepare(self.graph['reviewSnapshotId'],value)
        self.assertEqual(self.client.calls,[])
    def test_bounded_snapshots_expire_without_requerying_or_replacing_input(self):
        now=[0];store=BoundedStore(limit=1,ttl=10,clock=lambda:now[0]);first=store.put({'value':1});second=store.put({'value':2})
        with self.assertRaises(ReviewError):store.get(first)
        self.assertEqual(store.get(second),{'value':2});now[0]=10
        with self.assertRaises(ReviewError):store.get(second)
    def test_expired_graph_invalidates_existing_review(self):
        now=[0];bridge=ReviewBridge(self.client,clock=lambda:now[0],ttl=10)
        payload=bridge.remember_graph(self.payload);now[0]=5;result=bridge.prepare(payload['reviewSnapshotId'],0);now[0]=10
        with self.assertRaises(ReviewError):bridge.fact(result['reviewId'],'target.amountPaid')
    def test_network_errors_never_echo_secrets_and_arbitrary_routes_are_denied(self):
        with patch('review_bridge.urlopen',side_effect=URLError('private-credential-sentinel')):
            with self.assertRaises(ReviewError) as caught:LocalRagClient().call('POST','/v1/rag/reports/prepare',{})
            self.assertNotIn('private-credential',caught.exception.message)
        for path in ['/health','https://example.com','/v1/rag/citations/../../.env']:
            with self.assertRaises(ValueError):LocalRagClient().call('GET',path)
    def test_http_routes_reject_client_graphs_cross_origin_and_oversized_requests(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_for([],review_bridge=self.bridge))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def send(path,body,headers=None):
            connection=http.client.HTTPConnection(*server.server_address,timeout=5)
            try:
                connection.request('POST',path,body,headers={'Content-Type':'application/json',**(headers or {})});response=connection.getresponse();raw=response.read();return response.status,json.loads(raw)
            finally:connection.close()
        try:
            body=json.dumps({'snapshotId':self.graph['reviewSnapshotId'],'findingIndex':0})
            status,result=send('/api/review/prepare',body);self.assertEqual(status,200)
            self.assertEqual(send('/api/review/prepare',json.dumps({'snapshotId':self.graph['reviewSnapshotId'],'findingIndex':0,'graph':self.payload['graph']}))[0],400)
            self.assertEqual(send('/api/review/prepare',body,{'Origin':'http://localhost:9999'})[0],403)
            self.assertEqual(send('/api/review/prepare','{"snapshotId":"a","snapshotId":"b","findingIndex":0}')[0],400)
            self.assertEqual(send('/api/review/prepare','x'*2049)[0],413)
            self.assertEqual(send('/api/graph',body)[0],405)
            self.assertEqual(send('/api/review/verify',json.dumps({'reviewId':result['reviewId']}))[0],200)
            connection=http.client.HTTPConnection(*server.server_address,timeout=5)
            try:
                connection.request('GET','/api/review/download?reviewId='+result['reviewId']+'&format=json')
                response=connection.getresponse();download=json.loads(response.read())
                self.assertEqual(response.status,200);self.assertIn('attachment;',response.getheader('Content-Disposition'))
                self.assertEqual(download['report'],result['report'])
            finally:connection.close()
        finally:server.shutdown();server.server_close();thread.join(5)
