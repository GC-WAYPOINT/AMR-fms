"""로봇 레지스트리 REST + WebSocket 라우터.

    GET  /api/v1/robots            전체 목록 + 요약
    GET  /api/v1/robots/{id}       한 대 상세
    POST /api/v1/robots/{id}/init-position   위치 힌트 (INIT_POSITION 즉시명령 발행)
    POST /api/v1/robots/{id}/pause           원격 일시정지 (PAUSE)
    POST /api/v1/robots/{id}/resume          재개 (RESUME). FMS 가 건 일시정지만 풀 수 있다 (아니면 409)
    POST /api/v1/robots/{id}/reset-error     오류 해제 (RESET_ERROR)
    WS   /api/v1/robots/stream     스냅샷 1회 + 이후 변경 이벤트 스트림

오더 발행은 job API(api/jobs.py)가 한다.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.schemas import InstantActionType, PauseSource
from fms_server.mqtt_client import send_instant

from fms_server.api.views import FleetView, RobotView, build_robot_view

log = logging.getLogger("fms.api.robots")

router = APIRouter(prefix="/api/v1", tags=["robots"])


@router.get("/robots", response_model=FleetView)
def list_robots(request: Request) -> FleetView:
    return FleetView.from_fleet(request.app.state.fleet, request.app.state.sites)


@router.get("/robots/{robot_id}", response_model=RobotView)
def get_robot(robot_id: str, request: Request):
    fleet = request.app.state.fleet
    record = fleet.get(robot_id)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": f"로봇 {robot_id} 를 모른다"})
    return build_robot_view(record, fleet, request.app.state.sites)


class InitPositionRequest(BaseModel):
    x: float
    y: float
    theta: float = Field(default=0.0, description="rad")


@router.post("/robots/{robot_id}/init-position")
def init_position(robot_id: str, body: InitPositionRequest, request: Request):
    """
    위치 힌트를 보낸다. 로봇은 그 근처에서 스캔 매칭으로 정밀 위치를 잡고 State.localization 으로
    결과를 알린다 (이 응답은 발행 여부뿐이다). 힌트는 로봇이 지금 보고한 맵(map_id) 좌표로 본다 —
    로봇이 맵을 갈아타는 중이면 로봇이 이 힌트를 무시한다.
    """
    record = request.app.state.fleet.get(robot_id)
    if record is None or record.state is None:
        return JSONResponse(status_code=404, content={"detail": f"로봇 {robot_id} 의 state 를 아직 못 받았다"})
    params: dict[str, float | str | bool] = {"x": body.x, "y": body.y, "theta": body.theta}
    if record.state.map_id:
        params["map_id"] = record.state.map_id
    send_instant(request.app.state.mqtt, robot_id, InstantActionType.INIT_POSITION, params)
    log.info("위치 힌트 -> %s (%.2f, %.2f, %.2f)", robot_id, body.x, body.y, body.theta)
    return {"robot_id": robot_id, "sent": True, "params": params}


def _command(request: Request, robot_id: str, action_type: InstantActionType, check=None):
    record = request.app.state.fleet.get(robot_id)
    if record is None or record.state is None:
        return JSONResponse(status_code=404, content={"detail": f"로봇 {robot_id} 의 state 를 아직 못 받았다"})
    if check is not None:
        problem = check(record.state)
        if problem:
            return JSONResponse(status_code=409, content={"detail": problem})
    send_instant(request.app.state.mqtt, robot_id, action_type)
    from fms_server.api.events import record as record_event
    assignment = request.app.state.sites.assignment_for(robot_id)
    record_event(request, "robot.command", robot_id=robot_id, command=action_type.value,
                 site_id=assignment.site_id if assignment else None)
    log.info("%s -> %s", action_type.value, robot_id)
    return {"robot_id": robot_id, "sent": True, "action": action_type.value}


def _site_hold(request: Request, robot_id: str):
    """이 로봇이 현장 정지·대피(pause) 범위에 있으면 그 이유."""
    registry = request.app.state.sites
    assignment = registry.assignment_for(robot_id)
    if assignment is None:
        return None
    site = registry.get_site(assignment.site_id)
    if site.paused_for(assignment.map_id):
        return "대피 중" if site.evacuating else "현장 정지 중"
    return None


@router.post("/robots/{robot_id}/pause")
def pause_robot(robot_id: str, request: Request):
    return _command(request, robot_id, InstantActionType.PAUSE)


@router.post("/robots/{robot_id}/resume")
def resume_robot(robot_id: str, request: Request):
    hold = _site_hold(request, robot_id)

    def check(state):
        if not state.paused:
            return "로봇이 일시정지 상태가 아니다"
        if state.pause_source != PauseSource.FMS:
            return "로봇에서 건 일시정지(LOCAL)는 FMS 가 풀 수 없다 — 로봇에서 해제해라"
        if hold:
            return f"{hold} — 현장 재개(또는 대피 해제) 후에 풀린다"
        return None
    return _command(request, robot_id, InstantActionType.RESUME, check)


@router.post("/robots/{robot_id}/reset-error")
def reset_error(robot_id: str, request: Request):
    return _command(request, robot_id, InstantActionType.RESET_ERROR)


@router.websocket("/robots/stream")
async def robots_stream(ws: WebSocket) -> None:
    broadcaster = ws.app.state.broadcaster
    await broadcaster.register(ws)
    try:
        while True:
            # 클라이언트가 보내는 건 무시한다 (순수 관측 스트림). 연결 유지용.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unregister(ws)
