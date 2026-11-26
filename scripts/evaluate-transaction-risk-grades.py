"""Pilot risk grading, tune a review ranking, and test it on unseen targets."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path


weights = import_module("evaluate-detection-weights")
detector = weights.detector
from transaction_risk_grade import ANSWER_FIELDS, GRADE_VERSION, openai_risk_grade, validate_grade


POLICY = "traceon-risk-grade-ranking-v1"
BUDGET = weights.PRIMARY_BUDGET
AGENT_WEIGHTS = (5, 10, 25, 50)
RESULT_SUITE = "ibm_aml_transaction_risk_grades"
PILOT_GROUP_SIZE = 6


def seal(packet):
    return {**packet, "packetSha256": detector.digest(detector.canonical(packet))}


def target_ids(packet):
    return [case["input"]["targetTxId"] for case in packet["cases"]]


def ranking_input(packet):
    cases = packet["cases"]
    if packet["modelGraphScope"] != "2hop" or packet.get("riskGradeStage") == "pilot":
        raise ValueError("Ranking requires the full ranked 2-hop packet")
    tie, inputs = weights.rank_inputs(cases, [case["rankSignals"] for case in cases],
                                     [{"flagged": None}] * len(cases))
    return tie, inputs["ruleMl"]


def prepare_pilot(packet):
    tie, base = ranking_input(packet)
    cases = packet["cases"]
    order = sorted(range(len(cases)), key=lambda pos: (-base[pos], tie[pos]))
    groups = defaultdict(list)
    for place, pos in enumerate(order):
        case = cases[pos]
        if case["expectedLabel"]:
            group = "positive_in_ml_top175" if place < BUDGET else "positive_missed_by_ml"
        else:
            patterns = case["input"]["observations"]["patterns"]
            group = "normal_fan_in" if "FAN_IN" in patterns else "normal_other"
        groups[group].append(case)
    if len(groups) != 4 or any(len(group) < PILOT_GROUP_SIZE for group in groups.values()):
        raise ValueError("The pilot needs six cases from each of its four development groups")
    pilot_cases = [{**case, "pilotGroup": name} for name in sorted(groups)
                   for case in groups[name][:PILOT_GROUP_SIZE]]
    return seal({
        "schemaVersion": detector.SCHEMA_VERSION, "suite": packet["suite"], "riskGradeStage": "pilot",
        "modelGraphScope": packet["modelGraphScope"], "baselineFreezeSha256": packet["baselineFreezeSha256"],
        "developmentPacketSha256": packet["packetSha256"], "cases": pilot_cases,
    })


def prepare_evaluation(freeze, previous, development):
    detector_freeze = weights.baseline.digest(freeze)
    if (previous["packetSha256"] == development["packetSha256"]
            or any(packet["baselineFreezeSha256"] != detector_freeze
                   or packet["modelGraphScope"] != "2hop" or len(packet["cases"]) <= BUDGET
                   for packet in (previous, development))):
        raise ValueError("Use the two earlier full 2-hop packets for the same frozen model")
    development_ids = sorted(set(target_ids(previous) + target_ids(development)))
    source_sample = weights.sample_ranked_holdout(freeze, development_ids)
    return seal({
        "schemaVersion": detector.SCHEMA_VERSION, "suite": development["suite"], "riskGradeStage": "holdout",
        "modelGraphScope": "2hop", "baselineFreezeSha256": detector_freeze,
        "developmentPacketSha256": development["packetSha256"],
        "previousPacketSha256": previous["packetSha256"], **source_sample,
    })


def mock_grade(payload):
    patterns = payload["observations"]["patterns"]
    target = payload["targetTxId"]
    connected = next((patterns[kind] for kind in ("CYCLE", "SCATTER_GATHER", "GATHER_SCATTER")
                      if kind in patterns), None)
    if connected:
        grade, evidence = "high", connected
    elif "FAN_IN" in patterns or "FAN_OUT" in patterns:
        grade, evidence = "medium", patterns.get("FAN_IN", patterns.get("FAN_OUT"))
    elif payload["scope"]["truncated"]:
        grade, evidence = "insufficient", []
    else:
        grade, evidence = "low", [target]
    result = validate_grade(payload, {"riskGrade": grade, "evidenceTxIds": evidence,
                                      "reason": "모의 실행용 구조 응답입니다."})
    return {**result, "modelCallAttempts": 0, "latencyMs": None, "model": None, "detectorVersion": GRADE_VERSION}


def grade_packet(packet, mode, model, output, reuse_pilot_from=None):
    run = mock_grade if mode == "mock" else openai_risk_grade
    cases = packet["cases"]
    reused = {}
    pilot_sha = None
    if reuse_pilot_from is not None:
        if mode != "openai" or "riskGradeStage" in packet:
            raise ValueError("Reuse requires live grading of the original full development packet")
        pilot = read_live_result(prepare_pilot(packet), reuse_pilot_from)
        if pilot["modelConfig"]["model"] != model:
            raise ValueError("Use the same OpenAI model as the graded pilot")
        pilot_sha = detector.digest(detector.canonical(pilot))
        reused = {row["targetTxId"]: row for row in pilot["decisions"] if row["riskGrade"] != "unscored"}
    pending = [case for case in cases if case["input"]["targetTxId"] not in reused]
    new_rows, interrupted = detector.collect(
        packet, mode, model, output, cases=pending, prior_sha=pilot_sha, model_run=run, detector_version=GRADE_VERSION,
    )
    by_target = {**reused, **{row["targetTxId"]: row for row in new_rows}}
    rows = [by_target[case["input"]["targetTxId"]] for case in cases]
    new_attempts = sum(row["modelCallAttempts"] for row in new_rows) + interrupted
    groups = defaultdict(Counter)
    for case, row in zip(cases, rows, strict=True):
        if "pilotGroup" in case:
            groups[case["pilotGroup"]][row["riskGrade"]] += 1
    return {
        "suite": RESULT_SUITE, "caseCount": len(rows), "packetSha256": packet["packetSha256"],
        "mode": mode, "modelConfig": {"model": model, "detectorVersion": GRADE_VERSION},
        "modelCallAttempts": sum(row["modelCallAttempts"] for row in rows) + interrupted,
        "interruptedModelCallAttempts": interrupted,
        "gradeCounts": dict(Counter(row["riskGrade"] for row in rows)),
        "unscoredReasons": dict(Counter(row["reasonCode"] for row in rows if row["riskGrade"] == "unscored")),
        "pilotGrades": {group: dict(grades) for group, grades in groups.items()},
        **({"reuse": {"pilotResultSha256": pilot_sha, "reusedPilotDecisions": len(reused),
                      "newModelCallAttempts": new_attempts}} if reuse_pilot_from is not None else {}),
        "decisions": rows,
    }


def read_live_result(packet, path):
    result = json.loads(path.read_bytes())
    cases, rows = packet["cases"], result["decisions"]
    model = result["modelConfig"]
    if (result["suite"] != RESULT_SUITE or result["mode"] != "openai"
            or result["packetSha256"] != packet["packetSha256"] or len(rows) != len(cases)
            or result["caseCount"] != len(cases) or not detector.matches_case_order(rows, cases)
            or not model["model"] or model["detectorVersion"] != GRADE_VERSION):
        raise ValueError("Ranking requires the complete live grading result for this frozen packet")
    for case, row in zip(cases, rows, strict=True):
        if row["model"] != model["model"] or row["detectorVersion"] != GRADE_VERSION:
            raise ValueError("All grades must come from the pinned live model")
        if row["riskGrade"] == "unscored":
            if row["rankSignal"] is not None:
                raise ValueError("Unscored rows cannot have a grade signal")
        else:
            checked = validate_grade(case["input"], {field: row[field] for field in ANSWER_FIELDS})
            if checked["rankSignal"] != row["rankSignal"]:
                raise ValueError("Saved rank signal differs from the validated grade")
    return result


def blended_scores(base, rows, agent_weight):
    return [
        value * 100 if row["rankSignal"] is None
        else (100 - agent_weight) * value + agent_weight * Decimal(str(row["rankSignal"]))
        for value, row in zip(base, rows, strict=True)
    ]


def quality(row):
    return -row["recall"], row["falsePositiveRate"]


def tune(packet, result):
    if "riskGradeStage" in packet:
        raise ValueError("Tune only on the earlier full development packet")
    cases = packet["cases"]
    tie, base = ranking_input(packet)
    rows = result["decisions"]
    trial = [
        {"agentWeight": weight, **weights.top_k(cases, blended_scores(base, rows, weight), tie, BUDGET)}
        for weight in (0, *AGENT_WEIGHTS)
    ]
    best_hybrid = min(trial[1:], key=lambda row: (*quality(row), row["agentWeight"]))
    return {
        "rankingPolicy": POLICY, "primaryBudget": BUDGET, "tieSeed": weights.TIE_SEED,
        "baselineFreezeSha256": packet["baselineFreezeSha256"], "developmentPacketSha256": packet["packetSha256"],
        "developmentTargetIds": target_ids(packet), "modelConfig": result["modelConfig"],
        "developmentModelCallAttempts": result["modelCallAttempts"], "developmentGradeCounts": result["gradeCounts"],
        "selectedAgentWeight": best_hybrid["agentWeight"],
        "improvedOnDevelopment": quality(best_hybrid) < quality(trial[0]),
        "trials": trial,
    }


def evaluate(packet, result, lock):
    cases = packet["cases"]
    agent_weight = lock["selectedAgentWeight"]
    if (packet.get("riskGradeStage") != "holdout" or lock["rankingPolicy"] != POLICY
            or lock["primaryBudget"] != BUDGET or lock["tieSeed"] != weights.TIE_SEED
            or agent_weight not in AGENT_WEIGHTS
            or packet["developmentPacketSha256"] != lock["developmentPacketSha256"]
            or packet["baselineFreezeSha256"] != lock["baselineFreezeSha256"]
            or result["modelConfig"] != lock["modelConfig"]
            or set(target_ids(packet)).intersection(lock["developmentTargetIds"])):
        raise ValueError("Holdout, development lock, ranking budget, or live model does not match")
    tie, base = ranking_input(packet)
    rows = result["decisions"]
    return {
        "rankingPolicy": POLICY, "caseCount": len(cases), "primaryBudget": BUDGET,
        "packetSha256": packet["packetSha256"], "developmentLockSha256": detector.digest(detector.canonical(lock)),
        "modelConfig": result["modelConfig"], "gradeCounts": result["gradeCounts"],
        "noGradeSignalCount": sum(row["rankSignal"] is None for row in rows),
        "methods": {
            "ruleMl": {**weights.top_k(cases, base, tie, BUDGET), "modelCallAttempts": 0},
            "ruleMlWithAgentGrade": {
                "agentWeight": agent_weight,
                **weights.top_k(cases, blended_scores(base, rows, agent_weight), tie, BUDGET),
                "modelCallAttempts": result["modelCallAttempts"],
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    pilot = phases.add_parser("prepare-pilot")
    holdout = phases.add_parser("prepare-evaluation")
    grading = phases.add_parser("grade")
    calibration = phases.add_parser("tune")
    final = phases.add_parser("evaluate")
    for phase in (pilot, holdout, grading, calibration, final):
        phase.add_argument("--output", type=Path, required=True)
    for phase in (pilot, grading, calibration, final):
        phase.add_argument("--packet", type=Path, required=True)
    for phase in (calibration, final):
        phase.add_argument("--result", type=Path, required=True)
    holdout.add_argument("--freeze", type=Path, required=True)
    holdout.add_argument("--previous-packet", type=Path, required=True)
    holdout.add_argument("--development-packet", type=Path, required=True)
    grading.add_argument("--mode", choices=("mock", "openai"), default="mock")
    grading.add_argument("--reuse-pilot-from", type=Path, help="Keep valid live grades from the same pilot and model")
    final.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output name")
    if args.phase == "prepare-evaluation":
        result = prepare_evaluation(args.freeze, detector.load_packet(args.previous_packet),
                                    detector.load_packet(args.development_packet))
        summary = {"caseCount": len(result["cases"]), "sampleSizes": result["sampleSizes"]}
    else:
        packet = detector.load_packet(args.packet)
        if args.phase == "prepare-pilot":
            result = prepare_pilot(packet)
            summary = {"caseCount": len(result["cases"]),
                       "groups": dict(Counter(case["pilotGroup"] for case in result["cases"]))}
        elif args.phase == "grade":
            key, model = detector.judge_configuration() if args.mode == "openai" else (None, None)
            if args.mode == "openai" and not (key and model):
                parser.error("Set OPENAI_API_KEY and OPENAI_MODEL in the PowerShell session running grade")
            result = grade_packet(packet, args.mode, model, args.output, args.reuse_pilot_from)
            summary = {name: result[name] for name in (
                "caseCount", "mode", "modelCallAttempts", "gradeCounts", "unscoredReasons", "pilotGrades",
            )}
            if "reuse" in result:
                summary["reuse"] = result["reuse"]
        else:
            live = read_live_result(packet, args.result)
            if args.phase == "tune":
                result = tune(packet, live)
                summary = {name: result[name] for name in (
                    "primaryBudget", "selectedAgentWeight", "improvedOnDevelopment", "trials",
                )}
            else:
                result = evaluate(packet, live, json.loads(args.lock.read_bytes()))
                summary = {name: result[name] for name in (
                    "caseCount", "primaryBudget", "gradeCounts", "noGradeSignalCount", "methods",
                )}
    weights.baseline.save(args.output, result)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
