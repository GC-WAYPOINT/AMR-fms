"""자동문 존: 기본 진입점·저장 형태·문 열림 확인 후 진입·통과 후 해제."""
import json
import math
import time

import pytest

from common import door_proto
from common.map_annotations import AutoDoor, Zone, ZoneType
from common.schemas import Order, Point
from fms_server.door_ctl import DoorController, DoorState
from fms_server.door_sim import DoorUnit
from fms_server.tests.test_traffic import BASE, env, latest, leases, dispatch  # noqa: F401  (fixture 재사용)

DOOR = 'door-east'


def door_body(version, **extra):
    return dict(dict(base_version=version, name='자동문', type='autodoor', x=7.6, y=1, width=0.8, height=4,
                     door_id=DOOR), **extra)


class FakeDoors:
    """DoorController 대역. 열림 여부는 테스트가 정한다."""

    def __init__(self):
        self.open_doors, self.requests, self.released, self._holds = set(), [], [], {}

    def request_open(self, door, owner, now=None):
        self.requests.append((door, owner))
        self._holds[door] = owner
        return door in self.open_doors

    def release(self, door, owner):
        if self._holds.get(door) == owner:
            del self._holds[door]
            self.released.append((door, owner))

    def holder(self, door):
        return self._holds.get(door)

    def holds(self):
        return dict(self._holds)

    def reason(self, door):
        return '자동문 열림 대기'


def add_door(e, **extra):
    r = e['client'].post(BASE + '/zones', json=door_body(e['version'], **extra))
    assert r.status_code == 200, r.text
    for rid, x, y in [('R1', 3, 3), ('R2', 13, 7), ('R3', 3, 1)]:
        e['report'](rid, x, y)
    e['coord'].traffic.doors = e['doors'] = FakeDoors()
    return r.json()


def test_model_derives_gates_with_travel_direction():
    zone = Zone(zone_id='z', type=ZoneType.OCCUPANCY,
                polygon=[Point(x=0, y=0), Point(x=1, y=0), Point(x=1, y=4), Point(x=0, y=4)],
                door=AutoDoor(door_id='d1', node_a=Point(x=-1, y=2), node_b=Point(x=2, y=2)))
    a2b, b2a = zone.gates
    assert (a2b.wait.x, a2b.exit.x, a2b.wait.theta) == (-1, 2, 0)
    assert (b2a.wait.x, b2a.exit.x) == (2, -1)
    assert math.isclose(abs(b2a.wait.theta), math.pi, abs_tol=1e-9)
    with pytest.raises(ValueError, match='점유 존'):
        Zone(zone_id='z', type=ZoneType.PROHIBIT, polygon=zone.polygon, door=zone.door)
    with pytest.raises(ValueError, match='서로 다른'):
        AutoDoor(door_id='d', node_a=Point(x=1, y=1), node_b=Point(x=1, y=1))


def test_default_nodes_and_storage(env):
    add_door(env)
    zone = env['registry'].map_store('hq').load_annotations('f1').zones[-1]
    assert zone.type == ZoneType.OCCUPANCY and zone.door.door_id == DOOR
    # 짧은 변(가로 0.8) 방향 양쪽 경계 밖 0.8 m
    assert (zone.door.node_a.x, zone.door.node_a.y) == pytest.approx((6.8, 3))
    assert (zone.door.node_b.x, zone.door.node_b.y) == pytest.approx((9.2, 3))
    assert [g.gate_id for g in zone.gates] == ['a2b', 'b2a']
    assert env['registry'].map_store('hq').load_map_model('f1').is_free(8, 3)  # 굽지 않는다


def test_door_id_must_be_unique_and_nodes_valid(env):
    z = add_door(env)
    r = env['client'].post(BASE + '/zones', json=dict(door_body(z['version']), x=2, y=6))
    assert r.status_code == 422 and '이미' in r.text
    r = env['client'].post(BASE + '/zones', json=dict(door_body(z['version'], door_id='other', ), x=2, y=6,
                                                      door_nodes=[dict(x=1, y=1), dict(x=1, y=1)]))
    assert r.status_code == 422
    r = env['client'].post(BASE + '/zones', json=dict(door_body(z['version'], door_id='x y')))
    assert r.status_code == 422


def test_robot_waits_until_door_opens_then_passes_and_door_is_released(env):
    add_door(env)
    doors = env['doors']
    a, oa = dispatch(env, 'R1', 'east')
    assert oa.nodes[0].released and not oa.nodes[1].released
    assert env['coord'].traffic.data  # 교통 상태 존재
    assert a.traffic['crossings'][0]['door'] == DOOR
    env['report']('R1', 6.8, 3, oa, node=0)
    # 문이 닫혀 있다: 열림 신호는 갔지만 존 진입 허가는 없다
    assert doors.requests and doors.requests[-1][0] == DOOR
    assert not latest(a).nodes[1].released
    assert a.traffic['waiting'] == '자동문 열림 대기'
    assert not leases(env, 'zone')
    # 문이 열렸다
    doors.open_doors.add(DOOR)
    env['coord'].tick_traffic()
    assert latest(a).nodes[1].released
    assert leases(env, 'zone')[0]['door'] == DOOR
    assert not doors.released                      # 통과 중에는 계속 열어 둔다
    env['report']('R1', 9.2, 3, latest(a), node=1)
    assert not leases(env, 'zone')
    assert doors.released == [(DOOR, a.job_id)]


def test_canceled_while_waiting_releases_door_hold(env):
    add_door(env)
    doors = env['doors']
    a, oa = dispatch(env, 'R1', 'east')
    env['report']('R1', 6.8, 3, oa, node=0)
    assert doors.holder(DOOR) == a.job_id
    env['coord'].cancel(a.job_id)
    env['report']('R1', 6.8, 3)                      # 취소 확인: 로봇이 멈추고 작업이 끝난다
    env['coord'].tick_traffic()
    assert a.status.value == 'canceled'
    assert doors.holder(DOOR) is None


def test_no_door_controller_keeps_waiting(env):
    add_door(env)
    env['coord'].traffic.doors = None
    a, oa = dispatch(env, 'R1', 'east')
    env['report']('R1', 6.8, 3, oa, node=0)
    assert not latest(a).nodes[1].released
    assert a.traffic['waiting'] == '자동문 제어기 없음'


def test_door_unit_cycle():
    d = DoorUnit('d', door_time=1.0, max_open=5.0)
    assert d.command(door_proto.CMD_OPEN) and d.door == door_proto.DOOR_OPENING
    d.step(1.1)
    assert d.door == door_proto.DOOR_OPEN and d.held
    d.step(3)
    assert d.door == door_proto.DOOR_OPEN                  # RELEASE 전에는 안 닫힌다
    d.command(door_proto.CMD_RELEASE)
    d.step(0.1)
    assert d.door == door_proto.DOOR_CLOSING
    d.step(1.1)
    assert d.door == door_proto.DOOR_CLOSED
    d.command(door_proto.CMD_OPEN)
    d.step(1.1)
    d.step(6)                                              # 제한 시간이 지나면 스스로 닫힌다
    assert d.door in (door_proto.DOOR_CLOSING, door_proto.DOOR_CLOSED) and not d.held


def test_controller_open_state_staleness_and_hold(monkeypatch):
    from fms_server.config import Settings
    ctl = DoorController(Settings())
    sent = []
    monkeypatch.setattr(ctl, '_send', lambda door, body: sent.append((door, body['type'])))
    assert ctl.request_open('d', 'j1', now=100.0) is False          # 상태 없음 = 닫힘으로 본다
    assert ctl.request_open('d', 'j1', now=101.0) is False
    assert sent == [('d', 'OPEN')]                                   # 2초 안에는 다시 보내지 않는다
    assert ctl.request_open('d', 'j2', now=102.0) is False and ctl.holder('d') == 'j1'
    ctl.apply_state(DoorState('d', door_proto.DOOR_OPEN, True, time.time()))
    assert ctl.request_open('d', 'j1') is True
    ctl.apply_state(DoorState('d', door_proto.DOOR_OPEN, True, time.time(), door_proto.MODE_OUT_OF_SERVICE, '고장'))
    assert ctl.is_open('d') is False and '장애' in ctl.reason('d')
    ctl.release('d', 'j2')
    assert ctl.holder('d') == 'j1'                                   # 주인이 아니면 못 푼다
    ctl.release('d', 'j1')
    assert ctl.holder('d') is None and sent[-1] == ('d', 'RELEASE')
