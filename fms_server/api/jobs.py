"""job (작업) REST 라우터.

job = 이동/대기/도킹/주차 명령의 순서 묶음. site 스코프.
프리셋 게시·자동 배정은 presets API를 사용한다. 이 API의 생성은 수동 pending을 유지한다.
규약은 `../task.py` 참고.

    POST   /api/v1/sites/{site_id}/jobs                {commands:[{type:"move",target:"<loc>"}], robot_id?}
    GET    /api/v1/sites/{site_id}/jobs
    GET    /api/v1/sites/{site_id}/jobs/{job_id}
    POST   /api/v1/sites/{site_id}/jobs/{job_id}/dispatch   {robot_id?}
    POST   /api/v1/sites/{site_id}/jobs/{job_id}/cancel
    DELETE /api/v1/sites/{site_id}/jobs/{job_id}            (working 이 아닐 때만)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.sites import SiteError, SiteNotFound
from fms_server.route import stage_label
from fms_server.task import Job, JobError, JobNotFound

log = logging.getLogger("fms.api.jobs")

router = APIRouter(prefix="/api/v1/sites/{site_id}/jobs", tags=["jobs"])


# =============================================================================
# 모델
# =============================================================================

class CommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: str = "move"
    target: str | None = None
    duration: float | None = None
    params: dict | None = None


class CreateJobRequest(BaseModel):
    commands: list[CommandIn]
    robot_id: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    not_before: str | None = None
    deadline: str | None = None
    robot_group: str | None = None
    auto_assign: bool = False          # True 면 게시판에 올려 FMS 가 배정한다 (외부 주문)
    external_id: str | None = Field(default=None, max_length=128)   # 멱등 키: 같은 값 재요청은 기존 작업 반환


class PatchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int = Field(ge=-100, le=100)


class DispatchRequest(BaseModel):
    robot_id: str | None = None


class JobOut(BaseModel):
    job_id: str
    site_id: str
    status: str
    robot_id: str | None
    commands: list[dict]
    auto_assign: bool = False
    preset_name: str | None = None
    external_owner: str | None = None
    order_id: str | None
    order_update_id: int
    done_commands: int
    total_commands: int
    traffic_waiting: str | None = None
    stage: int = 0              # 다층 job: 지금 단계 (0부터)
    total_stages: int = 0       # 0 이면 단일 Order job
    stage_label: str | None = None
    error: str | None
    created_at: str
    updated_at: str
    dispatched_at: str | None
    priority: int = 0
    not_before: str | None = None
    deadline: str | None = None
    deadline_missed: bool = False
    robot_group: str | None = None
    system: str | None = None
    retry_of: str | None = None
    external_id: str | None = None

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls(
            job_id=job.job_id,
            site_id=job.site_id,
            status=job.status.value,
            robot_id=job.robot_id,
            commands=[c.to_dict() for c in job.commands],
            auto_assign=job.auto_assign, preset_name=job.preset_name,
            external_owner=job.external_owner,
            order_id=job.order_id,
            order_update_id=job.order_update_id,
            done_commands=job.done_commands,
            total_commands=len(job.commands),
            traffic_waiting=job.traffic.get('waiting'),
            stage=job.stage, total_stages=len(job.plan),
            stage_label=stage_label(job.plan[job.stage]) if job.plan and job.stage < len(job.plan) else None,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            dispatched_at=job.dispatched_at,
            priority=job.priority, not_before=job.not_before, deadline=job.deadline,
            deadline_missed=job.deadline_missed, robot_group=job.robot_group, system=job.system,
            retry_of=job.retry_of, external_id=job.external_id,
        )


# =============================================================================
# 헬퍼
# =============================================================================

def _store(request: Request):
    return request.app.state.jobs


def _coordinator(request: Request):
    return request.app.state.job_coordinator


def _check_site(request: Request, site_id: str) -> None:
    try:
        request.app.state.sites.get_site(site_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _job_in_site(request: Request, site_id: str, job_id: str) -> Job:
    try:
        job = _store(request).get(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if job.site_id != site_id:
        raise HTTPException(status_code=404, detail=f"job {job_id} 는 site {site_id} 에 없다")
    return job


# =============================================================================
# 라우트
# =============================================================================

@router.post("", response_model=JobOut)
def create_job(site_id: str, body: CreateJobRequest, request: Request) -> JobOut:
    _check_site(request, site_id)
    store = _store(request)
    try:
        with _coordinator(request).lock:
            if body.external_id:
                existing = store.by_external_id(site_id, body.external_id)
                if existing is not None:
                    return JobOut.of(existing)   # 멱등: 같은 외부 주문은 한 번만 만든다
            commands = [c.model_dump(exclude_none=True) for c in body.commands]
            resolved = None
            if body.auto_assign:
                # 게시판 작업은 게시 시점의 좌표 스냅샷을 가진다 (프리셋 게시와 같다).
                from fms_server.task import _parse_command, resolve_steps
                resolved = resolve_steps([_parse_command(c) for c in commands], site_id, request.app.state.sites)
            job = store.create(
                site_id, commands, body.robot_id, auto_assign=body.auto_assign, resolved_steps=resolved,
                priority=body.priority, not_before=body.not_before, deadline=body.deadline,
                robot_group=body.robot_group, external_id=body.external_id,
            )
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.patch("/{job_id}", response_model=JobOut)
def patch_job(site_id: str, job_id: str, body: PatchJobRequest, request: Request) -> JobOut:
    """우선순위 변경 (pending 만)."""
    with _coordinator(request).lock:
        job = _job_in_site(request, site_id, job_id)
        if job.status.value != "pending":
            raise HTTPException(status_code=409, detail="pending 작업만 우선순위를 바꿀 수 있다")
        previous, job.priority = job.priority, body.priority
        job.touch()
        _store(request).save(job)
    from fms_server.api.events import record
    record(request, "job.priority", site_id=site_id, job_id=job_id, priority=body.priority, previous=previous)
    return JobOut.of(job)


@router.post("/{job_id}/retry", response_model=JobOut)
def retry_job(site_id: str, job_id: str, request: Request) -> JobOut:
    """종료된 작업의 명령을 복사해 새 pending 작업을 만든다. 좌표는 지금 목적지로 다시 잡는다."""
    from fms_server.task import _parse_command, resolve_steps
    with _coordinator(request).lock:
        job = _job_in_site(request, site_id, job_id)
        if job.external_owner:
            raise HTTPException(status_code=409, detail="배송 하위 작업은 보관소 복귀 절차로 처리한다")
        if job.system:
            raise HTTPException(status_code=409, detail="시스템 작업은 재시도하지 않는다 (조건이 되면 다시 만들어진다)")
        if not job.status.terminal:
            raise HTTPException(status_code=409, detail=f"종료된 작업만 재시도할 수 있다 ({job.status.value})")
        commands = [c.to_dict() for c in job.commands]
        try:
            resolved = resolve_steps([_parse_command(c) for c in commands], site_id, request.app.state.sites)
            new = _store(request).create(
                site_id, commands, None if job.auto_assign else job.robot_id,
                auto_assign=job.auto_assign, preset_name=job.preset_name, resolved_steps=resolved,
                priority=job.priority, robot_group=job.robot_group, retry_of=job.job_id)
        except JobError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    from fms_server.api.events import record
    record(request, "job.retry", site_id=site_id, job_id=new.job_id, retry_of=job.job_id)
    return JobOut.of(new)


@router.get("", response_model=list[JobOut])
def list_jobs(site_id: str, request: Request) -> list[JobOut]:
    _check_site(request, site_id)
    return [JobOut.of(j) for j in _store(request).list(site_id)]


@router.get("/{job_id}", response_model=JobOut)
def get_job(site_id: str, job_id: str, request: Request) -> JobOut:
    return JobOut.of(_job_in_site(request, site_id, job_id))


@router.post("/{job_id}/dispatch", response_model=JobOut)
def dispatch_job(site_id: str, job_id: str, body: DispatchRequest, request: Request) -> JobOut:
    job = _job_in_site(request, site_id, job_id)
    if job.external_owner:
        raise HTTPException(status_code=409, detail="배송 하위 작업은 보관소 연동이 배정한다")
    blocked = request.app.state.sites.get_site(site_id).allocation_blocked
    if blocked:
        raise HTTPException(status_code=409, detail=f"{blocked} — 작업을 배정할 수 없다")
    try:
        job = _coordinator(request).dispatch(job_id, body.robot_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(site_id: str, job_id: str, request: Request) -> JobOut:
    _job_in_site(request, site_id, job_id)
    try:
        from fms_server.api.events import actor_of
        job = _coordinator(request).cancel(job_id, actor=actor_of(request))
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.delete("/{job_id}")
def delete_job(site_id: str, job_id: str, request: Request):
    job = _job_in_site(request, site_id, job_id)
    if job.external_owner:
        raise HTTPException(status_code=409, detail="배송 이력은 삭제할 수 없다")
    try:
        _coordinator(request).delete(job_id)
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"deleted": job_id, "site_id": site_id}
