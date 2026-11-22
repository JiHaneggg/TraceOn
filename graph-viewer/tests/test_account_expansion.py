"""Bounded, pinned account pages: source changes, duplicate cursors and stale reviews."""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as viewer
from account_expansion import ExpansionManager,VERSION_QUERY,VERIFY_QUERY,PAGE_QUERY,read_version,eligible_accounts
from graph_contract import MemoryGraph,extract
from review_bridge import ReviewBridge,ReviewError


def edge(i,s='1:hub',d=None,time='2026-01-09T12:00:00'):
    return dict(txId=f'TX-{i:09}',timestamp=time,fromAccountKey=s,toAccountKey=d or f'2:{i}',
                amountPaid='123.4500',paymentCurrency='Yuan',amountReceived='123.4500',receivingCurrency='Yuan',paymentFormat='ACH')


class Database:
    def __init__(self,count=130):
        self.rows=[edge(i) for i in range(1,count+1) if i!=999]+[edge(999,s='1:root',d='1:hub',time='2026-01-10T12:00:00')]
        self.version={'name':'synthetic-fixture','sha256':'a'*64};self.neighbor_reads={};self.calls=[];self.fail=None;self.modify=None
    def query(self,statement,parameters=None):
        self.calls.append((statement,deepcopy(parameters)))
        if self.fail==statement:raise OSError('transient fixture outage')
        if statement==VERSION_QUERY:return [deepcopy(self.version)] if self.version else []
        if statement==VERIFY_QUERY:result=[{'edge':deepcopy(row)} for row in self.rows if row['txId'] in parameters['ids']]
        elif statement in {viewer.NEIGHBOR_QUERY,PAGE_QUERY}:
            p=parameters;rows=MemoryGraph(self.rows).lookup(p['accountKey'],p['since'],p['asOf'],10000)
            if statement==PAGE_QUERY and p['cursorTime'] is not None:rows=[row for row in rows if (row['timestamp'],row['txId'])<(p['cursorTime'],p['cursorId'])]
            rows=rows[:p['limit']]
            if statement==viewer.NEIGHBOR_QUERY:self.neighbor_reads[p['accountKey']]=deepcopy(rows)
            result=[{'edge':deepcopy(row)} for row in rows]
        else:raise AssertionError('Unexpected template')
        if self.modify and statement==PAGE_QUERY:result=self.modify(result)
        return result
    def subgraph(self,tx_id,**kw):
        target=next(row for row in self.rows if row['txId']==tx_id)
        def lookup(key,since,as_of,limit):
            return [row['edge'] for row in self.query(viewer.NEIGHBOR_QUERY,dict(accountKey=key,since=since,asOf=as_of,limit=limit))]
        graph=extract(target,lookup,**kw);graph['meta'].update(sourceGraphComplete=False,sourceScope='synthetic-fixture')
        return graph


class ExpansionTest(unittest.TestCase):
    def setUp(self):
        self.db=Database();self.reviews=ReviewBridge();self.manager=ExpansionManager(self.reviews,viewer.render_graph_payload)
        self.base=self.initial()
    def initial(self,hops=1):
        before=read_version(self.db)
        return self.manager.initial(self.db,viewer.graph_payload(self.db,'TX-000000999',hops=hops),before)
    def expand(self,payload=None,key='1:hub'):
        return self.manager.expand((payload or self.base)['reviewSnapshotId'],key,self.db)
    def assert_code(self,code,fn):
        with self.assertRaises(ReviewError) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def test_page_preserves_scope_exact_values_and_source_rows(self):
        result=self.expand();before=self.base['graph'];after=result['graph']
        self.assertEqual(result['summary']['transactions'],75)
        self.assertEqual(result['accountExpansion']['lastPage']['readRows'],25)
        self.assertEqual(result['accountExpansion']['lastPage']['addedTransactions'],25)
        self.assertEqual(result['summary']['grossPaid']['Yuan'],'9258.7500')
        for key in ['targetTxId','asOf','windowStart']:self.assertEqual(before[key],after[key])
        self.assertEqual(before['meta']['hops'],after['meta']['hops'])
        self.assertTrue({r['txId'] for r in before['edges']}<={r['txId'] for r in after['edges']})
        self.assertFalse(after['meta']['sourceGraphComplete'])
        source={r['txId']:r for r in self.db.rows}
        for row in after['edges']:self.assertEqual(source[row['txId']],row)

    def test_same_timestamp_keyset_pages_have_no_skip_or_duplicate(self):
        result=self.base
        for _ in range(4):result=self.expand(result)
        self.assertEqual(len(result['graph']['edges']),131)
        self.assertEqual({r['txId'] for r in result['graph']['edges']},{r['txId'] for r in self.db.rows})
        self.assertFalse(result['accountExpansion']['canExpand'])

    def test_duplicate_only_page_advances_cursor_and_consumes_budget(self):
        # A newly discovered account's history may already be in the merged graph.
        self.db.rows.append(edge(500,s='2:120',d='3:other'))
        initial=self.initial(hops=2)
        # At depth 1 the account was already queried, so emulate an eligible new account
        # at an unqueried frontier using the exact current graph/source rows.
        token=initial['reviewSnapshotId'];self.manager.states[token]['state']['accounts'].pop('2:120')
        result=self.expand(initial,'2:120');last=result['accountExpansion']['lastPage']
        self.assertEqual(last['addedTransactions'],0);self.assertEqual(last['readRows'],2)
        self.assertEqual(last['duplicateTransactions'],2);self.assertEqual(result['accountExpansion']['pagesUsed'],1)
        self.assertFalse(any(row['accountKey']=='2:120' for row in result['accountExpansion']['candidates']))

    def test_past_only_and_window_start_are_preserved(self):
        self.db.rows.extend([edge(501,time='2026-01-11T12:00:00'),edge(502,time='2025-12-01T12:00:00')])
        result=self.expand();ids={r['txId'] for r in result['graph']['edges']}
        self.assertNotIn('TX-000000501',ids);self.assertNotIn('TX-000000502',ids)

    def test_boundary_unknown_account_and_user_input_do_not_query_database(self):
        count=len(self.db.calls)
        for key in ['2:130','unrelated',"' MATCH (n) DELETE n"]:
            self.assert_code('account_not_expandable',lambda:self.expand(key=key))
        self.assertEqual(len(self.db.calls),count)

    def test_replay_is_idempotent_and_parent_review_invalidated(self):
        result=self.expand();count=len(self.db.calls)
        self.assertEqual(self.expand(),result);self.assertEqual(len(self.db.calls),count)
        self.assert_code('review_expired',lambda:self.reviews.snapshots.get(self.base['reviewSnapshotId']))
        self.assert_code('graph_superseded',lambda:self.expand(key='1:root'))
        self.expand(result)
        self.assert_code('graph_superseded',lambda:self.expand())

    def test_source_version_or_existing_value_changes_invalidate_review(self):
        for change in ['version','amount','delete']:
            with self.subTest(change=change):
                self.setUp()
                if change=='version':self.db.version['sha256']='b'*64;code='dataset_changed'
                elif change=='amount':self.db.rows[-1]['amountPaid']='0.01';code='source_changed'
                else:self.db.rows.pop();code='source_changed'
                self.assert_code(code,lambda:self.expand())
                self.assert_code('review_expired',lambda:self.reviews.snapshots.get(self.base['reviewSnapshotId']))
                self.assert_code('source_changed',lambda:self.expand())

    def test_version_change_during_page_is_rejected(self):
        def change(rows):self.db.version['sha256']='b'*64;return rows
        self.db.modify=change
        self.assert_code('dataset_changed',lambda:self.expand())

    def test_network_failure_rolls_back_and_retry_uses_same_budget(self):
        self.db.fail=PAGE_QUERY
        with self.assertRaises(OSError):self.expand()
        self.assertEqual(self.reviews.snapshots.get(self.base['reviewSnapshotId'])['graph'],self.base['graph'])
        self.db.fail=None
        self.assertEqual(self.expand()['accountExpansion']['pagesUsed'],1)

    def test_bad_page_is_rejected_without_partial_merge(self):
        mutations=[lambda r:r+r[:1],lambda r:list(reversed(r)),lambda r:[{**r[0],'edge':{**r[0]['edge'],'timestamp':'2027-01-01T00:00:00'}}]+r[1:],
                   lambda r:[{**r[0],'edge':{**r[0]['edge'],'fromAccountKey':'9:foreign','toAccountKey':'9:other'}}]+r[1:]]
        for mutate in mutations:
            self.db.modify=mutate
            with self.assertRaises(ValueError):self.expand()
            self.assertEqual(self.manager.states[self.base['reviewSnapshotId']]['state']['pages'],0)

    def test_total_and_account_budgets_are_enforced(self):
        token=self.base['reviewSnapshotId'];state=self.manager.states[token]['state']
        state['pages']=8;self.assert_code('expansion_budget',lambda:self.expand())
        state['pages']=0;state['accounts']['1:hub']['pages']=4
        self.assert_code('account_not_expandable',lambda:self.expand())

    def test_remaining_edge_capacity_reduces_page_without_skipping_rows(self):
        self.db=Database(1020);base=self.initial()
        # Existing 999 source rows leave one available slot. Cursor still points at
        # row 972; most of this account's history is already known via other accounts.
        graph=deepcopy(base['graph']);graph['edges']=deepcopy(self.db.rows[-999:])
        graph['nodes']=[{'accountKey':k} for k in sorted({r[f] for r in graph['edges'] for f in ['fromAccountKey','toAccountKey']})]
        graph['meta']['truncated']=True
        state=deepcopy(self.manager.states[base['reviewSnapshotId']]['state'])
        state['accounts']['1:hub']['cursor']=('2026-01-09T12:00:00','TX-000000022')
        token=self.manager._remember(viewer.render_graph_payload(graph),state)
        result=self.expand(token)
        self.assertEqual(len(result['graph']['edges']),1000)
        self.assertEqual(result['accountExpansion']['lastPage']['readRows'],1)
        self.assertEqual([p['limit'] for q,p in self.db.calls if q==PAGE_QUERY][-1],2)
        self.assert_code('expansion_budget',lambda:self.expand(result))

    def test_missing_version_or_trace_disables_expansion(self):
        self.db.version=None;result=self.initial()
        self.assertFalse(result['accountExpansion']['enabled'])
        self.assert_code('expansion_not_available',lambda:self.expand(result))

    def test_initial_trace_must_have_a_stable_order(self):
        self.db.neighbor_reads['1:hub'].reverse()
        with self.assertRaises(ValueError):self.manager.initial(self.db,self.base,read_version(self.db))

    def test_newly_reachable_account_can_expand_inside_hops_but_not_beyond(self):
        self.db.rows.extend([edge(501,s='2:20',d='3:new'),edge(502,s='3:new',d='4:beyond')])
        base=self.initial(hops=2)
        # The 2:20 link is older than the initial hub page. Its account was not
        # reachable until those omitted hub transactions were requested.
        self.assertNotIn('2:20',eligible_accounts(base['graph']))
        first=self.expand(base);second=self.expand(first);third=self.expand(second)
        self.assertIn('2:20',eligible_accounts(third['graph']))
        result=self.expand(third,'2:20')
        self.assertIn('TX-000000501',{r['txId'] for r in result['graph']['edges']})
        self.assertNotIn('TX-000000502',{r['txId'] for r in result['graph']['edges']})
        self.assert_code('account_not_expandable',lambda:self.expand(result,'3:new'))

    def test_expired_snapshot_cannot_fall_back_to_current_db(self):
        self.manager.states[self.base['reviewSnapshotId']]['expires']=0
        count=len(self.db.calls);self.assert_code('expansion_expired',lambda:self.expand())
        self.assertEqual(count,len(self.db.calls))

    def test_concurrent_expansion_has_one_winner(self):
        started=threading.Event();release=threading.Event();results=[]
        def pause(rows):started.set();release.wait(5);return rows
        self.db.modify=pause
        thread=threading.Thread(target=lambda:results.append(self.expand()));thread.start()
        try:
            self.assertTrue(started.wait(5));self.assert_code('expansion_busy',lambda:self.expand())
        finally:release.set();thread.join(5)
        self.assertEqual(len(results),1);self.assertEqual(results[0]['accountExpansion']['pagesUsed'],1)


class ExpansionHttpTest(unittest.TestCase):
    def setUp(self):
        self.db=Database();self.server=ThreadingHTTPServer(('127.0.0.1',0),viewer.handler_for(['TX-000000999'],lambda:self.db))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(5)
    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        try:
            conn.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers={'Content-Type':'application/json',**(headers or {})})
            response=conn.getresponse();return response.status,json.loads(response.read())
        finally:conn.close()
    def test_http_expand_and_caller_scope_limit_cursor_injection_rejected(self):
        status,base=self.request('/api/graph?txId=TX-000000999&hops=1');self.assertEqual(status,200)
        body={'snapshotId':base['reviewSnapshotId'],'accountKey':'1:hub'}
        for field,value in [('cursor','forged'),('limit',99999),('hops',3),('windowDays',99),('graph',{})]:
            self.assertEqual(self.request('/api/graph/expand',{**body,field:value})[0],400)
        self.assertEqual(self.request('/api/graph/expand',body,{'Origin':'http://localhost:9999'})[0],403)
        status,result=self.request('/api/graph/expand',body)
        self.assertEqual(status,200);self.assertEqual(result['accountExpansion']['pagesUsed'],1)
        self.assertEqual(self.request('/api/graph/expand',body)[1],result)
