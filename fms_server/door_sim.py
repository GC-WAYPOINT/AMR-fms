"""
가상 자동문 (시뮬레이터)
=======================

`common/door_proto.py` 계약대로 자동문을 흉내 낸다. FMS 프로세스 안에서 돈다
(기본 켜짐, `FMS_DOOR_SIM=0` 이면 끔). 실제 제어기를 붙일 때는 끈다 (같은 토픽에 state 를 두 군데서 내면 안 된다).

문 하나를 따로 등록하지 않는다 — 처음 OPEN 명령을 받은 `door_id` 로 문을 만들고 그 뒤로 state 를 발행한다.
동작: OPEN -> 열리는 중(door_time) -> OPEN (RELEASE 까지 유지, `max_open` 초가 지나면 스스로 닫힘)
-> RELEASE -> 닫히는 중(door_time) -> CLOSED.
"""

from __future__ import annotations

import json
import logging
import queue as queue_mod
import threading
import time

from common import door_proto as P

log = logging.getLogger("fms.door_sim")


class DoorUnit:
    """문 하나. MQTT 를 모른다 (step(dt) 로 시간을 먹인다)."""

    def __init__(self, door_id: str, *, door_time: float = 1.0, max_open: float = 60.0) -> None:
        self.door_id = door_id
        self.door_time, self.max_open = door_time, max_open
        self.door = P.DOOR_CLOSED
        self.held = False
        self.mode = P.MODE_NORMAL
        self.fault: str | None = None
        self._timer = 0.0
        self._open_for = 0.0
        self.version = 0

    def command(self, cmd_type: str) -> bool:
        if cmd_type == P.CMD_OPEN:
            if self.mode != P.MODE_NORMAL:
                return False
            self.held = True
            self._open_for = 0.0
            if self.door in (P.DOOR_CLOSED, P.DOOR_CLOSING):
                self.door, self._timer = P.DOOR_OPENING, self.door_time
            self.version += 1
            return True
        if cmd_type == P.CMD_RELEASE:
            self.held = False
            self.version += 1
            return True
        return False

    def step(self, dt: float) -> None:
        if self.door == P.DOOR_OPENING:
            self._timer -= dt
            if self._timer <= 0:
                self.door = P.DOOR_OPEN
                self.version += 1
        elif self.door == P.DOOR_OPEN:
            self._open_for += dt
            if not self.held or self._open_for >= self.max_open:
                self.held = False
                self.door, self._timer = P.DOOR_CLOSING, self.door_time
                self.version += 1
        elif self.door == P.DOOR_CLOSING:
            self._timer -= dt
            if self._timer <= 0:
                self.door = P.DOOR_CLOSED
                self.version += 1

    def state(self) -> dict:
        return {"door_id": self.door_id, "timestamp": time.time(), "door": self.door, "held": self.held,
                "mode": self.mode, "fault": self.fault}


class BuiltinDoorSim:
    """FMS 프로세스 안의 가상 자동문들. OPEN 을 받은 문 id 마다 하나씩 만든다."""

    def __init__(self, settings, *, tick: float = 0.05, door_time: float = 1.0) -> None:
        import paho.mqtt.client as mqtt

        self._settings = settings
        self._tick = tick
        self._unit_args = dict(door_time=door_time)
        self.doors: dict[str, DoorUnit] = {}
        self._inbox: queue_mod.Queue = queue_mod.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"{settings.mqtt_client_id}-door-sim", clean_session=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = lambda c, *_a: c.subscribe(f"{P.TOPIC_PREFIX}/+/cmd", 1)
        self._client.on_message = self._on_message

    def start(self) -> None:
        try:
            self._client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port, self._settings.mqtt_keepalive)
            self._client.loop_start()
        except Exception:
            log.exception("가상 자동문 MQTT 시작 실패 (재연결은 paho 담당)")
        self._thread = threading.Thread(target=self._run, name="door-sim", daemon=True)
        self._thread.start()
        log.info("가상 자동문 시작 (FMS 내장, 끄려면 FMS_DOOR_SIM=0)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def _on_message(self, _c, _u, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[3] != "cmd":
            return
        try:
            self._inbox.put((parts[2], json.loads(msg.payload)))
        except ValueError:
            log.warning("%s: 잘못된 cmd", msg.topic)

    def _run(self) -> None:
        published: dict[str, tuple[int, float]] = {}
        prev = time.time()
        while not self._stop.wait(self._tick):
            now = time.time()
            dt, prev = now - prev, now
            while True:
                try:
                    did, body = self._inbox.get_nowait()
                except queue_mod.Empty:
                    break
                unit = self.doors.get(did)
                if unit is None and body.get("type") == P.CMD_OPEN:
                    unit = self.doors[did] = DoorUnit(did, **self._unit_args)
                    log.info("가상 자동문 %s 생성", did)
                ok = unit is not None and unit.command(str(body.get("type")))
                log.info("가상 자동문 %s %s -> %s", did, body.get("type"), "ok" if ok else "무시")
            for did, unit in list(self.doors.items()):
                unit.step(dt)
                v, t = published.get(did, (-1, 0.0))
                if unit.version != v or now - t >= 1.0:
                    self._client.publish(P.topic_state(did), json.dumps(unit.state()), 0, True)
                    published[did] = (unit.version, now)
