from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from service_flow import SupportAgent, SupportStore


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SupportStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.agent = SupportAgent(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_grounded_answer_has_citation(self):
        reply = self.agent.handle("q1", "C-001", "O-1001", "耳机蓝牙连不上怎么办")
        self.assertEqual("answered", reply.status)
        self.assertEqual("KB-AIR-RESET", reply.citations[0]["article_id"])

    def test_safety_routes_to_urgent_human(self):
        reply = self.agent.handle("q2", "C-001", "O-1001", "耳机发烫还有冒烟")
        self.assertEqual("handoff", reply.status)
        self.assertEqual("safety", reply.route)
        self.assertIsNotNone(reply.ticket_id)

    def test_unknown_routes_to_human(self):
        reply = self.agent.handle("q3", "C-001", "O-1001", "我想问下你们老板是谁")
        self.assertEqual("handoff", reply.status)

    def test_order_ownership_is_enforced(self):
        reply = self.agent.handle("q4", "C-999", "O-1001", "我要退款")
        self.assertEqual("denied", reply.status)

    def test_refund_requires_confirmation_and_is_idempotent(self):
        proposed = self.agent.handle("q5", "C-001", "O-1001", "七天内不想要了，退款")
        self.assertEqual("confirmation_required", proposed.status)
        self.assertEqual(0, self.store.effect_count())
        first = self.agent.confirm_refund(proposed.confirmation_token, "C-001")
        second = self.agent.confirm_refund(proposed.confirmation_token, "C-001")
        self.assertEqual("completed", first.status)
        self.assertTrue(second.diagnostics["idempotency_hit"])
        self.assertEqual(1, self.store.effect_count())

    def test_retry_recovers_transient_failure(self):
        proposed = self.agent.handle("q6", "C-001", "O-1001", "我要退货")
        reply = self.agent.confirm_refund(proposed.confirmation_token, "C-001", fail_policy=lambda attempt: attempt == 1)
        self.assertEqual("completed", reply.status)
        self.assertEqual(2, reply.diagnostics["attempts"])

    def test_failed_action_can_resume_with_same_token(self):
        one_try = SupportAgent(self.store, max_attempts=1)
        proposed = one_try.handle("q7", "C-001", "O-1001", "申请退款")
        failed = one_try.confirm_refund(proposed.confirmation_token, "C-001", fail_policy=lambda _: True)
        resumed = one_try.confirm_refund(proposed.confirmation_token, "C-001")
        self.assertEqual("failed", failed.status)
        self.assertEqual("completed", resumed.status)


if __name__ == "__main__":
    unittest.main()

