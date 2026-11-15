"""Verify a complete controlled report against backend graph and pinned sources.

Checks do not authenticate the upstream ledger, verify provider billing or
establish whether document passages legally/semantically prove a conclusion.
"""
from corpus import canonical, digest
from evidence import build_context, validate_draft
from evidence_lookup import EvidenceError
from reports import render_report, validate_plan


def verify_report(repository, graph, rule_result, report, query=None):
    if not isinstance(report, dict) or not isinstance(report.get("corpusSha256"), str):
        raise EvidenceError(400, "invalid_report", "Report must include corpusSha256 from the current report contract")
    index = repository.index_for(report["corpusSha256"])
    context = build_context(index, graph, rule_result, query)
    errors, checks = [], {}
    same_context = all(report.get(key) == context.get(key) for key in
                       ("contextId", "contextVersion", "corpusSha256", "graphSha256"))
    checks["context"] = {"valid": same_context, "expectedContextId": context["contextId"]}
    if not same_context:
        errors.append("context_mismatch: backend graph, query, rule or pipeline version may differ")
    structured = validate_draft(context, report["draft"], require_all_facts=True)
    checks.update(structured["checks"])
    checks["coverage"] = {"valid": not structured["coverage"]["missingFactIds"], **structured["coverage"]}
    errors.extend(structured["errors"])
    snapshots = []
    for citation in report["draft"]["citations"]:
        if not isinstance(citation, dict) or not isinstance(citation.get("chunkId"), str):
            continue
        try:
            resolved = repository.citation(citation["chunkId"], index.corpus_hash)
            status = resolved["sourceVerification"]["status"]
            snapshots.append({"chunkId": citation["chunkId"], "status": status})
        except EvidenceError as error:
            snapshots.append({"chunkId": citation["chunkId"], "status": error.code})
    source_ok = bool(snapshots) and all(row["status"] == "snapshot_checked" for row in snapshots)
    checks["sourceSnapshots"] = {"valid": source_ok, "citations": snapshots}
    if not source_ok:
        errors.append("source_snapshot_not_verified")
    fitness = context.get("evidenceFitness")
    if fitness is not None:
        quoted = {row.get("chunkId"): row.get("quote") for row in report["draft"]["citations"] if isinstance(row, dict)}
        anchors_ok = True
        for finding in fitness["findings"]:
            for role in finding["referenceChecks"]:
                for ref in role["references"]:
                    if ref["matched"]:
                        quote = quoted.get(ref["chunkId"])
                        anchors_ok &= isinstance(quote, str) and ref["quote"] in quote
        fitness_ok = report.get("evidenceFitness") == fitness and anchors_ok
        checks["evidenceFitness"] = {"valid": fitness_ok, "assessmentStatus": fitness["status"],
                                     "policySha256": fitness["policySha256"], "requiredAnchorsRetained": bool(anchors_ok),
                                     "semanticEntailmentVerified": False}
        if not fitness_ok:
            errors.append("evidence_fitness_or_required_anchor_mismatch")
    try:
        generation = report["generation"]
        if not isinstance(generation, dict) or generation.get("method") not in {"deterministic_template", "openai_structured_plan"}:
            raise ValueError("Unknown generation method")
        expected_called = generation["method"] == "openai_structured_plan"
        if type(generation.get("llmCalled")) is not bool or generation["llmCalled"] != expected_called:
            raise ValueError("Inconsistent generation metadata")
        allowed_generation = {"method", "llmCalled", "model", "responseId", "store"} if expected_called else {"method", "llmCalled"}
        if set(generation) != allowed_generation:
            raise ValueError("Unknown/missing generation metadata")
        if expected_called and (generation["store"] is not False or not isinstance(generation["model"], str)
                                or not 1 <= len(generation["model"]) <= 200
                                or generation["responseId"] is not None and not isinstance(generation["responseId"], str)):
            raise ValueError("Invalid provider metadata")
        validate_plan(context, report["plan"])
        expected = render_report(context, report["plan"], generation)
        mismatched = [field for field in expected if field != "reportId" and report.get(field) != expected[field]]
        mismatched += [field for field in report if field not in expected]
        checks["renderedContent"] = {"valid": not mismatched, "mismatchedFields": mismatched}
        if mismatched:
            errors.append("rendered_content_mismatch")
    except (ValueError, KeyError, TypeError):
        checks["renderedContent"] = {"valid": False, "mismatchedFields": ["plan_or_generation"]}
        errors.append("invalid_plan_or_generation")
    payload = {key: value for key, value in report.items() if key != "reportId"}
    digest_ok = report.get("reportId") == "RPT-" + digest(canonical(payload))[:24]
    checks["reportDigest"] = {"valid": digest_ok, "authentication": False}
    if not digest_ok:
        errors.append("report_digest_mismatch")
    assessment_summary = ({"evidenceAssessmentStatus": fitness["status"], "sufficientForAutomatedDecision": False}
                          if fitness is not None else {})
    return {"schemaVersion": "1.0", "valid": not errors, "errors": errors, "checks": checks, **assessment_summary,
            "contextId": context["contextId"], "reportId": report.get("reportId"),
            "humanReviewRequired": True, "readyForSubmission": False,
            "verificationScope": "backend_graph_facts_pinned_snapshots_and_controlled_rendering",
            "semanticEntailmentVerified": False, "externalLedgerChecked": False,
            "providerCallAuthenticated": False}
