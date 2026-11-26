"""맵 전역 장애물 레지스트리. 모든 호출은 JobCoordinator.lock 안에서 수행한다.

로봇은 센서 범위 안의 동적 장애물만 알고 (`State.detected_obstacles`), 다른 로봇은 그걸 모른다.
그래서 한 로봇이 막힌 통로 앞에 서 있어도 다음 로봇이 같은 통로로 들어가 병목이 생긴다.
여기서는 로봇이 보고한 **정지 장애물**을 맵 단위로 일정 시간(기본 5분) 모아 두고, 경로 선택
(`traffic.ZonePlanner`)과 막힌 작업의 재경로에 쓴다.

- 등록: 같은 자리에서 `obstacle_confirm` 초 동안 계속 보인 장애물만. 움직이는 장애물과 다른 로봇의
  몸체는 올리지 않는다.
- 유지: 다시 보고되면 `obstacle_ttl` 을 새로 센다.
- 해제: 아무 로봇이든 장애물에서 `obstacle_view_range` 안으로 지나가는데 그 자리의 장애물을 보고하지
  않으면 즉시 지운다. 시간이 지나도 지운다.

메모리에만 둔다 (서버를 다시 띄우면 비운다 — 다시 보고되면 채워진다).
"""
from __future__ import annotations

import math
import time

CELL = 0.02         # 확인 중인 후보를 묶는 격자 크기 (m). 장애물 한 줄(칸 하나 ≈ 0.09 m)이 하나로 뭉치지 않게 작게 둔다
SAME_SPOT = 0.04    # 보고된 장애물이 등록된 것과 같은 자리라고 보는 거리 (m)
SEEN_MATCH = 0.3    # 재경로 판단: 로봇이 지금 보고하는 장애물이 등록 항목과 같은 것이라고 보는 중심 거리 (m)
BLOCK_REACH = 3.0   # 재경로 판단: 로봇 앞 경로 이 길이(m) 안에 있어야 "길을 막고 있다"고 본다
PATH_STEP = 0.1     # 경로를 따라 장애물과의 거리를 재는 간격 (m)


def _cell(x, y):
    return round(x / CELL), round(y / CELL)


def _rect_distance(px, py, e):
    dx = max(abs(px - e["x"]) - e["width"] / 2, 0.0)
    dy = max(abs(py - e["y"]) - e["height"] / 2, 0.0)
    return math.hypot(dx, dy)


def _path_hits(points, e, radius, reach):
    """points 를 따라 처음 reach m 안에서 차체(반경 radius)가 사각형 e 에 닿는가."""
    if not points:
        return False
    if _rect_distance(points[0][0], points[0][1], e) <= radius:
        return True
    travelled = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        length = math.hypot(bx - ax, by - ay)
        steps = max(1, int(length / PATH_STEP))
        for i in range(1, steps + 1):
            if travelled + length * i / steps > reach:
                return False
            px, py = ax + (bx - ax) * i / steps, ay + (by - ay) * i / steps
            if _rect_distance(px, py, e) <= radius:
                return True
        travelled += length
    return False


class ObstacleRegistry:
    def __init__(self, registry, fleet, events=None, settings=None):
        self.registry, self.fleet, self.events = registry, fleet, events
        if settings is None:
            from fms_server.config import settings
        self.settings = settings
        self._entries = {}      # 맵 키 -> {cell: entry}
        self._candidates = {}   # (맵 키, cell) -> entry (아직 확인 중)

    # -- 조회 -------------------------------------------------------------

    def rects(self, site, map_id):
        """경로 계획에 넘길 [(x, y, width, height)]."""
        return [(e["x"], e["y"], e["width"], e["height"])
                for e in self._entries.get(f"{site}/{map_id}", {}).values()]

    def blocking(self, site, map_id, state, radius, paths, reach=BLOCK_REACH):
        """재경로 판단용: 등록 장애물 중 (1) 이 로봇이 지금 센서로 보고 있고 (2) 남은 경로 앞 reach m 안에서
        차체에 닿는 것. 로봇끼리 막힘(몸체는 등록하지 않음)이나 허가를 받고도 선 로봇(장애물을 안 봄)은 빈 목록이다."""
        out = []
        for e in self._entries.get(f"{site}/{map_id}", {}).values():
            if not any(math.hypot(o.x - e["x"], o.y - e["y"]) <= SEEN_MATCH for o in state.detected_obstacles):
                continue
            if any(_path_hits(path, e, radius, reach) for path in paths):
                out.append((e["x"], e["y"], e["width"], e["height"]))
        return out

    def snapshot(self, site, map_id, now=None):
        now = time.time() if now is None else now
        return [{"x": e["x"], "y": e["y"], "width": e["width"], "height": e["height"], "robot_id": e["robot"],
                 "since": e["first"], "expires_in": round(max(0.0, e["expires"] - now), 1)}
                for e in self._entries.get(f"{site}/{map_id}", {}).values()]

    # -- 갱신 (1초마다) -----------------------------------------------------

    def _robots_by_map(self):
        out = {}
        for record in self.fleet.all():
            if record.state is None or not record.state.map_id:
                continue
            if record.liveness(self.fleet.stale_after).value != "ONLINE":
                continue
            assignment = self.registry.assignment_for(record.robot_id)
            if assignment is None:
                continue
            try:
                uid = self.registry.map_store(assignment.site_id).map_uid(assignment.map_id)
            except Exception:
                continue
            if uid != record.state.map_id:
                continue
            out.setdefault(f"{assignment.site_id}/{assignment.map_id}", []).append(record)
        return out

    @staticmethod
    def _is_robot_body(rect, robots, skip):
        """다른 로봇의 몸체가 장애물로 보고된 것이다. 올리지 않는다."""
        x, y, w, h = rect
        for r in robots:
            if r.robot_id == skip:
                continue
            reach = math.hypot(r.state.width, r.state.length) / 2 + max(w, h) / 2 + 0.1
            if math.hypot(r.state.pose.x - x, r.state.pose.y - y) <= reach:
                return True
        return False

    def update(self, now=None):
        now = time.time() if now is None else now
        cfg = self.settings
        by_map = self._robots_by_map()
        refreshed = set()   # 이번 tick 에 보고된 등록 항목 (맵 키, cell)
        registered = {}     # 맵 키 -> 이번에 새로 등록한 수

        for key, robots in by_map.items():
            entries = self._entries.setdefault(key, {})
            for r in robots:
                for o in r.state.detected_obstacles:
                    rect = (o.x, o.y, o.width, o.height)
                    if self._is_robot_body(rect, robots, r.robot_id):
                        continue
                    cell = _cell(o.x, o.y)
                    hit = next((c for c, e in entries.items()
                                if math.hypot(e["x"] - o.x, e["y"] - o.y) <= SAME_SPOT), None)
                    if hit is not None:
                        entries[hit]["expires"] = now + cfg.obstacle_ttl
                        refreshed.add((key, hit))
                        continue
                    cand = self._candidates.get((key, cell))
                    if cand is None:
                        cand = self._candidates[(key, cell)] = {"x": o.x, "y": o.y, "width": o.width,
                                                                "height": o.height, "robot": r.robot_id,
                                                                "first": now, "last": now, "seen": now}
                    cand["seen"] = now
                    if now - cand["first"] >= cfg.obstacle_confirm:
                        entries[cell] = {"x": cand["x"], "y": cand["y"], "width": cand["width"],
                                         "height": cand["height"], "robot": r.robot_id, "first": now,
                                         "expires": now + cfg.obstacle_ttl}
                        refreshed.add((key, cell))
                        del self._candidates[(key, cell)]
                        registered[key] = registered.get(key, 0) + 1
        # 끊겨서 보이지 않는 후보는 버린다 (움직이는 장애물은 칸이 계속 바뀌어 여기서 걸러진다).
        for ck in [ck for ck, c in self._candidates.items() if now - c["seen"] > 2.0]:
            del self._candidates[ck]

        for key, count in registered.items():
            site = key.split("/", 1)[0]
            mine = self._entries[key]
            self._record("obstacle.register", site, key, count=count,
                         robot_id=next((e["robot"] for e in mine.values() if e["first"] == now), None),
                         total=len(mine), ttl=cfg.obstacle_ttl)

        cleared = {}
        for key, entries in self._entries.items():
            robots = by_map.get(key, [])
            for cell, e in list(entries.items()):
                if (key, cell) in refreshed:
                    continue
                reason = None
                by = None
                if now >= e["expires"]:
                    reason = "expired"
                else:
                    for r in robots:
                        near = math.hypot(r.state.pose.x - e["x"], r.state.pose.y - e["y"]) <= cfg.obstacle_view_range
                        if near and not any(math.hypot(o.x - e["x"], o.y - e["y"]) <= SAME_SPOT
                                            for o in r.state.detected_obstacles):
                            reason, by = "not_seen", r.robot_id
                            break
                if reason:
                    del entries[cell]
                    bucket = cleared.setdefault((key, reason, by), 0)
                    cleared[(key, reason, by)] = bucket + 1
        for (key, reason, by), count in cleared.items():
            self._record("obstacle.clear", key.split("/", 1)[0], key, count=count, reason=reason, robot_id=by)

    def _record(self, kind, site, key, **data):
        if self.events is not None:
            self.events.record(kind, site_id=site, map=key.split("/", 1)[1], **data)
