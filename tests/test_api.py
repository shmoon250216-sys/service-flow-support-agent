from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from service_flow.api import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "api.db"))
        self.client.__enter__()
        self.customer = self.login("customer", "C-001")
        self.staff = self.login("staff", "S-001")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def login(self, role, identity):
        r = self.client.post(
            "/api/demo/login", json={"role": role, "identity": identity}
        )
        return {"Authorization": "Bearer " + r.json()["token"]}

    def create(self):
        return self.client.post(
            "/api/conversations", headers=self.customer, json={"order_id": "O-1001"}
        ).json()["id"]

    def send(self, cid, message, rid="request-1"):
        return self.client.post(
            f"/api/conversations/{cid}/messages",
            headers=self.customer,
            json={"request_id": rid, "message": message},
        )

    def test_authenticated_chat_and_refund(self):
        cid = self.create()
        reply = self.send(cid, "我要退款").json()
        token = reply["confirmation_token"]
        for _ in range(2):
            r = self.client.post(
                f"/api/conversations/{cid}/refund/confirm",
                headers=self.customer,
                json={"token": token},
            )
            self.assertEqual("completed", r.json()["status"])
        self.assertIn("ServiceFlow", self.client.get("/").text)
        self.assertEqual(5, self.client.get("/health").json()["tools"])

    def test_auth_and_customer_isolation(self):
        self.assertEqual(401, self.client.get("/api/conversations").status_code)
        cid = self.create()
        other = self.login("customer", "C-002")
        self.assertEqual(
            404, self.client.get(f"/api/conversations/{cid}", headers=other).status_code
        )
        self.assertEqual(
            403, self.client.get("/api/staff/queue", headers=self.customer).status_code
        )
        self.assertEqual(
            403,
            self.client.post(
                "/api/conversations", headers=other, json={"order_id": "O-1001"}
            ).status_code,
        )

    def test_handoff_claim_reply_resolve(self):
        cid = self.create()
        self.assertEqual("handoff", self.send(cid, "请转人工").json()["status"])
        base = f"/api/staff/conversations/{cid}"
        self.assertEqual(
            403,
            self.client.post(
                base + "/case-action",
                headers=self.staff,
                json={"action": "reply", "message": "您好", "expected_version": 1},
            ).status_code,
        )
        self.assertEqual(
            200, self.client.post(base + "/claim", headers=self.staff).status_code
        )
        other = self.login("staff", "S-002")
        self.assertEqual(
            409, self.client.post(base + "/claim", headers=other).status_code
        )
        self.client.post(
            base + "/case-action",
            headers=self.staff,
            json={
                "action": "reply",
                "message": "请提供设备指示灯状态",
                "expected_version": 2,
            },
        )
        row = self.client.get(f"/api/conversations/{cid}", headers=self.customer).json()
        self.assertEqual("staff", row["messages"][-1]["role"])
        self.client.post(
            base + "/case-action",
            headers=self.staff,
            json={
                "action": "resolve",
                "message": "已联系用户完成处理",
                "expected_version": 3,
                "resolution_code": "solved",
            },
        )
        self.assertEqual("resolved", self.send(cid, "再退款", "r2").json()["status"])

    def test_message_validation_and_request_replay(self):
        cid = self.create()
        self.assertEqual(422, self.send(cid, "   ").status_code)
        a = self.send(cid, "蓝牙连不上").json()
        b = self.send(cid, "蓝牙连不上").json()
        self.assertEqual(a, b)
        self.assertEqual(409, self.send(cid, "我要退款").status_code)
        row = self.client.get(f"/api/conversations/{cid}", headers=self.customer).json()
        self.assertEqual(2, len(row["messages"]))

    def test_demo_login_disabled(self):
        with TestClient(
            create_app(
                Path(self.tmp.name) / "prod.db",
                demo_mode=False,
                identities={"secret": {"role": "customer", "id": "C-001"}},
            )
        ) as client:
            self.assertEqual(
                404,
                client.post(
                    "/api/demo/login", json={"role": "customer", "identity": "C-001"}
                ).status_code,
            )
            self.assertEqual(
                200,
                client.get(
                    "/api/orders", headers={"Authorization": "Bearer secret"}
                ).status_code,
            )

    def test_version_and_resolution_are_required(self):
        cid = self.create()
        self.send(cid, "转人工")
        base = f"/api/staff/conversations/{cid}"
        self.client.post(base + "/claim", headers=self.staff)
        for body in [
            {"action": "reply", "message": "您好"},
            {"action": "resolve", "message": "已处理", "expected_version": 2},
        ]:
            r = self.client.post(base + "/case-action", headers=self.staff, json=body)
            self.assertIn(r.status_code, (400, 422))
        row = self.client.get(f"/api/conversations/{cid}", headers=self.customer).json()
        self.assertEqual("in_progress", row["case"]["status"])

    def test_supervisor_reopen_and_customer_operations_forbidden(self):
        cid = self.create()
        self.send(cid, "转人工")
        base = f"/api/staff/conversations/{cid}"
        self.client.post(base + "/claim", headers=self.staff)
        r = self.client.post(
            base + "/case-action",
            headers=self.staff,
            json={
                "action": "resolve",
                "message": "已完成排障",
                "expected_version": 2,
                "resolution_code": "solved",
            },
        )
        self.assertEqual(200, r.status_code)
        body = {"action": "reopen", "message": "客户反馈复发", "expected_version": 3}
        self.assertEqual(
            403,
            self.client.post(
                base + "/case-action", headers=self.staff, json=body
            ).status_code,
        )
        supervisor = self.login("staff", "S-002")
        self.assertEqual(
            200,
            self.client.post(
                base + "/case-action", headers=supervisor, json=body
            ).status_code,
        )
        for path in ["/api/staff/overview", "/api/staff/outbox", base + "/audit"]:
            self.assertEqual(
                403, self.client.get(path, headers=self.customer).status_code
            )
        self.assertEqual(
            403,
            self.client.post(
                "/api/staff/outbox/missing/retry", headers=self.staff
            ).status_code,
        )

    def test_transfer_rejects_unknown_identity_and_stale_update(self):
        cid = self.create()
        self.send(cid, "转人工")
        base = f"/api/staff/conversations/{cid}"
        self.client.post(base + "/claim", headers=self.staff)
        body = {
            "action": "transfer",
            "message": "移交专员",
            "expected_version": 2,
            "target": "unknown",
        }
        self.assertIn(
            self.client.post(
                base + "/case-action", headers=self.staff, json=body
            ).status_code,
            (400, 422),
        )
        r = self.client.post(
            base + "/case-action",
            headers=self.staff,
            json={"action": "reply", "message": "您好", "expected_version": 1},
        )
        self.assertEqual(409, r.status_code)
