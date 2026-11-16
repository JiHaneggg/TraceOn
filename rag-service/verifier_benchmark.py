"""Freeze and run a controlled-report integrity benchmark, not a semantic judge.

REJECT is the positive class. Independent human/LLM semantic review and a retry
orchestrator are not measured by this deterministic template/fault-injection set.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter
import shutil
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'data-pipeline'),str(ROOT/'rule-service')]
from corpus import canonical,digest
from evidence import build_context
from evidence_lookup import EvidenceRepository
from reports import generate_report,default_plan,render_report
from report_verification import verify_report
from retrieval import Index

FAULTS=('amount_table','amount_narrative','timestamp_table','account_table','ghost_article','categorical_crime_contract')


def write_once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)


def rehash(report):
    report['reportId']='RPT-'+digest(canonical({k:v for k,v in report.items() if k!='reportId'}))[:24]
    return report


def inject(report,context,kind):
    result=deepcopy(report)
    target=context['targetTransaction']
    if kind in {'amount_table','timestamp_table','account_table'}:
        field={'amount_table':'amountPaid','timestamp_table':'timestamp','account_table':'toAccountKey'}[kind]
        old=target[field]
        new=(str(Decimal(old)+Decimal('1')) if kind=='amount_table' else
             (datetime.fromisoformat(old)+timedelta(seconds=1)).isoformat() if kind=='timestamp_table' else old+'-ALTERED')
        needle=f'| target.{field} | {old} |'
        if result['markdown'].count(needle)!=1:raise ValueError('Expected one anchored fact table row')
        result['markdown']=result['markdown'].replace(needle,f'| target.{field} | {new} |',1)
    elif kind=='amount_narrative':
        old=target['amountPaid']+' '+target['paymentCurrency']
        new='약 '+str(Decimal(target['amountPaid'])+Decimal('1'))+' '+target['paymentCurrency']
        if old not in result['observations'][0]['text']:raise ValueError('Expected amount observation')
        original=result['observations'][0]['text']
        result['observations'][0]['text']=original.replace(old,new,1)
        result['markdown']=result['markdown'].replace(original,result['observations'][0]['text'],1)
    elif kind=='ghost_article':
        result['markdown']+='\n판단 근거: [특금법 제99조 제1항].\n'
    elif kind=='categorical_crime_contract':
        result['markdown']+='\n급여 또는 가맹점 정산이라는 정상 상거래 가능성을 검토하지 않고 이 거래를 사기로 확정합니다.\n'
    else:raise ValueError('Unknown fault kind')
    assert result['markdown']!=report['markdown']
    # Every injected report has a valid self-digest. A checksum-only verifier
    # therefore cannot achieve the benchmark's rejection result.
    return rehash(result)


def make_cases(index,graphs,count=60):
    if type(count) is not int or not 50<=count<=100 or count%2:raise ValueError('Use an even count in 50..100')
    if not graphs:raise ValueError('Graphs required')
    cases=[]
    for number in range(count//2):
        graph=graphs[number%len(graphs)]
        context=build_context(index,graph)
        plan=default_plan(context)
        n=len(plan['citationIds']);offset=number%n if n else 0
        plan['citationIds']=plan['citationIds'][offset:]+plan['citationIds'][:offset]
        clean=render_report(context,plan,{'method':'deterministic_template','llmCalled':False})
        kind=FAULTS[number%len(FAULTS)]
        common={'pairId':f'PAIR-{number+1:03}','graph':graph,'query':None,
                'upstreamLedgerAuthenticated':False,'humanSemanticLabel':None,
                'syntheticCounterevidence':({'possiblePurpose':'급여 또는 가맹점 정산',
                    'provenance':'synthetic_review_card_not_observed_db_field'} if kind=='categorical_crime_contract' else None)}
        cases.extend([{**common,'id':common['pairId']+'-CLEAN','expectedVerdict':'PASS','faultKind':None,'report':clean},
                      {**common,'id':common['pairId']+'-FAULT','expectedVerdict':'REJECT','faultKind':kind,'report':inject(clean,context,kind)}])
    return cases


def confusion(rows):
    tp=sum(r['expectedVerdict']=='REJECT' and r['predictedVerdict']=='REJECT' for r in rows)
    fp=sum(r['expectedVerdict']=='PASS' and r['predictedVerdict']=='REJECT' for r in rows)
    tn=sum(r['expectedVerdict']=='PASS' and r['predictedVerdict']=='PASS' for r in rows)
    fn=sum(r['expectedVerdict']=='REJECT' and r['predictedVerdict']=='PASS' for r in rows)
    div=lambda a,b:a/b if b else None
    return {'positiveClass':'REJECT','TP':tp,'FP':fp,'TN':tn,'FN':fn,
            'rejectPrecision':div(tp,tp+fp),'rejectRecall':div(tp,tp+fn),'rejectF1':div(2*tp,2*tp+fp+fn),
            'passPrecision':div(tn,tn+fn),'passRecall':div(tn,tn+fp)}


def aggregate_metrics(rows):
    metrics=[r['integrityMetrics'] for r in rows if r.get('integrityMetrics')]
    def total(section,match,total):
        a=sum(r[section][match] for r in metrics);b=sum(r[section][total] for r in metrics)
        return {'matched':a,'total':b,'rate':a/b if b else None}
    return {'numeric':total('numericFacts','matched','total'),
            'citations':total('citations','matched','total'),
            'articleTags':total('articleTags','matched','total'),
            'fullFactTableCoverage':bool(metrics) and all(m['numericFacts']['tableCoverage']==1 and not m['numericFacts']['missingFactIds'] for m in metrics)}


def summarize(rows):
    cm=confusion(rows);accepted=[r for r in rows if r['predictedVerdict']=='PASS']
    times=sorted(r['latencySeconds'] for r in rows)
    accepted_metrics=aggregate_metrics(accepted)
    numeric_faults=[r for r in rows if r['faultKind'] in FAULTS[:4]]
    target={'numericAcceptedExactly':accepted_metrics['numeric']['rate']==1 and accepted_metrics['fullFactTableCoverage'],
            'quoteGroundingAcceptedExactly':accepted_metrics['citations']['rate']==1,
            'articlePresenceAcceptedExactly':accepted_metrics['articleTags']['rate'] in {None,1},
            'rejectF1AtLeast085':cm['rejectF1'] is not None and cm['rejectF1']>=.85,
            'rejectRecallAtLeast090':cm['rejectRecall'] is not None and cm['rejectRecall']>=.90,
            'noInjectedNumericFaultAccepted':not any(r['predictedVerdict']=='PASS' for r in numeric_faults),
            'eachLocalValidationAtMost2Seconds':bool(times) and max(times)<=2}
    return {'cases':len(rows),'confusionMatrix':cm,'allInputIntegrity':aggregate_metrics(rows),
            'acceptedIntegrity':accepted_metrics,'acceptedCount':len(accepted),
            'firstPassPassRateOnMixedBenchmark':len(accepted)/len(rows) if rows else None,
            'firstPassCleanPassRate':cm['passRecall'],'retryLoopMeasured':False,'meanRetryCount':None,
            'latencySeconds':{'mean':statistics.mean(times) if times else None,'median':statistics.median(times) if times else None,
                              'p95':times[max(0,(95*len(times)+99)//100-1)] if times else None,'max':max(times) if times else None,
                              'scope':'in_process_validation_including_context_rebuild_and_local_snapshots_excludes_db_fetch_llm_and_ui'},
            'byFaultKind':{kind:{'cases':sum(r['faultKind']==kind for r in rows),
                                'rejected':sum(r['faultKind']==kind and r['predictedVerdict']=='REJECT' for r in rows)} for kind in FAULTS},
            'thresholdChecks':target,'allMeasuredStructuralTargetsMet':all(target.values()),
            'semanticJudge':{'status':'not_measured','llmCalls':0,'normalCommerceReasoningF1':None,
                             'reason':'Contract rejection of altered prose does not measure semantic reasoning.'}}


def freeze(repo,index_path,graphs,run_dir,count=60):
    repo,run_dir=Path(repo).resolve(),Path(run_dir).resolve()
    if run_dir.exists():raise ValueError('Run directory already exists; preserve it')
    index=Index(index_path)
    cases=make_cases(index,graphs,count)
    run_dir.mkdir(parents=True)
    shutil.copyfile(index.path,run_dir/'corpus.json')
    for document in index.documents:
        relative=Path(document['snapshotFile']);source=index.path.parent/'raw'/relative
        if not source.resolve().is_relative_to(index.path.parent/'raw') or digest(source.read_bytes())!=document['sourceSha256']:
            raise ValueError('Missing or altered source snapshot')
        destination=run_dir/'raw'/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,destination)
    code_files=sorted((repo/'rag-service').glob('*.py'))+[repo/'rule-service/rules.py']
    for path in code_files:
        destination=run_dir/'engine'/path.relative_to(repo);destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,destination)
    dataset={'kind':'controlled_template_fault_injection_not_semantic_holdout','annotationStatus':'developer_defined_not_domain_adjudicated',
             'baseGraphCount':len(graphs),'uniqueGraphCount':len({digest(canonical(g)) for g in graphs}),
             'cases':cases,'upstreamGraphProvenance':'bounded_Neo4j_read_export_or_explicit_synthetic_fixture'}
    write_once(run_dir/'dataset.json',dataset)
    inventory={p.relative_to(run_dir).as_posix():digest(p.read_bytes()) for p in sorted(run_dir.rglob('*')) if p.is_file()}
    manifest={'schemaVersion':'1.0','kind':'frozen_controlled_verifier_benchmark','frozenAt':datetime.now(timezone.utc).isoformat(),
              'corpusSha256':index.corpus_hash,'inventorySha256':inventory,'caseCount':len(cases),
              'thresholds':{'acceptedNumeric':1,'acceptedQuotes':1,'rejectF1':.85,'rejectRecall':.9,'localLatencyMaxSeconds':2},
              'llmCalls':0,'semanticJudgmentMeasured':False,'retriesMeasured':False}
    manifest['manifestSha256']=digest(canonical(manifest))
    write_once(run_dir/'manifest.json',manifest)
    return manifest


def verify_manifest(path):
    path=Path(path).resolve();root=path.parent
    manifest=json.loads(path.read_bytes())
    if manifest['manifestSha256']!=digest(canonical({k:v for k,v in manifest.items() if k!='manifestSha256'})):
        raise ValueError('Manifest changed')
    for name,checksum in manifest['inventorySha256'].items():
        candidate=(root/name).resolve()
        if not candidate.is_relative_to(root) or digest(candidate.read_bytes())!=checksum:raise ValueError('Frozen input changed')
    actual={p.relative_to(root).as_posix() for folder in ['engine','raw'] for p in (root/folder).rglob('*')
            if p.is_file() and '__pycache__' not in p.parts}
    expected={name for name in manifest['inventorySha256'] if name.startswith(('engine/','raw/'))}
    if actual!=expected:raise ValueError('Frozen source/code inventory changed')
    return manifest


def render_summary(result):
    s=result['summary'];cm=s['confusionMatrix'];accepted=s['acceptedIntegrity']
    percent=lambda value:'미측정' if value is None else f'{value*100:.2f}%'
    return '\n'.join(['# 제어형 보고서 검증 벤치마크','',
        '템플릿 초안과 의도적 변조의 구조적 검증 결과입니다. 독립 금융 정확도·LLM 의미 판단 성능이 아닙니다.','',
        '| 지표 | 실측 | 기준 |','|---|---:|---:|',
        f"| 통과 초안의 검증 범위 내 수치 일치율 | {percent(accepted['numeric']['rate'])} | 100% |",
        f"| 통과 초안의 청크·발췌 일치율 | {percent(accepted['citations']['rate'])} | 100% |",
        f"| 입력 전체의 수치 일치율 (결함 포함) | {percent(s['allInputIntegrity']['numeric']['rate'])} | 입력 진단 |",
        f"| REJECT 정밀도 / 재현율 / F1 | {percent(cm['rejectPrecision'])} / {percent(cm['rejectRecall'])} / {cm['rejectF1']} | Recall ≥90%, F1 ≥0.85 |",
        f"| 혼동 행렬 (REJECT 양성) | TP={cm['TP']}, FP={cm['FP']}, TN={cm['TN']}, FN={cm['FN']} | — |",
        f"| 혼합 세트 1차 통과율 / 정상 초안 1차 통과율 | {percent(s['firstPassPassRateOnMixedBenchmark'])} / {percent(s['firstPassCleanPassRate'])} | 분포 의존 |",
        f"| 평균 / p95 / 최대 로컬 검증 시간 | {s['latencySeconds']['mean']:.4f}s / {s['latencySeconds']['p95']:.4f}s / {s['latencySeconds']['max']:.4f}s | 최대 2s |",
        '| 평균 재시도 횟수 | 미측정 | ≤1.5회 |','| 정상 상거래 반론·논리 비약 의미 판단 F1 | 미측정 | 별도 검수/실호출 필요 |','',
        '반려된 수치 오류를 제외해 입력 전체가 100%였다고 표시하지 않습니다. 통과 초안 지표에는 분모·사실표 누락 검사를 함께 적용합니다.',
        '수치 범위는 사실 ID가 붙은 표·제어형 관찰 문장·조회 기간입니다. 원문 인용의 숫자·문서 버전·ID와 임의 자유 문장 전체의 의미적 정확도를 혼합하지 않습니다.',
        '유령 조문은 본문의 조문 태그 존재율을 별도로 검사합니다. 청크 안에 인용이 존재한다는 사실은 해당 조항의 적용 타당성을 뜻하지 않습니다.',
        '정상 상거래를 무시하는 단정 문장 변조는 현재 제어형 보고서 계약 위반으로 반려합니다. 이를 급여·정산을 이해한 의미 판정으로 보고하지 않습니다.',
        '반복 그래프·개발자 라벨의 결함 주입 세트이며 실제 LLM 초안·멘토 검수·독립 평가와 재시도 루프는 후속 범위입니다. PASS는 기술적 무결성 통과이며 최종 STR 제출 승인이 아닙니다.','',
        '- 실제 LLM 호출: 0회','- Manifest: '+result['manifestSha256'],'- Result: '+result['resultSha256'],''])


def run(path):
    path=Path(path).resolve();root=path.parent;manifest=verify_manifest(path)
    if Path(__file__).resolve()!=root/'engine/rag-service/verifier_benchmark.py':raise ValueError('Run the frozen copied runner')
    if any((root/name).exists() for name in ['execution.json','results.json','report.md']):raise ValueError('Benchmark already started; preserve first run')
    write_once(root/'execution.json',{'startedAt':datetime.now(timezone.utc).isoformat(),'manifestSha256':manifest['manifestSha256']})
    dataset=json.loads((root/'dataset.json').read_bytes())
    repository=EvidenceRepository(Index(root/'corpus.json'),root/'raw');rows=[]
    for case in dataset['cases']:
        started=perf_counter()
        # Only graph/report/query are passed to the verifier; labels and fault
        # names remain evaluator-only metadata.
        result=verify_report(repository,case['graph'],None,case['report'],case['query'])
        elapsed=perf_counter()-started
        rows.append({'id':case['id'],'pairId':case['pairId'],'faultKind':case['faultKind'],
                     'expectedVerdict':case['expectedVerdict'],'predictedVerdict':'PASS' if result['valid'] else 'REJECT',
                     'errors':result['errors'],'latencySeconds':elapsed,'integrityMetrics':result['integrityMetrics']})
    verify_manifest(path)
    result={'kind':'controlled_verifier_first_run_not_semantic_accuracy','manifestSha256':manifest['manifestSha256'],
            'cases':rows,'summary':summarize(rows),'baseGraphCount':dataset['uniqueGraphCount'],'llmCalls':0}
    result['resultSha256']=digest(canonical(result));write_once(root/'results.json',result)
    with (root/'report.md').open('x',encoding='utf-8') as stream:stream.write(render_summary(result))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('prepare');prepare.add_argument('--index',type=Path,default=ROOT/'data/rag/index.json')
    prepare.add_argument('--run-dir',type=Path,required=True);prepare.add_argument('--count',type=int,default=60)
    execute=sub.add_parser('run');execute.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='prepare':
        from neo4j_mvp import Database
        targets=json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_bytes())['targets']
        db=Database();graphs=[db.subgraph(tx) for tx in targets]
        result=freeze(ROOT,args.index,graphs,args.run_dir,args.count)
        print(json.dumps({'frozenCases':result['caseCount'],'manifestSha256':result['manifestSha256']}))
    else:
        result=run(args.manifest);print(json.dumps(result['summary'],ensure_ascii=False))
