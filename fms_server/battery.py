"""자동 충전·유휴 대기 위치 복귀·대피 pullover.

셋 다 "작업이 없는 로봇을 어디론가 보내는 **시스템 작업**"이라 한 곳에 둔다.
`tick()` 은 배정 루프(`allocator.py`)가 1초마다 부른다. 새 스레드는 없다.

- 정책: `sites/{site}/charging.json`. 기본은 꺼짐 (파일이 없으면 이전 동작 그대로).
- 시스템 작업은 `Job.system` 으로 구분한다: `charge` / `staging` / `evacuation`.
  로봇을 지정해 바로 `dispatch` 한다. 실패하면 작업을 지우고 이유만 남긴다.
- staging 은 선점 가능하다 (`JobCoordinator.allocate_pending` 이 일반 작업을 위해 취소한다).
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from common.schemas import RobotState
from fms_server import charger_pool
from fms_server.task import JobError, JobStatus, _check_seg, _parse_command, resolve_steps

log = logging.getLogger("fms.battery")

POLICY_FILE = "charging.json"
RETRY_COOLDOWN = 60.0   # 같은 로봇에 시스템 작업을 다시 만들기까지 (실패 반복 방지)

DEFAULT_POLICY = {
    "enabled": False,          # 자동 충전
    "low": 25,                 # 이 미만이면 충전하러 보낸다 (%)
    "critical": 10,            # 이 미만이면 알림 (%). 충전 순서도 낮은 로봇 먼저
    "release": 60,             # 도킹 min_charge: 이만큼 차야 다음 작업을 받는다 (%)
    "opportunity": False,      # 유휴 시간에 미리 충전 (빈 충전소가 있을 때)
    "min_job_charge": 20,      # 이 미만인 로봇에는 일반 작업을 배정하지 않는다 (%). enabled 일 때만 적용
    "idle_after": 60,          # 기회 충전을 시작할 유휴 시간 (초)
    "staging": {"enabled": False, "idle_after": 60},   # 유휴 로봇을 parking 위치로 돌려보낸다
}


class PolicyError(ValueError):
    pass


def _validate(policy: dict) -> dict:
    out = copy.deepcopy(DEFAULT_POLICY)
    if not isinstance(policy, dict):
        raise PolicyError("정책은 객체여야 한다")
    unknown = set(policy) - set(out)
    if unknown:
        raise PolicyError(f"알 수 없는 항목: {', '.join(sorted(unknown))}")
    for key, value in policy.items():
        if key == "staging":
            if not isinstance(value, dict) or set(value) - set(out["staging"]):
                raise PolicyError("staging 은 {enabled, idle_after} 객체여야 한다")
            out["staging"].update(value)
        else:
            out[key] = value
    for key in ("enabled", "opportunity"):
        if not isinstance(out[key], bool):
            raise PolicyError(f"{key} 는 true/false 여야 한다")
    if not isinstance(out["staging"]["enabled"], bool):
        raise PolicyError("staging.enabled 는 true/false 여야 한다")
    for key in ("low", "critical", "release", "min_job_charge"):
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
            raise PolicyError(f"{key} 는 0~100 숫자여야 한다")
    for holder, key in ((out, "idle_after"), (out["staging"], "idle_after")):
        v = holder[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise PolicyError("idle_after 는 0 이상 초 단위 숫자여야 한다")
    if not out["critical"] <= out["low"] < out["release"]:
        raise PolicyError("critical <= low < release 순서여야 한다")
    if out["min_job_charge"] >= out["release"]:
        raise PolicyError("min_job_charge 는 release 보다 작아야 한다 (충전을 마친 로봇이 작업을 못 받는다)")
    return out


class BatteryManager:
    def __init__(self, coordinator, registry, fleet, jobs, data_root, *, events=None, clock=time.time) -> None:
        # 저장소는 코디네이터 것을 따라간다 (테스트가 코디네이터의 저장소를 갈아끼워도 같은 것을 본다).
        self.coordinator = coordinator
        self._own = (registry, fleet, jobs)
        self.root = Path(data_root) / "sites"
        self.events = events
        self._clock = clock
        self._lock = threading.RLock()
        self._policies: dict[str, dict] = {}
        self._idle_since: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}
        self._noted: dict[str, str] = {}   # robot_id -> 마지막으로 이벤트에 남긴 "못 보낸 이유"

    @property
    def registry(self):
        return getattr(self.coordinator, "_registry", self._own[0])

    @property
    def fleet(self):
        return getattr(self.coordinator, "_fleet", self._own[1])

    @property
    def jobs(self):
        return getattr(self.coordinator, "_store", self._own[2])

    # -- 정책 -------------------------------------------------------------

    def _path(self, site_id: str) -> Path:
        _check_seg(site_id, "site_id")
        return self.root / site_id / POLICY_FILE

    def policy(self, site_id: str) -> dict:
        with self._lock:
            if site_id not in self._policies:
                path = self._path(site_id)
                try:
                    stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                    self._policies[site_id] = _validate(stored)
                except (ValueError, OSError) as exc:
                    log.error("%s 충전 정책을 읽을 수 없다 — 기본값(꺼짐) 사용: %s", site_id, exc)
                    self._policies[site_id] = copy.deepcopy(DEFAULT_POLICY)
            return copy.deepcopy(self._policies[site_id])

    def set_policy(self, site_id: str, policy: dict) -> dict:
        self.registry.get_site(site_id)
        validated = _validate(policy)
        with self._lock:
            path = self._path(site_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
            self._policies[site_id] = validated
        return copy.deepcopy(validated)

    # -- 공통 -------------------------------------------------------------

    def _event(self, kind, site_id, robot_id, severity="info", **data):
        if self.events is not None:
            self.events.record(kind, site_id=site_id, robot_id=robot_id, severity=severity, **data)

    def _note(self, kind, site_id, robot_id, reason):
        """같은 이유를 매 tick 남기지 않는다."""
        key = f"{kind}:{reason}"
        if self._noted.get(robot_id) != key:
            self._noted[robot_id] = key
            self._event(kind, site_id, robot_id, "warning", reason=reason)

    def _free_robots(self, site):
        """작업도 예약도 없는 가용 로봇 (등록·서비스 중)."""
        active = {j.robot_id for j in self.jobs.list(site.site_id)
                  if j.robot_id and j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)}
        # 로봇이 지정된 pending 작업(수동 배정 대기)이 있으면 건드리지 않는다.
        active |= {j.robot_id for j in self.jobs.list(site.site_id)
                   if j.robot_id and j.status == JobStatus.PENDING}
        reserved = set(getattr(self.coordinator, "_reservations", {}))
        out = []
        for record in self.fleet.available_robots():
            assignment = site.robots.get(record.robot_id)
            if (assignment is None or record.robot_id in active or record.robot_id in reserved):
                continue
            out.append((record, assignment))
        return sorted(out, key=lambda pair: pair[0].robot_id)

    def _send(self, site_id, robot_id, system, commands, priority=0) -> Optional[str]:
        """시스템 작업을 만들고 바로 배정한다. 실패 이유를 돌려준다 (성공이면 None)."""
        try:
            resolved = resolve_steps([_parse_command(c) for c in commands], site_id, self.registry)
            job = self.jobs.create(site_id, commands, robot_id, resolved_steps=resolved,
                                   system=system, priority=priority,
                                   preset_name={"charge": "자동 충전", "staging": "대기 위치 복귀",
                                                "evacuation": "대피 이동"}[system])
        except JobError as exc:
            return str(exc)
        try:
            self.coordinator.dispatch(job.job_id)
        except JobError as exc:
            self.jobs.delete(job.job_id)
            return str(exc)
        self._cooldown[robot_id] = self._clock()
        self._noted.pop(robot_id, None)
        return None

    def _parking_commands(self, spot: dict) -> list[dict]:
        if spot["entry"] is None:
            return [{"type": "move", "target": spot["name"]}]   # 진입 직선을 낼 공간이 없으면 단순 이동
        return [{"type": "parking", "target": spot["name"], "params": {
            "entry": spot["entry"], "undock_distance": charger_pool.DOCK_UNDOCK_DISTANCE, "speed": 0.2}}]

    def _resting(self, site_id, assignment, state, chargers, parkings) -> bool:
        """이미 충전소·대기 위치에 서 있나."""
        if state.state == RobotState.CHARGING or state.battery.charging:
            return True
        x, y = state.pose.x, state.pose.y
        return bool(charger_pool.at_spot(chargers, assignment.map_id, x, y)
                    or charger_pool.at_spot(parkings, assignment.map_id, x, y))

    # -- 주기 -------------------------------------------------------------

    def tick(self) -> None:
        if not getattr(self.coordinator._mqtt, "connected", True):
            return
        with self.coordinator.lock:
            now = self._clock()
            seen = set()
            for site in self.registry.list_sites():
                try:
                    self._tick_site(site, now, seen)
                except Exception:
                    log.exception("%s 자동 충전·대기 처리 중 오류", site.site_id)
            for robot_id in list(self._idle_since):
                if robot_id not in seen:
                    del self._idle_since[robot_id]

    def _tick_site(self, site, now, seen) -> None:
        evacuation = site.ops["evacuation"]
        if evacuation["active"]:
            if evacuation["mode"] == "pullover":
                self._tick_pullover(site, now)
            return
        policy = self.policy(site.site_id)
        if not policy["enabled"] and not policy["staging"]["enabled"]:
            return
        free = self._free_robots(site)
        if not free:
            return
        chargers = charger_pool.chargers(self.registry, site.site_id)
        parkings = charger_pool.parkings(self.registry, site.site_id)
        claimed: set[str] = set()   # 이번 tick 에 이미 내준 자리
        # 배터리가 낮은 로봇부터 충전소를 고른다.
        for record, assignment in sorted(free, key=lambda p: (p[0].state.battery.charge, p[0].robot_id)):
            robot_id, state = record.robot_id, record.state
            seen.add(robot_id)
            idle = now - self._idle_since.setdefault(robot_id, now)
            if site.paused_for(assignment.map_id) or not assignment.in_service:
                continue
            if now - self._cooldown.get(robot_id, -1e9) < RETRY_COOLDOWN:
                continue
            charging = state.state == RobotState.CHARGING or state.battery.charging
            charge = state.battery.charge
            want_charge = policy["enabled"] and not charging and (
                charge < policy["low"]
                or (policy["opportunity"] and idle >= policy["idle_after"] and charge < policy["release"]
                    and not charger_pool.at_spot(chargers, assignment.map_id, state.pose.x, state.pose.y)))
            if want_charge:
                low = charge < policy["low"]
                pool = charger_pool.free_chargers(
                    self.registry, self.fleet, self.jobs, site.site_id, robot_id,
                    robot_groups=assignment.groups, extra_busy=claimed, pool=chargers)
                if not pool:
                    if low:
                        self._note("charge.no_charger", site.site_id, robot_id,
                                   "빈 충전소 없음" if chargers else "등록된 충전소 없음")
                else:
                    spot = charger_pool.sort_by_distance(self.registry, site.site_id, pool, assignment.map_id,
                                                         (state.pose.x, state.pose.y))[0]
                    problem = self._send(site.site_id, robot_id, "charge", [{
                        "type": "docking", "target": spot["name"], "params": {
                            "entry": spot["entry"], "undock_distance": charger_pool.DOCK_UNDOCK_DISTANCE,
                            "speed": 0.2, "min_charge": policy["release"]}}], priority=10 if low else 0)
                    if problem is None:
                        claimed.add(spot["name"])
                        self._event("charge.requested", site.site_id, robot_id, charger=spot["name"],
                                    charge=round(charge, 1), opportunity=not low)
                        continue
                    if low:
                        self._note("charge.no_charger", site.site_id, robot_id, problem)
                if low:
                    continue   # 충전이 급한 로봇은 대기 위치로 보내지 않는다
            staging = policy["staging"]
            if (staging["enabled"] and idle >= staging["idle_after"]
                    and not self._resting(site.site_id, assignment, state, chargers, parkings)):
                pool = [p for p in charger_pool.free_parkings(
                    self.registry, self.fleet, self.jobs, site.site_id, robot_id,
                    robot_groups=assignment.groups, map_id=assignment.map_id) if p["name"] not in claimed]
                if pool:
                    spot = charger_pool.sort_by_distance(self.registry, site.site_id, pool, assignment.map_id,
                                                         (state.pose.x, state.pose.y))[0]
                    if self._send(site.site_id, robot_id, "staging", self._parking_commands(spot)) is None:
                        claimed.add(spot["name"])
                        self._event("staging.requested", site.site_id, robot_id, parking=spot["name"])

    # -- 즉시 비켜주기 (traffic 이 유휴 로봇을 차단 원인으로 찾았을 때) -------------

    def move_aside(self, site_id: str, robot_id: str) -> bool:
        """유휴 로봇을 지금 바로 가장 가까운 빈 대기 위치로 보낸다. 보냈으면 True. 코디네이터 락 안에서 부른다."""
        now = self._clock()
        if now - self._cooldown.get(robot_id, -1e9) < RETRY_COOLDOWN:
            return False
        site = self.registry.get_site(site_id)
        pair = next((p for p in self._free_robots(site) if p[0].robot_id == robot_id), None)
        if pair is None or site.paused_for(pair[1].map_id) or site.evacuating:
            return False
        record, assignment = pair
        pool = charger_pool.free_parkings(self.registry, self.fleet, self.jobs, site_id, robot_id,
                                          robot_groups=assignment.groups, map_id=assignment.map_id)
        self._cooldown[robot_id] = now   # 자리가 없어도 매 tick 다시 찾지 않는다
        if not pool:
            return False
        spot = charger_pool.sort_by_distance(self.registry, site_id, pool, assignment.map_id,
                                             (record.state.pose.x, record.state.pose.y))[0]
        if self._send(site_id, robot_id, "staging", self._parking_commands(spot)) is None:
            self._event("staging.requested", site_id, robot_id, parking=spot["name"], reason="비켜주기")
            return True
        return False

    # -- 대피 pullover ----------------------------------------------------

    def _cancel_for_evacuation(self, site, actor=None) -> list[str]:
        canceled = []
        for job in self.jobs.list(site.site_id):
            if (job.status not in (JobStatus.ASSIGNED, JobStatus.WORKING) or job.system == "evacuation"
                    or self.coordinator.in_car(job)):
                continue
            try:
                self.coordinator.cancel(job.job_id, actor=actor or "system", reason="대피")
                canceled.append(job.job_id)
            except JobError as exc:
                log.warning("대피 취소 실패 %s: %s", job.job_id, exc)
        return canceled

    def start_pullover(self, site_id: str, *, actor=None) -> list[str]:
        """배정을 멈추고(현장 ops 가 막는다) 진행 작업을 취소한다. 취소한 job_id 목록."""
        with self.coordinator.lock:
            return self._cancel_for_evacuation(self.registry.get_site(site_id), actor)

    def end_pullover(self, site_id: str) -> None:
        """해제. 취소된 작업은 자동 재게시하지 않는다. 진행 중인 대피 이동은 끝까지 간다."""
        for robot_id in list(self._noted):
            self._noted.pop(robot_id, None)

    def _tick_pullover(self, site, now) -> None:
        # 칸 안에 있던 작업이 하차를 마치면 그때 취소한다.
        late = self._cancel_for_evacuation(site)
        if late:
            self.registry.update_ops(site.site_id, lambda ops: ops["evacuation"]["canceled_jobs"].extend(
                j for j in late if j not in ops["evacuation"]["canceled_jobs"]))
        free = self._free_robots(site)
        if not free:
            return
        chargers = charger_pool.chargers(self.registry, site.site_id)
        parkings = charger_pool.parkings(self.registry, site.site_id)
        claimed: set[str] = set()
        for record, assignment in free:
            robot_id, state = record.robot_id, record.state
            if now - self._cooldown.get(robot_id, -1e9) < RETRY_COOLDOWN:
                continue
            if self._resting(site.site_id, assignment, state, chargers, parkings):
                continue
            # 같은 층의 가장 가까운 빈 대기 위치. 층을 옮기지 않는다 (승강기 신규 임대 금지).
            pool = [p for p in charger_pool.free_parkings(
                self.registry, self.fleet, self.jobs, site.site_id, robot_id, map_id=assignment.map_id)
                if p["name"] not in claimed]
            if not pool:
                self._note("site.evacuation_no_parking", site.site_id, robot_id, "같은 층에 빈 대기 위치 없음")
                continue
            spot = charger_pool.sort_by_distance(self.registry, site.site_id, pool, assignment.map_id,
                                                 (state.pose.x, state.pose.y))[0]
            problem = self._send(site.site_id, robot_id, "evacuation", self._parking_commands(spot), priority=100)
            if problem is None:
                claimed.add(spot["name"])
            else:
                self._note("site.evacuation_no_parking", site.site_id, robot_id, problem)
