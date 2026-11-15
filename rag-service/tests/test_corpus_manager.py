"""Synthetic mechanics tests; these fixtures are not financial guidance."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / 'data-pipeline')]

from corpus import build, canonical, digest
from corpus_manager import CorpusManager, CuratedRedirect, validate_source, validate_url
from evidence import build_context
from evidence_lookup import EvidenceError, EvidenceRepository
from make_demo import generate
from rag_server import handler_for
from reports import generate_report
from report_verification import verify_report
from retrieval import Index


class CorpusManagementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.path = self.root / 'index.json'
        self.sources = []
        for name, title, heading, text in [
            ('str', '의심거래보고제도', '의심거래보고 정의', '의심거래보고 STR 합당한 근거 보고책임자 자료 보고 절차'),
            ('ctr', '고액현금거래보고제도', '고액현금거래보고 개념', '고액현금거래보고 CTR 동일인 하루 현금 자동 보고'),
            ('cdd', '고객확인제도', '고객확인 개념', '고객확인 CDD 실제 소유자 거래 목적 자금 원천 신원 확인')]:
            source = {'documentId': name, 'title': title, 'url': 'https://kofiu.go.kr/' + name,
                      'publisher': 'synthetic fixture', 'jurisdiction': 'KR', 'sourceKind': 'official_guidance',
                      'effectiveDate': None, 'sections': [{'heading': heading, 'end': '끝'}]}
            self.sources.append(source)
            (self.raw / (name + '.html')).write_bytes(self.html(source, text))
        build(self.raw, self.path, sources=self.sources)
        self.old = Index(self.path)
        self.old_bytes = self.path.read_bytes()
        self.manager = CorpusManager(self.path)
        self.manager.init(self.sources)

    @staticmethod
    def html(source, text):
        return ('<html><h3>' + source['sections'][0]['heading'] + '</h3><p>' + text + '</p><h3>끝</h3></html>').encode('utf-8')

    def candidate(self, text='의심거래보고 STR 갱신된 검증용 합성 본문'):
        return self.manager.register(self.sources[0], self.html(self.sources[0], text))

    def publish(self, candidate):
        return self.manager.publish('str', candidate['revisionId'], self.old.corpus_hash)

    def test_bootstrap_keeps_exact_active_corpus_and_metadata(self):
        self.assertEqual(self.path.read_bytes(), self.old_bytes)
        status = self.manager.status()
        self.assertEqual(status['activeCorpusSha256'], self.old.corpus_hash)
        self.assertEqual(status['documentCount'], 3)
        row = status['documents'][0]['revisions'][0]
        self.assertTrue(row['active'])
        self.assertIsNone(row['metadata']['effectiveDate'])
        self.assertEqual(row['capturedAt'], self.old.documents[0]['capturedAt'])
        self.assertTrue((self.root / 'versions' / (self.old.corpus_hash + '.json')).exists())

    def test_identical_registration_and_init_are_idempotent(self):
        registry = self.manager.registry_path.read_bytes()
        before = self.manager.status()
        result = self.manager.register(self.sources[0], (self.raw / 'str.html').read_bytes())
        self.assertEqual(result['status'], 'no_change')
        self.assertFalse(result['newRevision'])
        self.assertFalse(any(result['change'].values()))
        self.assertEqual(self.manager.init(), before)
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_candidate_is_not_searchable_until_publication(self):
        result = self.candidate()
        self.assertEqual(result['status'], 'candidate')
        self.assertFalse(result['searchUpdated'])
        self.assertEqual(self.path.read_bytes(), self.old_bytes)
        self.assertEqual(len(self.manager.status('str')['documents'][0]['revisions']), 2)
        again = self.candidate()
        self.assertEqual(again['revisionId'], result['revisionId'])
        self.assertFalse(again['newRevision'])

    def test_publication_preserves_other_docs_and_both_complete_indexes(self):
        result = self.publish(self.candidate())
        current = Index(self.path)
        self.assertEqual(result['status'], 'published')
        self.assertNotEqual(current.corpus_hash, self.old.corpus_hash)
        self.assertIn('갱신된', next(r['text'] for r in current.chunks if r['documentId'] == 'str'))
        self.assertEqual([r for r in current.chunks if r['documentId'] != 'str'],
                         [r for r in self.old.chunks if r['documentId'] != 'str'])
        for checksum in (self.old.corpus_hash, current.corpus_hash):
            self.assertEqual(Index(self.root / 'versions' / (checksum + '.json')).corpus_hash, checksum)

    def test_old_report_still_verifies_after_publication(self):
        graph = next(c['graph'] for c in generate()[1] if c['scenario'] == 'cycle')
        report = generate_report(build_context(self.old, graph))
        self.publish(self.candidate())
        repo = EvidenceRepository(self.old)
        self.assertTrue(verify_report(repo, graph, None, report)['valid'])
        old_chunk = next(r for r in self.old.chunks if r['documentId'] == 'str')
        result = repo.citation(old_chunk['chunkId'], self.old.corpus_hash)
        self.assertEqual(result['citation']['text'], old_chunk['text'])
        self.assertEqual(result['sourceVerification']['status'], 'snapshot_checked')

    def test_explicit_previous_revision_can_be_republished(self):
        original = self.manager.status('str')['documents'][0]['activeRevisionId']
        first = self.publish(self.candidate())
        result = self.manager.publish('str', original, first['corpusSha256'])
        self.assertTrue(result['searchUpdated'])
        self.assertEqual([r for r in Index(self.path).chunks if r['documentId'] == 'str'],
                         [r for r in self.old.chunks if r['documentId'] == 'str'])
        self.assertEqual(self.manager.status('str')['documents'][0]['activeRevisionId'], original)

    def test_stale_publication_hash_and_unknown_revision_leave_index_intact(self):
        candidate = self.candidate()
        with self.assertRaisesRegex(ValueError, 'Active corpus changed'):
            self.manager.publish('str', candidate['revisionId'], '0' * 64)
        with self.assertRaisesRegex(ValueError, 'Unknown document revision'):
            self.manager.publish('str', '0' * 64, self.old.corpus_hash)
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_corrupt_source_cannot_be_published(self):
        candidate = self.candidate()
        row = self.manager.load()['documents']['str']['revisions'][-1]
        path = self.manager.snapshot('str', row['sourceSha256'])
        path.write_bytes(b'<p>corrupt</p>')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.publish(candidate)
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_failed_download_or_changed_layout_keeps_good_data(self):
        registry = self.manager.registry_path.read_bytes()
        for raw in (b'<html>403 Forbidden</html>', b'<p>layout changed</p>', b'plain text', b'x' * 2_000_001):
            with self.assertRaises(ValueError):
                self.manager.check('str', raw)
        with patch('corpus_manager.fetch_html', side_effect=OSError('network unavailable')):
            with self.assertRaises(OSError):
                self.manager.check('str')
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_html_markup_change_and_metadata_change_are_distinguished(self):
        raw = (self.raw / 'str.html').read_bytes().replace(b'<p>', b'<p class="new">')
        change = self.manager.check('str', raw)['change']
        self.assertTrue(change['sourceBytesChanged'])
        self.assertFalse(change['normalizedDocumentChanged'])
        self.assertFalse(change['metadataOrSelectorsChanged'])
        source = copy.deepcopy(self.sources[0])
        source['version'] = 'synthetic-v2'
        change = self.manager.check('str', (self.raw / 'str.html').read_bytes(), source)['change']
        self.assertFalse(change['sourceBytesChanged'])
        self.assertTrue(change['metadataOrSelectorsChanged'])

    def test_diff_previews_changed_text_and_metadata_without_publication(self):
        source = {**self.sources[0], 'version': 'synthetic-v2'}
        candidate = self.manager.check('str', self.html(source, '새로 수정한 합성 검증 본문'), source)
        result = self.manager.diff('str', candidate['revisionId'])
        self.assertIn('새로 수정한', result['selectedSectionDiffs'][0]['diff'])
        self.assertEqual(result['metadataChanges']['version']['after'], 'synthetic-v2')
        self.assertFalse(result['previewTruncated'])
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_invalid_metadata_and_redirect_are_rejected(self):
        for update in ({'documentId': '../escape'}, {'effectiveDate': '2026-02-30'},
                       {'validUntil': '2026-01-01'}, {'sourceKind': 'invented'}, {'url': 'http://kofiu.go.kr/'},
                       {'url': 'https://kofiu.go.kr.evil.example/'}, {'url': 'https://user:pass@kofiu.go.kr/'},
                       {'sections': []}, {'unexpectedField': 'x'}):
            source = {**self.sources[0], **update}
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_source(source)
        with self.assertRaises(ValueError):
            CuratedRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')
        with self.assertRaises(ValueError):
            validate_url('https://kofiu.go.kr:8443/')

    def test_lock_and_registry_corruption_fail_closed(self):
        with self.manager.locked():
            with self.assertRaisesRegex(ValueError, 'holds the lock'):
                self.candidate()
        registry = json.loads(self.manager.registry_path.read_text(encoding='utf-8'))
        registry['documents']['str']['revisions'][0]['source']['title'] = 'tampered'
        self.manager.registry_path.write_text(json.dumps(registry), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.manager.status()

    def test_corrupt_archive_blocks_switch(self):
        archive = self.root / 'versions' / (self.old.corpus_hash + '.json')
        archive.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            self.publish(self.candidate())
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_archive_lookup_after_more_than_64_versions(self):
        self.publish(self.candidate())
        for number in range(70):
            (self.root / 'versions' / (f'{number:064x}' + '.json')).write_text('{}', encoding='utf-8')
        repo = EvidenceRepository(Index(self.path))
        self.assertEqual(repo.index_for(self.old.corpus_hash).corpus_hash, self.old.corpus_hash)

    def test_legacy_builder_cannot_bypass_managed_publication(self):
        with self.assertRaisesRegex(ValueError, 'Managed corpus'):
            build(self.raw, self.path, sources=self.sources)

    def test_empty_install_can_register_then_publish_first_document(self):
        manager = CorpusManager(self.root / 'new' / 'index.json')
        self.assertIsNone(manager.init([])['activeCorpusSha256'])
        result = manager.register(self.sources[0], (self.raw / 'str.html').read_bytes())
        self.assertFalse(manager.index_path.exists())
        result = manager.publish('str', result['revisionId'], 'none')
        self.assertEqual(len(Index(manager.index_path).documents), 1)
        self.assertEqual(result['status'], 'published')

    def test_new_document_and_noop_publication(self):
        source = {**self.sources[0], 'documentId': 'new-document', 'title': '추가 합성 문서'}
        candidate = self.manager.register(source, self.html(source, '새로 등록한 합성 근거'))
        result = self.manager.publish('new-document', candidate['revisionId'], self.old.corpus_hash)
        self.assertEqual(len(Index(self.path).documents), 4)
        before = self.path.read_bytes()
        repeated = self.manager.publish('new-document', candidate['revisionId'], result['corpusSha256'])
        self.assertFalse(repeated['searchUpdated'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_http_requests_reload_current_and_keep_old_citations_pinned(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.old))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def request(method, path, body=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=10)
                try:
                    connection.request(method, path, json.dumps(body) if body is not None else None,
                                       {'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            self.assertEqual(request('GET', '/health')[1]['corpusSha256'], self.old.corpus_hash)
            result = self.publish(self.candidate())
            self.assertEqual(request('GET', '/health')[1]['corpusSha256'], result['corpusSha256'])
            self.assertEqual(request('POST', '/v1/rag/search', {'query': 'STR 정의'})[1]['corpusSha256'], result['corpusSha256'])
            from evidence_lookup import citation_path
            row = next(r for r in self.old.chunks if r['documentId'] == 'str')
            code, body = request('GET', citation_path(row['chunkId'], self.old.corpus_hash))
            self.assertEqual(code, 200)
            self.assertEqual(body['citation']['text'], row['text'])
            self.path.write_text('{}', encoding='utf-8')
            self.assertEqual(request('GET', '/health')[0], 409)
            self.assertEqual(request('POST', '/v1/rag/search', {'query': 'STR 정의'})[0], 409)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
