from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field
from .store import SupportStore


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrderArgs(Arguments):
    customer_id: str = Field(min_length=1, max_length=80)
    order_id: str = Field(min_length=1, max_length=80)


class SearchArgs(Arguments):
    product: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=2000)


class TicketArgs(OrderArgs):
    request_id: str = Field(min_length=1, max_length=100)
    priority: str = Field(pattern="^(normal|urgent)$")
    reason: str = Field(min_length=1, max_length=2000)


class RefundArgs(Arguments):
    token: str = Field(min_length=16, max_length=100)
    order_id: str = Field(min_length=1, max_length=80)
    amount: float = Field(gt=0, le=1000000)


class TicketLookup(Arguments):
    ticket_id: str = Field(min_length=1, max_length=80)
    customer_id: str = Field(min_length=1, max_length=80)


class ToolOutput(BaseModel):
    status: str = Field(pattern="^(ok|error)$")


class TicketOutput(ToolOutput):
    ticket_id: str = Field(min_length=1)


class RefundOutput(ToolOutput):
    refund_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class OrderOutput(ToolOutput):
    order: dict


class SearchOutput(ToolOutput):
    articles: list[dict]


class TicketLookupOutput(ToolOutput):
    ticket: dict


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[Arguments]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    mutating: bool = False
    output: type[ToolOutput] = ToolOutput

    @property
    def schema(self):
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.arguments.model_json_schema(),
            "mutating": self.mutating,
            "outputSchema": self.output.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self.audit: Callable | None = None

    def register(self, spec):
        self._tools[spec.name] = spec

    def schemas(self):
        return [spec.schema for spec in self._tools.values()]

    def call(self, name, arguments):
        if name not in self._tools:
            raise ValueError(f"unknown tool: {name}")
        spec = self._tools[name]
        args = spec.arguments.model_validate(arguments).model_dump()
        start = time.monotonic()
        try:
            result = spec.handler(args)
            spec.output.model_validate(result)
            if result["status"] == "error":
                raise RuntimeError(result.get("message", "工具执行失败"))
            if self.audit:
                self.audit(name, "ok", round((time.monotonic() - start) * 1000))
            return result
        except Exception:
            if self.audit:
                self.audit(name, "error", round((time.monotonic() - start) * 1000))
            raise


def build_tools(store: SupportStore):
    registry = ToolRegistry()

    def lookup(args):
        row = store.order(args["order_id"])
        if not row or row["customer_id"] != args["customer_id"]:
            raise PermissionError("订单不存在或无权访问")
        return {"status": "ok", "order": dict(row)}

    def ticket(args):
        lookup(args)
        return {"status": "ok", "ticket_id": store.create_ticket(**args)}

    def search(args):
        from .agent import tokens

        terms = tokens(args["query"])
        hits = []
        for row in store.articles(args["product"]):
            score = len(terms & tokens(row["title"] + row["content"])) / max(
                1, len(terms)
            )
            if score:
                hits.append({**dict(row), "score": round(score, 4)})
        hits.sort(key=lambda x: (-x["score"], x["article_id"]))
        return {"status": "ok", "articles": hits[:3]}

    def lookup_ticket(args):
        with store.lock:
            row = store.connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=? AND customer_id=?",
                (args["ticket_id"], args["customer_id"]),
            ).fetchone()
        if not row:
            raise PermissionError("工单不存在或无权访问")
        return {"status": "ok", "ticket": dict(row)}

    registry.register(
        ToolSpec(
            "get_order", "查询本人订单及退款状态", OrderArgs, lookup, output=OrderOutput
        )
    )
    registry.register(
        ToolSpec(
            "search_knowledge",
            "检索指定产品的售后手册并返回来源",
            SearchArgs,
            search,
            output=SearchOutput,
        )
    )
    registry.register(
        ToolSpec(
            "get_ticket",
            "查询本人售后工单",
            TicketLookup,
            lookup_ticket,
            output=TicketLookupOutput,
        )
    )
    registry.register(
        ToolSpec(
            "create_ticket",
            "创建需人工接管的售后工单",
            TicketArgs,
            ticket,
            True,
            TicketOutput,
        )
    )
    registry.register(
        ToolSpec(
            "submit_refund",
            "仅执行已授权的退款提案；本地账本模拟，不调用支付平台",
            RefundArgs,
            store.execute_refund,
            True,
            RefundOutput,
        )
    )
    return registry
