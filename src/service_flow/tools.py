from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from .store import SupportStore


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    required_fields: tuple[str, ...]
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": {"type": "object", "required": list(self.required_fields)}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema for spec in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        spec = self._tools[name]
        missing = [field for field in spec.required_fields if arguments.get(field) in (None, "")]
        if missing:
            raise ValueError(f"missing tool fields: {', '.join(missing)}")
        result = spec.handler(dict(arguments))
        if not isinstance(result, dict) or result.get("status") not in {"ok", "error"}:
            raise ValueError(f"invalid output schema from {name}")
        return result


def build_tools(store: SupportStore) -> ToolRegistry:
    registry = ToolRegistry()

    def create_ticket(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id = store.create_ticket(args["request_id"], args["customer_id"], args.get("order_id"), args["priority"], args["reason"])
        return {"status": "ok", "ticket_id": ticket_id}

    def submit_refund(args: dict[str, Any]) -> dict[str, Any]:
        refund_id = "R-" + hashlib.sha1(args["token"].encode()).hexdigest()[:8].upper()
        return {"status": "ok", "refund_id": refund_id, "message": f"退款申请已提交，编号 {refund_id}。"}

    registry.register(ToolSpec("create_ticket", "Create a customer support ticket", ("request_id", "customer_id", "priority", "reason"), create_ticket))
    registry.register(ToolSpec("submit_refund", "Submit a confirmed refund", ("token", "order_id", "amount"), submit_refund))
    return registry

