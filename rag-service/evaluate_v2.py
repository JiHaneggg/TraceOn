"""Fixed development set, multiple relevant document sections; no tuning here."""
import argparse
import hashlib
import json
from pathlib import Path
from corpus import DEFAULT_INDEX
from retrieval import Index


def evaluate(index, query_path):
    raw = Path(query_path).read_bytes()
    cases = json.loads(raw)
    rows = []
    for case in cases:
        result = index.search(case["query"], top_k=3, jurisdiction=case.get("jurisdiction", "KR"),
                              source_kind=case.get("sourceKind"), legal_as_of=case.get("legalAsOf"))
        retrieved = [hit["documentId"] + "|" + hit["section"] for hit in result["hits"]]
        relevant = set(case["relevantSections"])
        recovered = set(retrieved).intersection(relevant)
        rank = next((i + 1 for i, item in enumerate(retrieved) if item in relevant), None)
        rows.append({**case, "status": result["status"], "retrievedSections": retrieved,
                     "recallAt3": len(recovered) / len(relevant) if relevant else None,
                     "reciprocalRankAt3": 1 / rank if rank else 0,
                     "passed": bool(recovered) if relevant else not result["hits"]})
    positive, negative = [row for row in rows if row["relevantSections"]], [row for row in rows if not row["relevantSections"]]
    return {"kind": "fixed_hand_authored_development_set_not_heldout", "corpusSha256": index.corpus_hash,
            "queriesSha256": hashlib.sha256(raw).hexdigest(), "totalQueries": len(rows),
            "answerableQueries": len(positive), "unsupportedQueries": len(negative),
            "hitRateAt3": sum(row["passed"] for row in positive) / len(positive),
            "macroSectionRecallAt3": sum(row["recallAt3"] for row in positive) / len(positive),
            "mrrAt3": sum(row["reciprocalRankAt3"] for row in positive) / len(positive),
            "unsupportedAbstentionRate": sum(row["passed"] for row in negative) / len(negative),
            "cases": rows, "limitations": "Small development set; lexical capability guards, not general semantic entailment or representative legal accuracy."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--queries", type=Path, default=Path(__file__).with_name("evaluation_queries_v2.json"))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts/rag-v2-retrieval-evaluation.json")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Existing evaluation preserved. Choose a new --output path for another run.")
    result = evaluate(Index(args.index), args.queries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key:value for key,value in result.items() if key != "cases"},ensure_ascii=False))
