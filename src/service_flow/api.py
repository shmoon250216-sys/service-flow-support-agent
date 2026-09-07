from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .agent import SupportAgent
from .routing import router_from_env
from .store import SupportStore
from .web import INDEX_HTML


class ChatRequest(BaseModel):
    request_id: str
    customer_id: str
    order_id: str
    message: str


class ConfirmRequest(BaseModel):
    token: str
    customer_id: str


def create_app(db_path: str | Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = SupportStore(db_path or os.getenv("SERVICE_FLOW_DB", "data/service-flow.db"))
        store.seed()
        app.state.store = store
        app.state.agent = SupportAgent(store, router=router_from_env())
        yield
        store.close()

    app = FastAPI(title="ServiceFlow 智能售后工单 Agent", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/health")
    def health() -> dict:
        agent = app.state.agent
        return {"status": "ok", "router": agent.router.name, "tools": len(agent.tools.schemas())}

    @app.post("/chat")
    def chat(body: ChatRequest):
        return app.state.agent.handle(body.request_id, body.customer_id, body.order_id, body.message)

    @app.post("/refund/confirm")
    def confirm(body: ConfirmRequest):
        return app.state.agent.confirm_refund(body.token, body.customer_id)

    @app.get("/api/orders/{customer_id}")
    def orders(customer_id: str) -> list[dict]:
        return app.state.store.orders_for(customer_id)

    @app.get("/api/tickets")
    def tickets(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
        return app.state.store.tickets(limit=limit)

    @app.get("/api/events/{request_id}")
    def events(request_id: str) -> list[dict]:
        return app.state.store.events(request_id)

    @app.get("/api/tools")
    def tools() -> list[dict]:
        return app.state.agent.tools.schemas()

    return app


app = create_app()
