"""Read-only Neo4j -> rule -> RAG -> review-report verification on MVP cases.

This is an administrator harness, not Spring/ML/UI integration or LLM accuracy.
Prior rule/holdout/RAG results are not overwritten. No real LLM call is made.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "data-pipeline"), str(ROOT / "rag-service")]
from neo4j_mvp import Database
from corpus import visible_text


def post(path, body):
    with urlopen(Request("http://127.0.0.1:" + path, json.dumps(body).encode("utf-8"),
                         {"Content-Type": "application/json"}), timeout=30) as response:
        return json.load(response)


def check_corpus():
    payload = json.loads((ROOT / 'data/rag/index.json').read_text(encoding='utf-8'))
    texts = {}
    for doc in payload['documents']:
        raw = (ROOT / 'data/rag/raw' / doc['snapshotFile']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == doc['sourceSha256']
        texts[doc['documentId']] = visible_text(raw.decode('utf-8-sig'))
    for row in payload['chunks']:
        location = row['locator']
        assert texts[row['documentId']][location['start']:location['end']] == row['text']
        assert hashlib.sha256(row['text'].encode('utf-8')).hexdigest() == row['textSha256']
    return {'documents': len(texts), 'chunkLocationsVerified':len(payload['chunks']), 'corpusSha256':payload['corpusSha256']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/rag-v2-verification.json')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Existing verification preserved. Choose a new --output path for another run.')
    with urlopen('http://127.0.0.1:8092/health',timeout=5) as response:
        health = json.load(response)
    corpus = check_corpus()
    assert health['corpusSha256'] == corpus['corpusSha256'], 'Restart the RAG service to load the new index'
    targets = json.loads((ROOT / 'data/processed/HI-Small-MVP/manifest.json').read_text(encoding='utf-8'))['targets']
    database = Database()
    report_directory = args.output.parent / (args.output.stem.replace('verification','reports'))
    report_directory.mkdir(parents=True,exist_ok=True)
    results = []
    for tx_id in targets:
        start = perf_counter()
        graph = database.subgraph(tx_id)
        rule = post('8091/v1/rules/score',{'graph':graph})
        context = post('8092/v1/rag/context',{'graph':graph,'ruleResult':rule})
        report = post('8092/v1/rag/report',{'graph':graph,'ruleResult':rule,'mode':'template'})
        assert report['validation']['valid'] and not report['generation']['llmCalled']
        assert report['contextId'] == context['contextId']
        assert report['draft']['facts'] == [{'factId':row['factId'],'value':row['value']} for row in context['reportFacts']]
        assert set(context['highlightTxIds']) <= {row['txId'] for row in graph['edges']}
        altered = json.loads(json.dumps(report['draft']))
        altered['facts'][0]['value'] = 'ALTERED'
        rejected = post('8092/v1/rag/validate',{'graph':graph,'ruleResult':rule,'draft':altered})
        assert not rejected['valid']
        (report_directory / (tx_id+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (report_directory / (tx_id+'.md')).write_text(report['markdown'],encoding='utf-8')
        results.append({'targetTxId':tx_id,'graphEdges':len(graph['edges']),'ruleScore':rule['ruleScore'],
                        'findingTypes':sorted({row['ruleId'] for row in rule['findings']}),
                        'reportFacts':len(context['reportFacts']),'citations':len(context['citations']),
                        'validReport':True,'alteredValueRejected':True,'llmCalled':False,
                        'elapsedMs':round((perf_counter()-start)*1000,2)})
    guards = {}
    for name, query in [('internal','KB국민은행 내부 룰 점수 70점의 의심거래보고 기준'),
                         ('law','특정금융정보법 제4조 조문과 시행일')]:
        result = post('8092/v1/rag/search',{'query':query})
        assert result['status'] == 'insufficient_scope' and not result['hits']
        guards[name] = result['status']
    unavailable_rejected = None
    if not health['reportModes']['openaiConfigured']:
        # The health contract guarantees no provider can be called without configuration.
        try:
            post('8092/v1/rag/report',{'graph':graph,'ruleResult':rule,'mode':'openai'})
            raise AssertionError('Unconfigured provider must not fall back')
        except HTTPError as error:
            assert error.code == 503
            unavailable_rejected = True
    result = {'kind':'actual_neo4j_mvp_report_wiring_not_spring_ml_ui_or_live_llm_evaluation',
              'corpus':corpus,'cases':results,'verifiedReports':len(results),'scopeGuards':guards,
              'unconfiguredOpenaiRejected':unavailable_rejected,'llmCalls':0,
              'reportDirectory':str(report_directory)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key != 'cases'},ensure_ascii=False))
