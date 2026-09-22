"""Persistent conversation and human-handoff state machine (not LangGraph)."""

from __future__ import annotations

import json
import hashlib
import secrets
from dataclasses import asdict

from .agent import SupportAgent
from .routing import rule_classify
from .store import utc_now


from .cases import CaseService, CaseError as WorkflowError


class ConversationService:
    def __init__(self, store, router=None):
        self.store = store
        self.agent = SupportAgent(store, router=router)
        with store.lock, store.connection:
            store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS conversations(
                  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, order_id TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'bot', topic TEXT NOT NULL DEFAULT '',
                  summary TEXT NOT NULL DEFAULT '', ticket_id TEXT, assignee TEXT,
                  reason TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'normal',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_messages(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                  role TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_conversation ON conversation_messages(conversation_id,id);
                CREATE TABLE IF NOT EXISTS conversation_requests(
                  request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL);
            """)

        self.cases = CaseService(store)

    def create(self, customer, order_id):
        self.agent.tools.call(
            "get_order", {"customer_id": customer, "order_id": order_id}
        )
        cid = secrets.token_urlsafe(18)
        now = utc_now()
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT INTO conversations(id,customer_id,order_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (cid, customer, order_id, now, now),
            )
        return self.get(cid, customer)

    def row(self, cid, customer=None):
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT * FROM conversations WHERE id=?", (cid,)
            ).fetchone()
        if not row or (customer is not None and row["customer_id"] != customer):
            raise WorkflowError("会话不存在或无权访问", 404)
        return dict(row)

    def get(self, cid, customer=None):
        row = self.row(cid, customer)
        with self.store.lock:
            messages = self.store.connection.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 100",
                (cid,),
            ).fetchall()
        row["messages"] = [
            {**dict(m), "metadata": json.loads(m["metadata"])}
            for m in reversed(messages)
        ]
        row["order"] = dict(self.store.order(row["order_id"]))
        row["case"] = self.cases.get(cid)
        import time

        for message in row["messages"]:
            token = message["metadata"].get("confirmation_token")
            if token:
                pending = self.store.pending(token)
                message["metadata"]["confirmation_status"] = (
                    "confirmed"
                    if pending and pending["status"] == "confirmed"
                    else "expired"
                    if not pending or pending["expires_at"] < time.time()
                    else "pending"
                )
        return row

    def list(self, customer=None):
        with self.store.lock:
            if customer is None:
                rows = self.store.connection.execute(
                    "SELECT * FROM conversations WHERE state IN ('waiting_human','human') ORDER BY CASE priority WHEN 'urgent' THEN 0 ELSE 1 END,created_at LIMIT 100"
                ).fetchall()
            else:
                rows = self.store.connection.execute(
                    "SELECT * FROM conversations WHERE customer_id=? ORDER BY updated_at DESC LIMIT 50",
                    (customer,),
                ).fetchall()
        return [dict(row) for row in rows]

    def append(self, cid, role, content, metadata=None):
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT INTO conversation_messages(conversation_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)",
                (
                    cid,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            self.store.connection.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (utc_now(), cid)
            )

    def context(self, cid):
        row = self.row(cid)
        with self.store.lock:
            rows = self.store.connection.execute(
                "SELECT role,content FROM conversation_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 9",
                (cid,),
            ).fetchall()
        recent = list(reversed(rows[:8]))
        # Move an old user statement into a bounded extractive digest; no generated facts.
        summary = row["summary"]
        if len(rows) > 8 and rows[8]["role"] == "user":
            line = rows[8]["content"][:240]
            lines = [x for x in summary.splitlines() if x != line] + [line]
            summary = "\n".join(lines)[-1200:]
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE conversations SET summary=? WHERE id=?", (summary, cid)
                )
        remaining = 5000
        chosen = []
        for message in reversed(recent):
            content = message["content"][: min(1000, remaining)]
            if not content:
                break
            chosen.append({"role": message["role"], "content": content})
            remaining -= len(content)
        return {
            "topic": row["topic"],
            "earlier_user_excerpts": summary,
            "recent_messages": list(reversed(chosen)),
        }

    def send(self, cid, customer, request_id, message):
        # One process serializes mutations. SQLite unique constraints also reject
        # conflicting request IDs across processes. Do not hold a payment network call here.
        with self.store.lock:
            row = self.row(cid, customer)
            fingerprint = hashlib.sha256(message.encode()).hexdigest()
            old = self.store.connection.execute(
                "SELECT * FROM conversation_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                if old["conversation_id"] != cid or old["fingerprint"] != fingerprint:
                    raise WorkflowError("请求编号已被其他内容使用")
                if old["response"]:
                    return json.loads(old["response"])
                raise WorkflowError("请求处理中或上次执行中断，请刷新会话核对结果")
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT INTO conversation_requests VALUES(?,?,?,?,?)",
                    (request_id, cid, fingerprint, None, utc_now()),
                )
            context = self.context(cid)
            self.append(cid, "user", message, {"request_id": request_id})
            self.cases.customer_message(cid)
            self.agent.tools.audit = lambda name, status, ms: self.store.append_event(
                request_id,
                "tool_call",
                {"tool": name, "status": status, "duration_ms": ms},
            )
            try:
                if row["state"] in {"waiting_human", "human", "resolved"}:
                    if row["state"] == "resolved":
                        reply = {
                            "status": "resolved",
                            "route": "human",
                            "message": "本会话已结束。需要新的帮助时请新建会话。",
                        }
                    else:
                        if rule_classify(message) == "safety":
                            self.cases.escalate_safety(cid)
                            self.append(
                                cid,
                                "system",
                                "检测到安全风险，请停止使用设备；已提高工单优先级。",
                            )
                        reply = {
                            "status": "handoff",
                            "route": "human",
                            "message": "消息已送达人工队列，自动处理已暂停。",
                            "ticket_id": row["ticket_id"],
                        }
                else:
                    current = rule_classify(message)
                    failed = any(
                        x in message
                        for x in ("还是不行", "仍然不行", "没解决", "没用", "试过了")
                    )
                    followup = message.startswith(
                        ("那", "这个", "它", "继续", "怎么操作", "具体")
                    )
                    query = message
                    if current == "unknown" and context["topic"] and followup:
                        query = context["topic"] + "；当前追问：" + message
                    if current == "safety":
                        query = message
                    elif failed and context["topic"]:
                        query = "请转人工。用户反馈排障未解决：" + message
                    if current == "knowledge":
                        with self.store.connection:
                            self.store.connection.execute(
                                "UPDATE conversations SET topic=? WHERE id=?",
                                (message[:500], cid),
                            )
                    # The optional classifier sees bounded role-labelled history;
                    # current explicit intent/safety still takes priority.
                    router = self.agent.router
                    fixed_route = None
                    if current in {"safety", "refund", "ticket"}:
                        fixed_route = current
                    elif failed and context["topic"]:
                        fixed_route = "ticket"
                    elif hasattr(router, "route_with_context"):
                        fixed_route = router.route_with_context(message, context)
                    if fixed_route is not None:

                        class FixedRouter:
                            def route(self, _):
                                return fixed_route

                        self.agent.router = FixedRouter()
                    try:
                        reply = asdict(
                            self.agent.handle(
                                request_id, customer, row["order_id"], query
                            )
                        )
                    finally:
                        self.agent.router = router
                    if reply["status"] == "handoff":
                        reason = (
                            "产品安全风险"
                            if reply["route"] == "safety"
                            else "排障未解决"
                            if failed
                            else "退款需人工核验"
                            if reply["route"] == "refund"
                            else "用户请求人工或知识证据不足"
                        )
                        priority = "urgent" if reply["route"] == "safety" else "normal"
                        self.cases.open(cid, reply["ticket_id"], reason, priority)
                        reply["message"] += (
                            " 客服工作台已收到本会话、订单和历史记录，等待接管。"
                        )
                    # Keep pending confirmation as explicit business data, not inferred dialogue.
                self.append(cid, "assistant", reply["message"], reply)
                reply["conversation_id"] = cid
                reply["state"] = self.row(cid)["state"]
                with self.store.connection:
                    self.store.connection.execute(
                        "UPDATE conversation_requests SET response=? WHERE request_id=?",
                        (json.dumps(reply, ensure_ascii=False), request_id),
                    )
                return reply
            finally:
                self.agent.tools.audit = None

    def claim(self, cid, staff, expected_version=None):
        self.cases.action(cid, staff, "claim", expected_version=expected_version)
        return self.get(cid)

    def staff_action(
        self,
        cid,
        staff,
        action,
        text,
        *,
        expected_version=None,
        target=None,
        resolution_code=None,
        supervisor=False,
    ):
        if action in {"resolve", "resume"} and resolution_code is None:
            resolution_code = "return_to_bot" if action == "resume" else "solved"
        self.cases.action(
            cid,
            staff,
            action,
            text,
            expected_version=expected_version,
            target=target,
            resolution_code=resolution_code,
            supervisor=supervisor,
        )
        return self.get(cid)

    def confirm(self, cid, customer, token):
        with self.store.lock:
            row = self.row(cid, customer)
            pending = self.store.pending(token)
            if (
                not pending
                or pending["customer_id"] != customer
                or pending["order_id"] != row["order_id"]
            ):
                raise WorkflowError("提案不属于当前会话", 403)
            owner = self.store.connection.execute(
                "SELECT conversation_id FROM conversation_requests WHERE request_id=?",
                (pending["request_id"],),
            ).fetchone()
            if not owner or owner[0] != cid:
                raise WorkflowError("提案不属于当前会话", 403)
            if row["state"] != "bot":
                raise WorkflowError("人工处理期间或结案后不能自动确认退款")
            result = asdict(self.agent.confirm_refund(token, customer))
            self.append(cid, "assistant", result["message"], result)
            return result
