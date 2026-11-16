"""Compare every live graph-viewer case with bounded Neo4j reads and rule output.

No DB writes, labels, model calls or credentials are saved in the proof.
"""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'data-pipeline'),str(ROOT/'rule-service')]
from neo4j_mvp import Database
from rules import score


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8093)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/graph-viewer/verification.json')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Existing verification preserved; choose another --output')
    def get(path):
        with urlopen(f'http://127.0.0.1:{args.port}'+path,timeout=30) as response:return json.load(response)
    assert get('/api/health')['readOnly'] is True
    cases=get('/api/cases')['items']
    expected_ids=json.loads((ROOT/'data/processed/HI-Small-MVP/manifest.json').read_bytes())['targets']
    assert [r['txId'] for r in cases]==expected_ids
    db=Database();checks=[]
    for case in cases:
        tx=case['txId'];payload=get('/api/graph?txId='+tx)
        expected=db.subgraph(tx,hops=2,window_days=7,max_neighbors=50,max_edges=1000)
        assert payload['graph']==expected
        assert payload['ruleResult']==score(expected)
        assert payload['target']==next(e for e in expected['edges'] if e['txId']==tx)
        highlighted={tx for f in payload['ruleResult']['findings'] for tx in f['evidenceTxIds']}
        assert set(payload['highlightTxIds'])==highlighted
        assert highlighted<={e['txId'] for e in expected['edges']}
        assert not any(word in json.dumps(payload) for word in ['Authorization','NEO4J_PASSWORD','C:\\Users\\'])
        checks.append({'txId':tx,'accounts':len(expected['nodes']),'transactions':len(expected['edges']),
                       'ruleScore':payload['ruleResult']['ruleScore'],'patterns':[f['ruleId'] for f in payload['ruleResult']['findings']],
                       'highlightedTransactions':len(highlighted),'truncated':expected['meta']['truncated']})
    probe=expected_ids[0]
    for hops,days in [(1,1),(3,3)]:
        payload=get(f'/api/graph?txId={probe}&hops={hops}&windowDays={days}')
        assert payload['graph']==db.subgraph(probe,hops=hops,window_days=days,max_neighbors=50,max_edges=1000)
    result={'kind':'live_graph_visualization_data_contract_check','casesVerified':len(checks),'checks':checks,
            'additionalScopeChecks':2,'writeOperations':0,'llmCalls':0,'sourceGraphComplete':False,
            'credentialsExposed':False,'note':'Selected synthetic demo subset; not detection accuracy or full-ledger coverage.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
