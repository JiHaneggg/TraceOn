"""Regression of known V4 questions, plus live scope/report compatibility checks.

Uses the original gold unchanged. This is development/regression evidence, not a
new independent evaluation. No source downloads, DB writes or model calls.
"""
import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'rag-service'),str(ROOT/'data-pipeline')]
from corpus import canonical,digest
from evaluate_frozen import verify_bundle,load_questions,evaluate_case,summarize
from retrieval import Index
from neo4j_mvp import Database

FOCAL={'V4-N02','V4-N09','V4-N10','V4-N11','V4-N16','V4-N20'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-abstention-verification.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Existing verification preserved; choose a new --output')
    reports=args.output.parent/args.output.stem.replace('verification','reports')
    if reports.exists():raise SystemExit('Existing reports preserved; choose a new --output')
    run_dir=ROOT/'artifacts/rag-evaluation-v4'
    manifest=verify_bundle(run_dir/'manifest.json')
    original_bytes=(run_dir/'results.json').read_bytes()
    original=json.loads(original_bytes)
    assert original['resultSha256']==digest(canonical({k:v for k,v in original.items() if k!='resultSha256'}))
    assert digest((ROOT/'rag-service/evaluation_queries_v4.json').read_bytes())==manifest['questionsSha256']
    original_index=Index(run_dir/'corpus.json')
    questions=load_questions(original_index,run_dir/'questions.json')
    index=Index();assert index.query_scope_policy_version=='1.0'
    assert index.chunks==original_index.chunks
    assert index.evidence_fit_policy==original_index.evidence_fit_policy
    class ObservedIndex:
        def search(self,*args,**kwargs):
            self.last=index.search(*args,**kwargs)
            return self.last
    observed=ObservedIndex();after=[]
    for case in questions['cases']:
        row=evaluate_case(observed,case,questions['evidenceUnits'])
        row['scopeAssessment']=observed.last['scopeAssessment']
        after.append(row)
    before_by_id={row['id']:row for row in original['cases']}
    supported_changes=[row['id'] for row in after if row['expected']=='retrieve' and row['expectationPassed']!=before_by_id[row['id']]['expectationPassed']]
    assert not supported_changes,supported_changes
    assert all(row['abstained'] for row in after if row['expected']=='abstain')
    assert all(row['scopeAssessment']['decision']=='abstain' for row in after if row['id'] in FOCAL)
    def request(path,body=None):
        req=Request('http://127.0.0.1:8092'+path,None if body is None else canonical(body).encode('utf-8'),{'Content-Type':'application/json'})
        with urlopen(req,timeout=20) as response:return json.loads(response.read())
    assert request('/health')['corpusSha256']==index.corpus_hash
    targets=json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_text(encoding='utf-8'))['targets']
    database=Database();graphs={tx_id:database.subgraph(tx_id) for tx_id in targets}
    reports.mkdir(parents=True)
    prepared=[]
    for tx_id in targets:
        bundle=request('/v1/rag/reports/prepare',{'graph':graphs[tx_id],'mode':'template'})
        assert bundle['verification']['valid'],bundle['verification']['errors']
        assert not bundle['report']['generation']['llmCalled']
        (reports/(tx_id+'.json')).write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
        (reports/(tx_id+'.md')).write_text(bundle['report']['markdown'],encoding='utf-8')
        prepared.append(tx_id)
    focal=[]
    for row in after:
        if row['id'] not in FOCAL:continue
        result=request('/v1/rag/search',row['request'])
        assert result['status']=='insufficient_scope' and not result['hits']
        assert result['scopeAssessment']==row['scopeAssessment']
        bundle=request('/v1/rag/reports/prepare',{'graph':graphs[targets[0]],'query':row['request']['query'],'mode':'template'})
        report=bundle['report'];assert bundle['verification']['valid'],bundle['verification']['errors']
        assert report['answerDisposition']['status']=='abstained'
        assert report['answerDisposition']['scopeAssessment']['reasonCodes']
        assert report['reviewReadiness']['status']=='needs_more_evidence'
        assert '## 원질문 답변 보류' in report['markdown']
        (reports/(row['id']+'.json')).write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
        (reports/(row['id']+'.md')).write_text(report['markdown'],encoding='utf-8')
        focal.append({'id':row['id'],'reasonCodes':result['scopeAssessment']['reasonCodes'],'reportAbstained':True})
    legacy=[]
    for folder in ['rag-document-evidence-reports','rag-domestic-evidence-reports','rag-evidence-fit-reports']:
        for path in sorted((ROOT/'artifacts'/folder).glob('TX-*.json')):
            old=json.loads(path.read_text(encoding='utf-8'));context=old['context'];graph=graphs[context['targetTxId']]
            result=request('/v1/rag/reports/verify',{'graph':graph,'report':old['report']})
            assert result['valid'],(str(path),result['errors'])
            fact=request('/v1/rag/facts/resolve',{'graph':graph,'factId':'target.amountPaid',
                         'contextId':context['contextId'],'corpusSha256':context['corpusSha256']})
            assert fact['fact']['value']==context['targetTransaction']['amountPaid']
            legacy.append(str(path.relative_to(ROOT)))
    assert len(legacy)==36
    try:
        request('/v1/rag/context',{'graph':graphs[targets[0]],'corpusSha256':original_index.corpus_hash})
        raise AssertionError('Legacy generation must not bypass the active scope policy')
    except HTTPError as error:
        assert error.code==409 and json.loads(error.read())['error']=='legacy_corpus_read_only'
    assert (run_dir/'results.json').read_bytes()==original_bytes
    result={'kind':'v4_known_question_regression_after_scope_guard_not_new_holdout',
            'originalQuestionsSha256':manifest['questionsSha256'],'originalResultSha256':original['resultSha256'],
            'originalCorpusSha256':original_index.corpus_hash,'corpusSha256':index.corpus_hash,
            'sameQuestionAnnotations':True,'sameOriginalChunks':True,'originalFirstRunPreserved':True,
            'before':original['summary'],'after':summarize(after),'supportedExpectationChanges':supported_changes,
            'changedIds':[row['id'] for row in after if row['expectationPassed']!=before_by_id[row['id']]['expectationPassed']],
            'remainingFailureIds':[row['id'] for row in after if not row['expectationPassed']],
            'focalLiveApiChecks':focal,'newCaseReportsVerified':len(prepared),'legacyReportsAndFactsVerified':len(legacy),
            'legacyGenerationBlocked':True,'llmCalls':0,'latenciesNotUsedForComparison':True,'cases':after,
            'reportDirectory':str(reports)}
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in {'cases','before','after'}},ensure_ascii=False))
    print(json.dumps({'before':result['before'],'after':result['after']},ensure_ascii=False))


if __name__=='__main__':main()
