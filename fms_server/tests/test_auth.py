"""4단계 — API 키 인증·역할. 꺼져 있으면(기본) 기존 동작 그대로."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from fms_server.auth import AuthError, AuthStore, required_role
from fms_server.main import app
from fms_server.sites import SiteRegistry


def test_required_role():
    assert required_role("GET", "/api/v1/robots") == "viewer"
    assert required_role("POST", "/api/v1/robots/AMR-001/pause") == "operator"
    assert required_role("DELETE", "/api/v1/sites/hq") == "admin"
    assert required_role("DELETE", "/api/v1/sites/hq/jobs/hq-J0001") == "operator"
    assert required_role("GET", "/api/v1/webhooks") == "admin"
    assert required_role("POST", "/api/v1/auth/keys") == "admin"


def test_store_hashes_keys_and_keeps_last_admin(tmp_path):
    store = AuthStore(tmp_path)
    key = store.create("ops", "operator")
    assert key not in (tmp_path / "auth.json").read_text()            # 원문은 저장하지 않는다
    assert store.verify(key) == {"name": "ops", "role": "operator"} and store.verify("wrong") is None
    with pytest.raises(AuthError):
        store.create("ops", "viewer")
    with pytest.raises(AuthError):
        store.create("x", "root")
    store.bootstrap("an-admin-key-0123456789")
    store.bootstrap("ignored-because-admin-exists")
    assert AuthStore(tmp_path).verify("an-admin-key-0123456789")["role"] == "admin"
    with pytest.raises(AuthError):
        store.delete("admin")
    store.delete("ops")
    assert [k["name"] for k in store.list()] == ["admin"]


@pytest.fixture
def secured(tmp_path):
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        registry.create_site("hq", "본사")
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        store = AuthStore(tmp_path)
        keys = {role: store.create(role, role) for role in ("viewer", "operator", "admin")}
        app.state.auth = store
        app.state.settings = dataclasses.replace(app.state.settings, auth=True, robot_token="robot-token")
        yield c, {role: {"Authorization": f"Bearer {key}"} for role, key in keys.items()}


def test_disabled_by_default():
    with TestClient(app) as c:
        assert c.get("/api/v1/robots").status_code == 200
        assert c.get("/api/v1/auth/me").json() == {"enabled": False, "name": None, "role": "admin"}


def test_roles(secured):
    c, h = secured
    assert c.get("/health").status_code == 200 and c.get("/").status_code == 200       # 열린 경로
    assert c.get("/api/v1/robots").status_code == 401
    assert c.get("/api/v1/robots", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/v1/robots", headers=h["viewer"]).status_code == 200
    assert c.post("/api/v1/sites/hq/pause", json={}, headers=h["viewer"]).status_code == 403
    assert c.post("/api/v1/sites/hq/pause", json={}, headers=h["operator"]).status_code == 200
    assert c.delete("/api/v1/sites/hq", headers=h["operator"]).status_code == 403
    assert c.get("/api/v1/webhooks", headers=h["operator"]).status_code == 403
    assert c.get("/api/v1/webhooks", headers=h["admin"]).status_code == 200
    assert c.get("/api/v1/auth/me", headers=h["operator"]).json() == {"enabled": True, "name": "operator", "role": "operator"}
    assert c.get("/api/v1/auth/me").json()["role"] is None
    # 조작자 이름이 이벤트에 남는다
    app.state.events.flush()
    assert app.state.events.query(kind="site.paused")[0]["actor"] == "operator"


def test_key_management_and_ws(secured):
    c, h = secured
    assert c.post("/api/v1/auth/keys", json={"name": "wms", "role": "operator"}, headers=h["operator"]).status_code == 403
    created = c.post("/api/v1/auth/keys", json={"name": "wms", "role": "operator"}, headers=h["admin"]).json()
    assert c.get("/api/v1/robots", headers={"Authorization": f"Bearer {created['key']}"}).status_code == 200
    names = [k["name"] for k in c.get("/api/v1/auth/keys", headers=h["admin"]).json()["keys"]]
    assert "wms" in names and all("hash" not in k for k in c.get("/api/v1/auth/keys", headers=h["admin"]).json()["keys"])
    assert c.delete("/api/v1/auth/keys/wms", headers=h["admin"]).status_code == 200
    assert c.get("/api/v1/robots", headers={"Authorization": f"Bearer {created['key']}"}).status_code == 401
    # WebSocket 은 쿼리로 키를 받는다
    key = h["viewer"]["Authorization"].split()[1]
    with c.websocket_connect(f"/api/v1/robots/stream?api_key={key}") as ws:
        assert ws.receive_json()["type"] == "snapshot"
    with pytest.raises(Exception):
        with c.websocket_connect("/api/v1/robots/stream"):
            pass


def test_robot_bundle_uses_robot_token(secured):
    c, h = secured
    url = "/api/v1/sites/hq/maps/floor-1/bundle"
    assert c.get(url).status_code == 401
    assert c.get(url, params={"token": "wrong"}).status_code == 401
    assert c.get(url, params={"token": "robot-token"}).status_code == 404        # 인증 통과 (맵이 없을 뿐)
    assert c.get(url, headers=h["viewer"]).status_code == 404
