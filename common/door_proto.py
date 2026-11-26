"""자동문 MQTT 계약 (FMS <-> 문 제어기). 시뮬레이터(`fms_server/door_sim.py`)도 같은 계약을 쓴다.

실제 자동문은 FMS 가 시뮬레이션하지 않는다 — 별도 제어기가 상태를 발행하고 명령을 받는다.

    door/v1/{door_id}/state   제어기 -> FMS   (retain, 바뀔 때 + 1 s 마다)
    door/v1/{door_id}/cmd     FMS -> 제어기   (QoS 1)

state 페이로드 (JSON):
    door_id     str
    timestamp   float (epoch s)
    door        CLOSED | OPENING | OPEN | CLOSING
    held        bool          RELEASE 전까지 닫히지 않는 열림 유지 중
    mode        NORMAL | OUT_OF_SERVICE   (선택, 없으면 NORMAL) 비정상이면 FMS 는 문이 열렸다고 보지 않는다
    fault       str | null    (선택) 장애 설명

cmd 페이로드 (JSON):
    cmd_id  str
    type    OPEN | RELEASE

OPEN 은 "열고 RELEASE 가 올 때까지 열어 둔다" 이다. FMS 는 문이 닫혀 있는 동안 OPEN 을 주기적으로 다시 보낸다
(멱등이어야 한다). RELEASE 는 열림 유지를 풀고 제어기는 문을 닫는다.
"""

from __future__ import annotations

TOPIC_PREFIX = "door/v1"
TOPIC_STATE_ALL = f"{TOPIC_PREFIX}/+/state"

DOOR_CLOSED = "CLOSED"
DOOR_OPENING = "OPENING"
DOOR_OPEN = "OPEN"
DOOR_CLOSING = "CLOSING"

MODE_NORMAL = "NORMAL"
MODE_OUT_OF_SERVICE = "OUT_OF_SERVICE"
MODES = (MODE_NORMAL, MODE_OUT_OF_SERVICE)

CMD_OPEN = "OPEN"
CMD_RELEASE = "RELEASE"


def topic_state(door_id: str) -> str:
    return f"{TOPIC_PREFIX}/{door_id}/state"


def topic_cmd(door_id: str) -> str:
    return f"{TOPIC_PREFIX}/{door_id}/cmd"
