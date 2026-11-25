"""Node 1 · alert_context (code, no LLM): why did this alert fire?

Re-runs the deterministic rule engine on the same subgraph and accepts the alert
only if the backend's rule result matches exactly. Ground-truth fields are
rejected so labels never reach the agent (PPT Slide 10: 정답은 평가 전용).
"""
from __future__ import annotations

import _paths  # noqa: F401  (rule-service, rag-service on sys.path)
from alert_rules import rule_config_id
from corpus import canonical, digest
from rules import RuleConfig, check_no_truth, score

NODE = "alert_context"
TARGET_FIELDS = ("timestamp", "fromAccountKey", "toAccountKey", "amountPaid",
                 "paymentCurrency", "amountReceived", "receivingCurrency", "paymentFormat")


def alert_context(state):
    alert = state["alert"]
    try:
        check_no_truth(alert)
    except ValueError:
        return _reject("ground_truth_in_input")
    graph, rule_result = alert["graph"], alert["ruleResult"]
    try:
        checked = score(graph, RuleConfig(**rule_result["config"]))
    except (ValueError, KeyError, TypeError) as error:
        return _reject("invalid_alert_input", str(error))
    if checked != rule_result:
        return _reject("rule_result_mismatch")

    edges = {edge["txId"]: edge for edge in graph["edges"]}
    target = edges[graph["targetTxId"]]
    findings = [{"findingId": f"F{number}", "ruleId": finding["ruleId"], "score": finding["score"],
                 "accountKey": finding.get("accountKey"), "evidenceTxIds": finding["evidenceTxIds"],
                 "facts": finding["facts"]}
                for number, finding in enumerate(checked["findings"], 1)]
    evidence = _evidence(target, checked, findings)
    meta = graph.get("meta", {})
    pinned = {"graph": graph, "ruleResult": rule_result}
    if "query" in alert:
        pinned["query"] = alert["query"]
    identity = {"snapshotId": "INV-" + digest(canonical(pinned))[:24],
                "graphSha256": digest(canonical(graph)),
                "ruleResultSha256": digest(canonical(rule_result)),
                "targetTxId": target["txId"]}
    context = {
        "targetTxId": target["txId"],
        "target": {key: target[key] for key in TARGET_FIELDS},
        "ruleScore": checked["ruleScore"], "flagged": checked["flagged"],
        "threshold": checked["config"]["threshold"], "ruleConfigId": rule_config_id(checked),
        "ruleIds": sorted({finding["ruleId"] for finding in findings}),
        "findings": findings,
        "scope": {"windowStart": graph.get("windowStart"), "asOf": graph["asOf"],
                  "hops": meta.get("hops"), "subgraphTruncated": meta.get("truncated"),
                  "sourceGraphComplete": meta.get("sourceGraphComplete"),
                  "ruleLimitations": checked["limitations"]},
        "graphSize": {"accounts": len(graph.get("nodes", [])), "transactions": len(edges)},
    }
    budget = {**state["budget"], "hopsExplored": meta.get("hops") or 0}
    return {"identity": identity, "alertContext": context, "budget": budget, "evidence": evidence,
            "trace": [{"node": NODE, "outcome": "ok", "llmCalls": 0,
                       "ruleScore": checked["ruleScore"], "ruleIds": context["ruleIds"],
                       "evidenceAdded": len(evidence)}]}


def _evidence(target, checked, findings):
    """Facts later nodes may cite by factId; values come only from the graph/rules."""
    rows = [_fact("alert.target." + key, target[key], [target["txId"]]) for key in TARGET_FIELDS]
    flagged_ids = sorted({tx for finding in findings for tx in finding["evidenceTxIds"]})
    rows.append(_fact("alert.rule.score", checked["ruleScore"], flagged_ids))
    for finding in findings:
        prefix, ids = "alert." + finding["findingId"], finding["evidenceTxIds"]
        rows.append(_fact(prefix + ".ruleId", finding["ruleId"], ids))
        for key, value in sorted(finding["facts"].items()):
            if isinstance(value, dict):
                rows.extend(_fact(f"{prefix}.{key}.{sub}", item, ids) for sub, item in sorted(value.items()))
            else:
                rows.append(_fact(f"{prefix}.{key}", value, ids))
    return rows


def _fact(fact_id, value, tx_ids):
    return {"factId": fact_id, "value": value, "evidenceTxIds": list(tx_ids), "source": NODE}


def _reject(reason, detail=None):
    entry = {"node": NODE, "outcome": "rejected", "reasonCode": reason, "llmCalls": 0}
    if detail:
        entry["detail"] = detail
    return {"status": "rejected", "reasonCodes": [reason], "trace": [entry]}
