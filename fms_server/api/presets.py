"""프리셋 CRUD 및 게시판 등록/로봇 지정 실행."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.api.jobs import CommandIn, JobOut, _check_site
from fms_server.presets import publish
from fms_server.task import JobError, JobNotFound

router = APIRouter(prefix='/api/v1/sites/{site_id}/presets', tags=['presets'])


class PresetIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=128)
    commands: list[CommandIn] = Field(min_length=1)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    robot_id: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)   # 클수록 먼저 배정
    robot_group: str | None = None                      # 이 그룹 로봇에만 배정
    not_before: str | None = None                       # 이 시각(ISO) 전에는 배정하지 않는다
    deadline: str | None = None                         # 마감(ISO). 넘기면 이벤트만 남긴다


def _call(request,site,fn):
    _check_site(request,site)
    try:
        return fn(request.app.state.presets)
    except JobNotFound as exc:
        raise HTTPException(404,str(exc)) from None
    except JobError as exc:
        raise HTTPException(422,str(exc)) from None


@router.get('')
def list_presets(site_id: str, request: Request):
    return _call(request,site_id,lambda store: store.list(site_id))


@router.post('')
def create_preset(site_id: str, body: PresetIn, request: Request):
    return _call(request,site_id,lambda store: store.save(site_id,body.name,
        [c.model_dump(exclude_none=True) for c in body.commands],request.app.state.sites))


@router.put('/{preset_id}')
def update_preset(site_id: str, preset_id: str, body: PresetIn, request: Request):
    return _call(request,site_id,lambda store: store.save(site_id,body.name,
        [c.model_dump(exclude_none=True) for c in body.commands],request.app.state.sites,preset_id))


@router.delete('/{preset_id}')
def delete_preset(site_id: str, preset_id: str, request: Request):
    def remove(store):
        store.delete(site_id,preset_id)
        return {'deleted':preset_id}
    return _call(request,site_id,remove)


@router.post('/{preset_id}/publish', response_model=JobOut)
def publish_preset(site_id: str, preset_id: str, body: PublishIn, request: Request):
    state = request.app.state
    blocked = state.sites.get_site(site_id).allocation_blocked if body.robot_id else None
    if blocked:
        raise HTTPException(409, f'{blocked} — 작업을 배정할 수 없다')
    return _call(request, site_id, lambda store: JobOut.of(publish(
        store, site_id, preset_id, registry=state.sites, jobs=state.jobs, coordinator=state.job_coordinator,
        robot_id=body.robot_id, priority=body.priority, robot_group=body.robot_group,
        not_before=body.not_before, deadline=body.deadline)))
