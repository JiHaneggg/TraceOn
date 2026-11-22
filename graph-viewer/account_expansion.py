"""Bounded account pages over immutable, server-owned graph snapshots.

No caller-provided Cypher, cursors, time windows, limits or graph values.
Managed demo data must stay immutable while a snapshot is being inspected.
"""
from collections import OrderedDict,defaultdict,deque
from copy import deepcopy
from datetime import datetime
import re
import threading
from time import monotonic

from graph_contract import EDGE_FIELDS
from neo4j_mvp import PROJECTION
from review_bridge import ReviewError

VERSION_QUERY='MATCH (m:TraceOnDataset) RETURN m.name AS name,m.sha256 AS sha256'
VERIFY_QUERY=f'''MATCH (s:Account)-[t:TRANSFER]->(d:Account)
WHERE t.txId IN $ids RETURN {PROJECTION} AS edge LIMIT $limit'''
PAGE_QUERY=f'''MATCH (a:Account {{accountKey:$accountKey}})-[t:TRANSFER]-(b:Account)
WHERE a <> b AND t.timestamp >= localdatetime($since) AND t.timestamp <= localdatetime($asOf)
AND ($cursorTime IS NULL OR t.timestamp < localdatetime($cursorTime)
 OR (t.timestamp = localdatetime($cursorTime) AND t.txId < $cursorId))
WITH t ORDER BY t.timestamp DESC,t.txId DESC LIMIT $limit
MATCH (s:Account)-[t]->(d:Account)
RETURN {PROJECTION} AS edge ORDER BY t.timestamp DESC,t.txId DESC'''
PAGE_SIZE=25
MAX_PAGES=8
MAX_ACCOUNT_PAGES=4
MAX_EDGES=1000


def eligible_accounts(graph):
    target=next(edge for edge in graph['edges'] if edge['txId']==graph['targetTxId'])
    adjacency=defaultdict(set)
    for edge in graph['edges']:
        adjacency[edge['fromAccountKey']].add(edge['toAccountKey']);adjacency[edge['toAccountKey']].add(edge['fromAccountKey'])
    distance={target['fromAccountKey']:0,target['toAccountKey']:0};queue=deque(distance)
    while queue:
        key=queue.popleft()
        if distance[key]>=graph['meta']['hops']:continue
        for other in sorted(adjacency[key]):
            if other not in distance:distance[other]=distance[key]+1;queue.append(other)
    return {key:depth for key,depth in distance.items() if depth<graph['meta']['hops']}


def read_version(database):
    rows=database.query(VERSION_QUERY)
    if len(rows)!=1 or not isinstance(rows[0].get('name'),str) or not isinstance(rows[0].get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',rows[0]['sha256']):
        return None
    return rows[0]


def from_trace(trace):
    result={}
    for key,rows in trace.items():
        order=[(row['timestamp'],row['txId']) for row in rows]
        if len(rows)>51 or len(set(order))!=len(order) or order!=sorted(order,reverse=True):
            raise ValueError('Initial account cursor is not a stable ordered page')
        visible=rows[:50]
        result[key]={'cursor':(visible[-1]['timestamp'],visible[-1]['txId']) if visible else None,
                     'hasMore':len(rows)>50,'fetchedRows':len(visible),'pages':0}
    return result


def public_status(graph,state):
    candidates=[]
    for key,depth in sorted(eligible_accounts(graph).items()):
        item=state['accounts'].get(key)
        if item is not None and not item['hasMore']:continue
        pages=item['pages'] if item else 0
        candidates.append({'accountKey':key,'depth':depth,'reason':'more_history' if item else 'unqueried',
                           'fetchedRows':item['fetchedRows'] if item else 0,'pagesUsed':pages,
                           'canExpand':pages<MAX_ACCOUNT_PAGES})
    budget_hit=state['pages']>=MAX_PAGES or len(graph['edges'])>=MAX_EDGES
    return {'schemaVersion':'1.0','enabled':state['version'] is not None and state['traceAvailable'],
            'pageSize':PAGE_SIZE,'maxPages':MAX_PAGES,'pagesUsed':state['pages'],'maxAccountPages':MAX_ACCOUNT_PAGES,
            'maxTotalEdges':MAX_EDGES,'budgetReached':budget_hit,'candidates':candidates,
            'initialTransactionCount':state['initialTransactionCount'],'totalAddedTransactions':len(graph['edges'])-state['initialTransactionCount'],
            'canExpand':state['version'] is not None and state['traceAvailable'] and not budget_hit and any(row['canExpand'] for row in candidates),
            'sourceGraphComplete':False,'lastPage':deepcopy(state.get('lastPage')),
            'pageHistory':deepcopy(state.get('pageHistory',[])),
            'datasetSha256':state['version']['sha256'] if state['version'] else None,
            'notice':'선택 계좌의 같은 기간 거래를 추가합니다. 전체 원장이나 적재 범위 밖의 거래 수를 확정하지 않습니다.'}


class ExpansionManager:
    def __init__(self,reviews,render_payload,*,clock=monotonic,ttl=1200,limit=24):
        self.reviews,self.render_payload=reviews,render_payload
        self.clock,self.ttl,self.limit=clock,ttl,limit
        self.states=OrderedDict();self.lock=threading.RLock()

    def _entry(self,token):
        entry=self.states.get(token) if isinstance(token,str) else None
        if entry is None or entry['expires']<=self.clock():
            raise ReviewError(409,'expansion_expired','추가 조회 정보가 만료됐습니다. 거래를 다시 조회해 주세요.')
        return entry

    def _remember(self,payload,state):
        payload={**payload,'accountExpansion':public_status(payload['graph'],state)}
        result=self.reviews.remember_graph(payload);token=result['reviewSnapshotId']
        self.states[token]={'expires':self.clock()+self.ttl,'state':deepcopy(state),'payload':deepcopy(result),'busy':False,'successor':None}
        for key in list(self.states):
            if self.states[key]['expires']<=self.clock():del self.states[key]
        while len(self.states)>self.limit:self.states.popitem(last=False)
        return result

    def initial(self,database,payload,version_before):
        version_after=read_version(database)
        if version_before!=version_after:raise ReviewError(409,'dataset_changed','조회 중 자료 버전이 바뀌었습니다. 다시 조회해 주세요.')
        trace=getattr(database,'neighbor_reads',None)
        state={'version':version_after,'traceAvailable':trace is not None,'accounts':from_trace(trace or {}),'pages':0,'initialTransactionCount':len(payload['graph']['edges'])}
        with self.lock:return self._remember(payload,state)

    def expand(self,snapshot_id,account_key,database):
        with self.lock:
            entry=self._entry(snapshot_id)
            if entry.get('invalid'):raise ReviewError(409,'source_changed','원본 자료가 바뀌었습니다. 거래를 다시 조회해 주세요.')
            if entry['successor']:
                successor=self._entry(entry['successor'])
                if entry['expandedAccount']==account_key and successor['successor'] is None:
                    self.reviews.snapshots.get(entry['successor'])
                    return deepcopy(successor['payload'])
                raise ReviewError(409,'graph_superseded','그래프가 이미 바뀌었습니다. 현재 조회 결과에서 계좌를 다시 선택해 주세요.')
            if entry['busy']:raise ReviewError(429,'expansion_busy','같은 그래프를 추가 조회 중입니다. 잠시 후 다시 시도해 주세요.')
            payload=self.reviews.snapshots.get(snapshot_id)
            state=deepcopy(entry['state']);graph=payload['graph'];status=public_status(graph,state)
            candidate=next((row for row in status['candidates'] if row['accountKey']==account_key),None)
            if not status['enabled']:raise ReviewError(409,'expansion_not_available','자료 버전 또는 조회 이력을 확인하지 못해 추가 조회할 수 없습니다.')
            if status['budgetReached']:raise ReviewError(409,'expansion_budget','추가 조회 횟수 또는 전체 거래 수 상한에 도달했습니다.')
            if candidate is None or not candidate['canExpand']:raise ReviewError(400,'account_not_expandable','현재 홉 범위에서 추가 조회 가능한 계좌를 선택해 주세요.')
            entry['busy']=True
        try:
            if read_version(database)!=state['version']:raise ReviewError(409,'dataset_changed','자료 버전이 바뀌었습니다. 거래를 다시 조회해 주세요.')
            existing={edge['txId']:edge for edge in graph['edges']}
            live=[row['edge'] for row in database.query(VERIFY_QUERY,{'ids':list(existing),'limit':MAX_EDGES+1})]
            if len(live)!=len(existing) or len({row['txId'] for row in live})!=len(live) or any(existing.get(row['txId'])!=row for row in live):
                raise ReviewError(409,'source_changed','기존 거래 값이 달라졌습니다. 거래를 다시 조회해 주세요.')
            item=state['accounts'].get(account_key,{'cursor':None,'hasMore':True,'fetchedRows':0,'pages':0})
            page_size=min(PAGE_SIZE,MAX_EDGES-len(existing));cursor=item['cursor']
            params={'accountKey':account_key,'since':graph['windowStart'],'asOf':graph['asOf'],'limit':page_size+1,
                    'cursorTime':cursor[0] if cursor else None,'cursorId':cursor[1] if cursor else None}
            rows=[row['edge'] for row in database.query(PAGE_QUERY,params)]
            if len(rows)>page_size+1 or len({row['txId'] for row in rows})!=len(rows):raise ValueError('Invalid/duplicate page')
            previous=cursor
            for row in rows:
                if set(row)!=set(EDGE_FIELDS) or any(not isinstance(value,str) or not 1<=len(value)<=200 for value in row.values()):raise ValueError('Invalid source contract')
                key=(row['timestamp'],row['txId']);when=datetime.fromisoformat(row['timestamp'])
                if (when.tzinfo is not None or not datetime.fromisoformat(graph['windowStart'])<=when<=datetime.fromisoformat(graph['asOf']) or
                    account_key not in {row['fromAccountKey'],row['toAccountKey']} or row['fromAccountKey']==row['toAccountKey'] or
                    previous is not None and not key<previous):raise ValueError('Page scope/order mismatch')
                if row['txId'] in existing and row!=existing[row['txId']]:raise ReviewError(409,'source_changed','추가 거래와 기존 거래 값이 달라 재조회가 필요합니다.')
                previous=key
            if read_version(database)!=state['version']:raise ReviewError(409,'dataset_changed','추가 조회 중 자료 버전이 바뀌었습니다. 다시 조회해 주세요.')
            consumed=rows[:page_size];before=len(existing)
            for row in consumed:existing[row['txId']]=row
            item={'cursor':(consumed[-1]['timestamp'],consumed[-1]['txId']) if consumed else cursor,
                  'hasMore':len(rows)>page_size,'fetchedRows':item['fetchedRows']+len(consumed),'pages':item['pages']+1}
            state['accounts'][account_key]=item;state['pages']+=1
            before_ids={row['txId'] for row in graph['edges']}
            state['lastPage']={'accountKey':account_key,'readRows':len(consumed),'addedTransactions':len(existing)-before,'duplicateTransactions':len(consumed)-(len(existing)-before),
                               'addedTxIds':sorted(set(existing)-before_ids)}
            state.setdefault('pageHistory',[]).append(deepcopy(state['lastPage']))
            expanded=deepcopy(graph);expanded['edges']=sorted(existing.values(),key=lambda row:(row['timestamp'],row['txId']))
            expanded['nodes']=[{'accountKey':key} for key in sorted({row[key] for row in existing.values() for key in ['fromAccountKey','toAccountKey']})]
            expanded['meta'].update(queryMode='bounded_account_pages_v1',additionalReadPages=state['pages'],additionalPageSize=PAGE_SIZE)
            pending=public_status(expanded,state)
            expanded['meta']['truncated']=bool(pending['candidates']) or len(expanded['edges'])>=MAX_EDGES
            rendered=self.render_payload(expanded)
            with self.lock:
                current=self._entry(snapshot_id)
                if current is not entry:raise ReviewError(409,'expansion_expired','추가 조회 정보가 만료됐습니다.')
                self.reviews.snapshots.get(snapshot_id)
                result=self._remember(rendered,state)
                current['successor']=result['reviewSnapshotId'];current['expandedAccount']=account_key
                self.reviews.invalidate_snapshot(snapshot_id)
                return result
        except ReviewError as error:
            if error.code in {'dataset_changed','source_changed'}:
                with self.lock:
                    entry['invalid']=True;self.reviews.invalidate_snapshot(snapshot_id)
            raise
        finally:
            with self.lock:entry['busy']=False
