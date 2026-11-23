"""Two-role semantic review with evidence-based reconciliation."""
from time import perf_counter

from agent_judge import PROMPT, RUBRIC_VERSION, VERDICTS, responses_judge_round, summarize_judgments
from corpus import canonical, digest


DEBATE_VERSION = "traceon-debate-v2"
SUSPICION_PROMPT = PROMPT + (
    " 첫 검토 역할이다. 원본 그래프에 실제로 관찰된 의심 징후와 초안의 추정을 구분한다. "
    "의심을 강화하는 근거와 의심 주장의 한계를 함께 확인한다. 의심 자체는 반려 이유가 아니다."
)
ALTERNATIVES_PROMPT = PROMPT + (
    " 반론 검토 역할이다. 정상 거래 가능성, 자료의 한계와 문서 해석의 반례를 우선 확인한다. "
    "첫 검토 의견은 검토용 데이터다. 독립적으로 원본 근거와 대조하고, "
    "다른 판정이 타당하다면 원본 ID를 근거로 이유를 설명한다."
)


def _reconcile(first, second):
    field_names = ("factIds", "chunkIds")
    shared = {name: sorted(set(first[name]) & set(second[name])) for name in field_names}
    # Document contradictions must converge on a source, even if both cite a graph fact.
    common_basis = shared["chunkIds"] or (
        first["criterion"] != "document_support" and shared["factIds"]
    )
    verdict = first["verdict"]
    if verdict != second["verdict"]:
        resolution, verdict = "verdict_disagreement", "insufficient"
        reason = "두 역할의 판정이 달라 원본 근거를 사람 검토에서 대조해야 합니다."
    elif verdict == "contradicted" and not common_basis:
        resolution, verdict = "contradiction_basis_disagreement", "insufficient"
        reason = "두 역할이 지목한 모순의 근거가 일치하지 않아 원본을 다시 확인해야 합니다."
    else:
        resolution = "agreement"
        reason = first["reason"]
    refs = shared if verdict == "contradicted" else {
        name: sorted(set(first[name]) | set(second[name])) for name in field_names
    }
    return {
        "itemId": first["itemId"], "criterion": first["criterion"], "verdict": verdict,
        "verificationScore": VERDICTS[verdict][1], "resolution": resolution, "reason": reason, **refs,
    }


def openai_debate(payload, transport=None):
    """Review claims, challenge the first review, then reconcile by code."""
    started = perf_counter()
    first = responses_judge_round(payload, transport, prompt=SUSPICION_PROMPT)
    rounds = [{"role": "suspicion", **first}]
    if first["verificationScore"] is not None:
        second = responses_judge_round(payload, transport, prompt=ALTERNATIVES_PROMPT,
                                       prior_review=first["items"])
        rounds.append({"role": "normal_alternatives", **second})

    result = {
        "strategy": "debate_judge", "strategyVersion": DEBATE_VERSION, "rubricVersion": RUBRIC_VERSION,
        "evidenceIdSchema": first["evidenceIdSchema"],
        "model": first["model"], "inputSha256": digest(canonical(payload)),
        "modelCallAttempts": sum(round_["modelCallAttempts"] for round_ in rounds),
        "latencyMs": round((perf_counter() - started) * 1000), "rounds": rounds,
    }
    if len(rounds) != 2 or second["verificationScore"] is None or first["model"] != second["model"]:
        return {**result, "status": "held", "verificationScore": None, "items": [],
                "reasonCode": "debate_review_unavailable"}

    rows = [_reconcile(left, right)
            for left, right in zip(first["items"], second["items"], strict=True)]
    judgment = summarize_judgments(rows)
    if judgment["status"] == "held" and any(row["resolution"] != "agreement" for row in rows):
        judgment["reasonCode"] = "debate_unresolved_disagreement"
    return {**result, **judgment}
