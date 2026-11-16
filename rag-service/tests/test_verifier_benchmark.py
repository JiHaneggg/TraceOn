"""Synthetic integrity metrics and benchmark lifecycle; no real gold execution."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT.parent/'data-pipeline')]
import test_domestic_sources as fixtures
from corpus import digest
from evidence import build_context
from reports import generate_report
from report_metrics import audit_report
from report_verification import verify_report
from evidence_lookup import EvidenceRepository
from verifier_benchmark import confusion,freeze,inject,make_cases,rehash,summarize,verify_manifest


class VerifierBenchmarkTest(unittest.TestCase):
    def setUp(self):
        fixtures.DomesticSourcesTest.setUp(self)
        self.graph=fixtures.DomesticSourcesTest.graph(self)
        self.context=build_context(self.index,self.graph)
        self.report=generate_report(self.context)
        self.repo=EvidenceRepository(self.index,self.raw)

    def audit(self,report):return audit_report(self.context,report,self.report['markdown'])

    def test_clean_report_has_full_coverage_and_exact_quotes(self):
        result=self.audit(self.report)
        self.assertEqual(result['numericFacts']['matchRate'],1)
        self.assertEqual(result['numericFacts']['tableCoverage'],1)
        self.assertEqual(result['citations']['groundingPrecision'],1)

    def test_one_unit_one_second_and_account_changes_are_rejected(self):
        for kind in ['amount_table','amount_narrative','timestamp_table','account_table']:
            with self.subTest(kind=kind):
                report=inject(self.report,self.context,kind)
                self.assertLess(self.audit(report)['numericFacts']['matchRate'],1)
                self.assertFalse(verify_report(self.repo,self.graph,None,report)['valid'])
                self.assertTrue(verify_report(self.repo,self.graph,None,report)['checks']['reportDigest']['valid'])

    def test_ghost_article_in_body_is_not_hidden_by_valid_chunk_quotes(self):
        report=inject(self.report,self.context,'ghost_article');result=self.audit(report)
        self.assertEqual(result['citations']['groundingPrecision'],1)
        self.assertLess(result['articleTags']['presencePrecision'],1)
        self.assertFalse(verify_report(self.repo,self.graph,None,report)['valid'])

    def test_categorical_claim_is_contract_reject_not_semantic_score(self):
        report=inject(self.report,self.context,'categorical_crime_contract')
        self.assertFalse(verify_report(self.repo,self.graph,None,report)['valid'])
        self.assertFalse(self.audit(report)['semanticReview']['measured'])

    def test_missing_numeric_fact_cannot_hide_behind_remaining_match_rate(self):
        report=deepcopy(self.report)
        line=next(s for s in report['markdown'].splitlines() if s.startswith('| target.amountPaid |'))
        report['markdown']=report['markdown'].replace(line+'\n','')
        metrics=self.audit(report)['numericFacts']
        self.assertLess(metrics['tableCoverage'],1)
        self.assertIn('target.amountPaid',metrics['missingFactIds'])

    def test_duplicate_numeric_claims_are_not_extra_correct_samples(self):
        report=deepcopy(self.report)
        line=next(s for s in report['markdown'].splitlines() if s.startswith('| target.amountPaid |'))
        report['markdown']+='\n'+line+'\n'
        metrics=self.audit(report)['numericFacts']
        self.assertEqual(metrics['duplicateTableClaims'],1)
        self.assertLess(metrics['matchRate'],1)

    def test_empty_report_has_no_successful_denominator(self):
        metrics=audit_report(self.context,{'markdown':'','draft':{'citations':[]}})
        self.assertIsNone(metrics['numericFacts']['matchRate'])
        self.assertIsNone(metrics['citations']['groundingPrecision'])
        self.assertEqual(metrics['numericFacts']['tableCoverage'],0)

    def test_quote_from_an_unlisted_chunk_does_not_ground(self):
        report=deepcopy(self.report)
        report['draft']['citations'][0]['chunkId']='unknown-chunk'
        self.assertLess(self.audit(report)['citations']['groundingPrecision'],1)

    def test_reject_is_positive_class_and_f1_is_computed_from_counts(self):
        rows=[{'expectedVerdict':gold,'predictedVerdict':pred} for gold,pred in
              [('REJECT','REJECT'),('REJECT','REJECT'),('PASS','REJECT'),('REJECT','PASS'),
               ('PASS','PASS'),('PASS','PASS'),('PASS','PASS')]]
        result=confusion(rows)
        self.assertEqual([result[k] for k in ['TP','FP','TN','FN']],[2,1,3,1])
        self.assertAlmostEqual(result['rejectF1'],2/3)
        self.assertAlmostEqual(result['passPrecision'],3/4)

    def test_empty_summary_does_not_claim_latency_or_retry_success(self):
        result=summarize([])
        self.assertFalse(result['allMeasuredStructuralTargetsMet'])
        self.assertIsNone(result['latencySeconds']['mean'])
        self.assertIsNone(result['meanRetryCount'])
        self.assertFalse(result['retryLoopMeasured'])

    def test_balanced_pairs_keep_fault_metadata_out_of_report(self):
        cases=make_cases(self.index,[self.graph],50)
        self.assertEqual(sum(c['expectedVerdict']=='PASS' for c in cases),25)
        self.assertEqual(sum(c['expectedVerdict']=='REJECT' for c in cases),25)
        self.assertTrue(all('faultKind' not in c['report'] for c in cases))
        self.assertTrue(all(c['humanSemanticLabel'] is None for c in cases))

    def test_frozen_runner_executes_once_and_detects_mutation(self):
        folder=self.root/'benchmark'
        freeze(ROOT.parent,self.path,[self.graph],folder,50)
        manifest=folder/'manifest.json'
        command=[sys.executable,str(folder/'engine/rag-service/verifier_benchmark.py'),'run','--manifest',str(manifest)]
        first=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(first.returncode,0,first.stderr)
        result=json.loads((folder/'results.json').read_bytes())
        self.assertEqual(result['summary']['confusionMatrix']['FN'],0)
        self.assertEqual(result['summary']['confusionMatrix']['FP'],0)
        self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
        with (folder/'dataset.json').open('ab') as stream:stream.write(b' ')
        with self.assertRaises(ValueError):verify_manifest(manifest)
