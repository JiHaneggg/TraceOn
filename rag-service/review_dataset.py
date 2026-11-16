"""Validate draft annotations and review completeness, never score search.

Reviewer names are declarations, not authenticated proof of independent review.
The draft stays excluded from reported performance until actual review/freeze.
"""
import argparse
from datetime import date
import json
from pathlib import Path

from corpus import DEFAULT_INDEX,digest
from retrieval import Index


def check_review_pack(pack,index,require_review=False):
    if (pack.get('schemaVersion')!='1.0' or pack.get('kind')!='mentor_review_draft_not_evaluation_results'
            or pack.get('searchExecuted') is not False or pack.get('corpusSha256')!=index.corpus_hash):
        raise ValueError('Draft type or pinned corpus mismatch')
    cases=pack.get('cases',[])
    if not 1<=len(cases)<=100:raise ValueError('Expected 1..100 draft cases')
    ids=set();approved=0
    for case in cases:
        if not isinstance(case['id'],str) or case['id'] in ids:raise ValueError('Duplicate/invalid question ID')
        ids.add(case['id'])
        req=case['request'];query=req['query']
        if not isinstance(query,str) or not 2<=len(query)<=500:raise ValueError('Invalid draft query')
        if case['proposedExpected'] not in {'retrieve','abstain'} or bool(case['anchors'])!=(case['proposedExpected']=='retrieve'):
            raise ValueError('Draft expectation/evidence mismatch')
        for ref in case['anchors']:
            row=index.by_id.get(ref['chunkId'])
            if row is None or any(row.get(k)!=ref[k] for k in ['documentId','section','sourceSha256','textSha256','url','jurisdiction','sourceKind']):
                raise ValueError('Draft source changed or missing')
            if not ref['quote'] or ref['quote'] not in row['text'] or digest(ref['quote'])!=ref['quoteSha256']:
                raise ValueError('Draft quote absent or changed')
            if req.get('jurisdiction','KR') not in {'ALL',row['jurisdiction']} or req.get('sourceKind') not in {None,row['sourceKind']}:
                raise ValueError('Draft anchor outside requested scope')
        review=case['review']
        if review['decision'] not in {'pending','approve','revise'}:raise ValueError('Invalid review decision')
        if review['decision']=='approve':
            if not isinstance(review['reviewer'],str) or not review['reviewer'].strip():raise ValueError('Approved annotation requires reviewer')
            date.fromisoformat(review['reviewedAt'])
            approved+=1
    ready=approved==len(cases)
    if require_review and not ready:raise ValueError('Human annotation review is incomplete; evaluation not enabled')
    return {'questions':len(cases),'reviewedAndApproved':approved,'pendingOrRevision':len(cases)-approved,
            'readyForSeparateFreeze':ready,'searchExecuted':False,'performanceMeasured':False,
            'reviewerIdentityAuthenticated':False,'independentAuthorshipVerified':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index',type=Path,default=DEFAULT_INDEX)
    parser.add_argument('--draft',type=Path,default=Path(__file__).parent/'review/mentor_questions_v1.json')
    parser.add_argument('--require-review',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(check_review_pack(json.loads(args.draft.read_bytes()),Index(args.index),args.require_review),ensure_ascii=False))
    except (ValueError,KeyError,TypeError) as error:parser.exit(1,str(error)+'\n')
