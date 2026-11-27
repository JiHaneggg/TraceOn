"""Deterministic graph tools on synthetic graphs (no LLM, no network)."""
import unittest

import support  # noqa: F401

from graph_tools import NoExpansionSource, candidate_accounts, distances, execute
from investigation_state import make_budget
from synthetic_graphs import SCENARIOS


def action(tool, account, **arguments):
    return {"type": "call_tool", "tool": tool, "accountKey": account, "direction": None, "hops": None,
            "windowHours": None, **arguments}


def run(name, act, source=None, budget=None, edges=None):
    graph = SCENARIOS[name]()["graph"]
    target = next(edge for edge in graph["edges"] if edge["txId"] == graph["targetTxId"])
    focus = [target["fromAccountKey"], target["toAccountKey"]]
    return execute(act, "T1", edges or graph["edges"], graph["asOf"], focus, budget or make_budget(now=0),
                   source or NoExpansionSource()), graph


def facts(result):
    return {row["factId"]: row["value"] for row in result["facts"]}


class FakeStore:
    name = "fake_store"

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def lookup(self, account, since, as_of, limit):
        self.calls.append((account, since, as_of, limit))
        return self.rows


class TraceTests(unittest.TestCase):
    def test_backward_trace_finds_the_collecting_sources(self):
        result, _ = run("fan_in_mule", action("trace_funds", "900:FANIN-MULE", direction="backward", hops=2,
                                              windowHours=168))
        values = facts(result)
        self.assertEqual((values["inv.T1.hop1Transactions"], values["inv.T1.hop1Counterparties"]), (12, 12))
        self.assertEqual(values["inv.T1.hop1Total.US Dollar"], "115755.00")

    def test_forward_trace_follows_money_chronologically_and_reports_forwarded_share(self):
        result, _ = run("fan_out_burst", action("trace_funds", "900:FANOUT-SRC", direction="forward", hops=1,
                                                windowHours=24))
        values = facts(result)
        self.assertEqual(values["inv.T1.hop1Counterparties"], 6)
        self.assertEqual(values["inv.T1.forwardedShare.US Dollar"], "0.9833")

    def test_window_excludes_older_transactions(self):
        result, _ = run("fan_in_mule", action("trace_funds", "900:FANIN-MULE", direction="backward", hops=1,
                                              windowHours=24))
        self.assertEqual(facts(result)["inv.T1.hop1Transactions"], 8)


class CycleAndProfileTests(unittest.TestCase):
    def test_cycle_is_found_only_when_hops_allow(self):
        found, _ = run("cycle_layering", action("find_cycles", "900:CYCLE-A", hops=4, windowHours=24))
        self.assertEqual((facts(found)["inv.T1.cycleCount"], facts(found)["inv.T1.shortestCycleHops"]), (1, 4))
        missing, _ = run("cycle_layering", action("find_cycles", "900:CYCLE-A", hops=3, windowHours=24))
        self.assertEqual(facts(missing)["inv.T1.cycleCount"], 0)
        self.assertTrue(missing["facts"][0]["evidenceTxIds"])  # a zero cites what was checked

    def test_account_profile_reuses_profile_metrics_and_hints(self):
        result, _ = run("normal_merchant", action("account_profile", "900:SHOP-STORE"))
        values = facts(result)
        self.assertEqual(values["inv.T1.flow.inCounterparties"], 11)
        self.assertEqual(values["inv.T1.hints"], "FAN_IN_SHAPE")
        self.assertTrue(all(row["source"] == "graph_tools" for row in result["facts"]))


class ExpansionTests(unittest.TestCase):
    def store_rows(self, graph):
        base = graph["edges"][0]
        later = {**base, "txId": "NEW-FUTURE", "timestamp": "2099-01-01T00:00:00"}
        fresh = {**base, "txId": "NEW-1", "fromAccountKey": "900:SHOP-C3", "toAccountKey": "900:NEW-ACCOUNT"}
        return [fresh, later, base]

    def test_expansion_adds_only_new_past_edges_and_records_the_hop(self):
        graph = SCENARIOS["normal_merchant"]()["graph"]
        store = FakeStore(self.store_rows(graph))
        result, _ = run("normal_merchant", action("expand_account", "900:SHOP-C3"), source=store)
        self.assertEqual([edge["txId"] for edge in result["addedEdges"]], ["NEW-1"])
        self.assertEqual(facts(result)["inv.T1.newCounterparties"], 1)
        self.assertEqual(result["hopsExplored"], 2)
        self.assertEqual(store.calls[0][2], graph["asOf"])  # lookups never go past the alert

    def test_expansion_beyond_four_hops_is_refused(self):
        graph = SCENARIOS["normal_merchant"]()["graph"]
        chain = list(graph["edges"])
        previous = "900:SHOP-STORE"
        for number in range(5):
            account = f"900:CHAIN-{number}"
            chain.append({**graph["edges"][0], "txId": f"CHAIN-{number}", "fromAccountKey": previous,
                          "toAccountKey": account})
            previous = account
        target = next(edge for edge in graph["edges"] if edge["txId"] == graph["targetTxId"])
        far = max(distances(chain, [target["fromAccountKey"], target["toAccountKey"]]).items(), key=lambda kv: kv[1])
        result = execute(action("expand_account", far[0]), "T1", chain, graph["asOf"],
                         [target["fromAccountKey"], target["toAccountKey"]], make_budget(now=0), FakeStore([]))
        self.assertEqual(result["refused"], "hop_limit")

    def test_unknown_account_is_refused(self):
        result, _ = run("normal_merchant", action("account_profile", "900:NOT-THERE"))
        self.assertEqual((result["refused"], result["facts"]), ("unknown_account", []))


class CandidateAccountTests(unittest.TestCase):
    def test_alert_accounts_come_first_with_roles(self):
        graph = SCENARIOS["fan_out_burst"]()["graph"]
        rows = candidate_accounts(graph["edges"], ["900:FANOUT-SRC", "900:FANOUT-R6"], limit=4)
        self.assertEqual([(row["accountKey"], row["role"]) for row in rows[:2]],
                         [("900:FANOUT-SRC", "sender"), ("900:FANOUT-R6", "receiver")])
        self.assertEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()
