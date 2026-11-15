"""Evaluation integrity tests on synthetic data, never the new 60-question set."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT.parent/'data-pipeline')]
import test_domestic_sources as fixtures
from corpus import canonical,digest
from evaluate_frozen import (freeze, hashes, load_questions, novelty_audit, production_files,
                             evaluate_case, summarize, verify_bundle, write_once, run)


class FrozenEvaluationTest(unittest.TestCase):
    def setUp(self):
        fixtures.DomesticSourcesTest.setUp(self)
        self.repo=ROOT.parent
        self.questions=self.root/'new-questions.json'
        self.data={'schemaVersion':'4.0','kind':'new_questions_frozen_before_execution',
            'corpusSha256':self.index.corpus_hash,'topK':3,'annotationStatus':'developer_authored_not_domain_adjudicated',
            'evidenceUnits':{'grounds':[{'documentId':'act','section':'제4조(의심거래 보고)','quote':'합당한 근거'},
                                      {'documentId':'guide','section':'의심거래보고 정의','quote':'합당한 근거'}]},
            'cases':[{'id':'TEST-P','category':'synthetic','request':{'query':'합성 테스트 전용 의심거래보고 근거 확인'},
                      'expected':'retrieve','requiredUnits':['grounds'],'annotationReason':'Synthetic mechanics only.'},
                     {'id':'TEST-N','category':'synthetic','request':{'query':'아주먼 은하 소행성 합성 알파베타 특수기호'},
                      'expected':'abstain','requiredUnits':[],'annotationReason':'Synthetic absent topic.'}]}
        self.save_questions()
        self.baseline=self.root/'baseline.json'
        write_once(self.baseline,{'implementationSha256':hashes(self.repo,production_files(self.repo)),
                                 'corpusSha256':self.index.corpus_hash,'indexFileSha256':digest(self.path.read_bytes())})
        self.run_dir=self.root/'run'

    def save_questions(self):
        self.questions.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf-8')

    def freeze(self):
        with patch('evaluate_frozen.prior_questions',return_value=[]):
            return freeze(self.repo,self.path,self.questions,self.run_dir,self.baseline)

    def test_freeze_validates_without_search_and_preserves_exact_snapshots(self):
        with patch('retrieval.Index.search',side_effect=AssertionError('freeze must not search')):
            manifest=self.freeze()
        self.assertEqual((self.run_dir/'questions.json').read_bytes(),self.questions.read_bytes())
        self.assertEqual((self.run_dir/'corpus.json').read_bytes(),self.path.read_bytes())
        self.assertEqual(verify_bundle(self.run_dir/'manifest.json')['manifestSha256'],manifest['manifestSha256'])

    def test_changed_pre_question_production_baseline_is_rejected(self):
        data=json.loads(self.baseline.read_text(encoding='utf-8'))
        data['implementationSha256']['rag-service/retrieval.py']='0'*64
        self.baseline.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Production code or corpus changed'):self.freeze()

    def test_missing_anchor_and_inconsistent_scope_are_rejected_before_execution(self):
        self.data['evidenceUnits']['grounds'][0]['quote']='not in any source'
        self.save_questions()
        with self.assertRaisesRegex(ValueError,'Gold anchor absent'):load_questions(self.index,self.questions)
        self.data['evidenceUnits']['grounds'][0]['quote']='합당한 근거'
        self.data['cases'][0]['request']['jurisdiction']='US'
        self.save_questions()
        with self.assertRaisesRegex(ValueError,'outside the requested source scope'):load_questions(self.index,self.questions)

    def test_duplicate_questions_and_close_paraphrases_are_audited(self):
        cases=copy.deepcopy(self.data['cases'])
        cases[1]['request']['query']=cases[0]['request']['query']+'!!'
        self.assertTrue(novelty_audit(cases,[])['exactDuplicates'])
        previous=[{'query':'긴문장의 의심거래보고 합당한 근거 확인 질문 하나','source':'prior'}]
        candidate=[{'id':'new','request':{'query':'긴문장의 의심거래보고 합당한 근거 확인 질문 둘'}}]
        self.assertTrue(novelty_audit(candidate,previous)['nearDuplicates'])

    def test_tampered_snapshot_code_and_unlisted_module_are_rejected(self):
        self.freeze();manifest_path=self.run_dir/'manifest.json'
        questions=self.run_dir/'questions.json';original=questions.read_bytes()
        questions.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'corpus/questions changed'):verify_bundle(manifest_path)
        questions.write_bytes(original)
        module=self.run_dir/'implementation/rag-service/retrieval.py';original=module.read_bytes()
        module.write_bytes(original+b'\n# changed\n')
        with self.assertRaisesRegex(ValueError,'implementation changed'):verify_bundle(manifest_path)
        module.write_bytes(original)
        (module.parent/'unlisted.py').write_text('pass',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'file inventory changed'):verify_bundle(manifest_path)

    def test_existing_freeze_or_results_cannot_be_overwritten(self):
        self.freeze();manifest=self.run_dir/'manifest.json';before=manifest.read_bytes()
        with self.assertRaisesRegex(ValueError,'Existing evaluation preserved'):self.freeze()
        self.assertEqual(manifest.read_bytes(),before)
        output=self.root/'result.json';write_once(output,{'original':True})
        with self.assertRaises(FileExistsError):write_once(output,{'original':False})

    def test_live_runner_cannot_claim_to_be_frozen_execution(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError,'copied runner'):run(self.run_dir/'manifest.json')
        self.assertFalse((self.run_dir/'execution.json').exists())

    def test_copied_runner_executes_once_and_second_attempt_is_refused(self):
        self.freeze();runner=self.run_dir/'implementation/rag-service/evaluate_frozen.py'
        command=[sys.executable,str(runner),'run','--manifest',str(self.run_dir/'manifest.json')]
        first=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(first.returncode,0,first.stderr)
        results=json.loads((self.run_dir/'results.json').read_text(encoding='utf-8'))
        self.assertEqual(results['summary']['queries'],2)
        self.assertFalse(results['legalOrSemanticAccuracyMeasured'])
        before=(self.run_dir/'results.json').read_bytes()
        second=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
        self.assertNotEqual(second.returncode,0)
        self.assertEqual((self.run_dir/'results.json').read_bytes(),before)

    def test_primary_and_supporting_anchor_coverage_are_separate(self):
        primary={'chunkId':'head','documentId':'act','section':'제4조(의심거래 보고)',
                 'sourceSha256':'source','jurisdiction':'KR','sourceKind':'law','effectiveDate':None,
                 'text':'제목만 있음','retrievalScore':1.0,'supportingChunks':[
                    {'chunkId':'tail','documentId':'act','section':'제4조(의심거래 보고)','text':'합당한 근거'}]}
        class SyntheticSearch:
            def search(self,*args,**kwargs):return {'hits':[primary],'status':'matched','answerability':'not_assessed'}
        row=evaluate_case(SyntheticSearch(),self.data['cases'][0],self.data['evidenceUnits'])
        self.assertEqual(row['sectionRecallAt3'],1)
        self.assertEqual(row['primaryAnchorRecallAt3'],0)
        self.assertEqual(row['contextAnchorRecall'],1)
        self.assertTrue(row['expectationPassed'])
        self.assertEqual(row['expandedChunks'],2)

    def test_related_passages_are_an_abstention_failure_without_claiming_an_answer(self):
        row={'chunkId':'related','documentId':'guide','section':'의심거래보고 정의','text':'안내',
             'retrievalScore':1.0,'supportingChunks':[]}
        class SyntheticSearch:
            def search(self,*args,**kwargs):return {'hits':[row],'status':'matched','answerability':'not_assessed'}
        result=evaluate_case(SyntheticSearch(),self.data['cases'][1],self.data['evidenceUnits'])
        self.assertFalse(result['expectationPassed'])
        self.assertEqual(result['answerability'],'not_assessed')
        summary=summarize([result])
        self.assertEqual(summary['unsupportedAbstentionCount'],0)
        self.assertEqual(summary['unsupportedRelatedPassageCount'],1)
        self.assertIsNone(summary['macroSectionRecallAt3'])


if __name__=='__main__':unittest.main()
