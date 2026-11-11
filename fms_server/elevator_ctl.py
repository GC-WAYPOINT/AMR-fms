"""
엘리베이터 제어기 클라이언트
===========================

실제(또는 시뮬레이터) 승강기 제어기와 MQTT 로 말한다 (`common/elevator_proto.py`).
FMS 는 승강기를 시뮬레이션하지 않는다 — 여기는 상태를 받아 두고 명령을 보내며,
**승강기 한 대를 한 job 이 독점하는 임대(lease)** 만 직접 관리한다.

- 임대는 `lease_path`(`{data_root}/elevator_leases.json`)에 저장하고 재시작 때 복원한다.
- 대기열: 임대를 못 잡은 요청은 요청 시각 순으로 줄을 선다 (FIFO). 임대가 풀리면 줄 맨 앞만 잡는다.
  줄에 선 작업이 죽으면(요청을 `QUEUE_STALE` 초 동안 다시 안 하면) 줄에서 뺀다.
- "확인 필요"(attention): 작업이 실패·취소됐는데 로봇이 칸 안이면 임대와 문 열림을 유지하고 표시해 둔다.
  운영자가 `recover` 로 하차시키거나 비었음을 확인해 풀 때까지 다른 작업이 못 쓴다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

from common import elevator_proto as P

log = logging.getLogger("fms.elevator")

QUEUE_STALE = 10.0   # 줄에 선 요청이 이 시간 동안 갱신되지 않으면 버린다 (초)


@dataclass(frozen=True)
class CarState:
    elevator_id: str
    at: Optional[str]
    target: Optional[str]
    door: str
    moving: bool
    held: bool
    received_at: float
    mode: str = P.MODE_NORMAL
    fault: Optional[str] = None

    def open_at(self, map_id: str) -> bool:
        return self.at == map_id and self.door == P.DOOR_OPEN

    @property
    def in_service(self) -> bool:
        return self.mode == P.MODE_NORMAL


class ElevatorController:
    def __init__(self, settings, *, stale_after: float = 5.0, events=None, lease_path=None) -> None:
        self._settings = settings
        self.events = events
        self._stale_after = stale_after
        self._lock = threading.RLock()
        self._cars: dict[str, CarState] = {}
        self._leases: dict[str, str] = {}
        self._attention: dict[str, dict] = {}                 # elevator_id -> {job_id, robot_id, reason, at}
        self._queue: dict[str, list[list]] = {}               # elevator_id -> [[owner, requested_at, seen_at], ...]
        self._lease_path = Path(lease_path) if lease_path else None
        self._listeners: list = []
        self._connected = False
        self._load()
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{settings.mqtt_client_id}-elevator", clean_session=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- 영속화 --
    def _load(self) -> None:
        if self._lease_path is None or not self._lease_path.is_file():
            return
        try:
            data = json.loads(self._lease_path.read_text(encoding="utf-8"))
            self._leases = {str(k): str(v) for k, v in (data.get("leases") or {}).items()}
            self._attention = {str(k): dict(v) for k, v in (data.get("attention") or {}).items()}
        except (ValueError, OSError, AttributeError) as exc:
            log.error("승강기 임대 파일을 읽을 수 없다 (%s) — 임대 없이 시작", exc)
            self._leases, self._attention = {}, {}
        if self._leases:
            log.info("승강기 임대 %d개 복원", len(self._leases))

    def _save(self) -> None:
        if self._lease_path is None:
            return
        try:
            self._lease_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._lease_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"leases": self._leases, "attention": self._attention},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._lease_path)
        except OSError:
            log.exception("승강기 임대 저장 실패")

    # -- 수명 주기 --
    def start(self) -> None:
        try:
            self._client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port, self._settings.mqtt_keepalive)
            self._client.loop_start()
        except Exception:
            log.exception("엘리베이터 MQTT 시작 실패 (재연결은 paho 담당)")

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            self._connected = True
            client.subscribe(P.TOPIC_STATE_ALL, 0)

    def _on_message(self, client, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[3] != "state" or not msg.payload:
            return
        try:
            body = json.loads(msg.payload)
            mode = str(body.get("mode") or P.MODE_NORMAL)
            car = CarState(parts[2], body.get("at"), body.get("target"), str(body.get("door")),
                           bool(body.get("moving")), bool(body.get("held")), time.time(),
                           mode if mode in P.MODES else P.MODE_OUT_OF_SERVICE, body.get("fault") or None)
        except (ValueError, AttributeError):
            log.warning("%s: 잘못된 state", msg.topic)
            return
        self.apply_state(car)

    def apply_state(self, car: CarState) -> None:
        with self._lock:
            previous = self._cars.get(car.elevator_id)
            self._cars[car.elevator_id] = car
            listeners = list(self._listeners)
        if self.events is not None and (previous.mode if previous else P.MODE_NORMAL) != car.mode:
            self.events.record("elevator.fault", severity="info" if car.in_service else "error",
                               elevator_id=car.elevator_id, mode=car.mode, fault=car.fault)
        for cb in listeners:
            try:
                cb(car)
            except Exception:
                log.exception("엘리베이터 리스너 실패")

    # -- 조회 --
    def state(self, elevator_id: str) -> Optional[CarState]:
        """최근(stale_after 이내) 상태. 제어기가 죽었으면 None."""
        with self._lock:
            c = self._cars.get(elevator_id)
        if c is None or time.time() - c.received_at > self._stale_after:
            return None
        return c

    def unavailable(self) -> frozenset:
        """계획에서 빼야 하는 승강기: 장애(mode != NORMAL) 또는 확인 필요."""
        with self._lock:
            out = {eid for eid, c in self._cars.items() if not c.in_service}
            out.update(self._attention)
            return frozenset(out)

    def snapshot(self, elevator_id: str) -> dict:
        """관제 표시용 상태. 오래된 수신값도 보존하되 현재 상태와 구분한다."""
        with self._lock:
            car = self._cars.get(elevator_id)
            owner = self._leases.get(elevator_id)
            attention = dict(self._attention[elevator_id]) if elevator_id in self._attention else None
            waiting = [entry[0] for entry in self._queue.get(elevator_id, [])]
        extra = dict(lease_owner=owner, attention=attention, queue=waiting)
        if car is None:
            return dict(liveness="UNKNOWN", at=None, target=None, door=None, moving=None, held=None,
                        received_at=None, mode=None, fault=None, **extra)
        fresh = time.time() - car.received_at <= self._stale_after
        return dict(liveness="ONLINE" if fresh else "STALE", at=car.at,
                    target=car.target, door=car.door, moving=car.moving, held=car.held,
                    received_at=car.received_at, mode=car.mode, fault=car.fault, **extra)

    # -- 임대 --
    def acquire(self, elevator_id: str, owner: str, now: Optional[float] = None) -> bool:
        """임대를 잡는다. 이미 내 것이면 True. 남이 쓰는 중이면 줄을 서고 False (요청 시각 순 FIFO)."""
        now = time.time() if now is None else now
        with self._lock:
            current = self._leases.get(elevator_id)
            if current == owner:
                return True
            queue = self._queue.setdefault(elevator_id, [])
            queue[:] = [entry for entry in queue if now - entry[2] <= QUEUE_STALE or entry[0] == owner]
            entry = next((e for e in queue if e[0] == owner), None)
            if entry is None:
                entry = [owner, now, now]
                queue.append(entry)
                queue.sort(key=lambda e: e[1])
            entry[2] = now
            if current is not None or elevator_id in self._attention or queue[0][0] != owner:
                return False
            queue.pop(0)
            self._leases[elevator_id] = owner
            self._save()
        if self.events is not None:
            self.events.record("elevator.lease", job_id=owner, elevator_id=elevator_id,
                               waited=round(now - entry[1], 2))
        return True

    def lease_owner(self, elevator_id: str) -> Optional[str]:
        with self._lock:
            return self._leases.get(elevator_id)

    def all_leases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._leases)

    def queue_position(self, elevator_id: str, owner: str) -> Optional[int]:
        with self._lock:
            return next((i for i, e in enumerate(self._queue.get(elevator_id, [])) if e[0] == owner), None)

    def release_lease(self, elevator_id: str, owner: str) -> None:
        with self._lock:
            if self._leases.get(elevator_id) != owner:
                return
            del self._leases[elevator_id]
            self._attention.pop(elevator_id, None)
            self._save()
        if self.events is not None:
            self.events.record("elevator.release", job_id=owner, elevator_id=elevator_id)

    def forget(self, owner: str) -> None:
        """작업이 끝났다 — 서 있던 줄에서 뺀다."""
        with self._lock:
            for queue in self._queue.values():
                queue[:] = [entry for entry in queue if entry[0] != owner]

    def transfer(self, elevator_id: str, owner: str, new_owner: str) -> bool:
        """임대를 다른 작업으로 넘긴다 (칸 내 복구 작업이 이어받는다). 확인 필요 표시는 유지한다."""
        with self._lock:
            if self._leases.get(elevator_id) != owner:
                return False
            self._leases[elevator_id] = new_owner
            if elevator_id in self._attention:
                self._attention[elevator_id]["job_id"] = new_owner
            self._save()
            return True

    def mark_attention(self, elevator_id: str, job_id: str, robot_id: Optional[str], reason: str) -> None:
        """칸 안에 로봇이 남았을 수 있다. 임대를 유지하고 운영자 확인을 기다린다."""
        with self._lock:
            self._attention[elevator_id] = {"job_id": job_id, "robot_id": robot_id, "reason": reason,
                                            "at": time.time()}
            self._save()
        if self.events is not None:
            self.events.record("elevator.attention", severity="error", job_id=job_id, robot_id=robot_id,
                               elevator_id=elevator_id, reason=reason)

    def attention(self, elevator_id: str) -> Optional[dict]:
        with self._lock:
            return dict(self._attention[elevator_id]) if elevator_id in self._attention else None

    def clear_attention(self, elevator_id: str) -> None:
        with self._lock:
            if self._attention.pop(elevator_id, None) is not None:
                self._save()

    # -- 명령 --
    def _send(self, elevator_id: str, body: dict) -> None:
        body = {"cmd_id": uuid.uuid4().hex[:12], **body}
        self._client.publish(P.topic_cmd(elevator_id), json.dumps(body), qos=1)
        log.info("엘리베이터 %s 명령 %s", elevator_id, body)

    def call(self, elevator_id: str, map_id: str, hold: bool = True) -> None:
        self._send(elevator_id, {"type": P.CMD_CALL, "map_id": map_id, "hold": hold})

    def go(self, elevator_id: str, map_id: str, hold: bool = True) -> None:
        self._send(elevator_id, {"type": P.CMD_GO, "map_id": map_id, "hold": hold})

    def release(self, elevator_id: str) -> None:
        self._send(elevator_id, {"type": P.CMD_RELEASE})

    def publish_json(self, topic: str, body: dict) -> None:
        self._client.publish(topic, json.dumps(body), qos=1)
