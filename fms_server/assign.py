"""비용 기반 로봇 선택. 게시판 작업 하나에 대해 후보 로봇을 가까운 순으로 줄 세운다.

전체 최적 할당(헝가리안)은 하지 않는다 — 작업은 우선순위 순으로 하나씩 고른다 (greedy).
최종 가용성 검사는 `JobCoordinator.dispatch` 가 한다. 여기는 순서만 정한다.
"""
from __future__ import annotations

import math
from typing import Optional

FLOOR_PENALTY = 60.0       # 승강기 한 번 타는 비용을 주행 거리로 환산한 값 (m)
BATTERY_WEIGHT = 0.2       # 배터리 1 % 부족당 가산 (m)
UNREACHABLE = 1.0e6        # 거리 계산이 안 되는 후보는 뒤로 민다 (제외하지는 않는다 — 비교 격자가 거칠다)


def _goal(job) -> Optional[dict]:
    return job.resolved_steps[0] if job.resolved_steps else None


_ACTIONS = {"wait": "WAIT", "docking": "DOCK", "parking": "PARK", "pickup": "PICK", "drop": "DROP"}


def _needed_actions(job) -> set:
    return {_ACTIONS[c.kind] for c in job.commands if getattr(c, "kind", None) in _ACTIONS}


def eligible(job, record, assignment, policy=None) -> Optional[str]:
    """이 로봇이 후보가 될 수 없는 이유 (될 수 있으면 None)."""
    if assignment is None or assignment.site_id != job.site_id:
        return "다른 현장 로봇"
    if not assignment.in_service:
        return "점검 중인 로봇"
    if job.robot_group and job.robot_group not in assignment.groups:
        return f"그룹 {job.robot_group} 로봇이 아니다"
    if not record.supports(_needed_actions(job)):
        return "이 작업의 액션을 지원하지 않는 로봇 (factsheet)"
    if (policy is not None and policy.get("enabled") and job.system != "charge" and record.state is not None
            and record.state.battery.charge < policy.get("min_job_charge", 0)):
        return f"배터리 {record.state.battery.charge:.0f}% — 작업 수락 기준 {policy['min_job_charge']}% 미만"
    return None


def rank_candidates(job, records, registry, elevators=None, policy=None) -> tuple[list[str], Optional[str]]:
    """(가까운 순 robot_id 목록, 후보가 없을 때의 이유).

    같은 맵: 첫 목적지까지 `route.path_distances` 거리. 다른 층: 승강기 환승 수 × 벌점 + 승강장까지 거리.
    배터리가 낮을수록 가산. 동점은 로봇 ID.
    """
    from fms_server.charger_pool import map_model
    from fms_server.route import RouteError, _find_hops, path_distances

    goal = _goal(job)
    reason = None
    candidates = []
    for record in records:
        assignment = registry.assignment_for(record.robot_id)
        problem = eligible(job, record, assignment, policy)
        if problem:
            reason = problem
            continue
        candidates.append((record, assignment))
    if not candidates:
        return [], reason
    if goal is None:
        return sorted(r.robot_id for r, _ in candidates), None

    store = registry.map_store(job.site_id)
    all_elevators = elevators.list(job.site_id) if elevators is not None else []
    costs: dict[str, float] = {}
    by_map: dict[str, list] = {}
    for record, assignment in candidates:
        by_map.setdefault(assignment.map_id, []).append(record)
    goal_xy = (goal["pose"]["x"], goal["pose"]["y"])
    for map_id, group in by_map.items():
        robots_xy = [(r.state.pose.x, r.state.pose.y) for r in group]
        try:
            model = map_model(store, job.site_id, map_id)
        except Exception:
            model = None
        if map_id == goal["map_id"]:
            # 목적지에서 한 번만 탐색한다 (격자 거리는 대칭이다).
            distances = _safe(path_distances, model, goal_xy, robots_xy) if model is not None else None
            extra = 0.0
        else:
            try:
                hops = _find_hops(all_elevators, map_id, goal["map_id"])
            except RouteError:
                for r in group:
                    costs[r.robot_id] = UNREACHABLE * 2
                continue
            extra = FLOOR_PENALTY * len(hops)
            # 이 층에서 다음 층으로 가는 승강기 중 가장 가까운 승강장까지.
            landings = []
            for e in all_elevators:
                if any(s.map_id == hops[0][0] for s in e.stops) and any(s.map_id == hops[0][1] for s in e.stops):
                    stop = next(s for s in e.stops if s.map_id == map_id)
                    try:
                        landings.append(elevators.geometry(job.site_id, stop).outside[:2])
                    except Exception:
                        pass
            distances = None
            if model is not None and landings:
                per_landing = [_safe(path_distances, model, xy, robots_xy) for xy in landings]
                per_landing = [d for d in per_landing if d is not None]
                if per_landing:
                    distances = [min(d[i] for d in per_landing) for i in range(len(group))]
        for i, r in enumerate(group):
            d = distances[i] if distances is not None else math.hypot(
                r.state.pose.x - goal_xy[0], r.state.pose.y - goal_xy[1])
            if not math.isfinite(d):
                d = UNREACHABLE
            costs[r.robot_id] = d + extra + BATTERY_WEIGHT * (100.0 - r.state.battery.charge)
    return sorted(costs, key=lambda rid: (costs[rid], rid)), None


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None
