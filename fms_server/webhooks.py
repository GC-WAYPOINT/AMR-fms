"""Webhook — 이벤트를 외부 시스템에 HTTP POST 로 알린다.

- 설정: `{data_root}/webhooks.json` `[{id, name, url, kinds, secret, enabled}]`.
  `kinds` 는 이벤트 종류 목록 (`job.` 처럼 점으로 끝나면 접두어, 비면 `job.`·`alert.` 기본).
- 전송 대기열은 `events.db` 의 `webhook_queue` 테이블이다 (재시작해도 남는다).
- 워커 스레드가 `httpx` 로 보낸다. 실패하면 지수 backoff 로 다시 보내고 `MAX_ATTEMPTS` 뒤에 버린다.
- 서명: `X-FMS-Signature: sha256=<본문의 HMAC-SHA256 hex>` (secret 이 있을 때).
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

log = logging.getLogger("fms.webhooks")

WEBHOOKS_FILE = "webhooks.json"
DEFAULT_KINDS = ["job.", "alert."]
MAX_ATTEMPTS = 8
MAX_BACKOFF = 300.0
NEVER = ("robot.usage",)        # 명시해도 보내지 않는다 (1분마다 쌓이는 원본 데이터)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_queue(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id TEXT NOT NULL,
    event_id INTEGER,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL,
    created_at REAL NOT NULL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS webhook_queue_due ON webhook_queue(next_at);
"""


class WebhookError(ValueError):
    pass


class WebhookNotFound(WebhookError):
    pass


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def matches(kinds: list[str], kind: str) -> bool:
    if kind in NEVER or kind.startswith("webhook."):
        return False
    return any(kind == k or (k.endswith(".") and kind.startswith(k)) for k in (kinds or DEFAULT_KINDS))


class WebhookManager:
    def __init__(self, events, data_root, *, clock=time.time, sender=None) -> None:
        self.events = events
        self.path = Path(data_root) / WEBHOOKS_FILE
        self._clock = clock
        self._sender = sender or self._post
        self._lock = threading.RLock()
        self._hooks: list[dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.path.is_file():
            try:
                self._hooks = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                log.exception("webhooks.json 을 읽을 수 없다 — webhook 없이 시작")
        with events.db_lock:
            events.db.executescript(_SCHEMA)
            events.db.commit()
        events.add_listener(self._on_event)

    # -- 설정 -------------------------------------------------------------

    @staticmethod
    def public(hook: dict) -> dict:
        """응답용. secret 은 돌려주지 않는다."""
        out = {k: v for k, v in hook.items() if k != "secret"}
        out["has_secret"] = bool(hook.get("secret"))
        return out

    def list(self) -> list[dict]:
        with self._lock:
            return [self.public(h) for h in self._hooks]

    def save(self, data: dict, hook_id: Optional[str] = None) -> dict:
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or len(url) > 2000:
            raise WebhookError("url 은 http:// 또는 https:// 로 시작해야 한다")
        kinds = data.get("kinds") or []
        if not isinstance(kinds, list) or any(not isinstance(k, str) or not k.strip() for k in kinds):
            raise WebhookError("kinds 는 이벤트 종류 문자열 목록이어야 한다")
        with self._lock:
            previous = next((h for h in self._hooks if h["id"] == hook_id), None)
            if hook_id and previous is None:
                raise WebhookNotFound("webhook 을 찾을 수 없다")
            secret = data.get("secret")
            hook = {
                "id": hook_id or uuid4().hex[:12], "name": (data.get("name") or "").strip()[:128], "url": url,
                "kinds": [k.strip() for k in kinds],
                # secret 을 안 보내면 기존 값을 유지한다 (목록 응답에 secret 이 없으므로).
                "secret": secret if secret is not None else (previous or {}).get("secret", ""),
                "enabled": bool(data.get("enabled", True)),
            }
            self._hooks = [h for h in self._hooks if h["id"] != hook["id"]] + [hook]
            self._write()
            return self.public(hook)

    def delete(self, hook_id: str) -> None:
        with self._lock:
            if not any(h["id"] == hook_id for h in self._hooks):
                raise WebhookNotFound("webhook 을 찾을 수 없다")
            self._hooks = [h for h in self._hooks if h["id"] != hook_id]
            self._write()
        with self.events.db_lock:
            self.events.db.execute("DELETE FROM webhook_queue WHERE webhook_id = ?", (hook_id,))
            self.events.db.commit()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- 대기열 -----------------------------------------------------------

    def _on_event(self, event: dict) -> None:
        """이벤트 로그 워커 스레드. 대기열에 넣기만 한다 (전송은 webhook 워커)."""
        with self._lock:
            targets = [h["id"] for h in self._hooks if h.get("enabled", True) and matches(h.get("kinds"), event["kind"])]
        if not targets:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        now = self._clock()
        with self.events.db_lock:
            for hook_id in targets:
                self.events.db.execute(
                    "INSERT INTO webhook_queue(webhook_id, event_id, payload, next_at, created_at) VALUES (?,?,?,?,?)",
                    (hook_id, event["id"], payload, now, now))
            self.events.db.commit()

    def pending(self) -> int:
        with self.events.db_lock:
            return self.events.db.execute("SELECT COUNT(*) FROM webhook_queue").fetchone()[0]

    def deliver_due(self) -> int:
        """보낼 때가 된 것을 보낸다. 성공한 건수."""
        now = self._clock()
        with self.events.db_lock:
            rows = self.events.db.execute(
                "SELECT id, webhook_id, payload, attempts FROM webhook_queue WHERE next_at <= ? ORDER BY id LIMIT 50",
                (now,)).fetchall()
        sent = 0
        for row_id, hook_id, payload, attempts in rows:
            with self._lock:
                hook = copy.deepcopy(next((h for h in self._hooks if h["id"] == hook_id), None))
            if hook is None or not hook.get("enabled", True):
                self._finish(row_id)          # 지워졌거나 꺼진 webhook 의 대기 건은 버린다
                continue
            body = payload.encode("utf-8")
            headers = {"Content-Type": "application/json", "User-Agent": "fms-webhook"}
            if hook.get("secret"):
                headers["X-FMS-Signature"] = sign(hook["secret"], body)
            try:
                error = self._sender(hook["url"], body, headers)
            except Exception as exc:  # 네트워크 오류 등
                error = f"{type(exc).__name__}: {exc}"
            if error is None:
                self._finish(row_id)
                sent += 1
                continue
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                self._finish(row_id)
                log.warning("webhook %s 전송 포기 (%d회): %s", hook.get("name") or hook_id, attempts, error)
                self.events.record("webhook.dropped", severity="warning", webhook_id=hook_id, error=error)
                continue
            delay = min(MAX_BACKOFF, 2.0 ** attempts)
            with self.events.db_lock:
                self.events.db.execute(
                    "UPDATE webhook_queue SET attempts = ?, next_at = ?, last_error = ? WHERE id = ?",
                    (attempts, self._clock() + delay, error[:500], row_id))
                self.events.db.commit()
        return sent

    def _finish(self, row_id: int) -> None:
        with self.events.db_lock:
            self.events.db.execute("DELETE FROM webhook_queue WHERE id = ?", (row_id,))
            self.events.db.commit()

    @staticmethod
    def _post(url: str, body: bytes, headers: dict) -> Optional[str]:
        import httpx
        response = httpx.post(url, content=body, headers=headers, timeout=5.0)
        return None if 200 <= response.status_code < 300 else f"HTTP {response.status_code}"

    # -- 워커 -------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="webhooks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=7)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.deliver_due()
            except Exception:
                log.exception("webhook 전송 루프 오류")
