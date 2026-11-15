"""Deterministic, label-free rules over the backend's common subgraph JSON.

Scores are demo rule scores (0..100), not probabilities. No database access.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite

TRUTH_FIELDS = {"isLaundering", "patternId", "patternType", "label", "expectedLabel"}


@dataclass(frozen=True)
class RuleConfig:
    min_counterparties: int = 3
    window_minutes: int = 1440
    max_cycle_hops: int = 4
    threshold: float = 65
    fan_in_min_counterparties: int | None = None
    fan_out_min_counterparties: int | None = None

    def __post_init__(self):
        if any(type(value) is not int for value in [self.min_counterparties, self.window_minutes, self.max_cycle_hops]):
            raise ValueError("Count/window/hop settings must be integers")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)):
            raise ValueError("threshold must be numeric")
        if not 2 <= self.min_counterparties <= 100:
            raise ValueError("min_counterparties must be in 2..100")
        if not 1 <= self.window_minutes <= 10080:
            raise ValueError("window_minutes must be in 1..10080")
        if not 2 <= self.max_cycle_hops <= 6:
            raise ValueError("max_cycle_hops must be in 2..6")
        if not isfinite(self.threshold) or not 0 <= self.threshold <= 100:
            raise ValueError("threshold must be in 0..100")
        for value in (self.fan_in_min_counterparties, self.fan_out_min_counterparties):
            if value is not None and (type(value) is not int or not 2 <= value <= 100):
                raise ValueError("Per-rule counterparty settings must be integers in 2..100")

    def minimum(self, rule_id):
        override = self.fan_in_min_counterparties if rule_id == "FAN_IN" else self.fan_out_min_counterparties
        return self.min_counterparties if override is None else override


def apply_config(analyzed: dict, config: RuleConfig) -> dict:
    """Reuse candidate findings only when their time/hop scope is identical.

    Cached analysis must have counterparty cutoffs no stricter than requested.
    This allows threshold sweeps without repeating temporal graph traversal.
    """
    previous = RuleConfig(**analyzed["config"])
    if (previous.window_minutes != config.window_minutes
            or previous.max_cycle_hops != config.max_cycle_hops
            or any(previous.minimum(kind) > config.minimum(kind) for kind in ["FAN_IN", "FAN_OUT"])):
        raise ValueError("Cached analysis cannot satisfy the requested configuration")
    findings = [f for f in analyzed["findings"] if f["ruleId"] not in {"FAN_IN", "FAN_OUT"}
                or f["facts"]["counterpartyCount"] >= config.minimum(f["ruleId"])]
    rule_score = max((f["score"] for f in findings), default=0)
    return {**analyzed, "ruleScore": rule_score, "flagged": rule_score >= config.threshold,
            "findings": findings, "config": asdict(config)}


def check_no_truth(value):
    if isinstance(value, dict):
        if TRUTH_FIELDS.intersection(value):
            raise ValueError("Ground-truth fields must not enter the rule engine")
        for child in value.values():
            check_no_truth(child)
    elif isinstance(value, list):
        for child in value:
            check_no_truth(child)


def parse_time(value):
    t = datetime.fromisoformat(value)
    if t.tzinfo is not None:
        raise ValueError("IBM timestamps must be timezone-free local datetimes")
    return t


def score(graph: dict, config: RuleConfig | None = None) -> dict:
    config = config or RuleConfig()
    if not isinstance(graph, dict):
        raise ValueError("graph must be an object")
    check_no_truth(graph)
    if graph.get("schemaVersion") != "1.0":
        raise ValueError("Unsupported subgraph schemaVersion")
    edges = graph["edges"]
    if not 1 <= len(edges) <= 2000:
        raise ValueError("A subgraph must contain 1..2000 edges")
    ids = [e["txId"] for e in edges]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate txId")
    target = next((e for e in edges if e["txId"] == graph["targetTxId"]), None)
    if target is None:
        raise ValueError("Target transaction is missing")
    as_of = parse_time(graph["asOf"])
    if as_of != parse_time(target["timestamp"]):
        raise ValueError("asOf must equal the target timestamp for online evaluation")
    since = as_of - timedelta(minutes=config.window_minutes)
    usable = []
    for e in edges:
        timestamp = parse_time(e["timestamp"])
        # Reject future data instead of silently allowing look-ahead.
        if timestamp > as_of:
            raise ValueError("Future transaction in subgraph")
        for field in ("amountPaid", "amountReceived"):
            amount = Decimal(str(e[field]))
            if not amount.is_finite() or amount < 0:
                raise ValueError("Invalid amount")
        if timestamp >= since and e["fromAccountKey"] != e["toAccountKey"]:
            usable.append(e)

    findings = []
    for kind, center_key, other_key, center in [
        ("FAN_IN", "toAccountKey", "fromAccountKey", target["toAccountKey"]),
        ("FAN_OUT", "fromAccountKey", "toAccountKey", target["fromAccountKey"]),
    ]:
        selected = [e for e in usable if e[center_key] == center]
        counterparties = {e[other_key] for e in selected}
        if len(counterparties) >= 2 and target in selected:
            totals = defaultdict(Decimal)
            amount_key = "amountReceived" if kind == "FAN_IN" else "amountPaid"
            currency_key = "receivingCurrency" if kind == "FAN_IN" else "paymentCurrency"
            for e in selected:
                totals[e[currency_key]] += Decimal(str(e[amount_key]))
            findings.append({
                "ruleId": kind, "score": 65, "accountKey": center,
                "evidenceTxIds": sorted(e["txId"] for e in selected),
                "facts": {"counterpartyCount": len(counterparties),
                          "transactionCount": len(selected),
                          "totalsByCurrency": {c: str(v) for c, v in sorted(totals.items())}},
            })

    # The target must close a directed cycle. Time along the preceding path
    # must be nondecreasing; topology alone is not temporal money flow.
    outgoing = defaultdict(list)
    for e in usable:
        if e["txId"] != target["txId"]:
            outgoing[e["fromAccountKey"]].append(e)
    for row in outgoing.values():
        row.sort(key=lambda e: (e["timestamp"], e["txId"]))
    work = [(target["toAccountKey"], [], {target["toAccountKey"]}, since)]
    examined = 0
    cycle = None
    while work and examined < 10000:
        node, path, visited, previous = work.pop()
        for edge in outgoing[node]:
            if examined >= 10000:
                break
            examined += 1
            when = parse_time(edge["timestamp"])
            if when < previous:
                continue
            new_path = path + [edge]
            dst = edge["toAccountKey"]
            if dst == target["fromAccountKey"] and dst != target["toAccountKey"]:
                cycle = new_path + [target]
                break
            if len(new_path) < config.max_cycle_hops - 1 and dst not in visited:
                work.append((dst, new_path, visited | {dst}, when))
        if cycle:
            break
    if cycle:
        findings.append({"ruleId": "CYCLE", "score": 90,
                         "evidenceTxIds": [e["txId"] for e in cycle],
                         "facts": {"hops": len(cycle)}})

    analyzed = {"schemaVersion": "1.0", "txId": target["txId"],
            "findings": findings,
            "config": asdict(replace(config, min_counterparties=2,
                                     fan_in_min_counterparties=None, fan_out_min_counterparties=None)),
            "limitations": {"subgraphTruncated": bool(graph.get("meta", {}).get("truncated")),
                            "sourceGraphComplete": bool(graph.get("meta", {}).get("sourceGraphComplete", False)),
                            "cycleSearchTruncated": examined >= 10000}}
    return apply_config(analyzed, config)
