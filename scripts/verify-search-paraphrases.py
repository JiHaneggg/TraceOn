"""Known-question regression; never label these tuned V4 questions a holdout.

Requires the preserved local evaluation/corpus artifacts. --live additionally
checks the running loopback API, read-only Neo4j graphs and stored reports.
No source downloads, database writes or model calls.
"""
import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'rag-service'), str(ROOT/'data-pipeline')]
from corpus import canonical, digest
from evaluate_frozen import verify_bundle, load_questions, evaluate_case, summarize, hashes, production_files
from retrieval import Index
from search_intent import prioritize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, default=ROOT/'artifacts/rag-search-v5/before.json')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/rag-search-v5/verification.json')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    args.before = args.before.resolve()
    args.output = args.output.resolve()
    if args.output.exists(): raise SystemExit('Preserve existing results; choose a new --output')
    frozen = ROOT/'artifacts/rag-evaluation-v4'
    manifest = verify_bundle(frozen/'manifest.json')
    preserved = {name:digest((frozen/name).read_bytes()) for name in ['manifest.json','questions.json','corpus.json','results.json']}
    question_path = ROOT/'rag-service/evaluation_queries_v4.json'
    assert digest(question_path.read_bytes()) == manifest['questionsSha256']
    original_index = Index(frozen/'corpus.json')
    index = Index()
    assert index.query_scope_policy_version == '1.0'
    assert index.chunks == original_index.chunks
    assert index.evidence_fit_policy == original_index.evidence_fit_policy
    questions = load_questions(original_index, question_path)
    before = json.loads(args.before.read_bytes())
    assert before['corpusSha256'] == index.corpus_hash
    assert before['questionsSha256'] == manifest['questionsSha256']
    by_id = {r['id']:r for r in before['cases']}
    assert len(by_id) == len(questions['cases'])
    for case in questions['cases']:
        assert all(by_id[case['id']][key] == case[key] for key in ['request','expected','requiredUnits'])
    assert before['summary'] == summarize(before['cases'])
    after = [evaluate_case(index, c, questions['evidenceUnits']) for c in questions['cases']]
    regressions = [r['id'] for r in after if by_id[r['id']]['expectationPassed'] and not r['expectationPassed']]
    assert not regressions, regressions
    assert all(r['expectationPassed'] for r in after)
    # Ablation isolates recipient-relation promotion from vocabulary expansion.
    # Both are tuned/regression measurements, not independent evaluations.
    with patch('retrieval.prioritize', lambda rows, query, **kw: prioritize(rows, query, signals=[])):
        expansion_only = [evaluate_case(index, c, questions['evidenceUnits']) for c in questions['cases']]
    live = None
    if args.live:
        from neo4j_mvp import Database
        def request(path, body=None):
            req = Request('http://127.0.0.1:8092'+path,
                          None if body is None else canonical(body).encode('utf-8'),
                          {'Content-Type':'application/json'})
            with urlopen(req, timeout=25) as response: return json.loads(response.read())
        assert request('/health')['corpusSha256'] == index.corpus_hash
        citations = set()
        for case in questions['cases']:
            result = request('/v1/rag/search', case['request'])
            options = case['request']
            expected = index.search(options['query'], jurisdiction=options.get('jurisdiction','KR'),
                                    source_kind=options.get('sourceKind'), legal_as_of=options.get('legalAsOf'))
            assert result == expected
            for hit in result['hits']:
                for row in [hit, *hit['supportingChunks']]:
                    assert row['text'] == index.by_id[row['chunkId']]['text']
                    assert row['textSha256'] == digest(row['text'])
                    citations.add(row['chunkId'])
        for chunk_id in sorted(citations):
            source = request('/v1/rag/citations/'+quote(chunk_id,safe='')+'?corpusSha256='+index.corpus_hash)
            assert source['sourceVerification']['status'] == 'snapshot_checked'
        targets = json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_bytes())['targets']
        database = Database()
        graphs = {tx:database.subgraph(tx) for tx in targets}
        reports_dir = args.output.parent/(args.output.stem+'-reports')
        reports_dir.mkdir(parents=True, exist_ok=False)
        def prepare(label, graph, query=None, abstained=False):
            body = {'graph':graph,'mode':'template'}
            if query is not None: body['query'] = query
            bundle = request('/v1/rag/reports/prepare', body)
            assert bundle['verification']['valid'], (label,bundle['verification']['errors'])
            assert not bundle['report']['generation']['llmCalled']
            if abstained: assert bundle['report']['answerDisposition']['status'] == 'abstained'
            with (reports_dir/(label+'.json')).open('x',encoding='utf-8') as stream:
                json.dump(bundle,stream,ensure_ascii=False,indent=2)
        for tx, graph in graphs.items(): prepare(tx,graph)
        old_misses = [r for r in before['cases'] if r['expected']=='retrieve' and not r['expectationPassed']]
        domestic = [r for r in old_misses if r['request'].get('jurisdiction','KR')=='KR']
        for row in domestic: prepare(row['id'],graphs[targets[0]],row['request']['query'])
        blocked_ids = {'V4-N02','V4-N09','V4-N10','V4-N11','V4-N16','V4-N20'}
        for case in questions['cases']:
            if case['id'] in blocked_ids: prepare(case['id'],graphs[targets[0]],case['request']['query'],True)
        legacy = []
        for folder in ['rag-document-evidence-reports','rag-domestic-evidence-reports',
                       'rag-evidence-fit-reports','rag-abstention-reports']:
            for path in sorted((ROOT/'artifacts'/folder).glob('TX-*.json')):
                old = json.loads(path.read_bytes())
                graph = graphs[old['context']['targetTxId']]
                checked = request('/v1/rag/reports/verify', {'graph':graph,'report':old['report']})
                assert checked['valid'], (path.name, checked['errors'])
                for fact_id, key in [('target.amountPaid','amountPaid'),('target.timestamp','timestamp')]:
                    fact = request('/v1/rag/facts/resolve', {'graph':graph,'factId':fact_id,
                            'contextId':old['context']['contextId'],'corpusSha256':old['context']['corpusSha256']})
                    assert fact['fact']['value'] == old['context']['targetTransaction'][key]
                legacy.append(path.relative_to(ROOT).as_posix())
        assert len(legacy) == 48
        live = {'allQuestionApiResponsesEqual':len(after),'sourceSnapshotsVerified':len(citations),
                'newTransactionReportsVerified':len(graphs),'paraphraseReportsVerified':len(domestic),
                'abstainedReportsVerified':len(blocked_ids),'legacyReportsAndFactsVerified':len(legacy),
                'reportsDirectory':reports_dir.relative_to(ROOT).as_posix()}
    assert all(digest((frozen/name).read_bytes()) == value for name,value in preserved.items())
    result = {'kind':'known_v4_search_paraphrase_regression_not_new_holdout',
              'questionsSha256':manifest['questionsSha256'],'corpusSha256':index.corpus_hash,
              'beforeFileSha256':digest(args.before.read_bytes()),'preservedOriginalFiles':preserved,
              'sameOriginalChunksAndAnnotations':True,'implementationSha256':hashes(ROOT,production_files(ROOT)),
              'before':before['summary'],'expansionOnly':summarize(expansion_only),'after':summarize(after),
              'fixedIds':[r['id'] for r in after if r['expectationPassed'] and not by_id[r['id']]['expectationPassed']],
              'regressions':regressions,'llmCalls':0,'live':live,'cases':after,
              'limitations':['V4 gold was exposed and used for tuning; this is regression evidence.',
                             'Exact source quotes do not prove semantic entailment or legal applicability.',
                             'Source snapshots and queries need independent domain review.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream: json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:result[k] for k in ['kind','before','expansionOnly','after','fixedIds','regressions','live','llmCalls']},ensure_ascii=False))


if __name__ == '__main__': main()
