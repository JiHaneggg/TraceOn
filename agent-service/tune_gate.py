"""Choose the gate's risk cut on the V2 validation split (out-of-fold scores only).

Objective: close as many false-positive alerts as possible while the laundering
alerts closed by the gate stay within the gate's share of the recall-loss budget
(sampling-weighted). The PPT allows 5% for the whole system (Slide 11); the gate
gets 2% so the LLM stages keep the rest. On validation, spending the full 5% at
the gate (cut 0.5618) bought 2.4 points more false-positive reduction than the 2%
budget at the cost of three more laundering alerts. Only alerts raised by the V2 trigger are
considered, because the agent never sees the others. The signal-hint definition
is fixed in evidence_check.SIGNAL_HINTS before tuning. The holdout is not read.

    python agent-service/tune_gate.py                 # print the table
    python agent-service/tune_gate.py --write         # also write gate_config.json
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json

import _paths
from alert_context import alert_context
from alert_rules import alert_rule_result
from basic_profile import make_basic_profile
from evidence_check import CONFIG_PATH, SIGNAL_HINTS, decide
from investigation_state import initial_state
from validation_scores import V2, validation_scores

SYSTEM_RECALL_LOSS = 0.05   # PPT Slide 11, whole system
GATE_RECALL_LOSS = 0.02     # the gate's share; the LLM stages keep the rest


def alerts_with_signals():
    data = validation_scores()
    profile_node = make_basic_profile()
    rows = []
    for case, risk in zip(data["cases"], data["hybridOutOfFold"]):
        alert = {"graph": case["graph"], "ruleResult": alert_rule_result(case["graph"])}
        state = initial_state(alert)
        first = alert_context(state)
        if "status" in first or not first["alertContext"]["flagged"]:
            continue
        state = {**state, **first}
        profile = profile_node(state)["profile"]
        rows.append({"case": case, "context": first["alertContext"], "profile": profile,
                     "risk": round(risk, 4), "budget": state["budget"]})
    return rows


def evaluate_cut(rows, cut):
    config = {"version": "tuning", "riskCut": cut, "signalHints": list(SIGNAL_HINTS)}
    totals = {"positive": 0.0, "negative": 0.0, "positiveClosed": 0.0, "negativeClosed": 0.0}
    quadrants = {}
    for row in rows:
        profile = {**row["profile"], "riskScore": {"status": "computed", "score": row["risk"]}}
        # The per-alert time budget does not apply to offline tuning.
        route, reason, _ = decide(row["context"], profile, row["budget"], config, now=float("-inf"))
        label = "positive" if row["case"]["expectedLabel"] else "negative"
        weight = row["case"]["samplingWeight"]
        totals[label] += weight
        if route == "close":
            totals[label + "Closed"] += weight
        quadrants.setdefault(reason, {"positive": 0, "negative": 0})[label] += 1
    return {"riskCut": cut, "recallLoss": round(totals["positiveClosed"] / totals["positive"], 4),
            "falsePositiveReduction": round(totals["negativeClosed"] / totals["negative"], 4),
            "quadrants": quadrants}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help=f"write the chosen cut to {CONFIG_PATH.name}")
    parser.add_argument("--max-recall-loss", type=float, default=GATE_RECALL_LOSS)
    args = parser.parse_args()
    rows = alerts_with_signals()
    with_facts = {label: sum(1 for r in rows if r["case"]["expectedLabel"] == label
                             and any(h["kind"] in SIGNAL_HINTS for h in r["profile"]["hints"]))
                  for label in (0, 1)}
    counts = {label: sum(1 for r in rows if r["case"]["expectedLabel"] == label) for label in (0, 1)}
    cuts = sorted({0.0, *(r["risk"] for r in rows), 1.01})
    table = [evaluate_cut(rows, cut) for cut in cuts]
    eligible = [row for row in table if row["recallLoss"] <= args.max_recall_loss]
    chosen = max(eligible, key=lambda row: (row["falsePositiveReduction"], -row["riskCut"]))
    report = {"split": "V2 validation (out-of-fold risk)", "alerts": {"laundering": counts[1], "normal": counts[0]},
              "signalFacts": {"laundering": with_facts[1], "normal": with_facts[0]},
              "maxRecallLoss": args.max_recall_loss, "chosen": chosen,
              "nearby": [row for row in table if abs(row["riskCut"] - chosen["riskCut"]) <= 0.1
                         and row is not chosen][:12:3]}
    print(json.dumps(report, indent=2))
    if args.write:
        manifest = (V2 / "manifest.json").read_bytes()
        config = {"version": "gate-v1", "riskCut": chosen["riskCut"], "signalHints": list(SIGNAL_HINTS),
                  "tuning": {"date": date.today().isoformat(), "split": "V2 validation", "riskScores": "out-of-fold",
                             "objective": f"max false-positive reduction with gate recall loss <= {args.max_recall_loss:.0%} "
                                          f"(system budget {SYSTEM_RECALL_LOSS:.0%})",
                             "v2ManifestSha256": hashlib.sha256(manifest).hexdigest(),
                             "validation": {key: chosen[key] for key in ("recallLoss", "falsePositiveReduction")}}}
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {CONFIG_PATH}")


if __name__ == "__main__":
    main()
