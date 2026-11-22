"""Audit exact source values, bounded-path omission and measured read latency.

Only the already loaded small demo subset is read. No DB writes, rule tuning,
LLM calls or authentication details are saved. Existing results are preserved.
"""
import argparse
import csv
from datetime import datetime,timedelta,timezone
import hashlib
from itertools import islice
import json
from pathlib import Path
import sys
from time import perf_counter
from collections import defaultdict

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'data-pipeline'),str(ROOT/'rule-service')]
from graph_contract import MemoryGraph
from graph_diagnostics import (AuditDatabase,MAX_SOURCE_ROWS,COUNT,ACCOUNT_COUNT,ALL_ROWS,INDEXES,VERSION,
                               TARGET_QUERY,NEIGHBOR_QUERY,source_map,fingerprint,audit_graph,latency_summary,plan_summary)
from rules import score


def build_markdown(report):
    summary=report['summary']
    lines=['# Neo4j 조회 정합성·누락·지연 점검','',
           '현재 적재된 합성 시연 자료만 비교합니다. 전체 IBM 원장·탐지 Recall·운영 부하 시험이 아닙니다.','',
           f"- 원본/DB 거래 {report['source']['transactions']}건·계좌 {report['source']['accounts']}개 일치",
           f"- {len(report['cases'])//len(report['scopes'])}개 대상 × {len(report['scopes'])}조건 = {len(report['cases'])}개 조합, 조합별 {report['repeats']}회 관측",
           f"- 원본/시간/노드/동일 제한 기준 정합성 {summary['validCases']}/{len(report['cases'])}",
           f"- 제한으로 거래가 빠진 조합 {summary['casesWithOmittedLoadedTransactions']}개",
           f"- 관측 전체 평균 {summary['latency']['meanMs']}ms · p95 {summary['latency']['p95Ms']}ms · 최대 {summary['latency']['maxMs']}ms",
           '- DB 쓰기·실제 LLM 호출 0회. 조회 상한·룰·고정 평가 코드는 변경하지 않음.','',
           '| 거래 | 홉/이전 일 | 반환/동일 조건 전체 | 빠진 거래 | 포함 비율 | p95(ms) | 룰 점수 제한/참고 |',
           '|---|---|---:|---:|---:|---:|---|']
    for row in report['cases']:
        rule=row['ruleComparison'];scores=f"{rule['boundedScore']}/{rule.get('uncappedLoadedScore','미측정')}"
        lines.append(f"| {row['txId']} | {row['hops']}/{row['windowDays']} | {row['returnedTransactions']}/{row['uncappedLoadedTransactions']} | {row['omittedLoadedTransactions']} | {row['coverageWithinLoadedSubset']:.1%} | {row['latency']['p95Ms']} | {scores} |")
    lines+=['','## 읽을 때 주의할 점','',
            '- 포함 비율의 분모는 같은 시간·홉 조건에서 현재 DB에 적재된 자료의 제한 없는 연결 거래입니다. 전체 금융 원장의 커버리지나 AML Recall이 아닙니다.',
            '- 제한된 결과가 원본과 정확히 일치하는 것과 필요한 경로가 충분히 포함되는 것은 별도입니다.',
            '- 룰 비교는 작은 적재 자료에서 상한만 제거한 참고 계산이며 새로운 정답 평가나 룰 설정 채택이 아닙니다.',
            '- 지연은 한 PC·한 인스턴스에서의 순차 관측입니다. 콜드 캐시·동시 부하·대규모 원장·모델/RAG/UI 지연을 대표하지 않습니다.',
            '- PROFILE 실행은 별도이며 지연 샘플에 포함하지 않습니다. 누적 실행으로 캐시는 영향을 받을 수 있습니다.','',
            '실행 계획 해석: [Neo4j 공식 PROFILE 안내](https://neo4j.com/docs/http-api/current/profile-query/). 현재 관리자 HTTP 연결은 5.26에서 deprecated인 경로이며 운영 연결은 Spring/공식 드라이버로 이관합니다.','']
    return '\n'.join(lines)


def run(args):
    markdown_path=args.output.with_suffix('.md')
    if args.output.exists() or markdown_path.exists():raise ValueError('Existing audit preserved; choose a new --output')
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    targets=manifest['targets']
    if not isinstance(targets,list) or not 1<=len(targets)<=20 or len(targets)!=len(set(targets)):raise ValueError('Invalid fixed target list')
    with args.transactions.open(encoding='utf-8',newline='') as stream:rows=list(islice(csv.DictReader(stream),MAX_SOURCE_ROWS+1))
    source=source_map(rows)
    if len(source)!=manifest['transactions'] or any(tx not in source for tx in targets):raise ValueError('Manifest/source mismatch')
    memory=MemoryGraph(source.values());db=AuditDatabase();db.begin()
    count=db.query(COUNT)[0]['transactions']
    if not 1<=count<=MAX_SOURCE_ROWS:raise ValueError('This tool only audits a small loaded subset')
    loaded=[row['edge'] for row in db.query(ALL_ROWS,{'limit':MAX_SOURCE_ROWS+1})]
    if source_map(loaded)!=source or len(loaded)!=count:raise ValueError('Loaded database and source CSV differ')
    accounts={edge[key] for edge in source.values() for key in ['fromAccountKey','toAccountKey']}
    if db.query(ACCOUNT_COUNT)[0]['accounts']!=len(accounts):raise ValueError('Loaded account/source counts differ')
    indexes=db.query(INDEXES);version=db.query(VERSION)
    initial_hash=fingerprint(loaded)
    cases=[];all_samples=[];total_queries=0
    scopes=[(1,1),(2,7),(3,7)]
    for case_index,tx_id in enumerate(targets,1):
        for hops,days in scopes:
            options={'hops':hops,'window_days':days,'max_neighbors':50,'max_edges':1000}
            expected=memory.subgraph(tx_id,**options);samples=[];query_counts=[];query_times=[];previous=None
            for _ in range(args.repeats):
                db.begin();started=perf_counter();graph=db.subgraph(tx_id,**options);samples.append((perf_counter()-started)*1000)
                if previous is not None and graph!=previous:raise ValueError('Repeated graph query changed')
                previous=graph;query_counts.append(len(db.timings));query_times.extend(db.timings)
            audit,reference=audit_graph(graph,source,expected)
            if not audit['valid']:raise ValueError('Graph/source or undeclared-omission check failed')
            bounded_rules=score(graph);rule_compare={'boundedScore':bounded_rules['ruleScore'],'boundedKinds':sorted({f['ruleId'] for f in bounded_rules['findings']})}
            if len(reference['edges'])<=2000:
                full_rules=score(reference);rule_compare.update({'uncappedLoadedScore':full_rules['ruleScore'],
                    'uncappedLoadedKinds':sorted({f['ruleId'] for f in full_rules['findings']}),
                    'kindsAbsentWithLimits':sorted({f['ruleId'] for f in full_rules['findings']}-{f['ruleId'] for f in bounded_rules['findings']})})
            else:rule_compare['notMeasuredReason']='Uncapped loaded reference exceeds the unchanged rule engine bound'
            all_samples.extend(samples);total_queries+=sum(query_counts)
            cases.append({'txId':tx_id,'hops':hops,'windowDays':days,**audit,'latency':latency_summary(samples),
                          'queryCounts':query_counts,'queryLatency':latency_summary([row['elapsedMs'] for row in query_times]),'ruleComparison':rule_compare})
        print(json.dumps({'completedTargets':case_index,'totalTargets':len(targets)},ensure_ascii=False),flush=True)
    # Intentionally small bounds prove limit handling without changing runtime defaults.
    db.begin();stress=db.subgraph(targets[0],hops=2,window_days=7,max_neighbors=2,max_edges=5)
    stress_audit,_=audit_graph(stress,source,memory.subgraph(targets[0],hops=2,window_days=7,max_neighbors=2,max_edges=5))
    if not stress_audit['valid']:raise ValueError('Stress-bound consistency check failed')
    adjacency=defaultdict(list)
    for edge in source.values():
        if edge['fromAccountKey']!=edge['toAccountKey']:
            for key in ['fromAccountKey','toAccountKey']:adjacency[edge[key]].append(edge)
    hubs=sorted(adjacency,key=lambda key:(-len(adjacency[key]),key))[:5];profiles=[]
    db.begin();profiles.append({'template':'target','plan':plan_summary(db.block(TARGET_QUERY,{'txId':targets[0]},profile=True))})
    for key in hubs:
        as_of=max(edge['timestamp'] for edge in adjacency[key]);since=(datetime.fromisoformat(as_of)-timedelta(days=7)).isoformat(timespec='seconds')
        eligible=[edge for edge in adjacency[key] if since<=edge['timestamp']<=as_of]
        db.begin();plan=plan_summary(db.block(NEIGHBOR_QUERY,{'accountKey':key,'since':since,'asOf':as_of,'limit':51},profile=True))
        profiles.append({'template':'neighbors','accountKey':key,'loadedIncidentTransactions':len(adjacency[key]),'windowCandidateTransactions':len(eligible),
                         'returnedProbeUpperBound':51,'windowNeighborLimitHit':len(eligible)>50,'plan':plan})
    db.begin();final_count=db.query(COUNT)[0]['transactions']
    final_rows=[row['edge'] for row in db.query(ALL_ROWS,{'limit':MAX_SOURCE_ROWS+1})]
    if final_count!=count or fingerprint(final_rows)!=initial_hash:raise ValueError('Source changed during measurements; discard this run')
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['data-pipeline/graph_diagnostics.py','data-pipeline/graph_contract.py','data-pipeline/neo4j_mvp.py','rule-service/rules.py', 'scripts/audit-neo4j-queries.py']}
    report={'kind':'loaded_subset_read_query_audit','checkedAt':datetime.now(timezone.utc).isoformat(),'repeats':args.repeats,
            'source':{'transactions':len(source),'accounts':len(accounts),'sourceSha256':initial_hash,'databaseStable':True,'originalLedgerComplete':False},
            'databaseVersion':version,'indexes':indexes,'scopes':[{'hops':h,'windowDays':d} for h,d in scopes],
            'runtimeLimits':{'maxNeighbors':50,'maxEdges':1000},'cases':cases,'stressProbe':stress_audit,'profiles':profiles,
            'summary':{'validCases':sum(row['valid'] for row in cases),
                'casesWithOmittedLoadedTransactions':sum(row['omittedLoadedTransactions']>0 for row in cases),
                'casesWithKindsAbsentWithLimits':sum(bool(row['ruleComparison'].get('kindsAbsentWithLimits')) for row in cases),
                'latency':latency_summary(all_samples),'boundedQueryCount':total_queries},
            'codeSha256':hashes,'graphWriteQueries':0,'llmCalls':0,'operatingThresholdChanged':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
    with markdown_path.open('x',encoding='utf-8') as stream:stream.write(build_markdown(report))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transactions',type=Path,default=ROOT/'data/processed/HI-Small-MVP/transactions.csv')
    parser.add_argument('--manifest',type=Path,default=ROOT/'data/processed/HI-Small-MVP/manifest.json')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/db-query-audit-20261117.json')
    parser.add_argument('--repeats',type=int,choices=range(1,6),default=3)
    args=parser.parse_args()
    try:
        result=run(args);print(json.dumps({'casesVerified':len(result['cases']),**result['summary'],'graphWriteQueries':0,'llmCalls':0},ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'auditCompleted':False,'errorType':type(error).__name__,'notice':'Read audit did not complete. Existing results and source data are preserved.'}),file=sys.stderr)
        raise SystemExit(1)
