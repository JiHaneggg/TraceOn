"""Synthetic source-separation mechanics, not a test of statutory truth."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / 'data-pipeline')]
from corpus import build, atomic_json, canonical, digest
from corpus_manager import CorpusManager
from retrieval import Index
from source_presentation import source_display, named_statutes
from evidence import build_context
from evidence_lookup import EvidenceRepository
from reports import generate_report
from report_verification import verify_report
from agent_tools import AgentTools
from make_demo import generate


class DomesticSourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.path = self.root / 'index.json'
        self.sources = []
        def add(name, title, kind, jurisdiction, sections, version=None):
            source = {'documentId': name, 'title': title, 'url': 'https://www.law.go.kr/' + name,
                      'publisher': 'synthetic fixture', 'sourceKind': kind, 'jurisdiction': jurisdiction,
                      'effectiveDate': '2026-10-02' if kind == 'law' else None,
                      'sections': [{'heading': heading, 'end': sections[i+1][0] if i+1 < len(sections) else '끝'}
                                   for i, (heading, text) in enumerate(sections)]}
            if version:
                source['version'] = version
                source['validUntil'] = None
            self.sources.append(source)
            html = ''.join('<h3>'+heading+'</h3><p>'+text+'</p>' for heading,text in sections)+'<h3>끝</h3>'
            (self.raw / (name+'.html')).write_text(html, encoding='utf-8')
        add('act', '특정 금융거래정보의 보고 및 이용 등에 관한 법률', 'law', 'KR', [
            ('제4조(의심거래 보고)', '의심거래보고 STR 합당한 근거 보고책임자 보고 방법 절차'),
            ('제4조의2(고액 현금거래 보고)', '고액현금거래보고 CTR 현금 지급 영수 보고'),
            ('제5조의2(고객 확인의무)', '고객확인 CDD 실제 소유자 거래 목적 자금의 원천')], '법률 합성 버전')
        add('decree', '특정 금융거래정보의 보고 및 이용 등에 관한 법률 시행령', 'law', 'KR', [
            ('제7조(보고 방법)', '의심거래보고 STR 합당한 근거 보고 방법 절차'),
            ('제8조의2(고액현금거래 보고 기준)', '고액현금거래보고 CTR 현금 보고 기준금액')], '대통령령 합성 버전')
        add('guide', '의심거래보고제도', 'official_guidance', 'KR', [
            ('의심거래보고 정의', '의심거래보고 STR 합당한 근거 보고책임자 보고 방법 절차'),
            ('고객확인 개념', '고객확인 CDD 실제 소유자 거래 목적 자금의 원천')])
        add('foreign', 'Funnel account indicators', 'foreign_typology', 'US', [
            ('Funnel indicators', '집금 funnel account multiple deposits unrelated accounts 의심거래보고 근거')])
        build(self.raw, self.path, sources=self.sources)
        self.index = Index(self.path)

    def graph(self):
        return next(c['graph'] for c in generate()[1] if c['scenario'] == 'fan_in')

    def test_explicit_article_does_not_return_branch_or_other_statute(self):
        result = self.index.search('법률 제4조 의심거래 보고 합당한 근거')
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(all(r['documentId']=='act' and r['section'].startswith('제4조(') for r in result['hits']))

    def test_decree_and_article_branch_are_selected_exactly(self):
        result = self.index.search('시행령 제8조의2 고액현금거래 보고 기준금액')
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(all(r['documentId']=='decree' and r['section'].startswith('제8조의2(') for r in result['hits']))

    def test_missing_article_other_law_and_internal_policy_abstain(self):
        for query in ['법률 제99조 의심거래 보고', '전자금융거래법 제4조 거래 보고',
                      '은행법 제4조 보고', 'KB국민은행 내부 규정 STR 승인 기준']:
            with self.subTest(query=query):
                result = self.index.search(query)
                self.assertEqual(result['status'], 'insufficient_scope')
                self.assertEqual(result['hits'], [])

    def test_unverified_historical_and_future_periods_abstain(self):
        for day in ['2022-01-01', '2026-11-15', '2027-01-01']:
            self.assertEqual(self.index.search('법률 제4조 의심거래 보고', legal_as_of=day)['status'], 'insufficient_scope')
        for query in ['2022년 법률 제4조 의심거래', '2027-01-01 시행령 제8조의2 현금 보고']:
            self.assertEqual(self.index.search(query)['status'], 'insufficient_scope')

    def test_domestic_default_and_foreign_scope_remain_separate(self):
        local = self.index.search('의심거래보고 합당한 근거', top_k=8)
        self.assertTrue(all(r['jurisdiction']=='KR' for r in local['hits']))
        foreign = self.index.search('집금 funnel account deposits', jurisdiction='ALL', source_kind='foreign_typology')
        self.assertEqual(foreign['status'], 'matched')
        self.assertTrue(all(r['sourceDisplay']['category']=='foreign_reference' for r in foreign['hits']))
        self.assertTrue(all(not r['sourceDisplay']['legalApplicabilityVerified'] for r in foreign['hits']))

    def test_labels_and_groups_cover_all_primary_hits_without_rewriting_original(self):
        before = copy.deepcopy(self.index.chunks)
        result = self.index.search('의심거래보고 STR 합당한 근거', top_k=8, jurisdiction='ALL')
        ids = [chunk for g in result['sourceGroups'] for chunk in g['chunkIds']]
        self.assertEqual(set(ids), {r['chunkId'] for r in result['hits']})
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.index.chunks, before)
        categories = {r['sourceDisplay']['category'] for r in result['hits']}
        self.assertTrue({'domestic_law','domestic_guidance','foreign_reference'} <= categories)

    def test_source_filter_conflict_is_not_silently_overridden(self):
        result = self.index.search('법률 제4조 의심거래', jurisdiction='ALL', source_kind='foreign_typology')
        self.assertEqual(result['status'], 'insufficient_scope')

    def test_named_statute_alias_does_not_treat_reporting_method_as_a_law(self):
        self.assertEqual(named_statutes('STR 보고방법 설명'), [])
        result = self.index.search('특금법 제4조 의심거래 보고')
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(all(r['documentId']=='act' for r in result['hits']))

    def test_context_report_and_lookup_expose_matching_labels(self):
        context = build_context(self.index, self.graph())
        report = generate_report(context)
        categories = {g['category'] for g in report['sourceGroups']}
        self.assertTrue({'domestic_law','foreign_reference'} <= categories)
        self.assertIn('### 국내 법령 원문', report['markdown'])
        self.assertIn('### 해외 참고 사례', report['markdown'])
        self.assertNotIn('법령 원문·시행일·은행 내규는 확보 전', report['markdown'])
        repo = EvidenceRepository(self.index)
        self.assertTrue(verify_report(repo, self.graph(), None, report)['valid'])
        for row in report['citations']:
            resolved = repo.citation(row['chunkId'], self.index.corpus_hash)
            self.assertEqual(resolved['citation']['sourceDisplay'], row['sourceDisplay'])
            self.assertEqual(resolved['sourceVerification']['status'], 'snapshot_checked')

    def test_legacy_corpus_report_keeps_original_contract_and_verifies(self):
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        payload.pop('evidencePresentationVersion')
        payload.pop('corpusSha256')
        payload['corpusSha256'] = digest(canonical(payload))
        archive = self.root/'versions'/(payload['corpusSha256']+'.json')
        atomic_json(archive, payload)
        old = Index(archive)
        context = build_context(old, self.graph())
        self.assertEqual(context['contextVersion'], 'evidence-v1')
        self.assertNotIn('sourceGroups', context)
        report = generate_report(context)
        self.assertNotIn('sourceGroups', report)
        self.assertTrue(verify_report(EvidenceRepository(self.index), self.graph(), None, report)['valid'])

    def test_agent_search_observes_new_publication(self):
        tools = AgentTools(self.index)
        manager = CorpusManager(self.path)
        manager.init(self.sources)
        source = copy.deepcopy(self.sources[2])
        source['version'] = 'new-fixture-version'
        registered = manager.register(source, (self.raw/'guide.html').read_bytes())
        published = manager.publish('guide', registered['revisionId'], self.index.corpus_hash)
        result = tools.call('search_regulations', {'query':'의심거래보고 합당한 근거'})
        self.assertEqual(result['corpusSha256'], published['corpusSha256'])

    def test_foreign_source_cannot_receive_domestic_law_label(self):
        row = {'jurisdiction':'US','sourceKind':'law','effectiveDate':'2026-01-01'}
        self.assertEqual(source_display(row)['category'], 'foreign_reference')


if __name__ == '__main__':
    unittest.main()
