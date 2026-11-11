"""이벤트 로그 — 기록·조회·보존, 워커 flush, 코디네이터 연동."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from common.schemas import ConnectionState, RobotState
from fms_server.events import EventLog
from fms_server.fleet import Fleet
from fms_server.robot_events import RobotEventRecorder
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobStatus, JobStore
from fms_server.tests.factories import make_state
from fms_server.tests.test_task import FakeMqtt, _vendor_files
from fms_server.mapkit.store import compute_map_uid


@pytest.fixture
def log(tmp_path):
    events = EventLog(tmp_path)
    yield events
    events.stop()


def test_record_and_query_filters(log):
    log.record("job.assigned", site_id="hq", robot_id="AMR-001", job_id="hq-J0001", nodes=2)
    log.record("job.success", site_id="hq", robot_id="AMR-001", job_id="hq-J0001")
    log.record("robot.error", site_id="hq", robot_id="AMR-002", severity="error", error_type="MOTOR")
    log.record("job.assigned", site_id="b", robot_id="AMR-009", job_id="b-J0001")
    rows = log.query(site="hq")
    assert [r["kind"] for r in rows] == ["robot.error", "job.success", "job.assigned"]  # 최신 순
    assert rows[2]["data"] == {"nodes": 2}
    assert [r["kind"] for r in log.query(job="hq-J0001", ascending=True)] == ["job.assigned", "job.success"]
    assert len(log.query(robot="AMR-002")) == 1
    assert len(log.query(kind="job.")) == 3
    assert len(log.query(kind="job.success,robot.error")) == 2
    assert len(log.query(severity="error")) == 1
    assert len(log.query(limit=1)) == 1


def test_since_until_and_after_id(log):
    log.record("a", ts=100.0)
    log.record("b", ts=200.0)
    log.record("c", ts=300.0)
    assert [r["kind"] for r in log.query(since=150, until=300)] == ["b"]
    first = log.query(ascending=True)[0]["id"]
    assert [r["kind"] for r in log.query(after_id=first, ascending=True)] == ["b", "c"]


def test_retention_purge(tmp_path):
    now = [1_000_000.0]
    log = EventLog(tmp_path, retention_days=30, clock=lambda: now[0])
    log.record("old", ts=now[0] - 31 * 86400)
    log.record("new", ts=now[0] - 86400)
    log.flush()
    assert log.purge() == 1
    assert [r["kind"] for r in log.query()] == ["new"]
    log.stop()


def test_worker_thread_commits_and_stop_flushes(tmp_path):
    log = EventLog(tmp_path)
    seen = []
    log.add_listener(seen.append)
    log.start()
    log.record("x", site_id="hq")
    deadline = time.time() + 3
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    assert seen and seen[0]["kind"] == "x" and seen[0]["id"] >= 1
    log.record("y")
    log.stop()   # 남은 것을 쓰고 닫는다
    reopened = EventLog(tmp_path)
    assert [r["kind"] for r in reopened.query(ascending=True)] == ["x", "y"]
    reopened.stop()


def test_listener_failure_does_not_stop_log(log):
    def broken(_event):
        raise RuntimeError("boom")
    log.add_listener(broken)
    log.record("x")
    log.flush()
    assert len(log.query()) == 1


def _coord(tmp_path, events):
    registry = SiteRegistry(tmp_path)
    registry.create_site("hq", "본사")
    version = registry.map_store("hq").ingest_upload("floor-1", _vendor_files(), activate=True).version
    registry.register_robot("hq", "AMR-001", "floor-1")
    registry.location_store("hq").create("dest", "floor-1", 3.0, 4.0, 0.0)
    fleet, mqtt, store = Fleet(stale_after=3.0), FakeMqtt(), JobStore(tmp_path)
    coord = JobCoordinator(fleet, registry, store, mqtt, events=events)
    coord.start()

    def online(state=RobotState.IDLE, order_id=None, **kw):
        fleet.apply_connection("AMR-001", ConnectionState.ONLINE)
        fleet.apply_state(make_state("AMR-001", state, order_id=order_id,
                                     map_id=compute_map_uid("hq", "floor-1"), map_version=version, **kw))
    online()
    return registry, fleet, store, coord, online


def test_job_lifecycle_is_recorded(tmp_path, log):
    _, _, store, coord, online = _coord(tmp_path, log)
    job = store.create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    coord.dispatch(job.job_id)
    online(RobotState.BUSY, order_id=job.job_id)
    online(RobotState.IDLE)
    assert store.get(job.job_id).status == JobStatus.SUCCESS
    kinds = [r["kind"] for r in log.query(job=job.job_id, ascending=True)]
    assert kinds == ["job.assigned", "job.working", "job.success"]
    assert all(r["site_id"] == "hq" and r["robot_id"] == "AMR-001" for r in log.query(job=job.job_id))
    coord.stop()


def test_cancel_records_actor(tmp_path, log):
    _, _, store, coord, _ = _coord(tmp_path, log)
    job = store.create("hq", [{"type": "move", "target": "dest"}])
    coord.cancel(job.job_id, actor="kim")
    rows = log.query(job=job.job_id, ascending=True)
    assert [r["kind"] for r in rows] == ["job.cancel_requested", "job.canceled"]
    assert rows[0]["actor"] == "kim"
    coord.stop()


def test_coordinator_without_events_behaves_the_same(tmp_path):
    _, _, store, coord, online = _coord(tmp_path, None)
    job = store.create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    coord.dispatch(job.job_id)
    online(RobotState.BUSY, order_id=job.job_id)
    online(RobotState.IDLE)
    assert store.get(job.job_id).status == JobStatus.SUCCESS
    coord.stop()


def test_robot_changes_only(tmp_path, log):
    registry, fleet, _, coord, online = _coord(tmp_path, None)
    now = [1000.0]
    recorder = RobotEventRecorder(fleet, registry, log, clock=lambda: now[0])
    recorder.start()
    online()
    online()                      # 같은 상태 반복 -> 기록 없음
    assert [r["kind"] for r in log.query(ascending=True)] == ["robot.liveness"]
    online(RobotState.BUSY, order_id="o1", x=2.0)   # 1 m 이동 + 상태 변화
    online(RobotState.BUSY, order_id="o1", x=2.5, battery=79.0)
    kinds = [r["kind"] for r in log.query(ascending=True)]
    assert kinds.count("robot.state") == 1 and kinds.count("robot.battery") == 1
    now[0] += 61
    recorder.tick()
    usage = log.query(kind="robot.usage", ascending=True)
    assert usage[0]["data"]["status"] == "idle"          # BUSY 로 바뀌는 순간 idle 구간을 닫는다
    assert usage[-1]["data"]["status"] == "working" and usage[-1]["data"]["seconds"] == 61
    assert sum(u["data"]["distance"] for u in usage) == pytest.approx(1.5)
    recorder.stop()
    coord.stop()


def test_events_api(tmp_path):
    from fms_server.main import app
    with TestClient(app) as c:
        # events.db 는 테스트 세션이 같이 쓴다. 다른 테스트와 겹치지 않는 현장 ID 로 센다.
        site = "events-api-only"
        app.state.events.record("job.success", site_id=site, job_id="hq-J0001")
        app.state.events.record("robot.error", site_id=site, robot_id="AMR-001", severity="error")
        body = c.get("/api/v1/events", params={"site": site, "kind": "job."}).json()
        assert body["count"] == 1 and body["events"][0]["job_id"] == "hq-J0001"
        assert c.get("/api/v1/events", params={"limit": 0}).status_code == 422
