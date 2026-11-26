"""맵별 이동 허가와 점유 존. 모든 호출은 JobCoordinator.lock 안에서 수행한다.

예약은 발행 전에 디스크에 기록한다. 취소/장애만으로 풀지 않고 새 State의
정지·위치·노드 통과 증거를 확인한다. TTL에 의한 자동 해제는 없다.
"""
from __future__ import annotations

import heapq
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from common.map_annotations import ZoneType, rectangle_frame
from common.schemas import ActionType, BusySubState, LocalizationState, Order, OrderNode, Pose, RobotState
from fms_server import zone_grid
from fms_server.fleet import RobotLiveness


class TrafficError(ValueError):
    pass


STALL_MOVE_SECONDS = 60.0     # BUSY/MOVING 인데 이 시간 동안 STALL_DISTANCE 도 못 가면 정체
STALL_DISTANCE = 0.3          # m
STALL_WAIT_SECONDS = 180.0    # BUSY/WAITING 이 이 시간을 넘으면 정체
CONFLICT_LOOKAHEAD = 5.0      # 경로 교차 예고: 앞으로 이 시간(초) 안에 지나갈 경로만 본다
CONFLICT_MIN_SPEED = 0.3      # 남은 경로 길이를 정할 때 쓰는 최소 속도 (m/s)
PASS_HISTORY = 10            # 존 통과 시간 평균에 쓰는 최근 통과 수 (배정 스냅샷 avg_pass_s)
ENTRY_WINDOW = 180.0         # 존 진입 시각을 이 시간(초)만큼 기억한다 (entries_1m/3m)
BEFORE_WAIT = ("대기 node 사용 중", "대기 node 주변 확인 대기")
GRID_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
PASS_THROUGH_DISTANCE = 1.5  # m. 줄 맨 앞 로봇이 대기 node 에 이만큼 가까워지면 서지 않고 존에 들어가게 허가한다
STOP_CLEARANCE = 0.12  # 도착 허용 오차를 포함해 임시 정차 위치가 경계 밖에 있도록 한다.


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


def straight_crosses(box, start, end, margin):
    """회전 사각형의 차체 여유까지 포함해 직선 이동을 검사한다."""
    cx, cy, ux, uy, hw, hh = box
    lo, hi = 0.0, 1.0
    for ax, ay, extent in ((ux, uy, hw + margin), (-uy, ux, hh + margin)):
        p = ax*(start.x - cx) + ay*(start.y - cy)
        d = ax*(end.x - start.x) + ay*(end.y - start.y)
        if not d:
            if abs(p) > extent:
                return False
            continue
        a, b = sorted(((-extent - p)/d, (extent - p)/d))
        lo, hi = max(lo, a), min(hi, b)
        if lo > hi:
            return False
    return True


@dataclass(frozen=True)
class AutomaticGate:
    wait: Pose | None
    exit: Pose | None
    gate_id: str


class ZonePlanner:
    """흰 픽셀 위 최단 경로가 지나는 존으로 통과 순서를 고른다. 실제 경로는 로봇이 계획한다.

    존은 경로 탐색에 반영하지 않는다. 벽(정적 맵의 주행 불가)과 등록된 장애물만 피한 최단 경로를 한 번
    구하고, 그 경로가 들어가는 존을 차례로 본다."""

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
        self.automatic_zones = [zone for zone in zones if zone.automatic_gate]
        self.cache = {}
        self.factor = zone_grid.coarse_factor(model.metadata.resolution)
        self.obstacles = ()           # 맵 전역 장애물 [(x, y, width, height)]. set_obstacles 로 바꾼다
        self.obstacle_margin = state.width / 2 + 0.05
        self._walk = self._coarse_walk = None

    def set_obstacles(self, rects):
        """막힌 통로를 경로 탐색에 반영한다."""
        rects = tuple(sorted((round(x, 3), round(y, 3), round(w, 3), round(h, 3)) for x, y, w, h in rects))
        if rects != self.obstacles:
            self.obstacles = rects
            self._walk = self._coarse_walk = None

    def walkable(self):
        """주행 가능 정밀 격자: 팽창한 흰 픽셀에서 등록 장애물(여유 포함)을 뺀 것. 존은 막지 않는다."""
        if self._walk is None:
            free = self.free.copy()
            for x, y, w, h in self.obstacles:
                hw, hh = w / 2 + self.obstacle_margin, h / 2 + self.obstacle_margin
                p0 = self.model.world_to_pixel(x - hw, y - hh)
                p1 = self.model.world_to_pixel(x + hw, y + hh)
                free[max(0, p0[1]):max(0, p1[1] + 1), max(0, p0[0]):max(0, p1[0] + 1)] = False
            self._walk = free
        return self._walk

    def _coordinates(self):
        m = self.model.metadata
        yy, xx = np.ogrid[:self.free.shape[0], :self.free.shape[1]]
        return m.origin_x + (xx + 0.5) * m.resolution, m.origin_y + (yy + 0.5) * m.resolution

    def _zone_cells(self, zone, margin, coordinates=None):
        cx, cy, ux, uy, half_width, half_height = bounds(zone)
        wx, wy = coordinates if coordinates is not None else self._coordinates()
        dx, dy = wx - cx, wy - cy
        return ((np.abs(ux*dx + uy*dy) <= half_width + margin)
                & (np.abs(-uy*dx + ux*dy) <= half_height + margin))

    def _walkable_at(self, p):
        x, y = self.model.world_to_pixel(p.x, p.y)
        walk = self.walkable()
        return 0 <= y < walk.shape[0] and 0 <= x < walk.shape[1] and bool(walk[y, x])

    def path(self, start, goal):
        """흰 픽셀 위 start -> goal 최단 경로 [(x, y)] (양 끝은 정확한 좌표). 거친 격자에서 A* 한 번."""
        model, f = self.model, self.factor
        walk = self.walkable()
        h, w = walk.shape
        a, b = model.world_to_pixel(start.x, start.y), model.world_to_pixel(goal.x, goal.y)
        # 출발점은 장애물 바로 옆에 서 있을 수 있다 (막혀서 멈춘 로봇). 정적 맵에서 갈 수 있으면 출발은 허용한다.
        if not (0 <= a[0] < w and 0 <= a[1] < h and self.free[a[1], a[0]]):
            raise TrafficError("출발 위치가 주행 가능 영역(흰 픽셀) 밖입니다")
        if not (0 <= b[0] < w and 0 <= b[1] < h and walk[b[1], b[0]]):
            raise TrafficError("목적지가 주행 가능 영역(흰 픽셀) 밖이거나 장애물에 막혔습니다")
        if self._coarse_walk is None:
            self._coarse_walk = zone_grid.coarsen(walk, f)
        grid = self._coarse_walk
        ca, cb = (a[0] // f, a[1] // f), (b[0] // f, b[1] // f)
        if not grid[ca[1], ca[0]]:                       # 장애물에 막힌 출발 칸만 연다
            grid = grid.copy()
            grid[ca[1], ca[0]] = True
        cells = zone_grid.path(grid, ca, cb)
        if not cells:
            raise TrafficError("흰 픽셀 위로 목적지까지 가는 경로가 없습니다")
        return [(start.x, start.y)] + [self._cell_point(c, walk) for c in cells[1:-1]] + [(goal.x, goal.y)]

    def _cell_point(self, cell, walk):
        """거친 칸 안에서 주행 가능한 정밀 픽셀 중 칸 중심에 가장 가까운 점 (대기·퇴장 node 후보가 된다)."""
        f = self.factor
        x0, y0 = cell[0] * f, cell[1] * f
        block = walk[y0:y0 + f, x0:x0 + f]
        if not block.any():
            block = self.free[y0:y0 + f, x0:x0 + f]
        ys, xs = np.nonzero(block) if block.any() else (np.array([f // 2]), np.array([f // 2]))
        c = (f - 1) / 2
        k = int(np.argmin((xs - c) ** 2 + (ys - c) ** 2))
        return self.model.pixel_to_world(x0 + int(xs[k]), y0 + int(ys[k]))

    def _spans(self, points):
        """경로가 들어가는 존 구간 [(zone, 첫 표본, 끝 표본)]과 표본 좌표·꼭짓점 표시.

        gate 가 있는 존은 차체 여유(margin)만, 노드 없는 존은 정차 여유(STOP_CLEARANCE)까지 더해 판정한다."""
        step = self.model.metadata.resolution
        xs, ys, vertex = [points[0][0]], [points[0][1]], [True]
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            n = max(1, math.ceil(math.hypot(bx - ax, by - ay) / step))
            t = np.arange(1, n + 1) / n
            xs.extend(ax + (bx - ax) * t)
            ys.extend(ay + (by - ay) * t)
            vertex.extend([False] * (n - 1) + [True])
        xs, ys = np.array(xs), np.array(ys)
        labels = np.full(len(xs), -1)
        for k, zone in enumerate(self.zones):
            margin = self.margin + (0 if zone.gates else STOP_CLEARANCE)
            inside = self._zone_cells(zone, margin, (xs, ys))
            if (inside & (labels >= 0)).any():
                raise TrafficError("경로가 겹치거나 맞닿은 점유 존을 지나 대기 위치를 둘 수 없습니다")
            labels[inside] = k
        spans, i = [], 0
        while i < len(labels):
            if labels[i] < 0:
                i += 1
                continue
            first = i
            while i + 1 < len(labels) and labels[i + 1] == labels[i]:
                i += 1
            spans.append((self.zones[labels[i]], first, i))
            i += 1
        return spans, xs, ys, vertex

    def route(self, start, goal):
        """start -> goal 이 지나는 [(존, gate)] 순서.

        - gate 가 지정된 존: 경로 진행 방향과 같은 방향 gate 중 경로의 진입·퇴장 점에 가까운 것을 고르고,
          그 출구에서 경로를 다시 구한다.
        - 노드 없는 존: 경로가 존에 들어가기 직전·나온 직후 꼭짓점을 대기·퇴장 node 로 쓴다
          (출발이 존 안이면 대기 없음, 목적지가 존 안이면 퇴장 없음). 두 존 사이에 꼭짓점이 없으면
          사이 구간 가운데 점을 쓴다.
        - gate 존이라도 출발점이나 목적지가 그 안(경계 포함)이면 이 경로에서는 노드 없는 존처럼 다룬다.
          gate 의 대기·출구 node 는 존을 끝까지 통과할 때만 뜻이 있어서다 (존 안 상차지점 등)."""
        resident = {id(z) for z in self.zones if z.gates
                    and any(intersects(bounds(z), p, self.margin) for p in (start, goal))}
        crossings, origin = [], start
        for _ in range(sum(len(z.gates) for z in self.zones) + 1):
            points = self.path(origin, goal)
            spans, xs, ys, vertex = self._spans(points)
            restart = None
            for k, (zone, first, last) in enumerate(spans):
                low = spans[k - 1][2] if k else -1                   # 이전 존 구간의 끝 표본
                high = spans[k + 1][1] if k + 1 < len(spans) else len(xs)
                if zone.gates and id(zone) not in resident:
                    gate = self._pick_gate(zone, (xs[max(first - 1, 0)], ys[max(first - 1, 0)]),
                                           (xs[min(last + 1, len(xs) - 1)], ys[min(last + 1, len(xs) - 1)]))
                    crossings.append((zone, gate))
                    restart = gate.exit
                    break
                before = next((j for j in range(first - 1, low, -1) if vertex[j]), None)
                after = next((j for j in range(last + 1, high) if vertex[j]), None)
                # 두 존을 직선으로 연달아 지나 사이에 꺾임점이 없으면 사이 구간 가운데 표본을 쓴다.
                # 존 구간 사이 표본은 양쪽 존의 여유 밖이라 그 자리에 서도 어느 존도 침범하지 않는다.
                if first > 0 and before is None and first - low > 1:
                    before = (low + first) // 2
                if last + 1 < len(xs) and after is None and high - last > 1:
                    after = (last + high) // 2
                if (first > 0 and before is None) or (last + 1 < len(xs) and after is None):
                    raise TrafficError("노드 없는 점유구역 앞뒤에 대기·퇴장 위치를 둘 공간이 없습니다")
                # 출발점 바로 앞이 존이면 대기 위치로 움직이지 않고 출발점에서 허가를 받는다.
                wait = self._pose(xs, ys, before, first) if before not in (None, 0) else None
                exit_pose = self._pose(xs, ys, after, last) if after is not None else None
                wx, wy = self.model.world_to_pixel(wait.x, wait.y) if wait else (-1, -1)
                crossings.append((zone, AutomaticGate(wait, exit_pose, f"auto-{wx}-{wy}")))
            if restart is None:
                return crossings
            origin = restart
        raise TrafficError("gate 를 지나도 목적지 경로가 점유 존을 다시 지납니다")

    @staticmethod
    def _pose(xs, ys, index, toward):
        """꼭짓점 index 의 자세. 대기는 존 쪽, 퇴장은 진행 방향을 본다."""
        x, y, qx, qy = xs[index], ys[index], xs[toward], ys[toward]
        theta = math.atan2(qy - y, qx - x) if toward > index else math.atan2(y - qy, x - qx)
        return Pose(x=float(x), y=float(y), theta=theta)

    def _pick_gate(self, zone, entry, leave):
        """경로 진행 방향(entry -> leave)과 같은 방향이고 대기·출구 node 가 주행 가능한 gate 중 가장 가까운 것."""
        heading = (leave[0] - entry[0], leave[1] - entry[1])
        best = None
        for gate in zone.gates:
            w, e = gate.wait, gate.exit
            if (e.x - w.x) * heading[0] + (e.y - w.y) * heading[1] <= 0:
                continue
            if not (self._walkable_at(w) and self._walkable_at(e)):
                continue
            if any(intersects(bounds(other), p, self.margin) for other in self.zones for p in (w, e)):
                continue
            cost = math.hypot(w.x - entry[0], w.y - entry[1]) + math.hypot(e.x - leave[0], e.y - leave[1])
            if best is None or cost < best[0]:
                best = (cost, gate)
        if best is None:
            raise TrafficError("점유 존 대기·출구 node를 통한 안전한 경로가 없습니다")
        return best[1]

    def leaves_room(self, zone, source, exit_pose, waiting, clearance):
        """대기 로봇이 정차해도 점유 로봇이 퇴장할 수 있는지 확인한다."""
        model = self.model
        start = model.world_to_pixel(source.x, source.y)
        if not model.in_bounds(source.x, source.y):
            return False
        goal = None
        if exit_pose is not None:
            if not model.in_bounds(exit_pose.x, exit_pose.y):
                return False
            goal = model.world_to_pixel(exit_pose.x, exit_pose.y)
        key = ("egress", zone.zone_id, start, goal, waiting.x, waiting.y, clearance)
        if key not in self.cache:
            wx, wy = self._coordinates()
            # 경계 주변에서 퇴장할 여유를 확인한다. 우회가 너무 멀면 접근 전에 기다리게 한다.
            points = [*zone.polygon, source, waiting]
            if exit_pose is not None:
                points.append(exit_pose)
            pad = 2*clearance + model.metadata.resolution
            x0, y0 = model.world_to_pixel(min(p.x for p in points) - pad, min(p.y for p in points) - pad)
            x1, y1 = model.world_to_pixel(max(p.x for p in points) + pad, max(p.y for p in points) + pad)
            x0, y0 = max(0, x0), max(0, y0)
            xs, ys = slice(x0, x1 + 1), slice(y0, y1 + 1)
            wx, wy = wx[:, xs], wy[ys, :]
            free = self.free[ys, xs] & ((wx - waiting.x)**2 + (wy - waiting.y)**2 > clearance**2)
            for other in self.zones:
                if other.zone_id != zone.zone_id:
                    free[self._zone_cells(other, self.margin, (wx, wy))] = False
            escape = None
            if goal is None:
                # 이후 작업의 목적지가 아직 없다. 대기 로봇 쪽에서도 그 차체를
                # 완전히 지나갈 공간이 남는 경우에만 경계 앞 대기를 허용한다.
                cx, cy, ux, uy, hw, hh = bounds(zone)
                dx, dy = waiting.x - cx, waiting.y - cy
                u, v = ux*dx + uy*dy, -uy*dx + ux*dy
                ax, ay, projection = (ux, uy, u) if abs(u) - hw >= abs(v) - hh else (-uy, ux, v)
                side = 1 if projection >= 0 else -1
                escape = free & (side*(ax*(wx-waiting.x) + ay*(wy-waiting.y)) > clearance)
            else:
                goal = goal[0] - x0, goal[1] - y0
            x, y = start[0] - x0, start[1] - y0
            self.cache[key] = bool(free[y, x] and _reachable(free, (x, y), goal, escape=escape))
        return self.cache[key]


def _reachable(free, start, goal=None, *, escape=None):
    """free(bool) 격자에서 start 로부터 goal 또는 escape 칸에 닿을 수 있는가 (8방향, 모서리 끼어들기 금지)."""
    height, width = free.shape
    queue, costs = [(0, 0, start)], {start: 0}
    while queue:
        _, cost, point = heapq.heappop(queue)
        if cost != costs[point]:
            continue
        x, y = point
        if point == goal or (escape is not None and escape[y, x]):
            return True
        for dx, dy in GRID_STEPS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < width and 0 <= ny < height) or not free[ny, nx]:
                continue
            if dx and dy and not (free[y, nx] and free[ny, x]):
                continue
            total = cost + math.hypot(dx, dy)
            if total < costs.get((nx, ny), math.inf):
                costs[(nx, ny)] = total
                heuristic = math.hypot(nx - goal[0], ny - goal[1]) if goal else 0
                heapq.heappush(queue, (total + heuristic, total, (nx, ny)))
    return False


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
        self._zone_hist = {}          # 존 예약 키 -> {entries: 진입 시각, passes: 통과 초} (메모리, 재시작 시 비움)
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

    def set_waiting(self, job, reason, crossing=None, phase=None):
        """대기 이유를 바꾸고, 대기 구간이 바뀔 때마다 이벤트로 남긴다.

        구간 하나 = `traffic.wait`(시작) + `traffic.wait_end`(끝, seconds). 이유가 같아도 존·gate·단계가 바뀌면
        새 구간이다 (예전에는 이유가 바뀌어도 한 번으로 합쳐졌고, 첫 대기는 기록되지 않았다).
        phase: approach(대기 node 로 주행 중 — 서 있지 않다) / at_wait(대기 node 에 정지) /
        before_wait(대기 node 앞에서 멈춤) / floor(층 정원) / map(맵·위치 확인).
        """
        job.traffic["waiting"] = reason
        job.traffic.pop("waiting_since", None)   # 예전 형식
        key = None
        if reason:
            key = [reason, crossing["zone"] if crossing else None, crossing["gate"] if crossing else None, phase]
        seg = job.traffic.get("wait_seg")
        if seg is not None and seg["key"] == key:
            return
        now = time.time()
        if seg is not None:
            self._end_segment(job, seg, now, "next" if reason else "clear")
        if reason:
            job.traffic["wait_seg"] = {"key": key, "since": now}
            self._wait_event("traffic.wait", job, key, ts=now)

    def end_wait(self, job, outcome):
        """열린 대기 구간을 닫는다 (작업 종료·오더 재구성·단계 전환). 대기 이유 표시는 그대로 둔다."""
        seg = (job.traffic or {}).get("wait_seg")
        if seg is not None:
            self._end_segment(job, seg, time.time(), outcome)

    def _end_segment(self, job, seg, now, outcome):
        job.traffic.pop("wait_seg", None)
        self._wait_event("traffic.wait_end", job, seg["key"], ts=now,
                         seconds=round(now - seg["since"], 2), outcome=outcome)

    def _wait_event(self, kind, job, key, **data):
        if self.events is None:
            return
        reason, zone, gate, phase = key
        self.events.record(kind, site_id=job.site_id, robot_id=job.robot_id, job_id=job.job_id,
                           reason=reason, zone_id=zone, gate_id=gate, phase=phase, **data)

    def _zone_entered(self, zone_key, now):
        hist = self._zone_hist.setdefault(zone_key, {"entries": deque(), "passes": deque(maxlen=PASS_HISTORY)})
        hist["entries"].append(now)
        while hist["entries"] and hist["entries"][0] < now - ENTRY_WINDOW:
            hist["entries"].popleft()

    def _zone_passed(self, zone_key, seconds):
        hist = self._zone_hist.setdefault(zone_key, {"entries": deque(), "passes": deque(maxlen=PASS_HISTORY)})
        hist["passes"].append(seconds)

    @staticmethod
    def _current(job):
        return next((c for c in (job.traffic or {}).get("crossings", []) if not c["done"]), None)

    def assign_snapshot(self, job, state, now=None):
        """배정 순간 FMS 가 알던 교통 상태. `job.assigned` 이벤트의 `snapshot` 으로 남긴다.

        AI 학습 특징(설계서 표 3.6)의 원본이자 누출 검사 대조값이다. `prepare` 뒤(이 작업의 존 목록이 정해진 뒤),
        `update` 앞(이 작업이 예약을 잡기 전)에 부른다. 이 작업 자신은 집계에서 뺀다.
        """
        now = time.time() if now is None else now
        snap = {"t": round(now, 3), "soc": round(state.battery.charge, 2),
                "x": round(state.pose.x, 3), "y": round(state.pose.y, 3), "zones": []}
        t = job.traffic or {}
        if not t:
            return snap
        key, leases = t["map"], self.data["leases"]
        snap["moving_robots"] = len({v["robot"] for v in leases.values()
                                     if v["kind"] == "floor" and v["map"] == key and not v.get("future")
                                     and v["robot"] != job.robot_id})
        snap["floor_cap"] = self.limit(job.site_id, t["map_id"])
        others = [j for j in self.jobs.list(job.site_id)
                  if j.job_id != job.job_id and not j.status.terminal and j.robot_id
                  and j.traffic and j.traffic.get("map") == key]
        for c in t.get("crossings", []):
            zk = f'zone:{key}:{c["zone"]}'
            queue = dir_queue = before_wait = ahead = 0
            for j in others:
                if any(cc["zone"] == c["zone"] and not cc["done"] for cc in j.traffic.get("crossings", [])):
                    ahead += 1
                cur = self._current(j)
                if cur is None or cur["zone"] != c["zone"]:
                    continue
                owns = leases.get(zk, {}).get("job") == j.job_id
                if cur.get("arrived") and not owns:
                    queue += 1
                    dir_queue += cur["gate"] == c["gate"]
                elif j.traffic.get("waiting") in BEFORE_WAIT:
                    before_wait += 1
            hist = self._zone_hist.get(zk, {"entries": (), "passes": ()})
            lease = leases.get(zk)
            snap["zones"].append({
                "zone_id": c["zone"], "gate_id": c["gate"],
                "queue_len": queue, "queue_len_dir": dir_queue, "before_wait": before_wait,
                "reserved_ahead": ahead,
                "occupied": lease is not None,
                "occ_elapsed_s": round(now - lease["at"], 2) if lease else None,
                "entries_1m": sum(1 for e in hist["entries"] if e >= now - 60),
                "entries_3m": sum(1 for e in hist["entries"] if e >= now - 180),
                "avg_pass_s": round(sum(hist["passes"]) / len(hist["passes"]), 2) if hist["passes"] else None,
                "passes_n": len(hist["passes"])})
        return snap

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
            owner = (lease or {}).get('robot')
            if z.automatic_gate and owner is None and len(occupants) == 1:
                owner = occupants[0]
            unauthorized = any(rid != owner for rid in occupants)
            status = 'blocked' if unknown or unauthorized else 'occupied' if occupants else 'reserved' if lease else 'free'
            zones.append({'zone_id':z.zone_id,'name':z.name,'status':status,
                          'owner':owner,'occupants':occupants,
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
        if job.traffic:
            self.end_wait(job, "reorder")   # 재경로·다층 단계로 오더를 새로 만들면 이전 대기 구간은 끝난다
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
        open_crossing = None

        def insert(node_pose, allowed=None):
            seq = len(nodes)
            nodes.append(OrderNode(node_id=f"{order.order_id}-traffic-{seq}", sequence_id=seq,
                                   released=False, position=node_pose, allowed_zone_ids=allowed or []))
            return seq

        for original in order.nodes:
            goal = next_start = original.position
            if planner and planner.automatic_zones and original.action:
                action = original.action
                offset = 0.0
                if action.action_type in (ActionType.DOCK, ActionType.PARK):
                    offset = float(action.params.get("undock_distance", 0))
                    if action.params.get("entry", "FORWARD") == "FORWARD":
                        offset = -offset
                elif action.action_type == ActionType.STRAIGHT:
                    offset = float(action.params.get("distance", 0))
                if action.action_type in (ActionType.DOCK, ActionType.PARK, ActionType.STRAIGHT):
                    p = original.position
                    fixed_end = Pose(x=p.x + offset*math.cos(p.theta), y=p.y + offset*math.sin(p.theta), theta=p.theta)
                    if any(straight_crosses(bounds(z), p, fixed_end, planner.margin + STOP_CLEARANCE)
                           for z in planner.automatic_zones):
                        raise TrafficError("도킹·주차·직선 동작은 노드 없는 점유구역과 분리하세요")
                    next_start = fixed_end
                    if action.action_type != ActionType.STRAIGHT:
                        goal = fixed_end
            try:
                route = self._route(planner, previous, goal, obstacles) if planner else []
            except TrafficError as exc:
                # 첫 구간(로봇 위치에서 출발)이 아니면 어느 로봇을 보내도 같은 이유로 실패한다.
                exc.robot_specific = previous is state.pose
                raise
            for z, gate in route:
                if open_crossing is None:
                    # 대기열은 줄 끝 -> 대기 node 바로 뒤 순서로 지나간다. slots[0] 이 대기 node, slots[k] 가 k+1 번째 자리.
                    behind = [insert(q) for q in reversed(getattr(gate, "queue", None) or [])][::-1]
                    wait_seq = insert(gate.wait) if gate.wait else None
                    open_crossing = {"zone": z.zone_id, "gate": gate.gate_id, "bounds": bounds(z), "done": False,
                                     "door": z.door.door_id if z.door else None,
                                     "wait": wait_seq, "exit": None}
                    if behind:
                        open_crossing["slots"] = [wait_seq, *behind]
                    if z.automatic_gate or isinstance(gate, AutomaticGate):
                        open_crossing["automatic"] = True
                        if gate.wait is None:
                            open_crossing["start"] = previous.model_dump()
                    crossings.append(open_crossing)
                if gate.exit is not None:
                    open_crossing["exit"] = insert(gate.exit, [z.zone_id])
                    open_crossing = None
            n = original.model_copy(deep=True)
            n.sequence_id, n.released = len(nodes), False
            if open_crossing is not None:
                n.allowed_zone_ids = [open_crossing["zone"]]
            sources[n.node_id] = original.sequence_id
            nodes.append(n)
            previous = next_start
        job.traffic = {"map": self.key(job.site_id, map_id), "map_id": map_id,
                       "version": version, "uid": store.map_uid(map_id), "crossings": crossings,
                       "sources": sources, "waiting": "층별 이동 허가 대기"}
        return order.model_copy(update={"nodes": nodes})

    def _planner(self, store, site, map_id, version, zones, state):
        cache_key = (site, map_id, version, state.width, state.length)
        planner = self.planners.pop(cache_key, None)
        if planner is None:
            if len(self.planners) == 2:
                self.planners.pop(next(iter(self.planners)))
            planner = ZonePlanner(store.load_map_model(map_id, version), zones, state)
        self.planners[cache_key] = planner
        return planner

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
        staying = None
        for goal in positions:
            for zone, gate in self._route(planner, previous, goal, obstacles):
                if staying != zone.zone_id:
                    gates.append((zone.zone_id, None if isinstance(gate, AutomaticGate) else gate.gate_id))
                staying = zone.zone_id if gate.exit is None else None
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

    def _clear_space(self, job, pose, margin, *, aside=True):
        for r in self._peers(job):
            if not self.fresh(r) or math.hypot(r.state.pose.x-pose.x, r.state.pose.y-pose.y) <= margin+radius(r.state):
                if r is not None and r.state is not None:
                    job.traffic["blocked_by"] = r.robot_id
                if aside and r is not None and r.available_for_job(self.fleet.stale_after):
                    # 작업 없이 서 있는 로봇이 길을 막는다 — 대기 위치로 비켜 달라고 요청해 둔다.
                    self.aside_requests.add((job.site_id, r.robot_id))
                return False
        for lease in self.data['leases'].values():
            if lease['map'] != job.traffic['map'] or lease['robot'] == job.robot_id or 'pose' not in lease:
                continue
            p = lease['pose']
            if math.hypot(p['x']-pose.x,p['y']-pose.y) <= margin+lease['radius']:
                return False
        return True

    def _automatic_wait_clear(self, job, crossing, wait, waiting_radius):
        if not self._clear_space(job, wait, waiting_radius, aside=False):
            return False
        t = job.traffic
        key = t["map"]
        relevant = [v for v in self.data["leases"].values()
                    if v["map"] == key and v.get("zone") == crossing["zone"]
                    and v["robot"] != job.robot_id and v.get("automatic")]
        peers = {r.robot_id: r for r in self._peers(job) if r is not None}
        traffic_plans = {}
        for lease in relevant:
            if lease["kind"] == "zone" or lease["robot"] not in traffic_plans:
                traffic_plans[lease["robot"]] = lease
        approaching = [(rid, peer.state, traffic_plans.get(rid),
                        intersects(crossing["bounds"], peer.state.pose, radius(peer.state)))
                       for rid, peer in peers.items()]
        approaching = [entry for entry in approaching if entry[2] or entry[3]]
        if not approaching:
            return True
        store = self.registry.map_store(job.site_id)
        zones = [z for z in store.load_annotations(t["map_id"], t["version"]).zones if z.type == ZoneType.OCCUPANCY]
        zone = next(z for z in zones if z.zone_id == crossing["zone"])
        for rid, s, lease, inside in approaching:
            exit_pose = lease.get("egress") if lease else None
            if lease and lease["kind"] == "wait" and not exit_pose and not inside:
                # 내부 목적지로 향하는 로봇은 아직 출구가 없다. 입장 후 실제 위치에서 다시 검사한다.
                job.traffic["blocked_by"] = rid
                return False
            approach = lease.get("approach") if lease else None
            source = Pose(**approach) if approach and not inside else s.pose
            end = Pose(**exit_pose) if exit_pose else None
            planner = self._planner(store, job.site_id, t["map_id"], t["version"], zones, s)
            if not planner.leaves_room(zone, source, end, wait, waiting_radius + radius(s) + STOP_CLEARANCE):
                job.traffic["blocked_by"] = rid
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
            exit_seq = lease.get("exit")
            passed = (exit_seq is not None and s.order_id == lease.get("order")
                      and s.order_update_id >= lease.get("update", 0)
                      and (s.last_node_id == lease.get("exit_id")
                           or (s.last_node_sequence_id > exit_seq and
                               (not lease.get("automatic") or s.last_node_id in
                                {n["node_id"] for n in (j.order_snapshot or {}).get("nodes", [])}))))
            if lease.get("automatic") and s.map_version != lease.get("version"):
                continue
            if lease["kind"] == "zone":
                clear = (passed or (stopped and j.status.terminal)) and not intersects(lease["bounds"], s.pose, radius(s))
            elif lease["kind"] == "wait" and lease.get("automatic"):
                p = lease["pose"]
                owner = self.data["leases"].get(f'zone:{lease["map"]}:{lease["zone"]}', {})
                admitted = (owner.get("robot") == lease["robot"] and s.order_id == owner.get("order")
                            and s.order_id is not None and s.order_update_id >= owner["update"])
                clear = ((passed or admitted or stopped and j.status.terminal)
                         and math.hypot(s.pose.x-p["x"], s.pose.y-p["y"]) > radius(s) + lease["radius"])
            elif lease["kind"] == "wait":
                clear = (passed or (stopped and j.status.terminal))
            elif lease["kind"] == "queue":
                # 대기열 자리: 그 node 를 지나(다음 node 도달) 차체가 자리에서 벗어났거나, 작업이 끝나 멈췄다.
                p = lease["pose"]
                moved_on = (s.order_id == lease["order"] and s.last_node_sequence_id > lease["seq"]
                            and math.hypot(s.pose.x-p["x"], s.pose.y-p["y"]) > lease["radius"])
                clear = moved_on or (stopped and j.status.terminal)
            else:
                if s.order_id == lease.get("order") and not lease.get("ack"):
                    lease["ack"] = True
                    changed = True
                clear = stopped and (j.status.terminal or (not lease.get("future") and lease.get("ack")))
            if clear:
                held = round(time.time() - lease["at"], 2)
                if lease["kind"] == "zone":
                    self._zone_passed(key, held)   # 진입 허가 -> 출구 통과 (avg_pass_s)
                self._event("traffic.release", lease, key, held_s=held)
                del self.data["leases"][key]
                changed = True
                if lease.get("door") and self.doors is not None:
                    self.doors.release(lease["door"], lease["job"])
        if changed:
            self.save()
        self._sweep_doors()

    def _can_transfer_automatic(self, lease, job, state, version):
        if not (lease.get("automatic") and lease["robot"] == job.robot_id
                and lease.get("version") == version and state.order_id != lease["order"]):
            return False
        if lease["job"] == job.job_id and lease.get("stage") != job.stage:
            return True
        return any(j.job_id == lease["job"] and j.status.terminal for j in self.jobs.list(job.site_id))

    def _arrived_at_wait(self, job, crossing, lease, zone_busy):
        if crossing.get("arrived"):
            return
        now = crossing["arrived"] = time.time()
        if self.events is not None:
            reserved = lease.get("at") if lease else None
            self.events.record("traffic.at_wait", site_id=job.site_id, robot_id=job.robot_id,
                               job_id=job.job_id, ts=now, zone_id=crossing["zone"], gate_id=crossing["gate"],
                               approach_s=round(now - reserved, 2) if reserved else None, zone_busy=zone_busy)

    def _yield_floor_at_automatic_zone(self, job, order, state):
        """정지한 후속 로봇이 유일한 층 허가를 잡고 내부 로봇의 퇴장을 막지 않게 한다."""
        t = job.traffic
        crossing = self._current(job)
        if (crossing is None or not crossing.get("automatic") or crossing["wait"] is None
                or state.order_id != order.order_id or state.order_update_id != order.order_update_id
                or state.sub_state != BusySubState.WAITING or abs(state.velocity) >= 0.01):
            return False
        frontier = next((n for n in reversed(order.nodes) if n.released), None)
        if frontier is not None and (frontier.sequence_id > crossing["wait"] or state.last_node_id != frontier.node_id):
            return False
        leases = self.data["leases"]
        if any(v["kind"] == "zone" and v["map"] == t["map"] and v["robot"] == job.robot_id
               for v in leases.values()):
            return False
        owner = leases.get(f'zone:{t["map"]}:{crossing["zone"]}')
        if owner is None and self._zone_clear(job, crossing):
            return False
        wait = order.nodes[crossing["wait"]]
        wait_lease = leases.get(f'wait:{t["map"]}:{crossing["zone"]}:{crossing["gate"]}')
        at_wait = (frontier is wait
                   and math.hypot(state.pose.x - wait.position.x, state.pose.y - wait.position.y) < 0.08)
        if at_wait:
            self._arrived_at_wait(job, crossing, wait_lease, owner is not None)
        elif self._automatic_wait_clear(job, crossing, wait.position, radius(state)):
            return False  # 먼저 안전한 대기점까지 접근한다.
        key = f'floor:{t["map"]}:{job.robot_id}'
        lease = leases.pop(key, None)
        if lease is not None:
            self.save()
            self._event("traffic.release", lease, key)
        self.set_waiting(job, "점유 존 진입 허가 대기", crossing, "at_wait" if at_wait else "before_wait")
        return True

    # -- 대기열 (gate 존) ---------------------------------------------------
    # 자리 0 = 대기 node(kind=wait), 자리 k = 대기열 k번째 뒤(kind=queue). 로봇은 줄 끝 -> 자리 0 으로 간다.
    # 새로 오는 로봇은 맨 뒤 점유 자리 바로 뒤를 잡고, 앞 자리가 비면 한 칸씩 당긴다 (선입선출).
    # 지나가는 자리도 예약해 두었다가 그 node 를 지나 차체가 벗어나면 푼다 (reconcile).

    def _slot_keys(self, job, c):
        base = f'{job.traffic["map"]}:{c["zone"]}:{c["gate"]}'
        slots = c.get("slots") or [c["wait"]]
        return slots, [f'wait:{base}'] + [f'queue:{base}:{i}' for i in range(1, len(slots))]

    def _queue_step(self, job, c, order, s, end):
        """대기열 자리를 잡거나 당긴다. 줄 맨 앞(대기 node)을 가졌으면 None, 아니면 (허가 끝 seq, 대기 이유, 단계)."""
        leases = self.data["leases"]
        slots, keys = self._slot_keys(job, c)
        held = [leases.get(k) for k in keys]
        mine = [bool(l and l["robot"] == job.robot_id and l.get("order") == order.order_id) for l in held]
        pos = next((i for i, m in enumerate(mine) if m), None)
        ahead = [i for i, l in enumerate(held) if l and not mine[i] and (pos is None or i < pos)]
        target = max(ahead) + 1 if ahead else 0
        if ahead:
            job.traffic["blocked_by"] = held[max(ahead)]["robot"]
        before = slots[-1] - 1        # 줄 끝 직전 node (대기열에 들어가기 전 위치)
        if pos is None:
            if target >= len(slots):
                return before, "대기열 가득 참", "before_wait"
            rival = self._queue_rival(job, c, s, order.nodes[slots[-1]].position)
            if rival is not None:
                job.traffic["blocked_by"] = rival
                return before, "대기열 진입 순서 대기", "before_wait"
        if pos is None or target < pos:
            upto = len(slots) - 1 if pos is None else pos - 1
            # 잡을 자리(지나갈 자리 포함)에 예약 없이 서 있는 로봇이 있으면 기다린다 (재경로·취소로 예약이 풀린 로봇 등).
            squatter = next((r for i in range(target, upto + 1)
                             if (r := self._standing_on(job, order.nodes[slots[i]].position, radius(s))) is not None), None)
            if squatter is not None:
                job.traffic["blocked_by"] = squatter
                if pos is None:
                    return before, "대기 node 주변 확인 대기", "before_wait"
                return slots[pos], f"대기열 {pos + 1}번째 자리 대기", "queue"
            now = time.time()
            for i in range(target, upto + 1):
                if held[i] is not None:
                    continue
                node = order.nodes[slots[i]]
                leases[keys[i]] = {"map": job.traffic["map"], "robot": job.robot_id, "job": job.job_id,
                                   "kind": "wait" if i == 0 else "queue", "at": now, "order": order.order_id,
                                   "pose": node.position.model_dump(), "radius": radius(s), "seq": slots[i],
                                   "node_id": node.node_id, "exit_id": end.node_id, "exit": c["exit"],
                                   "update": order.order_update_id, "zone": c["zone"]}
                self._event("traffic.reserve", leases[keys[i]], keys[i], zone_id=c["zone"], gate_id=c["gate"], slot=i)
            c.setdefault("ticket", now)   # 줄에 선 시각 (이후 새로 오는 로봇의 순서 경쟁에서 빠진다)
            self.save()
            pos = target
        if pos == 0:
            return None
        return slots[pos], f"대기열 {pos + 1}번째 자리 대기", "queue"

    def _standing_on(self, job, pose, margin):
        """예약 없이 그 자리에 서 있는 다른 로봇 (작업 없는 로봇이면 비켜 달라고 요청한다)."""
        for r in self._peers(job):
            if r is None or r.state is None:
                continue
            if math.hypot(r.state.pose.x - pose.x, r.state.pose.y - pose.y) < margin:
                if r.available_for_job(self.fleet.stale_after):
                    self.aside_requests.add((job.site_id, r.robot_id))
                return r.robot_id
        return None

    def _queue_rival(self, job, c, s, entrance):
        """같은 줄에 아직 서지 않은 다른 로봇 중 줄 끝에 더 가까운 로봇 (먼저 도착할 로봇이 먼저 선다)."""
        mine = math.hypot(s.pose.x - entrance.x, s.pose.y - entrance.y)
        for other in self.jobs.list(job.site_id):
            if other.job_id == job.job_id or other.status.terminal or not other.traffic:
                continue
            oc = self._current(other)
            if (oc is None or other.traffic.get("map") != job.traffic["map"]
                    or (oc["zone"], oc["gate"]) != (c["zone"], c["gate"]) or "ticket" in oc):
                continue
            r = self.fleet.get(other.robot_id)
            if not self.fresh(r):
                continue
            d = math.hypot(r.state.pose.x - entrance.x, r.state.pose.y - entrance.y)
            if (d, other.job_id) < (mine, job.job_id):
                return other.robot_id
        return None

    def _zone_turn(self, job, c):
        """같은 존의 다른 방향 줄 맨 앞에 먼저 도착한 로봇이 있으면 양보한다 (아직 오는 중인 로봇은 기다리지 않는다)."""
        mine = c.get("at_head", float("inf"))
        prefix = f'wait:{job.traffic["map"]}:{c["zone"]}:'
        for key, lease in self.data["leases"].items():
            if not key.startswith(prefix) or lease["robot"] == job.robot_id or lease.get("automatic"):
                continue
            try:
                other = self.jobs.get(lease["job"])
            except Exception:
                continue
            oc = self._current(other)
            if other.status.terminal or oc is None or oc["zone"] != c["zone"] or "at_head" not in oc:
                continue
            if (oc["at_head"], other.job_id) < (mine, job.job_id):
                job.traffic["blocked_by"] = lease["robot"]
                return False
        return True

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
            self.set_waiting(job, "맵·위치 확인 대기", phase="map")
            return None
        order = Order.model_validate(job.order_snapshot)
        if self.limit(job.site_id, t["map_id"]) is not None and self._yield_floor_at_automatic_zone(job, order, s):
            return None
        if not self.acquire_floor(job, t["map_id"]):
            self.set_waiting(job, "층별 동시 이동 대수 제한", phase="floor")
            return None
        through = len(order.nodes)-1
        leases = self.data["leases"]
        t.pop("blocked_by", None)
        reached = s.last_node_id in {n.node_id for n in order.nodes}
        for c in t["crossings"]:
            if c["done"]:
                continue
            zk = f'zone:{t["map"]}:{c["zone"]}'
            wk = f'wait:{t["map"]}:{c["zone"]}:{c["gate"]}'
            owner = leases.get(zk)
            own = owner and owner["order"] == order.order_id and owner["robot"] == job.robot_id
            end_seq = c["exit"] if c["exit"] is not None else len(order.nodes) - 1
            end = order.nodes[end_seq]
            automatic = c.get("automatic", False)
            passed = (c["exit"] is not None and s.order_id == order.order_id
                      and s.order_update_id >= order.order_update_id
                      and (s.last_node_id == end.node_id or (reached and s.last_node_sequence_id > end_seq)))
            if passed and not intersects(c["bounds"], s.pose, radius(s)):
                c["done"] = True
                continue
            if own:
                through = end_seq
                self.set_waiting(job, None)
                break
            wait = order.nodes[c["wait"]] if c["wait"] is not None else None
            door = c.get("door")
            if wait is not None and not automatic:
                step = self._queue_step(job, c, order, s, end)
                if step is not None:                  # 대기열 뒤쪽 자리에서 앞 로봇을 기다린다
                    through, reason, phase = step
                    self.set_waiting(job, reason, c, phase)
                    break
                at_wait = (s.order_id == order.order_id and s.last_node_id == wait.node_id
                           and abs(s.velocity) < 0.01 and math.hypot(s.pose.x-wait.position.x, s.pose.y-wait.position.y) < 0.08)
            elif wait is not None:
                held = leases.get(wk)
                transfer = held and self._can_transfer_automatic(held, job, s, t["version"])
                if held and not transfer and (held["robot"] != job.robot_id or held.get("order") != order.order_id):
                    through = c["wait"]-1
                    self.set_waiting(job, "대기 node 사용 중", c, "before_wait")
                    break
                if not held or transfer:
                    clear = (self._automatic_wait_clear(job, c, wait.position, radius(s)) if automatic
                             else self._clear_space(job, wait.position, radius(s)))
                    if not clear:
                        through = c["wait"]-1
                        self.set_waiting(job, "대기 node 주변 확인 대기", c, "before_wait")
                        break
                    leases[wk] = {"map": t["map"], "robot": job.robot_id, "job": job.job_id,
                                  "kind": "wait", "at": time.time(), "order": order.order_id,
                                  "pose": wait.position.model_dump(), "radius": radius(s),
                                  "exit_id": end.node_id, "exit": c["exit"], "update": order.order_update_id}
                    if automatic:
                        leases[wk].update(automatic=True, zone=c["zone"], version=t["version"], stage=job.stage,
                                          approach=wait.position.model_dump(),
                                          egress=end.position.model_dump() if c["exit"] is not None else None)
                    self.save()
                    self._event("traffic.reserve", leases[wk], wk, zone_id=c["zone"], gate_id=c["gate"])
                at_wait = (s.order_id == order.order_id and s.last_node_id == wait.node_id
                           and abs(s.velocity) < 0.01 and math.hypot(s.pose.x-wait.position.x, s.pose.y-wait.position.y) < 0.08)
            else:
                # 이미 안에 있는 로봇이 다음 작업을 받으면 같은 존에서 퇴장할 수 있다.
                origin = c.get("start")
                near_origin = origin and math.hypot(s.pose.x - origin["x"], s.pose.y - origin["y"]) < 0.08
                at_wait = (s.order_id in (None, order.order_id) and abs(s.velocity) < 0.01
                           and (near_origin or intersects(c["bounds"], s.pose, radius(s) + STOP_CLEARANCE)))
            if at_wait:
                self._arrived_at_wait(job, c, leases.get(wk), owner is not None)
            resident = wait is None and owner and self._can_transfer_automatic(owner, job, s, t["version"])
            # gate 존(자동문 제외)은 줄 맨 앞(대기 node 예약)이고 대기 node 에 PASS_THROUGH_DISTANCE 안으로 왔으면
            # 서기 전에도 진입을 허가한다. 존이 비어 있으면 먼저 온 로봇은 멈추지 않고 지나간다.
            # 멀리서 미리 잡지 않는다 (가는 동안 존을 묶어 두고, 장애물에 막혀도 재경로할 수 없게 된다).
            # 자동문은 진입점에서 문을 열어야 하므로 선다.
            head = (wait is not None and not automatic and not door
                    and math.hypot(s.pose.x - wait.position.x, s.pose.y - wait.position.y) <= PASS_THROUGH_DISTANCE)
            if (at_wait or head) and not automatic:
                c.setdefault("at_head", time.time())   # 줄 맨 앞에 도착한 시각. 다른 방향 줄과 진입 순서를 가린다
            ready = ((at_wait or head) and (not owner or resident) and self._zone_clear(job, c)
                     and self._clear_space(job, end.position, radius(s), aside=not automatic)
                     and (automatic or self._zone_turn(job, c)))
            if ready and door:
                # 자동문: 대기(진입) node 에서 열림 신호를 보내고, 열린 것을 확인해야 존에 들어간다.
                if not self._door_open(job, door):
                    through = c["wait"]
                    self.set_waiting(job, self.doors.reason(door) if self.doors else "자동문 제어기 없음", c, "at_wait")
                    break
            if ready:
                leases[zk] = {"map": t["map"], "robot": job.robot_id, "job": job.job_id,
                              "kind": "zone", "at": time.time(), "order": order.order_id,
                              "pose": end.position.model_dump(), "radius": radius(s),
                              "exit_id": end.node_id, "exit": c["exit"], "bounds": c["bounds"],
                              "update": order.order_update_id+1, "door": door}
                if automatic:
                    leases[zk].update(automatic=True, zone=c["zone"], version=t["version"], stage=job.stage,
                                      approach=(wait.position if wait else s.pose).model_dump(),
                                      egress=end.position.model_dump() if c["exit"] is not None else None)
                self.save()
                now = leases[zk]["at"]
                self._event("traffic.reserve", leases[zk], zk, zone_id=c["zone"], gate_id=c["gate"],
                            queued_s=round(now - c["arrived"], 2) if c.get("arrived") else None)
                self._zone_entered(zk, now)
                through = end_seq
                self.set_waiting(job, None)
            else:
                through = c["wait"] if wait is not None else -1
                self.set_waiting(job, "점유 존 진입 허가 대기", c, "at_wait" if at_wait else "approach")
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
                        add(job, t.get("blocked_by"), zone_key)   # 노드 없는 존의 퇴장 공간·다른 방향 줄의 선착 로봇
                        for r in self.fleet.all():
                            if (r.state and r.state.map_id == t.get("uid")
                                    and intersects(current["bounds"], r.state.pose, radius(r.state))):
                                add(job, r.robot_id, zone_key)
                    elif waiting.startswith("대기열"):
                        add(job, t.get("blocked_by"), wait_key)   # 바로 앞 자리 로봇 / 줄에 먼저 설 로봇
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
