"""로봇 State 의 **변화만** 이벤트 로그에 남기는 `Fleet` 리스너.

State 는 200 ms 마다 오므로 전체를 쌓지 않는다. 직전 값은 메모리에만 둔다 (재시작하면 첫 State 를 기준으로 다시 잡는다).

- 변화: state/sub_state, liveness, paused, mode, localization, 오류 발생·해제, 배터리 10 % 단위.
- 사용량: 상태별 체류 시간과 주행 거리를 `robot.usage` 로 주기 저장한다 (KPI 가동률·주행 거리의 원본).

리스너는 paho 스레드에서 불린다. 여기서는 메모리 비교와 `EventLog.record`(큐 넣기)만 한다.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

from common.schemas import OperatingMode, RobotState
from fms_server.fleet import Fleet, FleetEvent, RobotLiveness

USAGE_PERIOD = 60.0      # 상태 체류 시간·주행 거리를 저장하는 주기 (초)
JUMP_LIMIT = 2.0         # 한 State 사이 이동이 이보다 크면 위치 재설정(teleport·힌트)으로 보고 거리에 넣지 않는다


def status_of(state, in_service: bool = True) -> str:
    """ARC 식 한 줄 상태: working / idle / charging / manual / service / error."""
    if state is None:
        return "offline"
    if state.state in (RobotState.ERROR, RobotState.EMERGENCY):
        return "error"
    if not in_service:
        return "service"
    if state.mode == OperatingMode.MANUAL:
        return "manual"
    if state.state == RobotState.CHARGING:
        return "charging"
    if state.state == RobotState.BUSY:
        return "working"
    return "idle"


class RobotEventRecorder:
    def __init__(self, fleet: Fleet, registry, events, *, clock=time.time) -> None:
        self._fleet, self._registry, self._events = fleet, registry, events
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[str, dict] = {}
        self._unsubscribe = None

    def start(self) -> None:
        self._unsubscribe = self._fleet.add_listener(self._on_fleet_event)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            for robot_id, last in self._last.items():
                self._flush_usage(robot_id, last, self._clock())

    # -- 리스너 (paho 스레드) ----------------------------------------------

    def _site(self, robot_id: str) -> Optional[str]:
        assignment = self._registry.assignment_for(robot_id)
        return assignment.site_id if assignment else None

    def _in_service(self, robot_id: str) -> bool:
        assignment = self._registry.assignment_for(robot_id)
        return getattr(assignment, "in_service", True) if assignment else True

    def _on_fleet_event(self, event: FleetEvent) -> None:
        if event.kind == "removed":
            with self._lock:
                self._last.pop(event.robot_id, None)
            return
        with self._lock:
            self._observe(event.robot_id, event.record)

    def tick(self) -> None:
        """State 가 끊긴 로봇의 STALE 전환과 사용량 주기 저장을 본다 (배정 루프에서 1초마다)."""
        records = {r.robot_id: r for r in self._fleet.all()}
        with self._lock:
            for robot_id, record in records.items():
                self._observe(robot_id, record, from_tick=True)

    def _observe(self, robot_id: str, record, from_tick: bool = False) -> None:
        now = self._clock()
        site = self._site(robot_id)
        record_event = lambda kind, severity="info", **data: self._events.record(
            kind, site_id=site, robot_id=robot_id, severity=severity, **data)
        last = self._last.get(robot_id)
        liveness = record.liveness(self._fleet.stale_after).value
        state = record.state
        status = status_of(state, self._in_service(robot_id)) if liveness == RobotLiveness.ONLINE.value else "offline"
        if last is None:
            last = self._last[robot_id] = {"liveness": None, "status": status, "since": now, "usage_at": now,
                                           "distance": 0.0, "pose": None, "map": None}
        if liveness != last["liveness"]:
            bad = liveness in (RobotLiveness.LOST.value, RobotLiveness.STALE.value)
            record_event("robot.liveness", "warning" if bad else "info", liveness=liveness, previous=last["liveness"])
            last["liveness"] = liveness
        if state is None:
            return

        # 주행 거리: 같은 맵에서 이어진 State 만 적분한다.
        if not from_tick:
            pose, previous = (state.pose.x, state.pose.y), last["pose"]
            if previous is not None and last["map"] == state.map_id:
                step = math.hypot(pose[0] - previous[0], pose[1] - previous[1])
                if step < JUMP_LIMIT:
                    last["distance"] += step
            last["pose"], last["map"] = pose, state.map_id

        if status != last["status"]:
            self._flush_usage(robot_id, last, now, site)
            last["status"] = status
        elif now - last["usage_at"] >= USAGE_PERIOD:
            self._flush_usage(robot_id, last, now, site)
        if from_tick:
            return

        display = (state.state.value, state.sub_state.value if state.sub_state else None)
        if display != last.get("state"):
            severity = "error" if state.state in (RobotState.ERROR, RobotState.EMERGENCY) else "info"
            if last.get("state") is not None or severity == "error":
                record_event("robot.state", severity, state=display[0], sub_state=display[1],
                             previous=list(last["state"]) if last.get("state") else None)
            last["state"] = display
        if state.paused != last.get("paused"):
            if last.get("paused") is not None or state.paused:
                record_event("robot.paused" if state.paused else "robot.resumed",
                             source=state.pause_source.value if state.pause_source else None)
            last["paused"] = state.paused
        if state.mode.value != last.get("mode"):
            if last.get("mode") is not None:
                record_event("robot.mode", mode=state.mode.value)
            last["mode"] = state.mode.value
        if state.localization.value != last.get("localization"):
            if last.get("localization") is not None:
                record_event("robot.localization", "info" if state.localization.value == "LOCALIZED" else "warning",
                             localization=state.localization.value)
            last["localization"] = state.localization.value
        errors = {e.error_type: e.level.value for e in state.errors}
        previous_errors = last.get("errors", {})
        for error_type, level in errors.items():
            if error_type not in previous_errors:
                record_event("robot.error", "error" if level == "FATAL" else "warning",
                             error_type=error_type, level=level)
        for error_type in previous_errors:
            if error_type not in errors:
                record_event("robot.error_cleared", error_type=error_type)
        last["errors"] = errors
        band = int(state.battery.charge // 10)
        if band != last.get("battery_band"):
            if last.get("battery_band") is not None:
                record_event("robot.battery", charge=round(state.battery.charge, 1),
                             charging=state.battery.charging)
            last["battery_band"] = band
        if state.battery.charging != last.get("charging"):
            if last.get("charging") is not None:
                record_event("robot.charging_started" if state.battery.charging else "robot.charging_stopped",
                             charge=round(state.battery.charge, 1))
            last["charging"] = state.battery.charging

    def _flush_usage(self, robot_id: str, last: dict, now: float, site: Optional[str] = None) -> None:
        seconds = now - last["usage_at"]
        if seconds <= 0 and last["distance"] <= 0:
            return
        if site is None:
            site = self._site(robot_id)
        self._events.record("robot.usage", site_id=site, robot_id=robot_id, status=last["status"],
                            seconds=round(seconds, 2), distance=round(last["distance"], 3))
        last["usage_at"], last["distance"] = now, 0.0
