"""Equivalence check: the agent's HybridRiskScore must reproduce the team's frozen result.

Re-scores the team's fresh 250-case sample (target IDs from the team script's
output) through the agent wrapper and compares flagged counts with the counts
the team script recorded. Labels are read only here, for the comparison; no
threshold or setting is chosen from this run.

    python agent-service/check_risk_score.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import _paths
from rules import RuleConfig, score
from risk_score import DEFAULT_INDEX, HybridRiskScore, VARIANT

FRESH = _paths.ROOT / "artifacts" / "recipient-hybrid-fresh-next.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fresh", type=Path, default=FRESH)
    args = parser.parse_args()
    recorded = json.loads(args.fresh.read_bytes())
    scorer = HybridRiskScore()
    counts = {key: {"tp": 0, "fp": 0} for key in ("recallTarget", "fprBudget")}
    try:
        for tx_id in recorded["targetIds"]:
            graph = scorer.source.subgraph(tx_id)
            result = scorer.score({"graph": graph, "ruleResult": score(graph, RuleConfig())})
            if result["status"] != "computed":
                raise ValueError(f"{tx_id}: {result['reason']}")
            label = int(scorer.source.db.execute("SELECT isLaundering FROM events WHERE txId=?",
                                                 (tx_id,)).fetchone()[0])
            for key, flagged in (("recallTarget", result["aboveRecallTarget"]),
                                 ("fprBudget", result["aboveFprBudget"])):
                if flagged:
                    counts[key]["tp" if label else "fp"] += 1
    finally:
        scorer.close()
    report = {}
    for key, value in counts.items():
        expected = recorded["metrics"][f"{VARIANT}:{key}"]["sample"]
        report[key] = {"agent": value, "teamRecorded": {"tp": expected["tp"], "fp": expected["fp"]},
                       "match": value == {"tp": expected["tp"], "fp": expected["fp"]}}
    print(json.dumps({"cases": len(recorded["targetIds"]), "variant": VARIANT, "results": report}, indent=2))
    raise SystemExit(0 if all(row["match"] for row in report.values()) else 1)


if __name__ == "__main__":
    main()
