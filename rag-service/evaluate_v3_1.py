"""Iterated development comparison against the preserved exact V2 source.

The 48 questions are now development/regression checks, not an unseen holdout.
Initial V3 results and frozen implementation hashes remain preserved.
"""
import argparse
import importlib.util
import json
from pathlib import Path

from corpus import DEFAULT_INDEX, digest
from retrieval import Index
from evaluate_v3 import QUESTIONS, load_questions, evaluate_case, summarize

ROOT=Path(__file__).resolve().parents[1]
BASELINE=ROOT/'artifacts/rag-v3-original-retrieval-v2.py'


def baseline_index(path, baseline_path=BASELINE):
    spec=importlib.util.spec_from_file_location('traceon_preserved_retrieval_v2',baseline_path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Index(path)


def fingerprint(index, query_hash, baseline_path):
    files=[Path(__file__),Path(__file__).with_name('evaluate_v3.py'),Path(__file__).with_name('retrieval.py'),
           Path(__file__).with_name('query_processing.py'),Path(__file__).with_name('evidence.py')]
    return {'version':'3.1','corpusSha256':index.corpus_hash,'questionsSha256':query_hash,
            'baselineV2Sha256':digest(Path(baseline_path).read_bytes()),
            'implementationSha256':{p.name:digest(p.read_bytes()) for p in files},
            'topK':3,'maxSupportingChunksPerHit':2,
            'interpretation':'Iterative hand-authored development/regression checks. Previous verification questions are exposed; not a new or independent held-out benchmark.'}


def compare(index_path, query_path, baseline_path):
    candidate,baseline=Index(index_path),baseline_index(index_path,baseline_path)
    questions,query_hash=load_questions(candidate,query_path)
    before=[evaluate_case(baseline,case,questions['sections']) for case in questions['cases']]
    after=[evaluate_case(candidate,case,questions['sections']) for case in questions['cases']]
    by_split={split:{'baseline':summarize([row for row in before if row['split']==split]),
                     'candidate':summarize([row for row in after if row['split']==split])}
              for split in ['development','verification']}
    changes=[{'id':a['id'],'query':a['query'],'sectionRecallBefore':a['sectionRecallAt3'],'sectionRecallAfter':b['sectionRecallAt3'],
              'primaryEvidenceBefore':a['primaryEvidenceRecallAt3'],'primaryEvidenceAfter':b['primaryEvidenceRecallAt3'],
              'contextEvidenceBefore':a['contextEvidenceRecall'],'contextEvidenceAfter':b['contextEvidenceRecall'],
              'abstainedBefore':a['abstained'],'abstainedAfter':b['abstained'],
              'duplicateSlotsBefore':a['duplicateSlots'],'duplicateSlotsAfter':b['duplicateSlots']}
             for a,b in zip(before,after) if any(a[key]!=b[key] for key in
                   ['sectionRecallAt3','primaryEvidenceRecallAt3','contextEvidenceRecall','abstained','duplicateSlots'])]
    return {'kind':'iterated_development_paired_against_preserved_v2','fingerprint':fingerprint(candidate,query_hash,baseline_path),
            'baseline':summarize(before),'candidate':summarize(after),'byOriginalSplit':by_split,'changes':changes,
            'failures':[row for row in after if row['missingContextEvidence'] or row['unverifiedRelatedPassages']],
            'baselineCases':before,'candidateCases':after,
            'limitations':['All 48 hand-authored questions are development/regression checks after iteration; do not claim independent held-out accuracy.',
                           'Primary metrics have equal top-3 hit budgets; expanded context adds up to two supporting chunks per section and uses more text.',
                           'Exact quote anchors check evidence presence, not semantic entailment or legal applicability.',
                           'Lexical capability guards do not establish general answerability.']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--index',type=Path,default=DEFAULT_INDEX)
    parser.add_argument('--queries',type=Path,default=QUESTIONS)
    parser.add_argument('--baseline',type=Path,default=BASELINE)
    parser.add_argument('--manifest',type=Path,default=ROOT/'artifacts/rag-v3-1-frozen.json')
    parser.add_argument('--freeze',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-v3-1-comparison.json')
    args=parser.parse_args()
    index=Index(args.index);_,query_hash=load_questions(index,args.queries)
    current=fingerprint(index,query_hash,args.baseline)
    if args.freeze:
        if args.manifest.exists():raise SystemExit('Existing manifest preserved')
        args.manifest.parent.mkdir(parents=True,exist_ok=True)
        args.manifest.write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(current,ensure_ascii=False))
    else:
        if not args.manifest.exists() or json.loads(args.manifest.read_text(encoding='utf-8'))!=current:
            raise SystemExit('Fingerprint changed or not frozen; explicitly version a new run')
        if args.output.exists():raise SystemExit('Existing result preserved; choose a new --output')
        result=compare(args.index,args.queries,args.baseline)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({key:result[key] for key in ['baseline','candidate']},ensure_ascii=False))
