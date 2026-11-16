"""Read-only API, exact amounts, scope limits and secret-free responses."""
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

VIEWER=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('traceon_viewer_server',VIEWER/'server.py')
viewer=importlib.util.module_from_spec(spec);spec.loader.exec_module(viewer)
from make_demo import generate
from graph_contract import MemoryGraph


class FakeDatabase:
    def __init__(self):
        graph=next(case['graph'] for case in generate()[1] if case['scenario']=='cycle')
        self.memory=MemoryGraph(graph['edges']);self.target=graph['targetTxId']
    def query(self,statement,parameters=None):
        if statement==viewer.HEALTH_QUERY:return [{'connected':1}]
        if statement==viewer.LIST_QUERY:return [{'edge':self.memory.transactions[tx]} for tx in parameters['ids']]
        raise ValueError('Unexpected read')
    def subgraph(self,tx_id,**kwargs):
        result=self.memory.subgraph(tx_id,**kwargs)
        result['meta'].update(sourceGraphComplete=False,sourceScope='synthetic-fixture')
        return result


class ViewerTest(unittest.TestCase):
    def setUp(self):self.database=FakeDatabase();self.targets=[self.database.target]

    def request(self,path,*,method='GET',headers=None,database=None):
        server=ThreadingHTTPServer(('127.0.0.1',0),viewer.handler_for(self.targets,lambda:database or self.database))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connection=http.client.HTTPConnection(*server.server_address,timeout=5)
        try:
            connection.request(method,path,headers=headers or {})
            response=connection.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:connection.close();server.shutdown();server.server_close();thread.join(5)

    def test_graph_preserves_transaction_ids_direction_and_decimal_strings(self):
        p=viewer.graph_payload(self.database,self.targets[0])
        source=self.database.memory.subgraph(self.targets[0])
        self.assertEqual(p['graph']['edges'],source['edges'])
        self.assertTrue(set(p['highlightTxIds'])<={e['txId'] for e in p['graph']['edges']})
        self.assertTrue(p['source']['readOnly'])
        self.assertFalse(p['source']['ruleScoreIsProbability'])
        self.assertFalse(p['source']['sourceGraphComplete'])
        self.assertNotIn('isLaundering',json.dumps(p))

    def test_totals_preserve_large_and_fractional_values_and_separate_currencies(self):
        rows=[{'paymentCurrency':'A','receivingCurrency':'B','amountPaid':'99999999999999999999.00000001','amountReceived':'0.10'},
              {'paymentCurrency':'A','receivingCurrency':'B','amountPaid':'0.00000002','amountReceived':'0.20'}]
        result=viewer.currency_totals(rows)
        self.assertEqual(result['grossPaid']['A'],'99999999999999999999.00000003')
        self.assertEqual(result['grossReceived']['B'],'0.30')
        self.assertNotIn('B',result['grossPaid'])

    def test_range_and_target_allowlist_are_mandatory(self):
        target=self.targets[0]
        self.assertEqual(viewer.parse_graph_request('txId='+target,self.targets),(target,2,7))
        for query in ['txId=unknown',f'txId={target}&hops=4',f'txId={target}&windowDays=99',
                      f'txId={target}&txId={target}',f'txId={target}&cypher=MATCH',f'txId={target}&maxEdges=999999']:
            with self.subTest(query=query),self.assertRaises(ValueError):viewer.parse_graph_request(query,self.targets)

    def test_db_adapter_cannot_issue_writes(self):
        database=viewer.ReadOnlyDatabase.__new__(viewer.ReadOnlyDatabase)
        for query in ['MATCH (n) DELETE n','CREATE (a:Account)','RETURN $credential AS password']:
            with self.assertRaises(ValueError):database.query(query)

    def test_manifest_returns_no_local_paths_or_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            valid=['TX-000000001']
            path=Path(directory)/'manifest.json';path.write_text(json.dumps({'targets':valid,'source':'private-local-path','label':'not-public'}))
            self.assertEqual(viewer.read_targets(path),valid)
            path.write_text(json.dumps({'targets':valid*2}))
            with self.assertRaises(ValueError):viewer.read_targets(path)

    def test_cases_and_live_graph_http_contract(self):
        status,headers,raw=self.request('/api/cases')
        self.assertEqual(status,200);data=json.loads(raw)
        self.assertEqual(data['items'][0]['txId'],self.targets[0]);self.assertEqual(data['source'],'neo4j')
        self.assertNotIn('Authorization',raw.decode());self.assertEqual(headers['Cache-Control'],'no-store')
        status,_,raw=self.request('/api/graph?txId='+self.targets[0]+'&hops=1&windowDays=1')
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)['graph']['meta']['hops'],1)

    def test_missing_or_duplicate_db_transactions_fail_closed(self):
        class Incomplete(FakeDatabase):
            def query(self,*args,**kwargs):return []
        class Duplicate(FakeDatabase):
            def query(self,*args,**kwargs):return super().query(*args,**kwargs)*2
        for database in [Incomplete(),Duplicate()]:
            status,_,_=self.request('/api/cases',database=database)
            self.assertEqual(status,503)

    def test_database_errors_never_echo_credentials(self):
        class Failing:
            def query(self,*args,**kwargs):raise RuntimeError('credential-sentinel-do-not-expose')
        status,_,raw=self.request('/api/cases',database=Failing())
        self.assertEqual(status,503);self.assertNotIn('credential-sentinel',raw.decode())

    def test_static_allowlist_excludes_environment_and_arbitrary_paths(self):
        for path in ['/.env','/../.env','/.git/config','/server.py','/vendor/manifest.json','/api/graph?txId=no']:
            with self.subTest(path=path):self.assertIn(self.request(path)[0],{400,404})
        status,headers,_=self.request('/')
        self.assertEqual(status,200);self.assertIn("connect-src 'self'",headers['Content-Security-Policy'])

    def test_flow_module_is_served_only_as_the_explicit_static_asset(self):
        status,headers,raw=self.request('/flow.mjs')
        self.assertEqual(status,200)
        self.assertIn('text/javascript',headers['Content-Type'])
        self.assertIn(b'export function buildFlow',raw)
        self.assertEqual(self.request('/tests/flow.test.mjs')[0],404)

    def test_write_method_and_foreign_hosts_are_rejected(self):
        self.assertEqual(self.request('/api/graph',method='POST')[0],405)
        self.assertEqual(self.request('/api/cases',headers={'Host':'untrusted.invalid:8093'})[0],421)
        self.assertEqual(self.request('/api/cases',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
