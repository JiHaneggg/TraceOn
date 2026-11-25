"""Team rule+ML hybrid risk score as the gate's routing signal (team code, unchanged).

Takes the place of the PPT's embedding similarity axis. The team's recipient
activity logistic model (ml-service/recipient_hybrid.py, variant
graphWithAvailabilityAndDelta) is combined 50:50 with the V2 rule score, using
the coefficients and thresholds frozen by `scripts/evaluate-recipient-hybrid.py
develop`. The recipient's five earlier days are read from the offline source
index, read-only and strictly before the alert (data-pipeline/recipient_activity.py).

Routing signal only: never citable evidence and never a verdict. Alerts whose
target was one of the model's training cases (V2 validation) are flagged so
evaluation can exclude them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import _paths
from evaluation_index import SourceIndex
from recipient_activity import RecipientActivity
from recipient_hybrid import FEATURE_SETS, ML_WEIGHT, combined_score, features, model_score
from rules import RuleConfig, score

METHOD = "rule_ml_hybrid_v1"
VARIANT = "graphWithAvailabilityAndDelta"
DEFAULT_FREEZE = _paths.ROOT / "artifacts" / "recipient-hybrid-frozen-next.json"
DEFAULT_RULE_FREEZE = _paths.ROOT / "artifacts" / "rule-v2-frozen.json"
DEFAULT_INDEX = _paths.ROOT / "data" / "evaluation" / "HI-Small-source.sqlite3"
DEFAULT_TRAINING = _paths.ROOT / "data" / "evaluation" / "HI-Small-v2" / "validation.jsonl"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class HybridRiskScore:
    def __init__(self, freeze=DEFAULT_FREEZE, rule_freeze=DEFAULT_RULE_FREEZE, index=DEFAULT_INDEX,
                 training_cases=DEFAULT_TRAINING, variant=VARIANT):
        rule_freeze, freeze = Path(rule_freeze), Path(freeze)
        rule_digest = sha256_file(rule_freeze)
        seal = rule_freeze.with_suffix(rule_freeze.suffix + ".sha256")
        if not seal.exists() or seal.read_text(encoding="ascii").strip() != rule_digest:
            raise ValueError("Rule freeze does not match its seal")
        locked = json.loads(freeze.read_bytes())
        if (locked.get("phase") != "frozen_before_hybrid_fresh_sample"
                or locked["provenance"]["v2FrozenSha256"] != rule_digest
                or locked["mlWeight"] != ML_WEIGHT):
            raise ValueError("Hybrid model was not frozen against this rule configuration")
        chosen = locked["variants"][variant]
        if chosen["model"]["features"] != list(FEATURE_SETS[variant]):
            raise ValueError("Frozen model features differ from the team feature set")
        self.model, self.thresholds = chosen["model"], chosen["thresholds"]
        self.rule_config = RuleConfig(**json.loads(rule_freeze.read_bytes())["selectedConfig"])
        self.source = SourceIndex(Path(index))
        self.activity = RecipientActivity(self.source)
        training_cases = Path(training_cases) if training_cases else None
        self.training_ids = ({json.loads(line)["graph"]["targetTxId"]
                              for line in training_cases.read_text(encoding="utf-8").splitlines() if line.strip()}
                             if training_cases and training_cases.exists() else None)
        self.base = {"method": METHOD, "variant": variant, "mlWeight": ML_WEIGHT,
                     "freezeSha256": sha256_file(freeze), "ruleFreezeSha256": rule_digest,
                     "thresholds": dict(self.thresholds)}

    def close(self):
        self.source.close()

    def score(self, alert, context=None):
        graph = alert["graph"]
        target = next(edge for edge in graph["edges"] if edge["txId"] == graph["targetTxId"])
        try:
            history = self.activity.compare(target["txId"], expected_target=target)
        except ValueError:
            return {**self.base, "status": "unavailable", "reason": "target_not_in_source_index"}
        row = features(graph, target, history)
        rule_score = score(graph, self.rule_config)["ruleScore"]
        combined = combined_score(self.model, row, rule_score)
        return {**self.base, "status": "computed", "score": round(combined, 4),
                "mlProbability": round(model_score(self.model, row), 4), "ruleScoreComponent": rule_score,
                "features": row, "historyStatus": history["status"],
                "aboveRecallTarget": combined >= self.thresholds["recallTarget"],
                "aboveFprBudget": combined >= self.thresholds["fprBudget"],
                "inModelTrainingSet": (None if self.training_ids is None
                                       else target["txId"] in self.training_ids)}
