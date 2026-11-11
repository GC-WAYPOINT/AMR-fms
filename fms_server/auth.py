"""인증·권한 — API 키(`Authorization: Bearer <키>`)와 역할.

`FMS_AUTH=1` 일 때만 켜진다 (기본 꺼짐 — 개발·테스트는 그대로 동작).

- `{data_root}/auth.json`: `{"keys": [{name, hash, role, created_at}]}`. 키 원문은 저장하지 않는다 (sha256).
- 역할: `viewer`(조회) < `operator`(운영 조작) < `admin`(키·webhook·현장 삭제).
- 켜져 있는데 키가 하나도 없으면 시작할 때 admin 키를 만든다. `FMS_ADMIN_KEY` 가 있으면 그 값을 쓰고,
  없으면 무작위 키를 만들어 로그에 **한 번** 찍는다.
- 로봇의 맵 번들 GET 은 로봇이 API 키를 갖지 않으므로 따로 본다: `FMS_ROBOT_TOKEN` 이 있으면
  `?token=` 이 맞아야 하고(SET_MAP URL 에 FMS 가 붙여 준다), 없으면 인증 없이 열어 둔다.
- 브로커(MQTT) 인증은 FMS 소관이 아니다 — AMR-simulator `broker/mosquitto.conf` 에서 설정한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from starlette.requests import HTTPConnection

log = logging.getLogger("fms.auth")

AUTH_FILE = "auth.json"
ROLES = ("viewer", "operator", "admin")
_OPEN_PATHS = ("/", "/health", "/docs", "/redoc", "/openapi.json", "/api/v1/auth/me")
_BUNDLE = re.compile(r"^/api/v1/sites/[^/]+/maps/[^/]+/bundle$")
_ADMIN_PREFIXES = ("/api/v1/auth/keys", "/api/v1/webhooks")
_SITE = re.compile(r"^/api/v1/sites/[^/]+$")


class AuthError(ValueError):
    pass


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, data_root) -> None:
        self.path = Path(data_root) / AUTH_FILE
        self._lock = threading.RLock()
        self._keys: list[dict] = []
        if self.path.is_file():
            try:
                self._keys = list(json.loads(self.path.read_text(encoding="utf-8")).get("keys") or [])
            except (ValueError, OSError, AttributeError):
                log.exception("auth.json 을 읽을 수 없다 — 키 없이 시작")

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": self._keys}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def list(self) -> list[dict]:
        with self._lock:
            return [{"name": k["name"], "role": k["role"], "created_at": k.get("created_at")} for k in self._keys]

    def create(self, name: str, role: str, key: Optional[str] = None) -> str:
        """키를 만들고 원문을 돌려준다 (이때 한 번만 볼 수 있다)."""
        name = (name or "").strip()
        if not name or len(name) > 64:
            raise AuthError("키 이름은 1~64자여야 한다")
        if role not in ROLES:
            raise AuthError(f"역할은 {', '.join(ROLES)} 중 하나")
        key = key or secrets.token_urlsafe(32)
        if len(key) < 16:
            raise AuthError("키는 16자 이상이어야 한다")
        with self._lock:
            if any(k["name"] == name for k in self._keys):
                raise AuthError(f"키 이름 {name!r} 이(가) 이미 있다")
            self._keys.append({"name": name, "hash": _hash(key), "role": role, "created_at": time.time()})
            self._write()
        return key

    def delete(self, name: str) -> None:
        with self._lock:
            target = next((k for k in self._keys if k["name"] == name), None)
            if target is None:
                raise AuthError(f"키 {name!r} 없음")
            if target["role"] == "admin" and sum(1 for k in self._keys if k["role"] == "admin") <= 1:
                raise AuthError("마지막 admin 키는 지울 수 없다")
            self._keys.remove(target)
            self._write()

    def verify(self, key: str) -> Optional[dict]:
        digest = _hash(key)
        with self._lock:
            for entry in self._keys:
                if hmac.compare_digest(entry["hash"], digest):
                    return {"name": entry["name"], "role": entry["role"]}
        return None

    def bootstrap(self, admin_key: str = "") -> None:
        """인증이 켜져 있는데 admin 키가 없으면 하나 만든다."""
        with self._lock:
            if any(k["role"] == "admin" for k in self._keys):
                return
        if admin_key:
            self.create("admin", "admin", admin_key)
            log.warning("FMS_ADMIN_KEY 로 admin API 키를 등록했다")
        else:
            key = self.create("admin", "admin")
            log.warning("인증이 켜져 있지만 API 키가 없어 admin 키를 만들었다. 지금만 보인다: %s", key)


def required_role(method: str, path: str) -> str:
    if any(path.startswith(prefix) for prefix in _ADMIN_PREFIXES):
        return "admin"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    if method == "DELETE" and _SITE.match(path):
        return "admin"
    return "operator"


def _token(conn: HTTPConnection) -> str:
    header = conn.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # WebSocket 은 브라우저에서 헤더를 못 붙인다. 쿼리로 받는다.
    return conn.query_params.get("api_key", "")


def authorize(conn: HTTPConnection) -> None:
    """라우터마다 거는 dependency. REST 와 WebSocket 을 같이 본다."""
    app = conn.app
    settings = app.state.settings
    if not getattr(settings, "auth", False):
        return
    path = conn.url.path
    if path in _OPEN_PATHS or path.startswith("/static"):
        return
    method = conn.scope.get("method", "GET")
    store: AuthStore = app.state.auth
    identity = store.verify(_token(conn)) if _token(conn) else None
    if method == "GET" and _BUNDLE.match(path):
        robot_token = getattr(settings, "robot_token", "")
        if not robot_token or hmac.compare_digest(conn.query_params.get("token", ""), robot_token) or identity:
            return
        raise HTTPException(401, "맵 번들 토큰이 필요하다")
    if identity is None:
        raise HTTPException(401, "API 키가 필요하다 (Authorization: Bearer <키>)",
                            headers={"WWW-Authenticate": "Bearer"})
    need = required_role(method, path)
    if ROLES.index(identity["role"]) < ROLES.index(need):
        raise HTTPException(403, f"권한이 부족하다 ({identity['role']} — {need} 이상 필요)")
    conn.state.api_key = identity
