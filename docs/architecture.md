# 개발 구조와 구현 규칙

[프로젝트 시작 안내](../README.md) · [에이전트 작업 규칙](../CLAUDE.md)

구현의 세부 규칙을 모은 문서다. 설치·실행은 README, 서버 사용/API는 FMS README에서 관리한다.
기존 설계의 검증 수치와 성능 값은 개발 당시의 관측 참고값이며 환경에 따라 달라진다.

## 구성 요소와 통신

| 구성 요소 | 책임 | 통신 |
|---|---|---|
| C++ 로봇 클라이언트 | 로봇별 오더 실행·경로계획·물리·위치 추정 | MQTT 상태/오더, HTTP 맵 다운로드 |
| FMS | 현장·맵·목적지·작업·승강기 관리, 자동 배정 | REST/WebSocket, 로봇·승강기 MQTT |
| Qt 뷰어 | 관측과 표시, 로봇을 따라 맵 전환 | MQTT 구독과 FMS HTTP GET |
| Mosquitto | 메시지 중계 | 개발 기본 localhost:1883 |

`common/schemas.py`는 로봇 메시지, `common/map_model.py`는 맵,
`common/map_annotations.py`는 주석, `common/elevator_proto.py`는 승강기 MQTT 계약의 원본이다.
FMS는 이 리포의 common을 직접 사용한다. 로봇·뷰어는 별도 AMR-simulator 리포에 있고, 그쪽 `viewer/common`은 사본, C++ 계약은 수동 포팅한다.


## FMS와 웹 UI

- `main.py`의 lifespan이 저장소·MQTT·맵 코디네이터·작업 코디네이터·승강기 제어기·내장 시뮬레이터·배정 루프를 시작하고 종료한다.
- `task.py`는 작업 상태와 단일 층 Order, 다층 stage 실행을 담당한다. `route.py`가 층간 계획을 만들고 `elevator_ctl.py`가 수신 상태와 임대를 관리한다.
- `allocator.py`는 게시판 작업을 1초마다 확인한다. 수동·자동 배정과 상태 갱신은 작업 코디네이터의 같은 락을 사용한다.
- 지도 버전 저장은 `mapkit/store.py`, 읽기·검증은 `ingest.py`, 주행금지구역 굽기는 `bake.py`가 담당한다. 수정 시 이전 baked 이미지가 아니라 source를 재사용한다.
- `locations.py`의 공유 RLock과 임시 파일 교체가 요청 간 쓰기를 보호한다. 목적지 이름 변경은 프리셋과 미해결 수동 pending 작업의 참조를 갱신하며, 이미 생성된 작업 스냅샷은 유지한다.
- 픽업스테이션(`pickup_stations.py`, `api/pickup_stations.py`)은 맵 버전의 pickup zone과 `locations.json`의 `type=pickup` location을 `zone_id`로 1:1 연결한다.
  락 순서는 `JobCoordinator.lock → _EDIT_LOCK → LocationStore._lock`이다. 생성·zone 변경은 `edit_zone(activate=False)`로 새 버전을 준비하고
  location을 쓴 뒤 활성화한다. 두 파일은 원자적이지 않으므로 목록 조회가 양쪽을 대조해 broken을 표시하고 삭제로 정리한다.
  pickup zone은 `prohibit_zones()`에 들지 않아 굽지 않는다.
- HTML/CSS/JS는 `fms_server/static/`에서 제공하며 npm 빌드나 CDN은 필요 없다. `fms.js`는 현장·맵 선택과 Canvas, `zone-editor.js`/`map-points.js`는 배치 편집, `jobs.js`는 프리셋과 게시판을 맡는다.
- `robot-details.js`는 DOM을 유지하며 팝업 데이터를 갱신한다. `elevator-status.js`는 운행 상태 표만 갱신해 연결 편집 폼을 보존한다. 서버 미연결, 상태 미수신, 오래된 마지막 수신값을 구분한다.

## 교통 제어

교통 제어는 `fms_server/traffic.py`가 맵별 이동 허가·대기 node·존을 관리하고 `JobCoordinator.lock`을 공유한다.
예약은 MQTT 발행 전에 `traffic.json`에 저장하며 TTL로 해제하지 않는다. 해제에는 새 State의 노드 통과·차체 이탈·정지 증거가 필요하다.
`Job.traffic.sources`로 삽입 노드를 원래 명령 진행률에서 제외한다. C++ MapModel은 `annotations.json`의 occupancy 경계를 읽고
planner는 노드별 `allowed_zone_ids` 이외의 존을 차체 외접원+0.1 m로 막는다. 점유 존 맵에서는 PSO를 끈다.
사각형 회전은 실제 `polygon`에 저장한다. Python/C++은 같은 중심·직교축·반너비·반높이로 점유 경계를 검사하며,
예전 예약 파일의 축 평행 경계 4개 값도 복원한다. `zone-geometry.js`는 회전 핸들·로컬 축 크기 조절·범위 검사를 맡는다.
`traffic.js`는 편집 입력을 보존하면서 정원·예약·존 상태를 표시한다. 운영·제약은 [교통 제어](traffic.md)를 따른다.

## 세부 계약 변경 시

1. Python 원본 common을 바꾸면 뷰어에 대응 사본이 있는 파일도 동기화한다.
2. 로봇 프로토콜이면 AMR-simulator 의 `include/schemas.hpp`, `src/schemas.cpp`와 생산자·소비자를 함께 확인한다.
3. 기능 설명은 해당 사용자 문서 한 곳에서 관리하고 다른 문서는 링크로 연결한다.

## LastGate 배송 연동

- 보관소(LastGate, 이 리포 밖) `server/core.py`가 물품·계정·묶음을 SQLite에 저장하고 `locker/robot/*` 계약을 발행한다.
  DB 트랜잭션에 발행 대기열도 함께 기록하며 heartbeat로 active Job을 재통지한다.
- `fms_server/locker.py`가 FMS MQTT 콜백의 큐를 별도 워커에서 소비한다. 네트워크 콜백에서 배송 계획을 수행하지 않는다.
- 배송 전이와 작업 dispatch는 `JobCoordinator.lock`을 공유한다. `external_owner`와 로봇 예약으로
  보관소 접근·최종 묶음 대기·PICK·DROP·복귀·오류 차단 사이의 중복 배정을 막는다.
- 예약과 진행은 `locker_deliveries.json`에서 시작 시 복원한다. 하위 작업 생성 후 연결 저장 전에 중단되면
  external_owner와 단계 이름으로 기존 작업을 찾아 재사용한다. assigned/working 오더는 재발행하지 않는다.
- LastGate 확정 목록이 물품 장부의 기준이다. FMS는 목적지와 좌표를 복사해 하위 작업을 만들고 PICK/DROP FINISHED를 관측한다.
  마지막 액션 뒤 1초 대기로 종료 보고를 관측하며 놓친 보고는 완료로 추정하지 않는다.
- 물품별 DELIVERED는 DROP 후, FAILED와 JOB_DONE은 보관소 복귀 후 발행한다. 불확실 인도는 운영자가
  실제 인도 목록과 잔여 적재물을 대조한 뒤 복귀시킨다. `docs/pickup-station.md`는 픽업스테이션·트레이 설계안이며 구현 단계를 표시한다.
- FMS 물품 장부 `cargo.py`(`cargo.json`)가 물품 ID·묶음·트레이·상태를 소유한다. `locker.py`가 최종 묶음을 장부에 저장한 뒤
  물품별 PICK/DROP 하위 작업을 만들고, tick마다 로봇 action_states를 관측해 RUNNING/FINISHED를 반영한다.
  하위 작업이 FINISHED 없이 끝나면 시작이 관측된 물품만 unknown이 된다. 트레이는 탈착식이며 장부가 칸 상태(`trays`)를 물품 상태와 따로 둔다.
  반납 지점에서 칸별 `TRAY_RETURN:{bundle_id}:{tray}` DROP 완료로 칸을 비우고, 두 칸이 비면 묶음을 닫는다.
- 트레이 반납 뒤 `docking` 단계에서 충전소 DOCK 하위 작업을 만든다(`locker.py` `_dock`). 보관소 `JOB_DONE`은 `docking` 진입 때 보내고,
  로봇 예약은 도킹이 끝날 때 푼다. 도킹 실패는 배송을 차단하지 않고 `dock_note`로 남긴다.
- 배송 진행 표시는 `delivery_progress.py`가 배송 레코드·하위 작업 stage·장부로 계산한다(상태 저장 없음). `locker.py`는
  `_save`에서 단계 전환 시각(`timeline`)만 남긴다. 화면(로봇 팝업·진행 상세 창·작업 게시판·보관소 화면)은 `GET /sites/{site}/cargo`의
  `deliveries`와 `GET /locker`의 같은 값을 쓴다.
