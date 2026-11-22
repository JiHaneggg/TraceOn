"""Archive real local demo analyses, reopen offline, and preserve audit evidence."""
import argparse
from contextlib import contextmanager,closing
from datetime import datetime,timezone
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
from time import perf_counter
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'graph-viewer'),str(ROOT/'data-pipeline')]
import server as viewer
from history_store import HistoryStore
from review_bridge import ReviewBridge


class NoRag:
    def call(self,*args,**kwargs):raise AssertionError('Offline history must not access RAG')


def no_database():raise AssertionError('Offline history must not access Neo4j')


@contextmanager
def service(store,offline=False):
    handler=viewer.handler_for(viewer.read_targets(viewer.MANIFEST),no_database if offline else viewer.ReadOnlyDatabase,
        review_bridge=ReviewBridge(NoRag()) if offline else ReviewBridge(),history_store=store)
    class Quiet(handler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Quiet);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def call(path,body=None,expected=200):
        connection=http.client.HTTPConnection(*server.server_address,timeout=45)
        try:
            connection.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),{'Content-Type':'application/json'})
            response=connection.getresponse();raw=response.read();assert response.status==expected,(path,response.status,expected,raw[:120])
            return raw.decode() if response.headers['Content-Type'].startswith('text/markdown') else json.loads(raw)
        finally:connection.close()
    try:yield call
    finally:server.shutdown();server.server_close();thread.join(5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/graph-viewer/history-verification-20261118.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Use a new output name; earlier evidence is preserved.')
    db_path=args.output.with_suffix('.sqlite3')
    if db_path.exists():raise SystemExit('Use a new output name; archive database already exists.')
    store=HistoryStore(db_path);records=[];saved_times=[]
    cases=['TX-000619283','TX-005034034','TX-000988912']
    with service(store) as call:
        for tx in cases:
            payload=call('/api/graph?'+urlencode({'txId':tx,'hops':2,'windowDays':7}))
            graph_only=call('/api/history/save',{'snapshotId':payload['reviewSnapshotId'],'reviewId':None})
            records.append({'id':graph_only['history']['historyId'],'opened':call('/api/history/open?historyId='+graph_only['history']['historyId'])})
            candidates=[row for row in payload['accountExpansion']['candidates'] if row['canExpand']]
            if candidates:
                payload=call('/api/graph/expand',{'snapshotId':payload['reviewSnapshotId'],'accountKey':candidates[0]['accountKey']})
                assert payload['accountExpansion']['pageHistory']==[payload['accountExpansion']['lastPage']]
            findings=payload['ruleResult']['findings'];review=call('/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':0 if findings else None})
            started=perf_counter();saved=call('/api/history/save',{'snapshotId':payload['reviewSnapshotId'],'reviewId':review['reviewId']});saved_times.append(round((perf_counter()-started)*1000,3))
            repeated=call('/api/history/save',{'snapshotId':payload['reviewSnapshotId'],'reviewId':review['reviewId']})
            assert repeated['created'] is False and saved['history']==repeated['history']
            id=saved['history']['historyId'];opened=call('/api/history/open?historyId='+id)
            assert opened['payload']['graph']==payload['graph'];assert opened['payload']['ruleResult']==payload['ruleResult']
            assert opened['review']['report']==review['report'] and opened['review']['context']==review['context']
            records.append({'id':id,'opened':opened})
        assert call('/api/history')['total']==len(records)==6
    # New service, new bridge, new SQLite connections. Both live dependencies deny access.
    checked_facts=checked_citations=downloads=0
    with service(HistoryStore(db_path),offline=True) as call:
        assert {row['historyId'] for row in call('/api/history')['items']}=={row['id'] for row in records}
        for record in records:
            id=record['id'];opened=call('/api/history/open?historyId='+id);assert opened==record['opened']
            assert call('/api/history/verify?historyId='+id)['history']['integrity']['currentSourcesChecked'] is False
            assert opened['history']['readyForSubmission'] is False
            bundle=call('/api/history/download?'+urlencode({'historyId':id,'format':'json'}));downloads+=1
            assert bundle['capture']['payload']['graph']==opened['payload']['graph']
            assert bundle['snapshotSha256']==opened['history']['snapshotSha256']
            review=opened['review']
            if review:
                by_id={row['txId']:row for row in opened['payload']['graph']['edges']}
                for fact in review['context']['reportFacts']:
                    resolved=call('/api/history/fact?'+urlencode({'historyId':id,'factId':fact['factId']}))
                    assert resolved['fact']==fact and all(by_id[row['txId']]==row for row in resolved['transactions']);checked_facts+=1
                for citation in review['report']['citations']:
                    resolved=call('/api/history/citation?'+urlencode({'historyId':id,'chunkId':citation['chunkId']}))
                    start,end=resolved['section']['highlight']['start'],resolved['section']['highlight']['end']
                    assert resolved['section']['text'][start:end]==citation['text'];assert resolved['citation']['sourceSha256']==citation['sourceSha256'];checked_citations+=1
                markdown=call('/api/history/download?'+urlencode({'historyId':id,'format':'markdown'}));assert review['report']['markdown'] in markdown;downloads+=1
    # Corruption is exercised in a distinct backup; the verified archive is retained.
    corrupt=db_path.with_stem(db_path.stem+'-corrupt')
    with closing(sqlite3.connect(db_path)) as source,closing(sqlite3.connect(corrupt)) as destination:source.backup(destination)
    with closing(sqlite3.connect(corrupt)) as destination:destination.execute('UPDATE analysis_history SET body=? WHERE id=?',(b'corrupt-fixture',records[0]['id']));destination.commit()
    with service(HistoryStore(corrupt),offline=True) as call:
        for operation in ['open','verify','download']:
            call('/api/history/'+operation+'?'+urlencode({'historyId':records[0]['id'],**({'format':'json'} if operation=='download' else {})}),expected=409)
    report={'checkedAt':datetime.now(timezone.utc).isoformat(),'savedAnalyses':len(records),'savedFullReviews':3,
            'reopenedAfterServiceRestart':len(records),'offlineFactChecks':checked_facts,'offlineCitationChecks':checked_citations,'offlineDownloads':downloads,
            'idempotentSaves':3,'rejectedCorruptReads':3,'neo4jWrites':0,'llmCalls':0,'saveMilliseconds':saved_times,
            'scope':'local archived synthetic demo snapshots, not current-source/legal/semantic approval',
            'engineSha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['graph-viewer/server.py','graph-viewer/analysis_history.py','graph-viewer/history_store.py','graph-viewer/account_expansion.py']},
            'records':[{'historyId':r['id'],**r['opened']['history']} for r in records]}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key not in {'engineSha256','records'}},ensure_ascii=False))


if __name__=='__main__':main()
