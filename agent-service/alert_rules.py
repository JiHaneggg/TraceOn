"""Alert trigger: the team's V2 rule configuration, the broadest available candidate rule.

Values are the frozen V2 selection (docs/db-rule-v2.md). On the V2 validation split
it raised 94 of 100 laundering targets versus 56 for the 24-hour default, and
adding the default rule or the rule+ML score to it raised no further laundering
targets (agent-service/validation_scores.py). Alerts should be broad; the gate and
the investigation reduce false positives afterwards.
"""
from __future__ import annotations

from dataclasses import asdict

import _paths  # noqa: F401
from rules import RuleConfig, score

ALERT_RULE_CONFIG = RuleConfig(min_counterparties=3, window_minutes=10080, max_cycle_hops=4, threshold=65,
                               fan_in_min_counterparties=2, fan_out_min_counterparties=12)


def alert_rule_result(graph):
    return score(graph, ALERT_RULE_CONFIG)


def rule_config_id(rule_result):
    """'v2' when the alert was raised with the agreed trigger, otherwise 'other'."""
    return "v2" if rule_result.get("config") == asdict(ALERT_RULE_CONFIG) else "other"
