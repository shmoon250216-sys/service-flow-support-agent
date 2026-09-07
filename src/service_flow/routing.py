from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol

ROUTES = {"knowledge", "refund", "ticket", "safety", "unknown"}


def rule_classify(message: str) -> str:
    if any(k in message for k in ("冒烟", "起火", "鼓包", "烫手", "发烫")):
        return "safety"
    if any(k in message for k in ("退款", "退货", "不要了", "退钱")):
        return "refund"
    if any(k in message for k in ("人工", "投诉", "工单", "寄修", "坏了")):
        return "ticket"
    if any(k in message for k in ("连接", "连不上", "搜不到", "离线", "断网", "隐私", "杂音", "噪声", "质保", "保修")):
        return "knowledge"
    return "unknown"


class Router(Protocol):
    name: str
    def route(self, message: str) -> str: ...


@dataclass(slots=True)
class RuleRouter:
    name: str = "rules"
    def route(self, message: str) -> str:
        return rule_classify(message)


@dataclass(slots=True)
class OpenAICompatibleRouter:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 15.0
    name: str = "openai-compatible"

    def route(self, message: str) -> str:
        prompt = (
            "将售后消息分类为 knowledge/refund/ticket/safety/unknown 之一，只输出 JSON："
            '{"route":"..."}。冒烟、起火、鼓包、严重过热必须为 safety。'
            f"\n消息：{message}"
        )
        body = json.dumps({"model": self.model, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url.rstrip("/") + "/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        route = json.loads(payload["choices"][0]["message"]["content"])["route"]
        return route if route in ROUTES else "unknown"


def router_from_env() -> Router:
    base_url = os.getenv("SERVICE_FLOW_LLM_BASE_URL", "").strip()
    api_key = os.getenv("SERVICE_FLOW_LLM_API_KEY", "").strip()
    model = os.getenv("SERVICE_FLOW_LLM_MODEL", "").strip()
    return OpenAICompatibleRouter(base_url, api_key, model) if base_url and api_key and model else RuleRouter()

