from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from .agent import SupportAgent
from .store import SupportStore


class ChatRequest(BaseModel):
    request_id: str
    customer_id: str
    order_id: str
    message: str


class ConfirmRequest(BaseModel):
    token: str
    customer_id: str


def create_app(db_path: str | Path = "data/service-flow.db") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = SupportStore(db_path)
        store.seed()
        app.state.store = store
        app.state.agent = SupportAgent(store)
        yield
        store.close()

    app = FastAPI(title="ServiceFlow 智能售后工单 Agent", lifespan=lifespan)

    @app.post("/chat")
    def chat(body: ChatRequest):
        return app.state.agent.handle(body.request_id, body.customer_id, body.order_id, body.message)

    @app.post("/refund/confirm")
    def confirm(body: ConfirmRequest):
        return app.state.agent.confirm_refund(body.token, body.customer_id)

    return app


app = create_app()

