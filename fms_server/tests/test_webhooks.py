"""4단계 — webhook 대기열·재시도·서명 (로컬 가짜 수신기)."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from fms_server.events import EventLog
from fms_server.webhooks import MAX_ATTEMPTS, WebhookError, WebhookManager, matches, sign


@pytest.fixture
def setup(tmp_path):
    events = EventLog(tmp_path)
    now = [1000.0]
    sent, fail = [], [0]

    def sender(url, body, headers):
        if fail[0] > 0:
            fail[0] -= 1
            return "HTTP 500"
        sent.append((url, body, headers))
        return None

    manager = WebhookManager(events, tmp_path, clock=lambda: now[0], sender=sender)
    yield events, manager, now, sent, fail
    events.stop()


def test_kind_matching():
    assert matches([], "job.failed") and matches([], "alert.opened") and not matches([], "robot.state")
    assert matches(["robot."], "robot.state") and not matches(["robot."], "robot.usage")
    assert matches(["job.failed"], "job.failed") and not matches(["job.failed"], "job.success")
    assert not matches(["webhook."], "webhook.dropped")


def test_delivery_signature_and_filter(setup):
    events, manager, now, sent, fail = setup
    hook = manager.save({"name": "wms", "url": "http://wms.local/hook", "kinds": ["job."], "secret": "s3cret"})
    assert hook["has_secret"] and "secret" not in hook
    events.record("job.success", site_id="hq", job_id="hq-J0001", duration=3.0)
    events.record("robot.state", site_id="hq", robot_id="R1")
    events.flush()
    assert manager.pending() == 1
    assert manager.deliver_due() == 1 and manager.pending() == 0
    url, body, headers = sent[0]
    payload = json.loads(body)
    assert url == "http://wms.local/hook" and payload["kind"] == "job.success" and payload["job_id"] == "hq-J0001"
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-FMS-Signature"] == expected == sign("s3cret", body)


def test_retry_backoff_and_drop(setup):
    events, manager, now, sent, fail = setup
    manager.save({"url": "http://x.local/h"})
    events.record("job.failed", site_id="hq", job_id="hq-J0002")
    events.flush()
    fail[0] = 2
    assert manager.deliver_due() == 0
    assert manager.deliver_due() == 0 and manager.pending() == 1      # backoff 전에는 다시 보내지 않는다
    now[0] += 2.1
    assert manager.deliver_due() == 0                                  # 두 번째 실패 -> 4초 뒤
    now[0] += 4.1
    assert manager.deliver_due() == 1 and not manager.pending()
    events.record("job.failed", site_id="hq", job_id="hq-J0003")
    events.flush()
    fail[0] = 10 ** 6
    for _ in range(MAX_ATTEMPTS):
        now[0] += 1000
        manager.deliver_due()
    assert manager.pending() == 0                                      # 포기하고 버린다
    events.flush()
    assert events.query(kind="webhook.dropped")


def test_queue_survives_restart_and_config_persists(setup, tmp_path):
    events, manager, now, sent, fail = setup
    hook = manager.save({"url": "http://x.local/h", "secret": "abc"})
    events.record("alert.opened", site_id="hq", rule="robot_error")
    events.flush()
    again = WebhookManager(events, tmp_path, clock=lambda: now[0], sender=lambda *a: sent.append(a) and None)
    assert again.pending() == 1 and again.list()[0]["id"] == hook["id"]
    # secret 을 안 보내고 수정하면 기존 값을 유지한다
    again.save({"url": "http://y.local/h"}, hook["id"])
    assert again.list()[0]["url"] == "http://y.local/h" and again.list()[0]["has_secret"]
    with pytest.raises(WebhookError):
        again.save({"url": "ftp://x"})
    again.delete(hook["id"])
    assert again.pending() == 0 and again.list() == []


def test_real_http_post_to_local_receiver(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers.get("X-FMS-Signature"), body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    events = EventLog(tmp_path)
    try:
        manager = WebhookManager(events, tmp_path)
        manager.save({"url": f"http://127.0.0.1:{server.server_port}/hook", "secret": "k"})
        events.record("job.success", site_id="hq", job_id="hq-J0001")
        events.flush()
        assert manager.deliver_due() == 1
        signature, body = received[0]
        assert signature == sign("k", body) and json.loads(body)["kind"] == "job.success"
    finally:
        server.shutdown()
        events.stop()


def test_webhook_api():
    from fms_server.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/webhooks", json={"name": "w", "url": "http://x.local/h", "secret": "abc"})
        assert r.status_code == 200 and "secret" not in r.json()
        hook_id = r.json()["id"]
        assert c.post("/api/v1/webhooks", json={"url": "nope"}).status_code == 422
        assert c.put(f"/api/v1/webhooks/{hook_id}", json={"url": "http://x.local/h2", "enabled": False}).json()["enabled"] is False
        assert any(h["id"] == hook_id for h in c.get("/api/v1/webhooks").json()["webhooks"])
        assert c.delete(f"/api/v1/webhooks/{hook_id}").status_code == 200
        assert c.delete(f"/api/v1/webhooks/{hook_id}").status_code == 404
