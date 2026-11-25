"""Node 2 · basic_profile (code, no LLM): always-on profile of the alert's accounts.

Four PPT categories: flow (in/out counterparties and totals), timing (bursts,
time from deposit to withdrawal), amounts (repeats, just below a reporting
threshold, in/out ratio) and similarity. The first three become citable
evidence; pattern hints only say where to look; similarity only routes.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

import _paths  # noqa: F401
from corpus import canonical, digest
from similarity import FeatureSimilarity

NODE = "basic_profile"
RATIO = Decimal("0.0001")

# Untuned defaults. Hint cut-offs are to be calibrated on the validation split.
PROFILE_CONFIG = {
    "version": "profile-v1",
    "burstWindowMinutes": 60,
    "quickForwardMinutes": 60,
    "forwardWindowMinutes": 180,
    "passThroughRange": ["0.8", "1.2"],
    "similarAmountTolerance": "0.05",
    "reportingThresholds": {"US Dollar": "10000"},
    "belowThresholdBand": "0.10",
    "hints": {"minCounterparties": 3, "burstCount": 5, "similarAmountMin": 3,
              "similarAmountShare": "0.5", "justBelowCount": 2},
}
HINT_ORDER = ("CYCLE_PATH", "FAN_IN_SHAPE", "FAN_OUT_SHAPE", "PASS_THROUGH", "RAPID_BURST",
              "REPEATED_AMOUNTS", "JUST_BELOW_THRESHOLD")


def make_basic_profile(similarity_source=None, config=PROFILE_CONFIG, risk_source=None):
    """Bind the routing-signal sources once per graph build.

    similarity_source: an object with score(profiles, alert_context) such as
    EmbeddingSimilarity, a profile-feature reference set (dict), or None.
    risk_source: an object with score(alert, alert_context) such as the team's
    HybridRiskScore, or None. Both are routing signals, never evidence.
    """
    if isinstance(similarity_source, dict):
        similarity_source = FeatureSimilarity(similarity_source)

    def basic_profile(state):
        context = state["alertContext"]
        edges = state["alert"]["graph"]["edges"]
        profiles = [profile_account(edges, key, alias, roles, config)
                    for alias, key, roles in focus_accounts(context)]
        evidence = [row for profile in profiles for row in profile_facts(profile)]
        hint_rows = pattern_hints(profiles, context["findings"], config)
        routing = (similarity_source.score(profiles, context) if similarity_source is not None
                   else {"status": "unavailable", "reason": "similarity_source_not_loaded"})
        risk = (risk_source.score(state["alert"], context) if risk_source is not None
                else {"status": "unavailable", "reason": "risk_source_not_loaded"})
        profile = {"configVersion": config["version"], "configSha256": digest(canonical(config)),
                   "accounts": profiles, "hints": hint_rows, "similarity": routing, "riskScore": risk}
        return {"profile": profile, "evidence": evidence,
                "trace": [{"node": NODE, "outcome": "ok", "llmCalls": 0,
                           "accounts": [p["alias"] for p in profiles],
                           "hints": [hint["kind"] for hint in hint_rows],
                           "similarity": {"status": routing["status"], "score": routing.get("score")},
                           "riskScore": {"status": risk["status"], "score": risk.get("score")},
                           "evidenceAdded": len(evidence)}]}
    return basic_profile


def focus_accounts(context):
    """The alert transaction's sender and receiver, tagged with rule-center roles."""
    target = context["target"]
    sender, receiver = target["fromAccountKey"], target["toAccountKey"]
    focus = ([("account", sender, ["sender", "receiver"])] if sender == receiver
             else [("sender", sender, ["sender"]), ("receiver", receiver, ["receiver"])])
    for finding in context["findings"]:
        for _, key, roles in focus:
            if finding.get("accountKey") == key:
                roles.append("center:" + finding["findingId"])
    return focus


def profile_account(edges, account, alias, roles, config=PROFILE_CONFIG):
    incoming, outgoing, self_transfers = _rows(edges, account)
    rows = sorted(incoming + outgoing, key=lambda row: (row["time"], row["txId"]))
    all_ids = [row["txId"] for row in rows]
    burst, burst_ids = _burst(rows, timedelta(minutes=config["burstWindowMinutes"]))
    group_ids = _similar_amount_group(rows, Decimal(config["similarAmountTolerance"]))
    below_ids = _just_below(rows, config)
    fastest, fastest_ids = _fastest_forward(incoming, outgoing)
    return {
        "alias": alias, "accountKey": account, "roles": roles, "allTxIds": all_ids,
        "flow": {"inCount": len(incoming), "outCount": len(outgoing),
                 "inCounterparties": len({row["counterparty"] for row in incoming}),
                 "outCounterparties": len({row["counterparty"] for row in outgoing}),
                 "selfTransferCount": self_transfers,
                 "inTotals": _totals(incoming), "outTotals": _totals(outgoing),
                 "inOutRatio": _in_out_ratio(incoming, outgoing),
                 "inTxIds": [row["txId"] for row in incoming], "outTxIds": [row["txId"] for row in outgoing]},
        "timing": {"firstSeen": _iso(rows[0]["time"]) if rows else None,
                   "lastSeen": _iso(rows[-1]["time"]) if rows else None,
                   "activeSpanSeconds": int((rows[-1]["time"] - rows[0]["time"]).total_seconds()) if rows else 0,
                   "maxTxInBurstWindow": burst, "burstTxIds": burst_ids},
        "forwarding": {"fastestForwardSeconds": fastest, "fastestForwardTxIds": fastest_ids,
                       "quickForward": fastest is not None and fastest <= config["quickForwardMinutes"] * 60,
                       "passThrough": _pass_through(incoming, outgoing, config)},
        "amounts": {"largestSimilarAmountGroup": len(group_ids), "similarAmountTxIds": group_ids,
                    "similarAmountShare": str((Decimal(len(group_ids)) / len(rows)).quantize(RATIO)) if rows else "0",
                    "justBelowThresholdCount": len(below_ids), "justBelowTxIds": below_ids,
                    "thresholdCurrencies": sorted(config["reportingThresholds"])},
    }


def profile_facts(profile):
    """Citable facts. Zero counts cite the transactions that were checked."""
    prefix, every = "profile." + profile["alias"], profile["allTxIds"]
    flow, timing, forwarding, amounts = (profile[key] for key in ("flow", "timing", "forwarding", "amounts"))
    rows = [(".flow.inCount", flow["inCount"], flow["inTxIds"] or every),
            (".flow.outCount", flow["outCount"], flow["outTxIds"] or every),
            (".flow.inCounterparties", flow["inCounterparties"], flow["inTxIds"] or every),
            (".flow.outCounterparties", flow["outCounterparties"], flow["outTxIds"] or every)]
    rows += [(f".flow.inTotal.{currency}", total, flow["inTxIds"]) for currency, total in flow["inTotals"].items()]
    rows += [(f".flow.outTotal.{currency}", total, flow["outTxIds"]) for currency, total in flow["outTotals"].items()]
    rows += [(f".flow.inOutRatio.{currency}", ratio, every) for currency, ratio in flow["inOutRatio"].items()]
    rows += [(".timing.maxTxInBurstWindow", timing["maxTxInBurstWindow"], timing["burstTxIds"] or every),
             (".timing.activeSpanSeconds", timing["activeSpanSeconds"], every),
             (".amounts.largestSimilarAmountGroup", amounts["largestSimilarAmountGroup"],
              amounts["similarAmountTxIds"] or every),
             (".amounts.justBelowThresholdCount", amounts["justBelowThresholdCount"],
              amounts["justBelowTxIds"] or every)]
    if forwarding["fastestForwardSeconds"] is not None:
        rows.append((".forwarding.fastestForwardSeconds", forwarding["fastestForwardSeconds"],
                     forwarding["fastestForwardTxIds"]))
    if forwarding["passThrough"]:
        window = forwarding["passThrough"]
        rows += [(".forwarding.passThroughRatio", window["ratio"], window["txIds"]),
                 (".forwarding.passThroughDelaySeconds", window["delaySeconds"], window["txIds"])]
    return [{"factId": prefix + suffix, "value": value, "evidenceTxIds": list(ids), "source": NODE}
            for suffix, value, ids in rows if ids]


def pattern_hints(profiles, findings, config=PROFILE_CONFIG):
    """Where the investigator should look first. Not a verdict."""
    cut = config["hints"]
    found = []
    for finding in findings:
        if finding["ruleId"] == "CYCLE":
            found.append(("CYCLE_PATH", None, [f"alert.{finding['findingId']}.hops"]))
    for profile in profiles:
        alias, flow, timing = profile["alias"], profile["flow"], profile["timing"]
        amounts, forwarding = profile["amounts"], profile["forwarding"]
        fact = f"profile.{alias}."
        if flow["inCounterparties"] >= cut["minCounterparties"]:
            found.append(("FAN_IN_SHAPE", alias, [fact + "flow.inCounterparties"]))
        if flow["outCounterparties"] >= cut["minCounterparties"]:
            found.append(("FAN_OUT_SHAPE", alias, [fact + "flow.outCounterparties"]))
        if forwarding["passThrough"]:
            found.append(("PASS_THROUGH", alias, [fact + "forwarding.passThroughRatio",
                                                  fact + "forwarding.passThroughDelaySeconds"]))
        if timing["maxTxInBurstWindow"] >= cut["burstCount"]:
            found.append(("RAPID_BURST", alias, [fact + "timing.maxTxInBurstWindow"]))
        if (amounts["largestSimilarAmountGroup"] >= cut["similarAmountMin"]
                and Decimal(amounts["similarAmountShare"]) >= Decimal(cut["similarAmountShare"])):
            found.append(("REPEATED_AMOUNTS", alias, [fact + "amounts.largestSimilarAmountGroup"]))
        if amounts["justBelowThresholdCount"] >= cut["justBelowCount"]:
            found.append(("JUST_BELOW_THRESHOLD", alias, [fact + "amounts.justBelowThresholdCount"]))
    aliases = [None] + [profile["alias"] for profile in profiles]
    found.sort(key=lambda row: (HINT_ORDER.index(row[0]), aliases.index(row[1])))
    return [{"hintId": f"H{number}", "kind": kind, "accountAlias": alias, "factIds": fact_ids}
            for number, (kind, alias, fact_ids) in enumerate(found, 1)]


def _rows(edges, account):
    incoming, outgoing, self_transfers = [], [], 0
    for edge in edges:
        if edge["fromAccountKey"] == edge["toAccountKey"]:
            self_transfers += edge["fromAccountKey"] == account
        elif edge["toAccountKey"] == account:
            incoming.append(_row(edge, "amountReceived", "receivingCurrency", "fromAccountKey"))
        elif edge["fromAccountKey"] == account:
            outgoing.append(_row(edge, "amountPaid", "paymentCurrency", "toAccountKey"))
    order = lambda row: (row["time"], row["txId"])  # noqa: E731
    return sorted(incoming, key=order), sorted(outgoing, key=order), self_transfers


def _row(edge, amount_key, currency_key, counterparty_key):
    return {"txId": edge["txId"], "time": datetime.fromisoformat(edge["timestamp"]),
            "amount": Decimal(str(edge[amount_key])), "currency": edge[currency_key],
            "counterparty": edge[counterparty_key]}


def _iso(value):
    return value.isoformat(timespec="seconds")


def _totals(rows):
    totals = defaultdict(Decimal)
    for row in rows:
        totals[row["currency"]] += row["amount"]
    return {currency: str(total) for currency, total in sorted(totals.items())}


def _in_out_ratio(incoming, outgoing):
    """Outflow / inflow per currency over the whole subgraph window (PPT: 입금 대비 출금 비율)."""
    inflow, outflow = defaultdict(Decimal), defaultdict(Decimal)
    for row in incoming:
        inflow[row["currency"]] += row["amount"]
    for row in outgoing:
        outflow[row["currency"]] += row["amount"]
    return {currency: str((outflow[currency] / inflow[currency]).quantize(RATIO))
            for currency in sorted(set(inflow) & set(outflow)) if inflow[currency] > 0}


def _burst(rows, window):
    best, best_ids, end = 0, [], 0
    for start in range(len(rows)):
        end = max(end, start)
        while end < len(rows) and rows[end]["time"] - rows[start]["time"] <= window:
            end += 1
        if end - start > best:
            best, best_ids = end - start, [row["txId"] for row in rows[start:end]]
    return best, best_ids


def _similar_amount_group(rows, tolerance):
    best = []
    for currency in sorted({row["currency"] for row in rows}):
        same = sorted((row for row in rows if row["currency"] == currency),
                      key=lambda row: (row["amount"], row["txId"]))
        end = 0
        for start in range(len(same)):
            end = max(end, start)
            while end < len(same) and same[end]["amount"] <= same[start]["amount"] * (1 + tolerance):
                end += 1
            if end - start > len(best):
                best = same[start:end]
    return sorted(row["txId"] for row in best)


def _just_below(rows, config):
    band = Decimal(config["belowThresholdBand"])
    found = []
    for row in rows:
        limit = config["reportingThresholds"].get(row["currency"])
        if limit is not None and Decimal(limit) * (1 - band) <= row["amount"] < Decimal(limit):
            found.append(row["txId"])
    return found


def _fastest_forward(incoming, outgoing):
    """Shortest time from a deposit to the next withdrawal (PPT: 입금 후 출금까지 걸린 시간)."""
    times = [row["time"] for row in incoming]
    best = None
    for out in outgoing:
        position = bisect_right(times, out["time"]) - 1
        if position < 0:
            continue
        gap = int((out["time"] - incoming[position]["time"]).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, [incoming[position]["txId"], out["txId"]])
    return (best[0], best[1]) if best else (None, [])


def _pass_through(incoming, outgoing, config):
    """Best window where money that came in leaves again (out/in within the configured range)."""
    low, high = (Decimal(value) for value in config["passThroughRange"])
    window = timedelta(minutes=config["forwardWindowMinutes"])
    best = None
    for anchor in incoming:
        end = anchor["time"] + window
        ins = [row for row in incoming if row["currency"] == anchor["currency"] and anchor["time"] <= row["time"] <= end]
        outs = [row for row in outgoing if row["currency"] == anchor["currency"] and anchor["time"] <= row["time"] <= end]
        inflow, outflow = sum(row["amount"] for row in ins), sum((row["amount"] for row in outs), Decimal(0))
        if not outs or inflow <= 0:
            continue
        ratio = (outflow / inflow).quantize(RATIO)
        if low <= ratio <= high:
            key = (abs(1 - ratio), anchor["time"], anchor["txId"])
            if best is None or key < best[0]:
                best = (key, {"currency": anchor["currency"], "inflow": str(inflow), "outflow": str(outflow),
                              "ratio": str(ratio), "windowStart": _iso(anchor["time"]),
                              "delaySeconds": int((outs[0]["time"] - anchor["time"]).total_seconds()),
                              "txIds": sorted(row["txId"] for row in ins + outs)})
    return best[1] if best else None
