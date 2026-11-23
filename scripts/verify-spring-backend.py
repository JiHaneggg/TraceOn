"""Compare Spring to the fixed DB/rule contracts and reopen archives offline.

Starts only its own temporary Spring processes. Never writes Neo4j, invokes an
LLM, replaces an existing result, or stops another developer's server.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import subprocess
import socket
import sys
import threading
import time
from urllib.parse import urlencode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--jar', type=Path)
    parser.add_argument('--java', type=Path)
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    db_path = output.with_suffix('.sqlite3')
    if output.exists() or db_path.exists():
        raise SystemExit('Use a new output name; existing results and archives are preserved.')
    output.parent.mkdir(parents=True, exist_ok=True)
    jar = (args.jar or root/'backend/build/libs/traceon-0.0.1-SNAPSHOT.jar').resolve()
    java = (args.java or root/'.tools/jdk-21.0.1/bin/java.exe').resolve()
    if not jar.is_file() or not java.is_file():
        raise SystemExit('Build the backend JAR and supply --java if the local JDK is elsewhere.')
    sys.path[:0] = [str(root/'graph-viewer'), str(root/'data-pipeline'), str(root/'rule-service')]
    import server as viewer
    from account_expansion import ExpansionManager, read_version
    from review_bridge import ReviewBridge
    from analysis_history import AnalysisHistory
    from history_store import HistoryStore

    calls = []
    class NoLive(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(503); self.end_headers()
        def do_GET(self):
            self.do_POST()
        def log_message(self, *a):
            pass
    deny = ThreadingHTTPServer(('127.0.0.1', 0), NoLive)
    thread = threading.Thread(target=deny.serve_forever, daemon=True); thread.start()
    blocked = f'http://127.0.0.1:{deny.server_port}'

    def call(path, body=None, expected=200, raw=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', args.port, timeout=45)
        try:
            data = raw if raw is not None else None if body is None else json.dumps(body, ensure_ascii=False).encode()
            connection.request('GET' if data is None else 'POST', path, data,
                               {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse(); content = response.read()
            assert response.status == expected, (path, response.status, expected, content[:250])
            if expected == 200:
                assert response.getheader('Cache-Control') == 'no-store'
            if (response.getheader('Content-Type') or '').startswith('text/markdown'):
                return content.decode()
            return json.loads(content)
        finally:
            connection.close()

    @contextmanager
    def spring(offline=False):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', args.port)) == 0:
                raise RuntimeError('Verification port is already in use; choose another --port.')
        options = [str(java), '-jar', str(jar), f'--server.port={args.port}', '--server.address=127.0.0.1',
                   f'--spring.config.import=optional:file:{(root/".env").as_posix()}[.properties]',
                   f'--traceon.manifest={(root/"data/processed/HI-Small-MVP/manifest.json").as_posix()}',
                   f'--traceon.history-db={db_path.as_posix()}', '--management.health.neo4j.enabled=false']
        if offline:
            options += [f'--traceon.neo4j-http-url={blocked}', f'--traceon.rag-url={blocked}',
                        f'--traceon.rule-url={blocked}/rules', '--spring.neo4j.uri=bolt://127.0.0.1:1']
        log_path = output.with_suffix('.offline.log' if offline else '.spring.log')
        with log_path.open('x', encoding='utf-8') as log:
            process = subprocess.Popen(options, cwd=root/'backend', stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic()+60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f'Spring startup failed; inspect {log_path.name}')
                    try:
                        call('/api/history' if offline else '/api/health')
                        break
                    except (ConnectionError, OSError, AssertionError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f'Spring readiness timed out; inspect {log_path.name}')
                        time.sleep(.25)
                yield
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)

    results = []
    expansion_pages = 0
    records = []
    rejected = 0
    try:
        with spring():
            cases = call('/api/cases')
            targets = viewer.read_targets(root/'data/processed/HI-Small-MVP/manifest.json')
            assert [item['txId'] for item in cases['items']] == targets
            for tx in targets:
                for hops, days in [(1, 1), (2, 7), (3, 7)]:
                    reference_db = viewer.ReadOnlyDatabase()
                    reference_manager = ExpansionManager(ReviewBridge(), viewer.render_graph_payload)
                    before = read_version(reference_db)
                    ref = reference_manager.initial(reference_db, viewer.graph_payload(reference_db, tx, hops=hops, window_days=days), before)
                    live = call('/api/graph?'+urlencode({'txId': tx, 'hops': hops, 'windowDays': days}))
                    for key in ['graph', 'target', 'ruleResult', 'highlightTxIds', 'summary']:
                        assert live[key] == ref[key], (tx, hops, days, key)
                    results.append({'txId': tx, 'hops': hops, 'windowDays': days,
                                    'transactions': len(live['graph']['edges']),
                                    'evidenceTxIds': live['highlightTxIds'], 'ruleScore': live['ruleResult']['ruleScore']})
                    while live['accountExpansion']['canExpand']:
                        candidate = min((c for c in live['accountExpansion']['candidates'] if c['canExpand']),
                                        key=lambda c: (c['reason'] != 'more_history', c['depth'], c['accountKey']))
                        request = {'snapshotId': live['reviewSnapshotId'], 'accountKey': candidate['accountKey']}
                        next_live = call('/api/graph/expand', request)
                        next_ref = reference_manager.expand(ref['reviewSnapshotId'], candidate['accountKey'], viewer.ReadOnlyDatabase())
                        for key in ['graph', 'ruleResult', 'highlightTxIds', 'summary']:
                            assert next_live[key] == next_ref[key], (tx, hops, days, key, 'expanded')
                        for key in next_ref['accountExpansion']:
                            if key != 'notice':
                                assert next_live['accountExpansion'][key] == next_ref['accountExpansion'][key], (tx, key)
                        assert call('/api/graph/expand', request) == next_live
                        expansion_pages += 1
                        live, ref = next_live, next_ref
            ref_db = viewer.ReadOnlyDatabase()
            manager = ExpansionManager(ReviewBridge(), viewer.render_graph_payload)
            version = read_version(ref_db)
            ref = manager.initial(ref_db, viewer.graph_payload(ref_db, 'TX-000619283'), version)
            live = call('/api/graph?txId=TX-000619283&hops=2&windowDays=7')
            assert len(live['graph']['edges']) == 69
            assert live['graph'] == ref['graph'] and live['ruleResult'] == ref['ruleResult']
            graph_saved = call('/api/history/save', {'snapshotId': live['reviewSnapshotId'], 'reviewId': None})
            records.append({'id': graph_saved['history']['historyId'], 'open': call('/api/history/open?historyId='+graph_saved['history']['historyId'])})
            old_review = call('/api/review/prepare', {'snapshotId': live['reviewSnapshotId'], 'findingIndex': 0})
            candidate = min((c for c in live['accountExpansion']['candidates'] if c['canExpand']),
                            key=lambda c: (c['reason'] != 'more_history', c['depth'], c['accountKey']))
            body = {'snapshotId': live['reviewSnapshotId'], 'accountKey': candidate['accountKey']}
            expanded = call('/api/graph/expand', body)
            ref_expanded = manager.expand(ref['reviewSnapshotId'], candidate['accountKey'], viewer.ReadOnlyDatabase())
            for key in ['graph', 'ruleResult', 'highlightTxIds', 'summary']:
                assert expanded[key] == ref_expanded[key], key
            assert len(expanded['graph']['edges']) == 72
            last = expanded['accountExpansion']['lastPage']
            assert (last['readRows'], last['addedTransactions'], last['duplicateTransactions']) == (4, 3, 1)
            assert call('/api/graph/expand', body) == expanded
            for path, request in [('/api/review/prepare', {'snapshotId': live['reviewSnapshotId'], 'findingIndex': 0}),
                                  ('/api/review/verify', {'reviewId': old_review['reviewId']}),
                                  ('/api/history/save', {'snapshotId': live['reviewSnapshotId'], 'reviewId': None}),
                                  ('/api/history/save', {'snapshotId': expanded['reviewSnapshotId'], 'reviewId': old_review['reviewId']})]:
                call(path, request, 409); rejected += 1
            review = call('/api/review/prepare', {'snapshotId': expanded['reviewSnapshotId'], 'findingIndex': 0})
            assert review['verification']['valid'] is True and review['verification']['readyForSubmission'] is False
            assert review['report']['generation'] == {'method': 'deterministic_template', 'llmCalled': False}
            facts = review['context']['reportFacts']; citations = review['report']['citations']
            for fact in facts:
                resolved = call('/api/review/fact', {'reviewId': review['reviewId'], 'factId': fact['factId']})
                assert resolved['fact'] == fact
            for citation in citations:
                resolved = call('/api/review/citation?'+urlencode({'reviewId': review['reviewId'], 'chunkId': citation['chunkId']}))
                assert resolved['citation']['text'] == citation['text']
            saved = call('/api/history/save', {'snapshotId': expanded['reviewSnapshotId'], 'reviewId': review['reviewId']})
            duplicate = call('/api/history/save', {'snapshotId': expanded['reviewSnapshotId'], 'reviewId': review['reviewId']})
            assert duplicate['created'] is False and duplicate['history'] == saved['history']
            history_id = saved['history']['historyId']
            records.append({'id': history_id, 'open': call('/api/history/open?historyId='+history_id)})
            assert records[-1]['open']['review']['report'] == review['report']
            assert records[-1]['open']['payload']['graph'] == expanded['graph']
            for path, request in [('/api/graph?txId=TX-000619283&hops=0', None),
                                  ('/api/graph?txId=TX-000619283&txId=TX-000619283', None),
                                  ('/api/graph?txId=TX-000619283&cypher=MATCH', None),
                                  ('/api/graph/expand', {**body, 'cursor': None}),
                                  ('/api/history/save', {'snapshotId': expanded['reviewSnapshotId'], 'reviewId': None, 'graph': {}})]:
                call(path, request, 400); rejected += 1
            call('/api/graph/expand', expected=400, raw=b'{"snapshotId":"a","snapshotId":"b","accountKey":"A"}'); rejected += 1
            call('/api/graph/expand', expected=413, raw=b' '*2049); rejected += 1
            call('/api/cases', expected=403, headers={'Origin': 'http://evil.example'}); rejected += 1
            call('/api/cases', expected=421, headers={'Host': 'evil.example:18080'}); rejected += 1
        # Python reader validates the same SQLite v1 archive independently.
        python_history = AnalysisHistory(ReviewBridge(), HistoryStore(db_path))
        for record in records:
            assert python_history.open(record['id']) == record['open']
        with spring(offline=True):
            assert call('/api/history')['total'] == 2
            for record in records:
                opened = call('/api/history/open?historyId='+record['id'])
                assert opened == record['open']
                assert opened['history']['integrity']['currentSourcesChecked'] is False
                assert opened['history']['humanReviewRequired'] is True and opened['history']['readyForSubmission'] is False
                assert 'reviewSnapshotId' not in opened['payload']
                assert call('/api/history/verify?historyId='+record['id'])['history']['integrity']['valid'] is True
                call('/api/history/download?'+urlencode({'historyId': record['id'], 'format': 'json'}))
                if opened['review']:
                    for fact in facts:
                        resolved = call('/api/history/fact?'+urlencode({'historyId': record['id'], 'factId': fact['factId']}))
                        assert resolved['fact'] == fact and resolved['verification']['recomputed'] is False
                    for citation in citations:
                        resolved = call('/api/history/citation?'+urlencode({'historyId': record['id'], 'chunkId': citation['chunkId']}))
                        section = resolved['section']; highlight = section['highlight']
                        assert section['text'][highlight['start']:highlight['end']] == citation['text']
                    assert opened['review']['report']['markdown'] in call('/api/history/download?'+urlencode({'historyId': record['id'], 'format': 'markdown'}))
            assert calls == [], calls
        report = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'queryComparisons': len(results),
                  'baselineTransactions': 69, 'expandedTransactions': 72, 'expansionPage': last,
                  'expandedPageComparisons': expansion_pages, 'idempotentPageReplays': expansion_pages+1,
                  'sameGraphRuleAndEvidence': True, 'reopenedAfterSpringRestart': 2,
                  'independentPythonArchiveReader': True, 'offlineLiveRequests': len(calls),
                  'liveAndArchivedFacts': len(facts), 'liveAndArchivedCitations': len(citations),
                  'rejectedRequests': rejected, 'neo4jWrites': 0, 'llmCalls': 0,
                  'scope': 'synthetic demo contract parity and archive integrity, not AML accuracy or production SLA',
                  'queries': results, 'archives': [r['open']['history'] for r in records]}
        with output.open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2); file.write('\n')
        print(json.dumps({k:v for k,v in report.items() if k not in {'queries','archives','expansionPage'}}, ensure_ascii=False))
    finally:
        deny.shutdown(); deny.server_close(); thread.join(5)


if __name__ == '__main__':
    main()
