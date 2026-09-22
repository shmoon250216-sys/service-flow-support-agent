"""Ticket aggregate: explicit FSM, optimistic versioning, SLA and durable events."""

from __future__ import annotations
import json
import time
from .outbox import publish
from .store import utc_now


class CaseError(ValueError):
    def __init__(self, message, status=409):
        self.status = status
        super().__init__(message)


SLA = {"normal": (1800, 28800), "urgent": (300, 3600)}
SCHEMA = """
CREATE TABLE IF NOT EXISTS case_records(
 conversation_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, status TEXT NOT NULL,
 queue TEXT NOT NULL, priority TEXT NOT NULL, assignee TEXT, version INTEGER NOT NULL,
 created_at REAL NOT NULL, response_due REAL NOT NULL, resolve_due REAL NOT NULL,
 first_response_at REAL, resolved_at REAL, response_breached INTEGER NOT NULL DEFAULT 0,
 resolution_breached INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL, resolution_code TEXT);
CREATE TABLE IF NOT EXISTS case_audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, actor TEXT NOT NULL,
 action TEXT NOT NULL, from_status TEXT, to_status TEXT, version INTEGER NOT NULL,
 note TEXT NOT NULL, created_at REAL NOT NULL);
"""


class CaseService:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock
        with store.lock, store.connection:
            store.connection.executescript(SCHEMA)
        # Upgrade already-persisted 0.2 conversations without rewriting their history.
        with store.lock:
            old = store.connection.execute(
                "SELECT * FROM conversations WHERE ticket_id IS NOT NULL AND id NOT IN (SELECT conversation_id FROM case_records)"
            ).fetchall()
        for row in old:
            self.open(
                row["id"],
                row["ticket_id"],
                row["reason"],
                row["priority"],
                migration=dict(row),
            )

    def get(self, cid):
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT * FROM case_records WHERE conversation_id=?", (cid,)
            ).fetchone()
        return dict(row) if row else None

    def _audit(self, db, cid, actor, action, old, new, version, note):
        db.execute(
            "INSERT INTO case_audit(conversation_id,actor,action,from_status,to_status,version,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cid, actor, action, old, new, version, note, self.clock()),
        )

    def _message(self, db, cid, role, text, meta=None):
        db.execute(
            "INSERT INTO conversation_messages(conversation_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)",
            (cid, role, text, json.dumps(meta or {}, ensure_ascii=False), utc_now()),
        )
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (utc_now(), cid))

    def open(self, cid, ticket_id, reason, priority, *, migration=None):
        now = self.clock()
        response, resolution = SLA[priority]
        queue = (
            "safety"
            if priority == "urgent"
            else "billing"
            if any(x in reason for x in ["退款", "退货", "七天"])
            else "technical"
        )
        with self.store.lock, self.store.connection:
            db = self.store.connection
            conv = db.execute(
                "SELECT * FROM conversations WHERE id=?", (cid,)
            ).fetchone()
            latest = db.execute(
                "SELECT role,content FROM conversation_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 6",
                (cid,),
            ).fetchall()
            summary = json.dumps(
                {
                    "order_id": conv["order_id"],
                    "reason": reason,
                    "topic": conv["topic"],
                    "recent_statements": [
                        {"role": x["role"], "content": x["content"][:240]}
                        for x in reversed(latest)
                    ],
                },
                ensure_ascii=False,
            )
            status = (
                "in_progress"
                if migration and migration["state"] == "human"
                else "resolved"
                if migration and migration["state"] in {"resolved", "bot"}
                else "open"
            )
            assignee = migration["assignee"] if migration else None
            existing = self.get(cid)
            version = existing["version"] + 1 if existing else 1
            db.execute(
                "INSERT INTO case_records(conversation_id,ticket_id,status,queue,priority,assignee,version,created_at,response_due,resolve_due,summary) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET ticket_id=excluded.ticket_id,status=excluded.status,queue=excluded.queue,priority=excluded.priority,assignee=excluded.assignee,version=excluded.version,response_due=excluded.response_due,resolve_due=excluded.resolve_due,first_response_at=NULL,resolved_at=NULL,response_breached=0,resolution_breached=0,summary=excluded.summary,resolution_code=NULL",
                (
                    cid,
                    ticket_id,
                    status,
                    queue,
                    priority,
                    assignee,
                    version,
                    now,
                    now + response,
                    now + resolution,
                    summary,
                ),
            )
            if not migration:
                db.execute(
                    "UPDATE conversations SET state='waiting_human',ticket_id=?,reason=?,priority=?,assignee=NULL WHERE id=?",
                    (ticket_id, reason, priority, cid),
                )
            self._audit(
                db,
                cid,
                "system",
                "opened",
                existing["status"] if existing else None,
                status,
                version,
                reason,
            )
            publish(
                db,
                cid,
                "case.opened",
                {"ticket_id": ticket_id, "queue": queue, "priority": priority},
                now=now,
            )

    def action(
        self,
        cid,
        actor,
        action,
        note="",
        *,
        expected_version=None,
        target=None,
        resolution_code=None,
        supervisor=False,
    ):
        now = self.clock()
        with self.store.lock:
            db = self.store.connection
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM case_records WHERE conversation_id=?", (cid,)
                ).fetchone()
                if not row:
                    raise CaseError("没有可操作的工单", 404)
                if expected_version is not None and expected_version != row["version"]:
                    raise CaseError("工单已被更新，请刷新后重试")
                old = row["status"]
                new = old
                owner = row["assignee"]
                conv_state = "human"
                if action == "claim":
                    if old == "in_progress" and owner == actor:
                        db.rollback()
                        return self.get(cid)
                    if old != "open":
                        raise CaseError("工单已被接管或不在队列")
                    new = "in_progress"
                    owner = actor
                else:
                    if action == "reopen":
                        if old != "resolved":
                            raise CaseError("只有已解决工单可重开")
                        if not supervisor:
                            raise CaseError("重开需要主管权限", 403)
                        new = "open"
                        owner = None
                        conv_state = "waiting_human"
                    else:
                        if owner != actor and not supervisor:
                            raise CaseError("仅接管客服或主管可操作", 403)
                        if old not in {"in_progress", "pending_customer"}:
                            raise CaseError("当前工单状态不允许此操作")
                        if action == "reply":
                            new = "in_progress"
                        elif action == "wait_customer":
                            new = "pending_customer"
                        elif action == "transfer":
                            if not target:
                                raise CaseError("请选择接管人", 422)
                            owner = target
                            new = "in_progress"
                        elif action in {"resolve", "resume"}:
                            if not note.strip():
                                raise CaseError("必须填写处理结论", 422)
                            if action == "resume" and row["priority"] == "urgent":
                                raise CaseError("安全工单不能恢复自动排障")
                            if resolution_code not in {
                                "solved",
                                "repair",
                                "policy_explained",
                                "return_to_bot",
                            }:
                                raise CaseError("必须提供有效结案分类", 422)
                            new = "resolved"
                            owner = None
                            conv_state = "bot" if action == "resume" else "resolved"
                        else:
                            raise CaseError("不支持的工单操作", 422)
                # A reply/resolve arriving between scheduler ticks must still count as late.
                if old != "resolved":
                    for kind, due, completed in [
                        ("response", row["response_due"], row["first_response_at"]),
                        ("resolution", row["resolve_due"], row["resolved_at"]),
                    ]:
                        if (
                            completed is None
                            and due <= now
                            and not row[kind + "_breached"]
                        ):
                            db.execute(
                                f"UPDATE case_records SET {kind}_breached=1 WHERE conversation_id=?",
                                (cid,),
                            )
                            publish(
                                db,
                                cid,
                                "sla." + kind + "_breached",
                                {"ticket_id": row["ticket_id"], "queue": row["queue"]},
                                event_id=f"sla:{cid}:{kind}:{due}",
                                now=now,
                            )
                version = row["version"] + 1
                changed = db.execute(
                    "UPDATE case_records SET status=?,assignee=?,version=? WHERE conversation_id=? AND version=?",
                    (new, owner, version, cid, row["version"]),
                ).rowcount
                if not changed:
                    raise CaseError("版本冲突，请刷新重试")
                if action == "reply":
                    db.execute(
                        "UPDATE case_records SET first_response_at=COALESCE(first_response_at,?) WHERE conversation_id=?",
                        (now, cid),
                    )
                    self._message(db, cid, "staff", note, {"staff": actor})
                else:
                    labels = {
                        "claim": "客服已接管",
                        "wait_customer": "等待客户补充",
                        "transfer": "工单已转交",
                        "resolve": "客服已结案",
                        "resume": "客服已恢复自动服务",
                        "reopen": "主管已重开工单",
                    }
                    self._message(
                        db,
                        cid,
                        "system",
                        labels[action] + "：" + (note or actor),
                        {"staff": actor},
                    )
                if action in {"resolve", "resume"}:
                    db.execute(
                        "UPDATE case_records SET resolved_at=?,resolution_code=? WHERE conversation_id=?",
                        (now, resolution_code, cid),
                    )
                if action == "reopen":
                    r, s = SLA[row["priority"]]
                    db.execute(
                        "UPDATE case_records SET response_due=?,resolve_due=?,first_response_at=NULL,resolved_at=NULL,response_breached=0,resolution_breached=0,resolution_code=NULL WHERE conversation_id=?",
                        (now + r, now + s, cid),
                    )
                db.execute(
                    "UPDATE conversations SET state=?,assignee=? WHERE id=?",
                    (conv_state, owner, cid),
                )
                self._audit(db, cid, actor, action, old, new, version, note)
                publish(
                    db,
                    cid,
                    "case." + action,
                    {
                        "actor": actor,
                        "status": new,
                        "version": version,
                        "target": owner,
                    },
                    now=now,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self.get(cid)

    def customer_message(self, cid):
        with self.store.lock, self.store.connection:
            row = self.get(cid)
            if row and row["status"] == "pending_customer":
                self.store.connection.execute(
                    "UPDATE case_records SET status='in_progress',version=version+1 WHERE conversation_id=?",
                    (cid,),
                )
                self._audit(
                    self.store.connection,
                    cid,
                    "customer",
                    "customer_replied",
                    row["status"],
                    "in_progress",
                    row["version"] + 1,
                    "客户补充信息",
                )
                publish(
                    self.store.connection,
                    cid,
                    "case.customer_replied",
                    {"ticket_id": row["ticket_id"]},
                    now=self.clock(),
                )

    def escalate_safety(self, cid):
        now = self.clock()
        with self.store.lock, self.store.connection:
            db = self.store.connection
            row = self.get(cid)
            if not row or row["priority"] == "urgent":
                return
            db.execute(
                "UPDATE case_records SET priority='urgent',queue='safety',response_due=MIN(response_due,?),resolve_due=MIN(resolve_due,?),version=version+1 WHERE conversation_id=?",
                (now + 300, now + 3600, cid),
            )
            db.execute("UPDATE conversations SET priority='urgent' WHERE id=?", (cid,))
            db.execute(
                "UPDATE tickets SET priority='urgent' WHERE ticket_id=?",
                (row["ticket_id"],),
            )
            self._audit(
                db,
                cid,
                "system",
                "safety_escalated",
                row["status"],
                row["status"],
                row["version"] + 1,
                "用户补充安全风险",
            )
            publish(db, cid, "case.safety_escalated", {"priority": "urgent"}, now=now)

    def scan_sla(self):
        now = self.clock()
        count = 0
        with self.store.lock, self.store.connection:
            db = self.store.connection
            rows = db.execute(
                "SELECT * FROM case_records WHERE status!='resolved'"
            ).fetchall()
            for row in rows:
                for kind, due, complete in [
                    ("response", row["response_due"], row["first_response_at"]),
                    ("resolution", row["resolve_due"], row["resolved_at"]),
                ]:
                    if complete is None and due <= now and not row[kind + "_breached"]:
                        db.execute(
                            f"UPDATE case_records SET {kind}_breached=1,version=version+1 WHERE conversation_id=?",
                            (row["conversation_id"],),
                        )
                        self._audit(
                            db,
                            row["conversation_id"],
                            "scheduler",
                            kind + "_breached",
                            row["status"],
                            row["status"],
                            self.get(row["conversation_id"])["version"],
                            "处理时限超期，通知主管",
                        )
                        publish(
                            db,
                            row["conversation_id"],
                            "sla." + kind + "_breached",
                            {"ticket_id": row["ticket_id"], "queue": row["queue"]},
                            event_id=f"sla:{row['conversation_id']}:{kind}:{due}",
                            now=now,
                        )
                        count += 1
        return count

    def audit(self, cid):
        with self.store.lock:
            return [
                dict(r)
                for r in self.store.connection.execute(
                    "SELECT * FROM case_audit WHERE conversation_id=? ORDER BY id",
                    (cid,),
                ).fetchall()
            ]

    def overview(self):
        with self.store.lock:
            db = self.store.connection
            return {
                "cases": {
                    r["status"]: r["n"]
                    for r in db.execute(
                        "SELECT status,count(*) n FROM case_records GROUP BY status"
                    )
                },
                "sla_breaches": db.execute(
                    "SELECT COALESCE(sum(response_breached+resolution_breached),0) FROM case_records"
                ).fetchone()[0],
                "outbox": {
                    r["status"]: r["n"]
                    for r in db.execute(
                        "SELECT status,count(*) n FROM outbox_events GROUP BY status"
                    )
                },
                "notifications": db.execute(
                    "SELECT count(*) FROM notification_inbox"
                ).fetchone()[0],
            }

    def search(self, status="active", queue="all"):
        clauses = []
        params = []
        if status == "active":
            clauses.append("k.status!='resolved'")
        elif status != "all":
            clauses.append("k.status=?")
            params.append(status)
        if queue != "all":
            clauses.append("k.queue=?")
            params.append(queue)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.lock:
            rows = self.store.connection.execute(
                "SELECT c.*,k.status AS case_status,k.queue AS case_queue,k.response_due,k.resolve_due,k.response_breached,k.resolution_breached FROM conversations c JOIN case_records k ON c.id=k.conversation_id"
                + where
                + " ORDER BY CASE k.priority WHEN 'urgent' THEN 0 ELSE 1 END,k.response_due LIMIT 100",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
