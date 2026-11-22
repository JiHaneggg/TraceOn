"""Independent path/coverage fixtures; no Neo4j, real data or paid API."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch,MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from graph_contract import MemoryGraph
from graph_diagnostics import (AuditDatabase,ALL_ROWS,TARGET_QUERY,NEIGHBOR_QUERY,
    audit_graph,fingerprint,latency_summary,plan_summary,source_map,uncapped_reference)


def edge(id,source,target,time='2022-09-01T11:00:00',amount='0.10'):
    return {'txId':id,'fromAccountKey':source,'toAccountKey':target,'timestamp':time,
            'amountPaid':amount,'paymentCurrency':'US Dollar','amountReceived':amount,
            'receivingCurrency':'US Dollar','paymentFormat':'ACH'}


class GraphDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.rows=[edge('target','S','T','2022-09-01T12:00:00'),edge('left','S','A'),
                   edge('right','T','Z'),edge('hop2','A','B'),edge('hop3','B','C'),
                   edge('future','S','F','2022-09-01T13:00:00'),edge('old','S','O','2022-08-20T11:00:00'),
                   edge('self','A','A'),edge('parallel','S','A',amount='9007199254740993.00000001')]
        self.source=source_map(self.rows);self.memory=MemoryGraph(self.rows)
    def test_independent_bfs_uses_both_target_endpoints_and_exact_hops(self):
        expected={1:{'target','left','right','parallel'},2:{'target','left','right','parallel','hop2'},3:{'target','left','right','parallel','hop2','hop3'}}
        for hops in [1,2,3]:
            graph=self.memory.subgraph('target',hops=hops)
            reference=uncapped_reference(graph,self.source)
            self.assertEqual({row['txId'] for row in reference['edges']},expected[hops])
    def test_full_small_view_is_exact_without_claiming_ledger_completeness(self):
        graph=self.memory.subgraph('target');result,reference=audit_graph(graph,self.source,graph)
        self.assertTrue(result['valid']);self.assertEqual(result['coverageWithinLoadedSubset'],1)
        self.assertFalse(reference['meta']['sourceGraphComplete'])
    def test_neighbor_cap_reports_unique_missing_edges_without_declaring_bad_source(self):
        graph=self.memory.subgraph('target',max_neighbors=1)
        result,_=audit_graph(graph,self.source,graph)
        self.assertTrue(result['valid']);self.assertEqual(result['returnedTransactions'],1)
        self.assertEqual(result['omittedLoadedTransactions'],4);self.assertTrue(result['limitHit'])
    def test_edge_cap_preserves_target_and_reports_omission(self):
        graph=self.memory.subgraph('target',max_edges=1)
        result,_=audit_graph(graph,self.source,graph)
        self.assertTrue(result['valid']);self.assertTrue(result['checks']['targetRetained']);self.assertGreater(result['omittedLoadedTransactions'],0)
    def test_undeclared_omission_is_detected_even_if_bounded_reference_shares_the_flag(self):
        graph=self.memory.subgraph('target',max_neighbors=1);graph['meta']['truncated']=False
        result,_=audit_graph(graph,self.source,graph)
        self.assertFalse(result['valid']);self.assertFalse(result['checks']['omissionDeclared'])
    def test_exact_amount_change_is_not_hidden_by_float_rounding(self):
        graph=self.memory.subgraph('target');original=deepcopy(graph)
        next(row for row in graph['edges'] if row['txId']=='parallel')['amountPaid']='9007199254740993.00000002'
        result,_=audit_graph(graph,self.source,original)
        self.assertFalse(result['checks']['sourceValuesExact'])
    def test_future_edges_and_forged_nodes_are_rejected(self):
        graph=self.memory.subgraph('target');original=deepcopy(graph)
        graph['edges'].append(self.source['future']);graph['nodes'].append({'accountKey':'invented'})
        result,_=audit_graph(graph,self.source,original)
        self.assertFalse(result['checks']['timeWindowExact']);self.assertFalse(result['checks']['nodesMatchEdges']);self.assertFalse(result['checks']['withinRequestedHops'])
    def test_self_transfer_target_is_kept_but_unrelated_self_transfer_is_not_added(self):
        graph=self.memory.subgraph('self',hops=1)
        reference=uncapped_reference(graph,self.source)
        self.assertIn('self',{row['txId'] for row in reference['edges']})
        self.assertNotIn('self',{row['txId'] for row in uncapped_reference(self.memory.subgraph('target'),self.source)['edges']})
    def test_duplicate_source_ids_and_empty_sources_fail(self):
        for rows in [[],self.rows+[self.rows[0]]]:
            with self.assertRaises(ValueError):source_map(rows)
    def test_fingerprint_ignores_order_but_detects_exact_source_changes(self):
        self.assertEqual(fingerprint(self.rows),fingerprint(list(reversed(self.rows))))
        altered=deepcopy(self.rows);altered[0]['amountPaid']='0.11'
        self.assertNotEqual(fingerprint(self.rows),fingerprint(altered))
    def test_small_sample_percentiles_are_nearest_rank_and_not_a_service_sla(self):
        summary=latency_summary([100,10,50]);self.assertEqual(summary['p50Ms'],50);self.assertEqual(summary['p95Ms'],100)
        for value in [[],[float('nan')],[-1],[True]]:
            with self.assertRaises(ValueError):latency_summary(value)
    def test_profile_stats_keep_missing_db_hits_unknown(self):
        block={'plan':{'root':{'operatorType':'ProduceResults@neo4j','DbHits':1,'Rows':1,'children':[{'operatorType':'DirectedRelationshipIndexSeek@neo4j','DbHits':3,'Rows':1,'children':[]}]}}}
        result=plan_summary(block);self.assertTrue(result['usesIndexSeek']);self.assertEqual(result['totalDbHits'],4)
        del block['plan']['root']['DbHits'];self.assertIsNone(plan_summary(block)['totalDbHits'])
        with self.assertRaises(ValueError):plan_summary({})
    def test_arbitrary_write_and_unbounded_profiles_are_never_sent(self):
        db=AuditDatabase.__new__(AuditDatabase);db.begin()
        with patch('graph_diagnostics.urlopen') as transport:
            for query in ['CREATE (n)','MATCH (n) DELETE n','PROFILE '+TARGET_QUERY]:
                with self.assertRaises(ValueError):db.block(query)
            with self.assertRaises(ValueError):db.block(ALL_ROWS,{'limit':20002})
            with self.assertRaises(ValueError):db.block(NEIGHBOR_QUERY,{'limit':999999})
            transport.assert_not_called()
    def test_expired_read_budget_does_not_start_a_network_call(self):
        db=AuditDatabase.__new__(AuditDatabase);db.begin();db.deadline=0
        with patch('graph_diagnostics.urlopen') as transport:
            with self.assertRaises(TimeoutError):db.block(TARGET_QUERY,{'txId':'target'})
            transport.assert_not_called()
    def test_unexpected_write_statistics_are_rejected_without_echoing_source(self):
        db=AuditDatabase.__new__(AuditDatabase);db.begin();db.url='http://127.0.0.1:7474/db/neo4j/tx/commit';db.authorization='fixture'
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'errors':[],'results':[{'columns':[],'data':[],'stats':{'contains_updates':True}}]}).encode()
        with patch('graph_diagnostics.urlopen',return_value=response):
            with self.assertRaises(RuntimeError):db.block(TARGET_QUERY,{'txId':'target'})
