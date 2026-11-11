"""현장 운영 제어: 전체 정지·재개, 대피.

    GET  /api/v1/sites/{site_id}/ops
    POST /api/v1/sites/{site_id}/pause        {map_id?, reason}
    POST /api/v1/sites/{site_id}/resume
    POST /api/v1/sites/{site_id}/evacuation   {active, reason, mode: "pause"|"pullover"}

정지·대피는 **퇴장 증거가 아니다** — 교통 예약·승강기 임대는 건드리지 않는다.
운영 절차는 docs/operations.md.
"""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.api.events import actor_of, record
from fms_server.sites import SiteError, SiteNotFound

router = APIRouter(prefix="/api/v1/sites/{site_id}", tags=["ops"])


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    map_id: str | None = None
    reason: str = Field(default="", max_length=500)


class EvacuationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    reason: str = Field(default="", max_length=500)
    mode: Literal["pause", "pullover"] = "pause"


def _site(request: Request, site_id: str):
    try:
        return request.app.state.sites.get_site(site_id)
    except SiteNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except SiteError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/ops")
def get_ops(site_id: str, request: Request):
    return _site(request, site_id).ops


@router.post("/pause")
def pause_site(site_id: str, body: PauseIn, request: Request):
    registry = request.app.state.sites
    site = _site(request, site_id)
    if body.map_id is not None:
        try:
            registry.map_store(site_id).get_map(body.map_id)
        except Exception:
            raise HTTPException(404, f"site {site_id} 에 맵 {body.map_id} 가 없다") from None
    with request.app.state.job_coordinator.lock:
        if site.evacuating:
            raise HTTPException(409, "대피 중이다 — 대피 해제를 먼저 해라")
        scope = [body.map_id] if body.map_id else None
        sent = request.app.state.coordinator.pause_site(site_id, scope)

        def mutate(ops):
            previous = ops["map_ids"] if ops["paused"] else []
            # 범위 정지를 거듭하면 범위를 합친다. 전체 정지(None)가 한 번이라도 있으면 전체다.
            ops["map_ids"] = (None if scope is None or (ops["paused"] and previous is None)
                              else sorted(set(previous or []) | set(scope)))
            if not ops["paused"]:
                ops["since"] = time.time()
            ops["paused"], ops["reason"] = True, body.reason
            ops["robots"] = sorted(set(ops["robots"]) | set(sent))
        ops = registry.update_ops(site_id, mutate)
    record(request, "site.paused", site_id=site_id, severity="warning", map_id=body.map_id,
           reason=body.reason, robots=sent)
    return {"site_id": site_id, "ops": ops, "paused_robots": sent}


@router.post("/resume")
def resume_site(site_id: str, request: Request):
    registry = request.app.state.sites
    site = _site(request, site_id)
    with request.app.state.job_coordinator.lock:
        if site.evacuating:
            raise HTTPException(409, "대피 중에는 재개할 수 없다 — 대피 해제를 먼저 해라")
        if not site.ops["paused"]:
            raise HTTPException(409, "현장이 정지 상태가 아니다")
        robots = list(site.ops["robots"])
        # 상태를 먼저 풀어야 코디네이터가 재개한 로봇을 다시 세우지 않는다.
        ops = registry.update_ops(site_id, lambda o: o.update(
            paused=False, reason="", since=None, map_ids=None, robots=[]))
        sent = request.app.state.coordinator.resume_site(site_id, robots)
    record(request, "site.resumed", site_id=site_id, robots=sent)
    return {"site_id": site_id, "ops": ops, "resumed_robots": sent}


@router.post("/evacuation")
def evacuation(site_id: str, body: EvacuationIn, request: Request):
    registry = request.app.state.sites
    site = _site(request, site_id)
    coordinator = request.app.state.coordinator
    jobs = request.app.state.job_coordinator
    actor = actor_of(request)
    with jobs.lock:
        current = site.ops["evacuation"]
        if body.active:
            if current["active"]:
                raise HTTPException(409, "이미 대피 중이다")
            ops = registry.update_ops(site_id, lambda o: o["evacuation"].update(
                active=True, reason=body.reason, mode=body.mode, since=time.time(), canceled_jobs=[]))
            if body.mode == "pause":
                sent = coordinator.pause_site(site_id, None)
                ops = registry.update_ops(site_id, lambda o: o.update(
                    robots=sorted(set(o["robots"]) | set(sent))))
                result = {"paused_robots": sent}
            else:
                canceled = request.app.state.battery.start_pullover(site_id, actor=actor)
                ops = registry.update_ops(site_id, lambda o: o["evacuation"].update(canceled_jobs=canceled))
                result = {"canceled_jobs": canceled}
        else:
            if not current["active"]:
                raise HTTPException(409, "대피 상태가 아니다")
            mode, canceled = current["mode"], list(current["canceled_jobs"])
            robots = list(site.ops["robots"])
            still_paused = site.ops["paused"]

            def clear(o):
                o["evacuation"].update(active=False, reason="", since=None)
                if not still_paused:
                    o["robots"] = []
            ops = registry.update_ops(site_id, clear)
            result = {"canceled_jobs": canceled}   # 자동 재게시하지 않는다. 목록만 돌려준다
            if mode == "pause" and not still_paused:
                result["resumed_robots"] = coordinator.resume_site(site_id, robots)
            if mode == "pullover":
                request.app.state.battery.end_pullover(site_id)
    record(request, "site.evacuation", site_id=site_id, severity="error" if body.active else "info",
           active=body.active, mode=body.mode if body.active else mode, reason=body.reason)
    return {"site_id": site_id, "ops": ops, **result}
