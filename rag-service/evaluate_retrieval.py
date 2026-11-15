"""Small development retrieval check, not held-out legal/LLM accuracy."""
import argparse
import json
from pathlib import Path
from corpus import DEFAULT_INDEX
from retrieval import Index


def evaluate(index):
    queries = json.loads(Path(__file__).with_name("evaluation_queries.json").read_text(encoding="utf-8"))
    rows = []
    for case in queries:
        result = index.search(case["query"])
        rank = next((i + 1 for i, hit in enumerate(result["hits"])
                     if hit["documentId"] == case["documentId"] and (
                         "section" not in case or hit["section"] == case["section"])), None)
        rows.append({**case, "status": result["status"], "rank": rank,
                     "hitDocuments": [hit["documentId"] for hit in result["hits"]],
                     "passed": rank is not None if case["documentId"] else not result["hits"]})
    answerable = [row for row in rows if row["documentId"]]
    unsupported = [row for row in rows if not row["documentId"]]
    return {"kind": "small_development_retrieval_check_not_heldout", "corpusSha256": index.corpus_hash,
            "answerableQueries": len(answerable),
            "hitRateAt3": sum(row["rank"] is not None for row in answerable) / len(answerable),
            "mrrAt3": sum(1 / row["rank"] if row["rank"] else 0 for row in answerable) / len(answerable),
            "unsupportedAbstentionRate": sum(row["passed"] for row in unsupported) / len(unsupported),
            "cases": rows,
            "limitations": "Small hand-authored development set. Single expected passage/document per query. Not multi-relevant Recall@k or semantic grounding/production quality."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts/rag-retrieval-evaluation.json")
    args = parser.parse_args()
    result = evaluate(Index(args.index))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False))
