# AMR-fms

AMR 플릿 관리 서버(FMS). 로봇의 MQTT 상태를 모으고 현장·맵·목적지·작업·승강기·교통을 관리하는 FastAPI 서버와 웹 UI다.
로봇 클라이언트·뷰어·브로커 설정은 별도 리포 [AMR-simulator](https://github.com/DaeHwanZZang/AMR-simulator)에 있고, 두 리포는 MQTT+JSON(+맵 번들 HTTP)으로만 통신한다.

| 경로 | 내용 |
|---|---|
| `fms_server/` | FastAPI 서버, 웹 UI(`static/`), 테스트(`tests/`). 사용법·API는 [fms_server/README.md](fms_server/README.md) |
| `common/` | 프로토콜 계약 원본: `schemas.py`(로봇 메시지), `map_model.py`, `map_annotations.py`, `elevator_proto.py` |
| `tools/elevator_sim.py` | 외부 가상 승강기 (`FMS_ELEVATOR_SIM=0` 일 때만) |
| `maps/` | 테스트 고정 맵 (`641931de9eae7cecb34d5765`, `gen_loop`). AMR-simulator `maps/` 에서 복사. 테스트가 리포 루트 기준 상대경로로 읽는다 |
| `docs/` | [개발 구조](docs/architecture.md), [교통 제어](docs/traffic.md), [승강기](docs/elevators.md), [픽업스테이션 설계](docs/pickup-station.md), [알려진 문제](docs/known-issues.md), [로드맵](docs/roadmap.md) |

## 설치와 실행 (리포 루트)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000   # UI: http://localhost:8000/ , API 문서: /docs
.venv/bin/python -m pytest fms_server/tests/ -q
```

브로커와 로봇은 AMR-simulator 리포 루트에서 띄운다:

```bash
mosquitto -c broker/mosquitto.conf -v
./robot_client --id AMR-001 --host 127.0.0.1    # --map 없이 띄우면 FMS 가 SET_MAP 으로 맵을 준다
```

런타임 데이터(현장·맵 버전·작업)는 `FMS_DATA_ROOT`(기본 `fms_server/maps/`)에 쌓이며 git 에 넣지 않는다.

## 프로토콜 사본 동기화

`common/`이 원본이다. AMR-simulator `viewer/common/{schemas,map_model,map_annotations}.py`는 사본이고, C++ `include/schemas.hpp`/`src/schemas.cpp`는 수동 포팅이다.
계약을 바꾸면 이 리포를 먼저 고치고 사본을 복사한다. `fms_server/tests/test_common_sync.py`가 옆 폴더 `../AMR-simulator`(또는 `AMR_SIM_DIR`)의 사본과 바이트 비교한다.
