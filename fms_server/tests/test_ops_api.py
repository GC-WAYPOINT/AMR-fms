"""1단계 — 로봇 즉시 명령, 현장 정지·대피, 점검 모드, 재시도."""
from __future__ import annotations

import pytest

from common.schemas import InstantActionType, PauseSource, RobotState
from fms_server.main import app
from fms_server.task import JobStatus
from fms_server.tests.factories import make_state
from fms_server.tests.test_presets_allocator import BASE, env, preset  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def wire(env):
    """로봇 명령 API 와 SiteCoordinator 도 가짜 MQTT 로 발행하게 한다 (env 는 작업 코디네이터만 바꾼다)."""
    mqtt = env[4]
    app.state.mqtt = mqtt
    app.state.coordinator._mqtt = mqtt


def kinds(mqtt, robot=None):
    return [a.action_type for a in mqtt.instants if robot is None or a.robot_id == robot]


def paused_state(online, robot, source=PauseSource.FMS, **kw):
    """일시정지 보고를 흉내 낸다 (factories 는 paused 를 안 받는다)."""
    state = online(robot, **kw)
    app.state.fleet.apply_state(state.model_copy(update={"paused": True, "pause_source": source}))


def move_preset(client):
    return preset(client, [{"type": "move", "target": "a1"}])


# -- 1.1 즉시 명령 -----------------------------------------------------------

def test_pause_resume_reset(env):
    c, _, _, _, mqtt, online = env
    assert c.post("/api/v1/robots/AMR-404/pause").status_code == 404
    online()
    assert c.post("/api/v1/robots/AMR-001/pause").json()["action"] == "PAUSE"
    assert c.post("/api/v1/robots/AMR-001/resume").status_code == 409        # 일시정지가 아니다
    paused_state(online, "AMR-001", PauseSource.LOCAL)
    r = c.post("/api/v1/robots/AMR-001/resume")
    assert r.status_code == 409 and "LOCAL" in r.json()["detail"]             # 로봇에서 건 정지는 못 푼다
    paused_state(online, "AMR-001", PauseSource.FMS)
    assert c.post("/api/v1/robots/AMR-001/resume").status_code == 200
    assert c.post("/api/v1/robots/AMR-001/reset-error").status_code == 200
    assert kinds(mqtt) == [InstantActionType.PAUSE, InstantActionType.RESUME, InstantActionType.RESET_ERROR]
    assert mqtt.instants[0].action_id.startswith("PAUSE-")
    commands = app.state.events.query(kind="robot.command", robot="AMR-001")
    assert {e["data"]["command"] for e in commands} == {"PAUSE", "RESUME", "RESET_ERROR"}


# -- 1.2 현장 전체 정지 ------------------------------------------------------

def test_site_pause_skips_allocation_and_resumes_only_its_robots(env):
    c, registry, jobs, coord, mqtt, online = env
    p = move_preset(c)
    online("AMR-001")
    paused_state(online, "AMR-002", PauseSource.FMS)        # 운영자가 미리 세워 둔 로봇
    r = c.post(BASE + "/pause", json={"reason": "청소"})
    assert r.status_code == 200 and r.json()["paused_robots"] == ["AMR-001"]
    assert registry.get_site("hq").ops["paused"]
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    coord.allocate_pending()
    job = jobs.get(job_id)
    assert job.status == JobStatus.PENDING and job.error == "현장 정지 중"
    assert not mqtt.orders
    # 정지 중 개별 재개는 막는다
    paused_state(online, "AMR-001", PauseSource.FMS)
    assert c.post("/api/v1/robots/AMR-001/resume").status_code == 409
    # 재개: 현장 정지로 세운 로봇만 푼다
    r = c.post(BASE + "/resume")
    assert r.json()["resumed_robots"] == ["AMR-001"]
    assert kinds(mqtt, "AMR-002") == []
    assert c.post(BASE + "/resume").status_code == 409
    online("AMR-001")
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.ASSIGNED


def test_robot_coming_online_during_pause_is_paused(env):
    c, registry, _, _, mqtt, online = env
    online("AMR-001")
    c.post(BASE + "/pause", json={})
    mqtt.instants.clear()
    online("AMR-003")                       # 나중에 ONLINE
    assert kinds(mqtt, "AMR-003") == [InstantActionType.PAUSE]
    online("AMR-003")                       # 쿨다운 안에는 다시 안 보낸다
    assert kinds(mqtt, "AMR-003") == [InstantActionType.PAUSE]
    assert "AMR-003" in registry.get_site("hq").ops["robots"]


def test_pause_keeps_traffic_and_persists(env, tmp_path):
    c, registry, _, coord, _, online = env
    online()
    before = dict(coord.traffic.data["leases"])
    c.post(BASE + "/pause", json={"map_id": "f1", "reason": "점검"})
    assert coord.traffic.data["leases"] == before          # 정지는 퇴장 증거가 아니다
    from fms_server.sites import SiteRegistry
    reloaded = SiteRegistry(tmp_path).get_site("hq")
    assert reloaded.ops["paused"] and reloaded.ops["map_ids"] == ["f1"] and reloaded.paused_for("f1")
    assert not reloaded.paused_for("f2")
    assert c.post(BASE + "/pause", json={"map_id": "nope"}).status_code == 404


# -- 1.3 점검 모드 -----------------------------------------------------------

def test_service_mode_excludes_robot(env):
    c, registry, jobs, coord, mqtt, online = env
    p = move_preset(c)
    online("AMR-001")
    r = c.put(BASE + "/robots/AMR-001", json={"in_service": False, "note": "바퀴 교체"})
    assert r.status_code == 200 and r.json()["assignment"]["in_service"] is False
    view = c.get("/api/v1/robots/AMR-001").json()
    assert view["display_status"] == "service" and not view["available_for_job"] and view["note"] == "바퀴 교체"
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.PENDING and "점검" in jobs.get(job_id).error
    r = c.post(BASE + f"/jobs/{job_id}/dispatch", json={"robot_id": "AMR-001"})
    assert r.status_code == 422 and "점검" in r.json()["detail"]
    # 맵 재배정은 점검 상태를 유지한다
    registry.register_robot("hq", "AMR-001", "f1")
    assert registry.assignment_for("AMR-001").in_service is False
    c.put(BASE + "/robots/AMR-001", json={"in_service": True})
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.ASSIGNED
    assert c.get("/api/v1/robots/AMR-001").json()["display_status"] in ("idle", "working")


def test_display_status_values(env):
    c, _, _, _, _, online = env
    online("AMR-001", state=RobotState.CHARGING)
    assert c.get("/api/v1/robots/AMR-001").json()["display_status"] == "charging"
    online("AMR-001", state=RobotState.ERROR)
    assert c.get("/api/v1/robots/AMR-001").json()["display_status"] == "error"


# -- 1.4 대피 ----------------------------------------------------------------

def test_evacuation_pause_mode(env):
    c, registry, jobs, coord, mqtt, online = env
    p = move_preset(c)
    online("AMR-001")
    r = c.post(BASE + "/evacuation", json={"active": True, "reason": "화재 경보"})
    assert r.status_code == 200 and r.json()["paused_robots"] == ["AMR-001"]
    assert c.post(BASE + "/evacuation", json={"active": True}).status_code == 409
    assert c.post(BASE + "/resume").status_code == 409                     # 대피 해제 전에는 재개 불가
    assert c.post(BASE + "/pause", json={}).status_code == 409
    paused_state(online, "AMR-001")
    assert c.post("/api/v1/robots/AMR-001/resume").status_code == 409
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    coord.allocate_pending()
    assert jobs.get(job_id).error == "대피 중"
    assert c.post(BASE + f"/jobs/{job_id}/dispatch", json={"robot_id": "AMR-001"}).status_code == 409
    r = c.post(BASE + "/evacuation", json={"active": False})
    assert r.json()["resumed_robots"] == ["AMR-001"]
    assert not registry.get_site("hq").evacuating
    assert c.post(BASE + "/evacuation", json={"active": False}).status_code == 409


def test_evacuation_pullover_cancels_and_parks(env):
    c, registry, jobs, coord, mqtt, online = env
    registry.location_store("hq").create("park-1", "f1", 8, 8, 0, "parking")
    p = move_preset(c)
    online("AMR-001")
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={"robot_id": "AMR-001"}).json()["job_id"]
    online("AMR-001", state=RobotState.BUSY, order_id=job_id)
    r = c.post(BASE + "/evacuation", json={"active": True, "mode": "pullover"})
    assert r.json()["canceled_jobs"] == [job_id]
    assert jobs.get(job_id).status == JobStatus.CANCELING
    online("AMR-001")                                       # 취소 확인 -> IDLE
    assert jobs.get(job_id).status == JobStatus.CANCELED
    app.state.battery.tick()
    system = [j for j in jobs.list("hq") if j.system == "evacuation"]
    assert len(system) == 1 and system[0].commands[0].target == "park-1" and system[0].status == JobStatus.ASSIGNED
    app.state.battery.tick()                                # 같은 로봇에 또 만들지 않는다
    assert len([j for j in jobs.list("hq") if j.system == "evacuation"]) == 1
    # 해제 후 취소된 작업은 자동 재게시하지 않고 목록만 준다
    r = c.post(BASE + "/evacuation", json={"active": False})
    assert r.json()["canceled_jobs"] == [job_id]
    assert jobs.get(job_id).status == JobStatus.CANCELED
    assert len(jobs.list("hq")) == 2


# -- 1.5 재시도 --------------------------------------------------------------

def test_retry_creates_new_pending_job(env):
    c, _, jobs, coord, _, online = env
    p = move_preset(c)
    online()
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={"priority": 5}).json()["job_id"]
    assert c.post(BASE + f"/jobs/{job_id}/retry").status_code == 409        # 아직 끝나지 않았다
    c.post(BASE + f"/jobs/{job_id}/cancel")
    r = c.post(BASE + f"/jobs/{job_id}/retry")
    assert r.status_code == 200
    new = jobs.get(r.json()["job_id"])
    assert new.retry_of == job_id and new.status == JobStatus.PENDING and new.auto_assign
    assert new.priority == 5 and [x.to_dict() for x in new.commands] == [x.to_dict() for x in jobs.get(job_id).commands]
    child = jobs.create("hq", [{"type": "move", "target": "a1"}], external_owner="locker-1")
    coord.cancel(child.job_id)
    assert c.post(BASE + f"/jobs/{child.job_id}/retry").status_code == 409  # 배송은 보관소 복귀 절차
