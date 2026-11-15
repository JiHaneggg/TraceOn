"""Check collected official sources, typed search results and legacy reports.

Hand-authored integration/regression checks, not an independent retrieval benchmark.
No source downloads, model calls, rule tuning or database writes.
"""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'rag-service'))
from corpus import canonical, digest
from evidence_lookup import citation_path
from retrieval import Index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8092')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/rag-domestic-source-verification.json')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Existing verification preserved; choose a new --output')
    def request(path, body=None):
        req = Request(args.base_url + path, None if body is None else canonical(body).encode('utf-8'),
                      {'Content-Type': 'application/json'})
        with urlopen(req, timeout=15) as response:
            return json.loads(response.read())
    index = Index()
    assert request('/health')['corpusSha256'] == index.corpus_hash
    questions_path = ROOT/'rag-service/domestic_source_checks.json'
    questions = json.loads(questions_path.read_text(encoding='utf-8'))
    results = []
    for case in questions:
        result = request('/v1/rag/search', case['request'])
        if case.get('abstain'):
            passed = result['status'] == 'insufficient_scope' and not result['hits']
        else:
            passed = result['status'] == 'matched' and any(
                hit['documentId'] == case['documentId'] and case['section'] in hit['section']
                and hit['sourceDisplay']['category'] == case['category'] for hit in result['hits'])
        results.append({'id':case['id'], 'request':case['request'], 'passed':passed,
                        'status':result['status'], 'sourceGroups':result['sourceGroups'],
                        'hits':[{'documentId':r['documentId'],'section':r['section'],
                                 'sourceDisplay':r['sourceDisplay']} for r in result['hits']]})
    snapshots = []
    for row in index.chunks:
        resolved = request(citation_path(row['chunkId'], index.corpus_hash))
        assert resolved['citation']['text'] == row['text']
        assert resolved['sourceVerification']['status'] == 'snapshot_checked'
        assert resolved['citation']['sourceDisplay']['legalApplicabilityVerified'] is False
        snapshots.append(row['chunkId'])
    legacy = []
    for path in (ROOT/'data/rag/versions').glob('*.json'):
        previous = Index(path)
        if previous.presentation_version:
            continue
        row = previous.chunks[0]
        resolved = request(citation_path(row['chunkId'], previous.corpus_hash))
        assert resolved['sourceVerification']['status'] == 'snapshot_checked'
        assert resolved['citation']['text'] == row['text']
        legacy.append(previous.corpus_hash)
    result = {'kind':'collected_source_integration_regression_not_independent_benchmark',
              'corpusSha256':index.corpus_hash,'questionsSha256':digest(questions_path.read_bytes()),
              'documents':len(index.documents),'chunks':len(index.chunks),
              'searchChecks':len(results),'searchChecksPassed':sum(r['passed'] for r in results),
              'originalChunksChecked':len(snapshots),'legacyCorporaResolved':sorted(set(legacy)),
              'liveSourceFetches':0,'llmCalls':0,'legalApplicabilityVerified':False,'cases':results}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},ensure_ascii=False))
    if not all(r['passed'] for r in results):
        print(json.dumps([r for r in results if not r['passed']],ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
