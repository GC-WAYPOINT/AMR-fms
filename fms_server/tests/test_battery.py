"""2단계 — 자동 충전, 유휴 대기 위치 복귀, 선점, 충전소 그룹."""
from __future__ import annotations

import pytest

from common.schemas import RobotState
from fms_server.battery import BatteryManager, PolicyError
from fms_server.main import app
from fms_server.task import JobStatus
from fms_server.tests.test_presets_allocator import BASE, env, preset  # noqa: F401  (fixture)


@pytest.fixture
def battery(env, tmp_path):
    c, registry, jobs, coord, mqtt, online = env
    now = [10_000.0]
    registry.location_store("hq").update("charger", type="charger")   # env 는 일반 목적지로 만든다
    manager = BatteryManager(coord, registry, app.state.fleet, jobs, tmp_path,
                             events=app.state.events, clock=lambda: now[0])
    coord.battery = manager
    yield manager, now
    coord.battery = app.state.battery


def system_jobs(jobs, kind):
    return [j for j in jobs.list("hq") if j.system == kind]


def test_policy_validation_and_default_off(env, battery):
    c, *_ = env
    manager, _ = battery
    assert manager.policy("hq")["enabled"] is False
    with pytest.raises(PolicyError):
        manager.set_policy("hq", {"low": 70, "release": 60})
    with pytest.raises(PolicyError):
        manager.set_policy("hq", {"nope": 1})
    with pytest.raises(PolicyError):
        manager.set_policy("hq", {"min_job_charge": 80})
    saved = manager.set_policy("hq", {"enabled": True, "low": 30})
    assert saved["low"] == 30 and saved["release"] == 60
    assert BatteryManager(manager.coordinator, None, None, None, manager.root.parent).policy("hq")["low"] == 30
    # API 는 lifespan 의 관리자를 쓴다
    assert c.put(BASE + "/charging", json={"low": 70}).status_code == 422
    assert c.put(BASE + "/charging", json={"enabled": False}).json()["enabled"] is False
    assert c.get(BASE + "/charging").json()["low"] == 25


def test_low_battery_robot_is_sent_to_charger(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = battery
    manager.set_policy("hq", {"enabled": True})
    online("AMR-001", battery=18.0)
    online("AMR-002", battery=80.0)
    manager.tick()
    charge = system_jobs(jobs, "charge")
    assert len(charge) == 1 and charge[0].robot_id == "AMR-001" and charge[0].status == JobStatus.ASSIGNED
    command = charge[0].commands[0]
    assert command.kind == "docking" and command.target == "charger" and command.params["min_charge"] == 60
    manager.tick()                                   # 진행 중이면 또 만들지 않는다
    assert len(system_jobs(jobs, "charge")) == 1
    assert app.state.events.query(kind="charge.requested", robot="AMR-001")


def test_charger_contention_notes_once(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = battery
    manager.set_policy("hq", {"enabled": True})
    online("AMR-001", battery=10.0)
    online("AMR-002", battery=15.0)
    manager.tick()
    assert [j.robot_id for j in system_jobs(jobs, "charge")] == ["AMR-001"]      # 더 낮은 로봇 먼저
    now[0] += 5
    manager.tick()
    manager.tick()
    notes = app.state.events.query(kind="charge.no_charger", robot="AMR-002")
    assert len(notes) == 1 and notes[0]["data"]["reason"] == "빈 충전소 없음"


def test_min_job_charge_blocks_normal_jobs_only_when_enabled(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, _ = battery
    p = preset(c, [{"type": "move", "target": "a1"}])
    online("AMR-001", battery=15.0)
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    manager.set_policy("hq", {"enabled": True})
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.PENDING and "20%" in jobs.get(job_id).error
    manager.set_policy("hq", {"enabled": False})     # 꺼져 있으면 이전 동작 (0% 만 거절)
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.ASSIGNED


def test_opportunity_charging_after_idle(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = battery
    manager.set_policy("hq", {"enabled": True, "opportunity": True, "idle_after": 30})
    online("AMR-001", battery=50.0)
    manager.tick()
    assert not system_jobs(jobs, "charge")
    now[0] += 31
    manager.tick()
    assert len(system_jobs(jobs, "charge")) == 1


def test_charger_group_restriction(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, _ = battery
    manager.set_policy("hq", {"enabled": True})
    registry.location_store("hq").update("charger", groups=["cart"])
    online("AMR-001", battery=10.0)
    manager.tick()
    assert not system_jobs(jobs, "charge")
    registry.update_robot("hq", "AMR-001", groups=["cart"])
    manager._cooldown.clear()
    manager.tick()
    assert len(system_jobs(jobs, "charge")) == 1


def test_staging_sends_idle_robot_to_parking_and_is_preempted(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = battery
    registry.location_store("hq").create("park-1", "f1", 8, 8, 0, "parking")
    manager.set_policy("hq", {"staging": {"enabled": True, "idle_after": 20}})
    online("AMR-001", x=2.0, y=2.0)
    manager.tick()
    assert not system_jobs(jobs, "staging")
    now[0] += 21
    manager.tick()
    staging = system_jobs(jobs, "staging")
    assert len(staging) == 1 and staging[0].commands[0].target == "park-1"
    online("AMR-001", state=RobotState.BUSY, order_id=staging[0].job_id, x=3.0, y=3.0)
    # 일반 작업이 오면 대기 위치로 가던 시스템 작업을 취소하고, 취소 확인 뒤 배정한다
    p = preset(c, [{"type": "move", "target": "a1"}])
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    coord.allocate_pending()
    assert jobs.get(staging[0].job_id).status == JobStatus.CANCELING
    assert jobs.get(job_id).status == JobStatus.PENDING
    coord.allocate_pending()                         # 선점은 한 번만
    online("AMR-001", x=3.0, y=3.0)
    assert jobs.get(staging[0].job_id).status == JobStatus.CANCELED
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.ASSIGNED and jobs.get(job_id).robot_id == "AMR-001"


def test_robot_already_at_parking_stays(env, battery):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = battery
    registry.location_store("hq").create("park-1", "f1", 8, 8, 0, "parking")
    manager.set_policy("hq", {"staging": {"enabled": True, "idle_after": 0}})
    online("AMR-001", x=8.1, y=8.0)
    manager.tick()
    assert not system_jobs(jobs, "staging")


def test_spots_api(env):
    c, registry, _, _, _, online = env
    registry.location_store("hq").update("charger", type="charger")
    registry.location_store("hq").create("park-1", "f1", 8, 8, 0, "parking")
    online("AMR-001", x=5.0, y=5.0)
    body = c.get(BASE + "/charging/spots").json()
    assert body["chargers"][0]["name"] == "charger" and body["chargers"][0]["occupied"] is True
    assert body["parkings"][0]["name"] == "park-1" and body["parkings"][0]["occupied"] is False
