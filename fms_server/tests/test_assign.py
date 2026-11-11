"""2단계 — 우선순위·예약 시각·마감, 비용 기반 로봇 선택, 로봇 그룹."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fms_server.assign import rank_candidates
from fms_server.main import app
from fms_server.task import JobStatus
from fms_server.tests.test_presets_allocator import BASE, env, preset  # noqa: F401  (fixture)


def publish(c, p, **body):
    r = c.post(BASE + f"/presets/{p['preset_id']}/publish", json=body)
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def to(target):
    return [{"type": "move", "target": target}]


def test_nearest_robot_wins_not_lowest_id(env):
    c, registry, jobs, coord, mqtt, online = env
    online("AMR-001", x=9.0, y=9.0)      # a1(1,1) 에서 멀다
    online("AMR-002", x=1.5, y=1.5)
    job_id = publish(c, preset(c, to("a1")))
    coord.allocate_pending()
    assert jobs.get(job_id).robot_id == "AMR-002"


def test_low_battery_breaks_distance_tie_and_id_breaks_full_tie(env):
    c, registry, jobs, coord, mqtt, online = env
    online("AMR-001", x=2.0, y=2.0, battery=30.0)
    online("AMR-002", x=2.0, y=2.0, battery=90.0)
    job = jobs.get(publish(c, preset(c, to("a1"))))
    order, _ = rank_candidates(job, app.state.fleet.available_robots(), registry)
    assert order == ["AMR-002", "AMR-001"]
    online("AMR-001", x=2.0, y=2.0, battery=90.0)
    order, _ = rank_candidates(job, app.state.fleet.available_robots(), registry)
    assert order == ["AMR-001", "AMR-002"]


def test_priority_overtakes_older_job(env):
    c, _, jobs, coord, _, online = env
    p = preset(c, to("a1"))
    first = publish(c, p)
    urgent = publish(c, p, priority=10)
    online("AMR-001")
    coord.allocate_pending()
    assert jobs.get(urgent).status == JobStatus.ASSIGNED
    assert jobs.get(first).status == JobStatus.PENDING


def test_patch_priority_only_pending(env):
    c, _, jobs, coord, _, online = env
    p = preset(c, to("a1"))
    a, b = publish(c, p), publish(c, p)
    assert c.patch(BASE + f"/jobs/{b}", json={"priority": 3}).json()["priority"] == 3
    online("AMR-001")
    coord.allocate_pending()
    assert jobs.get(b).status == JobStatus.ASSIGNED
    assert c.patch(BASE + f"/jobs/{b}", json={"priority": 1}).status_code == 409
    assert c.patch(BASE + f"/jobs/{a}", json={"priority": 1000}).status_code == 422


def test_not_before_and_deadline(env):
    c, _, jobs, coord, _, online = env
    now = datetime.now(timezone.utc)
    p = preset(c, to("a1"))
    job_id = publish(c, p, not_before=(now + timedelta(hours=1)).isoformat(),
                     deadline=(now + timedelta(hours=2)).isoformat())
    online("AMR-001")
    coord.allocate_pending()
    assert jobs.get(job_id).status == JobStatus.PENDING and jobs.get(job_id).error == "예약 시각 전"
    # 마감을 넘겨도 실패 처리하지 않고 이벤트만 남긴다
    coord.allocate_pending(now=time.time() + 3 * 3600)
    job = jobs.get(job_id)
    assert job.status == JobStatus.ASSIGNED and job.deadline_missed
    assert len(app.state.events.query(kind="job.deadline_missed", job=job_id)) == 1
    assert c.post(BASE + f"/presets/{p['preset_id']}/publish", json={"deadline": "내일"}).status_code == 422


def test_robot_group_filters_candidates(env):
    c, registry, jobs, coord, _, online = env
    online("AMR-001", x=1.0, y=1.0)
    online("AMR-002", x=9.0, y=9.0)
    r = c.put(BASE + "/robots/AMR-002", json={"groups": ["cart"]})
    assert r.json()["assignment"]["groups"] == ["cart"]
    job_id = publish(c, preset(c, to("a1")), robot_group="cart")
    coord.allocate_pending()
    assert jobs.get(job_id).robot_id == "AMR-002"         # 더 멀어도 그룹 로봇만
    other = publish(c, preset(c, to("a2")), robot_group="tow")
    online("AMR-001", x=1.0, y=1.0)
    coord.allocate_pending()
    assert jobs.get(other).status == JobStatus.PENDING and "tow" in jobs.get(other).error


def test_external_order_is_idempotent(env):
    c, _, jobs, coord, _, online = env
    body = {"commands": to("a1"), "external_id": "WMS-77", "auto_assign": True, "priority": 2}
    first = c.post(BASE + "/jobs", json=body).json()
    again = c.post(BASE + "/jobs", json=body).json()
    assert first["job_id"] == again["job_id"] and first["external_id"] == "WMS-77"
    assert len(jobs.list("hq")) == 1
    online("AMR-001")
    coord.allocate_pending()
    assert jobs.get(first["job_id"]).status == JobStatus.ASSIGNED
