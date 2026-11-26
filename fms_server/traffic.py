"""맵별 이동 허가와 점유 존. 모든 호출은 JobCoordinator.lock 안에서 수행한다.

예약은 발행 전에 디스크에 기록한다. 취소/장애만으로 풀지 않고 새 State의
정지·위치·노드 통과 증거를 확인한다. TTL에 의한 자동 해제는 없다.
"""
from __future__ import annotations

import heapq
import json
import math
import time
from pathlib import Path

import numpy as np

from common.map_annotations import ZoneType, rectangle_frame
from common.schemas import LocalizationState, OrderNode, RobotState
from fms_server.fleet import RobotLiveness


class TrafficError(ValueError):
    pass


STALL_MOVE_SECONDS = 60.0     # BUSY/MOVING 인데 이 시간 동안 STALL_DISTANCE 도 못 가면 정체
STALL_DISTANCE = 0.3          # m
STALL_WAIT_SECONDS = 180.0    # BUSY/WAITING 이 이 시간을 넘으면 정체
CONFLICT_LOOKAHEAD = 5.0      # 경로 교차 예고: 앞으로 이 시간(초) 안에 지나갈 경로만 본다
CONFLICT_MIN_SPEED = 0.3      # 남은 경로 길이를 정할 때 쓰는 최소 속도 (m/s)


def find_cycles(edges):
    """wait-for 그래프(로봇 -> 기다리는 상대 로봇)의 순환을 DFS 로 찾는다. 순환마다 로봇 목록 하나."""
    graph = {}
    for edge in edges:
        graph.setdefault(edge["from"], []).append(edge["to"])
    cycles, seen_keys = [], set()
    color, stack = {}, []

    def visit(node):
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt) == 1:
                cycle = stack[stack.index(nxt):]
                key = frozenset(cycle)
                if key not in seen_keys:
                    seen_keys.add(key)
                    cycles.append(list(cycle))
            elif nxt not in color:
                visit(nxt)
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if node not in color:
            visit(node)
    return cycles


def _path_ahead(state, horizon):
    """로봇이 앞으로 horizon 초 동안 지나갈 경로 점들 (현재 위치 포함)."""
    budget = max(abs(state.velocity), CONFLICT_MIN_SPEED) * horizon
    points = [(state.pose.x, state.pose.y)]
    for p in state.local_path:
        step = math.hypot(p.x - points[-1][0], p.y - points[-1][1])
        if step > budget:
            ratio = budget / step if step else 0
            points.append((points[-1][0] + (p.x - points[-1][0]) * ratio,
                           points[-1][1] + (p.y - points[-1][1]) * ratio))
            break
        budget -= step
        points.append((p.x, p.y))
    return points


def _sample(points, spacing=0.15):
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, int(length / spacing))
        out.extend((a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps) for i in range(1, steps + 1))
    return out


def bounds(zone):
    return rectangle_frame(zone.polygon)


def intersects(box, pose, radius):
    if len(box) == 4:  # 회전 도입 전 저장한 예약도 같은 실제 경계로 복원한다.
        x0,y0,x1,y1 = box
        box = ((x0+x1)/2,(y0+y1)/2,1,0,(x1-x0)/2,(y1-y0)/2)
    cx,cy,ux,uy,half_width,half_height = box
    dx,dy = pose.x-cx,pose.y-cy
    return abs(ux*dx+uy*dy) <= half_width+radius and abs(-uy*dx+ux*dy) <= half_height+radius


def radius(state):
    # 회전 중에도 차체 전체가 존 밖에 있도록 외접원 + 추정 위치 여유를 사용한다.
    return math.hypot(state.width, state.length) / 2 + 0.1


class ZonePlanner:
    """정적 grid와 방향별 gate로 통과 순서를 고른다. 실제 경로는 로봇이 계획한다."""

    def __init__(self, model, zones, state):
        self.model, self.zones = model, zones
        self.margin = radius(state)
        grid = model.classify_grid() == 2
        self.free = grid.copy()
        h, w = grid.shape
        r = math.ceil(state.width / 2 / model.metadata.resolution)
        # planner.cpp와 같은 원판 팽창. UNKNOWN과 맵 밖은 주행 불가다.
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy > r * r:
                    continue
                y0, y1 = max(0, -dy), min(h, h - dy)
                x0, x1 = max(0, -dx), min(w, w - dx)
                self.free[y0:y1, x0:x1] &= grid[y0+dy:y1+dy, x0+dx:x1+dx]
        self.cache = {}
        self.grids = {}
        self.obstacles = ()           # 맵 전역 장애물 [(x, y, width, height)]. set_obstacles 로 바꾼다
        self.obstacle_margin = state.width / 2 + 0.05

    def set_obstacles(self, rects):
        """막힌 통로를 경로 선택에 반영한다. 바뀌면 거리 캐시를 비운다."""
        rects = tuple(sorted((round(x, 3), round(y, 3), round(w, 3), round(h, 3)) for x, y, w, h in rects))
        if rects != self.obstacles:
            self.obstacles = rects
            self.cache.clear()
            self.grids.clear()

    def mask(self, allowed):
        if allowed not in self.grids:
            free = self.free.copy()
            m = self.model.metadata
            yy, xx = np.ogrid[:free.shape[0], :free.shape[1]]
            wx = m.origin_x + (xx + 0.5) * m.resolution
            wy = m.origin_y + (yy + 0.5) * m.resolution
            for z in self.zones:
                if z.zone_id == allowed:
                    continue
                cx,cy,ux,uy,half_width,half_height = bounds(z)
                dx,dy = wx-cx,wy-cy
                free[(np.abs(ux*dx+uy*dy) <= half_width+self.margin)
                     & (np.abs(-uy*dx+ux*dy) <= half_height+self.margin)] = False
            for x, y, w, h in self.obstacles:
                hw, hh = w / 2 + self.obstacle_margin, h / 2 + self.obstacle_margin
                p0 = self.model.world_to_pixel(x - hw, y - hh)
                p1 = self.model.world_to_pixel(x + hw, y + hh)
                free[max(0, p0[1]):max(0, p1[1] + 1), max(0, p0[0]):max(0, p1[0] + 1)] = False
            self.grids[allowed] = free
        return self.grids[allowed]

    def distance(self, a, b, allowed=None):
        start, goal = self.model.world_to_pixel(a.x, a.y), self.model.world_to_pixel(b.x, b.y)
        key = (start, goal, allowed)
        if key in self.cache:
            return self.cache[key]
        free = self.mask(allowed)
        h, w = free.shape
        def ok(p):
            return 0 <= p[0] < w and 0 <= p[1] < h and free[p[1], p[0]]
        # 출발점은 장애물 바로 옆에 서 있을 수 있다 (막혀서 멈춘 로봇). 정적 맵에서 갈 수 있으면 출발은 허용한다.
        start_ok = ok(start) or (self.obstacles and 0 <= start[0] < w and 0 <= start[1] < h
                                 and self.free[start[1], start[0]])
        if not start_ok or not ok(goal):
            return math.inf
        q, costs = [(0, 0, start)], {start: 0}
        while q:
            _, cost, p = heapq.heappop(q)
            if cost != costs[p]:
                continue
            if p == goal:
                self.cache[key] = cost * self.model.metadata.resolution
                return self.cache[key]
            for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                n = p[0]+dx, p[1]+dy
                if not ok(n) or (dx and dy and (not ok((p[0]+dx,p[1])) or not ok((p[0],p[1]+dy)))):
                    continue
                nc = cost + math.hypot(dx, dy)
                if nc < costs.get(n, math.inf):
                    costs[n] = nc
                    heapq.heappush(q, (nc+math.hypot(n[0]-goal[0], n[1]-goal[1]), nc, n))
        self.cache[key] = math.inf
        return math.inf

    def route(self, start, goal):
        # 존 내부 목적지는 장기 점유·재출발 규칙이 필요하므로 첫 버전에서는 거절한다.
        if any(intersects(bounds(z), p, self.margin) for z in self.zones for p in (start, goal)):
            raise TrafficError("점유 존 내부·경계에는 작업 시작점이나 목적지를 둘 수 없습니다")
        gates = [(z, g) for z in self.zones for g in z.gates]
        points = [start, goal] + [p for _, g in gates for p in (g.wait, g.exit)]
        # 출발/출구 -> 목적지/대기 node는 모든 존을 막고, gate 통과만 해당 존을 연다.
        graph = {i: [] for i in range(len(points))}
        for i in [0] + list(range(3, len(points), 2)):
            for j in [1] + list(range(2, len(points), 2)):
                d = self.distance(points[i], points[j])
                if math.isfinite(d):
                    graph[i].append((j, d, None))
        for k, (z, g) in enumerate(gates):
            if any(intersects(bounds(other), p, self.margin) for other in self.zones for p in (g.wait, g.exit)):
                continue
            d = self.distance(g.wait, g.exit, z.zone_id)
            if math.isfinite(d):
                graph[2+2*k].append((3+2*k, d, k))
        q, dist, prev = [(0, 0)], {0: 0}, {}
        while q:
            cost, i = heapq.heappop(q)
            if cost != dist[i]:
                continue
            if i == 1:
                crossings = []
                while i:
                    source, gate = prev[i]
                    if gate is not None:
                        crossings.append(gates[gate])
                    i = source
                return list(reversed(crossings))
            for j, d, gate in graph[i]:
                if cost+d < dist.get(j, math.inf):
                    dist[j], prev[j] = cost+d, (i, gate)
                    heapq.heappush(q, (cost+d, j))
        raise TrafficError("점유 존 대기·출구 node를 통한 안전한 경로가 없습니다")


class TrafficController:
    def __init__(self, registry, fleet, jobs, events=None, doors=None):
        self.registry, self.fleet, self.jobs = registry, fleet, jobs
        self.events = events
        from fms_server.obstacles import ObstacleRegistry
        self.obstacles = ObstacleRegistry(registry, fleet, events)   # 맵 전역 장애물 (경로 선택·재경로에 쓴다)
        self.doors = doors   # DoorController (자동문 존용). 없으면 자동문 존에서 문이 안 열려 계속 기다린다
        self.aside_requests = set()   # (site_id, robot_id): 길을 막고 선 유휴 로봇. 코디네이터가 대기 위치로 보낸다
        self._motion = {}             # robot_id -> 정체 판정용 {x, y, since, waiting_since, reported}
        self.reroute_tried = {}       # job_id -> 마지막 재경로 판단 시각 (너무 자주 계산하지 않는다)
        self._deadlocks = {}          # frozenset(로봇) -> 처음 본 시각 (같은 순환을 반복 기록하지 않는다)
        self._conflicts = {}          # (로봇 a, 로봇 b) -> {since, last, paused}
        self.analysis = {"edges": [], "deadlocks": [], "stalls": [], "conflicts": []}
        self.path = Path(registry._sites_root).parent / "traffic.json"
        self.data = (json.loads(self.path.read_text()) if self.path.exists()
                     else {"limits": {}, "leases": {}})
        self.data.setdefault("options", {})
        self.planners = {}

    def _event(self, kind, lease, resource, **data):
        if self.events is None:
            return
        site = lease["map"].split("/", 1)[0]
        self.events.record(kind, site_id=site, robot_id=lease.get("robot"), job_id=lease.get("job"),
                           resource=resource, lease_kind=lease.get("kind"), **data)

    def set_waiting(self, job, reason):
        """대기 이유를 바꾸고, 대기 시작·해제를 이벤트로 남긴다."""
        previous = job.traffic.get("waiting")
        job.traffic["waiting"] = reason
        if self.events is None or previous == reason:
            return
        if reason and not previous:
            job.traffic["waiting_since"] = time.time()
            self.events.record("traffic.wait", site_id=job.site_id, robot_id=job.robot_id,
                               job_id=job.job_id, reason=reason)
        elif previous and not reason:
            since = job.traffic.pop("waiting_since", None)
            self.events.record("traffic.wait_end", site_id=job.site_id, robot_id=job.robot_id,
                               job_id=job.job_id, reason=previous,
                               seconds=round(time.time() - since, 2) if since else None)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2)+"\n")
        tmp.replace(self.path)

    @staticmethod
    def key(site, map_id):
        return f"{site}/{map_id}"

    def limit(self, site, map_id):
        return self.data["limits"].get(self.key(site, map_id))

    def set_limit(self, site, map_id, limit):
        self.data["limits"][self.key(site, map_id)] = limit
        self.save()

    def options(self, site, map_id):
        """맵별 선택 기능. conflict_pause: 경로 교차 예고 때 우선순위 낮은 로봇을 잠깐 세운다 (기본 끔)."""
        return dict({"conflict_pause": False}, **self.data["options"].get(self.key(site, map_id), {}))

    def set_options(self, site, map_id, **options):
        self.data["options"].setdefault(self.key(site, map_id), {}).update(options)
        self.save()

    def snapshot(self, site, map_id):
        key = self.key(site, map_id)
        store = self.registry.map_store(site)
        zones = []
        uid = store.map_uid(map_id)
        records = self.fleet.all()
        for z in (store.load_annotations(map_id).zones if store.active_version(map_id) else []):
            if z.type != ZoneType.OCCUPANCY:
                continue
            lease = self.data['leases'].get(f'zone:{key}:{z.zone_id}')
            nearby = [r for r in records if r.state and r.state.map_id == uid]
            occupants = [r.robot_id for r in nearby if intersects(bounds(z),r.state.pose,radius(r.state))]
            unknown = any(not self.fresh(r) for r in nearby)
            unknown |= any(a.map_id == map_id and not any(r.robot_id==a.robot_id and r.state and r.state.map_id for r in records)
                           for a in self.registry.get_site(site).robots.values())
            if lease:
                unknown |= not self.fresh(self.fleet.get(lease['robot']))
            unauthorized = any(rid != (lease or {}).get('robot') for rid in occupants)
            status = 'blocked' if unknown or unauthorized else 'occupied' if occupants else 'reserved' if lease else 'free'
            zones.append({'zone_id':z.zone_id,'name':z.name,'status':status,
                          'owner':(lease or {}).get('robot'),'occupants':occupants,
                          'door':(dict(door_id=z.door.door_id, **(self.doors.snapshot(z.door.door_id) if self.doors
                                                                  else dict(liveness='UNKNOWN', door=None)))
                                  if z.door else None)})
        on_map = {r.robot_id for r in records if r.state and r.state.map_id == uid}
        on_map.update(a.robot_id for a in self.registry.get_site(site).robots.values() if a.map_id == map_id)
        analysis = self.analysis
        return {"max_moving_robots": self.limit(site, map_id),
                "conflict_pause": self.options(site, map_id)["conflict_pause"],
                "deadlocks": [d for d in analysis["deadlocks"] if set(d["robots"]) & on_map],
                "stalls": [s for s in analysis["stalls"] if s["robot_id"] in on_map],
                "conflicts": [c for c in analysis["conflicts"] if set(c["robots"]) & on_map],
                "zones": zones,
                "obstacles": self.obstacles.snapshot(site, map_id),
                "leases": [dict(v, resource=k) for k, v in self.data["leases"].items() if v["map"] == key],
                "waiting": [{"job_id": j.job_id, "robot_id": j.robot_id,
                             "reason": j.traffic.get("waiting")}
                            for j in self.jobs.list(site) if j.traffic.get("map") == key
                            and not j.status.terminal and j.traffic.get("waiting")]}

    def prepare(self, job, order, map_id, state):
        store = self.registry.map_store(job.site_id)
        version = store.active_version(map_id)
        zones = [z for z in store.load_annotations(map_id, version).zones if z.type == ZoneType.OCCUPANCY]
        if not zones and self.limit(job.site_id, map_id) is None:
            job.traffic = {}
            return order
        record = self.fleet.get(job.robot_id)
        supported = record.traffic_control if record is not None and record.factsheet is not None else state.traffic_control
        if zones and not supported:
            raise TrafficError("이 로봇은 점유 존 제어를 지원하지 않습니다. 클라이언트를 갱신하세요")
        planner = self._planner(store, job.site_id, map_id, version, zones, state) if zones else None
        obstacles = self.obstacles.rects(job.site_id, map_id) if planner else []
        nodes, crossings, sources = [], [], {}
        previous = state.pose
        for original in order.nodes:
            # 직선/도킹/언도킹은 우회할 수 없다. 존을 걸치는 설비는 등록 시 분리한다.
            route = self._route(planner, previous, original.position, obstacles) if planner else []
            for z, gate in route:
                seq = len(nodes)
                nodes.append(OrderNode(node_id=f"{order.order_id}-traffic-{seq}", sequence_id=seq,
                                       released=False, position=gate.wait))
                nodes.append(OrderNode(node_id=f"{order.order_id}-traffic-{seq+1}", sequence_id=seq+1,
                                       released=False, position=gate.exit, allowed_zone_ids=[z.zone_id]))
                crossings.append({"zone": z.zone_id, "gate": gate.gate_id, "wait": seq,
                                  "exit": seq+1, "bounds": bounds(z), "done": False,
                                  "door": z.door.door_id if z.door else None})
            n = original.model_copy(deep=True)
            n.sequence_id, n.released = len(nodes), False
            sources[n.node_id] = original.sequence_id
            nodes.append(n)
            previous = original.position
        job.traffic = {"map": self.key(job.site_id, map_id), "map_id": map_id,
                       "version": version, "uid": store.map_uid(map_id), "crossings": crossings,
                       "sources": sources, "waiting": "층별 이동 허가 대기"}
        return order.model_copy(update={"nodes": nodes})

    def _planner(self, store, site, map_id, version, zones, state):
        cache_key = (site, map_id, version, state.width, state.length)
        if cache_key not in self.planners:
            self.planners = {cache_key: ZonePlanner(store.load_map_model(map_id, version), zones, state)}
        return self.planners[cache_key]

    @staticmethod
    def _route(planner, start, goal, obstacles):
        """맵 전역 장애물로 막힌 통로를 피해 gate 순서를 고른다. 피할 길이 없으면 장애물을 무시한 길로 대체한다
        (로봇이 그 앞에서 스스로 기다리거나 돌아간다)."""
        planner.set_obstacles(obstacles)
        try:
            return planner.route(start, goal)
        except TrafficError:
            if not obstacles:
                raise
            planner.set_obstacles(())
            return planner.route(start, goal)

    def preview_gates(self, job, map_id, state, positions, *, with_obstacles):
        """지금 위치에서 positions 를 차례로 지나는 gate 순서 [(zone_id, gate_id)]. 재경로 필요 여부를 가릴 때 쓴다."""
        store = self.registry.map_store(job.site_id)
        version = store.active_version(map_id)
        zones = [z for z in store.load_annotations(map_id, version).zones if z.type == ZoneType.OCCUPANCY]
        if not zones:
            return []
        planner = self._planner(store, job.site_id, map_id, version, zones, state)
        obstacles = self.obstacles.rects(job.site_id, map_id) if with_obstacles else []
        gates, previous = [], state.pose
        for goal in positions:
            gates += [(z.zone_id, g.gate_id) for z, g in self._route(planner, previous, goal, obstacles)]
            previous = goal
        return gates

    def fresh(self, record):
        return (record is not None and record.state is not None
                and record.liveness(self.fleet.stale_after) == RobotLiveness.ONLINE
                and record.state.localization == LocalizationState.LOCALIZED)

    def acquire_floor(self, job, map_id, *, future=False):
        key = self.key(job.site_id, map_id)
        leases = self.data["leases"]
        resource = f"floor:{key}:{job.robot_id}"
        if resource in leases:
            # 도착 층 예약을 실제 이동 허가로 전환한다.
            if not future and (leases[resource].get("future") or leases[resource].get("order") != job.order_id):
                leases[resource].update(future=False, order=job.order_id, job=job.job_id, ack=False, at=time.time())
                self.save()
            return True
        limit = self.limit(job.site_id, map_id)
        if future and limit is None:
            return True
        holders = {v["robot"] for k,v in leases.items() if k.startswith(f"floor:{key}:")}
        uid = self.registry.map_store(job.site_id).map_uid(map_id)
        # 수동·외부 오더도 실제 이동 중이면 정원을 차지한다.
        managed = {j.order_id for j in self.jobs.list() if not j.status.terminal}
        holders.update(r.robot_id for r in self.fleet.all() if r.state and r.state.map_id == uid
                       and (abs(r.state.velocity) > 0.01 or (r.state.order_id and r.state.order_id not in managed)))
        holders.discard(job.robot_id)
        if limit is not None and len(holders) >= limit:
            return False
        leases[resource] = {"map": key, "robot": job.robot_id, "job": job.job_id,
                            "at": time.time(), "future": future, "kind": "floor",
                            "stage": job.stage,
                            "order": job.order_id, "ack": False}
        self.save()
        self._event("traffic.reserve", leases[resource], resource)
        return True

    def _peers(self, job):
        records = {r.robot_id:r for r in self.fleet.all()}
        for r in records.values():
            if r.robot_id != job.robot_id and r.state and r.state.map_id == job.traffic['uid']:
                yield r
        # 재시작 직후 아직 한 번도 상태를 받지 못한 등록 로봇도 확인 대상으로 남긴다.
        for a in self.registry.get_site(job.site_id).robots.values():
            if a.robot_id == job.robot_id or a.map_id != job.traffic['map_id']:
                continue
            r = records.get(a.robot_id)
            if r is None or r.state is None or r.state.map_id is None:
                yield None

    def _clear_space(self, job, pose, margin):
        for r in self._peers(job):
            if not self.fresh(r) or math.hypot(r.state.pose.x-pose.x, r.state.pose.y-pose.y) <= margin+radius(r.state):
                if r is not None and r.state is not None and r.available_for_job(self.fleet.stale_after):
                    # 작업 없이 서 있는 로봇이 길을 막는다 — 대기 위치로 비켜 달라고 요청해 둔다.
                    self.aside_requests.add((job.site_id, r.robot_id))
                    job.traffic["blocked_by"] = r.robot_id
                return False
        for lease in self.data['leases'].values():
            if lease['map'] != job.traffic['map'] or lease['robot'] == job.robot_id or 'pose' not in lease:
                continue
            p = lease['pose']
            if math.hypot(p['x']-pose.x,p['y']-pose.y) <= margin+lease['radius']:
                return False
        return True

    def _door_open(self, job, door):
        """문을 열고 붙잡는다. 열려 있으면 True. 이 존의 예약이 없는 시점이므로 다른 작업의 붙잡음은 낡은 것이다."""
        if self.doors is None:
            return False
        holder = self.doors.holder(door)
        if holder is not None and holder != job.job_id:
            self.doors.release(door, holder)
        opened = self.doors.request_open(door, job.job_id)
        if opened and job.traffic.get("door_open") != door:
            job.traffic["door_open"] = door
            if self.events is not None:
                self.events.record("door.open", site_id=job.site_id, robot_id=job.robot_id,
                                   job_id=job.job_id, door_id=door)
        return opened

    def _sweep_doors(self):
        """존 예약이 없는데 문을 붙잡은 작업이 끝났으면 놓는다 (대기 중 취소된 경우)."""
        if self.doors is None:
            return
        held = {l.get("door"): l["job"] for l in self.data["leases"].values() if l.get("kind") == "zone"}
        for door, owner in self.doors.holds().items():
            if held.get(door) == owner:
                continue
            try:
                terminal = self.jobs.get(owner).status.terminal
            except Exception:
                terminal = True
            if terminal:
                self.doors.release(door, owner)

    def _zone_clear(self, job, crossing):
        for r in self._peers(job):
            if not self.fresh(r) or intersects(crossing["bounds"], r.state.pose, radius(r.state)):
                return False
        return True

    def reconcile(self):
        """새 상태로만 예약을 해제한다. 종료 작업의 남은 점유도 확인한다."""
        changed = False
        for key, lease in list(self.data["leases"].items()):
            r = self.fleet.get(lease["robot"])
            if not self.fresh(r) or r.state.timestamp <= lease["at"]:
                continue
            s = r.state
            try:
                j = self.jobs.get(lease["job"])
            except Exception:
                continue
            uid = self.registry.map_store(j.site_id).map_uid(lease["map"].split("/",1)[1])
            if (lease["kind"] == "floor" and lease.get("future") and j.status.terminal
                    and j.stage == lease.get("stage") and not s.order_id and abs(s.velocity) < .01
                    and s.state in (RobotState.IDLE, RobotState.CHARGING)):
                # 호출 단계를 벗어나지 않았으므로 탑승 전 도착 층 예약이다.
                self._event("traffic.release", lease, key)
                del self.data["leases"][key]
                changed = True
                continue
            if s.map_id != uid:
                continue  # 맵 변경만으로 안전한 퇴장을 증명할 수 없다
            stopped = abs(s.velocity) < 0.01 and s.order_id is None and s.state in (RobotState.IDLE, RobotState.CHARGING)
            passed = (s.order_id == lease.get("order") and s.order_update_id >= lease.get("update", 0)
                      and (s.last_node_id == lease.get("exit_id")
                           or s.last_node_sequence_id > lease.get("exit", math.inf)))
            if lease["kind"] == "zone":
                clear = (passed or (stopped and j.status.terminal)) and not intersects(lease["bounds"], s.pose, radius(s))
            elif lease["kind"] == "wait":
                clear = (passed or (stopped and j.status.terminal))
            else:
                if s.order_id == lease.get("order") and not lease.get("ack"):
                    lease["ack"] = True
                    changed = True
                clear = stopped and (j.status.terminal or (not lease.get("future") and lease.get("ack")))
            if clear:
                self._event("traffic.release", lease, key)
                del self.data["leases"][key]
                changed = True
                if lease.get("door") and self.doors is not None:
                    self.doors.release(lease["door"], lease["job"])
        if changed:
            self.save()
        self._sweep_doors()

    def update(self, job):
        """기존 허가를 유지하고 대기 node/출구까지 연속해서 허가한다."""
        t = job.traffic
        if not t or not job.order_snapshot:
            return None
        r = self.fleet.get(job.robot_id)
        if not self.fresh(r) or r.state.paused or r.state.state in (RobotState.ERROR, RobotState.EMERGENCY):
            return None
        s = r.state
        if s.map_id != t["uid"] or s.map_version != t["version"]:
            self.set_waiting(job, "맵·위치 확인 대기")
            return None
        if not self.acquire_floor(job, t["map_id"]):
            self.set_waiting(job, "층별 동시 이동 대수 제한")
            return None
        from common.schemas import Order
        order = Order.model_validate(job.order_snapshot)
        through = len(order.nodes)-1
        leases = self.data["leases"]
        for c in t["crossings"]:
            if c["done"]:
                continue
            zk = f'zone:{t["map"]}:{c["zone"]}'
            wk = f'wait:{t["map"]}:{c["zone"]}:{c["gate"]}'
            own = leases.get(zk, {}).get("order") == order.order_id and leases.get(zk, {}).get("robot") == job.robot_id
            passed = (s.order_id == order.order_id and s.order_update_id >= order.order_update_id
                      and (s.last_node_id == order.nodes[c["exit"]].node_id or s.last_node_sequence_id > c["exit"]))
            if passed and not intersects(c["bounds"], s.pose, radius(s)):
                c["done"] = True
                continue
            if own:
                through = c["exit"]
                self.set_waiting(job, None)
                break
            wait = order.nodes[c["wait"]]
            held = leases.get(wk)
            if held and (held["robot"] != job.robot_id or held.get("order") != order.order_id):
                through = c["wait"]-1
                self.set_waiting(job, "대기 node 사용 중")
                break
            if not held:
                if not self._clear_space(job, wait.position, radius(s)):
                    through = c["wait"]-1
                    self.set_waiting(job, "대기 node 주변 확인 대기")
                    break
                leases[wk] = {"map": t["map"], "robot": job.robot_id, "job": job.job_id,
                              "kind": "wait", "at": time.time(), "order": order.order_id,
                              "pose": wait.position.model_dump(), "radius": radius(s),
                              "exit_id": order.nodes[c["exit"]].node_id, "exit": c["exit"], "update": order.order_update_id}
                self.save()
                self._event("traffic.reserve", leases[wk], wk)
            at_wait = (s.order_id == order.order_id and s.last_node_id == wait.node_id
                       and abs(s.velocity) < 0.01 and math.hypot(s.pose.x-wait.position.x,s.pose.y-wait.position.y) < 0.08)
            end = order.nodes[c["exit"]]
            door = c.get("door")
            ready = (at_wait and zk not in leases and self._zone_clear(job, c)
                     and self._clear_space(job, end.position, radius(s)))
            if ready and door:
                # 자동문: 대기(진입) node 에서 열림 신호를 보내고, 열린 것을 확인해야 존에 들어간다.
                if not self._door_open(job, door):
                    through = c["wait"]
                    self.set_waiting(job, self.doors.reason(door) if self.doors else "자동문 제어기 없음")
                    break
            if ready:
                leases[zk] = {"map": t["map"], "robot": job.robot_id, "job": job.job_id,
                              "kind": "zone", "at": time.time(), "order": order.order_id,
                              "pose": end.position.model_dump(), "radius": radius(s),
                              "exit_id": end.node_id, "exit": c["exit"], "bounds": c["bounds"],
                              "update": order.order_update_id+1, "door": door}
                self.save()
                self._event("traffic.reserve", leases[zk], zk)
                through = c["exit"]
                self.set_waiting(job, None)
            else:
                through = c["wait"]
                self.set_waiting(job, "점유 존 진입 허가 대기")
            break
        else:
            self.set_waiting(job, None)
        changed = False
        for n in order.nodes:
            if n.sequence_id <= through and not n.released:
                n.released, changed = True, True
        if not changed:
            if s.order_id == order.order_id and s.order_update_id < order.order_update_id:
                # 기존 오더의 미수신 갱신만 재전송한다. 신규 오더·다른 로봇 재배정은 하지 않는다.
                return order
            return None
        order.order_update_id += 1
        order.timestamp = time.time()
        return order

    # -- 교착·정체 감지 ----------------------------------------------------

    def wait_graph(self, elevator_ctl=None):
        """wait-for 간선: "로봇 A 가 B 가 가진 자원(존·대기 node·층 정원·승강기 임대)을 기다림"."""
        leases = self.data["leases"]
        active = [j for j in self.jobs.list() if not j.status.terminal and j.robot_id and j.status.value != "pending"]
        robot_of = {j.job_id: j.robot_id for j in active}
        edges = []

        def add(job, other, resource):
            if other and other != job.robot_id:
                edges.append({"from": job.robot_id, "to": other, "resource": resource,
                              "job_id": job.job_id, "site_id": job.site_id})

        for job in active:
            t = job.traffic
            waiting = t.get("waiting") if t else None
            if waiting:
                key = t["map"]
                current = next((c for c in t.get("crossings", []) if not c["done"]), None)
                if waiting == "층별 동시 이동 대수 제한":
                    for resource, lease in leases.items():
                        if lease["kind"] == "floor" and lease["map"] == key:
                            add(job, lease["robot"], resource)
                elif current is not None:
                    zone_key = f'zone:{key}:{current["zone"]}'
                    wait_key = f'wait:{key}:{current["zone"]}:{current["gate"]}'
                    if waiting == "대기 node 사용 중" and wait_key in leases:
                        add(job, leases[wait_key]["robot"], wait_key)
                    elif waiting == "점유 존 진입 허가 대기":
                        if zone_key in leases:
                            add(job, leases[zone_key]["robot"], zone_key)
                        for r in self.fleet.all():
                            if (r.state and r.state.map_id == t.get("uid")
                                    and intersects(current["bounds"], r.state.pose, radius(r.state))):
                                add(job, r.robot_id, zone_key)
                    elif waiting == "대기 node 주변 확인 대기":
                        add(job, t.get("blocked_by"), wait_key)
                        for resource, lease in leases.items():
                            if lease["map"] == key and "pose" in lease and resource != wait_key:
                                add(job, lease["robot"], resource)
            # 다층 작업의 호출 단계: 승강기 임대·도착 층 정원
            if job.plan and job.stage < len(job.plan) and job.plan[job.stage]["t"] == "call":
                stage = job.plan[job.stage]
                if elevator_ctl is not None:
                    owner = elevator_ctl.lease_owner(stage["elevator_id"])
                    if owner and owner != job.job_id:
                        add(job, robot_of.get(owner), f'elevator:{stage["elevator_id"]}')
                ride = next((st for st in job.plan[job.stage + 1:] if st["t"] == "ride"), None)
                if ride is not None and job.error and "도착 층" in job.error:
                    key = self.key(job.site_id, ride["to_map"])
                    for resource, lease in leases.items():
                        if lease["kind"] == "floor" and lease["map"] == key:
                            add(job, lease["robot"], resource)
        return edges

    def analyze(self, now=None, elevator_ctl=None):
        """교착(진짜 순환)과 정체(안 움직임)를 구분해 찾고 이벤트로 남긴다. 결과는 `self.analysis`."""
        now = time.time() if now is None else now
        edges = self.wait_graph(elevator_ctl)
        cycles = find_cycles(edges)
        deadlocks = []
        for cycle in cycles:
            key = frozenset(cycle)
            resources = sorted({e["resource"] for e in edges if e["from"] in key and e["to"] in key})
            site = next((e["site_id"] for e in edges if e["from"] in key), None)
            since = self._deadlocks.get(key)
            if since is None:
                since = self._deadlocks[key] = now
                if self.events is not None:
                    self.events.record("traffic.deadlock", site_id=site, severity="error",
                                       robots=sorted(cycle), resources=resources)
            deadlocks.append({"robots": sorted(cycle), "resources": resources, "since": since, "site_id": site})
        for key in [k for k in self._deadlocks if k not in {frozenset(c) for c in cycles}]:
            del self._deadlocks[key]

        in_cycle = {r for c in cycles for r in c}
        stalls = []
        busy = {j.robot_id: j for j in self.jobs.list() if j.status.value == "working" and j.robot_id}
        for robot_id in [r for r in self._motion if r not in busy]:
            del self._motion[robot_id]
        for robot_id, job in busy.items():
            r = self.fleet.get(robot_id)
            if not self.fresh(r) or r.state.state != RobotState.BUSY or r.state.paused:
                self._motion.pop(robot_id, None)
                continue
            s = r.state
            m = self._motion.setdefault(robot_id, {"x": s.pose.x, "y": s.pose.y, "since": now,
                                                   "waiting_since": None, "reported": False})
            sub = s.sub_state.value if s.sub_state else None
            if math.hypot(s.pose.x - m["x"], s.pose.y - m["y"]) >= STALL_DISTANCE:
                m.update(x=s.pose.x, y=s.pose.y, since=now, reported=False)
            m["waiting_since"] = (m["waiting_since"] or now) if sub == "WAITING" else None
            kind = seconds = None
            if sub == "MOVING" and now - m["since"] >= STALL_MOVE_SECONDS:
                kind, seconds = "moving", now - m["since"]
            elif sub == "WAITING" and now - m["waiting_since"] >= STALL_WAIT_SECONDS:
                kind, seconds = "waiting", now - m["waiting_since"]
            if kind is None:
                m["reported"] = False
                continue
            stalls.append({"robot_id": robot_id, "job_id": job.job_id, "kind": kind, "seconds": round(seconds, 1),
                           "in_deadlock": robot_id in in_cycle, "reason": job.traffic.get("waiting") or job.error})
            # 순환에 든 로봇은 교착으로 이미 알렸다. 정체는 순환이 아닌 것만 따로 알린다.
            if not m["reported"] and robot_id not in in_cycle:
                m["reported"] = True
                if self.events is not None:
                    self.events.record("traffic.stall", site_id=job.site_id, robot_id=robot_id, job_id=job.job_id,
                                       severity="warning", stall=kind, seconds=round(seconds, 1),
                                       reason=job.traffic.get("waiting") or job.error)
        self.analysis = {"edges": edges, "deadlocks": deadlocks, "stalls": stalls,
                         "conflicts": self.analysis.get("conflicts", [])}
        return self.analysis

    def revoke_safe(self, analysis=None):
        """교착 자동 해소. **아직 로봇에 허가를 내보내지 않은 예약**만 가장 늦은 것부터 회수한다.

        회수 대상은 탑승 전 도착 층 예약(`future` floor lease)뿐이다 — 그 층으로 가는 오더를 아직 보내지 않았다.
        대기 node·점유 존 예약은 잡는 즉시 통행 허가(released)로 나가므로 회수하지 않는다 (기존 불변식:
        점유 중 존·발행된 허가는 새 State 의 증거로만 푼다). 회수한 예약 목록을 돌려준다.
        """
        analysis = analysis or self.analysis
        revoked = []
        for deadlock in analysis["deadlocks"]:
            members = set(deadlock["robots"])
            candidates = [(lease["at"], resource) for resource, lease in self.data["leases"].items()
                          if resource in deadlock["resources"] and lease["kind"] == "floor"
                          and lease.get("future") and lease["robot"] in members]
            if not candidates:
                continue
            _, resource = max(candidates)
            lease = self.data["leases"].pop(resource)
            revoked.append(resource)
            if self.events is not None:
                self.events.record("traffic.revoke", site_id=lease["map"].split("/", 1)[0], robot_id=lease["robot"],
                                   job_id=lease["job"], severity="warning", resource=resource,
                                   reason="교착 해소 — 탑승 전 도착 층 예약 회수")
        if revoked:
            self.save()
        return revoked

    # -- 경로 교차 예고 (실험) ---------------------------------------------

    def path_conflicts(self, now=None):
        """같은 맵 두 로봇의 남은 경로(`State.local_path`)가 곧 가까워지는지 본다. 점유 존 밖 마주침 예고용.

        돌려주는 값: [{robots:[a,b], distance, new}] — new 는 이번에 처음 잡힌 쌍.
        """
        now = time.time() if now is None else now
        moving = [r for r in self.fleet.all()
                  if self.fresh(r) and r.state.state == RobotState.BUSY and r.state.local_path and r.state.map_id]
        found = {}
        for i, a in enumerate(moving):
            for b in moving[i + 1:]:
                if a.state.map_id != b.state.map_id:
                    continue
                if abs(a.state.velocity) < 0.01 and abs(b.state.velocity) < 0.01:
                    continue
                limit = radius(a.state) + radius(b.state)
                pa = _sample(_path_ahead(a.state, CONFLICT_LOOKAHEAD))
                pb = _sample(_path_ahead(b.state, CONFLICT_LOOKAHEAD))
                # 같은 시각쯤에 지나는 점끼리만 비교한다 (경로가 겹쳐도 시간이 어긋나면 괜찮다).
                va = max(abs(a.state.velocity), CONFLICT_MIN_SPEED)
                vb = max(abs(b.state.velocity), CONFLICT_MIN_SPEED)
                best = math.inf
                for ia, p in enumerate(pa):
                    ta = ia * 0.15 / va
                    for ib, q in enumerate(pb):
                        if abs(ta - ib * 0.15 / vb) > 1.5:
                            continue
                        best = min(best, math.hypot(p[0] - q[0], p[1] - q[1]))
                if best <= limit:
                    found[tuple(sorted((a.robot_id, b.robot_id)))] = best
        out = []
        for pair, distance in found.items():
            entry = self._conflicts.get(pair)
            new = entry is None
            if new:
                entry = self._conflicts[pair] = {"since": now, "paused": None}
            entry["last"] = now
            out.append({"robots": list(pair), "distance": round(distance, 2), "new": new, "since": entry["since"],
                        "paused": entry["paused"]})
        self.analysis["conflicts"] = out
        return out

    def cleared_conflicts(self, now=None, hold=3.0):
        """더는 잡히지 않는 쌍 (hold 초 동안 안 보이면 해소). [(pair, entry)] 를 돌려주고 기억에서 지운다."""
        now = time.time() if now is None else now
        gone = [(pair, entry) for pair, entry in self._conflicts.items() if now - entry["last"] >= hold]
        for pair, _ in gone:
            del self._conflicts[pair]
        return gone
