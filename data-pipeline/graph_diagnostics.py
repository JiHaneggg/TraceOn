"""Read-only audit of bounded graph queries against a small loaded subset.

Coverage is retrieval coverage within the loaded synthetic subset, never AML
recall or full-ledger completeness. Existing extraction/rule code is untouched.
"""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
from time import perf_counter
from urllib.request import Request,urlopen

from graph_contract import EDGE_FIELDS,public_edge
from neo4j_mvp import Database,PROJECTION,TARGET_QUERY,NEIGHBOR_QUERY

MAX_SOURCE_ROWS=20000
MARKER='MATCH (m:TraceOnDataset) RETURN m.name AS name'
COUNT='MATCH ()-[t:TRANSFER]->() RETURN count(t) AS transactions'
ACCOUNT_COUNT='MATCH (a:Account) RETURN count(a) AS accounts'
ALL_ROWS=f'MATCH (s:Account)-[t:TRANSFER]->(d:Account) WITH s,t,d ORDER BY t.txId LIMIT $limit RETURN {PROJECTION} AS edge'
INDEXES='SHOW INDEXES YIELD name,state,type,entityType,labelsOrTypes,properties RETURN name,state,type,entityType,labelsOrTypes,properties'
VERSION='CALL dbms.components() YIELD versions,edition RETURN versions,edition'
READS={TARGET_QUERY:'target',NEIGHBOR_QUERY:'neighbors',MARKER:'marker',COUNT:'count',ACCOUNT_COUNT:'accounts',ALL_ROWS:'loaded_source',INDEXES:'indexes',VERSION:'version'}


class AuditDatabase(Database):
    """Only fixed read queries; PROFILE is limited to the two runtime reads."""
    def __init__(self):
        super().__init__();self.begin()
    def begin(self):self.deadline=perf_counter()+30;self.timings=[]
    def block(self,statement,parameters=None,profile=False):
        if statement not in READS or (profile and statement not in {TARGET_QUERY,NEIGHBOR_QUERY}):
            raise ValueError('Only fixed read templates may be audited')
        parameters=parameters or {}
        if statement==ALL_ROWS and (type(parameters.get('limit')) is not int or not 1<=parameters['limit']<=MAX_SOURCE_ROWS+1):
            raise ValueError('Source read exceeds the diagnostic bound')
        if statement==NEIGHBOR_QUERY and (type(parameters.get('limit')) is not int or not 1<=parameters['limit']<=101):
            raise ValueError('Neighbor read exceeds the diagnostic bound')
        remaining=self.deadline-perf_counter()
        if remaining<=0:raise TimeoutError('Read budget exceeded')
        body={'statements':[{'statement':('PROFILE ' if profile else '')+statement,'parameters':parameters,'includeStats':True}]}
        request=Request(self.url,json.dumps(body,ensure_ascii=False,allow_nan=False).encode(),
                        {'Content-Type':'application/json','Authorization':self.authorization})
        started=perf_counter()
        with urlopen(request,timeout=min(8,remaining)) as response:raw=response.read(12_000_001)
        if len(raw)>12_000_000:raise ValueError('Diagnostic response exceeds read bound')
        result=json.loads(raw)
        if result.get('errors'):raise RuntimeError('Neo4j rejected a fixed read query')
        block=result['results'][0]
        if block.get('stats',{}).get('contains_updates') or block.get('stats',{}).get('contains_system_updates'):
            raise RuntimeError('Unexpected write statistics in read audit')
        self.timings.append({'template':READS[statement],'profiled':profile,'rows':len(block['data']),
                             'elapsedMs':round((perf_counter()-started)*1000,3)})
        return block
    def query(self,statement,parameters=None):
        block=self.block(statement,parameters)
        return [dict(zip(block['columns'],row['row'])) for row in block['data']]


def source_map(rows):
    result={}
    for row in rows:
        if len(result)>=MAX_SOURCE_ROWS:raise ValueError('Use only a bounded loaded subset')
        edge=public_edge(row)
        if edge['txId'] in result:raise ValueError('Duplicate source txId')
        stamp=datetime.fromisoformat(edge['timestamp'])
        if stamp.tzinfo is not None:raise ValueError('Expected timezone-free source timestamps')
        result[edge['txId']]=edge
    if not result:raise ValueError('Empty loaded source')
    return result


def fingerprint(rows):
    data=sorted((public_edge(row) for row in rows),key=lambda edge:edge['txId'])
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def uncapped_reference(graph,source):
    """Independent reachability oracle: undirected time-window BFS from both endpoints.

    An edge is in the reference when either endpoint is reached before the last
    expansion. Non-target self-transfers are excluded just like the public query.
    """
    target=source[graph['targetTxId']]
    since,until=datetime.fromisoformat(graph['windowStart']),datetime.fromisoformat(graph['asOf'])
    eligible=[edge for edge in source.values() if since<=datetime.fromisoformat(edge['timestamp'])<=until and edge['fromAccountKey']!=edge['toAccountKey']]
    adjacency=defaultdict(list)
    for edge in eligible:
        adjacency[edge['fromAccountKey']].append(edge);adjacency[edge['toAccountKey']].append(edge)
    distances={target['fromAccountKey']:0,target['toAccountKey']:0}
    for depth in range(graph['meta']['hops']):
        for account in sorted(key for key,value in list(distances.items()) if value==depth):
            for edge in adjacency[account]:
                for key in [edge['fromAccountKey'],edge['toAccountKey']]:
                    if key not in distances:distances[key]=depth+1
    selected=[edge for edge in eligible if min(distances.get(edge['fromAccountKey'],math.inf),distances.get(edge['toAccountKey'],math.inf))<graph['meta']['hops']]
    if not any(edge['txId']==target['txId'] for edge in selected):selected.append(target)
    edges=sorted(selected,key=lambda edge:(edge['timestamp'],edge['txId']))
    nodes=sorted({edge[key] for edge in edges for key in ['fromAccountKey','toAccountKey']})
    return {**graph,'edges':edges,'nodes':[{'accountKey':key} for key in nodes],
            'meta':{**graph['meta'],'truncated':False,'sourceGraphComplete':False,'referenceScope':'uncapped_within_loaded_subset'}}


def audit_graph(graph,source,expected_bounded):
    edges=graph['edges'];ids=[edge['txId'] for edge in edges];actual=set(ids)
    start,end=datetime.fromisoformat(graph['windowStart']),datetime.fromisoformat(graph['asOf'])
    checks={'uniqueTransactionIds':len(ids)==len(actual),'targetRetained':graph['targetTxId'] in actual,
            'sourceValuesExact':all(source.get(edge['txId'])==edge for edge in edges),
            'timeWindowExact':all(start<=datetime.fromisoformat(edge['timestamp'])<=end for edge in edges),
            'targetAsOfExact':graph['asOf']==source[graph['targetTxId']]['timestamp'],
            'nodesMatchEdges':graph['nodes']==[{'accountKey':key} for key in sorted({edge[k] for edge in edges for k in ['fromAccountKey','toAccountKey']})],
            'boundedReferenceMatches':graph['edges']==expected_bounded['edges'] and graph['nodes']==expected_bounded['nodes'] and graph['meta']['truncated']==expected_bounded['meta']['truncated']}
    full=uncapped_reference(graph,source);full_ids={edge['txId'] for edge in full['edges']}
    missing=full_ids-actual;extra=actual-full_ids
    checks['withinRequestedHops']=not extra
    checks['omissionDeclared']=not missing or graph['meta']['truncated'] is True
    return {'valid':all(checks.values()),'checks':checks,'returnedTransactions':len(actual),
            'uncappedLoadedTransactions':len(full_ids),'omittedLoadedTransactions':len(missing),
            'coverageWithinLoadedSubset':len(actual&full_ids)/len(full_ids),
            'limitHit':bool(graph['meta']['truncated']),'missingTxIdSample':sorted(missing)[:10],
            'coverageScope':'same window/hops within loaded synthetic subset; not AML recall or original-ledger coverage'},full


def latency_summary(samples):
    if not samples or any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in samples):raise ValueError('Invalid latency samples')
    ordered=sorted(samples)
    return {'samplesMs':[round(value,3) for value in samples],'firstObservedMs':round(samples[0],3),
            'meanMs':round(sum(samples)/len(samples),3),'p50Ms':round(ordered[math.ceil(len(ordered)*.50)-1],3),
            'p95Ms':round(ordered[math.ceil(len(ordered)*.95)-1],3),'maxMs':round(max(samples),3),
            'percentileMethod':'nearest-rank','sampleCount':len(samples)}


def plan_summary(block):
    root=block.get('plan',{}).get('root')
    if not root:raise ValueError('PROFILE did not return an execution plan')
    stack=[root];operators=[]
    while stack:
        node=stack.pop()
        if len(operators)>=512:raise ValueError('Execution plan too large')
        operators.append({'operator':node['operatorType'].split('@')[0],
                          'dbHits':node.get('DbHits'),'rows':node.get('Rows'),'pageCacheMisses':node.get('PageCacheMisses')})
        stack.extend(node.get('children',[]))
    hits=[row['dbHits'] for row in operators]
    return {'operators':operators,'totalDbHits':sum(hits) if all(type(value) is int for value in hits) else None,
            'usesIndexSeek':any('IndexSeek' in row['operator'] for row in operators),
            'containsAllNodesScan':any(row['operator']=='AllNodesScan' for row in operators)}
