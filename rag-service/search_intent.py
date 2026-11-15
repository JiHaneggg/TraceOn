"""Bounded concept/section coverage for definition and comparison questions.

Only already eligible passages may be promoted; original BM25 scores, text and
citation IDs remain unchanged. This is lexical routing, not answer generation.
"""
import re

CONCEPTS = {
    "STR": r"(?<![a-z])str(?![a-z])|의심\s*거래\s*보고|의심거래",
    "CTR": r"(?<![a-z])ctr(?![a-z])|고액\s*현금\s*거래",
    "CDD": r"(?<![a-z])(?:cdd|kyc)(?![a-z])|고객\s*확인",
}


def concepts(text):
    return {name for name, pattern in CONCEPTS.items() if re.search(pattern, text, re.I)}


def prioritize(candidates, query):
    requested = concepts(query)
    comparison = len(requested) >= 2 and bool(re.search(r"차이|구분|각각|다른|다르|비교|difference|compare", query, re.I))
    definition = bool(requested) and bool(re.search(r"정의|개념|의미|뜻|이란|무엇|어떤\s*경우|what\s+is|definition", query, re.I))
    procedural = bool(re.search(r"보고서|기재|직원|보고책임자|FAX|전화|절차|양식", query, re.I))
    intent = "comparison" if comparison else "definition" if definition and not procedural else "lexical"
    picked, covered = [], set()
    if intent != "lexical":
        for row in candidates:
            if not re.search(r"정의|개념|definition|concept", row["section"], re.I):
                continue
            matches = concepts(row["title"]) & requested
            if matches - covered:
                picked.append(row)
                covered.update(matches)
                if covered >= requested:
                    break
    ids = {row["chunkId"] for row in picked}
    return picked + [row for row in candidates if row["chunkId"] not in ids], {
        "intent": intent, "requestedConcepts": sorted(requested),
        "prioritizedChunkIds": [row["chunkId"] for row in picked],
        "notice": "Eligible concept sections are prioritized; retrieval does not establish semantic entailment."}
