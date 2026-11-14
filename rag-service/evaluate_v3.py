"""Paired frozen-corpus evaluation: old chunk ranking vs source-section grouping.

Primary top-3 metrics use equal hit budgets. Expanded-context metrics separately
count bounded supporting chunks and do not imply equal context/token budgets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from corpus import DEFAULT_INDEX, canonical, digest
from retrieval import Index, section_key

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = Path(__file__).with_name("evaluation_queries_v3.json")


def load_questions(index, path=QUESTIONS):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if data["corpusSha256"] != index.corpus_hash:
        raise ValueError("Gold evidence belongs to a different corpus snapshot")
    ids = [case["id"] for case in data["cases"]]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("Empty or duplicate query IDs")
    for case in data["cases"]:
        if case["split"] not in {"development", "verification"}:
            raise ValueError("Unknown question split")
        for unit in case["requiredEvidence"]:
            target = data["sections"][unit["sectionId"]]
            if not isinstance(unit["quote"], str) or not unit["quote"].strip():
                raise ValueError("An evidence quote anchor is required")
            if not any(row["documentId"] == target["documentId"] and row["section"] == target["section"]
                       and unit["quote"] in row["text"] for row in index.chunks):
                raise ValueError("Gold anchor not present in the frozen corpus: " + case["id"])
    return data, hashlib.sha256(raw).hexdigest()


def evaluate_case(index, case, sections, top_k=3):
    result = index.search(case["query"], top_k=top_k, jurisdiction=case.get("jurisdiction", "KR"),
                          source_kind=case.get("sourceKind"), legal_as_of=case.get("legalAsOf"))
    primary = result["hits"]
    expanded = list({row["chunkId"]: row for hit in primary
                     for row in [hit] + hit.get("supportingChunks", [])}.values())
    expected_sections = {(sections[unit["sectionId"]]["documentId"], sections[unit["sectionId"]]["section"])
                         for unit in case["requiredEvidence"]}
    actual_sections = {(row["documentId"], row["section"]) for row in primary}
    def supports(unit, rows):
        target = sections[unit["sectionId"]]
        return any(row["documentId"] == target["documentId"] and row["section"] == target["section"]
                   and unit["quote"] in row["text"] for row in rows)
    units = case["requiredEvidence"]
    rank = next((i + 1 for i, row in enumerate(primary) if (row["documentId"], row["section"]) in expected_sections), None)
    duplicates = len(primary) - len({section_key(row) for row in primary})
    return {**case, "status": result["status"], "answerability": result["answerability"],
            "retrieved": [{"chunkId":row["chunkId"], "documentId":row["documentId"],
                           "section":row["section"], "score":row["retrievalScore"],
                           "supportingChunkIds":[child["chunkId"] for child in row.get("supportingChunks", [])]}
                          for row in primary],
            "primaryHits": len(primary), "expandedChunks": len(expanded),
            "contextCharacters": sum(len(row["text"]) for row in expanded),
            "duplicateSlots": duplicates,
            "sectionRecallAt3": len(actual_sections & expected_sections) / len(expected_sections) if units else None,
            "reciprocalRankAt3": 1 / rank if rank else 0,
            "primaryEvidenceRecallAt3": sum(supports(unit, primary) for unit in units) / len(units) if units else None,
            "contextEvidenceRecall": sum(supports(unit, expanded) for unit in units) / len(units) if units else None,
            "hit": rank is not None,
            "abstained": not primary and result["status"] in {"no_evidence", "insufficient_scope"},
            "unverifiedRelatedPassages": bool(primary) and not units,
            "missingPrimaryEvidence": [unit for unit in units if not supports(unit, primary)],
            "missingContextEvidence": [unit for unit in units if not supports(unit, expanded)]}


def summarize(rows):
    positive = [row for row in rows if row["requiredEvidence"]]
    negative = [row for row in rows if not row["requiredEvidence"]]
    avg = lambda values: sum(values) / len(values) if values else None
    return {"queries":len(rows), "answerable":len(positive), "unsupported":len(negative),
            "hitRateAt3":avg([row["hit"] for row in positive]),
            "macroSectionRecallAt3":avg([row["sectionRecallAt3"] for row in positive]),
            "mrrAt3":avg([row["reciprocalRankAt3"] for row in positive]),
            "macroPrimaryEvidenceRecallAt3":avg([row["primaryEvidenceRecallAt3"] for row in positive]),
            "macroContextEvidenceRecall":avg([row["contextEvidenceRecall"] for row in positive]),
            "allRequiredContextEvidenceRate":avg([row["contextEvidenceRecall"] == 1 for row in positive]),
            "unsupportedAbstentionRate":avg([row["abstained"] for row in negative]),
            "duplicateSlotRate":sum(row["duplicateSlots"] for row in rows) / max(1,sum(row["primaryHits"] for row in rows)),
            "meanPrimaryHits":avg([row["primaryHits"] for row in positive]),
            "meanExpandedChunks":avg([row["expandedChunks"] for row in positive]),
            "meanContextCharacters":avg([row["contextCharacters"] for row in positive])}


def fingerprint(index, questions, query_hash):
    files = [Path(__file__), Path(__file__).with_name("retrieval.py"), Path(__file__).with_name("evidence.py")]
    return {"schemaVersion":"3.0", "corpusSha256":index.corpus_hash, "questionsSha256":query_hash,
            "implementationSha256":{file.name:digest(file.read_bytes()) for file in files},
            "splits":dict(Counter(case["split"] for case in questions["cases"])),
            "topK":3, "candidatePolicy":"sections", "baselinePolicy":"chunks",
            "maxSupportingChunksPerHit":2,
            "interpretation":"Both splits are hand-authored. Verification is fixed before execution, not an independently adjudicated benchmark."}


def compare(index_path, query_path, split):
    baseline, candidate = Index(index_path, selection_policy="chunks"), Index(index_path, selection_policy="sections")
    questions, query_hash = load_questions(candidate, query_path)
    cases = [case for case in questions["cases"] if case["split"] == split]
    old = [evaluate_case(baseline, case, questions["sections"]) for case in cases]
    new = [evaluate_case(candidate, case, questions["sections"]) for case in cases]
    changes = [{"id":a["id"], "query":a["query"],
                "sectionRecallBefore":a["sectionRecallAt3"], "sectionRecallAfter":b["sectionRecallAt3"],
                "primaryEvidenceRecallBefore":a["primaryEvidenceRecallAt3"], "primaryEvidenceRecallAfter":b["primaryEvidenceRecallAt3"],
                "contextEvidenceRecallBefore":a["contextEvidenceRecall"], "contextEvidenceRecallAfter":b["contextEvidenceRecall"],
                "duplicateSlotsBefore":a["duplicateSlots"], "duplicateSlotsAfter":b["duplicateSlots"]}
               for a,b in zip(old,new) if any(a[key] != b[key] for key in
                   ("sectionRecallAt3","primaryEvidenceRecallAt3","contextEvidenceRecall","duplicateSlots"))]
    return {"kind":"paired_fixed_corpus_retrieval_check", "split":split,
            "fingerprint":fingerprint(candidate,questions,query_hash),
            "baseline":summarize(old), "candidate":summarize(new), "changes":changes,
            "failures":[row for row in new if row["missingContextEvidence"] or row["unverifiedRelatedPassages"]],
            "baselineCases":old, "candidateCases":new,
            "limitations":["Small hand-authored set; not general legal or semantic accuracy.",
                           "Primary top-3 metrics use equal hit budgets. Context recall includes up to two supporting chunks per primary hit and a larger text budget.",
                           "Quote anchors check exact evidence availability, not entailment or correctness of generated prose.",
                           "Unsupported queries may return related passages marked not_assessed; this is not an answered customer fact."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index",type=Path,default=DEFAULT_INDEX)
    parser.add_argument("--queries",type=Path,default=QUESTIONS)
    parser.add_argument("--freeze",action="store_true")
    parser.add_argument("--manifest",type=Path,default=ROOT / "artifacts/rag-v3-frozen.json")
    parser.add_argument("--split",choices=["development","verification"],default="development")
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    index = Index(args.index)
    questions, query_hash = load_questions(index,args.queries)
    current = fingerprint(index,questions,query_hash)
    if args.freeze:
        if args.manifest.exists():raise SystemExit("Existing frozen manifest preserved")
        args.manifest.parent.mkdir(parents=True,exist_ok=True)
        args.manifest.write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(current,ensure_ascii=False))
    else:
        if not args.manifest.exists() or json.loads(args.manifest.read_text(encoding="utf-8")) != current:
            raise SystemExit("Implementation/questions/corpus changed or not frozen. Preserve prior results and explicitly version a new run.")
        output = args.output or ROOT / f"artifacts/rag-v3-{args.split}.json"
        if output.exists():raise SystemExit("Existing result preserved; choose a new --output")
        result = compare(args.index,args.queries,args.split)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({key:result[key] for key in ("split","baseline","candidate")},ensure_ascii=False))
