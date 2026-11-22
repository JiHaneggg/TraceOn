"""Offline logistic benchmark for the recipient activity feature ablation."""
from collections import defaultdict
from datetime import datetime, timedelta
from math import copysign, exp, log1p
from statistics import fmean, pstdev

GRAPH_FEATURES = ("inboundSenders24h", "inboundTransfers24h", "outboundTransfers24h")
FEATURE_SETS = {
    "graph": GRAPH_FEATURES,
    "graphWithAvailability": (*GRAPH_FEATURES, "historyAvailable"),
    "graphWithAvailabilityAndDelta": (*GRAPH_FEATURES, "historyAvailable", "senderDelta"),
}
ML_WEIGHT = 0.5


def features(graph, target, profile):
    """Build features using only transactions available before or at the target."""
    when = datetime.fromisoformat(graph["asOf"])
    start = when - timedelta(hours=24)
    recipient = target["toAccountKey"]
    inbound_senders = set()
    inbound = outbound = 0
    for edge in graph["edges"]:
        moment = datetime.fromisoformat(edge["timestamp"])
        sender, receiver = edge["fromAccountKey"], edge["toAccountKey"]
        if (moment < start or moment > when or sender == receiver
                or (moment == when and edge["txId"] != target["txId"])):
            continue
        if receiver == recipient:
            inbound_senders.add(sender)
            inbound += 1
        if sender == recipient:
            outbound += 1
    available = profile["status"] == "comparable"
    return {
        "inboundSenders24h": len(inbound_senders), "inboundTransfers24h": inbound, "outboundTransfers24h": outbound,
        "historyAvailable": available, "senderDelta": profile["senderDelta"] if available else None,
    }


def vector(feature_row, names):
    result = []
    for name in names:
        value = feature_row[name]
        if name == "historyAvailable":
            result.append(float(value))
        elif name == "senderDelta":
            result.append(copysign(log1p(abs(value)), value) if value is not None else 0.0)
        else:
            result.append(log1p(value))
    return result


def sigmoid(value):
    if value >= 0:
        return 1 / (1 + exp(-value))
    z = exp(value)
    return z / (1 + z)


def train(feature_rows, cases, names):
    vectors = [vector(row, names) for row in feature_rows]
    columns = list(zip(*vectors))
    means = [fmean(col) for col in columns]
    scales = [pstdev(col) or 1.0 for col in columns]
    normalized = [[(x - mean) / scale for x, mean, scale in zip(row, means, scales)] for row in vectors]
    totals = defaultdict(float)
    for case in cases:
        totals[case["expectedLabel"]] += case["samplingWeight"]
    if set(totals) != {0, 1}:
        raise ValueError("Both labels are needed to train a fold")
    weights = [0.5 * case["samplingWeight"] / totals[case["expectedLabel"]] for case in cases]
    coefficients = [0.0] * len(names)
    intercept = 0.0
    for _ in range(320):
        gradient = [0.05 * coefficient for coefficient in coefficients]
        intercept_gradient = 0.0
        for case, row, weight in zip(cases, normalized, weights):
            prediction = sigmoid(intercept + sum(a * x for a, x in zip(coefficients, row)))
            error = weight * (prediction - case["expectedLabel"])
            intercept_gradient += error
            for index, value in enumerate(row):
                gradient[index] += error * value
        intercept -= 0.5 * intercept_gradient
        coefficients = [coefficient - 0.5 * step for coefficient, step in zip(coefficients, gradient)]
    return {"features": list(names), "means": means, "scales": scales,
            "coefficients": coefficients, "intercept": intercept}


def model_score(model, row):
    values = vector(row, model["features"])
    standardized = [(value - mean) / scale for value, mean, scale in zip(values, model["means"], model["scales"])]
    return sigmoid(model["intercept"] + sum(coef * value for coef, value in zip(model["coefficients"], standardized)))


def combined_score(model, row, rule_score):
    return (1 - ML_WEIGHT) * rule_score / 100 + ML_WEIGHT * model_score(model, row)


def grouped_folds(cases, recipients, count=5):
    """Keep any shared recipient or known positive pattern family in one fold."""
    parents = list(range(len(cases)))

    def root(index):
        while index != parents[index]:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    first_by_key = {}
    for index, (case, recipient) in enumerate(zip(cases, recipients)):
        keys = [("recipient", recipient)]
        if case["patternFamily"]:
            keys.append(("family", case["patternFamily"]))
        for key in keys:
            first = first_by_key.setdefault(key, index)
            parents[root(index)] = root(first)
    groups = defaultdict(list)
    for index in range(len(cases)):
        groups[root(index)].append(index)
    order = sorted(groups.values(), key=lambda group: (-len(group), group[0]))
    folds = [[] for _ in range(count)]
    loads = [[0, 0] for _ in folds]
    for group in order:
        positives = sum(cases[index]["expectedLabel"] for index in group)
        negatives = len(group) - positives
        fold = min(range(count), key=lambda n: (loads[n][0] * negatives + loads[n][1] * positives,
                                               len(folds[n]), n))
        folds[fold].extend(group)
        loads[fold][0] += negatives
        loads[fold][1] += positives
    if any(not all(load) for load in loads):
        raise ValueError("Each grouped fold must contain both labels")
    return folds
