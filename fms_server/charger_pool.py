"""충전소·대기(주차) 위치 고르기. 자동 충전·대기 복귀(`battery.py`)가 쓴다.

호출은 `JobCoordinator.lock` 안에서 한다 (작업 목록과 로봇 위치를 같이 본다).
"""
from __future__ import annotations

import json
import math
from typing import Optional

DOCK_UNDOCK_DISTANCE = 0.6   # 충전기·주차 진입 직선 거리 (m). 로봇은 이만큼 떨어진 진입 지점까지 경로계획으로 간다
OCCUPIED_RADIUS = 0.5        # 이 거리 안에 다른 로봇이 있으면 그 자리는 차 있다고 본다 (m)

_MODELS: dict[tuple, object] = {}


def map_model(store, site_id: str, map_id: str):
    """활성 버전 MapModel. `(site, map, version)` 키로 캐시한다 (맵 편집은 새 버전을 만든다)."""
    version = store.active_version(map_id)
    key = (str(store.root), site_id, map_id, version)
    model = _MODELS.get(key)
    if model is None:
        model = store.load_map_model(map_id, version)
        if len(_MODELS) > 16:
            _MODELS.clear()
        _MODELS[key] = model
    return model


def _approach_free(model, x, y, theta, entry) -> bool:
    sign = -1.0 if entry == "FORWARD" else 1.0
    ax = x + sign * DOCK_UNDOCK_DISTANCE * math.cos(theta)
    ay = y + sign * DOCK_UNDOCK_DISTANCE * math.sin(theta)
    try:
        return bool(model.in_bounds(ax, ay) and model.is_free(ax, ay))
    except Exception:
        return False


def chargers(registry, site_id: str) -> list[dict]:
    """도킹 가능한 충전소: type=charger 목적지 중 맵 chargers.json 의 물리 충전기와 위치·방향이 맞는 것.

    approach_free: 진입 지점(FORWARD 면 충전기 뒤쪽, BACKWARD 면 앞쪽 DOCK_UNDOCK_DISTANCE)이 주행 가능한 셀인지.
    막혀 있으면 로봇이 NO_PATH 로 실패하므로 고르지 않는다 (보통 진입 방향 설정이 반대인 경우).
    """
    from fms_server.mapkit.chargers import read_chargers
    maps, locations = registry.map_store(site_id), registry.location_store(site_id)
    physical, models = {}, {}
    out = []
    for loc in locations.list():
        if loc.type != "charger":
            continue
        if loc.map_id not in physical:
            try:
                physical[loc.map_id] = read_chargers(json.loads(maps.file_path(loc.map_id, "chargers.json").read_text()))
            except Exception:
                physical[loc.map_id] = []
        for c in physical[loc.map_id]:
            delta = math.atan2(math.sin(c.theta - loc.theta), math.cos(c.theta - loc.theta))
            if math.hypot(c.x - loc.x, c.y - loc.y) <= .3 and abs(delta) <= .35:
                entry = c.entry or "FORWARD"
                try:
                    if loc.map_id not in models:
                        models[loc.map_id] = maps.load_map_model(loc.map_id)
                    free = _approach_free(models[loc.map_id], loc.x, loc.y, loc.theta, entry)
                except Exception:
                    free = False
                out.append({"name": loc.name, "map_id": loc.map_id, "x": loc.x, "y": loc.y, "theta": loc.theta,
                            "entry": entry, "approach_free": free, "groups": list(getattr(loc, "groups", []) or [])})
                break
    return out


def parkings(registry, site_id: str) -> list[dict]:
    """대기 위치: type=parking 목적지. entry 는 진입 지점이 열린 쪽 (둘 다 막히면 None = 단순 이동)."""
    maps, locations = registry.map_store(site_id), registry.location_store(site_id)
    models, out = {}, []
    for loc in locations.list():
        if loc.type != "parking":
            continue
        entry = None
        try:
            if loc.map_id not in models:
                models[loc.map_id] = maps.load_map_model(loc.map_id)
            entry = next((e for e in ("FORWARD", "BACKWARD")
                          if _approach_free(models[loc.map_id], loc.x, loc.y, loc.theta, e)), None)
        except Exception:
            entry = None
        out.append({"name": loc.name, "map_id": loc.map_id, "x": loc.x, "y": loc.y, "theta": loc.theta,
                    "entry": entry, "groups": list(getattr(loc, "groups", []) or [])})
    return out


def targeted(jobs, site_id: str, robot_id: Optional[str], kinds=("docking",)) -> set[str]:
    """다른 로봇의 진행 중 작업이 향하는 목적지 이름 (도킹·주차 자리 선점 확인)."""
    busy = set()
    for job in jobs.list(site_id):
        if job.robot_id != robot_id and not job.status.terminal:
            busy.update(c.target for c in job.commands if getattr(c, "kind", None) in kinds and c.target)
    return busy


def occupied(fleet, registry, spot: dict, robot_id: Optional[str]) -> bool:
    """다른 로봇이 그 자리에 서 있나."""
    for record in fleet.all():
        a = registry.assignment_for(record.robot_id)
        if record.robot_id == robot_id or record.state is None or not a or a.map_id != spot["map_id"]:
            continue
        pose = record.state.pose
        if math.hypot(pose.x - spot["x"], pose.y - spot["y"]) < OCCUPIED_RADIUS:
            return True
    return False


def at_spot(spots: list[dict], map_id: str, x: float, y: float) -> Optional[dict]:
    return next((s for s in spots if s["map_id"] == map_id
                 and math.hypot(s["x"] - x, s["y"] - y) < OCCUPIED_RADIUS), None)


def group_ok(spot: dict, robot_groups) -> bool:
    """자리의 그룹이 비었거나 로봇 그룹과 겹칠 때만 쓴다."""
    return not spot.get("groups") or bool(set(spot["groups"]) & set(robot_groups or []))


def sort_by_distance(registry, site_id: str, spots: list[dict], origin_map: str, origin_xy) -> list[dict]:
    """같은 층을 먼저, 그 안에서는 주행 거리(`route.path_distances`) 순. 계산할 수 없으면 직선거리."""
    from fms_server.route import path_distances
    same = [s for s in spots if s["map_id"] == origin_map]
    distances = {}
    if same:
        try:
            model = map_model(registry.map_store(site_id), site_id, origin_map)
            values = path_distances(model, origin_xy, [(s["x"], s["y"]) for s in same])
            distances = {s["name"]: v for s, v in zip(same, values) if math.isfinite(v)}
        except Exception:
            distances = {}
    def key(s):
        straight = math.hypot(s["x"] - origin_xy[0], s["y"] - origin_xy[1])
        return (s["map_id"] != origin_map, s["name"] not in distances and bool(distances),
                distances.get(s["name"], straight), s["name"])
    return sorted(spots, key=key)


def free_chargers(registry, fleet, jobs, site_id: str, robot_id: Optional[str], *,
                  robot_groups=None, extra_busy=(), pool: Optional[list[dict]] = None) -> list[dict]:
    busy = targeted(jobs, site_id, robot_id) | set(extra_busy)
    return [c for c in (chargers(registry, site_id) if pool is None else pool)
            if c["approach_free"] and c["name"] not in busy and group_ok(c, robot_groups)
            and not occupied(fleet, registry, c, robot_id)]


def free_parkings(registry, fleet, jobs, site_id: str, robot_id: Optional[str], *,
                  robot_groups=None, map_id: Optional[str] = None) -> list[dict]:
    busy = targeted(jobs, site_id, robot_id, kinds=("parking", "move"))
    return [p for p in parkings(registry, site_id)
            if (map_id is None or p["map_id"] == map_id) and p["name"] not in busy
            and group_ok(p, robot_groups) and not occupied(fleet, registry, p, robot_id)]
