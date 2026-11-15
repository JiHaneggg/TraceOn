import copy
import json
import unittest

import test_rag as fixtures
from corpus import canonical, digest
from retrieval import Index, section_key, select_hits
from evidence import build_context, validate_draft
from evaluate_v3 import evaluate_case, summarize, load_questions
from query_processing import expand_query, missing_capability


def chunk(name, section="A", document="doc", score=10, version="snapshot-one", jurisdiction="KR"):
    text = "검증용 합성 문서 " + name
    return {"chunkId":name,"documentId":document,"sourceSha256":version,"jurisdiction":jurisdiction,
            "sourceKind":"official_guidance","section":section,"effectiveDate":None,
            "text":text,"textSha256":digest(text),"retrievalScore":score,"queryCoverage":1}


class RetrievalV3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RagTest.setUpClass()
        cls.base = fixtures.RagTest()

    @classmethod
    def tearDownClass(cls):
        fixtures.RagTest.tearDownClass()

    def test_same_section_occupies_one_primary_slot(self):
        rows = [chunk('a',score=20),chunk('b',score=19),chunk('c',score=18),chunk('d',section='B',score=17)]
        hits,info = select_hits(rows,3)
        self.assertEqual([row['chunkId'] for row in hits],['a','d'])
        self.assertEqual(info['suppressedSiblingChunks'],2)

    def test_distinct_sections_of_one_document_survive(self):
        rows = [chunk('a',section='definition'),chunk('b',section='procedure'),chunk('c',section='exceptions')]
        self.assertEqual(len(select_hits(rows,3)[0]),3)

    def test_authority_and_jurisdiction_are_preserved(self):
        rows = [chunk('a'),chunk('b',document='second'),chunk('c',jurisdiction='CA')]
        self.assertEqual(len(select_hits(rows,3)[0]),3)

    def test_source_versions_are_not_collapsed(self):
        self.assertNotEqual(section_key(chunk('a')),section_key(chunk('b',version='new-snapshot')))
        self.assertEqual(len(select_hits([chunk('a'),chunk('b',version='new-snapshot')],3)[0]),2)

    def test_legal_validity_is_part_of_group_identity(self):
        a,b=chunk('a'),chunk('b');b['validUntil']='2025-01-01'
        self.assertNotEqual(section_key(a),section_key(b))

    def test_best_original_chunk_is_not_rewritten(self):
        rows = [chunk('a',score=20),chunk('b',score=19)]
        hit=select_hits(rows,3)[0][0]
        self.assertEqual(hit['text'],rows[0]['text'])
        self.assertEqual(hit['textSha256'],digest(hit['text']))
        self.assertEqual(hit['chunkId'],'a')

    def test_supporting_chunks_are_bounded_and_individually_citable(self):
        rows=[chunk(str(i),score=20-i) for i in range(8)]
        hit=select_hits(rows,3)[0][0]
        self.assertEqual([row['chunkId'] for row in hit['supportingChunks']],['1','2'])
        self.assertEqual(hit['omittedSiblingCount'],5)

    def test_historical_chunk_policy_reproduces_top_k(self):
        rows=[chunk(str(i),score=20-i) for i in range(4)]
        hits,info=select_hits(rows,3,'chunks')
        self.assertEqual([row['chunkId'] for row in hits],['0','1','2'])
        self.assertTrue(all(not row['supportingChunks'] for row in hits))
        self.assertEqual(info['suppressedSiblingChunks'],0)

    def test_no_padding_with_irrelevant_documents(self):
        hits,_=select_hits([chunk('a'),chunk('b')],8)
        self.assertEqual(len(hits),1)

    def test_selector_does_not_mutate_candidates(self):
        rows=[chunk('a'),chunk('b')];before=canonical(rows)
        select_hits(rows,3)
        self.assertEqual(canonical(rows),before)

    def paired_indexes(self):
        payload=json.loads(self.base.path.read_text(encoding='utf-8'));payload.pop('corpusSha256')
        sibling=copy.deepcopy(payload['chunks'][0]);sibling['chunkId']+='-supplement'
        sibling['text']+=' 합당한 근거 보고책임자 의심거래보고 자료 종류'
        sibling['textSha256']=digest(sibling['text']);payload['chunks'].append(sibling)
        payload['corpusSha256']=digest(canonical(payload))
        path=self.base.directory/'group-fixture.json';path.write_text(json.dumps(payload),encoding='utf-8')
        return Index(path,selection_policy='chunks'),Index(path)

    def test_bm25_scores_filters_and_leading_chunk_stay_equal(self):
        old,new=self.paired_indexes()
        a=old.search('의심거래보고 합당한 근거 보고책임자',top_k=8)
        b=new.search('의심거래보고 합당한 근거 보고책임자',top_k=8)
        self.assertEqual(a['hits'][0]['chunkId'],b['hits'][0]['chunkId'])
        before={row['chunkId']:row['retrievalScore'] for row in a['hits']}
        for row in b['hits']:
            self.assertEqual(before[row['chunkId']],row['retrievalScore'])
        self.assertEqual(a['filters'],b['filters'])
        self.assertEqual(len({section_key(row) for row in b['hits']}),len(b['hits']))

    def test_supporting_citations_remain_valid_in_report_context(self):
        _,index=self.paired_indexes()
        context=build_context(index,self.base.graph())
        self.assertEqual(len(context['citations']),len({row['chunkId'] for row in context['citations']}))
        self.assertTrue(any(row['citationRole']=='supporting' for row in context['citations']))
        for row in context['citations']:
            self.assertEqual(row['text'],index.by_id[row['chunkId']]['text'])
            self.assertEqual(row['textSha256'],digest(row['text']))
            self.assertNotIn('supportingChunks',row)
        citation=next(row for row in context['citations'] if row['citationRole']=='supporting')
        draft=self.base.draft(context);draft['citations']=[{'chunkId':citation['chunkId'],'quote':citation['text'][:30]}]
        self.assertTrue(validate_draft(context,draft)['valid'])

    def test_invalid_policy_rejected(self):
        with self.assertRaises(ValueError):Index(self.base.path,selection_policy='random')

    def test_evaluation_distinguishes_primary_and_supporting_evidence(self):
        a,b=chunk('a'),chunk('b');a['text']='첫 번째 사실';b['text']='두 번째 사실'
        class Stub:
            def search(self,*args,**kwargs):return {'hits':[{**a,'supportingChunks':[b]}],'status':'matched','answerability':'not_assessed'}
        case={'id':'test','query':'질문','requiredEvidence':[{'sectionId':'x','quote':'첫 번째 사실'},{'sectionId':'x','quote':'두 번째 사실'}]}
        row=evaluate_case(Stub(),case,{'x':{'documentId':'doc','section':'A'}})
        self.assertEqual(row['sectionRecallAt3'],1)
        self.assertEqual(row['primaryEvidenceRecallAt3'],0.5)
        self.assertEqual(row['contextEvidenceRecall'],1)
        self.assertEqual(row['expandedChunks'],2)

    def test_evaluation_does_not_count_section_match_as_full_evidence(self):
        class Stub:
            def search(self,*args,**kwargs):return {'hits':[chunk('a')],'status':'matched','answerability':'not_assessed'}
        case={'id':'test','query':'질문','requiredEvidence':[{'sectionId':'x','quote':'존재하지 않는 근거'}]}
        row=evaluate_case(Stub(),case,{'x':{'documentId':'doc','section':'A'}})
        self.assertEqual(row['sectionRecallAt3'],1)
        self.assertEqual(row['contextEvidenceRecall'],0)

    def test_unsupported_related_passage_is_a_failure_not_abstention(self):
        class Stub:
            def search(self,*args,**kwargs):return {'hits':[chunk('a')],'status':'matched','answerability':'not_assessed'}
        row=evaluate_case(Stub(),{'id':'none','query':'고객의 실제 직업','requiredEvidence':[]},{})
        self.assertFalse(row['abstained']);self.assertTrue(row['unverifiedRelatedPassages'])
        self.assertEqual(summarize([row])['unsupportedAbstentionRate'],0)

    def test_invalid_gold_anchor_fails_before_evaluation(self):
        index=self.base.index
        payload={'corpusSha256':index.corpus_hash,'sections':{'x':{'documentId':'str','section':index.chunks[0]['section']}},
                 'cases':[{'id':'bad','split':'development','requiredEvidence':[{'sectionId':'x','quote':'만든 조항 제999조'}]}]}
        path=self.base.directory/'bad-gold.json';path.write_text(json.dumps(payload),encoding='utf-8')
        with self.assertRaises(ValueError):load_questions(index,path)

    def test_missing_customer_and_pricing_sources_abstain(self):
        for query in ['김철수 고객의 직업과 주소를 알려주세요',
                      'TX-123 송금인의 실제 소유자 이름을 알려주세요',
                      '고객확인 자료를 내면 대출 금리가 할인되나요']:
            self.assertEqual(self.base.index.search(query)['status'],'insufficient_scope')

    def test_long_internal_score_policy_request_abstains(self):
        self.assertEqual(self.base.index.search('KB 내부 AML 스코어별 의심거래 승인 규정을 알려주세요')['status'],'insufficient_scope')

    def test_general_customer_guidance_is_not_personal_lookup(self):
        for query in ['법인 고객의 실제 소유자를 확인하는 절차',
                      '신규 고객의 주소와 직업을 확인하는 이유를 알려주세요',
                      '기존 고객이 정보를 제공하지 않으면 거래를 종료하나요']:
            self.assertIsNone(missing_capability(query))

    def test_query_expansion_does_not_change_source_facts(self):
        value=expand_query('단일 징후와 사실과 맥락을 함께 보는 이유')
        self.assertIn('single indicator one activity',value)
        self.assertIn('facts context assessment',value)

    def test_english_inflections_share_search_terms(self):
        from retrieval import tokenize
        self.assertEqual(set(tokenize('accounts funds activities')),set(tokenize('account fund activity')))

    def test_korean_foreign_query_preserves_jurisdiction(self):
        payload=json.loads(self.base.path.read_text(encoding='utf-8'));payload.pop('corpusSha256')
        row=payload['chunks'][0]
        row.update({'documentId':'foreign-fixture','chunkId':'foreign-fixture:1','title':'Funnel accounts',
                    'section':'Funnel Account','jurisdiction':'US','sourceKind':'foreign_typology',
                    'text':'Funnel account receives multiple cash deposits and withdraws with little time elapsing.'})
        row['textSha256']=digest(row['text']);payload['chunks']=[row];payload['corpusSha256']=digest(canonical(payload))
        path=self.base.directory/'foreign-fixture.json';path.write_text(json.dumps(payload),encoding='utf-8')
        index=Index(path)
        self.assertEqual(index.search('집금 funnel account는 입금과 출금 사이 시간이 짧은 계좌인가요',jurisdiction='ALL',source_kind='foreign_typology')['status'],'matched')
        self.assertEqual(index.search('집금 funnel account',jurisdiction='KR')['hits'],[])


if __name__=='__main__':unittest.main()
