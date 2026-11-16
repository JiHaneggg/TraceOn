"""Compare the viewer bridge with the existing local RAG API; no DB writes/LLM."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]


def call(port,path,body=None,expected=200,origin=None):
    headers={'Content-Type':'application/json'}
    if origin:headers['Origin']=origin
    request=Request(f'http://127.0.0.1:{port}'+path,
                    None if body is None else json.dumps(body,ensure_ascii=False,allow_nan=False).encode(),headers)
    try:
        with urlopen(request,timeout=30) as response:status,data=response.status,response.read()
    except HTTPError as error:status,data=error.code,error.read()
    assert status==expected,(path,status,expected)
    return json.loads(data)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/graph-viewer/rag-integration-v2.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Use a new --output path; existing evidence is preserved.')
    cases=call(8093,'/api/cases')['items'];results=[];total_citations=total_facts=0
    for item in cases:
        started=perf_counter()
        payload=call(8093,'/api/graph?'+urlencode({'txId':item['txId'],'hops':2,'windowDays':7}))
        findings=payload['ruleResult']['findings']
        index=max(range(len(findings)),key=lambda i:findings[i]['score']) if findings else None
        prepared=call(8093,'/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':index})
        graph_and_prepare_seconds=perf_counter()-started
        inputs={'graph':payload['graph'],'ruleResult':payload['ruleResult']}
        direct=call(8092,'/v1/rag/reports/prepare',{**inputs,'mode':'template'})
        for key in ('context','report','verification'):assert prepared[key]==direct[key],(item['txId'],key)
        assert prepared['verification']['valid'] is True
        assert prepared['verification']['readyForSubmission'] is False
        assert prepared['report']['generation']=={'method':'deterministic_template','llmCalled':False}
        by_id={edge['txId']:edge for edge in payload['graph']['edges']}
        by_fact={fact['factId']:fact for fact in prepared['context']['reportFacts']}
        fact_ids=['target.amountPaid','target.timestamp','rule.score']
        if index is not None:fact_ids.extend([f'finding.{index}.transactionCount',f'finding.{index}.elapsedSeconds'])
        for fact_id in fact_ids:
            resolved=call(8093,'/api/review/fact',{'reviewId':prepared['reviewId'],'factId':fact_id})
            assert resolved['fact']==by_fact[fact_id]
            assert set(resolved['fact']['evidenceTxIds'])=={edge['txId'] for edge in resolved['transactions']}
            assert all(edge==by_id[edge['txId']] for edge in resolved['transactions'])
        for citation in prepared['report']['citations']:
            resolved=call(8093,'/api/review/citation?'+urlencode({'reviewId':prepared['reviewId'],'chunkId':citation['chunkId']}))
            assert resolved['sourceVerification']['status']=='snapshot_checked'
            assert resolved['corpusSha256']==prepared['context']['corpusSha256']
            highlight=resolved['section']['highlight']
            assert resolved['section']['text'][highlight['start']:highlight['end']]==citation['text']
        verified=call(8093,'/api/review/verify',{'reviewId':prepared['reviewId']})
        assert verified['report']==prepared['report']
        assert verified['verification']==prepared['verification']
        downloaded=call(8093,'/api/review/download?'+urlencode({'reviewId':prepared['reviewId'],'format':'json'}))
        assert downloaded['report']==prepared['report'] and downloaded['verification']['valid'] is True
        with urlopen('http://127.0.0.1:8093/api/review/download?'+urlencode({'reviewId':prepared['reviewId'],'format':'markdown'}),timeout=30) as response:
            assert 'attachment;' in response.headers['Content-Disposition']
            assert response.read().decode('utf-8')==prepared['report']['markdown']
        total_citations+=len(prepared['report']['citations']);total_facts+=len(fact_ids)
        results.append({'txId':item['txId'],'focusRule':prepared['focus']['ruleId'],
                        'reportId':prepared['report']['reportId'],'contextId':prepared['context']['contextId'],
                        'corpusSha256':prepared['context']['corpusSha256'],
                        'checkedCitations':len(prepared['report']['citations']),'checkedFacts':len(fact_ids),
                        'integrityValid':True,'evidenceReadiness':prepared['context'].get('evidenceFitness',{}).get('status'),
                        'graphAndPrepareSeconds':round(graph_and_prepare_seconds,4)})
    # Fail-closed client boundaries over the last current snapshot/report.
    call(8093,'/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':index,'graph':{}},400)
    call(8093,'/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':index},403,origin='http://localhost:9999')
    call(8093,'/api/review/fact',{'reviewId':prepared['reviewId'],'factId':'finding.999.amountPaid'},404)
    call(8093,'/api/review/citation?'+urlencode({'reviewId':prepared['reviewId'],'chunkId':'not-in-report:chunk'}),expected=404)
    call(8093,'/api/review/verify',{'reviewId':prepared['reviewId'],'report':{}},400)
    call(8093,'/api/review/verify',{'reviewId':'invalid'},409)
    call(8093,'/api/review/download?'+urlencode({'reviewId':prepared['reviewId'],'format':'html'}),expected=400)
    call(8093,'/api/review/download?reviewId=invalid&format=json',expected=409)
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['graph-viewer/server.py','graph-viewer/review_bridge.py']}
    report={'checkedAt':datetime.now(timezone.utc).isoformat(),'cases':len(results),'checkedFacts':total_facts,
            'checkedCitations':total_citations,'checkedDownloads':2*len(results),'negativeBoundaryChecks':8,'databaseWrites':0,'llmCalls':0,
            'scope':'local graph-to-RAG integration, not detection accuracy or semantic/legal validation',
            'engineSha256':hashes,'results':results}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:report[key] for key in ['cases','checkedFacts','checkedCitations','checkedDownloads','negativeBoundaryChecks','databaseWrites','llmCalls']},ensure_ascii=False))


if __name__=='__main__':main()
