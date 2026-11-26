"""Verify pinned Spring Judge results, expansion invalidation and offline archives.

Default: actual local Neo4j and corpus, existing Judge adapter with controlled
provider replies (no paid calls). --live-rag-url uses an already configured RAG
process for one real Judge request; credentials are never read by this script.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError

SOURCE = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,default=SOURCE)
    parser.add_argument('--jar',type=Path,default=SOURCE/'backend/build/libs/traceon-0.0.1-SNAPSHOT.jar')
    parser.add_argument('--java',type=Path)
    parser.add_argument('--port',type=int,default=18081)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--live-rag-url',help='Existing loopback RAG process with locally configured OPENAI_API_KEY and OPENAI_MODEL')
    args=parser.parse_args();runtime=args.runtime_root.resolve();output=args.output.resolve();db=output.with_suffix('.sqlite3')
    if output.exists() or db.exists():raise SystemExit('Choose a new output name; existing results and archives are preserved.')
    if not 1024<=args.port<=65535:raise SystemExit('Choose a port from 1024 to 65535.')
    java=(args.java or runtime/'.tools/jdk-21.0.1/bin/java.exe').resolve();jar=args.jar.resolve()
    if not java.is_file() or not jar.is_file():raise SystemExit('Build bootJar and supply a Java 21 executable.')
    output.parent.mkdir(parents=True,exist_ok=True)
    sys.path[:0]=[str(SOURCE/'rag-service')]
    from retrieval import Index
    from rag_server import handler_for
    from agent_judge import openai_judge

    def call(path,body=None,expected=200):
        connection=http.client.HTTPConnection('127.0.0.1',args.port,timeout=90)
        try:
            connection.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
            response=connection.getresponse();result=json.loads(response.read())
            assert response.status==expected,(path,response.status,expected,result.get('error'))
            return result
        finally:connection.close()

    @contextmanager
    def http_service(handler):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:yield 'http://127.0.0.1:'+str(server.server_port)
        finally:server.shutdown();server.server_close();worker.join(5)

    @contextmanager
    def spring(rag_url,offline=False):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1',args.port))==0:raise RuntimeError('Verification port is in use; choose another --port.')
        argv=[str(java),'-jar',str(jar),f'--server.port={args.port}','--server.address=127.0.0.1',
              f'--spring.config.import=optional:file:{(runtime/".env").as_posix()}[.properties]',
              f'--traceon.manifest={(runtime/"data/processed/HI-Small-MVP/manifest.json").as_posix()}',
              f'--traceon.history-db={db.as_posix()}',f'--traceon.rag-url={rag_url}','--management.health.neo4j.enabled=false']
        if offline:argv += [f'--traceon.neo4j-http-url={rag_url}',f'--traceon.rule-url={rag_url}/rules','--spring.neo4j.uri=bolt://127.0.0.1:1']
        env=dict(os.environ)
        # Spring never needs the provider credentials, even in the mock test.
        env.pop('OPENAI_API_KEY',None);env.pop('OPENAI_MODEL',None)
        with output.with_suffix('.offline.log' if offline else '.spring.log').open('x',encoding='utf-8') as log:
            process=subprocess.Popen(argv,cwd=SOURCE/'backend',env=env,stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+50
                while True:
                    if process.poll() is not None:raise RuntimeError('Spring startup failed; inspect the separate log.')
                    try:call('/api/history' if offline else '/api/health');break
                    except (OSError,AssertionError):
                        if time.monotonic()>=deadline:raise RuntimeError('Spring readiness timed out.')
                        time.sleep(.25)
                yield
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

    scenario={'name':'passed'};attempts=[];entered=threading.Event();release=threading.Event()
    def transport(request,timeout):
        mode=scenario['name'];attempts.append(mode)
        payload=json.loads(json.loads(request.data)['input'][1]['content'])
        if mode=='block':entered.set();assert release.wait(15)
        verdict='insufficient' if mode=='held' else 'contradicted' if mode=='rejected' else 'supported'
        items=[{'itemId':item['itemId'],'verdict':verdict,'factIds':[payload['facts'][0]['factId']],
                'chunkIds':[payload['sources'][0]['chunkId']],'reason':'실제 조회에 대한 통합 연결용 모의 판정'} for item in payload['items']]
        if mode=='not_judged':items[0]['factIds']=['invalid-evidence-reference']
        return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'items':items})}]}]}).encode())

    records=[];initial_statuses=[];offline_calls=[];stale_rejections=0
    class Denied(BaseHTTPRequestHandler):
        def do_GET(self):offline_calls.append(self.path);self.send_response(503);self.end_headers()
        def do_POST(self):self.do_GET()
        def log_message(self,*args):pass

    def new_review():
        payload=call('/api/graph?txId=TX-000619283&hops=2&windowDays=7');assert payload['summary']['transactions']==69
        prepared=call('/api/review/prepare',{'snapshotId':payload['reviewSnapshotId'],'findingIndex':0})
        assert prepared['judge'] is None and prepared['judgeState']=='not_requested'
        return payload,prepared
    def save(payload,prepared):
        request={'snapshotId':payload['reviewSnapshotId'],'reviewId':prepared['reviewId']}
        saved=call('/api/history/save',request);history_id=saved['history']['historyId']
        opened=call('/api/history/open?historyId='+history_id)
        assert call('/api/history/save',request)['created'] is False
        records.append({'id':history_id,'opened':opened});return opened

    @contextmanager
    def rag_service():
        if args.live_rag_url:
            from urllib.request import urlopen
            from urllib.parse import urlsplit
            url=args.live_rag_url.rstrip('/');parsed=urlsplit(url)
            if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password:
                raise SystemExit('Use a loopback RAG URL without embedded credentials.')
            with urlopen(url+'/health',timeout=10) as response:health=json.load(response)
            if not health.get('judgeModes',{}).get('configured'):
                raise SystemExit('Start the updated RAG service from a shell with OPENAI_API_KEY and OPENAI_MODEL set locally.')
            yield url
        else:
            os.environ['OPENAI_API_KEY']='integration-test-only-token'
            os.environ['OPENAI_MODEL']='integration-mock-model'
            handler=handler_for(Index(runtime/'data/rag/index.json'),judge=lambda payload:openai_judge(payload,transport))
            class Quiet(handler):
                def log_message(self,*args):pass
            with http_service(Quiet) as url:yield url

    with rag_service() as rag_url:
        with spring(rag_url):
            modes=['live'] if args.live_rag_url else ['passed','held','rejected','not_judged']
            for mode in modes:
                scenario['name']=mode;payload,prepared=new_review();id=prepared['reviewId']
                judged=call('/api/review/judge',{'reviewId':id});judge=judged['judge'];count=len(attempts)
                assert judged['snapshotId']==payload['reviewSnapshotId'] and judge['identity']['reportId']==prepared['report']['reportId']
                assert judge['identity']['graphSha256']==prepared['context']['graphSha256']
                if mode!='live':assert judge['decisionStatus']==mode
                cached=call('/api/review/judge',{'reviewId':id});assert cached['cached'] is True and cached['judge']==judge
                assert len(attempts)==count
                assert call('/api/review/judge?reviewId='+id)['judge']==judge
                opened=save(payload,prepared);assert opened['review']['judge']==judge
                initial_statuses.append({'decisionStatus':judge['decisionStatus'],'reasonCode':judge['reasonCode'],
                                         'verificationScore':judge['verificationScore'],'evaluationCompleted':judge['evaluationCompleted']})
            # Same graph, fixed window and hops; old review and Judge become inaccessible.
            candidate=next(c for c in payload['accountExpansion']['candidates'] if c['canExpand'])
            expanded=call('/api/graph/expand',{'snapshotId':payload['reviewSnapshotId'],'accountKey':candidate['accountKey']})
            assert expanded['summary']['transactions']==72
            for path,body in [('/api/review/judge?reviewId='+id,None),('/api/review/judge',{'reviewId':id}),
                              ('/api/history/save',{'snapshotId':payload['reviewSnapshotId'],'reviewId':id})]:
                call(path,body,409);stale_rejections+=1
            fresh=call('/api/review/prepare',{'snapshotId':expanded['reviewSnapshotId'],'findingIndex':0})
            assert fresh['judge'] is None and fresh['judgeState']=='not_requested'
            if not args.live_rag_url:
                scenario['name']='passed';call('/api/review/judge',{'reviewId':fresh['reviewId']})
            save(expanded,fresh)
            if not args.live_rag_url:
                # Expansion may complete while the provider is working; late result is discarded.
                scenario['name']='block';race_payload,race_review=new_review();result=[]
                worker=threading.Thread(target=lambda:result.append(call('/api/review/judge',{'reviewId':race_review['reviewId']},409)));worker.start()
                try:
                    assert entered.wait(10)
                    assert call('/api/review/judge?reviewId='+race_review['reviewId'])['judgeState']=='running'
                    call('/api/history/save',{'snapshotId':race_payload['reviewSnapshotId'],'reviewId':race_review['reviewId']},409)
                    candidate=next(c for c in race_payload['accountExpansion']['candidates'] if c['canExpand'])
                    call('/api/graph/expand',{'snapshotId':race_payload['reviewSnapshotId'],'accountKey':candidate['accountKey']})
                finally:release.set();worker.join(15)
                assert len(result)==1;stale_rejections+=1
        with http_service(Denied) as denied,spring(denied,offline=True):
            assert call('/api/history')['total']==len(records)
            for record in records:
                opened=call('/api/history/open?historyId='+record['id']);assert opened==record['opened']
                assert opened['history']['integrity']['currentSourcesChecked'] is False
                assert opened['history']['humanReviewRequired'] is True and opened['history']['readyForSubmission'] is False
                assert call('/api/history/verify?historyId='+record['id'])['history']['integrity']['valid'] is True
                archive=call('/api/history/download?historyId='+record['id']+'&format=json')
                assert archive['capture']['review'].get('judge')==opened['review'].get('judge')
            assert offline_calls==[]
    report={'checkedAt':datetime.now(timezone.utc).isoformat(),'baseCommit':'23e54a9',
            'mode':'live' if args.live_rag_url else 'mock_provider_over_actual_neo4j_and_rag',
            'initialTransactions':69,'expandedTransactions':72,'judgeResults':initial_statuses,
            'savedAndReopenedAfterRestart':len(records),'offlineUpstreamRequests':len(offline_calls),
            'staleJudgeRejections':stale_rejections,'mockProviderRequests':len(attempts),
            'liveJudgeRequests':1 if args.live_rag_url else 0,'neo4jWrites':0,
            'scope':'snapshot attribution and persistence, not Judge quality evaluation'}
    with output.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2);stream.write('\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
