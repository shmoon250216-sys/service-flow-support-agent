from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

from .routing import Router, RuleRouter, rule_classify
from .store import SupportStore
from .tools import ToolRegistry, build_tools
from .types import SupportReply


SYNONYMS = {
    "连不上": "连接 配对 蓝牙",
    "搜不到": "连接 配对 蓝牙",
    "断网": "离线 路由器",
    "没画面": "离线 摄像头",
    "退货": "退款 七天",
    "不要了": "退款 七天",
    "发烫": "过热 安全",
    "起火": "冒烟 安全",
    "噪声": "杂音 单耳",
}


def tokens(text: str) -> set[str]:
    normalized = text.lower()
    for source, target in SYNONYMS.items():
        if source in normalized:
            normalized += " " + target
    latin = set(re.findall(r"[a-z0-9]+", normalized))
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    chinese = set()
    for run in chinese_runs:
        chinese.update(run)
        chinese.update(run[i : i + 2] for i in range(len(run) - 1))
    return latin | chinese


class SupportAgent:
    def __init__(
        self,
        store: SupportStore,
        *,
        max_attempts: int = 2,
        router: Router | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.store = store
        self.max_attempts = max(1, max_attempts)
        self.router = router or RuleRouter()
        self.tools = tools or build_tools(store)

    @staticmethod
    def classify(message: str) -> str:
        return rule_classify(message)

    def _create_ticket(
        self,
        request_id: str,
        customer_id: str,
        order_id: str,
        priority: str,
        reason: str,
    ) -> str:
        output = self.tools.call(
            "create_ticket",
            {
                "request_id": request_id,
                "customer_id": customer_id,
                "order_id": order_id,
                "priority": priority,
                "reason": reason,
            },
        )
        return str(output["ticket_id"])

    def retrieve(self, product: str, query: str) -> list[dict]:
        return self.tools.call(
            "search_knowledge", {"product": product, "query": query}
        )["articles"]

    def handle(
        self, request_id: str, customer_id: str, order_id: str, message: str
    ) -> SupportReply:
        self.store.append_event(
            request_id, "received", {"order_id": order_id, "message": message}
        )
        order = self.store.order(order_id)
        if not order or order["customer_id"] != customer_id:
            self.store.append_event(request_id, "ownership_denied", {})
            return SupportReply(
                "denied", "security", "订单不存在或不属于当前用户，已停止调用售后工具。"
            )

        try:
            route = (
                "safety"
                if rule_classify(message) == "safety"
                else self.router.route(message)
            )
        except Exception:
            route = self.classify(message)
        self.store.append_event(request_id, "routed", {"route": route})
        if route in {"safety", "ticket", "unknown"}:
            priority = "urgent" if route == "safety" else "normal"
            reason = (
                "产品安全风险"
                if route == "safety"
                else ("用户要求人工处理" if route == "ticket" else "知识证据不足")
            )
            ticket_id = self._create_ticket(
                request_id, customer_id, order_id, priority, reason
            )
            self.store.append_event(
                request_id,
                "human_handoff",
                {"ticket_id": ticket_id, "priority": priority},
            )
            message_out = (
                "请立即停止使用并远离可燃物，已转人工安全专席。"
                if route == "safety"
                else "当前证据不足或需要人工处理，已创建售后工单。"
            )
            return SupportReply("handoff", route, message_out, ticket_id=ticket_id)

        articles = self.retrieve(order["product"], message)
        if route == "knowledge":
            if not articles or articles[0]["score"] < 0.12:
                ticket_id = self._create_ticket(
                    request_id, customer_id, order_id, "normal", "知识证据不足"
                )
                return SupportReply(
                    "handoff",
                    route,
                    "未检索到足够证据，已转人工。",
                    ticket_id=ticket_id,
                )
            top = articles[0]
            self.store.append_event(
                request_id,
                "answered",
                {"article_id": top["article_id"], "score": top["score"]},
            )
            return SupportReply(
                "answered",
                route,
                top["content"],
                citations=[
                    {k: top[k] for k in ("article_id", "title", "source", "score")}
                ],
            )

        policy = next(
            (item for item in articles if item["article_id"] == "KB-REFUND"), None
        )
        if order["delivered_days"] > 7:
            ticket_id = self._create_ticket(
                request_id, customer_id, order_id, "normal", "超出七天退款窗口"
            )
            return SupportReply(
                "handoff",
                "refund",
                "已超过七天窗口，转人工核验质量与保修条件。",
                citations=[] if not policy else [policy],
                ticket_id=ticket_id,
            )
        if order["status"] == "refunded":
            return SupportReply(
                "completed", "refund", "该订单已经完成本地退款，请勿重复申请。"
            )
        import secrets

        token = secrets.token_urlsafe(32)
        payload = {
            "order_id": order_id,
            "amount": order["paid_amount"],
            "reason": message,
        }
        self.store.save_pending(token, request_id, customer_id, order_id, payload)
        self.store.append_event(request_id, "confirmation_required", payload)
        return SupportReply(
            "confirmation_required",
            "refund",
            f"拟为订单 {order_id} 申请退款 ¥{order['paid_amount']:.2f}。确认后才会提交，请回复确认令牌。",
            citations=[]
            if not policy
            else [{k: policy[k] for k in ("article_id", "title", "source", "score")}],
            confirmation_token=token,
        )

    def confirm_refund(
        self,
        token: str,
        customer_id: str,
        *,
        fail_policy: Callable[[int], bool] | None = None,
    ) -> SupportReply:
        pending = self.store.pending(token)
        if not pending or pending["customer_id"] != customer_id:
            return SupportReply("denied", "refund", "确认令牌无效或不属于当前用户。")
        key = hashlib.sha256(
            f"refund:{token}:{pending['order_id']}".encode()
        ).hexdigest()
        cached = self.store.load_effect(key)
        if cached:
            return SupportReply(
                "completed",
                "refund",
                cached["message"],
                diagnostics={"idempotency_hit": True, "attempts": 0},
            )
        try:
            self.store.authorize_refund(token, customer_id)
        except (ValueError, PermissionError) as exc:
            return SupportReply("denied", "refund", str(exc))
        for attempt in range(1, self.max_attempts + 1):
            self.store.append_event(
                pending["request_id"], "refund_attempted", {"attempt": attempt}
            )
            if fail_policy and fail_policy(attempt):
                self.store.append_event(
                    pending["request_id"], "refund_failed", {"attempt": attempt}
                )
                continue
            try:
                tool_output = self.tools.call(
                    "submit_refund",
                    {
                        "token": token,
                        "order_id": pending["order_id"],
                        "amount": float(json.loads(pending["payload"])["amount"]),
                    },
                )
            except (TimeoutError, ConnectionError) as exc:
                self.store.append_event(
                    pending["request_id"],
                    "refund_failed",
                    {"attempt": attempt, "error": type(exc).__name__},
                )
                if attempt < self.max_attempts:
                    __import__("time").sleep(0.05 * attempt)
                continue
            except (ValueError, PermissionError):
                return SupportReply(
                    "denied", "refund", "退款条件已变化，请重新核对订单或联系人工。"
                )
            refund_id = str(tool_output["refund_id"])
            output = {"refund_id": refund_id, "message": str(tool_output["message"])}
            self.store.save_effect(key, output)
            self.store.mark_confirmed(token)
            self.store.append_event(
                pending["request_id"],
                "refund_succeeded",
                {"attempt": attempt, "refund_id": refund_id},
            )
            return SupportReply(
                "completed",
                "refund",
                output["message"],
                diagnostics={"idempotency_hit": False, "attempts": attempt},
            )
        return SupportReply(
            "failed",
            "refund",
            "退款工具连续失败，状态已保留，可使用同一令牌重试。",
            diagnostics={"attempts": self.max_attempts},
        )
