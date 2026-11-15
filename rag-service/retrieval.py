"""Local BM25 retrieval using words and Korean bigrams; no embeddings/API key.

Scores rank passages, not probabilities or legal applicability. Text is data.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from datetime import date
import json

from corpus import DEFAULT_INDEX, canonical, digest
from query_processing import ENGLISH_STOP, english_term, expand_query, missing_capability
from search_intent import prioritize
from source_presentation import source_display, source_groups, law_selector, matches_law_selector, named_statutes
from evidence_fit import validate_policy
from query_scope import assess_scope

ALIASES = {"str": "의심거래보고", "ctr": "고액현금거래보고", "cdd": "고객확인", "kyc": "고객확인"}


def tokenize(text):
    words = re.findall(r"[a-z0-9]+|[가-힣]+", text.lower())
    tokens = []
    for word in words:
        word = ALIASES.get(word, word)
        if re.fullmatch(r"[a-z]+", word):
            if word in ENGLISH_STOP:
                continue
            word = english_term(word)
        if len(word) >= 2:
            tokens.append("w:" + word)
        if re.fullmatch(r"[가-힣]+", word):
            tokens.extend("c:" + word[i:i + 2] for i in range(len(word) - 1))
    return tokens


class Index:
    def __init__(self, path=DEFAULT_INDEX, *, selection_policy="sections"):
        if selection_policy not in {"sections", "chunks"}:
            raise ValueError("Unknown retrieval selection policy")
        self.selection_policy = selection_policy
        self.path = Path(path).resolve()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        checksum = payload.pop("corpusSha256")
        if checksum != digest(canonical(payload)) or payload["schemaVersion"] != "1.0":
            raise ValueError("Corpus checksum/version mismatch; rebuild the corpus")
        self.corpus_hash = checksum
        self.presentation_version = payload.get("evidencePresentationVersion")
        if self.presentation_version not in {None, "1.0"}:
            raise ValueError("Unsupported evidence presentation version")
        self.query_scope_policy_version = payload.get("queryScopePolicyVersion")
        if self.query_scope_policy_version not in {None, "1.0"}:
            raise ValueError("Unsupported query scope policy version")
        self.chunks = payload["chunks"]
        self.documents = payload["documents"]
        self.by_id = {row["chunkId"]: row for row in self.chunks}
        if not self.chunks or len(self.by_id) != len(self.chunks):
            raise ValueError("Empty or duplicate corpus")
        for row in self.chunks:
            if row["textSha256"] != digest(row["text"]):
                raise ValueError("Chunk checksum mismatch")
        self.evidence_fit_policy = payload.get("evidenceFitPolicy")
        if self.evidence_fit_policy is not None:
            if self.presentation_version != "1.0":
                raise ValueError("Evidence fitness requires source presentation version 1.0")
            validate_policy(self.evidence_fit_policy)
        self.tokens = [Counter(tokenize(row["title"] + " " + row["section"] + " " + row["text"]))
                       for row in self.chunks]
        self.df = Counter(t for counter in self.tokens for t in counter)
        self.avg_len = sum(map(lambda c: sum(c.values()), self.tokens)) / len(self.tokens)

    def search(self, query, top_k=3, jurisdiction="KR", source_kind=None, legal_as_of=None):
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 500:
            raise ValueError("query must contain 2..500 characters")
        if type(top_k) is not int or not 1 <= top_k <= 8:
            raise ValueError("topK must be an integer in 1..8")
        if jurisdiction not in {"KR", "US", "CA", "ALL"} or source_kind not in {None, "official_guidance", "law", "foreign_typology", "bank_internal_policy"}:
            raise ValueError("Unsupported source filter")
        if legal_as_of is not None:
            if not isinstance(legal_as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", legal_as_of):
                raise ValueError("legalAsOf must be YYYY-MM-DD")
            date.fromisoformat(legal_as_of)
        available = [row for row in self.chunks if jurisdiction == "ALL" or row["jurisdiction"] == jurisdiction]
        scoped = None
        if self.query_scope_policy_version:
            scoped = assess_scope(query,[row for row in available if source_kind is None or row["sourceKind"]==source_kind])
            available = [row for row in available if row["chunkId"] in scoped["allowedChunkIds"]]
        # Explicit source requirements and recognizable scope requests fail closed.
        # This is a bounded lexical guard, not a general semantic entailment model.
        required_kind = source_kind
        if re.search(r"내규|내부.{0,16}(규정|점수|룰|기준)|internal\s+(policy|policies|score)", query, re.I):
            required_kind = "bank_internal_policy"
        elif re.search(r"점수|스코어|모델.{0,8}확률", query) and re.search(r"기준|신고|보고|확정|의무", query):
            required_kind = "score_reporting_policy"
        elif legal_as_of or re.search(r"법령|법률|시행일|시행령|조문|제\s*\d+\s*조", query):
            required_kind = "law"
        absent = missing_capability(query)
        if scoped and scoped["genericCustomerProcess"] and absent == "case_specific_customer_data":
            absent = None
        if absent:
            required_kind = absent
        if scoped and scoped["overrideRequiredKind"]:
            required_kind = scoped["requiredKind"] if scoped["requiredKind"] is not None else source_kind
        articles, level = law_selector(query) if self.presentation_version else (set(), None)
        names = named_statutes(query) if self.presentation_version else []
        if self.presentation_version and required_kind in {None, "law"} and (articles or level or names):
            required_kind = "law"
        if required_kind == "law" and (articles or level or names):
            available = [row for row in available if matches_law_selector(row, articles, level, names)]
        scope_supported = not required_kind or any(row["sourceKind"] == required_kind for row in available)
        if self.presentation_version and source_kind is not None and required_kind != source_kind:
            scope_supported = False
        if self.presentation_version and required_kind == "law" and legal_as_of is None and re.search(r"(?:19|20)\d{2}(?:년|-\d{2}-\d{2}|\.\s*\d{1,2}\.)", query):
            # A date in prose is not a verified applicability interval.
            scope_supported = False
        if required_kind == "law" and legal_as_of:
            scope_supported = any(row["sourceKind"] == "law" and row.get("effectiveDate")
                                  and row.get("validUntil") and row["effectiveDate"] <= legal_as_of < row["validUntil"]
                                  for row in available)
        if scoped and scoped["assessment"]["decision"] == "abstain":
            scope_supported = False
        elif scoped and not scope_supported:
            reason = "요청한 자료 종류·조문·시점이 현재 검색 범위에 없습니다."
            scoped["assessment"].update({"decision":"abstain","wholeRequestAbstained":True,
                                         "reasonCodes":["requested_scope_unavailable"],"message":reason})
            scoped["assessment"]["requirements"].append({"kind":"existing_scope_filter","requested":required_kind,
                "available":False,"reasonCode":"requested_scope_unavailable","message":reason})
        candidates = []
        expanded = expand_query(query)
        query_tokens = set(tokenize(expanded))
        for row, counter in zip(self.chunks, self.tokens):
            if not scope_supported:
                break
            if jurisdiction != "ALL" and row["jurisdiction"] != jurisdiction:
                continue
            if required_kind and row["sourceKind"] != required_kind:
                continue
            if required_kind == "law" and (articles or level or names) and not matches_law_selector(row, articles, level, names):
                continue
            if legal_as_of and not (row.get("effectiveDate") and row.get("validUntil")
                                    and row["effectiveDate"] <= legal_as_of < row["validUntil"]):
                continue
            coverage = len(query_tokens.intersection(counter)) / max(1, len(query_tokens))
            # Mixed Korean/English queries should not be rejected merely because
            # foreign passages contain no Korean. Require >=2 English content
            # tokens and at least two matches before using that language's coverage.
            english_tokens = {term for term in query_tokens if term.startswith("w:") and re.fullmatch(r"w:[a-z]+", term)}
            english_matches = english_tokens.intersection(counter)
            if row["jurisdiction"] != "KR" and len(english_tokens) >= 2 and len(english_matches) >= 2:
                coverage = max(coverage, len(english_matches) / len(english_tokens))
            score = 0.0
            for term in sorted(query_tokens.intersection(counter)):
                df, tf = self.df[term], counter[term]
                idf = math.log(1 + (len(self.chunks) - df + 0.5) / (df + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * sum(counter.values()) / self.avg_len))
            # Explicit abstention is heuristic, not proof that a question is answerable.
            if score >= 1.2 and coverage >= 0.22:
                candidates.append({**row, "retrievalScore": round(score, 6), "queryCoverage": round(coverage, 4)})
        candidates.sort(key=lambda row: (-row["retrievalScore"], row["chunkId"]))
        ranking = {"intent": "lexical", "requestedConcepts": [], "prioritizedChunkIds": []}
        if self.selection_policy == "sections":
            candidates, ranking = prioritize(candidates, query)
        hits, selection = select_hits(candidates, top_k, self.selection_policy)
        presentation = {}
        if scoped:
            presentation["scopeAssessment"] = scoped["assessment"]
        if self.presentation_version:
            for hit in hits:
                for row in [hit, *hit["supportingChunks"]]:
                    row["sourceDisplay"] = source_display(row)
            presentation.update({"sourceGroups": source_groups(hits), "evidencePresentationVersion": self.presentation_version})
        return {"schemaVersion": "1.0", "query": query, **presentation,
                "status": "insufficient_scope" if not scope_supported else ("matched" if hits else "no_evidence"),
                "answerability": "insufficient_scope" if not scope_supported else "not_assessed",
                "abstentionReason": (scoped["assessment"]["message"] if scoped else "Required source kind/version is not in the requested corpus scope") if not scope_supported else None,
                "corpusCoverage": {"officialGuidance": any(r["sourceKind"] == "official_guidance" for r in available),
                                   "statutoryText": any(r["sourceKind"] == "law" for r in available),
                                   "bankInternalPolicies": any(r["sourceKind"] == "bank_internal_policy" for r in available),
                                   "patternSpecificTypologies": any(r["sourceKind"] == "foreign_typology" for r in available)},
                "method": "bm25_words_korean_bigrams", "corpusSha256": self.corpus_hash,
                "selection": selection,
                "ranking": ranking,
                "filters": {"jurisdiction": jurisdiction, "sourceKind": source_kind, "legalAsOf": legal_as_of}, "hits": hits,
                "notice": "검색은 관련 본문을 반환합니다. 의미적 뒷받침·현재 법률 적용은 별도 검토가 필요하며 외국 사례를 한국 기준으로 적용하지 않습니다."}

    def verify_citation(self, chunk_id, quote):
        row = self.by_id.get(chunk_id)
        return bool(row and isinstance(quote, str) and 1 <= len(quote) <= 800 and quote in row["text"])


def section_key(row):
    """Never collapse different authorities, jurisdictions, source versions or legal intervals."""
    return tuple(row.get(key) for key in ("documentId", "sourceSha256", "jurisdiction",
                                         "sourceKind", "section", "effectiveDate", "validUntil"))


def select_hits(candidates, top_k, policy="sections"):
    """Keep the highest BM25 chunk per source section. Do not merge/rewrite text.

    Distinct sections of one document remain eligible. No unrelated source is
    promoted just to fill slots. `chunks` reproduces the previous ranking for
    controlled offline comparisons and is not exposed as an HTTP input.
    """
    groups = {}
    for row in candidates:
        groups.setdefault(section_key(row), []).append(row)
    ranked = [rows[0] for rows in groups.values()] if policy == "sections" else candidates
    hits = []
    for row in ranked[:top_k]:
        siblings = groups[section_key(row)]
        alternatives = [item for item in siblings if item["chunkId"] != row["chunkId"]]
        hits.append({**row, "selectionGroupId": "SEC-" + digest(canonical(section_key(row)))[:20],
                     "alternateChunkIds": [item["chunkId"] for item in alternatives[:16]],
                     "supportingChunks": alternatives[:2] if policy == "sections" else [],
                     "omittedSiblingCount": max(0, len(alternatives) - 2) if policy == "sections" else 0})
    return hits, {"policy": policy, "eligibleChunks": len(candidates), "eligibleSections": len(groups),
                  "suppressedSiblingChunks": len(candidates) - len(groups) if policy == "sections" else 0,
                  "returnedHits": len(hits), "returnedDocuments": len({row["documentId"] for row in hits}),
                  "primaryHitsAreRepresentatives": policy == "sections",
                  "supportingChunks": sum(len(row["supportingChunks"]) for row in hits),
                  "maxSupportingChunksPerHit": 2 if policy == "sections" else 0}
