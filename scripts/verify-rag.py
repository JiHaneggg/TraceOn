"""Read-only administrator integration smoke: actual Neo4j -> rule API -> RAG.

Production agent does not use this administrator adapter; Spring owns DB reads.
Does not overwrite any old rule evaluation or train/tune models.
"""
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "data-pipeline"))
from neo4j_mvp import Database


def post(url, body):
    with urlopen(Request(url, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}), timeout=20) as response:
        return json.load(response)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tx-id", default="TX-000619283")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/rag-neo4j-smoke.json")
    args = parser.parse_args()
    start = perf_counter()
    graph = Database().subgraph(args.tx_id)
    after_graph = perf_counter()
    rule = post("http://127.0.0.1:8091/v1/rules/score", {"graph": graph})
    context = post("http://127.0.0.1:8092/v1/rag/context", {"graph": graph, "ruleResult": rule})
    draft = {"contextId": context["contextId"],
             "facts": [{"factId": row["factId"], "value": row["value"]} for row in context["reportFacts"]],
             "citations": [{"chunkId": row["chunkId"], "quote": row["text"][:60]} for row in context["citations"]]}
    valid = post("http://127.0.0.1:8092/v1/rag/validate", {"graph": graph, "ruleResult": rule, "draft": draft})
    draft["facts"][0]["value"] = "ALTERED"
    rejected = post("http://127.0.0.1:8092/v1/rag/validate", {"graph": graph, "ruleResult": rule, "draft": draft})
    assert valid["valid"] and not rejected["valid"]
    assert set(context["highlightTxIds"]) <= {row["txId"] for row in graph["edges"]}
    result = {"kind": "actual_neo4j_readonly_integration_smoke_not_spring_or_llm_e2e",
              "targetTxId": args.tx_id, "graphEdges": len(graph["edges"]), "ruleScore": rule["ruleScore"],
              "highlightedTransactions": len(context["highlightTxIds"]), "reportFacts": len(context["reportFacts"]),
              "citations": len(context["citations"]), "validDraftAccepted": valid["valid"],
              "alteredDraftRejected": not rejected["valid"],
              "elapsedMs": round((perf_counter() - start) * 1000, 2),
              "graphReadMs": round((after_graph - start) * 1000, 2), "context": context}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    preview = ["# RAG 거래·문서 근거 미리보기", "", "LLM 연동 전 검증용 산출물. 최종 STR 보고서는 사람 검토가 필요합니다.", "",
               f"- 대상 거래: {args.tx_id}", f"- 조회 거래: {len(graph['edges'])}건",
               f"- 룰 점수: {rule['ruleScore']} (확률 아님)",
               f"- 강조할 거래: {', '.join(context['highlightTxIds'])}",
               f"- Context ID: {context['contextId']}", "", "## 계산된 사실", "",
               "| 근거 ID | 값 | 근거 거래 |", "|---|---|---|"]
    for fact in context["reportFacts"]:
        preview.append(f"| {fact['factId']} | {fact['value']} | {', '.join(fact['evidenceTxIds'])} |")
    preview += ["", "## 검색된 공식 안내", ""]
    for citation in context["citations"]:
        preview += [f"- [{citation['title']}]({citation['url']}) — {citation['section']}",
                    f"  - 근거 ID: `{citation['chunkId']}`", f"  - 본문: {citation['text'][:140]}"]
    preview += ["", "## 범위와 제한", ""] + ["- " + value for value in context["limitations"]]
    args.output.with_suffix(".md").write_text("\n".join(preview) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "context"}, ensure_ascii=False))
