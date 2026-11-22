"""Loopback-only graph demonstration with immutable local analysis history.

Ledger access uses fixed read templates and manifest-listed demo transactions.
SQLite stores selected analysis snapshots; production access stays in Spring.
Credentials remain server-side; no arbitrary query or ledger write is exposed.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
from decimal import Decimal,localcontext
import json
from pathlib import Path
import re
import sys
import threading
from time import perf_counter
from urllib.error import URLError
from urllib.parse import parse_qs,urlsplit
from urllib.request import Request,urlopen
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(Path(__file__).parent),str(ROOT/'data-pipeline'),str(ROOT/'rule-service')]
from neo4j_mvp import Database,TARGET_QUERY,NEIGHBOR_QUERY,PROJECTION
from rules import score
from review_bridge import ReviewBridge,ReviewError
from account_expansion import ExpansionManager,read_version,VERSION_QUERY,VERIFY_QUERY,PAGE_QUERY
from analysis_history import AnalysisHistory
from history_store import HistoryStore

MANIFEST=ROOT/'data/processed/HI-Small-MVP/manifest.json'
STATIC=Path(__file__).parent/'static'
LIST_QUERY=f'''MATCH (s:Account)-[t:TRANSFER]->(d:Account)
WHERE t.txId IN $ids RETURN {PROJECTION} AS edge ORDER BY t.txId'''
MARKER_QUERY='MATCH (m:TraceOnDataset) RETURN m.name AS name'
HEALTH_QUERY='RETURN 1 AS connected'
READ_QUERIES={TARGET_QUERY,NEIGHBOR_QUERY,LIST_QUERY,MARKER_QUERY,HEALTH_QUERY,VERSION_QUERY,VERIFY_QUERY,PAGE_QUERY}
ASSETS={'/':'index.html','/index.html':'index.html','/app.css':'app.css','/app.mjs':'app.mjs',
        '/review.css':'review.css',
        '/graph.mjs':'graph.mjs','/format.mjs':'format.mjs','/flow.mjs':'flow.mjs',
        '/review.mjs':'review.mjs','/review-format.mjs':'review-format.mjs','/vendor/cytoscape.min.js':'vendor/cytoscape.min.js'}
ASSETS.update({'/expansion-format.mjs':'expansion-format.mjs','/expansion.css':'expansion.css'})
ASSETS.update({'/history.mjs':'history.mjs','/history-format.mjs':'history-format.mjs','/history.css':'history.css'})


class ReadOnlyDatabase(Database):
    def __init__(self):
        super().__init__()
        self.deadline=perf_counter()+20
        self.neighbor_reads={}

    def query(self,statement,parameters=None):
        if statement not in READ_QUERIES:raise ValueError('Only fixed read templates are allowed')
        if statement==PAGE_QUERY and (type((parameters or {}).get('limit')) is not int or not 1<=parameters['limit']<=26):raise ValueError('Invalid page limit')
        if statement==VERIFY_QUERY and (type((parameters or {}).get('limit')) is not int or not 1<=parameters['limit']<=1001 or not isinstance(parameters.get('ids'),list) or len(parameters['ids'])>1000):raise ValueError('Invalid source verification limit')
        remaining=self.deadline-perf_counter()
        if remaining<=0:raise TimeoutError('Graph lookup deadline reached')
        payload=json.dumps({'statements':[{'statement':statement,'parameters':parameters or {}}]}).encode()
        request=Request(self.url,data=payload,headers={'Content-Type':'application/json','Authorization':self.authorization})
        with urlopen(request,timeout=min(8,remaining)) as response:raw=response.read(4_000_001)
        if len(raw)>4_000_000:raise ValueError('Read response too large')
        result=json.loads(raw)
        if result['errors']:raise RuntimeError('Database rejected read query')
        block=result['results'][0]
        rows=[dict(zip(block['columns'],row['row'])) for row in block['data']]
        if statement==NEIGHBOR_QUERY:self.neighbor_reads[parameters['accountKey']]=[row['edge'] for row in rows]
        return rows


def read_targets(path):
    path=Path(path)
    if path.stat().st_size>1_000_000:raise ValueError('Manifest too large')
    targets=json.loads(path.read_bytes())['targets']
    if (not isinstance(targets,list) or not 1<=len(targets)<=100 or len(set(targets))!=len(targets)
            or any(not isinstance(t,str) or not re.fullmatch(r'TX-\d{9}',t) for t in targets)):
        raise ValueError('Invalid demo target list')
    return targets


def currency_totals(edges):
    paid={};received={}
    with localcontext() as ctx:
        ctx.prec=80
        for edge in edges:
            for totals,currency,amount in [(paid,edge['paymentCurrency'],edge['amountPaid']),
                                          (received,edge['receivingCurrency'],edge['amountReceived'])]:
                value=Decimal(amount)
                if not value.is_finite() or value<0:raise ValueError('Invalid source amount')
                totals[currency]=totals.get(currency,Decimal(0))+value
        return {'grossPaid':{k:format(v,'f') for k,v in sorted(paid.items())},
                'grossReceived':{k:format(v,'f') for k,v in sorted(received.items())}}


def graph_payload(database,tx_id,*,hops=2,window_days=7):
    graph=database.subgraph(tx_id,hops=hops,window_days=window_days,max_neighbors=50,max_edges=1000)
    return render_graph_payload(graph)


def render_graph_payload(graph):
    tx_id=graph['targetTxId']
    rules=score(graph)
    ids={edge['txId'] for edge in graph['edges']}
    highlights=sorted({tx for finding in rules['findings'] for tx in finding['evidenceTxIds']})
    if not set(highlights)<=ids:raise ValueError('Rule evidence is outside the source graph')
    target=next(edge for edge in graph['edges'] if edge['txId']==tx_id)
    return {'schemaVersion':'1.0','graph':graph,'target':target,'ruleResult':rules,'highlightTxIds':highlights,
            'summary':{'accounts':len(graph['nodes']),'transactions':len(graph['edges']),
                       'evidenceTransactions':len(highlights),'patterns':len(rules['findings']),**currency_totals(graph['edges'])},
            'source':{'kind':'neo4j','queriedAt':datetime.now(timezone.utc).isoformat(),
                      'scope':'selected_demo_subset','sourceGraphComplete':bool(graph['meta'].get('sourceGraphComplete',False)),
                      'readOnly':True,'ruleScoreIsProbability':False},
            'notice':'시연용 부분 데이터의 관찰입니다. 룰 점수는 확률이 아니며 범죄·정상 거래를 확정하지 않습니다.'}


def parse_graph_request(query,targets):
    params=parse_qs(query,strict_parsing=True,keep_blank_values=True,max_num_fields=3)
    if set(params)-{'txId','hops','windowDays'} or any(len(v)!=1 for v in params.values()):raise ValueError('Invalid query parameters')
    tx_id=params.get('txId',[''])[0]
    if tx_id not in targets:raise ValueError('Select a listed demo transaction')
    def bounded(name,default,allowed):
        value=params.get(name,[default])[0]
        if value not in allowed:raise ValueError('Unsupported graph range')
        return int(value)
    return tx_id,bounded('hops','2',{'1','2','3'}),bounded('windowDays','7',{'1','3','7'})


def handler_for(targets,database_factory=ReadOnlyDatabase,static_dir=STATIC,review_bridge=None,history_store=None):
    targets=tuple(targets);gate=threading.BoundedSemaphore(2)
    reviews=review_bridge or ReviewBridge();review_gate=threading.BoundedSemaphore(2)
    expansions=ExpansionManager(reviews,render_graph_payload)
    history=AnalysisHistory(reviews,history_store or HistoryStore(ROOT/'data/history/analysis.sqlite3'))
    history_gate=threading.BoundedSemaphore(1)
    class Handler(BaseHTTPRequestHandler):
        def reply(self,status,body,content_type='application/json; charset=utf-8',download_name=None):
            data=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            if download_name:self.send_header('Content-Disposition','attachment; filename="'+download_name+'"')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(data)

        def valid_host(self):
            try:
                host=urlsplit('http://'+self.headers.get('Host',''))
                return (host.hostname in {'127.0.0.1','localhost'} and host.port==self.server.server_port
                        and host.username is None and host.password is None)
            except ValueError:return False

        def do_POST(self):
            if not self.valid_host():return self.reply(421,{'error':'invalid_host'})
            origin=self.headers.get('Origin')
            if self.headers.get('Sec-Fetch-Site')=='cross-site' or (origin and origin!='http://'+self.headers.get('Host','')):
                return self.reply(403,{'error':'same_origin_required'})
            operations={'/api/review/prepare':{'snapshotId','findingIndex'},
                        '/api/review/fact':{'reviewId','factId'},'/api/review/verify':{'reviewId'},
                        '/api/history/save':{'snapshotId','reviewId'},
                        '/api/graph/expand':{'snapshotId','accountKey'}}
            if self.path not in operations:return self.reply(405,{'error':'read_only','message':'거래 DB는 조회 전용입니다.'})
            if self.headers.get_content_type()!='application/json' or self.headers.get('Transfer-Encoding'):
                return self.reply(400,{'error':'invalid_body','message':'JSON 요청만 사용할 수 있습니다.'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=2048:return self.reply(413,{'error':'body_size'})
                def unique_object(pairs):
                    result={}
                    for key,value in pairs:
                        if key in result:raise ValueError('Duplicate field')
                        result[key]=value
                    return result
                def invalid_constant(_):raise ValueError('Non-finite JSON')
                self.connection.settimeout(5)
                body=json.loads(self.rfile.read(length),object_pairs_hook=unique_object,parse_constant=invalid_constant)
                if not isinstance(body,dict) or set(body)!=operations[self.path]:raise ValueError('Invalid operation fields')
                if 'factId' in body and (not isinstance(body['factId'],str) or not 1<=len(body['factId'])<=250):raise ValueError('Invalid fact')
                if 'accountKey' in body and (not isinstance(body['accountKey'],str) or not 1<=len(body['accountKey'])<=200):raise ValueError('Invalid account')
            except (ValueError,TypeError,TimeoutError,OSError):return self.reply(400,{'error':'invalid_body','message':'현재 화면의 조회 ID로 다시 요청해 주세요.'})
            operation_gate=gate if self.path=='/api/graph/expand' else history_gate if self.path=='/api/history/save' else review_gate
            if not operation_gate.acquire(blocking=False):return self.reply(429,{'error':'busy','message':'조회 중입니다. 잠시 후 다시 시도해 주세요.'})
            try:
                if self.path=='/api/history/save':result=history.save(body['snapshotId'],body['reviewId'])
                elif self.path=='/api/graph/expand':result=expansions.expand(body['snapshotId'],body['accountKey'],database_factory())
                elif self.path.endswith('/prepare'):result=reviews.prepare(body['snapshotId'],body['findingIndex'])
                elif self.path.endswith('/fact'):result=reviews.fact(body['reviewId'],body['factId'])
                else:result=reviews.verify(body['reviewId'])
                return self.reply(200,result)
            except ReviewError as error:return self.reply(error.status,{'error':error.code,'message':error.message})
            except (ValueError,KeyError,TypeError,RuntimeError,OSError,ArithmeticError):
                if self.path=='/api/history/save':return self.reply(503,{'error':'history_unavailable','message':'분석을 저장하지 못했습니다. 저장 이력을 확인한 뒤 다시 시도해 주세요.'})
                expanding=self.path=='/api/graph/expand'
                return self.reply(503,{'error':'expansion_unavailable' if expanding else 'review_unavailable',
                    'message':'추가 거래 조회를 완료하지 못했습니다. 기존 그래프를 유지했습니다. 잠시 후 다시 시도해 주세요.' if expanding else '근거와 초안을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.'})
            finally:operation_gate.release()

        def do_GET(self):
            if not self.valid_host():return self.reply(421,{'error':'invalid_host'})
            if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.reply(403,{'error':'same_origin_required'})
            parsed=urlsplit(self.path)
            if parsed.path.startswith('/api/history'):
                allowed={'/api/history':set(),'/api/history/open':{'historyId'},'/api/history/verify':{'historyId'},
                         '/api/history/citation':{'historyId','chunkId'},'/api/history/fact':{'historyId','factId'},
                         '/api/history/download':{'historyId','format'}}
                if parsed.path not in allowed:return self.reply(404,{'error':'not_found'})
                try:
                    params=parse_qs(parsed.query,strict_parsing=True,keep_blank_values=True,max_num_fields=2)
                    if any(len(v)!=1 or not v[0] for v in params.values()):raise ValueError('Invalid history fields')
                    if parsed.path=='/api/history':
                        if set(params)-{'cursor'}:raise ValueError('Unexpected history fields')
                    elif set(params)!=allowed[parsed.path]:raise ValueError('Missing history fields')
                    args={key:value[0] for key,value in params.items()}
                    if any(len(value)>250 for value in args.values()):raise ValueError('History field too long')
                except ValueError:return self.reply(400,{'error':'invalid_request','message':'저장 목록에서 분석을 다시 선택해 주세요.'})
                if not history_gate.acquire(blocking=False):return self.reply(429,{'error':'history_busy','message':'저장본을 확인 중입니다. 잠시 후 다시 시도해 주세요.'})
                try:
                    if parsed.path=='/api/history':result=history.listing(args.get('cursor'))
                    elif parsed.path.endswith('/open'):result=history.open(args['historyId'])
                    elif parsed.path.endswith('/verify'):result=history.verify(args['historyId'])
                    elif parsed.path.endswith('/citation'):result=history.citation(args['historyId'],args['chunkId'])
                    elif parsed.path.endswith('/fact'):result=history.fact(args['historyId'],args['factId'])
                    else:
                        name,data,mime=history.download(args['historyId'],args['format'])
                        return self.reply(200,data,mime+'; charset=utf-8',download_name=name)
                    return self.reply(200,result)
                except ReviewError as error:return self.reply(error.status,{'error':error.code,'message':error.message})
                except (ValueError,KeyError,TypeError,RuntimeError,OSError,ArithmeticError):return self.reply(503,{'error':'history_unavailable','message':'저장본을 확인하지 못했습니다. 기존 이력은 변경하지 않았습니다.'})
                finally:history_gate.release()
            if parsed.path=='/api/review/download':
                try:
                    params=parse_qs(parsed.query,strict_parsing=True,keep_blank_values=True,max_num_fields=2)
                    if set(params)!={'reviewId','format'} or any(len(values)!=1 for values in params.values()):raise ValueError('Invalid download fields')
                except ValueError:return self.reply(400,{'error':'invalid_request'})
                if not review_gate.acquire(blocking=False):return self.reply(429,{'error':'busy','message':'초안 재검사 중입니다. 잠시 후 다시 시도해 주세요.'})
                try:
                    name,data,mime=reviews.download(params['reviewId'][0],params['format'][0])
                    return self.reply(200,data,mime+'; charset=utf-8',download_name=name)
                except ReviewError as error:return self.reply(error.status,{'error':error.code,'message':error.message})
                except (ValueError,KeyError,TypeError,RuntimeError,OSError):return self.reply(503,{'error':'download_unavailable','message':'초안 내려받기를 준비하지 못했습니다.'})
                finally:review_gate.release()
            if parsed.path=='/api/review/citation':
                try:
                    params=parse_qs(parsed.query,strict_parsing=True,keep_blank_values=True,max_num_fields=2)
                    if set(params)!={'reviewId','chunkId'} or any(len(values)!=1 for values in params.values()):raise ValueError('Invalid citation fields')
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',params['chunkId'][0]):raise ValueError('Invalid chunk')
                except ValueError:return self.reply(400,{'error':'invalid_request'})
                if not review_gate.acquire(blocking=False):return self.reply(429,{'error':'busy','message':'근거 조회 중입니다. 잠시 후 다시 시도해 주세요.'})
                try:return self.reply(200,reviews.citation(params['reviewId'][0],params['chunkId'][0]))
                except ReviewError as error:return self.reply(error.status,{'error':error.code,'message':error.message})
                except (ValueError,KeyError,TypeError,RuntimeError,OSError):return self.reply(503,{'error':'citation_unavailable','message':'원문을 확인하지 못했습니다. 다시 시도해 주세요.'})
                finally:review_gate.release()
            if parsed.path in ASSETS and not parsed.query:
                path=Path(static_dir)/ASSETS[parsed.path]
                if not path.is_file():return self.reply(404,{'error':'asset_missing'})
                suffix=path.suffix
                mime={'.html':'text/html','.css':'text/css','.mjs':'text/javascript','.js':'text/javascript'}[suffix]
                return self.reply(200,path.read_bytes(),mime+'; charset=utf-8')
            if parsed.path not in {'/api/health','/api/cases','/api/graph'}:return self.reply(404,{'error':'not_found'})
            try:
                options=parse_graph_request(parsed.query,targets) if parsed.path=='/api/graph' else None
                if options is None and parsed.query:raise ValueError('Unexpected query')
            except ValueError:return self.reply(400,{'error':'invalid_request','message':'거래와 조회 범위를 다시 선택해 주세요.'})
            if not gate.acquire(blocking=False):return self.reply(429,{'error':'busy','message':'조회 중입니다. 잠시 후 다시 시도해 주세요.'})
            try:
                database=database_factory()
                if parsed.path=='/api/health':
                    database.query(HEALTH_QUERY)
                    return self.reply(200,{'service':'traceon-graph-viewer','status':'ok','database':'connected','readOnly':True})
                if parsed.path=='/api/cases':
                    rows=database.query(LIST_QUERY,{'ids':list(targets)})
                    by_id={row['edge']['txId']:row['edge'] for row in rows}
                    if set(by_id)!=set(targets) or len(rows)!=len(targets):raise RuntimeError('Demo transactions are missing or duplicated')
                    return self.reply(200,{'schemaVersion':'1.0','items':[by_id[tx] for tx in targets],
                        'source':'neo4j','scope':'selected_demo_subset','total':len(targets)})
                tx_id,hops,days=options
                before=read_version(database)
                return self.reply(200,expansions.initial(database,graph_payload(database,tx_id,hops=hops,window_days=days),before))
            except ReviewError as error:
                self.reply(error.status,{'error':error.code,'message':error.message})
            except (URLError,TimeoutError,OSError,RuntimeError):
                self.reply(503,{'error':'database_unavailable','message':'Neo4j 조회를 완료하지 못했습니다. DB 실행 상태를 확인하거나 잠시 후 다시 조회해 주세요.'})
            except (ValueError,KeyError,TypeError,ArithmeticError):
                self.reply(503,{'error':'graph_unavailable','message':'거래 데이터와 조회 범위를 확인하지 못했습니다. 시연 데이터 준비 상태를 확인해 주세요.'})
            finally:gate.release()
    return Handler


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8093)
    parser.add_argument('--manifest',type=Path,default=MANIFEST)
    parser.add_argument('--history-db',type=Path,default=ROOT/'data/history/analysis.sqlite3',help='Local immutable analysis archive')
    args=parser.parse_args()
    targets=read_targets(args.manifest)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(targets,history_store=HistoryStore(args.history_db)))
    print(f'TraceOn graph viewer: http://127.0.0.1:{server.server_port}',flush=True)
    server.serve_forever()
