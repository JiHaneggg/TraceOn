"""Read-only Neo4j integration: assess evidence, retain gaps, verify old reports.

This is a bounded regression check, not an independent detection/semantic/legal
benchmark. It uses template generation and never writes to the database.
"""
import argparse
import copy
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'rag-service'),str(ROOT/'data-pipeline')]
from corpus import canonical,digest
from neo4j_mvp import Database
from retrieval import Index
from evidence import build_context


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-evidence-fit-verification.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Existing verification preserved; choose a new --output')
    reports_dir=args.output.parent/args.output.stem.replace('verification','reports')
    if reports_dir.exists():raise SystemExit('Existing report directory preserved; choose a new --output')
    reports_dir.mkdir(parents=True)
    def request(path,body=None):
        req=Request('http://127.0.0.1:8092'+path,None if body is None else canonical(body).encode('utf-8'),{'Content-Type':'application/json'})
        with urlopen(req,timeout=20) as response:return json.loads(response.read())
    def rehash(report):
        report['reportId']='RPT-'+digest(canonical({k:v for k,v in report.items() if k!='reportId'}))[:24]
        return report
    index=Index();assert index.evidence_fit_policy is not None
    health=request('/health');assert health['corpusSha256']==index.corpus_hash
    previous_hash=json.loads((ROOT/'artifacts/rag-evidence-fit-policy-publication.json').read_text(encoding='utf-8'))['previousCorpusSha256']
    previous=Index(ROOT/'data/rag/versions'/(previous_hash+'.json'))
    assert index.chunks==previous.chunks
    database=Database()
    targets=json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_text(encoding='utf-8'))['targets']
    rows=[];tampering=[];graphs={}
    for tx_id in targets:
        graph=database.subgraph(tx_id);graphs[tx_id]=graph
        assessment=request('/v1/rag/evidence/assess',{'graph':graph})
        bundle=request('/v1/rag/reports/prepare',{'graph':graph,'mode':'template'})
        context,report,verified=bundle['context'],bundle['report'],bundle['verification']
        assert verified['valid'],verified['errors']
        assert assessment['evidenceFitness']==context['evidenceFitness']==report['evidenceFitness']
        assert report['reviewReadiness']['readyForSubmission'] is False
        assert not report['generation']['llmCalled'] and not report['evidenceFitness']['sufficientForAutomatedDecision']
        assert verified['checks']['evidenceFitness']['valid'] and verified['checks']['sourceSnapshots']['valid']
        assert context['ruleResult']==build_context(previous,graph)['ruleResult']
        for citation in report['citations']:
            assert request(citation['lookup']['path'])['sourceVerification']['status']=='snapshot_checked'
        fit=report['evidenceFitness']
        if fit['findings']:assert fit['status']=='needs_more_evidence'
        else:assert fit['status']=='no_rule_findings'
        (reports_dir/(tx_id+'.json')).write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
        (reports_dir/(tx_id+'.md')).write_text(report['markdown'],encoding='utf-8')
        rows.append({'targetTxId':tx_id,'status':fit['status'],'rules':[f['ruleId'] for f in fit['findings']],
                     'missingEvidence':{f['findingId']:[m['id'] for m in f['missingEvidence']] for f in fit['findings']},
                     'originalRuleResultUnchanged':True,'fullReportVerified':True,'citationsResolved':len(report['citations'])})
        if not tampering and fit['findings']:
            for kind in ['fake_sufficiency','fake_readiness','missing_anchor','missing_observation']:
                altered=copy.deepcopy(report)
                if kind=='fake_sufficiency':altered['evidenceFitness']['sufficientForAutomatedDecision']=True
                elif kind=='fake_readiness':altered['reviewReadiness']['readyForSubmission']=True
                elif kind=='missing_anchor':
                    reference=index.evidence_fit_policy['references']['str-context']
                    quote=next(c for c in altered['draft']['citations'] if c['chunkId']==reference['chunkId'])
                    quote['quote']=index.by_id[reference['chunkId']]['text'][:20]
                else:altered['plan']['observationIds'].remove('evidence_fitness')
                result=request('/v1/rag/reports/verify',{'graph':graph,'report':rehash(altered)})
                assert not result['valid'],kind
                tampering.append({'kind':kind,'rejected':True,'selfHashRecomputed':True})
    legacy=[]
    for folder in ['rag-domestic-evidence-reports','rag-document-evidence-reports']:
        for path in sorted((ROOT/'artifacts'/folder).glob('TX-*.json')):
            bundle=json.loads(path.read_text(encoding='utf-8'))
            tx_id=bundle['context']['targetTxId']
            result=request('/v1/rag/reports/verify',{'graph':graphs[tx_id],'report':bundle['report']})
            assert result['valid'],(str(path),result['errors'])
            legacy.append({'file':str(path.relative_to(ROOT)),'valid':True})
    assert len(legacy)==24
    insufficient=request('/v1/rag/evidence/assess',{'graph':graphs[targets[0]],'query':'KB국민은행 내부 규정 STR 승인 기준'})
    assert not insufficient['evidenceFitness']['queryCheck']['requestedScopeFound']
    result={'kind':'bounded_evidence_fit_actual_neo4j_regression_not_semantic_legal_benchmark',
            'corpusSha256':index.corpus_hash,'policySha256':index.evidence_fit_policy['policySha256'],
            'unchangedOriginalChunks':True,'verifiedReports':len(rows),'cases':rows,
            'citationsResolved':sum(r['citationsResolved'] for r in rows),'legacyReportsVerified':len(legacy),
            'legacyReports':legacy,'tampering':tampering,'unsupportedQuestionAbstention':True,
            'llmCalls':0,'semanticEntailmentVerified':False,'reportDirectory':str(reports_dir)}
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in {'cases','legacyReports'}},ensure_ascii=False))


if __name__=='__main__':main()
