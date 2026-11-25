"""Budget accounting and hold categories shared by every node."""
import unittest

import support  # noqa: F401

from investigation_state import HOLD_CATEGORIES, charge_llm, hold, llm_hold, make_budget, remaining_llm_calls


def llm_result(error_class, calls=2, reason="llm_http_503"):
    return {"status": "held", "reasonCode": reason, "errorClass": error_class, "lastErrorCode": reason,
            "callsUsed": calls, "costUsd": 0.0012,
            "usage": {"inputTokens": 900, "cachedTokens": 0, "outputTokens": 100, "reasoningTokens": 0},
            "attempts": [{"responseId": "resp_1"}, {"responseId": None}][:calls]}


class BudgetTests(unittest.TestCase):
    def test_deadline_follows_the_time_ceiling(self):
        self.assertEqual(make_budget(now=100.0)["deadlineAt"], 160.0)
        self.assertEqual(make_budget({"maxSeconds": 30}, now=100.0)["deadlineAt"], 130.0)
        with self.assertRaises(ValueError):
            make_budget({"maxSeconds": 61})

    def test_every_attempt_is_charged_including_failures(self):
        budget = charge_llm(make_budget(now=0.0), llm_result("transient"))
        budget = charge_llm(budget, llm_result("transient", calls=1))
        self.assertEqual((budget["llmCalls"], budget["inputTokens"], budget["costUsd"]), (3, 1800, 0.0024))
        self.assertEqual(remaining_llm_calls(budget), 5)


class HoldTests(unittest.TestCase):
    def test_hold_sends_to_human_review_with_a_known_category(self):
        update = hold("typology", "evidence_insufficient", "no_matching_regulation", missing=["purpose"])
        self.assertEqual((update["status"], update["route"]), ("held", "human_review"))
        self.assertEqual(update["hold"]["label"], HOLD_CATEGORIES["evidence_insufficient"])
        self.assertEqual(update["hold"]["missing"], ["purpose"])
        with self.assertRaises(ValueError):
            hold("typology", "unknown", "x")

    def test_llm_failures_are_model_failures_but_limits_are_budget(self):
        self.assertEqual(llm_hold("investigator", llm_result("transient"))["hold"]["category"], "model_failure")
        self.assertEqual(llm_hold("investigator", llm_result("output"))["hold"]["category"], "model_failure")
        self.assertEqual(llm_hold("investigator", llm_result("budget"))["hold"]["category"], "budget_exceeded")
        self.assertEqual(llm_hold("investigator", llm_result("deadline"))["hold"]["category"], "budget_exceeded")
        update = llm_hold("investigator", llm_result("transient"))
        self.assertEqual((update["hold"]["responseIds"], update["trace"][0]["llmCalls"]), (["resp_1"], 2))


if __name__ == "__main__":
    unittest.main()
