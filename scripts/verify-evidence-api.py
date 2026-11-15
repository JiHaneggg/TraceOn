"""Read-only actual Neo4j -> prepared report -> citation/fact/verification APIs.

Does not train/tune rules or overwrite old evaluations. Uses template mode only.
This administrator adapter does not implement the production Spring Graph API.
"""
import argparse
import copy
import json
from decimal import Decimal
from pathlib import Path
import sys
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'data-pipeline'),str(ROOT/'rag-service')]
from neo4j_mvp import Database
from corpus import canonical,digest
from evidence_lookup import citation_path

BASE='http://127.0.0.1:8092'


def post(path,body):
    with urlopen(Request(BASE+path,json.dumps(body).encode('utf-8'),{'Content-Type':'application/json'}),timeout=30) as response:
        return json.load(response)


def get(path):
    with urlopen(BASE+path,timeout=15) as response:return json.load(response)


def rehash(report):
    report['reportId']='RPT-'+digest(canonical({key:value for key,value in report.items() if key!='reportId'}))[:24]


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-evidence-api-verification.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Existing verification preserved; choose a new --output')
    health=get('/health');database=Database()
    targets=json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_text(encoding='utf-8'))['targets']
    reports_dir=args.output.parent/args.output.stem.replace('verification','reports')
    if reports_dir.exists():raise SystemExit('Existing report directory preserved; choose a new --output')
    reports_dir.mkdir(parents=True,exist_ok=True)
    rows=[];tampering=[]
    for tx_id in targets:
        started=perf_counter();graph=database.subgraph(tx_id)
        bundle=post('/v1/rag/reports/prepare',{'graph':graph,'mode':'template'})
        context,report,verified=bundle['context'],bundle['report'],bundle['verification']
        assert verified['valid'],verified['errors']
        assert not report['generation']['llmCalled']
        for citation in report['citations']:
            resolved=get(citation['lookup']['path'])
            assert resolved['corpusSha256']==context['corpusSha256']
            assert resolved['sourceVerification']['status']=='snapshot_checked'
            h=resolved['section']['highlight']
            assert resolved['section']['text'][h['start']:h['end']]==citation['text']
        ids=['target.amountPaid','target.timestamp']
        if context['findings']:ids+=['finding.0.transactionCount','finding.0.elapsedSeconds']
        for fact_id in ids:
            fact=next(item for item in context['reportFacts'] if item['factId']==fact_id)
            response=post('/v1/rag/facts/resolve',{'graph':graph,'factId':fact_id,
                          'contextId':context['contextId'],'corpusSha256':context['corpusSha256']})
            assert response['fact']['value']==fact['value']
            assert {row['txId'] for row in response['transactions']}==set(fact['evidenceTxIds'])
        (reports_dir/(tx_id+'.json')).write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
        (reports_dir/(tx_id+'.md')).write_text(report['markdown'],encoding='utf-8')
        if not tampering and context['findings']:
            for fact_id,check in [('target.amountPaid','amounts'),('finding.0.elapsedSeconds','times'),('finding.0.transactionCount','counts')]:
                altered=copy.deepcopy(report)
                fact=next(item for item in altered['draft']['facts'] if item['factId']==fact_id)
                fact['value']=fact['value']+1 if isinstance(fact['value'],int) else str(Decimal(fact['value'])+Decimal('0.01' if check=='amounts' else '1'))
                rehash(altered)
                result=post('/v1/rag/reports/verify',{'graph':graph,'report':altered})
                assert not result['valid'] and not result['checks'][check]['valid']
                tampering.append({'kind':check,'rejected':True,'reportDigestRecomputed':True})
            for kind in ['quote','renderedContent']:
                altered=copy.deepcopy(report)
                if kind=='quote':altered['draft']['citations'][0]['quote']='원문에 없는 인용 및 범죄 확정'
                else:altered['markdown']+='\n원문과 다른 결론\n'
                rehash(altered)
                result=post('/v1/rag/reports/verify',{'graph':graph,'report':altered})
                check='citations' if kind=='quote' else 'renderedContent'
                assert not result['valid'] and not result['checks'][check]['valid']
                tampering.append({'kind':kind,'rejected':True,'reportDigestRecomputed':True})
        rows.append({'targetTxId':tx_id,'graphEdges':len(graph['edges']),'citationsResolved':len(report['citations']),
                     'factsResolved':ids,'reportVerified':True,'elapsedMs':round((perf_counter()-started)*1000,2)})
    archive_result=None
    for path in (ROOT/'data/rag/versions').glob('*.json'):
        payload=json.loads(path.read_text(encoding='utf-8'))
        if payload['corpusSha256']==health['corpusSha256']:continue
        chunk=payload['chunks'][0]
        resolved=get(citation_path(chunk['chunkId'],payload['corpusSha256']))
        assert resolved['corpusSha256']==payload['corpusSha256']
        assert resolved['citation']['text']==chunk['text']
        assert resolved['sourceVerification']['status']=='snapshot_checked'
        archive_result={'corpusSha256':payload['corpusSha256'],'originalVersionResolved':True}
        break
    try:
        get(citation_path(report['citations'][0]['chunkId'],'0'*64))
        raise AssertionError('Unknown corpus must not use active data')
    except HTTPError as error:assert error.code==409
    result={'kind':'actual_neo4j_readonly_evidence_api_check_not_spring_ml_ui_or_live_llm',
            'corpusSha256':health['corpusSha256'],'verifiedReports':len(rows),
            'citationsResolved':sum(row['citationsResolved'] for row in rows),
            'factsResolved':sum(len(row['factsResolved']) for row in rows),
            'tampering':tampering,'archivedVersion':archive_result,'unknownVersionRejected':True,
            'llmCalls':0,'cases':rows,'reportDirectory':str(reports_dir)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='cases'},ensure_ascii=False))
