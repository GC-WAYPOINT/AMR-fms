"""
자동문 제어기 클라이언트
=======================

실제(또는 시뮬레이터) 자동문 제어기와 MQTT 로 말한다 (`common/door_proto.py`).
`TrafficController` 가 점유 존 통과 중 자동문 존에서 쓴다:

    request_open(door, owner)   대기 node 에 선 로봇이 문을 연다. 열려 있으면 True
    release(door, owner)        로봇이 통과 완료 node 를 지나 존을 벗어나면 열림 유지를 푼다

상태가 오래됐거나(제어기 두절) 모르는 문은 열린 것으로 보지 않는다 — 닫혀 있다고 가정하고 계속 기다린다.
열림 유지(hold)는 한 번에 한 주인만 가진다 (존 예약이 이미 한 대씩만 통과시킨다).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt

from common import door_proto as P

log = logging.getLogger("fms.door")

RESEND_EVERY = 2.0   # 문이 열리지 않은 동안 OPEN 명령을 다시 보내는 간격 (초)


@dataclass(frozen=True)
class DoorState:
    door_id: str
    door: str
    held: bool
    received_at: float
    mode: str = P.MODE_NORMAL
    fault: Optional[str] = None

    @property
    def in_service(self) -> bool:
        return self.mode == P.MODE_NORMAL


class DoorController:
    def __init__(self, settings, *, stale_after: float = 5.0, events=None) -> None:
        self._settings = settings
        self.events = events
        self._stale_after = stale_after
        self._lock = threading.RLock()
        self._doors: dict[str, DoorState] = {}
        self._holds: dict[str, str] = {}           # door_id -> owner (job_id)
        self._sent_at: dict[str, float] = {}       # door_id -> 마지막 OPEN 발행 시각
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{settings.mqtt_client_id}-door", clean_session=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- 수명 주기 --
    def start(self) -> None:
        try:
            self._client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port, self._settings.mqtt_keepalive)
            self._client.loop_start()
        except Exception:
            log.exception("자동문 MQTT 시작 실패 (재연결은 paho 담당)")

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            client.subscribe(P.TOPIC_STATE_ALL, 0)

    def _on_message(self, client, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[3] != "state" or not msg.payload:
            return
        try:
            body = json.loads(msg.payload)
            mode = str(body.get("mode") or P.MODE_NORMAL)
            self.apply_state(DoorState(parts[2], str(body.get("door")), bool(body.get("held")), time.time(),
                                       mode if mode in P.MODES else P.MODE_OUT_OF_SERVICE, body.get("fault") or None))
        except (ValueError, AttributeError):
            log.warning("%s: 잘못된 state", msg.topic)

    def apply_state(self, door: DoorState) -> None:
        with self._lock:
            previous = self._doors.get(door.door_id)
            self._doors[door.door_id] = door
        if self.events is not None and (previous.mode if previous else P.MODE_NORMAL) != door.mode:
            self.events.record("door.fault", severity="info" if door.in_service else "error",
                               door_id=door.door_id, mode=door.mode, fault=door.fault)

    # -- 조회 --
    def state(self, door_id: str) -> Optional[DoorState]:
        """최근(stale_after 이내) 상태. 제어기가 죽었으면 None."""
        with self._lock:
            d = self._doors.get(door_id)
        if d is None or time.time() - d.received_at > self._stale_after:
            return None
        return d

    def is_open(self, door_id: str) -> bool:
        d = self.state(door_id)
        return d is not None and d.in_service and d.door == P.DOOR_OPEN

    def holder(self, door_id: str) -> Optional[str]:
        with self._lock:
            return self._holds.get(door_id)

    def holds(self) -> dict:
        with self._lock:
            return dict(self._holds)

    def snapshot(self, door_id: str) -> dict:
        with self._lock:
            d = self._doors.get(door_id)
            owner = self._holds.get(door_id)
        if d is None:
            return dict(liveness="UNKNOWN", door=None, held=None, mode=None, fault=None, hold_owner=owner)
        fresh = time.time() - d.received_at <= self._stale_after
        return dict(liveness="ONLINE" if fresh else "STALE", door=d.door, held=d.held, mode=d.mode,
                    fault=d.fault, hold_owner=owner, received_at=d.received_at)

    def reason(self, door_id: str) -> str:
        """문이 안 열린 이유 (대기 사유 표시용)."""
        d = self.state(door_id)
        if d is None:
            return "자동문 상태 수신 대기"
        if not d.in_service:
            return f"자동문 장애 ({d.fault or d.mode})"
        return "자동문 열림 대기"

    # -- 명령 --
    def request_open(self, door_id: str, owner: str, now: Optional[float] = None) -> bool:
        """문을 열어 두고 붙잡는다. 열려 있으면 True. 다른 주인이 붙잡고 있으면 False."""
        now = time.time() if now is None else now
        with self._lock:
            current = self._holds.get(door_id)
            if current is not None and current != owner:
                return False
            self._holds[door_id] = owner
            due = now - self._sent_at.get(door_id, 0.0) >= RESEND_EVERY
            if due:
                self._sent_at[door_id] = now
        if due:
            self._send(door_id, {"type": P.CMD_OPEN})
        return self.is_open(door_id)

    def release(self, door_id: str, owner: str) -> None:
        with self._lock:
            if self._holds.get(door_id) != owner:
                return
            del self._holds[door_id]
            self._sent_at.pop(door_id, None)
        self._send(door_id, {"type": P.CMD_RELEASE})

    def _send(self, door_id: str, body: dict) -> None:
        body = {"cmd_id": uuid.uuid4().hex[:12], **body}
        self._client.publish(P.topic_cmd(door_id), json.dumps(body), qos=1)
        log.info("자동문 %s 명령 %s", door_id, body)
