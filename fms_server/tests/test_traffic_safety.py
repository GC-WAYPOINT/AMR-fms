"""3단계 — 교착·정체 감지, 안전한 예약 회수, 유휴 로봇 비켜주기, 경로 교차 예고."""
from __future__ import annotations

import time

import pytest

from common.schemas import BusySubState, InstantActionType, PauseSource, Point, RobotState
from fms_server import traffic as traffic_module
from fms_server.battery import BatteryManager
from fms_server.events import EventLog
from fms_server.task import JobStatus
from fms_server.tests.test_traffic import env  # noqa: F401  (fixture)
from fms_server.traffic import find_cycles

KEY = "hq/f1"
BOUNDS = (8.0, 5.0, 1.0, 0.0, 1.0, 5.0)


@pytest.fixture
def safety(env, tmp_path):
    events = EventLog(tmp_path / "ev")
    coord = env["coord"]
    coord._events = events
    coord.traffic.events = events
    yield env, coord, events
    events.stop()


def working(e, robot, waiting=None, gate="g1", **traffic):
    """교통 대기 중인 working 작업을 만든다 (오더 발행 없이 상태만)."""
    job = e["jobs"].create("hq", [{"type": "move", "target": "east"}], robot)
    job.status, job.order_id = JobStatus.WORKING, job.job_id
    uid = e["registry"].map_store("hq").map_uid("f1")
    job.traffic = dict({"map": KEY, "map_id": "f1", "uid": uid, "version": e["version"], "sources": {},
                        "waiting": waiting,
                        "crossings": [{"zone": "z1", "gate": gate, "wait": 0, "exit": 1, "bounds": BOUNDS,
                                       "done": False}]}, **traffic)
    e["jobs"].save(job)
    return job


def lease(coord, resource, robot, job, kind, **extra):
    coord.traffic.data["leases"][resource] = dict(
        {"map": KEY, "robot": robot, "job": job.job_id, "kind": kind, "at": time.time(), "order": job.job_id}, **extra)


def test_find_cycles():
    edges = [{"from": "A", "to": "B"}, {"from": "B", "to": "A"}, {"from": "C", "to": "A"}, {"from": "D", "to": "E"}]
    assert [sorted(c) for c in find_cycles(edges)] == [["A", "B"]]
    assert find_cycles([{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]) == []


def test_deadlock_is_detected_once_and_issued_permits_are_never_revoked(safety):
    e, coord, events = safety
    a = working(e, "R1", "대기 node 사용 중")
    b = working(e, "R2", "점유 존 진입 허가 대기", gate="g2")
    lease(coord, f"wait:{KEY}:z1:g1", "R2", b, "wait", pose={"x": 5, "y": 3, "theta": 0}, radius=0.5)
    lease(coord, f"zone:{KEY}:z1", "R1", a, "zone", bounds=BOUNDS)
    before = dict(coord.traffic.data["leases"])
    analysis = coord.tick_safety()
    assert [d["robots"] for d in analysis["deadlocks"]] == [["R1", "R2"]]
    assert set(analysis["deadlocks"][0]["resources"]) == {f"wait:{KEY}:z1:g1", f"zone:{KEY}:z1"}
    coord.tick_safety()
    assert len(events.query(kind="traffic.deadlock")) == 1            # 같은 순환은 한 번만 알린다
    assert coord.traffic.data["leases"] == before                      # 대기 node·점유 존 예약은 자동 해제하지 않는다
    assert not events.query(kind="traffic.revoke")
    shown = coord.traffic.snapshot("hq", "f1")
    assert shown["deadlocks"][0]["robots"] == ["R1", "R2"]
    # 순환이 풀리면 다시 생겼을 때 새로 알린다
    b.traffic["waiting"] = None
    assert coord.tick_safety()["deadlocks"] == []


def test_only_unissued_arrival_floor_reservation_is_revoked(safety):
    e, coord, events = safety
    a = working(e, "R1", "층별 동시 이동 대수 제한")
    b = working(e, "R2", "대기 node 사용 중")
    lease(coord, f"floor:{KEY}:R2", "R2", b, "floor", future=True, at=time.time())       # 탑승 전 도착 층 예약
    lease(coord, f"wait:{KEY}:z1:g1", "R1", a, "wait", pose={"x": 5, "y": 3, "theta": 0}, radius=0.5)
    analysis = coord.traffic.analyze()
    assert [d["robots"] for d in analysis["deadlocks"]] == [["R1", "R2"]]
    assert coord.traffic.revoke_safe(analysis) == [f"floor:{KEY}:R2"]
    assert set(coord.traffic.data["leases"]) == {f"wait:{KEY}:z1:g1"}
    revoked = events.query(kind="traffic.revoke")
    assert len(revoked) == 1 and revoked[0]["robot_id"] == "R2"
    # 실제 이동 허가(future 가 아닌 floor lease)는 회수하지 않는다
    lease(coord, f"floor:{KEY}:R2", "R2", b, "floor", future=False)
    assert coord.traffic.revoke_safe(coord.traffic.analyze()) == []


def test_stall_is_reported_separately_from_deadlock(safety, monkeypatch):
    e, coord, events = safety
    job = working(e, "R1")
    order = type("O", (), {"order_id": job.job_id, "order_update_id": 1})()
    now = time.time()
    def moving(x):
        e["report"]("R1", x, 3, order=order)      # report 는 오더가 있으면 WAITING 으로 만든다
        state = e["fleet"].get("R1").state
        e["fleet"].apply_state(state.model_copy(update={"sub_state": BusySubState.MOVING}))
    moving(3)
    assert coord.traffic.analyze(now)["stalls"] == []
    analysis = coord.traffic.analyze(now + 61)
    assert analysis["stalls"][0]["kind"] == "moving" and not analysis["stalls"][0]["in_deadlock"]
    coord.traffic.analyze(now + 70)
    assert len(events.query(kind="traffic.stall")) == 1 and not events.query(kind="traffic.deadlock")
    moving(4)                                                                # 움직였다
    assert coord.traffic.analyze(now + 71)["stalls"] == []
    # WAITING 은 더 오래 본다
    e["report"]("R1", 4, 3, order=order)
    coord.traffic.analyze(now + 80)
    assert coord.traffic.analyze(now + 80 + traffic_module.STALL_WAIT_SECONDS - 1)["stalls"] == []
    assert coord.traffic.analyze(now + 81 + traffic_module.STALL_WAIT_SECONDS)["stalls"][0]["kind"] == "waiting"


def test_idle_blocker_is_asked_to_move_aside(safety, tmp_path):
    e, coord, events = safety
    e["mqtt"].connected = True
    e["registry"].location_store("hq").create("park-1", "f1", 13, 1, 0, "parking")
    manager = BatteryManager(coord, e["registry"], e["fleet"], e["jobs"], tmp_path, events=events)
    coord.battery = manager
    job = working(e, "R1", "대기 node 주변 확인 대기")
    wait = type("P", (), {"x": 3.0, "y": 1.2})()          # R3(3,1) 이 대기 node 옆에 서 있다
    assert coord.traffic._clear_space(job, wait, 0.5) is False
    assert coord.traffic.aside_requests == {("hq", "R3")}
    coord.tick_safety()
    staging = [j for j in e["jobs"].list("hq") if j.system == "staging"]
    assert len(staging) == 1 and staging[0].robot_id == "R3" and staging[0].commands[0].target == "park-1"
    assert not coord.traffic.aside_requests
    assert events.query(kind="staging.requested", robot="R3")[0]["data"]["reason"] == "비켜주기"
    # 작업 중인 로봇은 비켜 달라고 하지 않는다
    coord.traffic._clear_space(job, type("P", (), {"x": 3.0, "y": 3.2})(), 0.5)
    assert coord.traffic.aside_requests == set()


def head_on(e, jobs):
    """R1 은 +x, R2 는 -x 로 같은 줄에서 마주 본다."""
    for robot, x, direction in (("R1", 3.0, 1), ("R2", 6.0, -1)):
        order = type("O", (), {"order_id": jobs[robot].job_id, "order_update_id": 1})()
        e["report"](robot, x, 5.0, order=order, sub_state=BusySubState.MOVING, velocity=0.5,
                    local_path=[Point(x=x + direction * i * 0.5, y=5.0) for i in range(1, 8)])


def test_path_conflict_notice_and_optional_pause(safety):
    e, coord, events = safety
    jobs = {"R1": working(e, "R1"), "R2": working(e, "R2")}
    jobs["R1"].priority = 5
    head_on(e, jobs)
    coord.tick_safety()
    coord.tick_safety()
    notices = events.query(kind="traffic.conflict")
    assert len(notices) == 1 and notices[0]["data"]["robots"] == ["R1", "R2"]
    assert not e["mqtt"].instants                                    # 자동 일시정지는 기본 끔
    coord.traffic.set_options("hq", "f1", conflict_pause=True)
    coord.tick_safety()
    assert [(a.robot_id, a.action_type) for a in e["mqtt"].instants] == [("R2", InstantActionType.PAUSE)]
    coord.tick_safety()                                              # 한 번만 세운다
    assert len(e["mqtt"].instants) == 1
    # 경로가 갈라지면 세웠던 로봇을 다시 보낸다
    order = type("O", (), {"order_id": jobs["R1"].job_id, "order_update_id": 1})()
    e["report"]("R1", 3.0, 9.0, order=order, sub_state=BusySubState.MOVING, velocity=0.5,
                local_path=[Point(x=3.0, y=9.5)])
    order2 = type("O", (), {"order_id": jobs["R2"].job_id, "order_update_id": 1})()
    e["report"]("R2", 6.0, 5.0, order=order2, sub_state=BusySubState.MOVING, velocity=0.0,
                local_path=[Point(x=5.5, y=5.0)], paused=True, pause_source=PauseSource.FMS)
    coord.tick_safety(time.time() + 10)
    assert e["mqtt"].instants[-1].action_type == InstantActionType.RESUME and e["mqtt"].instants[-1].robot_id == "R2"


def test_conflict_option_api_keeps_limit(env):
    c = env["client"]
    assert c.put("/api/v1/sites/hq/maps/f1/traffic", json={"max_moving_robots": 2}).status_code == 200
    r = c.put("/api/v1/sites/hq/maps/f1/traffic", json={"conflict_pause": True})
    assert r.status_code == 200 and r.json()["conflict_pause"] is True and r.json()["max_moving_robots"] == 2
