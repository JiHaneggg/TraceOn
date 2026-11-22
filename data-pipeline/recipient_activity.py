"""Point-in-time recipient inlet features from the read-only transaction index."""
from datetime import datetime, timedelta
from statistics import median

DAY = timedelta(days=1)
BASELINE_DAYS = 5
MIN_ACTIVE_DAYS = 3
MAX_INBOUND_ROWS = 50_000

BOUNDS_SQL = """SELECT timestamp FROM events ORDER BY timestamp {order},txId {order} LIMIT 1"""
INBOUND_SQL = """SELECT timestamp, fromAccountKey FROM events
                 WHERE toAccountKey=? AND timestamp>=? AND timestamp<?
                   AND fromAccountKey<>toAccountKey
                 LIMIT ?"""


class RecipientActivity:
    """Compare one rolling day to five earlier, non-overlapping days for the same recipient."""

    def __init__(self, source, max_rows=MAX_INBOUND_ROWS):
        self.source = source
        self.max_rows = max_rows
        first = source.db.execute(BOUNDS_SQL.format(order="ASC")).fetchone()
        last = source.db.execute(BOUNDS_SQL.format(order="DESC")).fetchone()
        if first is None or last is None:
            raise ValueError("Source index contains no transaction data")
        self.source_start = datetime.fromisoformat(first[0])
        self.source_end = datetime.fromisoformat(last[0])

    def compare(self, tx_id, expected_target=None):
        target = self.source.target(tx_id)
        if expected_target is not None and target != expected_target:
            raise ValueError("Target differs from the indexed source")
        as_of = datetime.fromisoformat(target["timestamp"])
        start = as_of - DAY * (BASELINE_DAYS + 1)
        if start < self.source_start or as_of > self.source_end:
            return {"status": "insufficient_coverage"}
        boundaries = [start + DAY * step for step in range(BASELINE_DAYS + 2)]
        recipient = target["toAccountKey"]
        records = self.source.db.execute(INBOUND_SQL, (recipient, start.isoformat(timespec="seconds"),
                                                      target["timestamp"], self.max_rows + 1))
        senders = [set() for _ in range(BASELINE_DAYS + 1)]
        transactions = [0] * (BASELINE_DAYS + 1)
        for number, (stamp, sender) in enumerate(records, 1):
            if number > self.max_rows:
                return {"status": "row_limit"}
            when = datetime.fromisoformat(stamp)
            slot = (when - start) // DAY
            senders[slot].add(sender)
            transactions[slot] += 1
        # Minute-level timestamps cannot establish the order of peer transactions.
        # The known target is the only row included at precisely the decision time.
        if target["fromAccountKey"] != recipient:
            senders[-1].add(target["fromAccountKey"])
            transactions[-1] += 1
        previous_senders = [len(group) for group in senders[:-1]]
        active_days = sum(bool(count) for count in previous_senders)
        result = {
            "status": "comparable" if active_days >= MIN_ACTIVE_DAYS else "insufficient_activity",
            "asOf": target["timestamp"],
            "historyStart": boundaries[0].isoformat(timespec="seconds"),
            "recentStart": boundaries[-2].isoformat(timespec="seconds"),
            "activeBaselineDays": active_days,
            "baselineSenderCounts": previous_senders,
            "baselineTransactionCounts": transactions[:-1],
            "recentSenderCount": len(senders[-1]),
            "recentTransactionCount": transactions[-1],
        }
        if result["status"] == "comparable":
            for stem, history, recent in (
                ("Sender", previous_senders, result["recentSenderCount"]),
                ("Transaction", transactions[:-1], result["recentTransactionCount"]),
            ):
                typical = median(history)
                result["typical" + stem + "Count"] = typical
                result[stem.lower() + "Delta"] = recent - typical
                result[stem.lower() + "Ratio"] = recent / typical
        return result
