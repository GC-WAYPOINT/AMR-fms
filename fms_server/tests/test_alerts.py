"""4단계 — 알림 규칙 (열림·중복 방지·해소·확인)과 실시간 스트림."""
from __future__ import annotations

import time

import pytest

from common import elevator_proto as P
from common.schemas import ConnectionState, ErrorInfo, ErrorLevel, RobotState
from fms_server.alerts import OFFLINE_AFTER, PENDING_AFTER, AlertManager
from fms_server.events import EventLog
from fms_server.main import app
from fms_server.tests.test_presets_allocator import BASE, env, preset  # noqa: F401  (fixture)


@pytest.fixture
def alerts(env, tmp_path):
    c, registry, jobs, coord, mqtt, online = env
    now = [time.time()]
    # 세션이 공유하는 events.db 의 알림이 섞이지 않게 테스트마다 새 로그를 쓴다.
    events = EventLog(tmp_path / "alerts")
    app.state.events = coord._events = events
    manager = AlertManager(events, coord, battery=app.state.battery, clock=lambda: now[0])
    app.state.alerts = manager
    yield manager, now
    events.stop()


def keys(manager):
    app.state.events.flush()
    return sorted(a["key"] for a in manager.list(site="hq"))


def test_robot_error_opens_once_and_resolves(env, alerts):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = alerts
    online("AMR-001")
    manager.tick()
    assert keys(manager) == []
    state = online("AMR-001", state=RobotState.ERROR)
    app.state.fleet.apply_state(state.model_copy(update={
        "errors": [ErrorInfo(error_type="MOTOR_FAULT", level=ErrorLevel.FATAL)]}))
    manager.tick()
    manager.tick()
    assert keys(manager) == ["robot_error:AMR-001"]
    alert = manager.list(site="hq")[0]
    assert alert["severity"] == "error" and "MOTOR_FAULT" in alert["message"] and alert["data"]["robot_id"] == "AMR-001"
    # 확인해도 조건이 남아 있으면 열려 있다
    acked = c.post(f"/api/v1/alerts/{alert['id']}/ack", headers={"X-FMS-Actor": "kim"}).json()
    assert acked["acked_by"] == "kim" and acked["open"]
    online("AMR-001")
    manager.tick()
    assert keys(manager) == []
    app.state.events.flush()
    kinds = [e["kind"] for e in app.state.events.query(kind="alert.", ascending=True)]
    assert kinds == ["alert.opened", "alert.acked", "alert.resolved"]
    assert c.get("/api/v1/alerts", params={"site": "hq", "open": False}).json()["count"] == 1
    assert c.post("/api/v1/alerts/99999/ack").status_code == 404


def test_battery_offline_and_pending_rules(env, alerts):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = alerts
    online("AMR-001", battery=5.0)
    online("AMR-002")
    manager.tick()
    assert keys(manager) == ["battery_critical:AMR-001"]
    app.state.fleet.apply_connection("AMR-002", ConnectionState.CONNECTION_BROKEN)
    manager.tick()
    assert "robot_offline:AMR-002" not in keys(manager)          # 잠깐 끊긴 것은 알리지 않는다
    now[0] += OFFLINE_AFTER + 1
    manager.tick()
    assert "robot_offline:AMR-002" in keys(manager)
    p = preset(c, [{"type": "move", "target": "a1"}])
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    manager.tick()
    assert f"job_pending:{job_id}" not in keys(manager)
    now[0] += PENDING_AFTER + 1
    manager.tick()
    assert f"job_pending:{job_id}" in keys(manager)
    c.post(BASE + f"/jobs/{job_id}/cancel")
    manager.tick()
    assert f"job_pending:{job_id}" not in keys(manager)


def test_job_failure_is_an_event_alert_closed_by_ack(env, alerts):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = alerts
    app.state.events.record("job.failed", site_id="hq", job_id="hq-J0009", robot_id="AMR-001", error="NO_PATH")
    app.state.events.record("job.failed", site_id="hq", job_id="hq-J0009", robot_id="AMR-001", error="NO_PATH")
    app.state.events.record("job.deadline_missed", site_id="hq", job_id="hq-J0010", deadline="2026-01-01T00:00:00+00:00")
    assert keys(manager) == ["deadline_missed:hq-J0010", "job_failed:hq-J0009"]
    manager.tick()                                               # 상태 규칙 평가가 사건 알림을 닫지 않는다
    assert len(keys(manager)) == 2
    failed = next(a for a in manager.list(site="hq") if a["rule"] == "job_failed")
    assert manager.ack(failed["id"], "kim")["open"] is False
    assert keys(manager) == ["deadline_missed:hq-J0010"]


def test_deadlock_and_elevator_alerts(env, alerts):
    c, registry, jobs, coord, mqtt, online = env
    manager, now = alerts
    coord.traffic.analysis["deadlocks"] = [{"robots": ["AMR-001", "AMR-002"], "resources": [], "site_id": "hq"}]
    manager.tick()
    assert keys(manager) == ["deadlock:AMR-001+AMR-002"]
    coord.traffic.analysis["deadlocks"] = []
    manager.tick()
    assert keys(manager) == []


def test_event_stream_websocket(env):
    c, *_ = env
    with c.websocket_connect("/api/v1/events/stream?site=hq") as ws:
        app.state.events.record("job.success", site_id="other", job_id="x")
        app.state.events.record("robot.usage", site_id="hq", robot_id="AMR-001", seconds=1)
        app.state.events.record("job.failed", site_id="hq", job_id="hq-J0001", error="boom")
        message = ws.receive_json()
        assert message["type"] == "event" and message["event"]["kind"] == "job.failed"
