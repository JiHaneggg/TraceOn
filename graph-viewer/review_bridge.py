"""Bounded, same-machine review navigation over server-owned graph snapshots.

The browser supplies IDs, never a graph, score, query, model, report or URL.
This is temporary demo state, not durable investigation/review storage.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import re
import secrets
import threading
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class ReviewError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def graph_hash(graph):
    return hashlib.sha256(json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class BoundedStore:
    def __init__(self, limit=24, ttl=1200, clock=monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self.items, self.lock = OrderedDict(), threading.RLock()

    def put(self, value):
        with self.lock:
            now = self.clock()
            for key in list(self.items):
                if self.items[key][0] <= now:
                    del self.items[key]
            token = secrets.token_urlsafe(24)
            self.items[token] = (now + self.ttl, deepcopy(value))
            while len(self.items) > self.limit:
                self.items.popitem(last=False)
            return token

    def get(self, token):
        with self.lock:
            entry = self.items.get(token) if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{32}', token) else None
            if entry is None or entry[0] <= self.clock():
                if entry is not None:
                    del self.items[token]
                raise ReviewError(409, 'review_expired', '조회 정보가 만료됐습니다. 거래를 다시 조회한 뒤 초안을 준비해 주세요.')
            return deepcopy(entry[1])

    def discard(self, token):
        with self.lock:self.items.pop(token, None)


class LocalRagClient:
    def call(self, method, path, body=None):
        allowed = {'/v1/rag/reports/prepare', '/v1/rag/reports/verify', '/v1/rag/facts/resolve'}
        if not ((method == 'POST' and path in allowed) or
                (method == 'GET' and re.fullmatch(r'/v1/rag/citations/[A-Za-z0-9_.%:-]+\?corpusSha256=[0-9a-f]{64}', path))):
            raise ValueError('Only fixed local RAG operations are allowed')
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        request = Request('http://127.0.0.1:8092' + path, data=data, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Oversized RAG response')
            result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite value')))
            if not isinstance(result, dict):
                raise ValueError('Invalid RAG response')
            return result
        except HTTPError as error:
            if error.code == 409:
                raise ReviewError(409, 'evidence_version_conflict', '자료 버전이나 원문 대조를 확인하지 못했습니다. 기존 초안을 승인 상태로 표시하지 않습니다.') from None
            raise ReviewError(503, 'rag_unavailable', 'RAG 근거 조회를 완료하지 못했습니다. 서비스와 자료 준비 상태를 확인한 뒤 다시 시도해 주세요.') from None
        except (URLError, OSError, TimeoutError, ValueError, TypeError):
            raise ReviewError(503, 'rag_unavailable', 'RAG 근거 조회를 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.') from None


class ReviewBridge:
    def __init__(self, client=None, *, clock=monotonic, limit=24, ttl=1200):
        self.client = client or LocalRagClient()
        self.snapshots = BoundedStore(limit, ttl, clock)
        self.reviews = BoundedStore(limit, ttl, clock)

    def remember_graph(self, payload):
        token = self.snapshots.put(payload)
        return {**payload, 'reviewSnapshotId': token}

    def invalidate_snapshot(self, snapshot_id):
        self.snapshots.discard(snapshot_id)

    def prepare(self, snapshot_id, finding_index):
        payload = self.snapshots.get(snapshot_id)
        findings = payload['ruleResult']['findings']
        if findings:
            if type(finding_index) is not int or not 0 <= finding_index < len(findings):
                raise ReviewError(400, 'invalid_finding', '현재 그래프에 있는 탐지 경로를 선택해 주세요.')
            finding = findings[finding_index]
        else:
            if finding_index is not None:
                raise ReviewError(400, 'invalid_finding', '현재 조회에는 선택할 룰 근거가 없습니다.')
            finding = None
        inputs = {'graph': payload['graph'], 'ruleResult': payload['ruleResult']}
        expected_hash = graph_hash(payload['graph'])
        result = self.client.call('POST', '/v1/rag/reports/prepare', deepcopy({**inputs, 'mode': 'template'}))
        try:
            context, report, verification = result['context'], result['report'], result['verification']
            if (context['targetTxId'] != payload['graph']['targetTxId'] or context['graphSha256'] != expected_hash or
                    context['ruleResult'] != payload['ruleResult'] or report['graphSha256'] != expected_hash or
                    context['contextId'] != report['contextId'] or context['corpusSha256'] != report['corpusSha256'] or
                    report['generation'] != {'method': 'deterministic_template', 'llmCalled': False} or
                    report['humanReviewRequired'] is not True):
                raise ValueError('Review identity mismatch')
            self.check_verification(verification, report)
        except (ValueError, KeyError, TypeError):
            raise ReviewError(502, 'review_identity_mismatch', '초안과 선택한 거래의 연결을 확인하지 못했습니다. 거래를 다시 조회해 주세요.') from None
        focus = {'findingIndex': finding_index, 'ruleId': finding['ruleId'] if finding else None,
                 'evidenceTxIds': finding['evidenceTxIds'] if finding else [payload['graph']['targetTxId']]}
        source = {'targetTxId': payload['graph']['targetTxId'], 'queriedAt': payload['source']['queriedAt'],
                  'graphSha256': expected_hash, 'reportScope': 'complete_queried_graph', 'llmCalled': False}
        record = {'snapshotId': snapshot_id, 'focus': focus, 'inputs': inputs, 'result': result, 'source': source}
        self.snapshots.get(snapshot_id)
        review_id = self.reviews.put(record)
        return {'reviewId': review_id, 'snapshotId': snapshot_id, 'focus': focus, 'source': source, **result}

    @staticmethod
    def check_verification(verification, report):
        if (type(verification['valid']) is not bool or verification['contextId'] != report['contextId'] or
                verification['reportId'] != report['reportId'] or verification['humanReviewRequired'] is not True or
                verification['readyForSubmission'] is not False or verification['semanticEntailmentVerified'] is not False):
            raise ValueError('Invalid verification envelope')
        if verification['valid'] and (not verification['checks'] or
                any(check.get('valid') is not True for check in verification['checks'].values())):
            raise ValueError('Contradictory verification result')

    def record(self, review_id):
        record = self.reviews.get(review_id)
        self.snapshots.get(record['snapshotId'])
        return record

    def citation(self, review_id, chunk_id):
        record = self.record(review_id)
        report = record['result']['report']
        row = next((row for row in report['citations'] if row['chunkId'] == chunk_id), None)
        if row is None:
            raise ReviewError(404, 'citation_not_in_report', '이 초안에 포함된 인용만 조회할 수 있습니다.')
        path = '/v1/rag/citations/' + quote(chunk_id, safe='') + '?' + urlencode({'corpusSha256': report['corpusSha256']})
        result = self.client.call('GET', path)
        try:
            citation = result['citation']
            if (result['corpusSha256'] != report['corpusSha256'] or
                    any(citation[key] != row[key] for key in ('chunkId', 'documentId', 'sourceSha256', 'textSha256', 'text', 'url')) or
                    result['sourceVerification']['status'] not in {'snapshot_checked', 'index_only'}):
                raise ValueError('Citation identity mismatch')
        except (ValueError, KeyError, TypeError):
            raise ReviewError(502, 'citation_identity_mismatch', '인용과 저장 원문의 버전이 일치하지 않습니다.') from None
        self.record(review_id)
        return result

    def fact(self, review_id, fact_id):
        record = self.record(review_id)
        context = record['result']['context']
        expected = next((fact for fact in context['reportFacts'] if fact['factId'] == fact_id), None)
        if expected is None:
            raise ReviewError(404, 'fact_not_in_report', '이 초안에 포함된 사실만 조회할 수 있습니다.')
        result = self.client.call('POST', '/v1/rag/facts/resolve', deepcopy({**record['inputs'], 'factId': fact_id,
                                 'contextId': context['contextId'], 'corpusSha256': context['corpusSha256']}))
        try:
            by_id = {edge['txId']: edge for edge in record['inputs']['graph']['edges']}
            transactions = result['transactions']
            if (result['contextId'] != context['contextId'] or result['corpusSha256'] != context['corpusSha256'] or
                    result['graphSha256'] != context['graphSha256'] or result['fact'] != expected or
                    len(transactions) != len(expected['evidenceTxIds']) or
                    {edge['txId'] for edge in transactions} != set(expected['evidenceTxIds']) or
                    any(by_id.get(edge['txId']) != edge for edge in transactions)):
                raise ValueError('Fact/source transaction mismatch')
        except (ValueError, KeyError, TypeError):
            raise ReviewError(502, 'fact_identity_mismatch', '사실과 원본 거래의 연결이 일치하지 않습니다.') from None
        self.record(review_id)
        return result

    def verify(self, review_id):
        record = self.record(review_id)
        report = record['result']['report']
        verification = self.client.call('POST', '/v1/rag/reports/verify', deepcopy({**record['inputs'], 'report': report}))
        try:
            self.check_verification(verification, report)
        except (ValueError, KeyError, TypeError):
            raise ReviewError(502, 'verification_identity_mismatch', '초안 검증 결과의 연결을 확인하지 못했습니다.') from None
        self.record(review_id)
        return {'reviewId': review_id, 'report': report, 'verification': verification}

    def download(self, review_id, format_name):
        if format_name not in {'json', 'markdown'}:
            raise ReviewError(400, 'invalid_format', '초안 또는 근거 JSON 형식을 선택해 주세요.')
        record = self.record(review_id)
        checked = self.verify(review_id)
        if checked['verification']['valid'] is not True:
            raise ReviewError(409, 'review_not_verified', '초안 검사를 통과하지 못해 내려받기를 중단했습니다.')
        result = {'reviewId': review_id, 'snapshotId': record['snapshotId'], 'focus': record['focus'],
                  'source': record['source'], **record['result'], 'verification': checked['verification']}
        tx_id = record['source']['targetTxId']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', tx_id):
            raise ReviewError(409, 'invalid_download_identity', '초안 파일의 거래 ID를 확인하지 못했습니다.')
        text = checked['report']['markdown'] if format_name == 'markdown' else json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        suffix, mime = ('md', 'text/markdown') if format_name == 'markdown' else ('json', 'application/json')
        return f'traceon-{tx_id}-review.{suffix}', text.encode('utf-8'), mime
