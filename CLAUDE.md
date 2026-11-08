# CLAUDE.md

이 리포에서 일하는 AI 코딩 에이전트용 안내.

## 개요

AMR 플릿 관리 서버(FMS, Python/FastAPI). 2026-11-08 에 makerton(대회 리포)의 `fms_server/`, `common/` 을 파일 복사로 옮겨 시작했다 (이력 없음).
로봇 클라이언트(C++)·뷰어·브로커 설정은 형제 폴더 `../AMR-simulator` (public 리포)에 있다. LastGate(보관소 서버·웹)는 makerton 리포에만 있고 여기에는 없다. `locker.py`/`cargo.py` 는 MQTT 로 LastGate 와 연동한다.

## 작업 규칙

- 사용자는 한국어로 소통한다. 주석·문서도 한국어로 쓰고 기존 스타일을 따른다.
- push, 태그, GitHub 리포 설정 변경은 사용자 확인 후에 한다.
- 기능을 바꾸면 `fms_server/README.md`(사용법·API)와 `docs/` 해당 문서도 같이 고친다.
- 단일 프로세스·worker 1개 전제다 (파일 저장소 + 프로세스 내부 락).

## 명령 (리포 루트)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
.venv/bin/python -m pytest fms_server/tests/ -q
```

환경변수(`FMS_MQTT_HOST`, `FMS_DATA_ROOT`, `FMS_PUBLIC_URL` 등)는 `fms_server/README.md` 표와 `fms_server/config.py` 참고. 테스트는 `conftest.py` 가 MQTT 포트(18859)와 데이터 루트(임시 폴더)를 격리한다. 테스트는 리포 루트에서 돌려야 한다: `maps/641931de9eae7cecb34d5765`, `maps/gen_loop` (AMR-simulator 에서 복사한 고정 맵)을 상대경로로 읽는다. JS 테스트: `node --test fms_server/tests/zone-geometry.test.js`.

## 계약 (가장 주의)

- `common/` 이 원본이다: `schemas.py`(로봇 Order/InstantAction/State), `map_model.py`, `map_annotations.py`, `elevator_proto.py`.
- 사본: AMR-simulator `viewer/common/` 의 앞 세 파일 (바이트 동일해야 함, `test_common_sync.py` 가 검사. 사본 폴더가 없으면 skip). C++ `include/schemas.hpp`/`src/schemas.cpp` 는 수동 포팅.
- 로봇 프로토콜을 바꾸면 순서: 이 리포 `common/` 수정 -> `cp` 로 AMR-simulator `viewer/common/` 갱신 -> C++ 포팅 -> 양쪽 리포 각각 커밋. 한쪽만 바꾸면 State 파싱(`extra=forbid`)에서 깨진다.
- MQTT 토픽: `fms/v1/{id}/{order,instant,state,connection}`. 맵은 `SET_MAP` instant action 의 URL 로 번들 zip 을 로봇이 받아간다 (`FMS_PUBLIC_URL`).

## 구조

세부는 [docs/architecture.md](docs/architecture.md). 요약:
- `main.py` lifespan: 저장소·MQTT·맵 코디네이터·작업 코디네이터·승강기 제어기/내장 시뮬레이터·배정 루프.
- `task.py`(작업 상태·Order·다층 stage), `route.py`(층간 계획), `allocator.py`(1초 배정), `traffic.py`(맵별 정원·점유 존 예약, `traffic.json`).
- `mapkit/`: `store.py` 버전 저장, `ingest.py` 검증, `bake.py` 주행금지 굽기, `chargers.py`.
- 락 순서: `JobCoordinator.lock -> _EDIT_LOCK -> LocationStore._lock`.
- 웹 UI 는 `static/` 의 순수 HTML/JS (빌드·CDN 없음). `tests/zone-geometry.test.js` 는 node 로 돈다.
