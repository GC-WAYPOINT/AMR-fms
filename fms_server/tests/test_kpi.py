"""4단계 — KPI 집계."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fms_server import kpi
from fms_server.events import EventLog

T0 = 1_700_000_000.0 - (1_700_000_000.0 % 86400)     # 하루 경계


@pytest.fixture
def log(tmp_path):
    events = EventLog(tmp_path)
    yield events
    events.stop()


def fill(log):
    r = lambda kind, ts, **kw: log.record(kind, site_id="hq", ts=T0 + ts, **kw)
    r("job.assigned", 100, job_id="hq-J0001", robot_id="R1", waited=20.0)
    r("job.success", 400, job_id="hq-J0001", robot_id="R1", duration=300.0)
    r("job.assigned", 500, job_id="hq-J0002", robot_id="R2", waited=40.0)
    r("job.failed", 700, job_id="hq-J0002", robot_id="R2", duration=200.0, error="NO_PATH")
    r("job.canceled", 800, job_id="hq-J0003")
    r("job.success", 900, job_id="hq-J0004", robot_id="R1", system="charge", duration=50.0)   # 시스템 작업
    r("job.success", 950, job_id="hq-J0005", robot_id="R1", delivery="L-1", duration=50.0)    # 배송 하위 작업
    r("robot.usage", 1000, robot_id="R1", status="working", seconds=600.0, distance=120.5)
    r("robot.usage", 1000, robot_id="R1", status="idle", seconds=200.0, distance=0.0)
    r("robot.usage", 1000, robot_id="R2", status="charging", seconds=400.0, distance=3.0)
    r("robot.usage", 1000, robot_id="R2", status="offline", seconds=999.0, distance=0.0)
    r("robot.charging_started", 1100, robot_id="R2")
    r("traffic.wait_end", 1200, robot_id="R1", seconds=12.0)
    r("traffic.wait_end", 1300, robot_id="R2", seconds=8.0)
    r("delivery.phase", 1400, job_id="L-1", phase="queued")
    r("delivery.phase", 2000, job_id="L-1", phase="done")
    r("job.success", 3600 + 10, job_id="hq-J0006", robot_id="R1", duration=100.0)             # 다음 시간대
    log.record("elevator.lease", ts=T0 + 1500, job_id="hq-J0001", elevator_id="e", waited=30.0)   # site 없음
    log.record("job.success", site_id="other", ts=T0 + 100, job_id="other-J0001", duration=1.0)


def test_kpi_totals_buckets_and_robots(log):
    fill(log)
    out = kpi.compute(log, "hq", T0, T0 + 7200, "hour")
    total = out["totals"]
    assert (total["completed"], total["failed"], total["canceled"], total["system_jobs"]) == (2, 1, 1, 1)
    assert total["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert total["assign_wait_avg"] == 30.0 and total["duration_avg"] == 200.0
    assert total["distance"] == 123.5 and total["charges"] == 1 and total["charge_seconds"] == 400.0
    assert total["utilization"] == pytest.approx(600 / 1200)          # offline 시간은 분모에서 뺀다
    assert total["traffic_wait_avg"] == 10.0 and total["traffic_waits"] == 2
    assert total["elevator_wait_avg"] == 30.0
    assert total["deliveries"] == 1 and total["delivery_lead_avg"] == 600.0
    assert [b["completed"] for b in out["buckets"]] == [1, 1]
    assert out["buckets"][0]["start"] == T0 and out["buckets"][1]["start"] == T0 + 3600
    robots = {r["robot_id"]: r for r in out["robots"]}
    assert robots["R1"]["distance"] == 120.5 and robots["R1"]["utilization"] == pytest.approx(0.75)
    assert robots["R2"]["charges"] == 1 and robots["R2"]["failed"] == 1


def test_kpi_day_bucket_and_validation(log):
    fill(log)
    out = kpi.compute(log, "hq", T0, T0 + 86400, "day")
    assert len(out["buckets"]) == 1 and out["buckets"][0]["completed"] == 2
    with pytest.raises(ValueError):
        kpi.compute(log, "hq", T0, T0, "hour")
    with pytest.raises(ValueError):
        kpi.compute(log, "hq", T0, T0 + 10, "week")
    with pytest.raises(ValueError):
        kpi.compute(log, "hq", T0, T0 + 3600 * 5000, "hour")
    assert kpi.compute(log, "empty", T0, T0 + 3600)["totals"]["success_rate"] is None


def test_kpi_api(tmp_path):
    from fms_server.main import app
    from fms_server.sites import SiteRegistry
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        registry.create_site("hq", "본사")
        app.state.sites = registry
        app.state.events.record("job.success", site_id="hq", job_id="hq-J0001", duration=10.0)
        body = c.get("/api/v1/sites/hq/kpi").json()
        assert body["bucket"] == "hour" and body["totals"]["completed"] >= 1 and len(body["buckets"]) in (24, 25)
        assert c.get("/api/v1/sites/hq/kpi", params={"bucket": "year"}).status_code == 422
        assert c.get("/api/v1/sites/nope/kpi").status_code == 404
