"""Bounded orchestration over the existing evidence tools, without model facts."""
from copy import deepcopy

from corpus import canonical, digest
from evidence_lookup import EvidenceError
from reports import ReportUnavailable
from agent_judge import build_judge_input


def run_review_workflow(call_tool, graph, rule_result, query=None,
                        corpus_sha256=None, mode="template", max_retries=0, judge=None):
    """Freeze one backend query; only generation failures can be retried.

    call_tool is the trusted AgentTools.call adapter, never a model callback.
    A verification failure rejects the candidate immediately, retaining it for
    review. A retry uses exactly the same graph, rules, query and corpus.
    """
    if mode not in {"template", "openai"}:
        raise ValueError("mode must be template or openai")
    if type(max_retries) is not int or not 0 <= max_retries <= 2:
        raise ValueError("maxRetries must be an integer from 0 to 2")
    if not isinstance(graph, dict) or not isinstance(rule_result, dict):
        raise ValueError("graph and ruleResult must be backend objects")
    pinned = deepcopy({"graph": graph, "ruleResult": rule_result})
    if query is not None:
        pinned["query"] = query
    identity = {"snapshotId": "AGENT-" + digest(canonical(pinned))[:24],
                "graphSha256": digest(canonical(pinned["graph"])),
                "ruleResultSha256": digest(canonical(pinned["ruleResult"]))}
    if corpus_sha256 is not None:
        pinned["corpusSha256"] = corpus_sha256
    result = {"schemaVersion": "1.0", "status": "held", "reasonCodes": [],
              "identity": identity, "identification": None, "context": None,
              "evidence": {"facts": [], "citations": []}, "report": None,
              "verification": {"performed": False, "valid": False, "errors": []},
              "humanReviewRequired": True, "readyForSubmission": False,
              "execution": {"mode": mode, "maxRetries": max_retries,
                            "generationAttempts": 0, "retryCount": 0,
                            "attempts": [], "toolCalls": []}}
    if judge is not None:
        result["judge"] = {"status": "skipped", "reasonCode": "code_verification_not_passed",
                           "verificationScore": None, "items": [], "modelCallAttempts": 0}

    def call(step, name, arguments):
        trace = {"step": step, "tool": name, "status": "started"}
        result["execution"]["toolCalls"].append(trace)
        try:
            response = call_tool(name, deepcopy(arguments))
        except (EvidenceError, ReportUnavailable, ValueError, KeyError, TypeError):
            trace["status"] = "error"
            raise
        trace["status"] = "returned"
        return response

    def finish(status, *reasons):
        result["status"], result["reasonCodes"] = status, list(reasons)
        return result

    try:
        context = call("identify", "build_evidence_context", pinned)
        result["context"] = context
        identity.update({key: context[key] for key in
                         ("contextId", "contextVersion", "corpusSha256", "targetTxId")})
        # All subsequent tool requests pin the corpus selected by the first call.
        pinned["corpusSha256"] = context["corpusSha256"]
        result["identification"] = {
            "targetTxId": context["targetTxId"],
            "ruleScore": context["ruleResult"]["ruleScore"],
            "findings": deepcopy(context["findings"]),
            "highlightTxIds": deepcopy(context["highlightTxIds"]),
            "scoreSource": "build_evidence_context.ruleResult.ruleScore"}
        for fact in context["reportFacts"]:
            resolved = call("collect_evidence", "resolve_report_fact", {
                **pinned, "factId": fact["factId"], "contextId": context["contextId"]})
            if (resolved["contextId"] != context["contextId"]
                    or resolved["graphSha256"] != identity["graphSha256"]
                    or resolved["corpusSha256"] != context["corpusSha256"]
                    or canonical(resolved["fact"]) != canonical(fact)):
                return finish("rejected", "fact_evidence_mismatch")
            result["evidence"]["facts"].append(resolved)
        for citation in context["citations"]:
            resolved = call("collect_evidence", "get_citation", {
                "chunkId": citation["chunkId"], "corpusSha256": context["corpusSha256"]})
            result["evidence"]["citations"].append(resolved)
            if (resolved["corpusSha256"] != context["corpusSha256"]
                    or resolved["citation"]["chunkId"] != citation["chunkId"]
                    or resolved["citation"]["textSha256"] != citation["textSha256"]):
                return finish("rejected", "citation_evidence_mismatch")
        primary = context["retrieval"][0]
        if (primary["status"] in {"no_evidence", "insufficient_scope"}
                or context.get("answerDisposition", {}).get("status") == "abstained"):
            return finish("held", "requested_evidence_unavailable")
        if not context["citations"]:
            return finish("held", "document_evidence_unavailable")
        if any(row["sourceVerification"]["status"] != "snapshot_checked"
               for row in result["evidence"]["citations"]):
            return finish("held", "source_snapshot_unverified")
        for attempt_number in range(1, max_retries + 2):
            execution = result["execution"]
            execution["generationAttempts"] = attempt_number
            execution["retryCount"] = attempt_number - 1
            attempt = {"attempt": attempt_number, "generationStatus": "started"}
            execution["attempts"].append(attempt)
            try:
                report = call("draft", "generate_review_report", {**pinned, "mode": mode})
            except ReportUnavailable:
                # Provider exception text may contain transport details. Return
                # a stable code, never keys, provider payloads or a silent fallback.
                attempt.update(generationStatus="failed", errorCode="generation_unavailable")
                if attempt_number <= max_retries:
                    continue
                return finish("held", "generation_unavailable")
            attempt["generationStatus"] = "generated"
            result["report"] = report
            verify_input = {k: v for k, v in pinned.items() if k != "corpusSha256"}
            try:
                verified = call("verify", "verify_review_report", {**verify_input, "report": report})
            except EvidenceError as error:
                if error.code != "invalid_report":
                    raise
                result["verification"] = {"performed": True, "valid": False,
                                          "errors": ["invalid_report_shape"]}
                attempt["verificationValid"] = False
                return finish("rejected", "invalid_report_shape")
            except (ValueError, KeyError, TypeError):
                result["verification"] = {"performed": True, "valid": False,
                                          "errors": ["invalid_report_shape"]}
                attempt["verificationValid"] = False
                return finish("rejected", "invalid_report_shape")
            result["verification"] = {**verified, "performed": True}
            attempt["verificationValid"] = verified["valid"]
            if not verified["valid"]:
                return finish("rejected", "report_verification_failed")
            if judge is not None:
                payload = build_judge_input(identity, context, result["evidence"], report)
                result["judge"] = judge(payload)
                review = result["judge"]
                return finish(review["status"], "facts_citations_and_rendering_verified", review["reasonCode"])
            return finish("passed", "facts_citations_and_rendering_verified")
    except EvidenceError as error:
        return finish("held", "evidence_unavailable", error.code)
