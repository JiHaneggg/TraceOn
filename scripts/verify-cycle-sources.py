"""Known V4 regression and source/report integration after corpus augmentation.

Mentor review drafts are never searched here. No model calls or database writes.
"""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'rag-service'),str(ROOT/'data-pipeline')]
from corpus import canonical,digest
from retrieval import Index
from evaluate_frozen import load_questions,evaluate_case,summarize,production_files,hashes,verify_bundle
from evidence_lookup import EvidenceRepository
from agent_tools import AgentTools
from neo4j_mvp import Database


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-cycle-sources/verification.json')
    args=parser.parse_args();args.output=args.output.resolve()
    if args.output.exists():raise SystemExit('Preserve existing verification; choose a new output')
    old=Index(ROOT/'data/rag/versions/d964bd9f8b4378b2cbd17ee24838873d7fe573cf3cd6e8a0f496465f5284b58f.json')
    index=Index()
    assert all(index.by_id[r['chunkId']]==r for r in old.chunks)
    assert index.query_scope_policy_version=='1.0'
    assert not any(r['sourceKind']=='bank_internal_policy' for r in index.chunks)
    for key in ['str-context','cdd-context','funnel-reference','wire-reference']:
        assert index.evidence_fit_policy['references'][key]==old.evidence_fit_policy['references'][key]
    verify_bundle(ROOT/'artifacts/rag-evaluation-v4/manifest.json')
    questions=load_questions(Index(ROOT/'artifacts/rag-evaluation-v4/corpus.json'),ROOT/'rag-service/evaluation_queries_v4.json')
    cases=[evaluate_case(index,c,questions['evidenceUnits']) for c in questions['cases']]
    assert all(c['expectationPassed'] for c in cases),[c['id'] for c in cases if not c['expectationPassed']]
    repository=EvidenceRepository(index)
    added=[r for r in index.chunks if r['chunkId'] not in old.by_id]
    for row in added:
        assert row['sourceKind']=='foreign_typology' and row['jurisdiction']=='CA' and row['applicability']
        assert repository.citation(row['chunkId'],index.corpus_hash)['sourceVerification']['status']=='snapshot_checked'
    smoke=[]
    for query,document in [('자동차 수출입 업체의 순환 자금 흐름','fintrac-underground-2023'),
                           ('온라인 도박 관련 순환 송금 거래','fintrac-gambling-2024')]:
        body={'query':query,'jurisdiction':'CA','sourceKind':'foreign_typology'}
        req=Request('http://127.0.0.1:8092/v1/rag/search',canonical(body).encode('utf-8'),{'Content-Type':'application/json'})
        with urlopen(req,timeout=15) as response:result=json.loads(response.read())
        assert any(h['documentId']==document for h in result['hits'])
        smoke.append({'query':query,'documentFound':document})
    targets=json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_bytes())['targets']
    db=Database();graphs={tx:db.subgraph(tx) for tx in targets}
    tools=AgentTools(index)
    report_dir=args.output.parent/(args.output.stem+'-reports');report_dir.mkdir(parents=True,exist_ok=False)
    cycle_findings=0
    for tx,graph in graphs.items():
        bundle=tools.call('prepare_review_report',{'graph':graph,'mode':'template'})
        assert bundle['verification']['valid'],bundle['verification']['errors']
        assert not bundle['report']['generation']['llmCalled']
        assert not bundle['context']['evidenceFitness']['sufficientForAutomatedDecision']
        for finding in bundle['context']['evidenceFitness']['findings']:
            if finding['ruleId']!='CYCLE':continue
            cycle_findings+=1
            pattern=next(r for r in finding['referenceChecks'] if r['role']=='pattern_reference')
            assert pattern['status']=='reference_available'
            assert all(r['matched'] for r in pattern['references'])
            assert not finding['boundedRequirementsMet']
            assert {'condition:same_funds','condition:economic_purpose','condition:client_identity'} <= {r['id'] for r in finding['missingEvidence']}
        with (report_dir/(tx+'.json')).open('x',encoding='utf-8') as stream:json.dump(bundle,stream,ensure_ascii=False,indent=2)
    assert cycle_findings>0
    legacy=0
    for folder in ['rag-document-evidence-reports','rag-domestic-evidence-reports','rag-evidence-fit-reports',
                   'rag-abstention-reports','rag-search-v5/verification-release-reports']:
        for path in (ROOT/'artifacts'/folder).glob('TX-*.json'):
            saved=json.loads(path.read_bytes());graph=graphs[saved['context']['targetTxId']]
            result=tools.call('verify_review_report',{'graph':graph,'report':saved['report']})
            assert result['valid'],(path.name,result['errors'])
            legacy+=1
    assert legacy==60
    result={'kind':'known_v4_regression_and_official_source_augmentation_not_independent_evaluation',
            'corpusSha256':index.corpus_hash,'previousCorpusSha256':old.corpus_hash,
            'documents':len(index.documents),'chunks':len(index.chunks),'oldChunksPreserved':len(old.chunks),
            'newSnapshotsVerified':len(added),'summary':summarize(cases),'smoke':smoke,
            'newReportsVerified':len(graphs),'cycleFindingsWithReferences':cycle_findings,
            'legacyReportsVerified':legacy,'mentorDraftQuestionsSearched':0,'bankInternalPolicySupplied':False,
            'implementationSha256':hashes(ROOT,production_files(ROOT)),'llmCalls':0}
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
