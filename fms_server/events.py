"""이벤트 로그 — 누가·언제·무엇을 했는지 `events.db`(sqlite3)에 쌓는다.

현재 상태는 JSON 파일이 원본이고, 여기에는 **쌓이는 이력만** 둔다 (KPI·알림·webhook 이 읽는다).

- `record()` 는 큐에 넣기만 한다. paho 스레드·코디네이터 락 안에서 불러도 블로킹하지 않는다.
- 전용 워커 스레드(`start()`)가 묶어서 commit 한다. 워커가 없어도 `flush()` 가 호출 스레드에서 쓴다 (테스트).
- commit 뒤에 구독자(`add_listener`)를 워커 스레드에서 부른다. 알림 규칙·webhook 전송 대기열이 여기 붙는다.
- 보존 기간(`FMS_EVENT_RETENTION_DAYS`)이 지난 행은 하루 한 번 지운다.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("fms.events")

DB_NAME = "events.db"
SEVERITIES = ("info", "warning", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    site_id TEXT,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    robot_id TEXT,
    job_id TEXT,
    actor TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_site_ts ON events(site_id, ts);
CREATE INDEX IF NOT EXISTS events_robot_ts ON events(robot_id, ts);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
"""


def _row(row) -> dict:
    return {"id": row[0], "ts": row[1], "site_id": row[2], "kind": row[3], "severity": row[4],
            "robot_id": row[5], "job_id": row[6], "actor": row[7], "data": json.loads(row[8] or "{}")}


class EventLog:
    def __init__(self, data_root, *, retention_days: float = 30.0, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(data_root) / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._clock = clock
        self._queue: queue.Queue = queue.Queue()
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        with self._db_lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._listeners: list[Callable[[dict], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_purge = 0.0
        self._closed = False

    # -- 다른 저장소가 같은 DB 를 쓴다 (알림·webhook 대기열) --------------

    @property
    def db(self) -> sqlite3.Connection:
        return self._db

    @property
    def db_lock(self):
        return self._db_lock

    # -- 수명 주기 --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="event-log", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.flush()
        with self._db_lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        """commit 된 이벤트마다 워커(또는 flush 호출) 스레드에서 불린다."""
        self._listeners.append(callback)

    # -- 기록 -------------------------------------------------------------

    def record(self, kind: str, *, site_id: Optional[str] = None, severity: str = "info",
               robot_id: Optional[str] = None, job_id: Optional[str] = None,
               actor: Optional[str] = None, ts: Optional[float] = None, **data) -> None:
        """큐에 넣기만 한다. 어떤 스레드에서 불러도 된다."""
        if severity not in SEVERITIES:
            severity = "info"
        self._queue.put((self._clock() if ts is None else ts, site_id, kind, severity,
                         robot_id, job_id, actor, data))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=0.3)
            except queue.Empty:
                self._purge_if_due()
                continue
            self._write([first] + self._drain())
            self._purge_if_due()

    def _drain(self) -> list:
        rows = []
        while True:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                return rows

    def flush(self) -> None:
        """큐에 남은 것을 지금 쓴다."""
        rows = self._drain()
        if rows:
            self._write(rows)

    def _write(self, rows: list) -> None:
        written = []
        with self._db_lock:
            if self._closed:
                return
            try:
                for ts, site_id, kind, severity, robot_id, job_id, actor, data in rows:
                    cursor = self._db.execute(
                        "INSERT INTO events(ts, site_id, kind, severity, robot_id, job_id, actor, data) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (ts, site_id, kind, severity, robot_id, job_id, actor,
                         json.dumps(data, ensure_ascii=False, default=str)))
                    written.append({"id": cursor.lastrowid, "ts": ts, "site_id": site_id, "kind": kind,
                                    "severity": severity, "robot_id": robot_id, "job_id": job_id,
                                    "actor": actor, "data": data})
                self._db.commit()
            except sqlite3.Error:
                log.exception("이벤트 %d건 기록 실패", len(rows))
                self._db.rollback()
                return
        for event in written:
            for callback in list(self._listeners):
                try:
                    callback(event)
                except Exception:
                    log.exception("이벤트 구독자 실패 (%s)", event["kind"])

    # -- 보존 -------------------------------------------------------------

    def _purge_if_due(self) -> None:
        now = self._clock()
        if now - self._last_purge >= 86400:
            self._last_purge = now
            self.purge(now)

    def purge(self, now: Optional[float] = None) -> int:
        """보존 기간이 지난 이벤트를 지운다. 지운 행 수를 돌려준다."""
        if self.retention_days <= 0:
            return 0
        cutoff = (self._clock() if now is None else now) - self.retention_days * 86400
        with self._db_lock:
            if self._closed:
                return 0
            cursor = self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.commit()
            return cursor.rowcount

    # -- 조회 -------------------------------------------------------------

    def query(self, *, site: Optional[str] = None, robot: Optional[str] = None, job: Optional[str] = None,
              kind: Optional[str] = None, severity: Optional[str] = None,
              since: Optional[float] = None, until: Optional[float] = None,
              after_id: Optional[int] = None, limit: int = 200, ascending: bool = False) -> list[dict]:
        """최신 순(기본). `kind` 는 정확히 일치하거나 `job.` 처럼 점으로 끝나면 접두어로 본다."""
        self.flush()
        where, args = [], []
        for column, value in (("site_id", site), ("robot_id", robot), ("job_id", job), ("severity", severity)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if kind:
            kinds = [k for k in kind.split(",") if k]
            parts = []
            for k in kinds:
                if k.endswith("."):
                    parts.append("kind LIKE ?")
                    args.append(k.replace("%", "") + "%")
                else:
                    parts.append("kind = ?")
                    args.append(k)
            if parts:
                where.append("(" + " OR ".join(parts) + ")")
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts < ?")
            args.append(until)
        if after_id is not None:
            where.append("id > ?")
            args.append(after_id)
        sql = "SELECT id, ts, site_id, kind, severity, robot_id, job_id, actor, data FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY id {'ASC' if ascending else 'DESC'} LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        with self._db_lock:
            if self._closed:
                return []
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]
