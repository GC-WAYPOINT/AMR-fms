"""관측·외부 연동: KPI, 알림, 실시간 이벤트 스트림, webhook.

    GET  /api/v1/sites/{site_id}/kpi?from=&to=&bucket=hour|day
    GET  /api/v1/alerts?site=&open=1
    POST /api/v1/alerts/{id}/ack
    WS   /api/v1/events/stream           커밋된 이벤트를 그대로 흘린다 (?site= 로 거를 수 있다)
    GET/POST          /api/v1/webhooks
    PUT/DELETE        /api/v1/webhooks/{id}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketState

from fms_server import kpi
from fms_server.api.events import actor_of, record
from fms_server.api.jobs import _check_site
from fms_server.webhooks import WebhookError, WebhookNotFound

log = logging.getLogger("fms.api.observe")
router = APIRouter(prefix="/api/v1", tags=["observe"])


@router.get("/sites/{site_id}/kpi")
def get_kpi(site_id: str, request: Request, bucket: str = "hour",
            start: Optional[float] = Query(default=None, alias="from"),
            end: Optional[float] = Query(default=None, alias="to")):
    """기본 구간: hour 는 최근 24시간, day 는 최근 14일."""
    _check_site(request, site_id)
    end = time.time() if end is None else end
    if start is None:
        start = end - (24 * 3600 if bucket == "hour" else 14 * 86400)
    try:
        return kpi.compute(request.app.state.events, site_id, start, end, bucket)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/alerts")
def list_alerts(request: Request, site: Optional[str] = None, open: bool = True,
                limit: int = Query(default=200, ge=1, le=2000)):
    rows = request.app.state.alerts.list(site=site, open_only=open, limit=limit)
    return {"alerts": rows, "count": len(rows)}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, request: Request):
    alert = request.app.state.alerts.ack(alert_id, actor_of(request))
    if alert is None:
        raise HTTPException(404, f"알림 {alert_id} 없음")
    return alert


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=128)
    url: str
    kinds: list[str] = []          # 비면 job.·alert.
    secret: Optional[str] = None   # 수정할 때 안 보내면 기존 값을 유지한다
    enabled: bool = True


def _hooks(request: Request, fn):
    try:
        return fn(request.app.state.webhooks)
    except WebhookNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except WebhookError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/webhooks")
def list_webhooks(request: Request):
    manager = request.app.state.webhooks
    return {"webhooks": manager.list(), "pending": manager.pending()}


@router.post("/webhooks")
def create_webhook(body: WebhookIn, request: Request):
    hook = _hooks(request, lambda m: m.save(body.model_dump()))
    record(request, "webhook.saved", webhook_id=hook["id"], url=hook["url"])
    return hook


@router.put("/webhooks/{hook_id}")
def update_webhook(hook_id: str, body: WebhookIn, request: Request):
    hook = _hooks(request, lambda m: m.save(body.model_dump(), hook_id))
    record(request, "webhook.saved", webhook_id=hook["id"], url=hook["url"])
    return hook


@router.delete("/webhooks/{hook_id}")
def delete_webhook(hook_id: str, request: Request):
    _hooks(request, lambda m: m.delete(hook_id))
    record(request, "webhook.deleted", webhook_id=hook_id)
    return {"deleted": hook_id}


class EventStream:
    """커밋된 이벤트를 WebSocket 으로 흘린다. `api/broadcaster.py` 와 같은 방식 (워커 스레드 -> 루프)."""

    def __init__(self, events) -> None:
        self._clients: dict[WebSocket, Optional[str]] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        events.add_listener(self._on_event)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._pump())

    async def stop(self) -> None:
        loop, self._loop = self._loop, None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()

    def _on_event(self, event: dict) -> None:
        """이벤트 로그 워커 스레드. 루프로 넘기기만 한다."""
        loop = self._loop
        if loop is None or event["kind"] == "robot.usage":
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            pass

    def _enqueue(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def _pump(self) -> None:
        while True:
            event = await self._queue.get()
            for ws, site in list(self._clients.items()):
                if site and event["site_id"] not in (site, None):
                    continue
                if ws.application_state != WebSocketState.CONNECTED:
                    self._clients.pop(ws, None)
                    continue
                try:
                    await ws.send_json({"type": "event", "event": event})
                except Exception:
                    self._clients.pop(ws, None)

    async def register(self, ws: WebSocket, site: Optional[str]) -> None:
        await ws.accept()
        self._clients[ws] = site

    def unregister(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)


@router.websocket("/events/stream")
async def events_stream(ws: WebSocket, site: Optional[str] = None) -> None:
    stream = ws.app.state.event_stream
    await stream.register(ws, site)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        stream.unregister(ws)
