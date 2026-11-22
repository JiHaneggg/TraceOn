"""Archive persistence, offline evidence, exact identities and request boundaries."""
from copy import deepcopy
from contextlib import closing
from http.server import ThreadingHTTPServer
import http.client
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as viewer
from analysis_history import AnalysisHistory
from history_store import HistoryStore,encoded
from review_bridge import ReviewBridge,ReviewError
import test_review_bridge as helpers


class HistoryFixture:
    @classmethod
    def setUpClass(cls):
        helpers.fixtures.RagTest.setUpClass();cls.fixture=helpers.fixtures.RagTest()
        cls.repository=helpers.EvidenceRepository(cls.fixture.index,cls.fixture.directory)
    @classmethod
    def tearDownClass(cls):helpers.fixtures.RagTest.tearDownClass()
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.path=Path(self.directory.name)/'history/analysis.sqlite3'
        self.store=HistoryStore(self.path);self.client=helpers.RagFunctions(self.fixture.index,self.repository)
        self.reviews=ReviewBridge(self.client);self.history=AnalysisHistory(self.reviews,self.store)
        self.payload=viewer.render_graph_payload(self.fixture.graph());self.graph=self.reviews.remember_graph(self.payload)
    def tearDown(self):self.directory.cleanup()
    def prepare(self):return self.reviews.prepare(self.graph['reviewSnapshotId'],0)
    def save(self,with_review=True):
        review=self.prepare() if with_review else None
        return self.history.save(self.graph['reviewSnapshotId'],review['reviewId'] if review else None)
    def assert_code(self,code,action):
        with self.assertRaises(ReviewError) as caught:action()
        self.assertEqual(caught.exception.code,code)


class HistoryTest(HistoryFixture,unittest.TestCase):
    def test_graph_only_save_is_durable_and_does_not_call_rag(self):
        saved=self.save(False);self.assertTrue(saved['created']);self.assertFalse(saved['history']['hasReview']);self.assertEqual(self.client.calls,[])
        restarted=AnalysisHistory(ReviewBridge(),HistoryStore(self.path));opened=restarted.open(saved['history']['historyId'])
        self.assertEqual(opened['payload']['graph'],self.payload['graph']);self.assertEqual(opened['payload']['ruleResult'],self.payload['ruleResult'])
        self.assertIsNone(opened['review']);self.assertNotIn('reviewSnapshotId',opened['payload'])
        self.assertFalse(opened['history']['integrity']['currentSourcesChecked']);self.assertFalse(opened['history']['readyForSubmission'])

    def test_full_save_reopens_report_and_original_citation_offline(self):
        prepared=self.prepare();saved=self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId']);id=saved['history']['historyId']
        expected=self.history.open(id);citation=prepared['report']['citations'][0]
        original=self.history.citation(id,citation['chunkId']);fact=self.history.fact(id,'target.amountPaid')
        self.assertEqual(original['citation']['text'],citation['text']);self.assertEqual(fact['fact']['value'],prepared['context']['targetTransaction']['amountPaid'])
        # Expired memory and an unavailable/new corpus must not be substituted.
        restarted=AnalysisHistory(ReviewBridge(),HistoryStore(self.path))
        with patch.object(restarted.reviews.client,'call',side_effect=AssertionError('No live RAG calls allowed')):
            self.assertEqual(restarted.open(id),expected);self.assertEqual(restarted.citation(id,citation['chunkId']),original)
            self.assertEqual(restarted.fact(id,'target.amountPaid'),fact);self.assertTrue(restarted.verify(id)['history']['integrity']['valid'])
        self.assertEqual(expected['review']['report'],prepared['report']);self.assertEqual(expected['review']['context'],prepared['context'])

    def test_same_save_is_idempotent_but_graph_only_and_full_are_distinct(self):
        first=self.save(False);second=self.save(False)
        self.assertEqual(first['history'],second['history']);self.assertFalse(second['created'])
        full=self.save(True);self.assertNotEqual(first['history']['historyId'],full['history']['historyId'])
        self.assertEqual(self.history.listing()['total'],2)

    def test_saving_same_report_twice_keeps_the_original_saved_time(self):
        prepared=self.prepare();a=self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId']);b=self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId'])
        self.assertEqual(a['history'],b['history']);self.assertFalse(b['created'])

    def test_invalidated_snapshot_or_cross_snapshot_report_cannot_be_saved(self):
        prepared=self.prepare();other=self.reviews.remember_graph(self.payload)
        self.assert_code('history_review_mismatch',lambda:self.history.save(other['reviewSnapshotId'],prepared['reviewId']))
        self.reviews.invalidate_snapshot(self.graph['reviewSnapshotId'])
        self.assert_code('review_expired',lambda:self.history.save(self.graph['reviewSnapshotId'],None))
        self.assertEqual(self.history.listing()['total'],0)

    def test_invalidated_graph_during_capture_has_no_partial_archive(self):
        prepared=self.prepare();original=self.reviews.citation
        def invalidate(*args):
            result=original(*args);self.reviews.invalidate_snapshot(self.graph['reviewSnapshotId']);return result
        with patch.object(self.reviews,'citation',side_effect=invalidate):
            self.assert_code('review_expired',lambda:self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId']))
        self.assertEqual(self.history.listing()['total'],0)

    def test_rejected_report_or_unavailable_original_is_not_saved(self):
        prepared=self.prepare()
        def reject(response):response['valid']=False
        self.client.modify=reject
        self.assert_code('review_not_verified',lambda:self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId']))
        self.client.modify=None
        with patch.object(self.reviews,'citation',side_effect=ReviewError(503,'rag_unavailable','fixture')):
            self.assert_code('rag_unavailable',lambda:self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId']))
        self.assertEqual(self.history.listing()['total'],0)

    def test_capture_deadline_does_not_create_incomplete_record(self):
        prepared=self.prepare();times=iter([0,31]);self.history.clock=lambda:next(times)
        self.assert_code('history_capture_timeout',lambda:self.history.save(self.graph['reviewSnapshotId'],prepared['reviewId']))
        self.assertEqual(self.history.listing()['total'],0)

    def test_unknown_history_ids_and_source_members_fail_closed(self):
        saved=self.save();id=saved['history']['historyId']
        for value in ['../.env',"H-' OR 1=1",[],None]:self.assert_code('invalid_history_id',lambda:self.history.open(value))
        self.assert_code('history_not_found',lambda:self.history.open('H-'+'0'*24))
        self.assert_code('citation_not_in_report',lambda:self.history.citation(id,'unknown:chunk'))
        self.assert_code('fact_not_in_report',lambda:self.history.fact(id,'finding.999.amount'))

    def test_capacity_never_evicts_or_overwrites_existing_snapshot(self):
        self.store.max_entries=1;saved=self.save(False);id=saved['history']['historyId'];original=self.history.open(id)
        self.graph=self.reviews.remember_graph({**self.payload,'source':{**self.payload['source'],'queriedAt':'2026-11-18T01:00:00Z'}})
        self.assert_code('history_capacity',lambda:self.save(False));self.assertEqual(self.history.open(id),original)

    def test_entry_and_total_size_bounds_do_not_write_partial_records(self):
        with patch('history_store.MAX_ENTRY_BYTES',100):self.assert_code('history_entry_too_large',lambda:self.save(False))
        self.store.max_bytes=1;self.assert_code('history_capacity',lambda:self.save(False));self.assertEqual(self.history.listing()['total'],0)

    def test_corrupt_saved_body_is_rejected_for_open_verify_and_download(self):
        saved=self.save(False);id=saved['history']['historyId']
        with self.store.connection() as db:db.execute('UPDATE analysis_history SET body=? WHERE id=?',(b'broken',id));db.commit()
        for action in [lambda:self.history.open(id),lambda:self.history.verify(id),lambda:self.history.download(id,'json'),lambda:self.history.listing()]:
            self.assert_code('history_integrity_failed',action)

    def test_listing_has_bounded_cursor_pagination_without_duplicate_records(self):
        ids=set()
        for i in range(23):
            self.graph=self.reviews.remember_graph({**self.payload,'source':{**self.payload['source'],'queriedAt':str(i)}})
            ids.add(self.save(False)['history']['historyId'])
        first=self.history.listing();second=self.history.listing(first['nextCursor'])
        self.assertEqual(len(first['items']),20);self.assertEqual(len(second['items']),3);self.assertIsNone(second['nextCursor'])
        self.assertEqual({item['historyId'] for item in first['items']+second['items']},ids)

    def test_download_preserves_full_bundle_and_marks_historical_scope(self):
        saved=self.save();id=saved['history']['historyId'];name,raw,mime=self.history.download(id,'json');data=json.loads(raw)
        self.assertEqual(mime,'application/json');self.assertTrue(name.endswith('-archive.json'))
        self.assertFalse(data['currentSourcesChecked']);self.assertNotIn('reviewId',data['capture']['review']);self.assertNotIn('reviewSnapshotId',data['capture']['payload'])
        self.assertEqual(data['snapshotSha256'],saved['history']['snapshotSha256'])
        _,raw,_=self.history.download(id,'markdown');self.assertIn('현재 자료는 재조회하지 않았습니다.',raw.decode())
        self.assert_code('invalid_format',lambda:self.history.download(id,'html'))

    def test_unsupported_or_broken_database_fails_without_echoing_paths(self):
        self.path.parent.mkdir(parents=True);self.path.write_bytes(b'not sqlite')
        with self.assertRaises(ReviewError) as caught:self.history.listing()
        self.assertEqual(caught.exception.status,503);self.assertNotIn(str(self.path),caught.exception.message)

    def test_unrelated_database_is_not_changed_or_adopted(self):
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as db:db.execute('CREATE TABLE unrelated (value TEXT)');db.commit()
        self.assert_code('history_schema_mismatch',lambda:self.history.listing())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('unrelated',)])


class HistoryHttpTest(HistoryFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        def unavailable():raise AssertionError('History must not access Neo4j')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),viewer.handler_for([self.payload['graph']['targetTxId']],unavailable,review_bridge=self.reviews,history_store=self.store))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(5);super().tearDown()
    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=10)
        try:
            conn.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers={'Content-Type':'application/json',**(headers or {})})
            response=conn.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:conn.close()
    def test_save_list_open_and_download_work_without_neo4j(self):
        status,_,raw=self.request('/api/history/save',{'snapshotId':self.graph['reviewSnapshotId'],'reviewId':None})
        self.assertEqual(status,200);id=json.loads(raw)['history']['historyId']
        for path in ['/api/history','/api/history/open?historyId='+id,'/api/history/verify?historyId='+id]:
            status,headers,_=self.request(path);self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'no-store')
        status,headers,raw=self.request('/api/history/download?historyId='+id+'&format=json')
        self.assertEqual(status,200);self.assertIn('attachment;',headers['Content-Disposition']);self.assertFalse(json.loads(raw)['currentSourcesChecked'])
    def test_cross_origin_extra_fields_and_arbitrary_database_paths_are_denied(self):
        body={'snapshotId':self.graph['reviewSnapshotId'],'reviewId':None}
        self.assertEqual(self.request('/api/history/save',body,{'Origin':'http://localhost:9999'})[0],403)
        for key,value in [('graph',{}),('path','../.env'),('title','injected')]:self.assertEqual(self.request('/api/history/save',{**body,key:value})[0],400)
        for path in ['/api/history?path=../.env','/api/history/open?historyId=bad&historyId=bad','/api/history/open?historyId=../.env','/data/history/analysis.sqlite3']:
            self.assertIn(self.request(path)[0],{400,404})
