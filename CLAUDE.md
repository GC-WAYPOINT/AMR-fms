# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

이 리포에서 일하는 AI 코딩 에이전트용 안내.

## 개요

AMR 플릿 관리 서버(FMS, Python/FastAPI). 2026-11-08 에 makerton(대회 리포)의 `fms_server/`, `common/` 을 파일 복사로 옮겨 시작했다 (이력 없음).
로봇 클라이언트(C++)·뷰어·브로커 설정은 형제 폴더 `../AMR-simulator` (public 리포)에 있다. 택배(LastGate 보관소 배송) 기능은 makerton 리포에만 있고 이 리포에서는 뺐다.

## 작업 규칙

- 사용자는 한국어로 소통한다. 주석·문서도 한국어로 쓰고 기존 스타일을 따른다.
- push, 태그, GitHub 리포 설정 변경은 사용자 확인 후에 한다.
- 기능을 바꾸면 `fms_server/README.md`(사용법·API)와 `docs/` 해당 문서도 같이 고친다. 기능 설명은 한 문서에만 두고 나머지는 링크로 연결한다.
- 단일 프로세스·worker 1개 전제다 (파일 저장소 + 프로세스 내부 락).
- FMS 는 로봇을 시뮬레이션하지 않는다. 로봇에 대해 아는 것은 MQTT 로 받은 State/Connection(`fleet.py`)뿐이고, 로봇별 경로계획은 로봇이 한다.

## 명령 (리포 루트)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.11+
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000   # UI: / , API 문서: /docs , 상태: /health
.venv/bin/python -m pytest fms_server/tests/ -q                      # 전체 (300개, 약 7분)
.venv/bin/python -m pytest fms_server/tests/test_task.py -q          # 파일 하나
.venv/bin/python -m pytest fms_server/tests/test_task.py::test_이름 -q   # 테스트 하나 (-k 패턴도 가능)
node --test fms_server/tests/zone-geometry.test.js                   # JS 기하 테스트
.venv/bin/python -m fms_server.mapkit.ingest maps/gen_loop           # 맵 폴더 검증 CLI
```

- 린터·포매터·타입체커 설정은 없다. 검증은 pytest 와 node 테스트로 한다.
- 전체 테스트가 느린 이유는 `TestClient(app)` 를 쓰는 테스트마다 lifespan 종료(스레드 join)에 2~3초가 걸려서다. 작업 중에는 관련 파일만 돌리고 마지막에 전체를 돌린다.
- 테스트는 리포 루트에서 돌려야 한다: `maps/641931de9eae7cecb34d5765`, `maps/gen_loop` (AMR-simulator 에서 복사한 고정 맵)을 상대경로로 읽는다.
- `conftest.py` 가 MQTT 포트(18859, 아무도 안 듣는 포트)와 데이터 루트(임시 폴더)를 격리한다. `fms_server.config` 는 import 시점에 환경변수를 읽으므로 설정은 import 전에 정해져야 한다.
- 환경변수(`FMS_MQTT_HOST`, `FMS_DATA_ROOT`, `FMS_PUBLIC_URL`, `FMS_ELEVATOR_SIM` 등)는 `fms_server/README.md` 표와 `fms_server/config.py` 참고. 브로커 없이도 서버는 뜬다.
- 브로커·로봇은 AMR-simulator 리포 루트에서 띄운다: `mosquitto -c broker/mosquitto.conf -v`, `./robot_client --id AMR-001 --host 127.0.0.1`.

### 테스트 작성 패턴

- API 테스트: `with TestClient(app) as c:` 로 lifespan 을 띄운 뒤 `app.state.sites = SiteRegistry(tmp_path)` 로 갈아끼운다. lifespan 에서 registry 를 캡처한 객체(`app.state.coordinator._registry`, `app.state.broadcaster._registry` 등)도 같이 바꿔야 한다 (`test_sites_api.py` 의 `client` fixture 참고).
- 로직 테스트: `JobCoordinator` 를 가짜 `Mqtt` 클래스(발행을 리스트에 모음)와 `tmp_path` 저장소로 직접 만든다 (`test_presets_allocator.py` 의 `env` fixture 참고). 로봇 보고는 `tests/factories.py` 의 `make_state`/`make_connection` 을 `fleet.apply_state` 에 넣어 흉내 낸다.

## 계약 (가장 주의)

- `common/` 이 원본이다: `schemas.py`(로봇 Order/InstantAction/State), `map_model.py`, `map_annotations.py`, `elevator_proto.py`.
- 사본: AMR-simulator `viewer/common/` 의 앞 세 파일 (바이트 동일해야 함, `test_common_sync.py` 가 검사. 사본 폴더가 없으면 skip, 위치는 `AMR_SIM_DIR` 로 바꿀 수 있다). C++ `include/schemas.hpp`/`src/schemas.cpp` 는 수동 포팅.
- 로봇 프로토콜을 바꾸면 순서: 이 리포 `common/` 수정 -> `cp` 로 AMR-simulator `viewer/common/` 갱신 -> C++ 포팅 -> 양쪽 리포 각각 커밋. 한쪽만 바꾸면 State 파싱(`extra=forbid`)에서 깨진다.
- MQTT 토픽:
  - 로봇: `fms/v1/{id}/{order,instant,state,connection,factsheet}` (factsheet 는 retained, 선택). 맵은 `SET_MAP` instant action 의 URL 로 번들 zip 을 로봇이 받아간다 (`FMS_PUBLIC_URL`).
  - 승강기: `elev/v1/{elevator_id}/{state,cmd}` (`common/elevator_proto.py`). 내장 시뮬레이터(`FMS_ELEVATOR_SIM=1`, 기본)와 외부 `tools/elevator_sim.py` 가 같은 계약을 쓴다.
- 단위: API 의 `theta` 는 라디안, 웹 입력과 구역 `rotation` 은 도(°).

## 구조

세부는 [docs/architecture.md](docs/architecture.md), 기능별로 [docs/traffic.md](docs/traffic.md)·[docs/elevators.md](docs/elevators.md)·[docs/pickup-station.md](docs/pickup-station.md), 제약은 [docs/known-issues.md](docs/known-issues.md), 미구현 기능 계획은 [docs/roadmap.md](docs/roadmap.md). 요약:

- `main.py` lifespan: 저장소·MQTT·맵 코디네이터·작업 코디네이터·승강기 제어기/내장 시뮬레이터·배정 루프를 만들어 `app.state` 에 싣는다. `api/` 라우터는 전역 객체 대신 `request.app.state.*` 로 꺼내 쓴다.
- `task.py`(작업 상태·Order·다층 stage), `route.py`(층간 계획), `allocator.py`(1초 배정), `traffic.py`(맵별 정원·점유 존 예약, `traffic.json`).
- `mapkit/`: `store.py` 버전 저장, `ingest.py` 검증, `bake.py` 주행금지 굽기, `chargers.py`.
- `events.py`(이벤트 로그 `events.db`) 위에 `robot_events.py`·`kpi.py`·`alerts.py`·`webhooks.py`. `auth.py`(API 키, 기본 꺼짐).
- `assign.py`(로봇 비용 순위), `battery.py`·`charger_pool.py`(자동 충전·대기 복귀, 기본 꺼짐), `scheduler.py`(예약 실행), `api/ops.py`(현장 정지·대피). 사용법은 [docs/operations.md](docs/operations.md)·[docs/charging.md](docs/charging.md).
- 웹 UI 는 `static/` 의 순수 HTML/JS (빌드·CDN 없음). `tests/zone-geometry.test.js` 는 node 로 돈다.
- 영속 데이터는 전부 `FMS_DATA_ROOT`(기본 `fms_server/maps/`, gitignore) 아래 JSON 파일이다. 배치는 `fms_server/README.md` "저장 구조" 참고.

### 스레드와 락

- 스레드: paho 네트워크 스레드(State 수신 -> `Fleet` 리스너 호출), asyncio 루프(REST/WebSocket), `job-allocator`(1초), `elevator-sim`, `event-log`(이벤트 commit·구독자 호출), `webhooks`(전송).
- 새 주기 로직은 스레드를 만들지 않고 `allocator.py` 1초 루프의 tick 목록에 얹는다 (로봇 이벤트·교통 안전·배터리·예약 실행·알림).
- `EventLog.record()` 는 큐에 넣기만 한다. 어느 스레드·락 안에서 불러도 된다. 구독자(`add_listener`)는 `event-log` 스레드에서 불리므로 거기서 코디네이터 락을 잡고 오래 일하지 않는다.
- `Fleet` 리스너는 paho 스레드에서 불린다. 그 안에서 블로킹하거나 asyncio 객체를 직접 만지지 않는다 (`api/broadcaster.py` 처럼 `call_soon_threadsafe` 로 넘긴다).
- 작업 상태 변경(배정·취소·진행·교통 전이)은 모두 `JobCoordinator.lock`(RLock) 안에서 한다. `traffic.py` 의 호출도 이 락을 전제로 한다.
- 락 순서: `JobCoordinator.lock -> _EDIT_LOCK -> LocationStore._lock`. 거꾸로 잡지 않는다.

### 헷갈리기 쉬운 개념

- 맵 식별자 셋: `map_id`(현장 안 관리자 라벨, 예 `floor-1`) / `map_uid`(`sha256(site_id/map_id)[:24]`, 로봇이 State 로 보고하는 값) / `version`(내용 해시 12자). 로봇이 배정 맵의 `map_uid` 와 활성 `version` 을 보고해야 작업이 배정된다.
- 맵 편집(구역·목적지·픽업스테이션)은 `base_version` 으로 낙관적 잠금을 한다. 어긋나면 409. 편집은 새 버전을 만들고, 굽기는 항상 `source/` 원본에서 다시 한다 (이전 baked 이미지를 재입력하면 지운 금지 영역이 남는다).
- 작업은 게시 시점에 명령·목적지 좌표·맵을 복사한 스냅샷이다. 이후 프리셋·목적지 수정이 기존 작업에 반영되지 않는 것이 의도다.
- 시스템 작업(`Job.system`: charge / staging / recover)은 FMS 가 만든다. 일반 작업과 같은 경로로 실행하되 게시판·KPI·재시도에서 구분한다.
- 현장 정지·대피는 로봇을 세울 뿐 교통 예약·승강기 임대를 풀지 않는다 (퇴장 증거가 아니다). 취소된 작업도 자동 재게시하지 않는다.
- `assigned` 는 로봇 수신 확인 전에도 예약을 유지한다. 수신 여부가 불명확한 작업은 자동 재게시하지 않는다 (중복 실행 방지). 교통 예약도 TTL 로 풀지 않고 새 State 의 증거(노드 통과·차체 이탈·정지)로만 푼다.
- API 오류 규약: 도메인 예외 `*NotFound` -> 404, `*Error` -> 422, 버전·상태 충돌 -> 409.
