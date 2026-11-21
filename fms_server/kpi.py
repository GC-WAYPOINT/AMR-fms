"""KPI — `events.db` 를 시간 구간(bucket)으로 집계한다. 상태 저장은 없다 (조회할 때 계산).

지표:
- 작업: 완료·실패·취소 수와 성공률, 배정 대기 시간, 수행 시간 (시스템 작업·배송 하위 작업은 따로 센다)
- 로봇: 상태별 시간 비율(가동률), 주행 거리, 충전 횟수·시간 (`robot.usage` 가 원본)
- 대기: 교통 대기, 승강기 임대 대기
- 배송: 리드타임 (첫 단계 기록 ~ done)
"""
from __future__ import annotations

from typing import Optional

BUCKETS = {"hour": 3600, "day": 86400}
KINDS = ("job.assigned", "job.success", "job.failed", "job.canceled", "job.deadline_missed",
         "robot.usage", "robot.charging_started", "traffic.wait_end", "traffic.deadlock", "traffic.stall",
         "elevator.lease")
STATUSES = ("working", "idle", "charging", "manual", "service", "error", "offline")


def _avg(total: float, count: int) -> Optional[float]:
    return round(total / count, 2) if count else None


def _empty(start: float) -> dict:
    return {"start": start, "completed": 0, "failed": 0, "canceled": 0, "system_jobs": 0,
            "assign_wait": [0.0, 0], "duration": [0.0, 0], "distance": 0.0,
            "seconds": {s: 0.0 for s in STATUSES}, "charges": 0,
            "traffic_wait": [0.0, 0], "elevator_wait": [0.0, 0], "deadlocks": 0, "stalls": 0,
            "deadline_missed": 0}


def _finish(b: dict) -> dict:
    done = b["completed"] + b["failed"]
    online = sum(v for k, v in b["seconds"].items() if k != "offline")
    return {
        "start": b["start"], "completed": b["completed"], "failed": b["failed"], "canceled": b["canceled"],
        "system_jobs": b["system_jobs"],
        "success_rate": round(b["completed"] / done, 4) if done else None,
        "assign_wait_avg": _avg(*b["assign_wait"]), "duration_avg": _avg(*b["duration"]),
        "distance": round(b["distance"], 2),
        "seconds": {k: round(v, 1) for k, v in b["seconds"].items()},
        "utilization": round(b["seconds"]["working"] / online, 4) if online else None,
        "charges": b["charges"], "charge_seconds": round(b["seconds"]["charging"], 1),
        "traffic_wait_avg": _avg(*b["traffic_wait"]), "traffic_wait_total": round(b["traffic_wait"][0], 1),
        "traffic_waits": b["traffic_wait"][1],
        "elevator_wait_avg": _avg(*b["elevator_wait"]), "elevator_wait_total": round(b["elevator_wait"][0], 1),
        "deadlocks": b["deadlocks"], "stalls": b["stalls"], "deadline_missed": b["deadline_missed"],
    }


def _apply(b: dict, event: dict) -> None:
    kind, data = event["kind"], event["data"]
    counted = not data.get("system")   # 일반 작업만 작업 지표에 넣는다
    if kind == "job.assigned":
        if counted and data.get("waited") is not None:
            b["assign_wait"][0] += data["waited"]
            b["assign_wait"][1] += 1
    elif kind in ("job.success", "job.failed", "job.canceled"):
        if data.get("system"):
            b["system_jobs"] += 1
        elif counted:
            b[{"job.success": "completed", "job.failed": "failed", "job.canceled": "canceled"}[kind]] += 1
            if kind == "job.success" and data.get("duration") is not None:
                b["duration"][0] += data["duration"]
                b["duration"][1] += 1
    elif kind == "job.deadline_missed":
        b["deadline_missed"] += 1
    elif kind == "robot.usage":
        status = data.get("status") if data.get("status") in STATUSES else "offline"
        b["seconds"][status] += float(data.get("seconds") or 0)
        b["distance"] += float(data.get("distance") or 0)
    elif kind == "robot.charging_started":
        b["charges"] += 1
    elif kind == "traffic.wait_end":
        if data.get("seconds") is not None:
            b["traffic_wait"][0] += data["seconds"]
            b["traffic_wait"][1] += 1
    elif kind == "traffic.deadlock":
        b["deadlocks"] += 1
    elif kind == "traffic.stall":
        b["stalls"] += 1
    elif kind == "elevator.lease":
        if data.get("waited") is not None:
            b["elevator_wait"][0] += data["waited"]
            b["elevator_wait"][1] += 1



def compute(events, site_id: str, start: float, end: float, bucket: str = "hour") -> dict:
    """[start, end) 구간의 KPI. `events` 는 EventLog."""
    if bucket not in BUCKETS:
        raise ValueError("bucket 은 hour 또는 day")
    if end <= start:
        raise ValueError("to 는 from 보다 뒤여야 한다")
    size = BUCKETS[bucket]
    if (end - start) / size > 2000:
        raise ValueError("구간이 너무 길다 — bucket=day 를 쓰거나 기간을 줄여라")
    first = start - (start % size)
    buckets = {}
    t = first
    while t < end:
        buckets[t] = _empty(t)
        t += size
    total = _empty(start)
    robots: dict[str, dict] = {}
    events.flush()
    marks = ",".join("?" * len(KINDS))
    # 승강기 임대 이벤트는 site 를 모른다 (승강기 id 만 있다). 작업 id 접두어(site-J…)로 고른다.
    sql = (f"SELECT id, ts, site_id, kind, severity, robot_id, job_id, actor, data FROM events "
           f"WHERE ts >= ? AND ts < ? AND kind IN ({marks}) "
           f"AND (site_id = ? OR (site_id IS NULL AND job_id LIKE ?)) ORDER BY id")
    from fms_server.events import _row
    with events.db_lock:
        rows = [_row(r) for r in events.db.execute(sql, (start, end, *KINDS, site_id, f"{site_id}-J%")).fetchall()]
    for event in rows:
        b = buckets.get(event["ts"] - (event["ts"] % size))
        if b is None:
            continue
        _apply(b, event)
        _apply(total, event)
        if event["robot_id"] and event["kind"] in ("robot.usage", "robot.charging_started", "job.success", "job.failed"):
            r = robots.setdefault(event["robot_id"], _empty(start))
            _apply(r, event)
    return {
        "site_id": site_id, "from": start, "to": end, "bucket": bucket,
        "totals": _finish(total),
        "buckets": [_finish(b) for b in buckets.values()],
        "robots": [dict(_finish(r), robot_id=robot_id) for robot_id, r in sorted(robots.items())],
    }
