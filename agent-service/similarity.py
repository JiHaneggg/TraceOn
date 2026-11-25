"""Routing-only similarity to past laundering accounts (PPT Slide 8).

Two interchangeable sources expose `score(profiles, alert_context)`:
- FeatureSimilarity (here): cosine over a fixed profile feature vector. Needs
  no data files; used by unit tests and as a fallback.
- EmbeddingSimilarity (embedding_similarity.py): FastRP account embeddings
  built offline from the training period.
Reference accounts come from the training split only. The score routes the
investigation; it is never a verdict and never citable evidence.
"""
from __future__ import annotations

import math

import _paths  # noqa: F401
from corpus import canonical, digest

METHOD = "profile_feature_cosine_v1"
FEATURES = ("inCounterparties", "outCounterparties", "inCount", "outCount", "burstShare",
            "similarAmountShare", "justBelowShare", "passThrough", "quickForward")
COUNT_SCALE = 20       # counts scaled as log1p(n) / log1p(20), capped at 1
MIN_TRANSACTIONS = 3   # PPT edge case: too few transactions -> skip similarity


def feature_vector(profile):
    flow, timing = profile["flow"], profile["timing"]
    amounts, forwarding = profile["amounts"], profile["forwarding"]
    total = flow["inCount"] + flow["outCount"]

    def scaled(count):
        return min(1.0, math.log1p(count) / math.log1p(COUNT_SCALE))

    def share(count):
        return count / total if total else 0.0

    values = (scaled(flow["inCounterparties"]), scaled(flow["outCounterparties"]),
              scaled(flow["inCount"]), scaled(flow["outCount"]),
              share(timing["maxTxInBurstWindow"]), float(amounts["similarAmountShare"]),
              share(amounts["justBelowThresholdCount"]),
              1.0 if forwarding["passThrough"] else 0.0,
              1.0 if forwarding["quickForward"] else 0.0)
    return [round(value, 6) for value in values]


def build_reference_set(rows, source):
    """rows: [(referenceId, account profile)] taken from training-split laundering accounts."""
    body = {"schemaVersion": "1.0", "method": METHOD, "features": list(FEATURES), "source": source,
            "profiles": [{"referenceId": reference_id, "vector": feature_vector(profile)}
                         for reference_id, profile in rows]}
    return {**body, "referenceSetSha256": digest(canonical(body))}


def validate_reference_set(reference_set):
    body = {key: value for key, value in reference_set.items() if key != "referenceSetSha256"}
    if (reference_set.get("method") != METHOD or reference_set.get("features") != list(FEATURES)
            or not reference_set.get("profiles")
            or any(len(row["vector"]) != len(FEATURES) for row in reference_set["profiles"])
            or reference_set.get("referenceSetSha256") != digest(canonical(body))):
        raise ValueError("Reference set does not match the similarity method or was modified")


def cosine(left, right):
    norm = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right)) / norm if norm else 0.0


def summarize(accounts, base):
    """Alert-level routing signal = the highest account score; skipped accounts stay listed."""
    result = {**base, "accounts": accounts}
    computed = [row for row in accounts if row["status"] == "computed"]
    if not computed:
        reasons = sorted({row["reason"] for row in accounts})
        return {**result, "status": "unavailable", "reason": reasons[0] if len(reasons) == 1 else "no_account_scored"}
    top = sorted(computed, key=lambda row: (-row["score"], row["alias"]))[0]
    return {**result, "status": "computed", "score": top["score"], "alias": top["alias"],
            "nearestReferenceId": top["nearestReferenceId"]}


class FeatureSimilarity:
    def __init__(self, reference_set):
        validate_reference_set(reference_set)
        self.reference_set = reference_set

    def score(self, profiles, context=None):
        accounts = []
        for profile in profiles:
            if profile["flow"]["inCount"] + profile["flow"]["outCount"] < MIN_TRANSACTIONS:
                accounts.append({"alias": profile["alias"], "status": "skipped", "reason": "too_few_transactions"})
                continue
            vector = feature_vector(profile)
            score, reference_id = sorted(((round(cosine(vector, row["vector"]), 4), row["referenceId"])
                                          for row in self.reference_set["profiles"]),
                                         key=lambda item: (-item[0], item[1]))[0]
            accounts.append({"alias": profile["alias"], "status": "computed", "score": score,
                             "nearestReferenceId": reference_id})
        return summarize(accounts, {"method": METHOD,
                                    "referenceSetSha256": self.reference_set["referenceSetSha256"],
                                    "referenceSource": self.reference_set["source"]})
