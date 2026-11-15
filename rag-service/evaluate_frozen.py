"""Freeze a new-query check before searching; run the copied engine once.

Developer-authored gold is not externally adjudicated. Results describe retrieval
and abstention on this snapshot, not generated answers or legal correctness.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import re
import shutil
import statistics
import sys
from time import perf_counter
import unicodedata

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def normalized(query):
    return re.sub(r"[^a-z0-9가-힣]", "", unicodedata.normalize("NFKC", query).casefold())


def prior_questions(repo, exclude=None):
    rows = []
    for path in sorted((repo / 'rag-service').glob('evaluation_queries*.json')) + [repo / 'rag-service/domestic_source_checks.json']:
        if path == exclude or not path.exists():
            continue
        data = json.loads(path.read_bytes())
        cases = data if isinstance(data, list) else data.get('cases', data.get('queries', []))
        for case in cases:
            if isinstance(case, dict):
                query = case.get('query', case.get('request', {}).get('query'))
                if isinstance(query, str):
                    rows.append({'query': query, 'source': str(path.relative_to(repo))})
    for path in sorted((repo / 'rag-service').glob('*.py')) + sorted((repo / 'rag-service/tests').glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            query = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'search' and node.args:
                if isinstance(node.args[0], ast.Constant):
                    query = node.args[0].value
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'query' and isinstance(value, ast.Constant):
                        query = value.value
            if isinstance(query, str):
                rows.append({'query': query, 'source': str(path.relative_to(repo)) + ':' + str(node.lineno)})
    return rows


def novelty_audit(cases, previous):
    seen, exact, near = {}, [], []
    for case in cases:
        query = case['request']['query']
        key = normalized(query)
        if key in seen:
            exact.append({'id': case['id'], 'previous': seen[key], 'kind': 'within_set'})
        seen[key] = case['id']
        grams = set(zip(key, key[1:]))
        for old in previous:
            old_key = normalized(old['query'])
            if key == old_key:
                exact.append({'id': case['id'], 'previous': old, 'kind': 'prior_query'})
            elif len(key) >= 10 and len(old_key) >= 10:
                other = set(zip(old_key, old_key[1:]))
                similarity = len(grams & other) / max(1, len(grams | other))
                if similarity >= 0.85:
                    near.append({'id': case['id'], 'previous': old, 'bigramJaccard': round(similarity, 4)})
    return {'previousQueriesChecked': len(previous), 'exactDuplicates': exact, 'nearDuplicates': near,
            'nearDuplicateThreshold': 0.85,
            'notice': 'Lexical overlap audit does not establish semantic or author independence.'}


def load_questions(index, path):
    data = json.loads(Path(path).read_bytes())
    if not isinstance(data, dict) or set(data) != {'schemaVersion', 'kind', 'corpusSha256', 'topK', 'annotationStatus', 'evidenceUnits', 'cases'}:
        raise ValueError('Invalid new-query dataset shape')
    if (data['schemaVersion'] != '4.0' or data['kind'] != 'new_questions_frozen_before_execution'
            or data['annotationStatus'] != 'developer_authored_not_domain_adjudicated'
            or data['corpusSha256'] != index.corpus_hash or data['topK'] != 3):
        raise ValueError('Unsupported dataset or mismatched corpus')
    units = data['evidenceUnits']
    if not isinstance(units, dict) or not units:
        raise ValueError('Evidence units required')
    for key, alternatives in units.items():
        if not isinstance(key, str) or not isinstance(alternatives, list) or not alternatives:
            raise ValueError('Each evidence unit requires acceptable alternatives')
        for alternative in alternatives:
            if not isinstance(alternative, dict) or set(alternative) != {'documentId', 'section', 'quote'} or any(not isinstance(v, str) or not v.strip() for v in alternative.values()):
                raise ValueError('Invalid gold evidence anchor')
            if not any(row['documentId'] == alternative['documentId'] and row['section'] == alternative['section']
                       and alternative['quote'] in row['text'] for row in index.chunks):
                raise ValueError('Gold anchor absent from pinned corpus: ' + key)
    cases, ids = data['cases'], set()
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
        raise ValueError('Expected 1..500 cases')
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'category', 'request', 'expected', 'requiredUnits', 'annotationReason'}:
            raise ValueError('Invalid question annotation')
        if not isinstance(case['id'], str) or case['id'] in ids:
            raise ValueError('Duplicate/invalid question ID')
        ids.add(case['id'])
        if not all(isinstance(case[k], str) and case[k].strip() for k in ('category', 'annotationReason')):
            raise ValueError('Category and annotation reason required')
        request = case['request']
        if not isinstance(request, dict) or set(request) - {'query', 'jurisdiction', 'sourceKind', 'legalAsOf'}:
            raise ValueError('Invalid search request')
        if not isinstance(request.get('query'), str) or not 2 <= len(request['query'].strip()) <= 500:
            raise ValueError('Invalid query text')
        if request.get('jurisdiction', 'KR') not in {'KR', 'US', 'CA', 'ALL'} or request.get('sourceKind') not in {None, 'law', 'official_guidance', 'foreign_typology', 'bank_internal_policy'}:
            raise ValueError('Invalid search scope')
        if 'legalAsOf' in request:
            from datetime import date
            if not isinstance(request['legalAsOf'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', request['legalAsOf']):
                raise ValueError('Invalid legalAsOf')
            date.fromisoformat(request['legalAsOf'])
        required = case['requiredUnits']
        if not isinstance(required, list) or any(not isinstance(u, str) or u not in units for u in required) or len(set(required)) != len(required):
            raise ValueError('Invalid required evidence units')
        if case['expected'] not in {'retrieve', 'abstain'} or (case['expected'] == 'retrieve') != bool(required):
            raise ValueError('Expected behavior and required evidence disagree')
        for unit in required:
            if not any(row['documentId'] == alternative['documentId'] and row['section'] == alternative['section']
                       and request.get('jurisdiction', 'KR') in {'ALL', row['jurisdiction']}
                       and request.get('sourceKind') in {None, row['sourceKind']}
                       for alternative in units[unit] for row in index.chunks):
                raise ValueError('Gold evidence is outside the requested source scope: '+case['id'])
    return data


def production_files(repo):
    return sorted([path for path in (repo / 'rag-service').glob('*.py') if not path.name.startswith('evaluate_')]
                  + [repo / 'rule-service/rules.py'])


def hashes(repo, files):
    return {path.relative_to(repo).as_posix(): digest(path.read_bytes()) for path in files}


def write_once(path, payload):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def freeze(repo, index_path, questions_path, run_dir, baseline_path):
    from retrieval import Index
    repo, run_dir = Path(repo).resolve(), Path(run_dir).resolve()
    if any((run_dir / name).exists() for name in ('manifest.json', 'implementation', 'questions.json', 'corpus.json', 'execution.json')):
        raise ValueError('Existing evaluation preserved; use a new run directory')
    baseline = json.loads(Path(baseline_path).read_bytes())
    production = hashes(repo, production_files(repo))
    index = Index(index_path)
    if (baseline['implementationSha256'] != production or baseline['corpusSha256'] != index.corpus_hash
            or baseline['indexFileSha256'] != digest(Path(index_path).read_bytes())):
        raise ValueError('Production code or corpus changed after pre-question baseline')
    questions = load_questions(index, questions_path)  # Validation never calls search.
    audit = novelty_audit(questions['cases'], prior_questions(repo, Path(questions_path).resolve()))
    if audit['exactDuplicates'] or audit['nearDuplicates']:
        raise ValueError('Question novelty audit failed: ' + canonical(audit))
    run_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((repo / 'rag-service').glob('*.py')) + [repo / 'rule-service/rules.py']
    for path in files:
        destination = run_dir / 'implementation' / path.relative_to(repo)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    shutil.copyfile(index_path, run_dir / 'corpus.json')
    shutil.copyfile(questions_path, run_dir / 'questions.json')
    manifest = {'schemaVersion': '4.0', 'kind': 'frozen_developer_authored_new_query_check',
                'frozenAt': datetime.now(timezone.utc).isoformat(), 'corpusSha256': index.corpus_hash,
                'corpusFileSha256': digest((run_dir / 'corpus.json').read_bytes()),
                'questionsSha256': digest((run_dir / 'questions.json').read_bytes()),
                'implementationSha256': hashes(repo, files), 'productionBaseline': baseline, 'noveltyAudit': audit,
                'pythonVersion': platform.python_version(), 'topK': 3, 'maxSupportingChunksPerHit': 2,
                'annotationStatus': questions['annotationStatus'], 'firstRunResultsMustNotTuneThisVersion': True}
    manifest['manifestSha256'] = digest(canonical(manifest))
    write_once(run_dir / 'manifest.json', manifest)
    return manifest


def evaluate_case(index, case, units):
    from retrieval import section_key
    request = case['request']
    started = perf_counter()
    result = index.search(request['query'], top_k=3, jurisdiction=request.get('jurisdiction', 'KR'),
                          source_kind=request.get('sourceKind'), legal_as_of=request.get('legalAsOf'))
    latency = (perf_counter() - started) * 1000
    primary = result['hits']
    expanded = list({row['chunkId']: row for hit in primary for row in [hit, *hit.get('supportingChunks', [])]}.values())
    def matching(unit, rows, quote_required):
        return sorted({row['chunkId'] for row in rows for target in units[unit]
                       if row['documentId'] == target['documentId'] and row['section'] == target['section']
                       and (not quote_required or target['quote'] in row['text'])})
    coverage = [{'unitId': unit, 'primarySectionMatches': matching(unit, primary, False),
                 'primaryAnchorMatches': matching(unit, primary, True), 'contextAnchorMatches': matching(unit, expanded, True)}
                for unit in case['requiredUnits']]
    average = lambda key: sum(bool(c[key]) for c in coverage) / len(coverage) if coverage else None
    ranks = [i + 1 for i, row in enumerate(primary) if any(
        row['documentId'] == target['documentId'] and row['section'] == target['section']
        for unit in case['requiredUnits'] for target in units[unit])]
    abstained = not primary and result['status'] in {'no_evidence', 'insufficient_scope'}
    passed = all(c['contextAnchorMatches'] for c in coverage) if coverage else abstained
    return {**case, 'status': result['status'], 'answerability': result['answerability'], 'abstained': abstained,
            'expectationPassed': passed, 'sectionHitAt3': bool(ranks), 'reciprocalRankAt3': 1 / min(ranks) if ranks else 0,
            'sectionRecallAt3': average('primarySectionMatches'), 'primaryAnchorRecallAt3': average('primaryAnchorMatches'),
            'contextAnchorRecall': average('contextAnchorMatches'), 'evidenceCoverage': coverage,
            'missingUnits': [c['unitId'] for c in coverage if not c['contextAnchorMatches']],
            'primaryHits': len(primary), 'expandedChunks': len(expanded), 'contextCharacters': sum(len(r['text']) for r in expanded),
            'duplicateSlots': len(primary) - len({section_key(r) for r in primary}), 'retrievalLatencyMs': round(latency, 4),
            'retrieved': [{'chunkId': r['chunkId'], 'documentId': r['documentId'], 'section': r['section'],
                           'score': r['retrievalScore'], 'sourceDisplay': r.get('sourceDisplay'),
                           'supportingChunkIds': [c['chunkId'] for c in r.get('supportingChunks', [])]} for r in primary]}


def summarize(rows):
    positive = [r for r in rows if r['expected'] == 'retrieve']
    negative = [r for r in rows if r['expected'] == 'abstain']
    avg = lambda seq: sum(seq) / len(seq) if seq else None
    latency = sorted(r['retrievalLatencyMs'] for r in rows)
    return {'queries': len(rows), 'supported': len(positive), 'unsupported': len(negative),
            'supportedHitCountAt3': sum(r['sectionHitAt3'] for r in positive),
            'supportedHitRateAt3': avg([r['sectionHitAt3'] for r in positive]),
            'macroSectionRecallAt3': avg([r['sectionRecallAt3'] for r in positive]),
            'mrrAt3': avg([r['reciprocalRankAt3'] for r in positive]),
            'macroPrimaryAnchorRecallAt3': avg([r['primaryAnchorRecallAt3'] for r in positive]),
            'macroContextAnchorRecall': avg([r['contextAnchorRecall'] for r in positive]),
            'allRequiredContextCount': sum(r['expectationPassed'] for r in positive),
            'allRequiredContextRate': avg([r['expectationPassed'] for r in positive]),
            'unsupportedAbstentionCount': sum(r['abstained'] for r in negative),
            'unsupportedAbstentionRate': avg([r['abstained'] for r in negative]),
            'unsupportedRelatedPassageCount': sum(bool(r['primaryHits']) for r in negative),
            'expectationPassCount': sum(r['expectationPassed'] for r in rows),
            'duplicateSlotRate': sum(r['duplicateSlots'] for r in rows) / max(1, sum(r['primaryHits'] for r in rows)),
            'meanPrimaryHits': avg([r['primaryHits'] for r in positive]),
            'meanExpandedChunks': avg([r['expandedChunks'] for r in positive]),
            'meanContextCharacters': avg([r['contextCharacters'] for r in positive]),
            'latencyMedianMs': statistics.median(latency) if latency else None,
            'latencyP95Ms': latency[max(0, math.ceil(len(latency)*0.95)-1)] if latency else None}


def verify_bundle(manifest_path):
    path = Path(manifest_path).resolve(); root = path.parent
    manifest = json.loads(path.read_bytes())
    if manifest.get('schemaVersion') != '4.0' or manifest.get('manifestSha256') != digest(canonical({k:v for k,v in manifest.items() if k != 'manifestSha256'})):
        raise ValueError('Frozen manifest checksum/version mismatch')
    for filename, key in [('corpus.json', 'corpusFileSha256'), ('questions.json', 'questionsSha256')]:
        if digest((root/filename).read_bytes()) != manifest[key]:
            raise ValueError('Frozen corpus/questions changed')
    for name, checksum in manifest['implementationSha256'].items():
        if not re.fullmatch(r'(?:rag-service|rule-service)/[A-Za-z0-9_]+\.py', name):
            raise ValueError('Invalid frozen implementation path')
        target = (root/'implementation'/name).resolve()
        if not target.is_relative_to(root/'implementation') or digest(target.read_bytes()) != checksum:
            raise ValueError('Frozen implementation changed: '+name)
    actual = {p.relative_to(root/'implementation').as_posix()
              for folder in ('rag-service','rule-service') for p in (root/'implementation'/folder).glob('*.py')}
    if actual != set(manifest['implementationSha256']):
        raise ValueError('Frozen implementation file inventory changed')
    return manifest


def run(manifest_path):
    path = Path(manifest_path).resolve(); root = path.parent
    manifest = verify_bundle(path)
    if Path(__file__).resolve() != root/'implementation/rag-service/evaluate_frozen.py':
        raise ValueError('Run the copied runner under this evaluation directory, not the live engine')
    if any((root/name).exists() for name in ('execution.json', 'results.json', 'report.md')):
        raise ValueError('This frozen evaluation already started; preserve its results')
    from retrieval import Index
    index = Index(root/'corpus.json'); questions = load_questions(index,root/'questions.json')
    write_once(root/'execution.json', {'startedAt':datetime.now(timezone.utc).isoformat(),
               'manifestSha256':manifest['manifestSha256'], 'kind':'first_execution_receipt'})
    rows = [evaluate_case(index,case,questions['evidenceUnits']) for case in questions['cases']]
    result = {'kind':'new_questions_frozen_engine_first_run_not_external_adjudication',
              'manifestSha256':manifest['manifestSha256'], 'corpusSha256':index.corpus_hash,
              'questionsSha256':manifest['questionsSha256'], 'pythonVersion':platform.python_version(),
              'frozenPythonVersion':manifest['pythonVersion'], 'summary':summarize(rows),
              'byCategory':{category:summarize([r for r in rows if r['category']==category])
                            for category in sorted({r['category'] for r in rows})},
              'cases':rows, 'failures':[r for r in rows if not r['expectationPassed']],
              'llmCalls':0, 'legalOrSemanticAccuracyMeasured':False,
              'limitations':['Same developer authored the questions/gold; not external independent adjudication.',
                              'Corpus and code were fixed before execution and were not tuned on these results.',
                              'Exact-quote availability and search abstention do not measure generated-answer correction, hallucination or legal applicability.',
                              'Expanded-context coverage uses up to two extra chunks per primary hit and a larger text budget.',
                              'Unlabeled alternatives are not established irrelevant. Some missing units require annotation review.',
                              'Nonrandom small question set: counts do not establish production population accuracy.']}
    result['resultSha256']=digest(canonical(result))
    write_once(root/'results.json',result)
    (root/'report.md').write_text(render_report(result),encoding='utf-8')
    return result


def render_report(result):
    s = result['summary']
    pct = lambda v: '—' if v is None else f'{v*100:.1f}%'
    lines=['# 새 질문 고정 평가 V4', '', '개발자가 작성한 새 질문의 첫 실행 결과입니다. 외부 전문가의 독립 평가·답변 정확도·법률 정확도를 뜻하지 않습니다.', '',
           '| 확인 항목 | 결과 |', '|---|---|',
           f"| 필요한 절을 하나 이상 찾은 질문 | {s['supportedHitCountAt3']}/{s['supported']} ({pct(s['supportedHitRateAt3'])}) |",
           f"| 질문별 필요한 절 확보율 평균 | {pct(s['macroSectionRecallAt3'])} |",
           f"| 대표 본문만의 발췌 확보율 평균 | {pct(s['macroPrimaryAnchorRecallAt3'])} |",
           f"| 보조 본문 포함 발췌 확보율 평균 | {pct(s['macroContextAnchorRecall'])} |",
           f"| 필요한 발췌를 모두 확보한 질문 | {s['allRequiredContextCount']}/{s['supported']} |",
           f"| 미지원 질문의 검색 보류 | {s['unsupportedAbstentionCount']}/{s['unsupported']} ({pct(s['unsupportedAbstentionRate'])}) |",
           f"| 같은 절이 대표 검색 자리를 중복 사용한 비중 | {pct(s['duplicateSlotRate'])} |", '',
           '## 범주별 결과', '', '| 범주 | 지원 질문 | 근거 모두 확보 | 미지원 질문 | 검색 보류 |', '|---|---:|---:|---:|---:|']
    for category, row in result['byCategory'].items():
        lines.append(f"| {category} | {row['supported']} | {row['allRequiredContextCount']} | {row['unsupported']} | {row['unsupportedAbstentionCount']} |")
    lines += ['', '## 실패·검토 대상', '']
    for row in result['failures']:
        lines += [f"### {row['id']} · {row['category']}", '', row['request']['query'], '',
                  '- 검색 상태: '+row['status'], '- 누락 근거: '+(', '.join(row['missingUnits']) or '미지원 질문에 관련 본문이 노출됨'),
                  '- 검색된 절: '+' / '.join(r['documentId']+' · '+r['section'] for r in row['retrieved']), '']
    if not result['failures']: lines.append('이 고정 질문 세트의 실패는 없습니다. 일반 정확도를 의미하지 않습니다.')
    lines += ['', '## 해석 범위', '']+['- '+text for text in result['limitations']]
    lines += ['', '- Corpus: '+result['corpusSha256'], '- Questions: '+result['questionsSha256'],
              '- Result: '+result['resultSha256'], '- 실제 LLM 호출: 0회', '']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    prepare=commands.add_parser('freeze')
    prepare.add_argument('--repo',type=Path,default=ROOT)
    prepare.add_argument('--index',type=Path,required=True)
    prepare.add_argument('--questions',type=Path,required=True)
    prepare.add_argument('--run-dir',type=Path,required=True)
    prepare.add_argument('--baseline',type=Path,required=True)
    execute=commands.add_parser('run');execute.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.command=='freeze':
            result=freeze(args.repo,args.index,args.questions,args.run_dir,args.baseline)
            print(json.dumps({'manifestSha256':result['manifestSha256'],'noveltyAudit':result['noveltyAudit']},ensure_ascii=False))
        else:
            result=run(args.manifest)
            print(json.dumps({'summary':result['summary'],'failureIds':[r['id'] for r in result['failures']]},ensure_ascii=False))
        return 0
    except (ValueError,KeyError,TypeError,OSError) as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
