"""Capture/review official cycle references; publish only with --publish.

Source snapshots and registry are local. Foreign references never establish
Korean reporting duties or the same-funds/economic-purpose/client facts.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'rag-service'))
from corpus import DEFAULT_INDEX,canonical,digest
from corpus_manager import CorpusManager,fetch_html
from retrieval import Index
from evidence_fit import policy_digest,validate_policy


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index',type=Path,default=DEFAULT_INDEX)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/rag-cycle-sources/publication.json')
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Preserve previous publication record; use another --output')
    manager=CorpusManager(args.index)
    before=Index(args.index)
    sources=json.loads((ROOT/'rag-service/cycle_sources.json').read_bytes())
    previews=[]
    for source in sources:
        registered=manager.register(source,fetch_html(source['url']))
        revision=registered['revisionId']
        previews.append(manager.diff(source['documentId'],revision))
    published=[]
    if args.publish:
        for preview in previews:
            active=Index(args.index)
            published.append(manager.publish(preview['documentId'],preview['revisionId'],active.corpus_hash))
        index=Index(args.index)
        policy=deepcopy(before.evidence_fit_policy)
        if policy is None:raise ValueError('Existing reviewed draft evidence policy required')
        for key,document,anchor in [
            ('cycle-automotive-reference','fintrac-underground-2023','circular flow of funds between different entities'),
            ('cycle-gambling-reference','fintrac-gambling-2024',"Client's account activity appears to be circular in nature")]:
            row=next(r for r in index.chunks if r['documentId']==document and anchor in r['text'])
            ref={k:row[k] for k in ('chunkId','documentId','section','sourceSha256','textSha256','jurisdiction','sourceKind')}
            ref.update(quote=anchor,quoteSha256=digest(anchor))
            policy['references'][key]=ref
        policy['rules']['CYCLE']['roles']['pattern_reference']=['cycle-automotive-reference','cycle-gambling-reference']
        policy['policySha256']=policy_digest(policy)
        validate_policy(policy,index,strict_bindings=True)
        policy_publication=manager.apply_evidence_policy(policy,index.corpus_hash)
        policy_file=args.output.parent/'evidence_fit_policy.json'
        policy_file.parent.mkdir(parents=True,exist_ok=True)
        with policy_file.open('x',encoding='utf-8') as stream:json.dump(policy,stream,ensure_ascii=False,indent=2)
    else:policy_publication=None
    after=Index(args.index)
    assert all(after.by_id[r['chunkId']]==r for r in before.chunks)
    assert after.query_scope_policy_version==before.query_scope_policy_version
    result={'kind':'official_foreign_cycle_references','published':args.publish,
            'previousCorpusSha256':before.corpus_hash,'corpusSha256':after.corpus_hash,
            'documents':len(after.documents),'chunks':len(after.chunks),
            'previousChunksPreserved':True,'queryScopePolicyVersion':after.query_scope_policy_version,
            'previews':previews,'publications':published,'evidencePolicyPublication':policy_publication,
            'bankInternalPolicySupplied':False,'humanDomainReview':'pending','llmCalls':0}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:result[k] for k in ['published','previousCorpusSha256','corpusSha256','documents','chunks','previousChunksPreserved','queryScopePolicyVersion']},ensure_ascii=False))


if __name__=='__main__':main()
