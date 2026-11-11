"""5단계 — 로봇 협조 항목의 FMS 쪽: 감속 구역, Factsheet, 적재 상태 대조."""
from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError

from common.map_annotations import Point, Zone, ZoneType
from common.schemas import ActionType, Factsheet, LoadInfo
from fms_server.alerts import LOAD_MISMATCH_AFTER, AlertManager
from fms_server.events import EventLog
from fms_server.main import app
from fms_server.mqtt_client import _parse_topic
from fms_server.task import JobStatus
from fms_server.tests.test_presets_allocator import BASE, env, preset  # noqa: F401  (fixture)

SQUARE = [Point(x=0, y=0), Point(x=1, y=0), Point(x=1, y=1), Point(x=0, y=1)]
MAP = BASE + "/maps/f1"


def sheet(robot="AMR-001", **kw):
    return Factsheet(header_id=0, timestamp=time.time(), robot_id=robot, **kw)


def test_slow_zone_requires_speed_limit_and_others_reject_it():
    assert Zone(zone_id="s", type=ZoneType.SLOW, polygon=SQUARE, speed_limit=0.4).speed_limit == 0.4
    with pytest.raises(ValidationError):
        Zone(zone_id="s", type=ZoneType.SLOW, polygon=SQUARE)
    with pytest.raises(ValidationError):
        Zone(zone_id="p", type=ZoneType.PROHIBIT, polygon=SQUARE, speed_limit=0.4)


def test_slow_zone_api_writes_speed_limit_into_bundle_annotations(env):
    c, registry, *_ = env
    store = registry.map_store("hq")
    body = dict(base_version=store.active_version("f1"), name="복도", type="slow", x=2, y=2, width=2, height=1)
    assert c.post(MAP + "/zones", json=body).status_code == 422             # 속도가 없다
    r = c.post(MAP + "/zones", json=dict(body, speed_limit=0.3))
    assert r.status_code == 200, r.text
    zone = next(z for z in store.load_annotations("f1").zones if z.type == ZoneType.SLOW)
    assert zone.speed_limit == 0.3
    # 로봇이 받는 번들의 annotations.json 에 그대로 들어간다 (C++ MapModel 이 읽는다)
    raw = json.loads(store.file_path("f1", "annotations.json").read_text())
    assert [z["speed_limit"] for z in raw["zones"] if z["type"] == "slow"] == [0.3]
    # 주행금지처럼 굽지 않는다: 구역 안도 여전히 FREE
    assert store.load_map_model("f1").is_free(3.0, 2.5)
    bad = dict(body, base_version=store.active_version("f1"), type="prohibit", speed_limit=0.3, name="x", x=6)
    assert c.post(MAP + "/zones", json=bad).status_code == 422


def test_factsheet_topic_and_fleet_record(env):
    c, registry, jobs, coord, mqtt, online = env
    assert _parse_topic("fms/v1/AMR-001/factsheet") == ("AMR-001", "factsheet")
    online("AMR-001")
    app.state.fleet.apply_factsheet(sheet(model="amr-sim", trays=2, max_speed=1.2, traffic_control=True,
                                          actions=[ActionType.WAIT, ActionType.PARK]))
    record = app.state.fleet.get("AMR-001")
    assert record.factsheet.trays == 2 and record.traffic_control is True       # State 값(False) 대신 factsheet
    assert record.supports({"PARK"}) and not record.supports({"DOCK"})
    view = c.get("/api/v1/robots/AMR-001").json()
    assert view["factsheet"]["model"] == "amr-sim" and view["factsheet"]["actions"] == ["WAIT", "PARK"]


def test_candidates_are_filtered_by_supported_actions(env):
    c, registry, jobs, coord, mqtt, online = env
    online("AMR-001", x=1.0, y=1.0)
    online("AMR-002", x=9.0, y=9.0)
    app.state.fleet.apply_factsheet(sheet("AMR-001", actions=[ActionType.WAIT]))           # 도킹을 못 한다
    p = preset(c)                                                                          # 도킹이 든 프리셋
    job_id = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    coord.allocate_pending()
    assert jobs.get(job_id).robot_id == "AMR-002"                                          # 구형(미보고)은 된다고 본다
    from common.schemas import RobotState
    online("AMR-002", state=RobotState.BUSY, order_id=job_id, x=9.0, y=9.0)                # 이제 작업 중
    other = c.post(BASE + f"/presets/{p['preset_id']}/publish", json={}).json()["job_id"]
    coord.allocate_pending()
    assert jobs.get(other).status == JobStatus.PENDING and "factsheet" in jobs.get(other).error


def test_load_mismatch_alert_only_for_reporting_robots(env, tmp_path):
    c, registry, jobs, coord, mqtt, online = env
    events = EventLog(tmp_path / "ev")
    now = [time.time()]
    ledger = {"AMR-001": ["P1"], "AMR-002": ["P9"]}

    class Cargo:
        def robot_cargo(self, robot_id):
            return {"trays": [{"tray_id": "T1", "state": "loaded", "last_parcel_id": p, "parcel": {"parcel_id": p}}
                              for p in ledger.get(robot_id, [])]}

    manager = AlertManager(events, coord, cargo=Cargo(), clock=lambda: now[0])

    def report(robot, payloads):
        state = online(robot)
        app.state.fleet.apply_state(state.model_copy(update={
            "loads": [LoadInfo(tray=f"T{i + 1}", payload_id=p) for i, p in enumerate(payloads)]}))

    report("AMR-001", [])            # 장부에는 P1 이 실려 있는데 로봇은 비었다고 보고
    report("AMR-002", [])            # 보고 기능이 없는 구형 — 장부만 믿는다
    app.state.fleet.apply_factsheet(sheet("AMR-001", reports_loads=True))
    manager.tick()
    assert manager.list(site="hq") == []                       # 잠깐 어긋난 것은 알리지 않는다 (적재 중 전이)
    now[0] += LOAD_MISMATCH_AFTER + 1
    manager.tick()
    alerts = manager.list(site="hq")
    assert [a["key"] for a in alerts] == ["cargo_mismatch:AMR-001"]
    assert alerts[0]["data"]["ledger"] == ["P1"] and alerts[0]["data"]["reported"] == []
    report("AMR-001", ["P1"])
    manager.tick()
    assert manager.list(site="hq") == []
    events.stop()
