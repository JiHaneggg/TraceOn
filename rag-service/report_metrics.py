"""Mechanical report metrics with explicit denominators and coverage.

The audited numeric scope is the ID-bound fact table, controlled observation
section, and query-window line. Numbers in source quotations, document versions
and IDs are not treated as ledger amounts. This is not a free-prose semantic
judge, source applicability test or authentication of the upstream ledger.
"""
from difflib import SequenceMatcher
import re

from evidence import fact_category

FACT_ROW = re.compile(r'^\|\s*([^|\r\n]+?)\s*\|\s*([^|\r\n]*)\|[^|\r\n]*\|\s*$', re.M)
NUMBER = re.compile(r'(?<![A-Za-z0-9])(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}|(?:약\s*)?\d+(?:[.,:]\d+)*)(?![A-Za-z0-9])')
ARTICLE = re.compile(r'제\s*(\d+)\s*조(?:\s*의\s*(\d+))?(?:\s*제\s*(\d+)\s*항)?')


def ratio(matched,total):
    return matched/total if total else None


def numeric_fact(fact_id):
    return fact_category(fact_id) in {'amounts','times','counts'} or fact_id.endswith(('fromAccountKey','toAccountKey')) or fact_id=='rule.score'


def controlled_numbers(markdown):
    section = re.search(r'## 관찰한 거래와 탐지 근거\s*\n(.*?)\n## 확인된 수치·거래',markdown,re.S)
    text = section.group(1) if section else ''
    text = '\n'.join(line for line in text.splitlines() if not line.startswith('근거 ID:'))
    window = re.search(r'^조회 기간: (.+)$',markdown,re.M)
    if window: text += '\n'+window.group(1)
    return NUMBER.findall(text)


def article_tags(text):
    return [tuple(value or None for value in match) for match in ARTICLE.findall(text)]


def audit_report(context,report,expected_markdown=None):
    markdown=report.get('markdown','')
    if not isinstance(markdown,str):markdown=''
    expected={r['factId']:str(r['value']) for r in context['reportFacts'] if numeric_fact(r['factId'])}
    found=[]
    for fact_id,value in FACT_ROW.findall(markdown):
        if fact_id in expected or fact_id.startswith(('target.','finding.','scope.','rule.')):
            if numeric_fact(fact_id) or fact_id not in {r['factId'] for r in context['reportFacts']}:
                found.append((fact_id,value.strip()))
    seen=set();table_matches=0;duplicates=0;missing=[]
    for key,value in found:
        if key in seen:duplicates+=1
        elif key in expected and value==expected[key]:table_matches+=1
        seen.add(key)
    missing=sorted(set(expected)-seen)
    actual_numbers=controlled_numbers(markdown)
    expected_numbers=controlled_numbers(expected_markdown) if isinstance(expected_markdown,str) else []
    # Sequence alignment counts inserts/deletions as mismatches without shifting
    # every following correct occurrence. No matching to an unrelated number bag.
    aligned=SequenceMatcher(a=expected_numbers,b=actual_numbers,autojunk=False)
    narrative_matches=sum(block.size for block in aligned.get_matching_blocks()) if expected_markdown is not None else 0
    narrative_denominator=max(len(expected_numbers),len(actual_numbers))
    total=len(found)+narrative_denominator
    matched=table_matches+narrative_matches
    chunks={row['chunkId']:row for row in context['citations']}
    claims=report.get('draft',{}).get('citations',[]) if isinstance(report.get('draft'),dict) else []
    if not isinstance(claims,list):claims=[]
    quote_matches=0
    for claim in claims:
        if not isinstance(claim,dict):continue
        row=chunks.get(claim.get('chunkId')) if isinstance(claim.get('chunkId'),str) else None
        quote=claim.get('quote')
        quote_matches+=bool(row and isinstance(quote,str) and quote.strip() and quote in row['text'])
    tags=article_tags(markdown)
    available={tag for row in context['citations'] for tag in article_tags(row['section']+' '+row['text'])}
    tag_matches=sum(tag in available for tag in tags)
    return {'numericFacts':{'matched':matched,'total':total,'matchRate':ratio(matched,total),
                           'tableMatched':table_matches,'tableClaims':len(found),'expectedTableFacts':len(expected),
                           'tableCoverage':ratio(len(set(expected)&seen),len(expected)),
                           'duplicateTableClaims':duplicates,'missingFactIds':missing,
                           'controlledNarrativeMatched':narrative_matches,'controlledNarrativeTotal':narrative_denominator,
                           'scope':'id_bound_fact_table_controlled_observations_and_query_window',
                           'unrestrictedProseFullyAudited':False},
            'citations':{'matched':quote_matches,'total':len(claims),'groundingPrecision':ratio(quote_matches,len(claims)),
                         'scope':'allowlisted_context_chunk_id_and_verbatim_quote','semanticEntailmentVerified':False},
            'articleTags':{'matched':tag_matches,'total':len(tags),'presencePrecision':ratio(tag_matches,len(tags)),
                           'scope':'article_tag_occurs_in_retrieved_context_not_statutory_interpretation'},
            'semanticReview':{'measured':False,'normalCommerceReasoningVerified':False,'humanReviewRequired':True}}
