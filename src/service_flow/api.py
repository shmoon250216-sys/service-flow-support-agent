from __future__ import annotations

import json
import os
import secrets
import asyncio
import logging
from .outbox import OutboxDispatcher
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .conversations import ConversationService, WorkflowError
from .routing import router_from_env
from .store import SupportStore


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewConversation(Body):
    order_id: str = Field(min_length=1, max_length=80)


class Message(Body):
    request_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class Confirmation(Body):
    token: str = Field(min_length=16, max_length=100)


class StaffAction(Body):
    action: Literal["reply", "resume", "resolve", "wait_customer", "transfer", "reopen"]
    message: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class Version(Body):
    expected_version: int | None = Field(default=None, ge=1)


class CaseAction(StaffAction):
    expected_version: int = Field(ge=1)
    target: str | None = None
    resolution_code: (
        Literal["solved", "repair", "policy_explained", "return_to_bot"] | None
    ) = None


class DemoLogin(Body):
    role: Literal["customer", "staff"]
    identity: str


def create_app(
    db_path: str | Path | None = None,
    *,
    demo_mode: bool | None = None,
    identities: dict | None = None,
    worker_enabled: bool | None = None,
):
    @asynccontextmanager
    async def lifespan(app):
        store = SupportStore(
            db_path or os.getenv("SERVICE_FLOW_DB", "data/service-flow.db")
        )
        store.seed()
        app.state.store = store
        app.state.service = ConversationService(store, router_from_env())
        app.state.demo = (
            (os.getenv("SERVICE_FLOW_DEMO_MODE", "true").lower() == "true")
            if demo_mode is None
            else demo_mode
        )
        app.state.identities = dict(
            identities
            if identities is not None
            else json.loads(os.getenv("SERVICE_FLOW_IDENTITIES", "{}"))
        )
        if not app.state.demo and not app.state.identities:
            store.close()
            raise RuntimeError(
                "关闭演示模式后必须配置 SERVICE_FLOW_IDENTITIES；建议部署时由身份网关签发凭据"
            )
        app.state.outbox = OutboxDispatcher(store)
        stop = asyncio.Event()

        async def maintenance():
            while not stop.is_set():
                try:

                    def tick():
                        app.state.service.cases.scan_sla()
                        app.state.outbox.drain()

                    await asyncio.to_thread(tick)
                except Exception:
                    logging.getLogger("service_flow.worker").exception(
                        "maintenance cycle failed"
                    )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass

        enabled = (db_path is None) if worker_enabled is None else worker_enabled
        worker = asyncio.create_task(maintenance()) if enabled else None
        try:
            yield
        finally:
            stop.set()
            if worker:
                await worker
            store.close()

    app = FastAPI(
        title="ServiceFlow 售后协作工作台", version="0.3.0", lifespan=lifespan
    )

    app.mount(
        "/assets",
        StaticFiles(directory=Path(__file__).with_name("static")),
        name="assets",
    )

    @app.exception_handler(WorkflowError)
    async def workflow_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(PermissionError)
    async def denied(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    def principal(authorization: str = Header(default="")):
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else ""
        )
        identity = app.state.identities.get(token)
        if not identity or identity.get("role") not in {"customer", "staff"}:
            raise HTTPException(401, "请先登录")
        return identity

    def customer(user=Depends(principal)):
        if user["role"] != "customer":
            raise HTTPException(403, "需要客户身份")
        return user["id"]

    def staff(user=Depends(principal)):
        if user["role"] != "staff":
            raise HTTPException(403, "需要客服身份")
        return user["id"]

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (
            Path(__file__)
            .with_name("static")
            .joinpath("index.html")
            .read_text(encoding="utf-8")
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "router": app.state.service.agent.router.name,
            "tools": len(app.state.service.agent.tools.schemas()),
            "demo_mode": app.state.demo,
            "version": "0.3.0",
        }

    @app.post("/api/demo/login")
    def login(body: DemoLogin, request: Request):
        if not app.state.demo:
            raise HTTPException(404, "演示登录已关闭")
        allowed = {
            "customer": {"C-001", "C-002", "C-003", "C-004"},
            "staff": {"S-001", "S-002"},
        }
        if body.identity not in allowed[body.role]:
            raise HTTPException(403, "演示身份不存在")
        token = secrets.token_urlsafe(32)
        app.state.identities[token] = {
            "role": body.role,
            "id": body.identity,
            "permissions": ["supervisor"]
            if body.role == "staff" and body.identity == "S-002"
            else [],
        }
        # Demo credentials only; restart revokes them. Never enable this endpoint on a public service.
        return {"token": token, "role": body.role, "id": body.identity}

    @app.get("/api/me")
    def me(user=Depends(principal)):
        return user

    @app.get("/api/orders")
    def orders(cid=Depends(customer)):
        return app.state.store.orders_for(cid)

    @app.get("/api/conversations")
    def conversations(cid=Depends(customer)):
        return app.state.service.list(cid)

    @app.post("/api/conversations")
    def create(body: NewConversation, cid=Depends(customer)):
        return app.state.service.create(cid, body.order_id)

    @app.get("/api/conversations/{conversation_id}")
    def get(conversation_id: str, user=Depends(principal)):
        return app.state.service.get(
            conversation_id, user["id"] if user["role"] == "customer" else None
        )

    @app.post("/api/conversations/{conversation_id}/messages")
    def send(conversation_id: str, body: Message, cid=Depends(customer)):
        return app.state.service.send(
            conversation_id, cid, body.request_id, body.message
        )

    @app.post("/api/conversations/{conversation_id}/refund/confirm")
    def confirm(conversation_id: str, body: Confirmation, cid=Depends(customer)):
        return app.state.service.confirm(conversation_id, cid, body.token)

    @app.get("/api/staff/queue")
    def queue(
        sid=Depends(staff),
        status: Literal[
            "active", "all", "open", "in_progress", "pending_customer", "resolved"
        ] = "active",
        queue: Literal["all", "technical", "billing", "safety"] = "all",
    ):
        return app.state.service.cases.search(status, queue)

    @app.post("/api/staff/conversations/{conversation_id}/claim")
    def claim(conversation_id: str, sid=Depends(staff)):
        return app.state.service.claim(conversation_id, sid)

    @app.get("/api/staff/overview")
    def overview(sid=Depends(staff)):
        return app.state.service.cases.overview()

    @app.get("/api/staff/conversations/{conversation_id}/audit")
    def audit(conversation_id: str, sid=Depends(staff)):
        app.state.service.row(conversation_id)
        return app.state.service.cases.audit(conversation_id)

    @app.post("/api/staff/conversations/{conversation_id}/case-action")
    def case_action(conversation_id: str, body: CaseAction, user=Depends(principal)):
        if user["role"] != "staff":
            raise HTTPException(403, "需要客服身份")
        if body.action in {"resolve", "resume"} and body.resolution_code is None:
            raise HTTPException(422, "请选择结案分类")
        if body.target and body.target not in {
            x["id"] for x in app.state.identities.values() if x["role"] == "staff"
        }:
            raise HTTPException(422, "目标客服未配置或尚未登录")
        return app.state.service.staff_action(
            conversation_id,
            user["id"],
            body.action,
            body.message,
            expected_version=body.expected_version,
            target=body.target,
            resolution_code=body.resolution_code,
            supervisor="supervisor" in user.get("permissions", []),
        )

    @app.get("/api/staff/notifications")
    def notifications(sid=Depends(staff)):
        with app.state.store.lock:
            return [
                dict(x)
                for x in app.state.store.connection.execute(
                    "SELECT event_id,aggregate_id,event_type,received_at FROM notification_inbox ORDER BY received_at DESC LIMIT 50"
                )
            ]

    @app.get("/api/staff/outbox")
    def outbox(sid=Depends(staff)):
        with app.state.store.lock:
            return [
                dict(x)
                for x in app.state.store.connection.execute(
                    "SELECT id,aggregate_id,event_type,status,attempts,last_error FROM outbox_events ORDER BY created_at DESC LIMIT 50"
                )
            ]

    @app.post("/api/staff/outbox/{event_id}/retry")
    def retry(event_id: str, user=Depends(principal)):
        if user["role"] != "staff" or "supervisor" not in user.get("permissions", []):
            raise HTTPException(403, "需要主管权限")
        try:
            app.state.outbox.retry_dead(event_id, user["id"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @app.get("/api/tools")
    def tools(user=Depends(principal)):
        return app.state.service.agent.tools.schemas()

    @app.get("/api/tickets")
    def tickets(sid=Depends(staff), limit: int = Query(50, ge=1, le=200)):
        return app.state.store.tickets(limit)

    @app.get("/api/events/{request_id}")
    def events(request_id: str, sid=Depends(staff)):
        return app.state.store.events(request_id)

    return app


app = create_app()
