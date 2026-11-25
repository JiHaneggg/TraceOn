"""Deterministic synthetic alerts for agent unit tests (not IBM data, not a benchmark).

Each scenario returns an alert {graph, ruleResult} whose rule result is produced
by the real rule engine, so alert_context accepts it. The graphs are marked
sourceScope=synthetic_agent_dev and carry no ground-truth fields. Two normal
look-alikes (merchant, payroll) cover the PPT edge case of normal accounts with
a laundering-like shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import _paths  # noqa: F401
from rules import RuleConfig, score

BASE = datetime(2022, 9, 8, 9, 0, 0)
SCOPE = "synthetic_agent_dev"
WINDOW = timedelta(days=7)


class _Builder:
    def __init__(self, name):
        self.name, self.edges = name, []

    def account(self, label):
        return f"900:{self.name}-{label}"

    def tx(self, minute, source, target, amount, *, currency="US Dollar", payment_format="ACH"):
        tx_id = f"SYN-{self.name}-{len(self.edges):03d}"
        self.edges.append({"txId": tx_id, "timestamp": (BASE + timedelta(minutes=minute)).isoformat(timespec="seconds"),
                           "fromAccountKey": self.account(source), "toAccountKey": self.account(target),
                           "amountPaid": amount, "paymentCurrency": currency,
                           "amountReceived": amount, "receivingCurrency": currency,
                           "paymentFormat": payment_format})
        return tx_id

    def alert(self, target_id):
        target = next(edge for edge in self.edges if edge["txId"] == target_id)
        as_of = datetime.fromisoformat(target["timestamp"])
        for edge in self.edges:
            moment = datetime.fromisoformat(edge["timestamp"])
            if moment > as_of or as_of - moment > WINDOW:
                raise ValueError("Synthetic edges must lie in the 7-day past-only window")
        edges = sorted(self.edges, key=lambda edge: (edge["timestamp"], edge["txId"]))
        accounts = sorted({edge[key] for edge in edges for key in ("fromAccountKey", "toAccountKey")})
        graph = {"schemaVersion": "1.0", "targetTxId": target_id, "asOf": target["timestamp"],
                 "windowStart": (as_of - WINDOW).isoformat(timespec="seconds"),
                 "nodes": [{"accountKey": key} for key in accounts], "edges": edges,
                 "meta": {"hops": 2, "maxNeighbors": 50, "maxEdges": 1000, "truncated": False,
                          "timeDirection": "past-only", "sourceScope": SCOPE, "sourceGraphComplete": False}}
        return {"graph": graph, "ruleResult": score(graph, RuleConfig())}


def fan_in_mule(name="FANIN",
                senders=("9480.00", "9750.00", "9620.00", "9890.00", "9555.00", "9700.00", "9810.00", "9640.00"),
                previous=("9450.00", "9610.00", "9720.00", "9530.00"), forwarded="36000.00"):
    """A mule collects just-below-10k deposits; a day earlier it forwarded a similar batch within 30 minutes."""
    b = _Builder(name)
    for number, amount in enumerate(previous, 1):
        b.tx(-1500 + 20 * (number - 1), f"P{number}", "MULE", amount, payment_format="Cash")
    b.tx(-1500 + 20 * (len(previous) - 1) + 30, "MULE", "EXIT", forwarded, payment_format="Wire")
    b.tx(-3000, "EMPLOYER", "S1", "4200.00")
    b.tx(-2500, "S5", "LANDLORD", "1300.00")
    b.tx(-2000, "S3", "SHOP", "54.20", payment_format="Credit Card")
    b.tx(-1300, "EXIT", "OFFSHORE", "35500.00", payment_format="Wire")
    b.tx(-900, "P2", "GROCER", "86.40", payment_format="Credit Card")
    minutes = (0, 20, 40, 60, 85, 110, 140, 175)
    last = None
    for number, (minute, amount) in enumerate(zip(minutes, senders), 1):
        last = b.tx(minute, f"S{number}", "MULE", amount)
    return b.alert(last)


def cycle_layering(name="CYCLE", amounts=("50000.00", "49500.00", "49000.00", "48600.00"), funded="50200.00"):
    """A -> B -> C -> D -> A within about two hours, each hop forwarding most of what arrived."""
    b = _Builder(name)
    b.tx(-4000, "EMPLOYER", "B", "3900.00")
    b.tx(-3000, "D", "UTILITY", "210.00")
    b.tx(-2000, "A", "VENDOR", "1200.00", currency="Euro", payment_format="Wire")
    b.tx(-1000, "C", "SHOP", "75.30", payment_format="Credit Card")
    b.tx(-500, "FUNDER", "OTHER", "800.00")
    b.tx(-60, "FUNDER", "A", funded, payment_format="Wire")
    b.tx(0, "A", "B", amounts[0], payment_format="Wire")
    b.tx(40, "B", "C", amounts[1], payment_format="Wire")
    b.tx(85, "C", "D", amounts[2], payment_format="Wire")
    return b.alert(b.tx(125, "D", "A", amounts[3], payment_format="Wire"))


def fan_out_burst(name="FANOUT", amounts=("9850.00", "9900.00", "9780.00", "9820.00", "9950.00", "9700.00"),
                  received="60000.00"):
    """A large deposit is split into just-below-10k transfers within 80 minutes."""
    b = _Builder(name)
    b.tx(-3000, "SRC", "RENT", "1500.00")
    b.tx(-2000, "EMPLOYER", "SRC", "3800.00")
    b.tx(-1000, "R2", "SHOP", "45.00", payment_format="Credit Card")
    b.tx(-500, "ORIGIN", "PARTNER", "12000.00", payment_format="Wire")
    b.tx(0, "ORIGIN", "SRC", received, payment_format="Wire")
    last = None
    for number, (minute, amount) in enumerate(zip((15, 25, 35, 50, 65, 80), amounts), 1):
        last = b.tx(minute, "SRC", f"R{number}", amount)
    return b.alert(last)


def normal_merchant(name="SHOP"):
    """Normal look-alike for fan-in: card sales of varied amounts spread over a day, no forwarding."""
    b = _Builder(name)
    b.tx(-6000, "OWNER", "STORE", "5000.00", payment_format="Wire")
    b.tx(-5000, "EMPLOYER", "C1", "3600.00")
    b.tx(-4320, "STORE", "SUPPLIER", "2400.00", payment_format="Wire")
    b.tx(-300, "C4", "GROCER", "33.10", payment_format="Credit Card")
    sales = ("12.50", "48.00", "230.00", "75.90", "380.00", "19.99", "142.30", "64.00", "27.45", "310.00")
    last = None
    for number, amount in enumerate(sales, 1):
        last = b.tx(130 * (number - 1), f"C{number}", "STORE", amount, payment_format="Credit Card")
    return b.alert(last)


def normal_payroll(name="PAYROLL"):
    """Normal look-alike for fan-out: a monthly payroll batch from a funded company account."""
    b = _Builder(name)
    b.tx(-6000, "CLIENT2", "CORP", "45000.00", payment_format="Wire")
    b.tx(-4320, "CLIENT", "CORP", "120000.00", payment_format="Wire")
    b.tx(-2880, "CORP", "OFFICE", "8000.00", payment_format="Wire")
    b.tx(-2000, "E1", "LANDLORD", "1100.00")
    b.tx(-1000, "E3", "SHOP", "62.80", payment_format="Credit Card")
    salaries = ("3250.00", "4100.00", "3780.00", "4620.00", "3410.00", "3990.00", "4280.00", "3560.00")
    last = None
    for number, amount in enumerate(salaries, 1):
        last = b.tx(number - 1, "CORP", f"E{number}", amount)
    return b.alert(last)


SCENARIOS = {"fan_in_mule": fan_in_mule, "cycle_layering": cycle_layering, "fan_out_burst": fan_out_burst,
             "normal_merchant": normal_merchant, "normal_payroll": normal_payroll}


def dev_reference_set():
    """Synthetic stand-in for training-split laundering accounts (variants, not the test alerts)."""
    from basic_profile import focus_accounts, profile_account
    from alert_context import alert_context
    from investigation_state import initial_state
    from similarity import build_reference_set

    variants = [
        (fan_in_mule("REFMULE", senders=("9300.00", "9420.00", "9580.00", "9660.00", "9710.00", "9790.00"),
                     previous=("9350.00", "9500.00", "9680.00"), forwarded="27000.00"), {"receiver"}),
        (cycle_layering("REFCYCLE", amounts=("30000.00", "29700.00", "29400.00", "29150.00"), funded="30150.00"),
         {"sender", "receiver"}),
        (fan_out_burst("REFOUT", amounts=("9450.00", "9600.00", "9520.00", "9380.00", "9700.00"),
                       received="48000.00"), {"sender"}),
    ]
    rows = []
    for alert, aliases in variants:
        state = initial_state(alert)
        context = alert_context(state)["alertContext"]
        for alias, key, roles in focus_accounts(context):
            if alias in aliases:
                profile = profile_account(alert["graph"]["edges"], key, alias, roles)
                rows.append((f"{alert['graph']['targetTxId']}:{alias}", profile))
    return build_reference_set(rows, "synthetic_dev_variants")
