"""Evaluate a fixed repeated cash deposit signal on an unused SAML-D period."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta
from importlib import import_module
import json
from pathlib import Path
import sqlite3

import polars as pl

weights = import_module('evaluate-detection-weights')
detector, baseline = weights.detector, weights.baseline
from graph_contract import EDGE_FIELDS

ROOT = Path(__file__).resolve().parents[1]
RAW_COLUMNS = ('Time', 'Date', 'Sender_account', 'Receiver_account', 'Amount', 'Payment_currency', 'Received_currency',
               'Sender_bank_location', 'Receiver_bank_location', 'Payment_type', 'Is_laundering', 'Laundering_type')
FIELDS = (*EDGE_FIELDS, 'sourceRow', 'recordedAmount', 'isLaundering', 'patternId', 'patternType')
PAIR = ['Sender_account', 'Receiver_account']
PAYMENT = 'Cash Deposit'
LOOKBACK_DAYS, MINIMUM_COUNT, RESERVE, SAMPLE_DAYS = 7, 4, 10, 28


def read(path):
    return json.loads(path.read_bytes())


def seal(value):
    return detector.digest(detector.canonical(value))


def source_frame(path):
    frame = pl.scan_csv(path, infer_schema=False, row_index_name='sourceRow', row_index_offset=2)
    if tuple(frame.collect_schema().names()) != ('sourceRow', *RAW_COLUMNS):
        raise ValueError('SAML-D column contract did not match')
    return frame.with_columns(pl.concat_str('Date', 'Time', separator='T').alias('timestamp'))


def transaction_id(source_row):
    return f'SAML-{source_row:09}'


def date_slice(frame, start, end):
    return frame.filter(pl.col('Date').is_between(pl.lit(start.date().isoformat()), pl.lit(end.date().isoformat()), closed='left'))


def index_slice(data, output, source_hash):
    temporary = output.with_suffix('.building.sqlite3')
    with closing(sqlite3.connect(temporary)) as db:
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA synchronous=OFF')
        columns = ','.join(f'{name} {"INTEGER" if name == "sourceRow" else "TEXT"}' for name in FIELDS)
        db.execute(f'CREATE TABLE events ({columns}, PRIMARY KEY(txId)) WITHOUT ROWID')
        db.execute('CREATE TABLE pattern_bounds(patternId TEXT PRIMARY KEY,firstTime TEXT,lastTime TEXT)')
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')

        def rows():
            keys = ('Sender_account', 'Receiver_account', 'Payment_currency', 'Received_currency', 'Payment_type')
            for raw in data.iter_rows(named=True):
                if raw['Is_laundering'] not in ('0', '1') or any(not raw[k] for k in keys):
                    raise ValueError('A source record is missing a label or account/transaction field')
                yield (transaction_id(raw['sourceRow']), raw['timestamp'], 'saml:' + raw[PAIR[0]], 'saml:' + raw[PAIR[1]],
                       None, raw['Payment_currency'], None, raw['Received_currency'], raw['Payment_type'],
                       raw['sourceRow'], raw['Amount'], raw['Is_laundering'], '', raw['Laundering_type'] or '')

        db.executemany(f'INSERT INTO events VALUES ({",".join("?" * len(FIELDS))})', rows())
        for name, columns in (('source_time', 'fromAccountKey,timestamp DESC,txId DESC'),
                              ('destination_time', 'toAccountKey,timestamp DESC,txId DESC'),
                              ('event_time', 'timestamp,txId')):
            db.execute(f'CREATE INDEX {name} ON events({columns})')
        db.execute("INSERT INTO metadata VALUES ('sourceSha256', ?)", (source_hash,))
        db.commit()
    temporary.rename(output)


def repeat_evidence(source, target):
    sender, receiver = target['fromAccountKey'], target['toAccountKey']
    if target['paymentFormat'] != PAYMENT or sender == receiver:
        return []
    as_of = target['timestamp']
    since = (datetime.fromisoformat(as_of) - timedelta(days=LOOKBACK_DAYS)).isoformat(timespec='seconds')
    rows = source.db.execute('SELECT txId,sourceRow,timestamp,fromAccountKey,toAccountKey,paymentFormat '
                             'FROM events WHERE fromAccountKey=? AND toAccountKey=? AND paymentFormat=? '
                             'AND timestamp>=? AND timestamp<? ORDER BY timestamp DESC,txId DESC LIMIT ?',
                             (sender, receiver, PAYMENT, since, as_of, MINIMUM_COUNT - 1))
    prior = [dict(row) for row in rows]
    return prior if len(prior) == MINIMUM_COUNT - 1 else []


def rule_score(graph, config):
    # Amount fields are unavailable directionally in SAML-D. This internal projection
    # keeps the pinned structural rules unchanged and omits all monetary totals.
    structural = {**graph, 'edges': [{**row, 'amountPaid': '0', 'amountReceived': '0'} for row in graph['edges']]}
    result = baseline.score(structural, config)
    for finding in result['findings']:
        finding['facts'].pop('totalsByCurrency', None)
    return result


def summarize(cases, chosen):
    metrics = detector.metrics(cases, [i in chosen for i in range(len(cases))])
    return {'reviewCount': metrics['reviewCount'], 'recall': metrics['weighted']['recall'],
            'falsePositiveRate': metrics['weighted']['falsePositiveRate'], **metrics['sample']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-csv', 'previous-sample', 'development-protocol', 'development-candidate', 'model-freeze', 'rule-freeze', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    out = args.output_dir
    if out.exists():
        parser.error('Use a new output folder')
    earlier, development, candidate = map(read, (args.previous_sample, args.development_protocol, args.development_candidate))
    previous_hash = earlier.pop('sampleSha256')
    source_hash = baseline.digest(args.source_csv)
    model = read(args.model_freeze)
    if (seal(earlier) != previous_hash or development['previousSampleSha256'] != previous_hash
            or earlier['sourceSha256'] != source_hash or development['sourceSha256'] != source_hash
            or earlier['ibmStructuralParity'] != 250 or earlier['sampleStart'] != development['endExclusive']
            or candidate['paymentType'] != PAYMENT or candidate['lookbackMinutes'] != LOOKBACK_DAYS * 1440
            or candidate['minimumDepositCountIncludingTarget'] != MINIMUM_COUNT
            or not candidate['requireSameDirectedPair'] or not candidate['includeOnlyStrictlyPriorHistory']
            or model['provenance']['v2FrozenSha256'] != baseline.digest(args.rule_freeze)
            or any(baseline.digest(ROOT / path) != checksum for path, checksum in model['provenance']['codeSha256'].items())):
        raise ValueError('Source, candidate policy or frozen scoring code changed')
    end = datetime.fromisoformat(development['start'])
    start = end - timedelta(days=SAMPLE_DAYS)
    history_start = start - timedelta(days=LOOKBACK_DAYS)
    budget = weights.PRIMARY_BUDGET
    protocol = {'suite': 'saml_d_repeat_deposit_holdout', 'sourceSha256': source_hash, 'previousSampleSha256': previous_hash,
                'developmentCandidateSha256': baseline.digest(args.development_candidate), 'start': start.isoformat(),
                'endExclusive': end.isoformat(), 'historyFrom': history_start.isoformat(), 'reviewBudget': budget,
                'reserve': RESERVE, 'rankPolicy': 'highest baseline ranks among new signals replace the lowest selected ranks',
                'lookbackDays': LOOKBACK_DAYS, 'minimumDepositCountIncludingTarget': MINIMUM_COUNT,
                'sameSecondOtherTransactionsCounted': False, 'samplingSeed': baseline.SEED,
                'rankTieSeed': weights.TIE_SEED, 'modelFreezeSha256': baseline.digest(args.model_freeze),
                'ruleFreezeSha256': baseline.digest(args.rule_freeze)}
    baseline.save(out / 'protocol.json', protocol)

    raw = source_frame(args.source_csv)
    old_ids = {case['graph']['targetTxId'] for case in earlier['cases']}
    old_rows = raw.filter(pl.col('sourceRow').is_in([int(key.removeprefix('SAML-')) for key in old_ids])).collect(engine='streaming')
    found = {transaction_id(row): stamp for row, stamp in old_rows.select('sourceRow', 'timestamp').iter_rows()}
    if found != {item['graph']['targetTxId']: item['graph']['asOf'] for item in earlier['cases']}:
        raise ValueError('Previous sample IDs did not match the original CSV')
    dev_start, dev_end = map(datetime.fromisoformat, (development['start'], development['endExclusive']))
    dev_pairs = (date_slice(raw, dev_start, dev_end).filter((pl.col('Payment_type') == PAYMENT) &
        (pl.col(PAIR[0]) != pl.col(PAIR[1]))).select(*PAIR).unique().collect(engine='streaming'))
    excluded_pairs = pl.concat([old_rows.select(*PAIR), dev_pairs]).unique()
    data = date_slice(raw, history_start, end).collect(engine='streaming')
    excluded_rows = (data.filter(pl.col('timestamp') >= start.isoformat()).join(excluded_pairs, on=PAIR, how='inner')
                     .get_column('sourceRow'))
    excluded_ids = {transaction_id(row) for row in excluded_rows} | old_ids
    index_path = out / 'saml-index.sqlite3'
    index_slice(data, index_path, source_hash)
    print(f'Prepared {data.height:,} source rows; {excluded_pairs.height:,} previously used directed account pairs excluded from sampling', flush=True)

    config = baseline.RuleConfig(**read(args.rule_freeze)['selectedConfig'])
    fit = model['variants'][detector.replay_module.ML_VARIANT]['model']
    with closing(baseline.SourceIndex(index_path)) as source:
        rows, population, sizes = baseline.select_targets(source, start.isoformat(), end.isoformat(), excluded_ids,
                                                            set(), holdout=False, positives=100, normals=150)
        if len(rows) != 250 or any(row['txId'] in excluded_ids for row in rows):
            raise ValueError('Expected 250 fresh, pair-disjoint target transactions')
        selected_rows = [{'targetTxId': row['txId'], 'expectedLabel': int(row['isLaundering']),
                          'expectedPatternType': row['patternType'], 'samplingWeight': row['samplingWeight'],
                          'asOf': row['timestamp']} for row in rows]
        packet = {'protocolSha256': baseline.digest(out / 'protocol.json'), 'population': population, 'sampleSizes': sizes,
                  'excludedDirectedPairs': excluded_pairs.height, 'priorDirectedPairOverlap': 0, 'cases': selected_rows}
        baseline.save(out / 'sample.json', {**packet, 'sampleSha256': seal(packet)})

        activity = baseline.RecipientActivity(source)
        cases, observations = [], []
        for item in selected_rows:
            key = item['targetTxId']
            graph = source.subgraph(key)
            graph['meta'].update(sourceScope='saml-d-offline-date-slice', sourceGraphComplete=False)
            target = source.target(key)
            if target['timestamp'] != item['asOf']:
                raise ValueError('Target time differs from the frozen sample')
            profile = activity.compare(key, expected_target=target)
            rule = rule_score(graph, config)
            value = baseline.combined_score(fit, baseline.features(graph, target, profile), rule['ruleScore'])
            cases.append({**item, 'graph': graph, 'ruleFlagged': rule['flagged']})
            observations.append({'rule': rule['ruleScore'], 'ruleMl': value, 'target': target,
                                 'repeatEvidence': repeat_evidence(source, target)})

    tie, values = weights.rank_inputs(cases, observations, [{'flagged': None}] * len(cases))
    order = sorted(range(len(cases)), key=lambda i: (-values['ruleMl'][i], tie[i]))
    previous = set(order[:budget])
    promoted = [i for i in order[budget:] if observations[i]['repeatEvidence']][:RESERVE]
    new = set(order[:budget - len(promoted)]) | set(promoted)
    changes, source_ids = [], set()
    for i in order:
        if i not in previous ^ new:
            continue
        evidence = observations[i]['repeatEvidence']
        key = cases[i]['targetTxId']
        source_ids.add(int(key.removeprefix('SAML-')))
        source_ids.update(row['sourceRow'] for row in evidence)
        changes.append({'targetTxId': key, 'move': 'promoted' if i in new else 'removed',
                        'expectedLabel': cases[i]['expectedLabel'], 'expectedPatternType': cases[i]['expectedPatternType'],
                        'target': observations[i]['target'], 'repeatEvidence': evidence})
    evidence_rows = raw.filter(pl.col('sourceRow').is_in(sorted(source_ids))).collect(engine='streaming') if source_ids else None
    original = {} if evidence_rows is None else {transaction_id(r['sourceRow']): r for r in evidence_rows.iter_rows(named=True)}
    if len(original) != len(source_ids):
        raise ValueError('Some changed-case evidence is missing from the raw CSV')
    for row in changes:
        target = row['target']
        raw_target = original[row['targetTxId']]
        if (raw_target['timestamp'] != target['timestamp'] or raw_target['Is_laundering'] != str(row['expectedLabel'])
                or raw_target['Laundering_type'] != row['expectedPatternType']):
            raise ValueError('Moved target does not match its raw record')
        for evidence in row['repeatEvidence']:
            raw_evidence = original[evidence['txId']]
            if ((raw_evidence[PAIR[0]], raw_evidence[PAIR[1]]) != (raw_target[PAIR[0]], raw_target[PAIR[1]])
                    or raw_evidence['Payment_type'] != PAYMENT or raw_evidence['timestamp'] != evidence['timestamp']):
                raise ValueError('Repeated deposit evidence differs from its raw record')
    result = {'suite': protocol['suite'], 'sampleSha256': seal(packet), 'caseCount': len(cases),
              'repeatSignalCases': sum(bool(row['repeatEvidence']) for row in observations),
              'repeatSignalBelowBaselineCutoff': sum(bool(observations[i]['repeatEvidence']) for i in order[budget:]),
              'baseline': summarize(cases, previous), 'withRepeatDeposits': summarize(cases, new),
              'changedCasesVerified': len(changes), 'evidenceRowsVerified': len(original), 'changedCases': changes}
    baseline.save(out / 'result.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'changedCases'}))


if __name__ == '__main__':
    main()
