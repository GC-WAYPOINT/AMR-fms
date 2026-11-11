# FMS 상세 구현 계획 (타사 FMS 요소 반영)

## Context

현재 FMS 는 현장·맵 버전·목적지·작업 게시판·점유 존 교통·승강기·LastGate 배송까지 동작한다. 그러나 상용 FMS 가 기본으로 갖춘 운영 기능이 빠져 있다.

- 로봇 프로토콜에 `PAUSE`/`RESUME`/`RESET_ERROR` 가 있고 C++ 클라이언트도 처리하지만 FMS 는 발행 API 가 없다.
- 배정은 게시 순서 × 로봇 ID 순 (`task.py` `allocate_pending`). 우선순위·거리·배터리를 보지 않는다.
- `battery.py`, `planner.py` 는 빈 stub. 자동 충전은 LastGate 배송 끝(`locker.py` `_dock`)에만 있다.
- 이벤트 이력·KPI·알림·인증·webhook 이 없다.
- 교착 감지, 승강기 장애 복구가 없다 (`docs/known-issues.md`).

사용자 결정:
- 산출물: 이 계획을 **`docs/roadmap.md` 로 저장**하고 링크만 추가. 코드 구현은 단계별로 따로 지시받는다.
- 네 영역 모두 포함. 순서는 아래 0→5 단계 (의존 관계 기준으로 정함).
- 프로토콜: FMS 만으로 되는 것 먼저. 로봇 협조 필요한 항목은 5단계에 "양쪽 리포 작업"으로 분리. VDA 5050 어댑터는 제외.
- 저장소: 현재 상태는 JSON 유지, 쌓이는 로그·KPI 만 표준 라이브러리 `sqlite3`.

## 승인 후 실행 (이번 작업 범위)

1. `docs/roadmap.md` 생성: 아래 "타사 조사 요약" ~ "출처" 절을 그대로 옮긴다 (Context·이 절 제외).
2. `README.md` 의 `docs/` 표 행, `docs/architecture.md` 상단 링크, `CLAUDE.md` "구조" 절 문서 링크에 `docs/roadmap.md` 추가.
3. `docs/known-issues.md` 의 해당 제약(충전 스케줄링 없음, 승강기 복구 없음, 자동 교착 해소 없음)에 로드맵 단계 링크를 붙인다.
4. 코드 변경 없음. 커밋·push 는 사용자 확인 후.

검증: 링크 경로가 실제 파일을 가리키는지 `ls` 로 확인. 문서만 바꾸므로 테스트 재실행 불필요.

---

## 타사 조사 요약

| 요소 | 타사 사례 | 현재 FMS | 단계 |
|---|---|---|---|
| 로봇 원격 일시정지·재개·오류 해제 | VDA 5050 instantActions, MiR Fleet, Open-RMF | 프로토콜만 있음, API 없음 | 1 |
| 전체 정지·화재 대피 | MiR evacuation zones, Open-RMF emergency alarm·pullover | 없음 | 1 |
| 로봇 점검(서비스 제외) 상태 | 네이버 ARC brain 상태 구분(수행·대기·충전·수동·점검) | 없음 | 1 |
| 작업 우선순위·예약 실행 | MiR mission scheduler, OTTO schedule, Omron job queuing | FIFO 만 | 2 |
| 거리·배터리 기반 로봇 선택 | OTTO(배터리·유휴 최소화 매칭), Open-RMF(비용 입찰), MiR(위치·가용성) | 로봇 ID 순 | 2 |
| 자동 충전·유휴 대기 위치 복귀 | MiR auto charging & staging, Omron charge management, RMF finishing request | 배송 끝 도킹만 | 2 |
| 로봇 그룹·충전 그룹 | MiR robot/charging groups, OTTO teams | 없음 | 2 |
| 교착 감지 | MiR 특허(진짜 교착과 일시 정체 구분), wait-for 그래프 DFS(논문) | 없음, 대기 이유만 표시 | 3 |
| 유휴 로봇 비켜주기 | 학술 FM, Open-RMF 협상 | 없음 | 3 |
| 구역 정원(limit-robots)·대기열 | MiR limit-robots zone, OTTO queuing | 점유 존(1대), 맵 정원 | 있음 / 3 보강 |
| 감속·일방통행 | OTTO traffic plan(속도 제한·배제 구역) | 유형만 예약, 미구현 | 3, 5 |
| 승강기 예비 전환·타임아웃 | Otis Integrated Dispatch(backup elevator), Schindler(상태 조회) | 임대 1개, 복구 없음 | 3 |
| 이벤트 로그·KPI·알림 | OTTO Fleet Analytics, InOrbit, Formant | 없음 | 0, 4 |
| 외부 연동 API·webhook | MiR REST+webhook, OTTO REST/WebSocket/OPC-UA | REST 있음, webhook 없음 | 4 |
| 인증·권한 | MiR Fleet Enterprise(IEC 62443) | 없음 | 4 |
| 로봇 사양 보고(factsheet)·적재 상태 | VDA 5050 factsheet, state.loads | `State.width/length` 만 | 5 |
| 계획 경로 공유 | VDA 5050 3.0 | `State.local_path` 있음, FMS 미사용 | 3 |
| base/horizon 통행 허가 | VDA 5050 | 있음 (`OrderNode.released`) | 완료 |
| 가상 승강기·플릿 시뮬레이터 | Omron Fleet Simulator | 있음 (내장 승강기 sim + AMR-simulator) | 완료 |

조사 한계: 대부분 벤더 홍보 자료·목차 수준이다. 세부 동작은 추정이 섞여 있으므로 설계는 타사 "기능 목록"만 참고하고 동작은 이 FMS 의 기존 불변식(예약은 증거로만 해제, 불명확한 작업은 재게시 안 함)에 맞춘다.

## 공통 설계 규칙

- 작업·예약 상태 변경은 `JobCoordinator.lock` 안에서. 락 순서 `JobCoordinator.lock -> _EDIT_LOCK -> LocationStore._lock` 유지.
- 주기 로직은 새 스레드를 만들지 않고 `allocator.py` 의 1초 루프에 tick 을 추가한다 (`allocate_pending` 뒤).
- paho 스레드에서 불리는 `Fleet` 리스너는 블로킹 금지. DB 쓰기·HTTP 는 큐에 넣고 워커 스레드에서.
- 새 구성요소는 `main.py` lifespan 에서 만들어 `app.state` 에 싣고, `JobCoordinator` 등에는 선택 인자(`=None` 이면 no-op)로 넘긴다. 기존 테스트가 `JobCoordinator(fleet, registry, jobs, mqtt)` 로 직접 만드는 패턴을 깨지 않는다.
- JSON 필드 추가는 `from_dict` 기본값으로 하위 호환. 기존 `jobs.json`·`site.json` 그대로 읽혀야 한다.
- 기능마다 `fms_server/README.md`(API 표·사용법)와 해당 `docs/` 문서를 같이 고친다.
- 테스트는 기존 두 패턴(`TestClient` + `app.state` 교체 / 가짜 `Mqtt` + `make_state`)을 따른다. 단계 끝에 전체 pytest.

## 0단계 — 이벤트 로그 (다른 단계의 기반)

목표: 누가·언제·무엇을 했는지 남긴다. 1~4단계가 여기에 기록하고 4단계 KPI·알림이 읽는다.

- 새 파일 `fms_server/events.py`: `EventLog(data_root)`.
  - `{FMS_DATA_ROOT}/events.db` (stdlib `sqlite3`, WAL). 테이블 `events(id, ts, site_id, kind, severity, robot_id, job_id, actor, data JSON)`, 인덱스 `(site_id, ts)`, `(robot_id, ts)`, `(job_id)`.
  - `record(kind, **fields)` 는 `queue.Queue` 에 넣기만 한다. 전용 워커 스레드가 묶어서 commit. `stop()` 에서 flush.
  - `query(site, robot, job, kind, since, until, limit)`.
  - 보존: `FMS_EVENT_RETENTION_DAYS`(기본 30) 지난 행을 하루 한 번 삭제. `config.py` 에 필드 추가.
- 기록 지점:
  - `task.py`: `dispatch`(job.assigned), `_advance` 첫 ack(job.working), `_finish`(job.success/failed/canceled), `cancel`.
  - 새 `fms_server/robot_events.py`: `Fleet` 리스너. State 전체가 아니라 **변화만** 기록 (state/sub_state, liveness, paused, mode, localization, 오류 발생·해제, 배터리 10 % 단위). 직전 값은 메모리 dict.
  - `traffic.py`: 대기 시작·해제, 존 예약·해제. `elevator_ctl.py`: 임대 획득·해제. `locker.py` `_save`: 단계 전환 (이미 `timeline` 을 남기는 자리).
  - API 계층: 운영자 조작(일시정지, 취소, 맵 활성화, 구역 편집)은 `actor` 와 함께.
- API: `api/events.py` `GET /api/v1/events?site=&robot=&job=&kind=&since=&limit=`.
- UI: 왼쪽 메뉴 "이력" 페이지(`static/events.js`), 로봇 팝업·작업 행에서 해당 필터로 이동.
- 테스트 `tests/test_events.py`: 기록·조회·보존 삭제, 워커 flush, 코디네이터에 `events=None` 일 때 기존 동작 동일.

## 1단계 — 운영 제어·안전

### 1.1 로봇 즉시 명령 API
- `api/robots.py` 에 `POST /api/v1/robots/{id}/pause`, `/resume`, `/reset-error`.
- `init_position` 과 `task.py` `cancel`, `coordinator.py` SET_MAP 에 흩어진 `InstantAction(...)` 조립을 `mqtt_client.py` 의 `send_instant(robot_id, action_type, params)` 하나로 모은다.
- `RESUME` 은 `pause_source=FMS` 일 때만 보낸다 (LOCAL 은 못 푼다는 프로토콜 규칙). 아니면 409.
- UI: `static/robot-details.js` 팝업에 [일시정지]/[재개]/[오류 해제] 버튼. 상태에 따라 비활성.
- 프로토콜 변경 없음 (C++ `executor.cpp` 가 이미 처리).

### 1.2 현장 전체 정지
- `sites.py` `Site` 에 `ops: {paused: bool, reason, since}` 추가 (`site.json`).
- `POST /api/v1/sites/{site}/pause {map_id?, reason}`, `/resume`. 대상 등록 로봇 전부에 PAUSE.
- 정지 중: `allocate_pending` 이 해당 현장 작업을 건너뛰고 대기 이유를 "현장 정지 중"으로 쓴다. 나중에 ONLINE 이 되는 로봇에도 PAUSE 를 보낸다 (`coordinator.py` `_on_fleet_event` 에 추가, 기존 SET_MAP 쿨다운 방식 재사용).
- 교통 예약·승강기 임대는 건드리지 않는다 (정지는 퇴장 증거가 아니다).

### 1.3 로봇 점검 모드
- `RobotAssignment` 에 `in_service: bool = True`, `note`. `PUT /sites/{site}/robots/{id}` 에 필드 추가.
- `dispatch` 와 `allocate_pending` 후보에서 제외. 진행 중 작업은 그대로 끝낸다.
- `api/views.py` `RobotView` 에 표시 상태 추가: 수행 중 / 대기 / 충전 / 수동 / 점검 / 오류 (ARC 식 한 줄 상태). `static/fms.js` 라벨 표에 추가.

### 1.4 대피 모드
- `POST /api/v1/sites/{site}/evacuation {active, reason, mode: "pause"|"pullover"}`. 상태는 `Site.ops.evacuation`.
- `pause`: 1.2 와 같되 해제 전까지 재개 API 를 막는다.
- `pullover`: 배정 중지 → 진행 작업 취소(`cancel`, 승강기 칸 안 stage 는 하차까지 진행) → 로봇이 IDLE 이 되면 같은 층의 가장 가까운 빈 `parking` 목적지로 시스템 작업 생성 (2.5 의 대기 위치 선택 로직 재사용, 2단계 전에는 `pause` 만 제공).
- 대피 중 승강기 신규 임대 금지 (`task.py` `_run_stage` 의 임대 획득 지점에서 검사).
- 해제 후 취소된 작업은 자동 재게시하지 않는다 (기존 정책). 목록만 보여준다.
- 외부 화재 신호: 4단계 인증이 들어온 뒤 같은 REST 를 건물 시스템이 호출한다. 별도 MQTT 토픽은 만들지 않는다.

### 1.5 실패 작업 재시도
- `POST /sites/{site}/jobs/{id}/retry`: 종료된 작업의 명령을 복사해 새 pending 작업 생성, `retry_of` 필드 기록. `external_owner` 작업은 거절(409, 배송은 `locker` 복귀 절차 사용).

테스트: `tests/test_ops_api.py` (명령 발행·409 조건·정지 중 배정 건너뜀·대피 중 임대 거절), 기존 `test_presets_allocator.py` 에 점검 모드 사례 추가.
문서: `fms_server/README.md` API 표, 새 `docs/operations.md` (정지·대피·점검 운영 절차).

## 2단계 — 배정·충전 자동화

### 2.1 우선순위·시작 시각·마감
- `Job` 에 `priority: int = 0`, `not_before`, `deadline` (ISO 문자열). `api/presets.py` `PublishIn`, `api/jobs.py` `CreateJobRequest` 에 추가.
- `allocate_pending` 정렬 키 `(-priority, created_at, job_id)`. `not_before` 가 미래면 건너뜀. 마감 초과는 실패 처리하지 않고 이벤트만 남긴다.
- `PATCH /sites/{site}/jobs/{id} {priority}` (pending 만). 게시판 UI(`static/jobs.js`)에 우선순위 열·변경.

### 2.2 비용 기반 로봇 선택
- 새 파일 `fms_server/assign.py`: `rank_candidates(job, records, registry, elevators) -> list[robot_id]`.
  - 같은 맵: `route.path_distances(model, robot_xy, [첫 목적지])` (기존 함수 재사용, 0.25 m 격자).
  - 다른 층: 승강기 hop 수 × 고정 벌점 + 승강장까지 거리.
  - 배터리: `charge < policy.min_job_charge` 면 제외, 낮을수록 가산.
  - 동점은 로봇 ID.
- `MapModel` 은 `(map_id, version)` 키로 캐시 (`maps.load_map_model`).
- `allocate_pending` 의 `sorted(available_robots, key=robot_id)` 를 이 순위로 교체. 최종 가용성 검사는 지금처럼 `dispatch` 가 한다.
- 전체 최적 할당(헝가리안)은 하지 않는다. 우선순위 순 greedy.

### 2.3 로봇 그룹
- `RobotAssignment.groups: list[str]`, 프리셋·작업에 `robot_group`, 충전소·대기 목적지에 선택 `groups`.
- 후보 필터: 작업 그룹이 있으면 그 그룹 로봇만. 충전소는 그룹이 비었거나 교집합이 있을 때만 사용.

### 2.4 자동 충전 (`battery.py` 구현)
- 충전소 선택 로직을 `locker.py` 의 `_chargers`/`_busy_chargers`/`_occupied`/`_choose_charger` 에서 새 `fms_server/charger_pool.py` 로 옮기고 `locker.py` 는 그것을 호출한다. 정렬은 직선거리 대신 `path_distances`.
- 정책 `sites/{site}/charging.json`: `{enabled, low: 25, critical: 10, release: 60, opportunity: false, min_job_charge: 20}`. API `GET/PUT /sites/{site}/charging`.
- `BatteryManager.tick()` (allocator 루프, 코디네이터 락 안):
  - 가용·미예약·`in_service` 로봇 중 `charge < low` 이고 충전 중이 아니면 시스템 작업 생성: `docking` 명령, `min_charge = release`, `Job.system = "charge"`.
  - `opportunity` 면 `idle_after` 초 이상 유휴이고 빈 충전소가 있을 때도 보낸다.
  - 빈 충전소가 없으면 이벤트·대기 이유만 남긴다.
- `dispatch` 의 `charge <= 0` 거절을 `min_job_charge` 기준으로 바꾼다.

### 2.5 유휴 대기 위치 복귀 (staging)
- 정책 `staging: {enabled, idle_after: 60}`. 유휴 로봇이 충전소·`parking` 위치에 있지 않으면 가장 가까운 빈 `parking` 으로 `parking` 명령 시스템 작업 (`Job.system = "staging"`).
- 시스템 작업은 선점 가능: 일반 작업 후보가 없고 staging 중인 로봇만 있으면 `allocate_pending` 이 그 시스템 작업을 취소한다. 취소 확인(CANCELED) 후 다음 tick 에 배정된다.
- 게시판은 시스템 작업을 접어서 표시 (배송 하위 작업 숨김과 같은 방식).

### 2.6 예약 실행
- `sites/{site}/schedules.json`: `{id, preset_id, days: [0..6], time: "HH:MM" | interval_s, robot_group, priority, enabled, last_run}`.
- 새 `fms_server/scheduler.py` `Scheduler.tick()`: 때가 되면 프리셋을 게시 (`api/presets.py` 의 게시 로직을 `presets.py` 함수로 내려 공용화). `last_run` 을 먼저 저장해 재시작 중복 방지. 놓친 실행은 건너뛴다.
- API `api/schedules.py` CRUD, UI 는 프리셋 페이지 안에 예약 표.

테스트: `tests/test_assign.py`(거리·배터리·그룹 순위), `tests/test_battery.py`(임계값·충전소 경합·locker 도킹과 충돌 없음), `tests/test_scheduler.py`(시각 주입으로 실행·중복 방지), 기존 `test_locker.py` 가 `charger_pool` 이전 후에도 통과.
문서: `fms_server/README.md` "배정 정책과 상태" 갱신, 새 `docs/charging.md`.

## 3단계 — 교통·승강기 고도화

### 3.1 교착·정체 감지
- `traffic.py` 에 `wait_graph()`: 간선 "로봇 A 가 B 가 가진 자원(존·대기 node·층 정원·승강기 임대)을 기다림". DFS 로 순환 탐지.
- 정체: BUSY/MOVING 인데 N 초 동안 이동 거리 < 임계, 또는 WAITING 이 T 초 초과. 진짜 순환과 구분해 `traffic.deadlock` / `traffic.stall` 이벤트.
- `snapshot()` 응답과 `static/traffic.js` 에 순환에 든 로봇·자원 표시.
- 자동 해소는 **아직 로봇에 허가를 내보내지 않은 예약**(접근 전 대기 node 예약)만 가장 늦은 것부터 회수. 점유 중 존·발행된 허가는 절대 자동 해제하지 않는다 (기존 불변식).

### 3.2 유휴 로봇 비켜주기
- `traffic.py` `_clear_space` 가 차단 원인으로 유휴(가용) 로봇을 찾으면 그 로봇에 2.5 의 staging 작업을 즉시 만든다.

### 3.3 경로 교차 예고 (실험)
- `State.local_path` 로 같은 맵 두 로봇의 남은 경로가 T 초 안에 근접하는지 계산. 점유 존 밖 마주침을 `traffic.conflict` 이벤트로 알린다.
- 자동 PAUSE 는 기본 끔. 옵션을 켜면 우선순위 낮은 쪽을 1.1 의 PAUSE/RESUME 으로 잠깐 세운다.

### 3.4 일방통행·구역 정원
- 일방통행은 FMS 만으로: gate 1개짜리 점유 존을 UI 에서 "일방통행 통로"로 만드는 단축 메뉴 (`static/zone-editor.js`). 새 로직 없음.
- 같은 방향 연속 진입(정원 > 1)은 차체 간격 검증이 필요해 보류.

### 3.5 승강기 견고화
- 임대 영속화: `elevator_ctl.py` 임대를 `{data_root}/elevator_leases.json` 에 저장·복원 (재시작 시 유실 문제 해결).
- 대기열: `acquire` 를 요청 시각 순 FIFO 로 (지금은 먼저 호출한 tick 이 이김). 기아 방지.
- 타임아웃·예비 전환: 호출 대기가 T 초를 넘고 로봇이 아직 탑승 전이면 임대 반납 후 `route.build_plan(avoid={해당 승강기})` 로 재계획 (기존 `avoid` 인자 재사용). 후보가 없으면 계속 대기 + 알림.
- 장애 상태: `common/elevator_proto.py` state 에 선택 필드 `mode: NORMAL|OUT_OF_SERVICE|FIRE`, `fault`. `route._find_hops` 가 비정상 승강기 제외. `elevator_sim.py`·`tools/elevator_sim.py` 에 장애 주입 추가. (이 파일은 뷰어 사본 대상이 아니므로 이 리포만 수정)
- 칸 내 복구: 작업이 실패·취소됐는데 로봇이 칸 안이면 임대와 문 열림(hold)을 유지하고 승강기를 "확인 필요"로 표시. `POST /sites/{site}/elevators/{id}/recover {robot_id, action: "exit"|"release"}` — `exit` 는 현재 층 문이 열려 있을 때 후진 하차 STRAIGHT 오더를 보낸다 (`route.py` 하차 stage 생성 코드 재사용).

테스트: `tests/test_traffic.py` 에 순환·정체·안전한 회수만 되는지, `tests/test_multifloor.py` 에 임대 복원·FIFO·예비 전환·칸 내 복구.
문서: `docs/traffic.md`, `docs/elevators.md`, `docs/known-issues.md` 갱신.

## 4단계 — 관측·외부 연동

### 4.1 KPI
- 새 `fms_server/kpi.py`: `events.db` 집계. `GET /sites/{site}/kpi?from=&to=&bucket=hour|day`.
- 지표: 작업 완료·실패·취소 수와 성공률, 배정 대기 시간, 수행 시간, 로봇별 상태 시간 비율(가동률), 주행 거리(로봇별 누적, `robot_events.py` 에서 pose 적분 후 주기 저장), 충전 횟수·시간, 교통·승강기 대기 시간, 배송 리드타임.
- UI "통계" 페이지: Canvas 직접 그리기 (CDN·빌드 없음 원칙 유지).

### 4.2 알림
- 규칙: 로봇 ERROR/EMERGENCY, LOST/STALE 지속, 배터리 critical, 작업 실패, pending 장기화, 마감 초과, 교착, 승강기 STALE·장애, 배송 차단.
- `events.db` 에 `alerts(id, rule, key, opened_at, acked_by, resolved_at)`. 같은 `key` 는 열린 알림 하나만.
- `GET /alerts`, `POST /alerts/{id}/ack`. 실시간은 `api/broadcaster.py` 와 같은 방식의 `/api/v1/events/stream` WebSocket.

### 4.3 Webhook
- `{data_root}/webhooks.json`: `{id, url, kinds, secret, enabled}`. API CRUD.
- 전송 대기열을 `events.db` 테이블에 저장 (재시작 유지). 워커 스레드가 `httpx` 로 POST, 지수 backoff, `X-FMS-Signature: sha256=` HMAC.

### 4.4 인증·권한
- `FMS_AUTH`(기본 0 — 개발·테스트 무영향). 켜면 `Authorization: Bearer` API 키 필요.
- `{data_root}/auth.json`: 키 해시 + 역할 `viewer`/`operator`/`admin`. FastAPI dependency 를 라우터별로 건다.
- 로봇의 맵 번들 GET 은 별도 로봇 토큰 또는 예외 경로. 이벤트 `actor` 에 키 이름 기록.
- 브로커 인증은 AMR-simulator `broker/mosquitto.conf` 소관 — 문서에 안내만.

### 4.5 외부 주문 API
- `POST /sites/{site}/jobs` 에 `external_id`(멱등 키), `auto_assign`, `priority`, `deadline` 허용. 같은 `external_id` 재요청은 기존 작업 반환.
- 상태 변화는 4.3 webhook 으로 통지.

테스트: `tests/test_kpi.py`, `tests/test_alerts.py`, `tests/test_webhooks.py`(로컬 가짜 수신기), `tests/test_auth.py`(역할별 403, 끔 상태에서 기존 테스트 무변경).

## 5단계 — 로봇 협조 필요 (양쪽 리포 작업)

각 항목은 CLAUDE.md "계약" 순서를 따른다: 이 리포 `common/` 수정 → AMR-simulator `viewer/common/` 복사 → C++ `schemas.hpp/.cpp` 포팅 → 양쪽 커밋.

- 5.1 감속 구역: `map_annotations.py` 존에 `speed_limit`(m/s), `ZoneType.SLOW` 실제 적용. C++ MapModel 이 `annotations.json` 에서 읽어 구역 안 속도 제한. FMS 는 구역 API·UI 만.
- 5.2 Factsheet: retained 토픽 `fms/v1/{id}/factsheet` (모델, 크기, 최대 속도, 트레이 수, 지원 액션, 프로토콜 버전). FMS `fleet.py` 가 보관, 2.2 후보 필터에 사용. `State.traffic_control` 판정도 이쪽으로 이전.
- 5.3 적재 상태: `State.loads: [{tray, payload_id}]`. `cargo.py` 장부와 대조해 불일치를 알림으로. 지금은 시간 기반 시뮬레이션이라 장부만 믿는다.
- 5.4 일방통행 planner 제약: C++ planner 가 역방향 통과 비용을 막는 방식. 3.4 로 충분하면 하지 않는다.

## 범위 밖

- VDA 5050 어댑터, 다중 worker·DB 전면 이전, 중앙 다중 로봇 경로 계획(MAPF), 사람·장애물 안전 인터록.

## 검증 (각 구현 단계 공통, 리포 루트)

```bash
.venv/bin/python -m pytest fms_server/tests/test_<해당>.py -q   # 작업 중
.venv/bin/python -m pytest fms_server/tests/ -q                  # 단계 끝 (약 5분)
node --test fms_server/tests/zone-geometry.test.js               # static JS 기하를 건드렸을 때
```

실기동 확인 (포그라운드, 창 띄움): AMR-simulator 루트에서 `mosquitto -c broker/mosquitto.conf -v` 와 `./robot_client --id AMR-001 --host 127.0.0.1` 2대 이상, 이 리포 루트에서 `.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000` 후 `http://localhost:8000/` 에서 단계별 시나리오(일시정지→재개, 저배터리 자동 충전, 우선순위 추월, 승강기 장애 주입 후 예비 전환)를 확인한다.

## 출처

- MiR Fleet: https://mobile-industrial-robots.com/products/software/mir-fleet , https://www.manualslib.com/manual/1919196/Mir-Fleet.html , https://control.com/news/mir-launches-scalable-cybersecurity-enhanced-autonomous-mobile-robot-amr-fleet-management-software/
- OTTO Fleet Manager: https://ottomotors.com/fleet-manager , https://ottomotors.com/blog/fleet-manager-capabilities-for-amr-scaling
- Omron FLOW Core: https://industrial.omron.eu/en/products/fleet-manager , https://industrial.omron.eu/en/solutions/product-solutions/fleet-simulator
- VDA 5050: https://www.vda.de/en/news/publications/publication/vda-5050 , https://www.ifl.kit.edu/english/6614.php , https://github.com/vda5050/vda5050
- Open-RMF: https://osrf.github.io/ros2multirobotbook/rmf-core.html , https://docs.ros.org/en/jazzy/p/rmf_fleet_adapter/CHANGELOG.html , https://github.com/open-rmf/rmf-web
- 승강기 연동: https://www.schindler.sg/en/elevators/built-in/robot-service-connection.html , https://otis.com/ar/qa/products-services/products/otis-integrated-dispatch
- 네이버 ARC brain: https://guide.ncloud-docs.com/docs/arc-brain-overview
- 관측 플랫폼: https://docs.formant.io/docs/fleet-observability , https://inorbit.ai/operations
- 교착 감지: https://patents.justia.com/patent/20250036145 , https://arxiv.org/pdf/2609.10400
