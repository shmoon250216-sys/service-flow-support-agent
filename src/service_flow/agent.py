from __future__ import annotations

import hashlib
import re
from typing import Callable

from .store import SupportStore
from .types import SupportReply


SYNONYMS = {
    "连不上": "连接 配对 蓝牙", "搜不到": "连接 配对 蓝牙", "断网": "离线 路由器",
    "没画面": "离线 摄像头", "退货": "退款 七天", "不要了": "退款 七天",
    "发烫": "过热 安全", "起火": "冒烟 安全", "噪声": "杂音 单耳",
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
    def __init__(self, store: SupportStore, *, max_attempts: int = 2) -> None:
        self.store = store
        self.max_attempts = max(1, max_attempts)

    @staticmethod
    def classify(message: str) -> str:
        if any(k in message for k in ("冒烟", "起火", "鼓包", "烫手", "发烫")):
            return "safety"
        if any(k in message for k in ("退款", "退货", "不要了", "退钱")):
            return "refund"
        if any(k in message for k in ("人工", "投诉", "工单", "寄修", "坏了")):
            return "ticket"
        if any(k in message for k in ("连接", "连不上", "搜不到", "离线", "断网", "隐私", "杂音", "噪声", "质保", "保修")):
            return "knowledge"
        return "unknown"

    def retrieve(self, product: str, query: str) -> list[dict]:
        query_tokens = tokens(query)
        scored = []
        for row in self.store.articles(product):
            doc_tokens = tokens(f"{row['title']} {row['content']}")
            overlap = len(query_tokens & doc_tokens)
            score = overlap / max(1, len(query_tokens))
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["article_id"]))
        return [
            {"article_id": row["article_id"], "title": row["title"], "source": row["source"], "content": row["content"], "score": round(score, 4)}
            for score, row in scored[:3]
        ]

    def handle(self, request_id: str, customer_id: str, order_id: str, message: str) -> SupportReply:
        self.store.append_event(request_id, "received", {"order_id": order_id, "message": message})
        order = self.store.order(order_id)
        if not order or order["customer_id"] != customer_id:
            self.store.append_event(request_id, "ownership_denied", {})
            return SupportReply("denied", "security", "订单不存在或不属于当前用户，已停止调用售后工具。")

        route = self.classify(message)
        self.store.append_event(request_id, "routed", {"route": route})
        if route in {"safety", "ticket", "unknown"}:
            priority = "urgent" if route == "safety" else "normal"
            reason = "产品安全风险" if route == "safety" else ("用户要求人工处理" if route == "ticket" else "知识证据不足")
            ticket_id = self.store.create_ticket(request_id, customer_id, order_id, priority, reason)
            self.store.append_event(request_id, "human_handoff", {"ticket_id": ticket_id, "priority": priority})
            message_out = "请立即停止使用并远离可燃物，已转人工安全专席。" if route == "safety" else "当前证据不足或需要人工处理，已创建售后工单。"
            return SupportReply("handoff", route, message_out, ticket_id=ticket_id)

        articles = self.retrieve(order["product"], message)
        if route == "knowledge":
            if not articles or articles[0]["score"] < 0.12:
                ticket_id = self.store.create_ticket(request_id, customer_id, order_id, "normal", "知识证据不足")
                return SupportReply("handoff", route, "未检索到足够证据，已转人工。", ticket_id=ticket_id)
            top = articles[0]
            self.store.append_event(request_id, "answered", {"article_id": top["article_id"], "score": top["score"]})
            return SupportReply("answered", route, top["content"], citations=[{k: top[k] for k in ("article_id", "title", "source", "score")}])

        policy = next((item for item in articles if item["article_id"] == "KB-REFUND"), None)
        if order["delivered_days"] > 7:
            ticket_id = self.store.create_ticket(request_id, customer_id, order_id, "normal", "超出七天退款窗口")
            return SupportReply("handoff", "refund", "已超过七天窗口，转人工核验质量与保修条件。", citations=[] if not policy else [policy], ticket_id=ticket_id)
        token = hashlib.sha256(f"{request_id}:{customer_id}:{order_id}:refund".encode()).hexdigest()[:20]
        payload = {"order_id": order_id, "amount": order["paid_amount"], "reason": message}
        self.store.save_pending(token, request_id, customer_id, order_id, payload)
        self.store.append_event(request_id, "confirmation_required", payload)
        return SupportReply(
            "confirmation_required", "refund",
            f"拟为订单 {order_id} 申请退款 ¥{order['paid_amount']:.2f}。确认后才会提交，请回复确认令牌。",
            citations=[] if not policy else [{k: policy[k] for k in ("article_id", "title", "source", "score")}],
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
        key = hashlib.sha256(f"refund:{token}:{pending['order_id']}".encode()).hexdigest()
        cached = self.store.load_effect(key)
        if cached:
            return SupportReply("completed", "refund", cached["message"], diagnostics={"idempotency_hit": True, "attempts": 0})
        for attempt in range(1, self.max_attempts + 1):
            self.store.append_event(pending["request_id"], "refund_attempted", {"attempt": attempt})
            if fail_policy and fail_policy(attempt):
                self.store.append_event(pending["request_id"], "refund_failed", {"attempt": attempt})
                continue
            refund_id = "R-" + hashlib.sha1(token.encode()).hexdigest()[:8].upper()
            output = {"refund_id": refund_id, "message": f"退款申请已提交，编号 {refund_id}。"}
            self.store.save_effect(key, output)
            self.store.mark_confirmed(token)
            self.store.append_event(pending["request_id"], "refund_succeeded", {"attempt": attempt, "refund_id": refund_id})
            return SupportReply("completed", "refund", output["message"], diagnostics={"idempotency_hit": False, "attempts": attempt})
        return SupportReply("failed", "refund", "退款工具连续失败，状态已保留，可使用同一令牌重试。", diagnostics={"attempts": self.max_attempts})
