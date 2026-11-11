"""API 키 관리 (admin).

    GET    /api/v1/auth/me              인증 사용 여부와 내 역할 (키 없이도 열려 있다)
    GET    /api/v1/auth/keys
    POST   /api/v1/auth/keys            {name, role} -> 키 원문은 이 응답에서만 볼 수 있다
    DELETE /api/v1/auth/keys/{name}
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from fms_server.api.events import record
from fms_server.auth import AuthError, _token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str = "viewer"


@router.get("/me")
def me(request: Request):
    enabled = bool(getattr(request.app.state.settings, "auth", False))
    identity = request.app.state.auth.verify(_token(request)) if enabled and _token(request) else None
    return {"enabled": enabled, "name": identity["name"] if identity else None,
            "role": identity["role"] if identity else (None if enabled else "admin")}


@router.get("/keys")
def list_keys(request: Request):
    return {"keys": request.app.state.auth.list()}


@router.post("/keys")
def create_key(body: KeyIn, request: Request):
    try:
        key = request.app.state.auth.create(body.name, body.role)
    except AuthError as exc:
        raise HTTPException(422, str(exc)) from None
    record(request, "auth.key_created", name=body.name, role=body.role)
    return {"name": body.name.strip(), "role": body.role, "key": key}


@router.delete("/keys/{name}")
def delete_key(name: str, request: Request):
    try:
        request.app.state.auth.delete(name)
    except AuthError as exc:
        raise HTTPException(422, str(exc)) from None
    record(request, "auth.key_deleted", name=name)
    return {"deleted": name}
