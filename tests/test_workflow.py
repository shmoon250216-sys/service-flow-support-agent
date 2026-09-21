from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from service_flow import SupportAgent, SupportStore
from service_flow.conversations import ConversationService, WorkflowError


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.store = SupportStore(self.path)
        self.store.seed()
        self.service = ConversationService(self.store)
        self.cid = self.service.create("C-001", "O-1001")["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, text, rid="r1"):
        return self.service.send(self.cid, "C-001", rid, text)

    def proposal(self):
        return self.send("我要退款")["confirmation_token"]

    def test_followup_uses_topic(self):
        self.send("耳机蓝牙连不上")
        result = self.send("那具体怎么操作", "r2")
        self.assertEqual("answered", result["status"])
        self.assertEqual("KB-AIR-RESET", result["citations"][0]["article_id"])

    def test_failed_troubleshooting_handoff_keeps_transcript(self):
        self.send("耳机蓝牙连不上")
        result = self.send("我试过了，还是不行", "r2")
        self.assertEqual("handoff", result["status"])
        row = self.service.get(self.cid)
        self.assertEqual("waiting_human", row["state"])
        self.assertEqual(4, len(row["messages"]))
        self.send("请再帮我退款", "r3")
        self.assertEqual(1, self.store.ticket_count())
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT count(*) FROM pending_actions"
            ).fetchone()[0],
        )

    def test_restart_restores_handoff_and_history(self):
        self.send("我要人工")
        self.store.close()
        self.store = SupportStore(self.path)
        self.service = ConversationService(self.store)
        row = self.service.get(self.cid, "C-001")
        self.assertEqual("waiting_human", row["state"])
        self.assertEqual(2, len(row["messages"]))
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(self.cid, "S-001", "reply", "您好，请描述情况")

    def test_urgent_cannot_resume_bot(self):
        self.send("电池鼓包")
        self.service.claim(self.cid, "S-001")
        with self.assertRaises(WorkflowError):
            self.service.staff_action(self.cid, "S-001", "resume", "继续排障")

    def test_normal_can_resume_bot(self):
        self.send("请转人工")
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(self.cid, "S-001", "resume", "已确认可以自助处理")
        self.assertEqual("answered", self.send("蓝牙连不上", "r2")["status"])

    def test_model_receives_bounded_history(self):
        class Router:
            name = "mock"

            def route_with_context(self, message, context):
                self.context = context
                return "knowledge"

            def route(self, message):
                return "knowledge"

        router = Router()
        self.service.agent.router = router
        self.send("耳机蓝牙连不上")
        self.send("那具体怎么操作", "r2")
        self.assertIn("蓝牙", router.context["recent_messages"][0]["content"])

    def test_context_budget_and_extractive_digest(self):
        for i in range(30):
            self.service.append(
                self.cid, "user" if i % 2 == 0 else "assistant", str(i) + "字" * 1800
            )
            self.service.context(self.cid)
        context = self.service.context(self.cid)
        self.assertLessEqual(len(context["recent_messages"]), 8)
        self.assertLessEqual(
            sum(len(x["content"]) for x in context["recent_messages"]), 5000
        )
        self.assertLessEqual(len(context["earlier_user_excerpts"]), 1200)
        self.assertTrue(context["earlier_user_excerpts"])

    def test_model_cannot_override_explicit_safety(self):
        class BadRouter:
            name = "bad"

            def route(self, m):
                return "refund"

        self.service.agent.router = BadRouter()
        self.assertEqual("safety", self.send("设备冒烟，我要退款")["route"])

    def test_expired_and_wrong_owner_tokens(self):
        token = self.proposal()
        self.assertEqual(
            "denied", self.service.agent.confirm_refund(token, "C-002").status
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE pending_actions SET expires_at=0 WHERE token=?", (token,)
            )
        self.assertEqual(
            "denied", self.service.confirm(self.cid, "C-001", token)["status"]
        )

    def test_cross_conversation_token_denied(self):
        token = self.proposal()
        other = self.service.create("C-001", "O-1001")["id"]
        with self.assertRaises(WorkflowError):
            self.service.confirm(other, "C-001", token)

    def test_confirmation_blocked_during_handoff(self):
        token = self.proposal()
        self.send("请转人工", "r2")
        with self.assertRaises(WorkflowError):
            self.service.confirm(self.cid, "C-001", token)

    def test_concurrent_distinct_tokens_one_order_effect(self):
        token = self.proposal()
        other = self.service.agent.handle(
            "another", "C-001", "O-1001", "退款"
        ).confirmation_token

        def run(t):
            store = SupportStore(self.path)
            try:
                return SupportAgent(store).confirm_refund(t, "C-001").status
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [token, other]))
        self.assertEqual(["completed", "completed"], results)
        self.assertEqual(
            1,
            self.store.connection.execute(
                "SELECT count(*) FROM refund_ledger"
            ).fetchone()[0],
        )
        self.assertEqual("refunded", self.store.order("O-1001")["status"])

    def test_refund_rejects_unconfirmed_or_wrong_amount(self):
        token = self.proposal()
        args = {"token": token, "order_id": "O-1001", "amount": 699.0}
        with self.assertRaises(PermissionError):
            self.service.agent.tools.call("submit_refund", args)
        self.store.authorize_refund(token, "C-001")
        args["amount"] = 1.0
        with self.assertRaises(ValueError):
            self.service.agent.tools.call("submit_refund", args)

    def test_real_transient_exception_retry(self):
        token = self.proposal()
        spec = self.service.agent.tools._tools["submit_refund"]
        original = spec.handler
        calls = []

        def flaky(args):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("injected provider timeout")
            return original(args)

        spec.handler = flaky
        self.assertEqual(
            "completed", self.service.confirm(self.cid, "C-001", token)["status"]
        )
        self.assertEqual(2, len(calls))

    def test_tool_types_extra_fields_and_schema(self):
        registry = self.service.agent.tools
        for args in [
            {"token": "x" * 20, "order_id": "O-1001", "amount": "699"},
            {"token": "x" * 20, "order_id": "O-1001", "amount": -1.0},
            {"token": "x" * 20, "order_id": "O-1001", "amount": 699.0, "extra": True},
        ]:
            with self.assertRaises(ValueError):
                registry.call("submit_refund", args)
        schema = next(x for x in registry.schemas() if x["name"] == "submit_refund")
        self.assertEqual("number", schema["parameters"]["properties"]["amount"]["type"])

    def test_no_duplicate_claim(self):
        self.send("请转人工")
        self.service.claim(self.cid, "S-001")
        with self.assertRaises(WorkflowError):
            self.service.claim(self.cid, "S-002")

    def test_other_conversation_handoff_blocks_order_refund(self):
        token = self.proposal()
        other = self.service.create("C-001", "O-1001")["id"]
        self.service.send(other, "C-001", "handoff-other", "电池鼓包")
        self.assertEqual(
            "denied", self.service.confirm(self.cid, "C-001", token)["status"]
        )
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT count(*) FROM refund_ledger"
            ).fetchone()[0],
        )

    def test_malformed_output_rejected(self):
        tool = self.service.agent.tools._tools["get_order"]
        tool.handler = lambda args: {"status": "ok"}
        with self.assertRaises(ValueError):
            self.service.agent.tools.call(
                "get_order", {"customer_id": "C-001", "order_id": "O-1001"}
            )

    def test_completed_refund_button_state(self):
        token = self.proposal()
        self.service.confirm(self.cid, "C-001", token)
        messages = self.service.get(self.cid)["messages"]
        proposal = next(m for m in messages if m["metadata"].get("confirmation_token"))
        self.assertEqual("confirmed", proposal["metadata"]["confirmation_status"])
