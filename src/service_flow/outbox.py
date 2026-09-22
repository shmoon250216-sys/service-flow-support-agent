"""Transactional outbox with leased delivery and an idempotent local inbox."""

from __future__ import annotations
import json
import secrets
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_events(
 id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT,
 last_error TEXT, created_at REAL NOT NULL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_events(status,available_at);
CREATE TABLE IF NOT EXISTS notification_inbox(
 event_id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL,
 payload TEXT NOT NULL, received_at REAL NOT NULL);
"""


def publish(connection, aggregate_id, event_type, payload, *, event_id=None, now=None):
    """Caller owns the transaction: business state and event commit together."""
    now = time.time() if now is None else now
    event_id = event_id or secrets.token_urlsafe(20)
    connection.execute(
        "INSERT OR IGNORE INTO outbox_events(id,aggregate_id,event_type,payload,available_at,created_at) VALUES(?,?,?,?,?,?)",
        (
            event_id,
            aggregate_id,
            event_type,
            json.dumps(payload, ensure_ascii=False),
            now,
            now,
        ),
    )
    return event_id


class OutboxDispatcher:
    def __init__(self, store, *, clock=time.time, max_attempts=3, lease_seconds=30):
        self.store = store
        self.clock = clock
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds

    def claim(self):
        now = self.clock()
        with self.store.lock:
            db = self.store.connection
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "UPDATE outbox_events SET status='dead',lease_token=NULL,last_error='lease exhausted' WHERE status='processing' AND lease_until<=? AND attempts>=?",
                    (now, self.max_attempts),
                )
                row = db.execute(
                    "SELECT * FROM outbox_events WHERE attempts<? AND ((status='pending' AND available_at<=?) OR (status='processing' AND lease_until<=?)) ORDER BY created_at,id LIMIT 1",
                    (self.max_attempts, now, now),
                ).fetchone()
                if not row:
                    db.commit()
                    return None
                lease = secrets.token_urlsafe(12)
                db.execute(
                    "UPDATE outbox_events SET status='processing',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",
                    (now + self.lease_seconds, lease, row["id"]),
                )
                db.commit()
                return {
                    **dict(row),
                    "lease_token": lease,
                    "attempts": row["attempts"] + 1,
                }
            except Exception:
                db.rollback()
                raise

    def local_sink(self, event):
        # This is a local notification center, not a sent email / Slack message.
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT OR IGNORE INTO notification_inbox VALUES(?,?,?,?,?)",
                (
                    event["id"],
                    event["aggregate_id"],
                    event["event_type"],
                    event["payload"],
                    self.clock(),
                ),
            )

    def drain(self, limit=20, sink=None):
        result = {"delivered": 0, "failed": 0}
        for _ in range(limit):
            event = self.claim()
            if event is None:
                break
            try:
                (sink or self.local_sink)(event)
            except Exception as exc:
                state = "dead" if event["attempts"] >= self.max_attempts else "pending"
                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        "UPDATE outbox_events SET status=?,available_at=?,lease_token=NULL,last_error=? WHERE id=? AND lease_token=?",
                        (
                            state,
                            self.clock() + min(60, 2 ** event["attempts"]),
                            type(exc).__name__,
                            event["id"],
                            event["lease_token"],
                        ),
                    )
                result["failed"] += 1
            else:
                with self.store.lock, self.store.connection:
                    updated = self.store.connection.execute(
                        "UPDATE outbox_events SET status='delivered',delivered_at=?,lease_token=NULL,last_error=NULL WHERE id=? AND lease_token=?",
                        (self.clock(), event["id"], event["lease_token"]),
                    ).rowcount
                result["delivered"] += updated
        return result

    def retry_dead(self, event_id, actor):
        with self.store.lock, self.store.connection:
            updated = self.store.connection.execute(
                "UPDATE outbox_events SET status='pending',attempts=0,available_at=?,last_error=NULL WHERE id=? AND status='dead'",
                (self.clock(), event_id),
            ).rowcount
            if not updated:
                raise ValueError("只能重放失败队列中的事件")
            publish(
                self.store.connection,
                event_id,
                "outbox.replayed",
                {"actor": actor},
                now=self.clock(),
            )
