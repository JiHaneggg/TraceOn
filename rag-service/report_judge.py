"""Judge the exact, verified backend draft without generating a replacement.

The model, key and transport are owned by this RAG process. The HTTP caller
supplies only the pinned graph, rules and already-prepared report.
"""
from copy import deepcopy
from datetime import datetime, timezone
import secrets

from agent_judge import (openai_judge, build_judge_input, decide, InvalidJudgeResponse,
                         judge_configuration, STRATEGY, SINGLE_VERSION, RUBRIC_VERSION,
                         EVIDENCE_ID_SCHEMA, PUBLIC_API_ERROR_CODES)
from corpus import canonical, digest
from evidence import build_context
from evidence_lookup import EvidenceError, resolve_fact
from report_verification import verify_report

FAILURES = {
    "judge_not_configured": "configuration",
    "judge_api_error": "provider_error",
    "judge_model_unavailable": "transport",
    "judge_invalid_response": "model_response",
    "judge_model_incomplete": "model_response",
    "judge_model_refused": "model_response",
    "judge_id_schema_unavailable": "input_limit",
    "judge_input_too_large": "input_limit",
}
RESPONSE_ERRORS = {
    "invalid_judgment_shape", "incomplete_judgment_coverage", "invalid_judgment_item_shape",
    "unknown_or_duplicate_item_id", "invalid_verdict", "invalid_reason", "invalid_evidence_list",
    "unknown_evidence_id", "duplicate_evidence_id", "missing_evidence_reference",
    "missing_document_reference", "response_too_large", "invalid_response_json",
    "invalid_response_envelope", "missing_output_text", "credential_in_output",
}


def normalize_judgment(payload, raw):
    """Keep legacy Judge branches, and make an unscored failure explicit for UIs."""
    if not isinstance(raw, dict):
        raw = {"reasonCode": "judge_invalid_response"}
    key, model = judge_configuration()
    if key and key in canonical(raw):
        raw = {"reasonCode": "judge_invalid_response", "responseErrorCode": "credential_in_output"}
    reason = raw.get("reasonCode")
    scored = reason in {"judge_supported", "judge_insufficient_evidence", "judge_contradicted"}
    if scored:
        try:
            checked = decide(payload, {"items": [
                {k: item[k] for k in ("itemId", "verdict", "factIds", "chunkIds", "reason")}
                for item in raw["items"]]})
            if any(checked[k] != raw.get(k) for k in ("status", "verificationScore", "reasonCode")):
                raise InvalidJudgeResponse("invalid_response_envelope")
        except (ValueError, KeyError, TypeError):
            scored = False
            raw = {**raw, "reasonCode": "judge_invalid_response", "responseErrorCode": "invalid_response_envelope"}
    if not scored:
        reason = raw.get("reasonCode")
        if reason not in FAILURES and reason != "judge_evidence_missing":
            reason = "judge_invalid_response"
        checked = {"status": "held", "verificationScore": None, "items": [], "reasonCode": reason}

    facts = {row["factId"]: row for row in payload["facts"]}
    for item in checked["items"]:
        item["evidenceTxIds"] = sorted({tx for fact_id in item["factIds"]
                                        for tx in facts[fact_id]["evidenceTxIds"]})
    all_fact_ids = sorted({ref for item in checked["items"] for ref in item["factIds"]})
    all_chunk_ids = sorted({ref for item in checked["items"] for ref in item["chunkIds"]})
    all_tx_ids = sorted({ref for item in checked["items"] for ref in item["evidenceTxIds"]})
    reason = checked["reasonCode"]
    insufficient = reason in {"judge_insufficient_evidence", "judge_evidence_missing"}
    result = {
        "schemaVersion": "1.0", "assessmentId": "J-" + secrets.token_hex(12),
        "evaluatedAt": datetime.now(timezone.utc).isoformat(),
        "strategy": STRATEGY, "strategyVersion": SINGLE_VERSION, "rubricVersion": RUBRIC_VERSION,
        "evidenceIdSchema": EVIDENCE_ID_SCHEMA, "model": model,
        "identity": deepcopy(payload["identity"]), "inputSha256": digest(canonical(payload)),
        **checked,
        "decisionStatus": checked["status"] if scored or insufficient else "not_judged",
        "evaluationCompleted": scored,
        "outcomeCategory": "insufficient_evidence" if insufficient else "semantic_decision" if scored else FAILURES[reason],
        "factIds": all_fact_ids, "chunkIds": all_chunk_ids, "evidenceTxIds": all_tx_ids,
        "modelCallAttempts": raw.get("modelCallAttempts") if type(raw.get("modelCallAttempts")) is int and raw["modelCallAttempts"] in (0, 1) else 0,
        "modelCallAttemptsKnown": True,
        "latencyMs": raw.get("latencyMs") if type(raw.get("latencyMs")) in (int, float) and 0 <= raw["latencyMs"] <= 180_000 else None,
        "humanReviewRequired": True, "readyForSubmission": False,
    }
    if raw.get("responseErrorCode") in RESPONSE_ERRORS:
        result["responseErrorCode"] = raw["responseErrorCode"]
    if type(raw.get("providerHttpStatus")) is int and 100 <= raw["providerHttpStatus"] <= 599:
        result["providerHttpStatus"] = raw["providerHttpStatus"]
    if raw.get("providerErrorCode") in PUBLIC_API_ERROR_CODES:
        result["providerErrorCode"] = raw["providerErrorCode"]
    return result


def judge_verified_report(repository, graph, rule_result, report, judge=None):
    pinned = deepcopy({"graph": graph, "ruleResult": rule_result, "report": report})
    graph, rule_result, report = pinned["graph"], pinned["ruleResult"], pinned["report"]
    verification = verify_report(repository, graph, rule_result, report)
    if verification["valid"] is not True:
        raise EvidenceError(409, "report_not_verified", "Facts, citations and rendering must pass before Judge")
    context = build_context(repository.index_for(report["corpusSha256"]), graph, rule_result)
    identity = {
        "snapshotId": "AGENT-" + digest(canonical({"graph": graph, "ruleResult": rule_result}))[:24],
        "graphSha256": digest(canonical(graph)), "ruleResultSha256": digest(canonical(rule_result)),
        "reportSha256": digest(canonical(report)),
        **{key: context[key] for key in ("contextId", "contextVersion", "corpusSha256", "targetTxId")},
    }
    evidence = {"facts": [resolve_fact(context, row["factId"], context["contextId"]) for row in context["reportFacts"]],
                "citations": [repository.citation(row["chunkId"], context["corpusSha256"]) for row in report["citations"]]}
    if any(row["sourceVerification"]["status"] != "snapshot_checked" for row in evidence["citations"]):
        raise EvidenceError(409, "source_snapshot_unverified", "Original citation snapshots are required before Judge")
    payload = build_judge_input(identity, context, evidence, report)
    raw = (judge or openai_judge)(deepcopy(payload))
    return {"schemaVersion": "1.0", "verification": verification, "judge": normalize_judgment(payload, raw)}
