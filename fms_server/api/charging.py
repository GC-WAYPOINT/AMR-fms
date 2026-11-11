"""자동 충전·대기 위치 정책과 예약 실행.

    GET/PUT /api/v1/sites/{site_id}/charging
    GET     /api/v1/sites/{site_id}/charging/spots       충전소·대기 위치와 점유 상태
    GET/POST            /api/v1/sites/{site_id}/schedules
    PUT/DELETE          /api/v1/sites/{site_id}/schedules/{id}

정책 설명은 docs/charging.md.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server import charger_pool
from fms_server.api.events import record
from fms_server.api.jobs import _check_site
from fms_server.battery import PolicyError
from fms_server.task import JobError, JobNotFound

router = APIRouter(prefix="/api/v1/sites/{site_id}", tags=["charging"])


@router.get("/charging")
def get_policy(site_id: str, request: Request):
    _check_site(request, site_id)
    return request.app.state.battery.policy(site_id)


@router.put("/charging")
def set_policy(site_id: str, body: dict, request: Request):
    _check_site(request, site_id)
    try:
        policy = request.app.state.battery.set_policy(site_id, body)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from None
    record(request, "charge.policy", site_id=site_id, enabled=policy["enabled"],
           staging=policy["staging"]["enabled"])
    return policy


@router.get("/charging/spots")
def spots(site_id: str, request: Request):
    _check_site(request, site_id)
    state = request.app.state
    with state.job_coordinator.lock:
        busy = charger_pool.targeted(state.jobs, site_id, None, kinds=("docking", "parking"))
        def view(spot):
            return dict(spot, occupied=charger_pool.occupied(state.fleet, state.sites, spot, None),
                        targeted=spot["name"] in busy)
        return {"chargers": [view(c) for c in charger_pool.chargers(state.sites, site_id)],
                "parkings": [view(p) for p in charger_pool.parkings(state.sites, site_id)]}


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=128)
    preset_id: str
    days: list[int] | None = None           # 0(월)~6(일). 없으면 매일
    time: str | None = None                 # "HH:MM" (서버 로컬 시간)
    interval_s: float | None = None         # 또는 반복 간격 (초)
    robot_group: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    enabled: bool = True


def _call(request: Request, site_id: str, fn):
    _check_site(request, site_id)
    try:
        return fn(request.app.state.schedules)
    except JobNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except JobError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/schedules")
def list_schedules(site_id: str, request: Request):
    return _call(request, site_id, lambda store: store.list(site_id))


@router.post("/schedules")
def create_schedule(site_id: str, body: ScheduleIn, request: Request):
    return _call(request, site_id, lambda store: store.save(site_id, body.model_dump(), request.app.state.presets))


@router.put("/schedules/{schedule_id}")
def update_schedule(site_id: str, schedule_id: str, body: ScheduleIn, request: Request):
    return _call(request, site_id, lambda store: store.save(
        site_id, body.model_dump(), request.app.state.presets, schedule_id))


@router.delete("/schedules/{schedule_id}")
def delete_schedule(site_id: str, schedule_id: str, request: Request):
    def remove(store):
        store.delete(site_id, schedule_id)
        return {"deleted": schedule_id}
    return _call(request, site_id, remove)
