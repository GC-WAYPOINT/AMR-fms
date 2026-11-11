"""2단계 — 예약 실행 (시각 주입으로 실행·중복 방지·놓친 실행 건너뜀)."""
from __future__ import annotations

from datetime import datetime

import pytest

from fms_server.main import app
from fms_server.scheduler import ScheduleStore, Scheduler, due
from fms_server.task import JobStatus
from fms_server.tests.test_presets_allocator import BASE, env, preset  # noqa: F401  (fixture)


def at(hour, minute, second=0, day=2):
    return datetime(2026, 11, day, hour, minute, second).timestamp()    # 2026-11-02 = 월요일


def test_due_time_slot():
    row = {"enabled": True, "days": [0], "time": "09:00", "interval_s": None, "last_run": None}
    assert due(row, at(8, 59, 59)) is None
    assert due(row, at(9, 0, 5)) == at(9, 0)
    assert due(row, at(9, 5)) is None                       # 놓친 실행은 건너뛴다
    assert due(dict(row, last_run=at(9, 0, 1)), at(9, 0, 30)) is None   # 같은 회차 중복 방지
    assert due(row, at(9, 0, 5, day=3)) is None             # 화요일
    assert due(dict(row, enabled=False), at(9, 0, 5)) is None


def test_due_interval():
    row = {"enabled": True, "days": list(range(7)), "time": None, "interval_s": 600,
           "last_run": None, "created_at": at(9, 0)}
    assert due(row, at(9, 5)) is None                       # 만들자마자 실행하지 않는다
    assert due(row, at(9, 10)) == at(9, 10)
    assert due(dict(row, last_run=at(9, 10)), at(9, 15)) is None
    assert due(dict(row, last_run=at(9, 10)), at(9, 20)) is not None


@pytest.fixture
def sched(env, tmp_path):
    c, registry, jobs, coord, mqtt, online = env
    now = [at(8, 0)]
    store = ScheduleStore(tmp_path)
    scheduler = Scheduler(store, app.state.presets, registry, jobs, coord,
                          events=app.state.events, clock=lambda: now[0])
    app.state.schedules = store
    return store, scheduler, now


def test_schedule_publishes_once_and_survives_restart(env, sched, tmp_path):
    c, registry, jobs, coord, mqtt, online = env
    store, scheduler, now = sched
    p = preset(c, [{"type": "move", "target": "a1"}])
    r = c.post(BASE + "/schedules", json={"name": "아침 순회", "preset_id": p["preset_id"], "time": "9:00",
                                          "days": [0], "priority": 4, "robot_group": "cart"})
    assert r.status_code == 200 and r.json()["time"] == "09:00"
    assert scheduler.tick() == []
    now[0] = at(9, 0, 2)
    created = scheduler.tick()
    assert len(created) == 1
    job = jobs.get(created[0])
    assert job.status == JobStatus.PENDING and job.auto_assign and job.priority == 4 and job.robot_group == "cart"
    assert scheduler.tick() == []
    # 재시작: 파일의 last_run 으로 같은 회차를 다시 게시하지 않는다
    again = Scheduler(ScheduleStore(tmp_path), app.state.presets, registry, jobs, coord, clock=lambda: now[0])
    assert again.tick() == []
    assert app.state.events.query(kind="schedule.run", job=created[0])


def test_schedule_validation_and_failed_publish(env, sched):
    c, registry, jobs, coord, mqtt, online = env
    store, scheduler, now = sched
    p = preset(c, [{"type": "move", "target": "a1"}])
    base = {"preset_id": p["preset_id"]}
    assert c.post(BASE + "/schedules", json=base).status_code == 422                       # 시각·간격 없음
    assert c.post(BASE + "/schedules", json=dict(base, time="09:00", interval_s=60)).status_code == 422
    assert c.post(BASE + "/schedules", json=dict(base, time="25:00")).status_code == 422
    assert c.post(BASE + "/schedules", json=dict(base, interval_s=1)).status_code == 422
    assert c.post(BASE + "/schedules", json=dict(base, time="09:00", days=[9])).status_code == 422
    assert c.post(BASE + "/schedules", json={"preset_id": "nope", "time": "09:00"}).status_code == 404
    row = c.post(BASE + "/schedules", json=dict(base, interval_s=60)).json()
    c.delete(BASE + f"/presets/{p['preset_id']}")           # 프리셋이 사라진 예약
    import time
    now[0] = time.time() + 61                              # created_at 은 실제 시각으로 찍힌다
    assert scheduler.tick() == []
    assert store.get("hq", row["id"])["last_error"]
    assert c.put(BASE + f"/schedules/{row['id']}", json=dict(base, interval_s=60)).status_code == 404
    assert c.delete(BASE + f"/schedules/{row['id']}").status_code == 200
    assert c.get(BASE + "/schedules").json() == []
