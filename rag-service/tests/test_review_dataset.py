"""Synthetic review gate checks; no mentor draft searches are performed."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from corpus import digest
from review_dataset import check_review_pack


class ReviewDatasetTest(unittest.TestCase):
    def setUp(self):
        row={'chunkId':'fixture-1','documentId':'fixture','section':'합성 구간','sourceSha256':'a'*64,
             'textSha256':digest('합성 근거 문장'),'url':'https://www.kofiu.go.kr/fixture',
             'jurisdiction':'KR','sourceKind':'official_guidance','text':'합성 근거 문장'}
        self.index=SimpleNamespace(corpus_hash='b'*64,by_id={row['chunkId']:row})
        ref={k:v for k,v in row.items() if k!='text'};ref.update(quote=row['text'],quoteSha256=digest(row['text']))
        self.pack={'schemaVersion':'1.0','kind':'mentor_review_draft_not_evaluation_results','searchExecuted':False,
                   'corpusSha256':self.index.corpus_hash,'cases':[{'id':'fixture-question','request':{'query':'합성 근거 질문'},
                   'proposedExpected':'retrieve','anchors':[ref],
                   'review':{'decision':'pending','reviewer':None,'reviewedAt':None}}]}

    def test_pending_draft_is_valid_but_not_evaluated(self):
        result=check_review_pack(self.pack,self.index)
        self.assertFalse(result['readyForSeparateFreeze'])
        self.assertFalse(result['performanceMeasured'])

    def test_pending_review_cannot_pass_gate(self):
        with self.assertRaises(ValueError):check_review_pack(self.pack,self.index,True)

    def test_approved_case_requires_reviewer_and_date(self):
        review=self.pack['cases'][0]['review'];review['decision']='approve'
        with self.assertRaises(ValueError):check_review_pack(self.pack,self.index,True)
        review.update(reviewer='합성 검토자',reviewedAt='2026-11-15')
        self.assertTrue(check_review_pack(self.pack,self.index,True)['readyForSeparateFreeze'])

    def test_tampered_quote_rejected(self):
        self.pack['cases'][0]['anchors'][0]['quote']='원문에 없는 근거'
        with self.assertRaises(ValueError):check_review_pack(self.pack,self.index)

    def test_changed_corpus_requires_rebinding(self):
        self.pack['corpusSha256']='c'*64
        with self.assertRaises(ValueError):check_review_pack(self.pack,self.index)

    def test_anchor_cannot_cross_requested_jurisdiction(self):
        self.pack['cases'][0]['request']['jurisdiction']='CA'
        with self.assertRaises(ValueError):check_review_pack(self.pack,self.index)
