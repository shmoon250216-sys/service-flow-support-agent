from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SupportStore:
    def __init__(self, path: str | Path = "data/service-flow.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.lock:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders(
                  order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, product TEXT NOT NULL,
                  paid_amount REAL NOT NULL, delivered_days INTEGER NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS articles(
                  article_id TEXT PRIMARY KEY, product TEXT NOT NULL, title TEXT NOT NULL,
                  content TEXT NOT NULL, source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions(
                  token TEXT PRIMARY KEY, request_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                  order_id TEXT NOT NULL, action_type TEXT NOT NULL, payload TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effects(
                  idempotency_key TEXT PRIMARY KEY, action_type TEXT NOT NULL,
                  output TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets(
                  ticket_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                  customer_id TEXT NOT NULL, order_id TEXT, priority TEXT NOT NULL,
                  reason TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                  event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id,id);
                """
            )
            self.connection.commit()

    def seed(self) -> None:
        orders = [
            ("O-1001", "C-001", "AirBuds Pro", 699.0, 3, "delivered"),
            ("O-1002", "C-002", "AirBuds Pro", 699.0, 12, "delivered"),
            ("O-1003", "C-003", "HomeCam Mini", 299.0, 2, "delivered"),
            ("O-1004", "C-004", "HomeCam Mini", 299.0, 45, "delivered"),
        ]
        articles = [
            ("KB-AIR-RESET", "AirBuds Pro", "耳机无法连接的复位流程", "忘记旧蓝牙记录，将双耳放回充电盒并长按按键十秒，指示灯白色闪烁后重新配对。", "AirBuds Pro 用户手册 v2.1"),
            ("KB-AIR-NOISE", "AirBuds Pro", "单耳杂音排查", "先清洁网罩并关闭空间音频；仍有杂音时交换左右耳测试，记录序列号后创建质检工单。", "AirBuds Pro 售后知识库 2026-08"),
            ("KB-CAM-OFFLINE", "HomeCam Mini", "摄像头离线处理", "确认路由器为 2.4GHz，重启摄像头和路由器；长按 Reset 五秒后在 App 中重新添加设备。", "HomeCam Mini 运维手册 v3.0"),
            ("KB-CAM-PRIVACY", "HomeCam Mini", "隐私模式说明", "开启隐私模式后镜头物理遮蔽并停止视频上传，状态灯熄灭；关闭后恢复监控。", "HomeCam Mini 隐私说明 2026-06"),
            ("KB-REFUND", "ALL", "七天无理由退款规则", "签收七天内且配件齐全、无非质量损坏可申请退款；退款提交前必须由订单本人确认。", "售后政策 v4.2"),
            ("KB-WARRANTY", "ALL", "质保与换新规则", "主机质保十二个月。出现冒烟、过热或电池鼓包时停止使用，不再引导自助排查，立即转人工安全专席。", "产品安全政策 v5.0"),
        ]
        with self.lock, self.connection:
            self.connection.executemany("INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?)", orders)
            self.connection.executemany("INSERT OR IGNORE INTO articles VALUES(?,?,?,?,?)", articles)

    def order(self, order_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()

    def orders_for(self, customer_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM orders WHERE customer_id=? ORDER BY order_id", (customer_id,)).fetchall()
        return [dict(row) for row in rows]

    def articles(self, product: str) -> list[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM articles WHERE product IN (?, 'ALL') ORDER BY article_id", (product,)
            ).fetchall()

    def append_event(self, request_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO events(request_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (request_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
            )

    def create_ticket(self, request_id: str, customer_id: str, order_id: str | None, priority: str, reason: str) -> str:
        ticket_id = "T-" + hashlib.sha1(request_id.encode()).hexdigest()[:8].upper()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO tickets VALUES(?,?,?,?,?,?,?)",
                (ticket_id, request_id, customer_id, order_id, priority, reason, utc_now()),
            )
        return ticket_id

    def ticket_count(self, request_id: str | None = None) -> int:
        with self.lock:
            if request_id:
                row = self.connection.execute("SELECT COUNT(*) n FROM tickets WHERE request_id=?", (request_id,)).fetchone()
            else:
                row = self.connection.execute("SELECT COUNT(*) n FROM tickets").fetchone()
        return int(row["n"])

    def tickets(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [dict(row) for row in rows]

    def save_pending(self, token: str, request_id: str, customer_id: str, order_id: str, payload: dict[str, Any]) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO pending_actions VALUES(?,?,?,?,?,?,?,?)",
                (token, request_id, customer_id, order_id, "refund", json.dumps(payload, ensure_ascii=False), "pending", utc_now()),
            )

    def pending(self, token: str) -> sqlite3.Row | None:
        with self.lock:
            return self.connection.execute("SELECT * FROM pending_actions WHERE token=?", (token,)).fetchone()

    def mark_confirmed(self, token: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE pending_actions SET status='confirmed' WHERE token=?", (token,))

    def load_effect(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute("SELECT output FROM effects WHERE idempotency_key=?", (key,)).fetchone()
        return json.loads(row["output"]) if row else None

    def save_effect(self, key: str, output: dict[str, Any]) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO effects VALUES(?,?,?,?)",
                (key, "refund", json.dumps(output, ensure_ascii=False), utc_now()),
            )
        return cursor.rowcount == 1

    def effect_count(self) -> int:
        with self.lock:
            return int(self.connection.execute("SELECT COUNT(*) n FROM effects").fetchone()["n"])

    def events(self, request_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT event_type,payload,created_at FROM events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self.lock:
            self.connection.close()
