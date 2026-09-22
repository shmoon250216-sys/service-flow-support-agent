from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from service_flow.store import SupportStore
from service_flow.conversations import ConversationService
from service_flow.cases import CaseError
from service_flow.outbox import OutboxDispatcher, publish


class EnterpriseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "app.db"
        self.store = SupportStore(self.path)
        self.store.seed()
        self.service = ConversationService(self.store)
        self.now = 1000.0
        self.service.cases.clock = lambda: self.now
        self.cid = self.service.create("C-001", "O-1001")["id"]
        self.outbox = OutboxDispatcher(self.store, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def open(self, text="请转人工"):
        self.service.send(self.cid, "C-001", "request-1", text)
        return self.service.cases.get(self.cid)

    def publish(self, event_id="one"):
        with self.store.lock, self.store.connection:
            publish(
                self.store.connection,
                self.cid,
                "test.event",
                {},
                event_id=event_id,
                now=self.now,
            )

    def test_ticket_has_sla_queue_and_handoff_packet(self):
        row = self.open()
        self.assertEqual("technical", row["queue"])
        self.assertEqual(2800, row["response_due"])
        packet = json.loads(row["summary"])
        self.assertEqual("O-1001", packet["order_id"])
        self.assertTrue(packet["recent_statements"])

    def test_safety_queue_shorter_deadline(self):
        row = self.open("设备冒烟")
        self.assertEqual("safety", row["queue"])
        self.assertEqual(1300, row["response_due"])

    def test_billing_queue(self):
        cid = self.service.create("C-002", "O-1002")["id"]
        self.service.send(cid, "C-002", "billing", "我要退款")
        self.assertEqual("billing", self.service.cases.get(cid)["queue"])

    def test_wait_customer_and_reply_transition(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(
            self.cid, "S-001", "wait_customer", "请提供序列号", expected_version=2
        )
        self.assertEqual("pending_customer", self.service.cases.get(self.cid)["status"])
        self.service.send(self.cid, "C-001", "customer-response", "序列号 ABC123")
        self.assertEqual("in_progress", self.service.cases.get(self.cid)["status"])

    def test_transfer_revokes_previous_assignee(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(
            self.cid,
            "S-001",
            "transfer",
            "升级专席",
            target="S-002",
            expected_version=2,
        )
        with self.assertRaises(CaseError):
            self.service.staff_action(
                self.cid, "S-001", "reply", "旧客服回复", expected_version=3
            )
        self.service.staff_action(
            self.cid, "S-002", "reply", "已接手", expected_version=3
        )

    def test_stale_version_rejected_without_message(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        before = len(self.service.get(self.cid)["messages"])
        with self.assertRaises(CaseError):
            self.service.staff_action(
                self.cid, "S-001", "reply", "旧页面", expected_version=1
            )
        self.assertEqual(before, len(self.service.get(self.cid)["messages"]))

    def test_only_supervisor_reopens_and_resets_sla(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(self.cid, "S-001", "resolve", "已解决")
        self.now = 5000
        with self.assertRaises(CaseError):
            self.service.staff_action(
                self.cid, "S-002", "reopen", "复发", expected_version=3
            )
        self.service.staff_action(
            self.cid, "S-002", "reopen", "复发", expected_version=3, supervisor=True
        )
        row = self.service.cases.get(self.cid)
        self.assertEqual("open", row["status"])
        self.assertEqual(6800, row["response_due"])

    def test_case_and_outbox_rollback_together(self):
        self.open()
        before = self.service.cases.overview()["outbox"]
        with patch(
            "service_flow.cases.publish", side_effect=RuntimeError("disk failure")
        ):
            with self.assertRaises(RuntimeError):
                self.service.claim(self.cid, "S-001")
        self.assertEqual("open", self.service.cases.get(self.cid)["status"])
        self.assertEqual("waiting_human", self.service.row(self.cid)["state"])
        self.assertEqual(before, self.service.cases.overview()["outbox"])

    def test_sla_scan_idempotent(self):
        self.open()
        self.now = 2801
        self.assertEqual(1, self.service.cases.scan_sla())
        self.assertEqual(0, self.service.cases.scan_sla())
        self.assertEqual(1, self.service.cases.get(self.cid)["response_breached"])

    def test_first_reply_stops_response_clock(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(self.cid, "S-001", "reply", "正在处理")
        self.now = 3000
        self.assertEqual(0, self.service.cases.scan_sla())

    def test_reply_after_deadline_records_breach_without_scheduler(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        self.now = 3000
        self.service.staff_action(self.cid, "S-001", "reply", "抱歉久等")
        self.assertEqual(1, self.service.cases.get(self.cid)["response_breached"])

    def test_pending_customer_does_not_pause_absolute_sla(self):
        self.open()
        self.service.claim(self.cid, "S-001")
        self.service.staff_action(self.cid, "S-001", "wait_customer", "请补充")
        self.now = 30000
        self.service.cases.scan_sla()
        self.assertEqual(1, self.service.cases.get(self.cid)["resolution_breached"])

    def test_outbox_delivery_and_recipient_dedup(self):
        self.publish()
        event = self.outbox.claim()
        self.outbox.local_sink(event)
        self.outbox.local_sink(event)
        self.assertEqual(
            1,
            self.store.connection.execute(
                "SELECT count(*) FROM notification_inbox"
            ).fetchone()[0],
        )

    def test_delivery_error_backoff_then_recovery(self):
        self.publish()

        def fail(_):
            raise ConnectionError("down")

        self.assertEqual(1, self.outbox.drain(sink=fail)["failed"])
        self.assertIsNone(self.outbox.claim())
        self.now += 3
        self.assertEqual(1, self.outbox.drain()["delivered"])

    def test_dead_letter_and_authorized_replay(self):
        self.publish()
        for _ in range(3):
            self.outbox.drain(sink=lambda e: (_ for _ in ()).throw(TimeoutError()))
            self.now += 60
        row = self.store.connection.execute(
            "SELECT * FROM outbox_events WHERE id='one'"
        ).fetchone()
        self.assertEqual("dead", row["status"])
        self.outbox.retry_dead("one", "S-002")
        self.outbox.drain()
        self.assertEqual(
            "delivered",
            self.store.connection.execute(
                "SELECT status FROM outbox_events WHERE id='one'"
            ).fetchone()[0],
        )

    def test_crash_after_delivery_before_ack_is_deduplicated(self):
        self.publish()
        event = self.outbox.claim()
        self.outbox.local_sink(event)
        self.now += 31
        self.outbox.drain()
        self.assertEqual(
            1,
            self.store.connection.execute(
                "SELECT count(*) FROM notification_inbox"
            ).fetchone()[0],
        )

    def test_stale_worker_cannot_ack_new_lease(self):
        self.publish()
        old = self.outbox.claim()
        self.now += 31
        new = self.outbox.claim()
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.store.connection:
            changed = self.store.connection.execute(
                "UPDATE outbox_events SET status='delivered' WHERE id=? AND lease_token=?",
                (old["id"], old["lease_token"]),
            ).rowcount
        self.assertEqual(0, changed)

    def test_concurrent_workers_claim_single_event(self):
        self.publish()

        def claim(_):
            store = SupportStore(self.path)
            try:
                return OutboxDispatcher(store, clock=lambda: self.now).claim()
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(claim, [1, 2]))
        self.assertEqual(1, sum(x is not None for x in result))

    def test_refund_outbox_matches_unique_ledger(self):
        reply = self.service.send(self.cid, "C-001", "refund-request", "我要退款")
        for _ in range(2):
            self.service.confirm(self.cid, "C-001", reply["confirmation_token"])
        self.assertEqual(
            1,
            self.store.connection.execute(
                "SELECT count(*) FROM outbox_events WHERE event_type='refund.completed'"
            ).fetchone()[0],
        )

    def test_safety_escalates_existing_queue(self):
        self.open()
        self.service.send(self.cid, "C-001", "safety-new", "电池鼓包")
        row = self.service.cases.get(self.cid)
        self.assertEqual("safety", row["queue"])
        self.assertEqual("urgent", row["priority"])
