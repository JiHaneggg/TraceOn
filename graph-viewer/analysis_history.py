"""Capture server-owned graph/review evidence and reopen it without live sources."""
from copy import deepcopy
import hashlib
from pathlib import Path
from time import monotonic

from history_store import encoded,digest,history_id
from review_bridge import ReviewError,ReviewBridge,graph_hash

PAYLOAD_FIELDS=('schemaVersion','graph','target','ruleResult','highlightTxIds','summary','source','notice','accountExpansion')
ROOT=Path(__file__).resolve().parents[1]


def validate_capture(capture):
    """Check archived identities, never rerun a newer rule/search implementation."""
    payload=capture['payload'];graph=payload['graph'];edges=graph['edges'];by_id={row['txId']:row for row in edges}
    if not 1<=len(edges)<=1000 or len(by_id)!=len(edges) or by_id[graph['targetTxId']]!=payload['target']:
        raise ValueError('Archived graph identity mismatch')
    if not set(payload['highlightTxIds'])<=set(by_id):raise ValueError('Archived evidence outside graph')
    review=capture['review'];citations=capture['citations']
    if review is None:
        if citations:raise ValueError('Citations without review')
        return
    context,report,verification=review['context'],review['report'],review['verification']
    ReviewBridge.check_verification(verification,report)
    if (verification['valid'] is not True or graph_hash(graph)!=context['graphSha256'] or context['graphSha256']!=report['graphSha256'] or
        context['contextId']!=report['contextId'] or context['corpusSha256']!=report['corpusSha256'] or
        context['targetTxId']!=graph['targetTxId'] or context['ruleResult']!=payload['ruleResult'] or
        report['humanReviewRequired'] is not True or report['generation']!={'method':'deterministic_template','llmCalled':False}):
        raise ValueError('Archived review identity mismatch')
    if {row['chunkId'] for row in report['citations']}!=set(citations):raise ValueError('Missing archived citations')
    for row in report['citations']:
        resolved=citations[row['chunkId']];section=resolved['section'];citation=resolved['citation'];highlight=section['highlight']
        if (resolved['corpusSha256']!=context['corpusSha256'] or resolved['sourceVerification']['status']!='snapshot_checked' or
            any(citation[key]!=row[key] for key in ('chunkId','documentId','sourceSha256','textSha256','text','url')) or
            not isinstance(section['text'],str) or not 0<=highlight['start']<=highlight['end']<=len(section['text']) or
            section['text'][highlight['start']:highlight['end']]!=row['text'] or digest(row['text'].encode())!=row['textSha256']):
            raise ValueError('Archived citation mismatch')
    for fact in context['reportFacts']:
        if not set(fact['evidenceTxIds'])<=set(by_id):raise ValueError('Archived fact source missing')
    if any(by_id.get(row['txId'])!=row for row in context['evidenceTransactions']):raise ValueError('Archived fact transaction changed')
    index=review['focus']['findingIndex'];findings=payload['ruleResult']['findings']
    expected=findings[index]['evidenceTxIds'] if type(index) is int and 0<=index<len(findings) else [graph['targetTxId']] if index is None and not findings else None
    if expected!=review['focus']['evidenceTxIds']:raise ValueError('Archived focus mismatch')


def metadata(body,sha):
    capture=body['capture'];p=capture['payload'];review=capture['review']
    return {'historyId':body['historyId'],'savedAt':body['savedAt'],'snapshotSha256':sha,
            'targetTxId':p['graph']['targetTxId'],'transactions':len(p['graph']['edges']),'accounts':len(p['graph']['nodes']),
            'ruleScore':p['ruleResult']['ruleScore'],'hops':p['graph']['meta']['hops'],
            'asOf':p['graph']['asOf'],'windowStart':p['graph']['windowStart'],
            'additionalReadPages':p.get('accountExpansion',{}).get('pagesUsed',0),'hasReview':review is not None,
            'reportId':review['report']['reportId'] if review else None,
            'corpusSha256':review['context']['corpusSha256'] if review else None,
            'graphSha256':graph_hash(p['graph']),
            'integrity':{'valid':True,'basis':'saved_bundle_sha256_and_identity_checks','currentSourcesChecked':False},
            'humanReviewRequired':True,'readyForSubmission':False,'sourceGraphComplete':False}


class AnalysisHistory:
    def __init__(self,reviews,store,*,clock=monotonic):self.reviews,self.store,self.clock=reviews,store,clock

    def save(self,snapshot_id,review_id):
        payload=self.reviews.snapshots.get(snapshot_id)
        capture={'payload':{key:deepcopy(payload[key]) for key in PAYLOAD_FIELDS if key in payload},'review':None,'citations':{},
                 'componentSha256':{label:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for label,path in
                    [('graphViewer','graph-viewer/server.py'),('reviewBridge','graph-viewer/review_bridge.py'),('ruleEngine','rule-service/rules.py'),
                     ('ragEvidence','rag-service/evidence.py'),('ragReports','rag-service/reports.py'),('ragVerification','rag-service/report_verification.py')]}}
        if review_id is not None:
            deadline=self.clock()+30
            record=self.reviews.record(review_id)
            if record['snapshotId']!=snapshot_id:raise ReviewError(409,'history_review_mismatch','현재 그래프와 같은 조회에서 준비한 초안만 함께 저장할 수 있습니다.')
            checked=self.reviews.verify(review_id)
            if checked['verification']['valid'] is not True:raise ReviewError(409,'review_not_verified','초안 검사를 통과하지 못해 저장하지 않았습니다.')
            result=deepcopy(record['result']);result['verification']=checked['verification']
            capture['review']={'focus':record['focus'],'source':record['source'],**result}
            if len(result['report']['citations'])>32:raise ReviewError(413,'history_citation_limit','함께 저장할 인용 개수가 시연 한도를 넘었습니다.')
            for citation in result['report']['citations']:
                if self.clock()>=deadline:raise ReviewError(503,'history_capture_timeout','원문을 함께 저장하는 시간이 초과됐습니다. 저장본은 생성하지 않았습니다.')
                resolved=self.reviews.citation(review_id,citation['chunkId'])
                capture['citations'][citation['chunkId']]={key:deepcopy(resolved[key]) for key in ['schemaVersion','corpusSha256','citation','section','sourceVerification']}
            if self.clock()>=deadline:raise ReviewError(503,'history_capture_timeout','원문 저장 시간이 초과됐습니다. 다시 시도해 주세요.')
        validate_capture(capture)
        # Expansion invalidation and committing a saved snapshot cannot interleave.
        with self.reviews.snapshots.lock:
            self.reviews.snapshots.get(snapshot_id)
            if review_id is not None:self.reviews.record(review_id)
            body,sha,created=self.store.put(capture)
        return {'schemaVersion':'1.0','history':metadata(body,sha),'created':created}

    def load(self,id):
        body,sha=self.store.get(id)
        try:validate_capture(body['capture'])
        except (ValueError,KeyError,TypeError):
            raise ReviewError(409,'history_integrity_failed','저장된 거래·초안·원문의 연결을 확인하지 못했습니다.') from None
        return body,sha

    def listing(self,before=None):
        rows,cursor,total=self.store.list(before)
        for body,_ in rows:
            try:validate_capture(body['capture'])
            except (ValueError,KeyError,TypeError):raise ReviewError(409,'history_integrity_failed','목록의 저장본 중 내용 확인이 필요한 항목이 있습니다.') from None
        return {'schemaVersion':'1.0','items':[metadata(body,sha) for body,sha in rows],'nextCursor':cursor,'total':total,'maxEntries':self.store.max_entries}

    def open(self,id):
        body,sha=self.load(id);meta=metadata(body,sha);capture=body['capture']
        return {'schemaVersion':'1.0','history':meta,'payload':{**deepcopy(capture['payload']),'history':meta},
                'review':deepcopy(capture['review'])}

    def citation(self,id,chunk_id):
        body,sha=self.load(id);resolved=body['capture']['citations'].get(chunk_id)
        if resolved is None:raise ReviewError(404,'citation_not_in_report','이 저장본에 함께 보관한 인용만 조회할 수 있습니다.')
        return {**resolved,'history':metadata(body,sha)}

    def fact(self,id,fact_id):
        body,sha=self.load(id);capture=body['capture'];review=capture['review']
        context=review['context'] if review else None
        fact=next((f for f in context['reportFacts'] if f['factId']==fact_id),None) if context else None
        if fact is None:raise ReviewError(404,'fact_not_in_report','이 저장본에 포함된 사실만 조회할 수 있습니다.')
        ids=set(fact['evidenceTxIds'])
        return {'schemaVersion':'1.0','contextId':context['contextId'],'graphSha256':context['graphSha256'],'corpusSha256':context['corpusSha256'],
                'fact':fact,'transactions':[row for row in capture['payload']['graph']['edges'] if row['txId'] in ids],
                'history':metadata(body,sha),'verification':{'recomputed':False,'basis':'archived_verified_context_and_graph','externalLedgerChecked':False}}

    def verify(self,id):
        body,sha=self.load(id);return {'schemaVersion':'1.0','history':metadata(body,sha)}

    def download(self,id,format):
        body,sha=self.load(id);review=body['capture']['review']
        if format=='json':raw=encoded({**body,'snapshotSha256':sha,'currentSourcesChecked':False,'humanReviewRequired':True})+b'\n';mime='application/json';ext='json'
        elif format=='markdown' and review:
            raw=(f"# 저장 당시 분석 초안\n\n저장 시각: {body['savedAt']}\n\n저장본 SHA-256: {sha}\n\n현재 자료는 재조회하지 않았습니다. 사람 검토가 필요합니다.\n\n"+review['report']['markdown']).encode();mime='text/markdown';ext='md'
        else:raise ReviewError(400,'invalid_format','초안이 있는 저장본의 Markdown 또는 근거 JSON을 선택해 주세요.')
        return f"traceon-{history_id(id)}-archive.{ext}",raw,mime
