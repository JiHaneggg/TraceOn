"""Read-only integration audit of bounded account pages; no DB writes/model calls."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'data-pipeline'),str(ROOT/'graph-viewer')]
from graph_contract import MemoryGraph,read_transactions
from graph_diagnostics import AuditDatabase,ALL_ROWS,source_map,fingerprint,uncapped_reference


def call(path,body=None,expected=200):
    request=Request('http://127.0.0.1:8093'+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
    try:
        with urlopen(request,timeout=30) as response:status,raw=response.status,response.read()
    except HTTPError as error:status,raw=error.code,error.read()
    result=json.loads(raw)
    if status!=expected:raise AssertionError((path,status,expected,result.get('error')))
    return result


def prepare(payload):
    findings=payload['ruleResult']['findings']
    return call('/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':0 if findings else None})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/graph-viewer/account-expansion-20261118.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Use a new output name; previous evidence is preserved.')
    db=AuditDatabase();source=source_map(row['edge'] for row in db.query(ALL_ROWS,{'limit':20001}))
    csv_source=source_map(read_transactions(ROOT/'data/processed/HI-Small-MVP/transactions.csv'))
    assert source==csv_source
    memory=MemoryGraph(source.values());results=[];page_times=[];review_count=fact_count=citation_count=stale_count=replay_count=0
    for case in call('/api/cases')['items']:
        for hops,days in [(1,1),(2,7),(3,7)]:
            payload=call('/api/graph?'+urlencode({'txId':case['txId'],'hops':hops,'windowDays':days}))
            before=payload;bounded=memory.subgraph(case['txId'],hops=hops,window_days=days,max_neighbors=50,max_edges=1000)
            assert payload['graph']['edges']==bounded['edges']
            reference=uncapped_reference(payload['graph'],source);expected={row['txId'] for row in reference['edges']}
            pages=[];old_review=None
            if payload['accountExpansion']['canExpand']:old_review=prepare(payload)
            while payload['accountExpansion']['canExpand']:
                candidates=[row for row in payload['accountExpansion']['candidates'] if row['canExpand']]
                candidate=min(candidates,key=lambda row:(row['reason']!='more_history',row['depth'],row['accountKey']))
                body={'snapshotId':payload['reviewSnapshotId'],'accountKey':candidate['accountKey']}
                started=perf_counter();expanded=call('/api/graph/expand',body);elapsed=round((perf_counter()-started)*1000,3);page_times.append(elapsed)
                for key in ['targetTxId','asOf','windowStart']:assert expanded['graph'][key]==before['graph'][key]
                assert expanded['graph']['meta']['hops']==hops
                edges=expanded['graph']['edges'];ids={row['txId'] for row in edges};last=expanded['accountExpansion']['lastPage']
                assert len(ids)==len(edges)<=1000 and ids<=expected
                assert all(source[row['txId']]==row for row in edges)
                assert {row['txId'] for row in payload['graph']['edges']}<=ids
                assert 0<=last['readRows']<=25 and last['readRows']==last['addedTransactions']+last['duplicateTransactions']
                assert expanded['accountExpansion']['pagesUsed']==len(pages)+1<=8
                assert expanded['source']['sourceGraphComplete'] is False
                assert call('/api/graph/expand',body)==expanded;replay_count+=1
                call('/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':0 if payload['ruleResult']['findings'] else None},409);stale_count+=1
                pages.append({'accountKey':candidate['accountKey'],**last,'elapsedMs':elapsed,'transactionsAfter':len(edges)})
                payload=expanded
            if old_review:
                call('/api/review/verify',{'reviewId':old_review['reviewId']},409);stale_count+=1
                report=prepare(payload);review_count+=1
                assert report['verification']['valid'] is True and report['verification']['readyForSubmission'] is False
                assert report['report']['generation']=={'method':'deterministic_template','llmCalled':False}
                checked=call('/api/review/verify',{'reviewId':report['reviewId']});assert checked['verification']['valid'] is True
                for fact_id in ['target.amountPaid','target.timestamp','rule.score']:
                    resolved=call('/api/review/fact',{'reviewId':report['reviewId'],'factId':fact_id})
                    assert all(source[row['txId']]==row for row in resolved['transactions']);fact_count+=1
                for citation in report['report']['citations']:
                    resolved=call('/api/review/citation?'+urlencode({'reviewId':report['reviewId'],'chunkId':citation['chunkId']}))
                    assert resolved['sourceVerification']['status']=='snapshot_checked';citation_count+=1
            missing=expected-{row['txId'] for row in payload['graph']['edges']}
            assert not missing or payload['graph']['meta']['truncated'] is True
            if pages:assert payload['graph']['meta']['truncated']==bool(payload['accountExpansion']['candidates'] or len(payload['graph']['edges'])>=1000)
            results.append({'txId':case['txId'],'hops':hops,'windowDays':days,'initialTransactions':len(before['graph']['edges']),
                            'finalTransactions':len(payload['graph']['edges']),'loadedReferenceTransactions':len(expected),
                            'remainingLoadedTransactions':len(missing),'pages':pages,'budgetReached':payload['accountExpansion']['budgetReached']})
    db.begin();assert fingerprint(source.values())==fingerprint(row['edge'] for row in db.query(ALL_ROWS,{'limit':20001}))
    report={'checkedAt':datetime.now(timezone.utc).isoformat(),'scope':'bounded immutable loaded synthetic subset; not AML recall or full-ledger completeness',
            'sourceSha256':fingerprint(source.values()),'sourceTransactions':len(source),'conditions':len(results),'expandedConditions':sum(bool(row['pages']) for row in results),
            'pages':len(page_times),'maxPageMs':max(page_times,default=0),'meanPageMs':round(sum(page_times)/len(page_times),3) if page_times else 0,
            'verifiedNewReviews':review_count,'checkedFacts':fact_count,'checkedCitations':citation_count,'rejectedStaleOperations':stale_count,'idempotentReplays':replay_count,
            'databaseWrites':0,'llmCalls':0,'engineSha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['graph-viewer/account_expansion.py','graph-viewer/server.py','graph-viewer/review_bridge.py']},'results':results}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key not in {'engineSha256','results'}},ensure_ascii=False))


if __name__=='__main__':main()
