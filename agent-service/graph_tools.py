"""Deterministic graph tools for the investigator (PPT: 계산은 툴, 판단은 LLM).

The LLM only picks a tool and its arguments from allowlists; these functions do
all path following, counting and amount arithmetic on the alert's subgraph plus
bounded, past-only expansions. Every result becomes citable facts with
transaction IDs. Large results are capped and marked instead of growing.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal

import _paths  # noqa: F401
from basic_profile import PROFILE_CONFIG, pattern_hints, profile_account, profile_facts
from graph_contract import EDGE_FIELDS

NODE = "graph_tools"
TOOL_SPECS = {
    "trace_funds": {
        "description": "Follow money from one account forward (onward transfers after it arrived) or backward "
                       "(where it came from), chronologically, 1-2 hops, within a window before the alert.",
        "arguments": ("accountKey", "direction", "hops", "windowHours"), "hops": (1, 2)},
    "find_cycles": {
        "description": "Find chronological paths that leave an account and return to it within 2-4 hops.",
        "arguments": ("accountKey", "hops", "windowHours"), "hops": (2, 3, 4)},
    "account_profile": {
        "description": "Profile any account: counterparties, bursts, deposit-to-withdrawal time, pass-through, "
                       "repeated or just-below-threshold amounts, and its pattern hints.",
        "arguments": ("accountKey",)},
    "expand_account": {
        "description": "Load up to 50 more past transactions of one account from the transaction store "
                       "(one hop further out; at most 4 hops from the alert accounts).",
        "arguments": ("accountKey",)},
}
ARGUMENT_FIELDS = ("accountKey", "direction", "hops", "windowHours")
DIRECTIONS = ("forward", "backward")
WINDOW_HOURS = (24, 72, 168)
MAX_PATHS = 20
MAX_FACT_TX = 50
EXPANSION_LIMIT = 50
EXPANSION_DAYS = 7
MAX_WORKING_EDGES = 2000
MAX_CYCLE_STEPS = 5000
RATIO = Decimal("0.0001")


class NoExpansionSource:
    """Subgraph only: synthetic tests, or no transaction store attached."""
    name = "subgraph_only"

    def lookup(self, account, since, as_of, limit):
        return []


class IndexSource:
    """Read-only, past-only lookups in the offline source index (public edge fields only)."""
    name = "offline_source_index"

    def __init__(self, source):
        self.source = source  # data-pipeline evaluation_index.SourceIndex

    def lookup(self, account, since, as_of, limit):
        return self.source.lookup(account, since, as_of, limit)


def _time(edge):
    return datetime.fromisoformat(edge["timestamp"])


def _usable(edges, start, end):
    return [edge for edge in edges if edge["fromAccountKey"] != edge["toAccountKey"] and start <= _time(edge) <= end]


def distances(edges, focus):
    adjacency = defaultdict(set)
    for edge in edges:
        if edge["fromAccountKey"] != edge["toAccountKey"]:
            adjacency[edge["fromAccountKey"]].add(edge["toAccountKey"])
            adjacency[edge["toAccountKey"]].add(edge["fromAccountKey"])
    found = {account: 0 for account in focus}
    queue = deque(focus)
    while queue:
        account = queue.popleft()
        for other in sorted(adjacency[account]):
            if other not in found:
                found[other] = found[account] + 1
                queue.append(other)
    return found


def candidate_accounts(edges, focus, limit=150):
    """Accounts the LLM may name: sender, receiver, then nearest to the alert and busiest."""
    found = distances(edges, focus)
    counts = Counter(key for edge in edges for key in (edge["fromAccountKey"], edge["toAccountKey"]))
    roles = {focus[0]: "sender", focus[-1]: "receiver"} if focus else {}
    rank = {key: position for position, key in enumerate(focus)}
    ordered = sorted(found, key=lambda key: (found[key], rank.get(key, len(focus)), -counts[key], key))[:limit]
    return [{"accountKey": key, "role": roles.get(key, "counterparty"), "distance": found[key],
             "transactions": counts[key]} for key in ordered]


def tool_arguments(action):
    spec = TOOL_SPECS[action["tool"]]
    return {field: action[field] for field in spec["arguments"]}


def execute(action, call_id, edges, as_of, focus, budget, source):
    """Run one tool call. Returns facts, a short summary, added edges and the new hop count."""
    arguments = tool_arguments(action)
    account = arguments["accountKey"]
    known = {key for edge in edges for key in (edge["fromAccountKey"], edge["toAccountKey"])}
    base = {"facts": [], "addedEdges": [], "hopsExplored": budget["hopsExplored"], "refused": None}
    if account not in known:
        return {**base, "refused": "unknown_account", "summary": f"{account} is not in the working graph"}
    end = datetime.fromisoformat(as_of)
    tool = action["tool"]
    if tool == "trace_funds":
        return {**base, **_trace(call_id, edges, account, arguments, end)}
    if tool == "find_cycles":
        return {**base, **_cycles(call_id, edges, account, arguments, end)}
    if tool == "account_profile":
        return {**base, **_profile(call_id, edges, account)}
    return {**base, **_expand(call_id, edges, account, end, focus, budget, source)}


def _fact(call_id, name, value, tx_ids):
    tx_ids = list(dict.fromkeys(tx_ids))
    row = {"factId": f"inv.{call_id}.{name}", "value": value, "evidenceTxIds": tx_ids[:MAX_FACT_TX], "source": NODE}
    if len(tx_ids) > MAX_FACT_TX:
        row["evidenceTruncated"] = True
    return row


def _account_tx(edges, account):
    return [edge["txId"] for edge in edges if account in (edge["fromAccountKey"], edge["toAccountKey"])]


def _totals(edges, amount_key, currency_key):
    totals = defaultdict(Decimal)
    for edge in edges:
        totals[edge[currency_key]] += Decimal(str(edge[amount_key]))
    return {currency: total for currency, total in sorted(totals.items())}


def _trace(call_id, edges, account, arguments, end):
    direction, hops, window = arguments["direction"], arguments["hops"], arguments["windowHours"]
    usable = _usable(edges, end - timedelta(hours=window), end)
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for edge in sorted(usable, key=lambda e: (e["timestamp"], e["txId"])):
        outgoing[edge["fromAccountKey"]].append(edge)
        incoming[edge["toAccountKey"]].append(edge)
    forward = direction == "forward"
    first = outgoing[account] if forward else incoming[account]
    second, continued = {}, 0
    if hops == 2:
        for edge in first:
            nexts = ([e for e in outgoing[edge["toAccountKey"]] if _time(e) >= _time(edge)] if forward else
                     [e for e in incoming[edge["fromAccountKey"]] if _time(e) <= _time(edge)])
            continued += bool(nexts)
            second.update((e["txId"], e) for e in nexts)
    other = "toAccountKey" if forward else "fromAccountKey"
    amount, currency = ("amountPaid", "paymentCurrency") if forward else ("amountReceived", "receivingCurrency")
    first_ids = [edge["txId"] for edge in first]
    # A zero count cites the transactions that were checked instead of nothing.
    checked = first_ids or _account_tx(usable, account) or _account_tx(edges, account)
    facts = [_fact(call_id, "hop1Transactions", len(first), checked),
             _fact(call_id, "hop1Counterparties", len({edge[other] for edge in first}), checked)]
    facts += [_fact(call_id, f"hop1Total.{code}", str(total), first_ids)
              for code, total in _totals(first, amount, currency).items()]
    pieces = [f"{direction} trace from {account} ({window}h): {len(first)} transfers with "
              f"{len({edge[other] for edge in first})} accounts"]
    if hops == 2:
        second_ids = list(second)
        share = str((Decimal(continued) / len(first)).quantize(RATIO)) if first else "0"
        facts += [_fact(call_id, "hop2Transactions", len(second), second_ids or checked),
                  _fact(call_id, "hop2Counterparties", len({e[other] for e in second.values()}), second_ids or checked),
                  _fact(call_id, "continuedShare", share, first_ids + second_ids or checked)]
        pieces.append(f"{len(second)} second-hop transfers; {share} of first-hop transfers continued")
    if forward:
        arrived = incoming[account]
        inflow, outflow = _totals(arrived, "amountReceived", "receivingCurrency"), _totals(first, amount, currency)
        arrived_ids = [edge["txId"] for edge in arrived]
        facts += [_fact(call_id, f"inflowInWindow.{code}", str(total), arrived_ids) for code, total in inflow.items()]
        for code in sorted(set(inflow) & set(outflow)):
            if inflow[code] > 0:
                share = str((outflow[code] / inflow[code]).quantize(RATIO))
                facts.append(_fact(call_id, f"forwardedShare.{code}", share, arrived_ids + first_ids))
                pieces.append(f"forwarded {share} of {code} inflow")
    return {"facts": [row for row in facts if row["evidenceTxIds"]], "summary": "; ".join(pieces)}


def _cycles(call_id, edges, account, arguments, end):
    hops, window = arguments["hops"], arguments["windowHours"]
    start = end - timedelta(hours=window)
    outgoing = defaultdict(list)
    for edge in sorted(_usable(edges, start, end), key=lambda e: (e["timestamp"], e["txId"])):
        outgoing[edge["fromAccountKey"]].append(edge)
    stack, found, steps = [(account, [], start)], [], 0
    while stack and steps < MAX_CYCLE_STEPS and len(found) < MAX_PATHS:
        node, path, previous = stack.pop()
        visited = {e["fromAccountKey"] for e in path}
        for edge in outgoing[node]:
            steps += 1
            if _time(edge) < previous:
                continue
            route = path + [edge]
            if edge["toAccountKey"] == account:
                found.append(route)
            elif len(route) < hops and edge["toAccountKey"] not in visited:
                stack.append((edge["toAccountKey"], route, _time(edge)))
    truncated = steps >= MAX_CYCLE_STEPS or len(found) >= MAX_PATHS
    if not found:
        checked = [edge["txId"] for edge in outgoing[account]] or _account_tx(edges, account)
        facts = [_fact(call_id, "cycleCount", 0, checked)]
        return {"facts": facts, "summary": f"no cycle back to {account} within {hops} hops and {window}h"}
    shortest = min(found, key=lambda route: (len(route), _time(route[-1]) - _time(route[0])))
    seconds = int((_time(shortest[-1]) - _time(shortest[0])).total_seconds())
    all_ids = [edge["txId"] for route in found for edge in route]
    shortest_ids = [edge["txId"] for edge in shortest]
    facts = [_fact(call_id, "cycleCount", len(found), all_ids),
             _fact(call_id, "shortestCycleHops", len(shortest), shortest_ids),
             _fact(call_id, "shortestCycleSeconds", seconds, shortest_ids)]
    if truncated:
        facts.append(_fact(call_id, "cycleSearchTruncated", True, all_ids))
    return {"facts": facts, "summary": f"{len(found)} cycles back to {account}; shortest {len(shortest)} hops in "
                                       f"{seconds}s" + (" (search capped)" if truncated else "")}


def _profile(call_id, edges, account):
    profile = profile_account(edges, account, call_id, ["investigated"])
    facts = []
    for row in profile_facts(profile):
        facts.append({**row, "factId": row["factId"].replace("profile." + call_id, "inv." + call_id, 1),
                      "source": NODE})
    kinds = [hint["kind"] for hint in pattern_hints([profile], [], PROFILE_CONFIG)]
    facts.append(_fact(call_id, "hints", ",".join(kinds) or "none", profile["allTxIds"]))
    flow = profile["flow"]
    return {"facts": facts, "summary": f"profile of {account}: {flow['inCount']} in from {flow['inCounterparties']} "
                                       f"accounts, {flow['outCount']} out to {flow['outCounterparties']}; hints: "
                                       f"{', '.join(kinds) or 'none'}"}


def _expand(call_id, edges, account, end, focus, budget, source):
    level = distances(edges, focus).get(account, 0) + 1
    if level > budget["maxHops"]:
        return {"refused": "hop_limit", "summary": f"{account} is already {level - 1} hops out; limit {budget['maxHops']}"}
    if len(edges) >= MAX_WORKING_EDGES:
        return {"refused": "edge_limit", "summary": "working graph is at its edge limit"}
    since = (end - timedelta(days=EXPANSION_DAYS)).isoformat(timespec="seconds")
    rows = source.lookup(account, since, end.isoformat(timespec="seconds"), EXPANSION_LIMIT)
    existing = {edge["txId"] for edge in edges}
    known = {key for edge in edges for key in (edge["fromAccountKey"], edge["toAccountKey"])}
    added = []
    for row in rows:
        edge = {field: row[field] for field in EDGE_FIELDS}
        if edge["txId"] not in existing and _time(edge) <= end:
            added.append(edge)
            existing.add(edge["txId"])
    added = added[:MAX_WORKING_EDGES - len(edges)]
    new_accounts = {key for edge in added for key in (edge["fromAccountKey"], edge["toAccountKey"])} - known
    evidence = [edge["txId"] for edge in added] or _account_tx(edges, account)
    facts = [_fact(call_id, "addedTransactions", len(added), evidence),
             _fact(call_id, "newCounterparties", len(new_accounts), evidence)]
    return {"facts": facts, "addedEdges": added,
            "hopsExplored": max(budget["hopsExplored"], level) if added else budget["hopsExplored"],
            "summary": f"loaded {len(added)} more transactions of {account} ({len(new_accounts)} new accounts)"
                       + ("" if source.name != "subgraph_only" else "; no transaction store attached")}
