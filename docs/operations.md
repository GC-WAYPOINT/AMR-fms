# 운영 제어와 관측

[프로젝트 시작 안내](../README.md) · [FMS](../fms_server/README.md) · [알려진 문제](known-issues.md)

현장을 운영하면서 쓰는 기능의 절차를 모았다: 로봇 즉시 명령, 현장 정지, 대피, 점검 모드, 재시도, 이력·알림·통계, 외부 연동, 인증.
자동 충전·대기 위치·예약 실행은 [충전과 대기 위치](charging.md)에 있다.

## 로봇 즉시 명령

실시간 맵에서 로봇을 눌러 여는 상세 창에 **일시정지 / 재개 / 오류 해제** 버튼이 있다.

| API | 로봇에 보내는 명령 | 거절 |
|---|---|---|
| `POST /api/v1/robots/{id}/pause` | `PAUSE` | state 를 못 받은 로봇 404 |
| `POST /api/v1/robots/{id}/resume` | `RESUME` | 일시정지가 아니거나, 로봇에서 건 일시정지(`pause_source=LOCAL`)거나, 현장 정지·대피 중이면 409 |
| `POST /api/v1/robots/{id}/reset-error` | `RESET_ERROR` | 404 |

- 일시정지는 작업을 유지한다. 재개하면 이어서 간다.
- 로봇 화면·버튼으로 건 일시정지(LOCAL)는 FMS 가 풀 수 없다. 로봇에서 해제한다 (프로토콜 규칙).
- 응답은 발행 여부뿐이다. 결과는 로봇 State 로 확인한다.

## 현장 정지

**운영 제어** 화면의 "현장 정지"는 등록 로봇 전부(또는 선택한 맵의 로봇)에 `PAUSE` 를 보낸다.

| API | 동작 |
|---|---|
| `GET /api/v1/sites/{site}/ops` | 지금 정지·대피 상태 |
| `POST /api/v1/sites/{site}/pause` `{map_id?, reason}` | 정지. `map_id` 를 주면 그 맵만 |
| `POST /api/v1/sites/{site}/resume` | 재개 |

정지 중에는:

- 게시판 작업을 배정하지 않는다. 대기 이유에 "현장 정지 중"이 보인다.
- 나중에 접속하는 로봇에도 `PAUSE` 를 보낸다 (10초 쿨다운). 개별 재개(`/robots/{id}/resume`)는 409 로 막는다.
- **교통 예약과 승강기 임대는 그대로 둔다.** 정지는 로봇이 자리를 비웠다는 증거가 아니다.
- 상태는 `site.json` 의 `ops` 에 저장한다. 서버를 다시 띄워도 정지가 유지된다.

재개는 **현장 정지로 세운 로봇만** 푼다. 그 전에 운영자가 따로 세워 둔 로봇과 LOCAL 일시정지는 건드리지 않는다.

## 대피

`POST /api/v1/sites/{site}/evacuation` `{active, reason, mode}`. 화재 경보 같은 건물 신호는 같은 REST 를 건물 시스템이 호출한다 (별도 MQTT 토픽은 없다. 인증을 켰다면 operator 키를 발급해 준다).

| mode | 동작 |
|---|---|
| `pause` (기본) | 모든 로봇을 제자리에 세운다. 현장 정지와 같되 해제 전까지 현장 재개·개별 재개를 막는다 |
| `pullover` | 배정을 멈추고 진행 중 작업을 취소한 뒤, 유휴가 된 로봇을 **같은 층의 가장 가까운 빈 주차 지점**으로 보낸다 |

- pullover 는 층을 옮기지 않는다. 대피 중에는 승강기 신규 임대를 금지한다.
- 승강기 칸 안에 있는 로봇의 작업은 **하차까지 진행**한 뒤 취소한다.
- 주차 지점(목적지 용도 "주차 지점")이 없거나 다 찼으면 로봇은 그 자리에 선다. 이력에 남는다.
- 해제해도 취소된 작업을 **자동으로 다시 게시하지 않는다** (중복 실행 방지). 응답과 운영 제어 화면에 목록만 보인다. 작업 이력에서 "재시도"로 다시 만든다.

## 점검 모드

로봇 화면의 "점검 전환" 또는 `PUT /api/v1/sites/{site}/robots/{id}` `{in_service: false, note}`.

- 점검 중인 로봇에는 새 작업을 배정하지 않는다 (자동·수동·자동 충전 모두).
- 진행 중인 작업은 끝까지 수행한다.
- 맵 재배정·층 이동으로도 점검 상태·메모·그룹은 유지된다.

로봇의 한 줄 상태(`RobotView.display_status`): `working` 수행 중 / `idle` 대기 / `charging` 충전 / `manual` 수동 / `service` 점검 / `error` 오류 / `offline` 미접속.

## 실패 작업 재시도

작업 이력의 "재시도" 또는 `POST /api/v1/sites/{site}/jobs/{id}/retry`. 종료된 작업의 명령을 복사해 새 pending 작업을 만든다 (`retry_of` 에 원본).

- 좌표는 **지금 목적지**로 다시 잡는다. 원본이 게시판 작업이면 새 작업도 게시판에 오른다.
- 시스템 작업(자동 충전·대기 복귀)은 409 — 조건이 되면 다시 만들어진다.

## 이력

모든 조작과 상태 변화는 `{FMS_DATA_ROOT}/events.db` (sqlite3)에 쌓인다. **이력** 화면 또는 `GET /api/v1/events?site=&robot=&job=&kind=&severity=&since=&until=&after_id=&limit=`.

- `kind` 는 쉼표로 여러 개, `job.` 처럼 점으로 끝나면 접두어다.
- 로봇 State 는 변화만 남긴다 (상태·연결·일시정지·모드·위치 추정·오류·배터리 10 % 단위). 상태별 시간과 주행 거리는 1분마다 `robot.usage` 로 남는다.
- 운영자 조작에는 `actor` 가 남는다: 인증을 켰으면 API 키 이름, 아니면 요청 헤더 `X-FMS-Actor` 또는 `operator`.
- `FMS_EVENT_RETENTION_DAYS`(기본 30)가 지난 이벤트는 하루 한 번 지운다.

| 접두어 | 내용 |
|---|---|
| `job.` | 배정·시작·완료·실패·취소·재시도·우선순위·마감 초과·선점 |
| `robot.` | 상태 변화, 명령, 점검, 사용량 |
| `traffic.` | 대기·예약·해제·교착·정체·회수·경로 교차 예고 |
| `elevator.` | 임대·반납·장애·예비 전환·확인 필요·복구 |
| `site.`, `map.`, `zone.`, `schedule.` | 운영 조작 |
| `charge.`, `staging.` | 자동 충전·대기 복귀 |
| `alert.`, `webhook.`, `auth.` | 알림·연동 |

## 알림

**알림** 화면 또는 `GET /api/v1/alerts?site=&open=`, `POST /api/v1/alerts/{id}/ack`. 같은 대상의 알림은 열린 것이 하나뿐이다.

| 규칙 | 조건 | 닫힘 |
|---|---|---|
| `robot_error` | ERROR·EMERGENCY 또는 FATAL 오류 | 해소되면 자동 |
| `robot_offline` | LOST·STALE 30초 이상 | 자동 |
| `battery_critical` | 충전 정책의 `critical` 미만이고 충전 중 아님 | 자동 |
| `job_pending` | 게시판 작업이 10분 넘게 미배정 | 자동 |
| `deadlock` | 교착(대기 순환) | 자동 |
| `elevator_stale` / `elevator_fault` / `elevator_attention` | 승강기 상태 끊김 / 장애 / 칸 안 로봇 확인 필요 | 자동 |
| `job_failed` / `deadline_missed` | 작업 실패 / 마감 초과 | 운영자 확인(ack) |

실시간은 WebSocket `GET /api/v1/events/stream?site=` — 기록된 이벤트를 그대로 흘린다 (`robot.usage` 제외). 알림은 `alert.opened` / `alert.resolved` / `alert.acked` 이벤트로 온다.

## 통계

**통계** 화면 또는 `GET /api/v1/sites/{site}/kpi?from=&to=&bucket=hour|day` (unix 초. 기본은 hour 24시간, day 14일).

`totals`, `buckets[]`, `robots[]` 에 같은 지표가 들어 있다: 완료·실패·취소 수와 성공률, 배정 대기(게시 → 배정)·수행 시간(배정 → 완료) 평균, 상태별 로봇 시간과 가동률(접속 시간 중 수행 비율), 주행 거리, 충전 횟수·시간, 교통·승강기 대기, 교착·정체 수.

- 시스템 작업은 작업 수에서 빼고 `system_jobs` 로 따로 센다.
- 이벤트 보존 기간보다 긴 구간은 집계할 수 없다.

## 외부 연동

**외부 주문**: `POST /api/v1/sites/{site}/jobs` `{commands, auto_assign: true, external_id, priority, not_before, deadline, robot_group}`. 같은 `external_id` 로 다시 요청하면 새로 만들지 않고 기존 작업을 돌려준다 (멱등).

**Webhook**: 현장 설정 화면 또는 `/api/v1/webhooks` (GET·POST, `/{id}` PUT·DELETE). 이벤트를 JSON 그대로 POST 한다.

- `kinds` 가 비면 `job.` 과 `alert.` 를 보낸다. `robot.usage` 는 보내지 않는다.
- `secret` 을 주면 `X-FMS-Signature: sha256=<본문의 HMAC-SHA256 hex>` 헤더를 붙인다. 수신 쪽에서 같은 방법으로 계산해 비교한다.
- 2xx 가 아니면 2, 4, 8 … 초(최대 300초) 간격으로 8번까지 다시 보내고 버린다 (`webhook.dropped` 이벤트). 대기열은 `events.db` 에 있어 재시작해도 남는다.
- 같은 이벤트가 두 번 갈 수 있다. 수신 쪽은 이벤트 `id` 로 중복을 거른다.

## 인증

기본은 꺼져 있다. `FMS_AUTH=1` 로 켜면 REST·WebSocket 에 API 키가 필요하다 (`Authorization: Bearer <키>`, WebSocket·이미지는 `?api_key=`).

| 역할 | 할 수 있는 것 |
|---|---|
| `viewer` | 조회 (GET) |
| `operator` | viewer + 운영 조작 (작업·정지·대피·구역·목적지 등) |
| `admin` | operator + API 키·webhook 관리, 현장 삭제 |

- 키는 `{FMS_DATA_ROOT}/auth.json` 에 sha256 해시로만 저장한다. 원문은 발급 응답에서 한 번만 볼 수 있다.
- 켰는데 키가 없으면 시작할 때 admin 키를 만든다. `FMS_ADMIN_KEY` 가 있으면 그 값을 쓰고, 없으면 무작위 키를 서버 로그에 한 번 찍는다.
- 키 관리: 현장 설정 화면 또는 `/api/v1/auth/keys` (GET·POST, `/{name}` DELETE). 마지막 admin 키는 지울 수 없다. `GET /api/v1/auth/me` 는 키 없이도 열려 있다.
- 열린 경로: `/`, `/static`, `/health`, `/docs`.
- 로봇은 API 키가 없다. 맵 번들(`.../maps/{map}/bundle`)은 `FMS_ROBOT_TOKEN` 을 정하면 `?token=` 이 맞아야 내려주고(FMS 가 SET_MAP URL 에 붙인다), 정하지 않으면 인증 없이 열어 둔다.
- 웹 UI 는 키를 브라우저 localStorage 에 둔다. 공용 PC 에서는 쓰고 나서 "API 키 바꾸기"로 지운다.
- 브로커(MQTT) 인증은 FMS 소관이 아니다. AMR-simulator `broker/mosquitto.conf` 에서 설정한다. 키를 평문 HTTP 로 보내지 않도록 FMS 앞에 TLS 를 둔다.
