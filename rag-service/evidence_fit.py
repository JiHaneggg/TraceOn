"""Bounded source/condition checks. Not a semantic judge or legal decision.

Policies are inert JSON and are pinned inside the corpus, including exact source
and excerpt hashes. There is no generated query, expression evaluation or model.
"""
from copy import deepcopy
import re

from corpus import canonical, digest

ROLES = {"reporting_context", "due_diligence_context", "pattern_reference"}
ROLE_LABELS = {"reporting_context": "STR 보고 절차의 일반 근거", "due_diligence_context": "고객확인 관련 일반 근거",
               "pattern_reference": "해당 패턴을 설명하는 참고 근거"}
LEGACY_POLICY_ID = "traceon-evidence-fit-v1"
CASH_OBSERVATION_POLICY_ID = "traceon-evidence-fit-v2"
MIN_CONDITIONS = {
    "CYCLE": {"temporal_cycle", "same_funds", "economic_purpose", "client_identity"},
    "FAN_IN": {"multiple_sending_accounts", "cash_format_recorded", "geographic_withdrawal_context", "economic_purpose", "client_identity"},
    "FAN_OUT": {"multiple_receiving_accounts", "wire_format_recorded", "economic_purpose", "client_identity"},
}
CONDITIONS = {
    "temporal_cycle": "시간 순서가 맞는 순환 구조",
    "multiple_sending_accounts": "여러 송신 계좌에서 집금되는 구조",
    "multiple_receiving_accounts": "여러 수취 계좌로 분산되는 구조",
    "cash_format_recorded": "관련 거래의 원천 지급 방식이 모두 Cash로 기록됨",
    "wire_format_recorded": "관련 거래의 원천 지급 방식이 모두 Wire로 기록됨",
    "same_funds": "순환 경로에서 동일 자금이 이동했는지 확인",
    "economic_purpose": "거래 목적과 경제적 합리성 확인",
    "client_identity": "서로 다른 계좌의 실제 고객·소유자 관계 확인",
    "geographic_withdrawal_context": "입금·출금 지역 차이와 후속 출금 맥락 확인",
}
STATUS_LABELS = {"needs_more_evidence": "근거 부족·추가 검토 필요", "no_rule_findings": "현재 룰 탐지 없음·검토 필요",
                 "bounded_requirements_met": "정의한 확인 항목 충족·사람 검토 필요"}


def policy_digest(policy):
    return digest(canonical({k: v for k, v in policy.items() if k != "policySha256"}))


def validate_policy(policy, index=None, *, strict_bindings=False):
    if not isinstance(policy, dict) or set(policy) != {"schemaVersion", "policyId", "reviewStatus", "references", "rules", "policySha256"}:
        raise ValueError("Invalid evidence policy shape")
    if (policy["schemaVersion"] != "1.0" or policy["policyId"] not in (LEGACY_POLICY_ID, CASH_OBSERVATION_POLICY_ID)
            or policy["reviewStatus"] != "developer_curated_pending_domain_review"
            or policy["policySha256"] != policy_digest(policy)):
        raise ValueError("Unsupported evidence policy or checksum mismatch")
    refs = policy["references"]
    if not isinstance(refs, dict) or not 1 <= len(refs) <= 24:
        raise ValueError("Evidence policy requires 1..24 references")
    fields = {"chunkId", "documentId", "section", "sourceSha256", "textSha256", "jurisdiction", "sourceKind", "quote", "quoteSha256"}
    for key, ref in refs.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", key) or not isinstance(ref, dict) or set(ref) != fields:
            raise ValueError("Invalid policy reference")
        if any(not isinstance(ref[k], str) or not 1 <= len(ref[k]) <= 500 for k in fields):
            raise ValueError("Policy reference fields must be bounded strings")
        if any(not re.fullmatch(r"[0-9a-f]{64}", ref[k]) for k in ("sourceSha256", "textSha256", "quoteSha256")) or digest(ref["quote"]) != ref["quoteSha256"]:
            raise ValueError("Policy reference hash mismatch")
        if strict_bindings and (index is None or match_reference(index, ref) is None):
            raise ValueError("Policy reference is absent or changed: " + key)
    if not isinstance(policy["rules"], dict) or set(policy["rules"]) != set(MIN_CONDITIONS):
        raise ValueError("Evidence policy must cover CYCLE, FAN_IN and FAN_OUT")
    for kind, rule in policy["rules"].items():
        if not isinstance(rule, dict) or set(rule) != {"roles", "conditions"} or not isinstance(rule["roles"], dict) or set(rule["roles"]) != ROLES:
            raise ValueError("Invalid policy rule roles")
        conditions = rule["conditions"]
        expected_conditions = MIN_CONDITIONS[kind]
        if policy["policyId"] == CASH_OBSERVATION_POLICY_ID and kind == "FAN_IN":
            expected_conditions = expected_conditions - {"cash_format_recorded"}
        if (not isinstance(conditions, list) or any(not isinstance(c, str) for c in conditions)
                or len(conditions) != len(set(conditions)) or set(conditions) != expected_conditions):
            raise ValueError("Condition set does not match the evidence policy version")
        for role, ids in rule["roles"].items():
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in refs for i in ids) or len(ids) != len(set(ids)):
                raise ValueError("Unknown/duplicate policy reference ID")
            for key in ids:
                ref = refs[key]
                if role in {"reporting_context", "due_diligence_context"} and (ref["jurisdiction"] != "KR" or ref["sourceKind"] != "law"):
                    raise ValueError("Domestic legal context cannot use foreign references")
                if role == "pattern_reference" and ref["sourceKind"] not in {"foreign_typology", "official_guidance"}:
                    raise ValueError("General legal context cannot stand in for a pattern reference")
    return policy


def match_reference(index, reference):
    row = index.by_id.get(reference["chunkId"])
    if row is None or any(row.get(key) != reference[key] for key in
                          ("documentId", "section", "sourceSha256", "textSha256", "jurisdiction", "sourceKind")):
        return None
    return row if reference["quote"] in row["text"] and digest(row["text"]) == row["textSha256"] else None


def selected_references(index, findings):
    policy = getattr(index, "evidence_fit_policy", None)
    if policy is None:
        return []
    keys = {key for f in findings for ids in policy["rules"][f["ruleId"]]["roles"].values() for key in ids}
    result = []
    for key in sorted(keys):
        ref = policy["references"][key]
        row = match_reference(index, ref)
        if row is not None:
            result.append({"referenceId": key, "reference": ref, "chunk": row})
    return result


def check_condition(condition, finding, transactions):
    status, fact_ids, detail = "not_verified", [], "현재 거래 데이터에는 확인에 필요한 정보가 없습니다."
    prefix = finding["findingId"]
    if condition == "temporal_cycle":
        status = "observed" if finding["ruleId"] == "CYCLE" else "not_observed"
        fact_ids = [prefix + ".hops", prefix + ".elapsedSeconds"]
        detail = "재검산한 룰의 시간 순서와 경로 사실입니다. 동일 원금의 이동을 뜻하지 않습니다."
    elif condition in {"multiple_sending_accounts", "multiple_receiving_accounts"}:
        status = "observed" if finding["facts"].get("counterpartyCount", 0) >= 2 else "not_observed"
        fact_ids = [prefix + ".counterpartyCount"]
        detail = "서로 다른 계좌 수를 확인했습니다. 서로 다른 실제 고객임을 확인한 것은 아닙니다."
    elif condition in {"cash_format_recorded", "wire_format_recorded"}:
        expected = "cash" if condition == "cash_format_recorded" else "wire"
        status = "observed" if transactions and all(row["paymentFormat"].casefold() == expected for row in transactions) else "not_observed"
        detail = "원본 paymentFormat만 대조했습니다. 현금 입금의 성격이나 Wire의 법적 분류를 인증하지 않습니다."
    return {"conditionId": condition, "label": CONDITIONS[condition], "status": status,
            "factIds": fact_ids, "detail": detail}


def observe_fan_in_cash(findings, by_tx, target_payment_format):
    rows = []
    for finding in findings:
        if finding["ruleId"] != "FAN_IN":
            continue
        evidence_ids = set(finding["evidenceTxIds"])
        cash_count = sum(by_tx[tx_id]["paymentFormat"].casefold() == "cash" for tx_id in evidence_ids)
        rows.append({"findingId": finding["findingId"], "transactionCount": len(evidence_ids), "cashCount": cash_count})
    return {"targetPaymentFormat": target_payment_format, "findings": rows} if rows else None


def assess(index, findings, citations, transactions, primary_search, *, target_payment_format):
    policy = getattr(index, "evidence_fit_policy", None)
    if policy is None:
        return None
    by_citation = {c["chunkId"]: c for c in citations}
    by_tx = {t["txId"]: t for t in transactions}
    assessments, required_ids = [], set()
    for finding in findings:
        configured = policy["rules"][finding["ruleId"]]
        roles, missing = [], []
        for role in sorted(ROLES):
            references = []
            for key in configured["roles"][role]:
                ref = policy["references"][key]
                row = match_reference(index, ref)
                matched = row is not None and row["chunkId"] in by_citation
                references.append({"referenceId": key, "matched": matched, "chunkId": ref["chunkId"],
                                   "quote": ref["quote"], "quoteSha256": ref["quoteSha256"],
                                   "sourceVerification": "version_pinned_index" if matched else "missing_or_changed_source"})
                if matched:
                    required_ids.add(row["chunkId"])
            available = any(r["matched"] for r in references)
            roles.append({"role": role, "label": ROLE_LABELS[role],
                          "status": "reference_available" if available else "reference_missing", "references": references})
            if not available:
                missing.append({"id": "reference:" + role, "label": ROLE_LABELS[role],
                                "reason": "해당 역할의 검토된 원문 연결이 없거나 원문 버전이 바뀌었습니다."})
        txs = [by_tx[tx_id] for tx_id in finding["evidenceTxIds"]]
        conditions = [check_condition(c, finding, txs) for c in configured["conditions"]]
        missing += [{"id": "condition:" + c["conditionId"], "label": c["label"], "reason": c["detail"]}
                    for c in conditions if c["status"] != "observed"]
        missing_reference = any(r["status"] == "reference_missing" for r in roles)
        status = "insufficient_evidence" if missing_reference else ("partial_reference" if missing else "bounded_requirements_met")
        assessments.append({"findingId": finding["findingId"], "ruleId": finding["ruleId"], "status": status,
                            "referenceChecks": roles, "conditionChecks": conditions, "missingEvidence": missing,
                            "evidenceTxIds": finding["evidenceTxIds"], "boundedRequirementsMet": not missing})
    question_supported = primary_search["status"] == "matched"
    query_check = {"searchStatus": primary_search["status"], "requestedScopeFound": question_supported,
                   "semanticAnswerEstablished": False}
    status = "needs_more_evidence" if not question_supported or any(a["missingEvidence"] for a in assessments) else (
        "bounded_requirements_met" if findings else "no_rule_findings")
    result = {"schemaVersion": "1.0", "policyId": policy["policyId"], "policySha256": policy["policySha256"],
              "policyReviewStatus": policy["reviewStatus"], "method": "curated_references_and_observable_conditions",
              "status": status, "label": STATUS_LABELS[status], "queryCheck": query_check, "findings": assessments,
              "requiredCitationIds": sorted(required_ids), "humanReviewRequired": True,
              "sufficientForAutomatedDecision": False, "semanticEntailmentVerified": False,
              "legalApplicabilityVerified": False, "bankInternalPolicyVerified": False,
              "rawSnapshotChecked": False, "snapshotCheckLocation": "report_verification.checks.sourceSnapshots",
              "notice": "원문 연결과 관찰 가능한 조건을 점검한 결과입니다. 법률 적용·범죄·보고 의무의 자동 판정이 아닙니다."}
    if policy["policyId"] == CASH_OBSERVATION_POLICY_ID:
        cash_evidence = observe_fan_in_cash(findings, by_tx, target_payment_format)
        if cash_evidence is not None:
            result["cashEvidence"] = cash_evidence
    result["assessmentSha256"] = digest(canonical(result))
    return result


def assessment_response(context):
    assessment = context.get("evidenceFitness")
    result = {"contextId": context["contextId"], "corpusSha256": context["corpusSha256"],
            "graphSha256": context["graphSha256"], "contextVersion": context["contextVersion"],
            "status": assessment["status"] if assessment else "not_configured",
            "evidenceFitness": deepcopy(assessment), "humanReviewRequired": True}
    if context.get("answerDisposition"):
        result["answerDisposition"] = deepcopy(context["answerDisposition"])
    return result


def quotes_for_report(context):
    """Quote the checked anchor itself, rather than an unrelated chunk prefix."""
    anchors = {}
    for finding in context.get("evidenceFitness", {}).get("findings", []):
        for role in finding["referenceChecks"]:
            for ref in role["references"]:
                if ref["matched"]:
                    anchors.setdefault(ref["chunkId"], []).append(ref["quote"])
    result = {}
    for row in context["citations"]:
        if row["chunkId"] not in anchors:
            result[row["chunkId"]] = row["text"][:120]
            continue
        quotes = anchors[row["chunkId"]]
        start = min(row["text"].index(q) for q in quotes)
        end = max(row["text"].index(q) + len(q) for q in quotes)
        if end - start > 800:
            raise ValueError("Policy anchors exceed report quote bounds")
        result[row["chunkId"]] = row["text"][start:end]
    return result
