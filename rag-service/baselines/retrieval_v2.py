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

ALIASES = {"str": "의심거래보고", "ctr": "고액현금거래보고", "cdd": "고객확인", "kyc": "고객확인"}


def tokenize(text):
    words = re.findall(r"[a-z0-9]+|[가-힣]+", text.lower())
    tokens = []
    for word in words:
        word = ALIASES.get(word, word)
        if len(word) >= 2:
            tokens.append("w:" + word)
        if re.fullmatch(r"[가-힣]+", word):
            tokens.extend("c:" + word[i:i + 2] for i in range(len(word) - 1))
    return tokens


class Index:
    def __init__(self, path=DEFAULT_INDEX):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        checksum = payload.pop("corpusSha256")
        if checksum != digest(canonical(payload)) or payload["schemaVersion"] != "1.0":
            raise ValueError("Corpus checksum/version mismatch; rebuild the corpus")
        self.corpus_hash = checksum
        self.chunks = payload["chunks"]
        self.documents = payload["documents"]
        self.by_id = {row["chunkId"]: row for row in self.chunks}
        if not self.chunks or len(self.by_id) != len(self.chunks):
            raise ValueError("Empty or duplicate corpus")
        for row in self.chunks:
            if row["textSha256"] != digest(row["text"]):
                raise ValueError("Chunk checksum mismatch")
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
        # Explicit source requirements and recognizable scope requests fail closed.
        # This is a bounded lexical guard, not a general semantic entailment model.
        required_kind = source_kind
        if re.search(r"내규|내부.{0,16}(규정|점수|룰|기준)|internal\s+(policy|policies|score)", query, re.I):
            required_kind = "bank_internal_policy"
        elif re.search(r"점수|스코어|모델.{0,8}확률", query) and re.search(r"기준|신고|보고|확정|의무", query):
            required_kind = "score_reporting_policy"
        elif legal_as_of or re.search(r"법령|법률|시행일|시행령|조문|제\s*\d+\s*조", query):
            required_kind = "law"
        scope_supported = not required_kind or any(row["sourceKind"] == required_kind for row in available)
        if required_kind == "law" and legal_as_of:
            scope_supported = any(row["sourceKind"] == "law" and row.get("effectiveDate")
                                  and row.get("validUntil") and row["effectiveDate"] <= legal_as_of < row["validUntil"]
                                  for row in available)
        candidates = []
        expanded = query
        for term, english in {"집금": "funnel account multiple deposits", "분산": "multiple beneficiaries wire transfers",
                              "쪼개기": "structuring reporting threshold", "신속": "rapid movement funds"}.items():
            if term in query:
                expanded += " " + english
        query_tokens = set(tokenize(expanded))
        for row, counter in zip(self.chunks, self.tokens):
            if not scope_supported:
                break
            if jurisdiction != "ALL" and row["jurisdiction"] != jurisdiction:
                continue
            if required_kind and row["sourceKind"] != required_kind:
                continue
            if legal_as_of and not (row.get("effectiveDate") and row.get("validUntil")
                                    and row["effectiveDate"] <= legal_as_of < row["validUntil"]):
                continue
            coverage = len(query_tokens.intersection(counter)) / max(1, len(query_tokens))
            score = 0.0
            for term in query_tokens.intersection(counter):
                df, tf = self.df[term], counter[term]
                idf = math.log(1 + (len(self.chunks) - df + 0.5) / (df + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * sum(counter.values()) / self.avg_len))
            # Explicit abstention is heuristic, not proof that a question is answerable.
            if score >= 1.2 and coverage >= 0.22:
                candidates.append({**row, "retrievalScore": round(score, 6), "queryCoverage": round(coverage, 4)})
        candidates.sort(key=lambda row: (-row["retrievalScore"], row["chunkId"]))
        hits = candidates[:top_k]
        return {"schemaVersion": "1.0", "query": query,
                "status": "insufficient_scope" if not scope_supported else ("matched" if hits else "no_evidence"),
                "answerability": "insufficient_scope" if not scope_supported else "not_assessed",
                "abstentionReason": "Required source kind/version is not in the requested corpus scope" if not scope_supported else None,
                "corpusCoverage": {"officialGuidance": any(r["sourceKind"] == "official_guidance" for r in available),
                                   "statutoryText": any(r["sourceKind"] == "law" for r in available),
                                   "bankInternalPolicies": any(r["sourceKind"] == "bank_internal_policy" for r in available),
                                   "patternSpecificTypologies": any(r["sourceKind"] == "foreign_typology" for r in available)},
                "method": "bm25_words_korean_bigrams", "corpusSha256": self.corpus_hash,
                "filters": {"jurisdiction": jurisdiction, "sourceKind": source_kind, "legalAsOf": legal_as_of}, "hits": hits,
                "notice": "검색은 관련 본문을 반환합니다. 의미적 뒷받침·현재 법률 적용은 별도 검토가 필요하며 외국 사례를 한국 기준으로 적용하지 않습니다."}

    def verify_citation(self, chunk_id, quote):
        row = self.by_id.get(chunk_id)
        return bool(row and isinstance(quote, str) and 1 <= len(quote) <= 800 and quote in row["text"])
