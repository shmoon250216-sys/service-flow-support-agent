from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from service_flow.api import create_app


class ApiTests(unittest.TestCase):
    def test_chat_and_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(Path(tmp) / "api.db")) as client:
                proposed = client.post("/chat", json={"request_id": "api-1", "customer_id": "C-001", "order_id": "O-1001", "message": "我要退款"})
                self.assertEqual(200, proposed.status_code)
                self.assertEqual("confirmation_required", proposed.json()["status"])
                confirmed = client.post("/refund/confirm", json={"token": proposed.json()["confirmation_token"], "customer_id": "C-001"})
                self.assertEqual("completed", confirmed.json()["status"])
                self.assertIn("ServiceFlow", client.get("/").text)
                self.assertEqual(2, client.get("/health").json()["tools"])
                self.assertEqual(1, len(client.get("/api/orders/C-001").json()))
                self.assertEqual({"create_ticket", "submit_refund"}, {item["name"] for item in client.get("/api/tools").json()})
                self.assertGreaterEqual(len(client.get("/api/events/api-1").json()), 1)



if __name__ == "__main__":
    unittest.main()
