"""Locally prepared IBM HI-Small cases as agent alerts (data/ is not in Git).

Build the cases first (repository root):
    python data-pipeline/preprocess.py --input data/raw/HI-Small --dataset HI-Small
    python data-pipeline/prepare_mvp.py

Labels are returned in a separate list for evaluation only; each alert carries
just the public graph and the rule engine's result, exactly like a backend alert.

    python agent-service/real_cases.py            # profile every case
    python agent-service/real_cases.py --split validation
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import _paths  # noqa: F401
from alert_rules import alert_rule_result

DEFAULT_CASES = _paths.ROOT / "data" / "processed" / "HI-Small-MVP" / "cases.json"


def load_cases(path=DEFAULT_CASES, split=None):
    """Return (alerts, labels) in the same order; labels never enter an alert.

    Each alert carries the V2 trigger's rule result (alert_rules.ALERT_RULE_CONFIG).
    """
    alerts, labels = [], []
    for case in json.loads(Path(path).read_text(encoding="utf-8")):
        if split is not None and case["split"] != split:
            continue
        graph = case["graph"]
        alerts.append({"graph": graph, "ruleResult": alert_rule_result(graph)})
        labels.append({"targetTxId": graph["targetTxId"], "split": case["split"],
                       "expectedLabel": case["expectedLabel"]})
    return alerts, labels


def main():
    from alert_context import alert_context
    from basic_profile import make_basic_profile
    from investigation_state import initial_state

    parser = argparse.ArgumentParser(description="Profile locally prepared HI-Small cases (developer view).")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--split", choices=("validation", "test"))
    args = parser.parse_args()
    if not args.cases.exists():
        parser.error(f"{args.cases} not found; run data-pipeline/preprocess.py and prepare_mvp.py first")
    profile_node = make_basic_profile()
    alerts, labels = load_cases(args.cases, args.split)
    for alert, label in zip(alerts, labels):
        state = initial_state(alert)
        first = alert_context(state)
        row = {"txId": label["targetTxId"], "split": label["split"], "label": label["expectedLabel"],
               "edges": len(alert["graph"]["edges"]), "truncated": alert["graph"]["meta"].get("truncated")}
        if "status" in first:
            row["alertContext"] = first["reasonCodes"]
        else:
            second = profile_node({**state, **first})
            row.update(rules=first["alertContext"]["ruleIds"], ruleScore=first["alertContext"]["ruleScore"],
                       hints=sorted(Counter(h["kind"] for h in second["profile"]["hints"]).items()))
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
