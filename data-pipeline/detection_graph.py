"""Bounded graph inputs for an offline transaction detection benchmark."""
from collections import defaultdict, deque
from itertools import chain

from account_expansion import PAGE_SIZE, eligible_accounts
from graph_contract import public_edge


GRAPH_VARIANTS = ("2hop", "3hop", "selective")
SELECTIVE_ACCOUNTS = 8
SELECTIVE_PAGES = 2
MODEL_EDGE_LIMIT = 64
PATTERN_KINDS = ("CYCLE", "FAN_IN", "FAN_OUT", "SCATTER_GATHER", "GATHER_SCATTER")


def selective_expand(graph, lookup):
    """Replay up to two older pages from accounts eligible in the 2-hop graph."""
    visible = {edge["txId"]: edge for edge in graph["edges"]}
    depth = eligible_accounts(graph)
    degree = defaultdict(set)
    for edge in graph["edges"]:
        sender, recipient = edge["fromAccountKey"], edge["toAccountKey"]
        degree[sender].add(recipient)
        degree[recipient].add(sender)
    accounts = sorted(depth, key=lambda key: (depth[key], -len(degree[key]), key))[:SELECTIVE_ACCOUNTS]
    neighbor_limit = graph["meta"]["maxNeighbors"]
    max_edges = graph["meta"]["maxEdges"]
    reads = pages = 0
    for account in accounts:
        if pages >= SELECTIVE_PAGES or len(visible) >= max_edges:
            break
        rows = lookup(account, graph["windowStart"], graph["asOf"], neighbor_limit + PAGE_SIZE)
        reads += 1
        order = [edge_order(edge) for edge in rows]
        if order != sorted(set(order), reverse=True) or len(rows) > neighbor_limit + PAGE_SIZE:
            raise ValueError("Selected account page is not a bounded, ordered source page")
        older = rows[neighbor_limit:]
        if not older:
            continue
        pages += 1
        for edge in older:
            edge = public_edge(edge)
            edge_id = edge["txId"]
            if not graph["windowStart"] <= edge["timestamp"] <= graph["asOf"]:
                raise ValueError("Expanded transaction is outside the original time window")
            if account not in (edge["fromAccountKey"], edge["toAccountKey"]):
                raise ValueError("Expanded transaction does not belong to the selected account")
            if edge_id in visible and visible[edge_id] != edge:
                raise ValueError("Expanded transaction differs from the original graph")
            if len(visible) < max_edges:
                visible[edge_id] = edge
    edges = sorted(visible.values(), key=edge_order)
    nodes = sorted({edge[k] for edge in edges for k in ("fromAccountKey", "toAccountKey")})
    return {
        **graph, "edges": edges, "nodes": [{"accountKey": node} for node in nodes],
        "meta": {**graph["meta"], "evaluationVariant": "selective",
                 "evaluationAdditionalReads": reads, "evaluationAdditionalPages": pages},
    }


def edge_order(edge):
    return edge["timestamp"], edge["txId"]


def core2_members(edges):
    """Use distinct undirected neighbors for density, with raw direction kept elsewhere."""
    adjacency = defaultdict(set)
    for edge in edges:
        sender, receiver = edge["fromAccountKey"], edge["toAccountKey"]
        if sender != receiver:
            adjacency[sender].add(receiver)
            adjacency[receiver].add(sender)
    pending = deque(node for node, neighbors in adjacency.items() if len(neighbors) < 2)
    while pending:
        node = pending.popleft()
        neighbors = adjacency.pop(node, None)
        if neighbors is not None:
            for neighbor in neighbors:
                adjacency[neighbor].remove(node)
                if len(adjacency[neighbor]) == 1:
                    pending.append(neighbor)
    return set(adjacency)


def scatter_gather(target, outward, inward):
    """Find two time-ordered branches including the target edge."""
    left, right = target["fromAccountKey"], target["toAccountKey"]
    anchors = [
        (left, right, destination, target, rows[-1])
        for destination, rows in outward[right].items()
        if target["timestamp"] < rows[-1]["timestamp"]
    ]
    anchors.extend(
        (source, left, right, rows[0], target)
        for source, rows in inward[left].items()
        if rows[0]["timestamp"] < target["timestamp"]
    )
    for source, middle, destination, first, second in anchors:
        if len({source, middle, destination}) < 3:
            continue
        siblings = set(outward[source]) & set(inward[destination])
        for sibling in sorted(siblings - {source, middle, destination}):
            start = outward[source][sibling][0]
            end = inward[destination][sibling][-1]
            if start["timestamp"] < end["timestamp"]:
                return sorted((first, second, start, end), key=edge_order)
    return []


def gather_scatter(target, outward, inward):
    """Look for two funding accounts before the target and another payout."""
    center, receiver = target["fromAccountKey"], target["toAccountKey"]
    entries, exits = inward[center], outward[center]
    for peer, rows in sorted(exits.items()):
        if peer == receiver:
            continue
        payout = rows[-1]
        cutoff = min(target["timestamp"], payout["timestamp"])
        funding = [txs[0] for key, txs in sorted(entries.items())
                   if key not in {receiver, peer} and txs[0]["timestamp"] < cutoff]
        if len(funding) >= 2:
            return sorted([*funding[:2], payout, target], key=edge_order)
    return []


def describe(graph, rule):
    edges = graph["edges"]
    target = next(edge for edge in edges if edge["txId"] == graph["targetTxId"])
    outward = defaultdict(lambda: defaultdict(list))
    inward = defaultdict(lambda: defaultdict(list))
    for edge in sorted(edges, key=edge_order):
        sender, receiver = edge["fromAccountKey"], edge["toAccountKey"]
        if sender != receiver:
            outward[sender][receiver].append(edge)
            inward[receiver][sender].append(edge)
    sender, receiver = target["fromAccountKey"], target["toAccountKey"]
    patterns = {}
    if sender != receiver:
        for kind, groups, counterparty in (
            ("FAN_IN", inward[receiver], sender), ("FAN_OUT", outward[sender], receiver),
        ):
            if len(groups) >= 2:
                witness = [target] + [rows[-1] for peer, rows in sorted(groups.items()) if peer != counterparty][:7]
                patterns[kind] = [edge["txId"] for edge in sorted(witness, key=edge_order)]
        for name, witness in (("SCATTER_GATHER", scatter_gather(target, outward, inward)),
                              ("GATHER_SCATTER", gather_scatter(target, outward, inward))):
            if witness:
                patterns[name] = [edge["txId"] for edge in witness]
    cycle = next((row for row in rule["findings"] if row["ruleId"] == "CYCLE"), None)
    if cycle:
        patterns["CYCLE"] = cycle["evidenceTxIds"]
    core = core2_members(edges)
    return {
        "patterns": patterns, "twoCoreAccountCount": len(core),
        "targetInTwoCore": sender != receiver and {sender, receiver} <= core,
        "distinctIncomingSenders": len(inward[receiver]), "distinctOutgoingRecipients": len(outward[sender]),
    }


def agent_input(graph, observations):
    """Keep the target and every witness, then add recent adjacent transactions."""
    edges = graph["edges"]
    target_id = graph["targetTxId"]
    target = next(edge for edge in edges if edge["txId"] == target_id)
    by_id = {edge["txId"]: edge for edge in edges}
    required = {target_id, *(tx_id for ids in observations["patterns"].values() for tx_id in ids)}
    if not required <= by_id.keys() or len(required) > MODEL_EDGE_LIMIT:
        raise ValueError("Pattern witnesses cannot be represented in the bounded model input")
    endpoints = {target["fromAccountKey"], target["toAccountKey"]}
    direct = [edge for edge in reversed(edges)
              if endpoints.intersection((edge["fromAccountKey"], edge["toAccountKey"]))]
    for edge in chain(direct, reversed(edges)):
        if len(required) >= MODEL_EDGE_LIMIT:
            break
        required.add(edge["txId"])
    return {
        "targetTxId": target_id, "asOf": graph["asOf"], "windowStart": graph["windowStart"],
        "scope": {"hops": graph["meta"]["hops"], "truncated": graph["meta"]["truncated"],
                  "graphTransactionCount": len(edges), "shownTransactionCount": len(required)},
        "observations": observations, "transactions": [public_edge(edge) for edge in edges if edge["txId"] in required],
    }
