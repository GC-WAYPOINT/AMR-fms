"""알림 — 운영자가 봐야 하는 상황을 `events.db` 의 `alerts` 테이블에 연다.

같은 `key` 로는 열린 알림이 하나뿐이다. 두 종류가 있다.

- 상태 알림: `tick()` 이 현재 상태로 조건을 다시 본다. 조건이 사라지면 스스로 닫힌다 (resolved).
  로봇 ERROR/EMERGENCY, LOST/STALE 지속, 배터리 critical, pending 장기화, 교착, 승강기 STALE·장애·확인 필요, 배송 차단.
- 사건 알림: 이벤트가 열고, 운영자가 확인(ack)하면 닫힌다. 작업 실패, 마감 초과.

열림·해소·확인은 이벤트 로그에도 남긴다 (`alert.*`) — 실시간 스트림과 webhook 이 그걸 받는다.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from common.schemas import ErrorLevel, RobotState
from fms_server.fleet import RobotLiveness

log = logging.getLogger("fms.alerts")

OFFLINE_AFTER = 30.0      # LOST/STALE 이 이만큼 이어지면 알린다 (초)
PENDING_AFTER = 600.0     # 게시판 작업이 이만큼 배정되지 않으면 알린다 (초)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT,
    rule TEXT NOT NULL,
    key TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    opened_at REAL NOT NULL,
    acked_by TEXT,
    acked_at REAL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts(resolved_at, key);
"""
EVENT_RULES = {"job.failed": "job_failed", "job.deadline_missed": "deadline_missed"}


def _row(r) -> dict:
    return {"id": r[0], "site_id": r[1], "rule": r[2], "key": r[3], "severity": r[4], "message": r[5],
            "data": json.loads(r[6] or "{}"), "opened_at": r[7], "acked_by": r[8], "acked_at": r[9],
            "resolved_at": r[10], "open": r[10] is None}


class AlertManager:
    def __init__(self, events, coordinator, *, battery=None, locker=None, elevators=None, clock=time.time) -> None:
        self.events, self.coordinator = events, coordinator
        self.battery, self.locker, self._elevators = battery, locker, elevators
        self._clock = clock
        self._lock = threading.RLock()
        self._offline_since: dict[str, float] = {}
        with events.db_lock:
            events.db.executescript(_SCHEMA)
            events.db.commit()
        events.add_listener(self._on_event)

    # -- 저장 -------------------------------------------------------------

    def _open_keys(self) -> dict[str, int]:
        with self.events.db_lock:
            return {key: alert_id for alert_id, key in self.events.db.execute(
                "SELECT id, key FROM alerts WHERE resolved_at IS NULL").fetchall()}

    def open(self, rule: str, key: str, message: str, *, site_id=None, severity="warning", **data) -> Optional[int]:
        """같은 key 의 열린 알림이 없을 때만 연다. 새로 열었으면 id."""
        with self._lock, self.events.db_lock:
            if self.events.db.execute("SELECT 1 FROM alerts WHERE key = ? AND resolved_at IS NULL",
                                      (key,)).fetchone():
                return None
            cursor = self.events.db.execute(
                "INSERT INTO alerts(site_id, rule, key, severity, message, data, opened_at) VALUES (?,?,?,?,?,?,?)",
                (site_id, rule, key, severity, message, json.dumps(data, ensure_ascii=False, default=str),
                 self._clock()))
            self.events.db.commit()
            alert_id = cursor.lastrowid
        self.events.record("alert.opened", site_id=site_id, severity=severity, robot_id=data.get("robot_id"),
                           job_id=data.get("job_id"), alert_id=alert_id, rule=rule, key=key, message=message)
        return alert_id

    def resolve(self, key: str, *, by: Optional[str] = None) -> bool:
        with self._lock, self.events.db_lock:
            row = self.events.db.execute(
                "SELECT id, site_id, rule FROM alerts WHERE key = ? AND resolved_at IS NULL", (key,)).fetchone()
            if row is None:
                return False
            self.events.db.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (self._clock(), row[0]))
            self.events.db.commit()
        self.events.record("alert.resolved", site_id=row[1], alert_id=row[0], rule=row[2], key=key, actor=by)
        return True

    def ack(self, alert_id: int, actor: str) -> Optional[dict]:
        """확인. 사건 알림(작업 실패·마감 초과)은 확인하면 닫힌다. 상태 알림은 조건이 사라질 때 닫힌다."""
        with self._lock, self.events.db_lock:
            row = self.events.db.execute("SELECT rule, key, resolved_at FROM alerts WHERE id = ?",
                                         (alert_id,)).fetchone()
            if row is None:
                return None
            now = self._clock()
            self.events.db.execute("UPDATE alerts SET acked_by = ?, acked_at = ? WHERE id = ?",
                                   (actor, now, alert_id))
            if row[0] in EVENT_RULES.values() and row[2] is None:
                self.events.db.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (now, alert_id))
            self.events.db.commit()
        self.events.record("alert.acked", alert_id=alert_id, rule=row[0], key=row[1], actor=actor)
        return self.get(alert_id)

    def get(self, alert_id: int) -> Optional[dict]:
        with self.events.db_lock:
            row = self.events.db.execute(
                "SELECT id, site_id, rule, key, severity, message, data, opened_at, acked_by, acked_at, resolved_at "
                "FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return _row(row) if row else None

    def list(self, *, site: Optional[str] = None, open_only: bool = True, limit: int = 200) -> list[dict]:
        where, args = [], []
        if site is not None:
            where.append("(site_id = ? OR site_id IS NULL)")
            args.append(site)
        if open_only:
            where.append("resolved_at IS NULL")
        sql = ("SELECT id, site_id, rule, key, severity, message, data, opened_at, acked_by, acked_at, resolved_at "
               "FROM alerts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?")
        with self.events.db_lock:
            return [_row(r) for r in self.events.db.execute(sql, (*args, max(1, min(limit, 2000)))).fetchall()]

    # -- 사건 알림 (이벤트 로그 워커 스레드) ---------------------------------

    def _on_event(self, event: dict) -> None:
        rule = EVENT_RULES.get(event["kind"])
        if rule is None or event["data"].get("system") == "staging":
            return
        job = event["job_id"]
        if rule == "job_failed":
            message = f"작업 {job} 실패: {event['data'].get('error') or '원인 미상'}"
        else:
            message = f"작업 {job} 마감 초과 ({event['data'].get('deadline')})"
        self.open(rule, f"{rule}:{job}", message, site_id=event["site_id"],
                  severity="error" if rule == "job_failed" else "warning",
                  job_id=job, robot_id=event["robot_id"])

    # -- 상태 알림 (배정 루프, 1초) ------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        try:
            wanted = self._evaluate(now)
        except Exception:
            log.exception("알림 규칙 평가 실패")
            return
        state_rules = ("robot_error", "robot_offline", "battery_critical", "job_pending", "deadlock",
                       "elevator_stale", "elevator_fault", "elevator_attention", "delivery_blocked")
        for key, alert in wanted.items():
            self.open(alert.pop("rule"), key, alert.pop("message"), **alert)
        for key in self._open_keys():
            if key.split(":", 1)[0] in state_rules and key not in wanted:
                self.resolve(key)

    def _evaluate(self, now: float) -> dict[str, dict]:
        coordinator = self.coordinator
        registry, fleet, jobs = coordinator._registry, coordinator._fleet, coordinator._store
        wanted: dict[str, dict] = {}
        with coordinator.lock:
            sites = list(registry.list_sites())
            records = {r.robot_id: r for r in fleet.all()}
            for site in sites:
                policy = self.battery.policy(site.site_id) if self.battery is not None else {"critical": 10}
                for robot_id in site.robots:
                    record = records.get(robot_id)
                    if record is None:
                        continue
                    common = dict(site_id=site.site_id, robot_id=robot_id)
                    liveness = record.liveness(fleet.stale_after)
                    if liveness in (RobotLiveness.LOST, RobotLiveness.STALE):
                        since = self._offline_since.setdefault(robot_id, now)
                        if now - since >= OFFLINE_AFTER:
                            wanted[f"robot_offline:{robot_id}"] = dict(
                                common, rule="robot_offline", severity="error",
                                message=f"로봇 {robot_id} 연결 {'유실' if liveness == RobotLiveness.LOST else '수신 지연'}")
                        continue
                    self._offline_since.pop(robot_id, None)
                    state = record.state
                    if state is None or liveness != RobotLiveness.ONLINE:
                        continue
                    fatal = [e.error_type for e in state.errors if e.level == ErrorLevel.FATAL]
                    if state.state in (RobotState.ERROR, RobotState.EMERGENCY) or fatal:
                        label = "비상정지" if state.state == RobotState.EMERGENCY else "오류"
                        wanted[f"robot_error:{robot_id}"] = dict(
                            common, rule="robot_error", severity="error",
                            message=f"로봇 {robot_id} {label}{': ' + ', '.join(fatal) if fatal else ''}")
                    if state.battery.charge < policy["critical"] and not state.battery.charging:
                        wanted[f"battery_critical:{robot_id}"] = dict(
                            common, rule="battery_critical", severity="error",
                            message=f"로봇 {robot_id} 배터리 {state.battery.charge:.0f}% (위험 기준 {policy['critical']}%)")
                for job in jobs.list(site.site_id):
                    if job.status.value != "pending" or not job.auto_assign:
                        continue
                    from fms_server.task import _epoch
                    start = max(_epoch(job.created_at) or now, _epoch(job.not_before) or 0)
                    if now - start >= PENDING_AFTER:
                        wanted[f"job_pending:{job.job_id}"] = dict(
                            site_id=site.site_id, job_id=job.job_id, rule="job_pending", severity="warning",
                            message=f"작업 {job.job_id} 이(가) {int((now - start) // 60)}분째 배정되지 않음: {job.error or ''}")
            for deadlock in coordinator.traffic.analysis.get("deadlocks", []):
                key = "deadlock:" + "+".join(deadlock["robots"])
                wanted[key] = dict(site_id=deadlock.get("site_id"), rule="deadlock", severity="error",
                                   message=f"교착: {' ↔ '.join(deadlock['robots'])}", robots=deadlock["robots"])
            ctl = getattr(coordinator, "_ctl", None)
            store = self._elevators() if callable(self._elevators) else self._elevators
            if ctl is not None and store is not None and hasattr(ctl, "snapshot"):
                for site in sites:
                    for elevator in store.list(site.site_id):
                        eid = elevator.elevator_id
                        snap = ctl.snapshot(eid)
                        common = dict(site_id=site.site_id, elevator_id=eid)
                        if snap["liveness"] == "STALE":
                            wanted[f"elevator_stale:{eid}"] = dict(common, rule="elevator_stale", severity="error",
                                                                   message=f"승강기 {eid} 상태 수신 끊김")
                        elif snap.get("mode") and snap["mode"] != "NORMAL":
                            wanted[f"elevator_fault:{eid}"] = dict(
                                common, rule="elevator_fault", severity="error",
                                message=f"승강기 {eid} {snap['mode']}{': ' + snap['fault'] if snap.get('fault') else ''}")
                        if snap.get("attention"):
                            wanted[f"elevator_attention:{eid}"] = dict(
                                common, rule="elevator_attention", severity="error",
                                robot_id=snap["attention"].get("robot_id"),
                                message=f"승강기 {eid} 확인 필요: {snap['attention'].get('reason')}")
            if self.locker is not None:
                for delivery in self.locker.deliveries.values():
                    if delivery.get("phase") == "blocked":
                        site_id = (delivery.get("config") or {}).get("site_id")
                        wanted[f"delivery_blocked:{delivery['job_id']}"] = dict(
                            site_id=site_id, job_id=delivery["job_id"], robot_id=delivery.get("robot_id"),
                            rule="delivery_blocked", severity="error",
                            message=f"배송 {delivery['job_id']} 차단: {delivery.get('error') or ''}")
        return wanted
