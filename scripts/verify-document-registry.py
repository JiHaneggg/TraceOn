"""Verify the local registry against existing snapshots without publishing edits."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'rag-service'))
from corpus import digest
from corpus_manager import CorpusManager
from evidence_lookup import EvidenceRepository
from retrieval import Index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=ROOT / 'data/rag/index.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/rag-document-registry-verification.json')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Existing verification preserved; choose a new --output')
    manager = CorpusManager(args.index)
    before = manager.index_path.read_bytes()
    registry_before = manager.registry_path.read_bytes()
    index = Index(manager.index_path)
    repository = EvidenceRepository(index)
    results = []
    for document_id, document in manager.load()['documents'].items():
        row = manager.active_revision(document, manager.active_payload())
        if row is None:
            continue
        raw = manager.snapshot(document_id, row['sourceSha256']).read_bytes()
        registered = manager.register(row['source'], raw)
        assert registered['status'] == 'no_change' and not registered['newRevision']
        assert not registered['searchUpdated'] and not any(registered['change'].values())
        diff = manager.diff(document_id, row['revisionId'])
        assert not diff['selectedSectionDiffs'] and not diff['metadataChanges']
        for chunk in manager.rows(row):
            resolved = repository.citation(chunk['chunkId'], index.corpus_hash)
            assert resolved['sourceVerification']['status'] == 'snapshot_checked'
        results.append({'documentId': document_id, 'revisionId': row['revisionId'],
                        'duplicateRegistrationUnchanged': True, 'sourceChunksChecked': len(row['chunkIds'])})
    historical = []
    for path in sorted(manager.archive_dir.glob('*.json')):
        archived = Index(path)
        if archived.corpus_hash == index.corpus_hash:
            continue
        chunk = archived.chunks[0]
        resolved = repository.citation(chunk['chunkId'], archived.corpus_hash)
        assert resolved['citation']['text'] == chunk['text']
        assert resolved['sourceVerification']['status'] == 'snapshot_checked'
        historical.append({'corpusSha256': archived.corpus_hash, 'originalCitationResolved': True})
    assert manager.index_path.read_bytes() == before
    assert manager.registry_path.read_bytes() == registry_before
    result = {'kind': 'existing_local_document_registry_integrity_check', 'activeCorpusSha256': index.corpus_hash,
              'activeIndexBytesUnchanged': True, 'activeIndexFileSha256': digest(before),
              'registryBytesUnchanged': True, 'documentsChecked': len(results),
              'sourceChunksChecked': sum(row['sourceChunksChecked'] for row in results),
              'documents': results, 'historicalVersions': historical,
              'networkCalls': 0, 'llmCalls': 0, 'newDocumentsPublished': 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key != 'documents'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
