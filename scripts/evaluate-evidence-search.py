"""Compare concept routing against shipped V3 on unchanged development gold."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'rag-service'))
from corpus import digest
from retrieval import Index
from evaluate_v3 import load_questions,evaluate_case,summarize


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline-ref',help='Optional local historical Git ref; the shared snapshot needs no old history')
    parser.add_argument('--baseline-file',type=Path,default=ROOT/'rag-service/baselines/retrieval_v3_published.py')
    parser.add_argument('--index',type=Path,default=ROOT/'data/rag/versions/a811aa50fffe640332a25843af96b1ba39ba77aada9a3020b20acf8693c51263.json')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-evidence-search.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Existing comparison preserved; choose a new --output')
    raw=(subprocess.check_output(['git','show',args.baseline_ref+':rag-service/retrieval.py'],cwd=ROOT)
         if args.baseline_ref else args.baseline_file.read_bytes())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    saved=args.output.parent/('evidence-baseline-'+digest(raw)[:16]+'.py')
    if not saved.exists():saved.write_bytes(raw)
    assert saved.read_bytes()==raw
    spec=importlib.util.spec_from_file_location('traceon_shipped_v3',saved)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    baseline=module.Index(args.index);candidate=Index(args.index)
    questions,query_hash=load_questions(candidate,ROOT/'rag-service/evaluation_queries_v3.json')
    before=[evaluate_case(baseline,row,questions['sections']) for row in questions['cases']]
    after=[evaluate_case(candidate,row,questions['sections']) for row in questions['cases']]
    changes=[{'id':a['id'],'query':a['query'],'beforeSections':[h['section'] for h in a['retrieved']],
              'afterSections':[h['section'] for h in b['retrieved']],
              'beforeRecall':a['sectionRecallAt3'],'afterRecall':b['sectionRecallAt3']}
             for a,b in zip(before,after) if a['sectionRecallAt3']!=b['sectionRecallAt3']]
    probes=[]
    for query,expected_intent in [('STR의 뜻이 무엇인가요','definition'),
                                  ('STR과 CTR의 차이를 알려주세요','comparison'),
                                  ('의심거래보고와 고액현금거래보고를 구분해 주세요','comparison'),
                                  ('CDD 개념을 알려주세요','definition'),
                                  ('STR 보고서에 무엇을 기재하나요','lexical'),
                                  ('긴급 의심거래를 전화나 FAX로 보고하는 절차','lexical')]:
        result=candidate.search(query)
        assert result['ranking']['intent']==expected_intent
        probes.append({'query':query,'intent':result['ranking']['intent'],
                       'hits':[{'chunkId':h['chunkId'],'section':h['section']} for h in result['hits']]})
    files=['retrieval.py','search_intent.py','evidence.py','evidence_lookup.py','report_verification.py','reports.py']
    result={'kind':'same_gold_development_regression_not_independent_evaluation','baselineRef':args.baseline_ref,
            'baselineSourceSha256':digest(raw),'corpusSha256':candidate.corpus_hash,'questionsSha256':query_hash,
            'implementationSha256':{name:digest((ROOT/'rag-service'/name).read_bytes()) for name in files},
            'goldChanged':False,'baseline':summarize(before),'candidate':summarize(after),'changes':changes,
            'definitionAndProcedureProbes':probes,'candidateCases':after,
            'limitations':['Questions are exposed development/regression cases, not a new holdout.',
                           'Exact quote availability and section coverage are not semantic/financial accuracy.']}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:result[key] for key in ['goldChanged','baseline','candidate','changes']},ensure_ascii=False))
