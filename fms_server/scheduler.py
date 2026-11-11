"""예약 실행 — 정해진 요일·시각 또는 간격으로 프리셋을 게시한다.

`sites/{site}/schedules.json`:
    {id, name, preset_id, days: [0..6](월=0), time: "HH:MM" | null, interval_s: 초 | null,
     robot_group, priority, enabled, last_run, last_error}

`tick()` 은 배정 루프가 1초마다 부른다. `last_run` 을 **먼저** 저장하고 게시하므로 재시작해도 같은 회차를
두 번 실행하지 않는다. 서버가 꺼져 있던 동안 놓친 실행은 건너뛴다 (지난 것을 몰아서 게시하지 않는다).
시각은 서버 로컬 시간대 기준이다.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fms_server.task import JobError, JobNotFound, _check_seg

log = logging.getLogger("fms.scheduler")

SCHEDULES_FILE = "schedules.json"
MISS_GRACE = 90.0   # time 예약: 정시에서 이만큼 지나면 놓친 것으로 보고 건너뛴다 (초)


class ScheduleStore:
    def __init__(self, data_root) -> None:
        self.root = Path(data_root) / "sites"
        self._lock = threading.RLock()

    def _path(self, site_id: str) -> Path:
        _check_seg(site_id, "site_id")
        return self.root / site_id / SCHEDULES_FILE

    def list(self, site_id: str) -> list[dict]:
        with self._lock:
            path = self._path(site_id)
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def get(self, site_id: str, schedule_id: str) -> dict:
        for row in self.list(site_id):
            if row["id"] == schedule_id:
                return row
        raise JobNotFound("예약을 찾을 수 없습니다")

    def _write(self, site_id: str, rows: list[dict]) -> None:
        path = self._path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def save(self, site_id: str, data: dict, presets, schedule_id: Optional[str] = None) -> dict:
        presets.get(site_id, data["preset_id"])   # 없는 프리셋이면 JobNotFound
        time_of_day, interval = data.get("time"), data.get("interval_s")
        if (time_of_day is None) == (interval is None):
            raise JobError("time(HH:MM) 과 interval_s 중 하나만 지정하세요")
        if time_of_day is not None:
            try:
                parsed = datetime.strptime(time_of_day, "%H:%M")
                time_of_day = parsed.strftime("%H:%M")
            except (TypeError, ValueError):
                raise JobError("time 은 HH:MM 형식이어야 합니다") from None
        if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float))
                                     or interval < 10):
            raise JobError("interval_s 는 10초 이상이어야 합니다")
        days = sorted(set(data.get("days") or range(7)))
        if any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days):
            raise JobError("days 는 0(월)~6(일) 정수 목록이어야 합니다")
        with self._lock:
            rows = self.list(site_id)
            previous = self.get(site_id, schedule_id) if schedule_id else None
            row = {
                "id": schedule_id or uuid4().hex[:12],
                "name": (data.get("name") or "").strip()[:128],
                "preset_id": data["preset_id"], "days": days, "time": time_of_day,
                "interval_s": interval, "robot_group": data.get("robot_group") or None,
                "priority": int(data.get("priority") or 0), "enabled": bool(data.get("enabled", True)),
                # 새 예약·간격 변경은 지금부터 센다 (만들자마자 실행하지 않는다).
                "last_run": previous["last_run"] if previous else None,
                "last_error": None,
                "created_at": previous["created_at"] if previous else time.time(),
            }
            self._write(site_id, [r for r in rows if r["id"] != row["id"]] + [row])
            return copy.deepcopy(row)

    def delete(self, site_id: str, schedule_id: str) -> None:
        with self._lock:
            self.get(site_id, schedule_id)
            self._write(site_id, [r for r in self.list(site_id) if r["id"] != schedule_id])

    def mark(self, site_id: str, schedule_id: str, **fields) -> None:
        with self._lock:
            rows = self.list(site_id)
            for row in rows:
                if row["id"] == schedule_id:
                    row.update(fields)
            self._write(site_id, rows)


def due(row: dict, now: float) -> Optional[float]:
    """지금 실행할 회차의 기준 시각 (없으면 None). 순수 함수 — 테스트는 now 를 주입한다."""
    if not row.get("enabled", True):
        return None
    local = datetime.fromtimestamp(now)
    if local.weekday() not in (row.get("days") or range(7)):
        return None
    last = row.get("last_run")
    if row.get("interval_s"):
        base = last if last is not None else row.get("created_at", now)
        return now if now - base >= row["interval_s"] else None
    hour, minute = (int(part) for part in row["time"].split(":"))
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    if now < slot or now - slot > MISS_GRACE:
        return None   # 아직 전이거나, 놓친 실행 (건너뛴다)
    if last is not None and last >= slot:
        return None   # 이 회차는 이미 실행했다
    return slot


class Scheduler:
    def __init__(self, store: ScheduleStore, presets, registry, jobs, coordinator, *, events=None,
                 clock=time.time) -> None:
        self.store, self.presets, self.coordinator, self.events = store, presets, coordinator, events
        self._own = (registry, jobs)
        self._clock = clock

    @property
    def registry(self):
        return getattr(self.coordinator, "_registry", self._own[0])

    @property
    def jobs(self):
        return getattr(self.coordinator, "_store", self._own[1])

    def tick(self) -> list[str]:
        """때가 된 예약을 게시한다. 만든 job_id 목록."""
        from fms_server.presets import publish
        now = self._clock()
        created = []
        for site in self.registry.list_sites():
            try:
                rows = self.store.list(site.site_id)
            except (OSError, ValueError):
                log.exception("%s 예약 목록을 읽을 수 없다", site.site_id)
                continue
            for row in rows:
                slot = due(row, now)
                if slot is None:
                    continue
                # 먼저 기록한다. 게시가 실패해도 같은 회차를 다시 시도하지 않는다.
                self.store.mark(site.site_id, row["id"], last_run=now, last_error=None)
                try:
                    with self.coordinator.lock:
                        job = publish(self.presets, site.site_id, row["preset_id"], registry=self.registry,
                                      jobs=self.jobs, coordinator=self.coordinator,
                                      priority=row.get("priority", 0), robot_group=row.get("robot_group"))
                except JobError as exc:
                    self.store.mark(site.site_id, row["id"], last_error=str(exc))
                    if self.events is not None:
                        self.events.record("schedule.run", site_id=site.site_id, severity="warning",
                                           schedule_id=row["id"], name=row.get("name"), error=str(exc))
                    continue
                created.append(job.job_id)
                if self.events is not None:
                    self.events.record("schedule.run", site_id=site.site_id, job_id=job.job_id,
                                       schedule_id=row["id"], name=row.get("name"), preset=job.preset_name)
        return created
