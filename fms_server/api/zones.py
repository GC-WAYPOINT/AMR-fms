"""맵 구역 편집. 저장 시 새 맵 버전을 굽고 활성화한 뒤 SET_MAP을 알린다."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

import math

from common.map_annotations import (AutoDoor, TrafficGate, Zone, ZoneType, rectangle_frame, rectangle_polygon,
                                    rectangles_overlap)
from common.schemas import Point
from fms_server.mapkit.store import MapStoreError, MapVersionConflict, ZoneNotFound
from fms_server.sites import SiteError

router = APIRouter(prefix='/api/v1/sites/{site_id}/maps/{map_id}/zones', tags=['zones'])


class RectangleZoneRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)
    base_version: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    type: Literal['prohibit', 'elevator', 'occupancy', 'occupancy_auto', 'slow', 'autodoor']
    speed_limit: float | None = Field(default=None, gt=0, le=10)   # 감속 구역(slow)의 최고 속도 m/s
    gates: list[TrafficGate] = Field(default_factory=list, max_length=16)
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: float = 0  # 도 단위. 회전 전 사각형의 중심을 고정한다
    # 자동문(type=autodoor) 전용. 점유 존 + door 로 저장한다 (로봇은 일반 점유 존으로 본다).
    door_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]+$', max_length=64)  # 없으면 zone_id
    door_nodes: list[Point] | None = Field(default=None, min_length=2, max_length=2)       # [A, B]. 없으면 문 양쪽 기본 위치
    door_queues: list[list[Point]] | None = Field(default=None, min_length=2, max_length=2)  # [A 쪽, B 쪽] 대기열

    @model_validator(mode='after')
    def validate_zone(self):
        self.zone('validation')
        return self

    def zone(self, zone_id: str) -> Zone:
        polygon = rectangle_polygon(self.x,self.y,self.width,self.height,self.rotation)
        if self.type == 'autodoor':
            if self.gates:
                raise ValueError('자동문의 진입점은 door_nodes 로 지정한다')
            a, b = self.door_nodes or default_door_nodes(polygon)
            qa, qb = self.door_queues or ([], [])
            return Zone(zone_id=zone_id, name=self.name, type=ZoneType.OCCUPANCY, polygon=polygon,
                        door=AutoDoor(door_id=self.door_id or zone_id, node_a=a, node_b=b, queue_a=qa, queue_b=qb))
        if self.door_id or self.door_nodes or self.door_queues:
            raise ValueError('door_id·door_nodes·door_queues 는 자동문(autodoor)에만 지정할 수 있다')
        if self.type == 'occupancy' and not self.gates:
            raise ValueError('점유 존에는 대기·출구 node 쌍이 필요합니다.')
        if self.type == 'occupancy_auto' and self.gates:
            raise ValueError('노드 없는 점유구역에는 대기·출구 node를 지정할 수 없습니다.')
        zone_type = ZoneType.OCCUPANCY if self.type == 'occupancy_auto' else self.type
        return Zone(zone_id=zone_id, name=self.name, type=zone_type, gates=self.gates, speed_limit=self.speed_limit,
                    polygon=polygon)


DOOR_NODE_OFFSET = 0.8  # 자동문 기본 진입점이 문 구역 경계에서 떨어지는 거리 (m)
QUEUE_MIN_GAP = 0.8     # 대기열 자리 사이 최소 간격 (m). 실제로는 로봇 길이 + 여유 이상 띄운다


def default_door_nodes(polygon) -> tuple[Point, Point]:
    """문 구역의 짧은 변 방향(통과 방향)으로 양쪽 경계 밖에 진입점 A·B 를 둔다."""
    cx, cy, ux, uy, half_w, half_h = rectangle_frame(polygon)
    if half_w <= half_h:
        dx, dy, half = ux, uy, half_w
    else:
        dx, dy, half = -uy, ux, half_h
    d = half + DOOR_NODE_OFFSET
    return Point(x=cx - dx * d, y=cy - dy * d), Point(x=cx + dx * d, y=cy + dy * d)


def _edit_locked(request, site_id, map_id, version, zone_id, zone=None, *, create=False):
    try:
        store = request.app.state.sites.map_store(site_id)
        existing = store.load_annotations(map_id).zone_by_id(zone_id)
        if (zone and zone.type == ZoneType.OCCUPANCY) or (existing and existing.type == ZoneType.OCCUPANCY):
            from fms_server.traffic import bounds, intersects
            coord = request.app.state.job_coordinator
            key = coord.traffic.key(site_id, map_id)
            if (any(v['map'] == key for v in coord.traffic.data['leases'].values())
                    or any(j.site_id == site_id and not j.status.terminal and j.status.value != 'pending'
                           for j in request.app.state.jobs.list(site_id))):
                raise HTTPException(409, '교통 예약·진행 중인 작업이 있습니다. 모두 종료한 뒤 점유 존을 편집하세요.')
            uid = store.map_uid(map_id)
            if any(r.state and r.state.map_id == uid and (r.state.order_id or abs(r.state.velocity) > .01)
                   for r in request.app.state.fleet.all()):
                raise HTTPException(409, '이동 중인 로봇이 있습니다. 정지한 뒤 점유 존을 편집하세요.')
            if zone and zone.door:
                for site in request.app.state.sites.list_sites():
                    other_store = request.app.state.sites.map_store(site.site_id)
                    for summary in other_store.list_maps():
                        if site.site_id == site_id and summary.map_id == map_id:
                            others = [z for z in store.load_annotations(map_id).zones if z.zone_id != zone_id]
                        elif other_store.active_version(summary.map_id):
                            others = other_store.load_annotations(summary.map_id).zones
                        else:
                            continue
                        if any(z.door and z.door.door_id == zone.door.door_id for z in others):
                            raise HTTPException(422, f'자동문 id {zone.door.door_id!r} 는 이미 쓰고 있습니다.')
            if zone and zone.type == ZoneType.OCCUPANCY:
                model = store.load_map_model(map_id)
                for other in store.load_annotations(map_id).zones:
                    if other.type != ZoneType.OCCUPANCY or other.zone_id == zone_id:
                        continue
                    if rectangles_overlap(zone.polygon, other.polygon):
                        raise HTTPException(422, '점유 존은 서로 겹치거나 경계를 공유할 수 없습니다.')
                for gate in zone.gates:
                    if any(not model.is_free(p.x,p.y) or intersects(bounds(zone),p,0.1) for p in (gate.wait,gate.exit)):
                        raise HTTPException(422, '대기·출구 node는 존 바깥의 FREE 위치에 지정하세요.')
                    if gate.wait == gate.exit:
                        raise HTTPException(422, '대기 node와 출구 node는 달라야 합니다.')
                    if any(not model.is_free(p.x,p.y) or intersects(bounds(zone),p,0.1) for p in gate.queue):
                        raise HTTPException(422, '대기열 node는 존 바깥의 FREE 위치에 지정하세요.')
                    line = [gate.wait, *gate.queue]
                    if any(math.hypot(a.x-b.x, a.y-b.y) < QUEUE_MIN_GAP for a, b in zip(line, line[1:])):
                        raise HTTPException(422, f'대기열 자리는 앞 자리와 {QUEUE_MIN_GAP} m 이상 떨어뜨리세요.')
        if not create:
            existing = store.load_annotations(map_id).zone_by_id(zone_id)
            if existing is not None and existing.type == ZoneType.PICKUP:
                # zone 만 바꾸면 node 와의 1:1 연결이 깨진다.
                raise HTTPException(422, '픽업 구역은 픽업스테이션 편집에서 변경하세요.')
        result = store.edit_zone(map_id, version, zone_id, zone, create=create)
    except MapVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ZoneNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (SiteError, MapStoreError) as exc:
        raise HTTPException(422, str(exc)) from None
    notified = request.app.state.coordinator.notify_map_activated(site_id, map_id)
    from fms_server.api.events import record
    record(request, 'zone.created' if create else 'zone.deleted' if zone is None else 'zone.updated',
           site_id=site_id, map_id=map_id, zone_id=zone_id, version=result.version)
    return {'zone_id': zone_id, 'version': result.version, 'warnings': result.warnings,
            'notified_robots': notified}


def _edit(request, *args, **kwargs):
    with request.app.state.job_coordinator.lock:
        from fms_server.api.traffic import ensure_editable
        ensure_editable(request, args[0], args[1])
        return _edit_locked(request, *args, **kwargs)


@router.post('')
def create_zone(site_id: str, map_id: str, body: RectangleZoneRequest, request: Request):
    zone_id = f'zone-{uuid4().hex[:16]}'
    return _edit(request, site_id, map_id, body.base_version, zone_id,
                 body.zone(zone_id), create=True)


@router.put('/{zone_id}')
def update_zone(site_id: str, map_id: str, zone_id: str, body: RectangleZoneRequest, request: Request):
    return _edit(request, site_id, map_id, body.base_version, zone_id, body.zone(zone_id))


@router.delete('/{zone_id}')
def delete_zone(site_id: str, map_id: str, zone_id: str, base_version: str, request: Request):
    return _edit(request, site_id, map_id, base_version, zone_id)
