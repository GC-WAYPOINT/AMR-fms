"""3단계 — 승강기 임대 영속화·FIFO 대기열·장애 시 예비 전환·칸 내 복구."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from common import elevator_proto as P
from common.schemas import ConnectionState, LocalizationState, RobotState
from fms_server import task as task_module
from fms_server.config import settings
from fms_server.elevator_ctl import QUEUE_STALE, CarState, ElevatorController
from fms_server.elevator_sim import ElevatorCar
from fms_server.elevators import ElevatorStore, Stop
from fms_server.events import EventLog
from fms_server.fleet import Fleet
from fms_server.sites import SiteRegistry
from fms_server.task import JobConflict, JobCoordinator, JobError, JobStatus, JobStore
from fms_server.tests.factories import make_state
from fms_server.tests.test_multifloor import FakeMqtt, _png, finish_order


def _upload(store, map_id):
    zones = [{"id": "lift", "name": "A", "type": "elevator", "polygon": [[3, 2], [5, 2], [5, 4], [3, 4]]},
             {"id": "lift2", "name": "B", "type": "elevator", "polygon": [[3, 5], [5, 5], [5, 7], [3, 7]]}]
    files = {"fms_map.json": json.dumps({"name": map_id, "image": "grid.png", "resolution": 0.1, "origin_x": 0,
                                         "origin_y": 0, "width_px": 100, "height_px": 80}).encode(),
             "grid.png": _png(), "zone_meta.json": json.dumps({"zones": zones}).encode()}
    return store.ingest_upload(map_id, files, activate=True).version


def car(ctl, eid, at, door=P.DOOR_CLOSED, held=False, mode=P.MODE_NORMAL):
    ctl.apply_state(CarState(eid, at, None, door, at is None, held, time.time(), mode, "시험" if mode != P.MODE_NORMAL else None))


@pytest.fixture(params=[2])
def env(tmp_path, request):
    registry = SiteRegistry(tmp_path)
    registry.create_site("s", "s")
    store = registry.map_store("s")
    ver = {m: _upload(store, m) for m in ("f1", "f3")}
    registry.register_robot("s", "AMR-001", "f1")
    locs = registry.location_store("s")
    locs.create("dest3", "f3", 8.0, 6.0, 0.0)
    elevators = ElevatorStore(tmp_path, registry)
    elevators.create("s", "elev-A", "A", [Stop("f1", "lift", "1F"), Stop("f3", "lift", "3F")])
    if request.param == 2:
        elevators.create("s", "elev-B", "B", [Stop("f1", "lift2", "1F"), Stop("f3", "lift2", "3F")])
    events = EventLog(tmp_path)
    ctl = ElevatorController(settings, events=events, lease_path=tmp_path / "elevator_leases.json")
    fleet, mqtt, jobs = Fleet(), FakeMqtt(), JobStore(tmp_path)
    coord = JobCoordinator(fleet, registry, jobs, mqtt, elevators=elevators, elevator_ctl=ctl, events=events)
    ctl.add_listener(coord.on_elevator_state)      # main.py lifespan 과 같은 연결
    coord.start()
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)

    def robot(map_id="f1", state=RobotState.IDLE, order_id=None, loc=LocalizationState.LOCALIZED, **kw):
        fleet.apply_state(make_state("AMR-001", state, order_id=order_id, map_id=store.map_uid(map_id),
                                     map_version=ver[map_id], localization=loc, **kw))

    robot()
    yield SimpleNamespace(registry=registry, fleet=fleet, mqtt=mqtt, ctl=ctl, jobs=jobs, coord=coord, robot=robot,
                          store=store, events=events, elevators=elevators, tmp=tmp_path)
    coord.stop()
    events.stop()


single = pytest.mark.parametrize("env", [1], indirect=True)


def to_call_stage(env):
    job = env.jobs.create("s", [{"type": "move", "target": "dest3"}])
    env.coord.dispatch(job.job_id, "AMR-001")
    assert job.plan[1] == {"t": "call", "elevator_id": "elev-A", "map_id": "f1"}     # 가까운 A 를 고른다
    car(env.ctl, "elev-A", "f3")
    car(env.ctl, "elev-B", "f3")
    finish_order(env)
    assert job.stage == 1 and env.ctl.lease_owner("elev-A") == job.job_id
    return job


# -- 임대 영속화·대기열 ------------------------------------------------------

def test_lease_survives_restart_and_orphans_are_dropped(env):
    job = to_call_stage(env)
    restored = ElevatorController(settings, lease_path=env.tmp / "elevator_leases.json")
    assert restored.lease_owner("elev-A") == job.job_id
    # 주인 작업이 없는 임대는 시작할 때 놓는다
    restored.acquire("elev-B", "s-J9999")
    again = ElevatorController(settings, lease_path=env.tmp / "elevator_leases.json")
    other = JobCoordinator(env.fleet, env.registry, env.jobs, env.mqtt, elevators=env.elevators, elevator_ctl=again)
    other.start()
    assert again.lease_owner("elev-B") is None and again.lease_owner("elev-A") == job.job_id
    other.stop()


def test_lease_queue_is_fifo_and_drops_dead_waiters():
    ctl = ElevatorController(settings)
    assert ctl.acquire("e", "A", now=0)
    assert not ctl.acquire("e", "B", now=1)
    assert not ctl.acquire("e", "C", now=2)
    ctl.release_lease("e", "A")
    assert not ctl.acquire("e", "C", now=3)          # 나중에 온 요청이 먼저 불러도 못 잡는다
    assert ctl.queue_position("e", "C") == 1
    assert ctl.acquire("e", "B", now=3.5)
    ctl.release_lease("e", "B")
    assert ctl.acquire("e", "C", now=4)
    ctl.release_lease("e", "C")
    # 줄만 서고 사라진 요청은 시간이 지나면 버린다
    assert ctl.acquire("e", "A", now=10)
    assert not ctl.acquire("e", "D", now=11)
    assert not ctl.acquire("e", "E", now=12)
    ctl.release_lease("e", "A")
    assert ctl.acquire("e", "E", now=12 + QUEUE_STALE + 1)
    ctl.forget("E")


# -- 장애·타임아웃 예비 전환 -------------------------------------------------

def test_sim_fault_injection_rejects_requests():
    c = ElevatorCar("e", ["f1", "f3"])
    assert c.set_fault(P.MODE_OUT_OF_SERVICE, "문 고장") and c.state()["mode"] == P.MODE_OUT_OF_SERVICE
    assert not c.command(P.CMD_CALL, "f3", True)
    assert not c.set_fault("BROKEN")
    assert c.set_fault(P.MODE_NORMAL) and c.state()["fault"] is None and c.command(P.CMD_CALL, "f3", True)


def test_faulty_elevator_is_left_out_of_new_plans(env):
    car(env.ctl, "elev-A", "f1", mode=P.MODE_OUT_OF_SERVICE)
    assert env.ctl.unavailable() == frozenset({"elev-A"})
    job = env.jobs.create("s", [{"type": "move", "target": "dest3"}])
    env.coord.dispatch(job.job_id, "AMR-001")
    assert job.plan[1]["elevator_id"] == "elev-B"
    assert env.events.query(kind="elevator.fault")[0]["data"]["mode"] == P.MODE_OUT_OF_SERVICE


def test_fault_before_boarding_switches_to_backup(env):
    job = to_call_stage(env)
    car(env.ctl, "elev-A", "f3", mode=P.MODE_FIRE)
    assert job.plan[job.stage]["t"] == "order" and job.plan[job.stage + 1]["elevator_id"] == "elev-B"
    assert env.ctl.lease_owner("elev-A") is None                    # 임대 반납
    assert job.status in (JobStatus.ASSIGNED, JobStatus.WORKING)
    assert job.plan[-1]["nodes"][-1]["cmd"] == 0                     # 명령 번호는 원래 것을 가리킨다
    reroute = env.events.query(kind="elevator.reroute")
    assert reroute[0]["data"]["to"] == "elev-B" and reroute[0]["data"]["reason"] == "장애"
    finish_order(env)                                               # B 승강장으로 이동한 뒤 B 를 부른다
    assert env.ctl.lease_owner("elev-B") == job.job_id


def test_call_timeout_switches_to_backup(env, monkeypatch):
    job = to_call_stage(env)
    monkeypatch.setattr(task_module, "ELEVATOR_WAIT_TIMEOUT", -1.0)
    env.coord.tick_multifloor()
    assert job.plan[job.stage + 1]["elevator_id"] == "elev-B"
    assert "초과" in env.events.query(kind="elevator.reroute")[0]["data"]["reason"]


@single
def test_no_backup_keeps_waiting_and_notifies_once(env):
    job = env.jobs.create("s", [{"type": "move", "target": "dest3"}])
    env.coord.dispatch(job.job_id, "AMR-001")
    car(env.ctl, "elev-A", "f3")
    finish_order(env)
    car(env.ctl, "elev-A", "f3", mode=P.MODE_OUT_OF_SERVICE)
    env.coord.tick_multifloor()
    assert job.stage == 1 and job.status in (JobStatus.ASSIGNED, JobStatus.WORKING)
    assert "예비 승강기 없음" in job.error
    assert len(env.events.query(kind="elevator.reroute")) == 1
    car(env.ctl, "elev-A", "f3")                                    # 복구되면 이어서 부른다
    env.coord.tick_multifloor()
    assert job.error is None


# -- 칸 내 복구 --------------------------------------------------------------

def board_then_cancel(env):
    job = to_call_stage(env)
    car(env.ctl, "elev-A", "f1", P.DOOR_OPEN, held=True)
    assert job.stage == 2                                           # 탑승 오더 발행
    oid = env.mqtt.orders[-1].order_id
    env.robot("f1", RobotState.BUSY, order_id=oid, x=4.0, y=3.0)
    env.coord.cancel(job.job_id)
    env.robot("f1", x=4.0, y=3.0)                                   # 칸 안에서 멈췄다
    assert job.status == JobStatus.CANCELED
    return job


def test_failed_job_in_car_keeps_lease_and_marks_attention(env):
    job = board_then_cancel(env)
    assert env.ctl.lease_owner("elev-A") == job.job_id              # 임대와 문 열림 유지
    attention = env.ctl.attention("elev-A")
    assert attention["robot_id"] == "AMR-001" and "칸 안" in attention["reason"]
    assert "elev-A" in env.ctl.unavailable()
    assert env.ctl.snapshot("elev-A")["attention"]["job_id"] == job.job_id
    # 재시작해도 확인 필요 표시와 임대가 남는다
    restored = ElevatorController(settings, lease_path=env.tmp / "elevator_leases.json")
    again = JobCoordinator(env.fleet, env.registry, env.jobs, env.mqtt, elevators=env.elevators, elevator_ctl=restored)
    again.start()
    assert restored.attention("elev-A") and restored.lease_owner("elev-A") == job.job_id
    again.stop()


def test_recover_exit_backs_robot_out_and_releases(env):
    job = board_then_cancel(env)
    with pytest.raises(JobConflict):
        env.coord.recover_elevator("s", "elev-B", None, "exit")     # 확인 필요 상태가 아니다
    car(env.ctl, "elev-A", "f1", P.DOOR_CLOSED)
    with pytest.raises(JobConflict):
        env.coord.recover_elevator("s", "elev-A", None, "exit")     # 문이 닫혀 있다
    car(env.ctl, "elev-A", "f1", P.DOOR_OPEN, held=True)
    result = env.coord.recover_elevator("s", "elev-A", None, "exit", actor="kim")
    recovery = env.jobs.get(result["job_id"])
    assert recovery.system == "recover" and recovery.robot_id == "AMR-001"
    assert env.ctl.lease_owner("elev-A") == recovery.job_id
    action = env.mqtt.orders[-1].nodes[0].action
    assert action.action_type.value == "STRAIGHT" and action.params["distance"] < 0
    finish_order(env)
    assert recovery.status == JobStatus.SUCCESS
    assert env.ctl.lease_owner("elev-A") is None and env.ctl.attention("elev-A") is None
    assert env.events.query(kind="elevator.recover")[0]["actor"] == "kim"
    with pytest.raises(JobError):
        env.coord.recover_elevator("s", "elev-A", None, "exit")


def test_recover_release_when_car_is_confirmed_empty(env):
    job = board_then_cancel(env)
    with pytest.raises(JobError):
        env.coord.recover_elevator("s", "elev-A", None, "teleport")
    env.coord.recover_elevator("s", "elev-A", None, "release")
    assert env.ctl.lease_owner("elev-A") is None and env.ctl.attention("elev-A") is None
    assert "elev-A" not in env.ctl.unavailable()
