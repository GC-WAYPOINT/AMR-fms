"""
작업 묶음 — 프리셋에서 명령과 좌표를 복사하고 MQTT Order로 실행한다.

단계: move(target), wait(duration, 직전 위치), docking(target, params), parking(target, params).
목적지는 같은 현장이어야 하고 여러 층이면 승강기로 이동한다(route.py). 게시판 작업(auto_assign)만 FMS 배정 대상이다.
상태: pending → assigned(로봇 예약/수신 대기) → working → success/failed.
실행 중 취소는 canceling으로 예약을 유지하고 새 로봇 상태로 취소를 확인한다.
수신 여부가 불명확한 작업은 자동 재게시하지 않는다 (중복 실행 방지).
JobStore는 sites/{site_id}/jobs.json에 스냅샷·상태를 저장한다.
"""

from __future__ import annotations

import json
import copy
import math
from functools import wraps
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from common.schemas import (
    Action, ActionType, ActionStatus, Pose, LocalizationState,
    ErrorLevel,
    InstantActionType,
    Order,
    OrderNode,
    RobotState,
    State,
)
from fms_server.fleet import Fleet, FleetEvent
from fms_server.locations import LocationError, LocationNotFound
from fms_server.mqtt_client import FmsMqttClient, send_instant
from fms_server.sites import SiteRegistry

log = logging.getLogger("fms.task")

JOBS_FILE = "jobs.json"
# 다층 작업 stage 오더를 보낸 뒤 로봇 State 에 order_id 가 보일 때까지 기다리는 시간.
# 로봇은 수신 즉시 State 를 보내지만, 큰 맵 경로계획 중 연결 지연까지 넉넉히 둔다.
ORDER_ACK_TIMEOUT = 60.0
# 호출 단계(탑승 전)에서 이만큼 기다리면 임대를 반납하고 다른 승강기로 다시 계획한다. 후보가 없으면 계속 기다린다.
ELEVATOR_WAIT_TIMEOUT = 120.0


class JobError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class RouteBlocked(JobError):
    """점유 존 경로를 만들 수 없음. robot_specific=False 면 어느 로봇을 보내도 같은 이유로 실패한다."""

    def __init__(self, message, robot_specific=True):
        super().__init__(message)
        self.robot_specific = robot_specific


ROUTE_RETRY_SECONDS = 30.0   # 경로를 못 만든 게시물은 맵이 바뀌지 않으면 이 시간 동안 자동 배정을 다시 시도하지 않는다


class JobNotFound(JobError):
    """job 을 못 찾음 (HTTP 404 급)."""


class JobConflict(JobError):
    """지금 상태로는 할 수 없음 (HTTP 409 급)."""


class JobStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    WORKING = "working"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELING = "canceling"
    CANCELED = "canceled"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value, name: str = "시각") -> Optional[str]:
    """ISO 8601 문자열(또는 None)을 검증해 UTC ISO 로 돌려준다. 시간대가 없으면 UTC 로 본다."""
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise JobError(f"{name} 은 ISO 8601 시각이어야 한다: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _epoch(iso: Optional[str]) -> Optional[float]:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _check_seg(value: str, kind: str) -> None:
    if not value or "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise JobError(f"허용되지 않는 {kind}: {value!r}")


# =============================================================================
# 명령
# =============================================================================

@dataclass
class MoveCommand:
    """named location 하나로 이동."""

    target: str  # location 이름 (site 스코프)

    kind: str = "move"

    def to_dict(self) -> dict:
        return {"type": "move", "target": self.target}


@dataclass
class ActionCommand:
    kind: str
    target: Optional[str] = None
    duration: float = 0.0
    params: dict = field(default_factory=dict)

    def to_dict(self):
        data = {"type": self.kind}
        if self.target is not None:
            data["target"] = self.target
        if self.kind in ("wait", "pickup", "drop"):
            data["duration"] = self.duration
            if self.params:
                data["params"] = dict(self.params)
        else:
            data["params"] = dict(self.params)
        return data


def _parse_command(entry: dict):
    kind = entry.get("type")
    target = entry.get("target")
    if kind not in ("move", "wait", "docking", "parking", "pickup", "drop"):
        raise JobError(f"알 수 없는 명령 type: {kind!r}")
    if kind != "wait" and (not isinstance(target, str) or not target.strip()):
        raise JobError(f"{kind} 명령에 target(location 이름)이 없다")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise JobError("target은 비어 있지 않은 목적지 이름이어야 한다")
    if kind == "move":
        return MoveCommand(target=target)
    if kind in ("wait", "pickup", "drop"):
        duration = entry.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise JobError("대기 시간은 0보다 큰 유한한 초 단위 숫자여야 한다")
        if kind == "wait":
            return ActionCommand(kind, target, duration=float(duration))
        params = entry.get("params", {})
        if not isinstance(params, dict) or set(params) - {"payload_id"}:
            raise JobError("적재·하차 params에는 payload_id만 지정할 수 있다")
        if params and (not isinstance(params["payload_id"], str) or not params["payload_id"].strip()):
            raise JobError("payload_id는 비어 있지 않은 문자열이어야 한다")
        return ActionCommand(kind, target, duration=float(duration), params=dict(params))
    params = entry.get("params", {})
    if not isinstance(params, dict):
        raise JobError("도킹/주차 params는 객체여야 한다")
    for key, value in params.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            raise JobError(f"params.{key}는 유한한 숫자여야 한다")
    try:
        Action(action_id="validate", action_type=ActionType.DOCK if kind == "docking" else ActionType.PARK, params=params)
    except ValueError as exc:
        raise JobError(str(exc)) from None
    return ActionCommand(kind, target, params=copy.deepcopy(params))


def resolve_steps(commands, site_id, registry):
    """작업 생성 시 목적지의 좌표·맵을 복사한다. wait는 직전 위치를 사용한다."""
    locations = registry.location_store(site_id)
    resolved = []
    previous = None
    for command in commands:
        if command.target:
            try:
                map_id, pose = locations.resolve_pose(command.target)
            except (LocationError, LocationNotFound) as exc:
                raise JobError(str(exc)) from None
            previous = (map_id, pose)
        elif previous is None:
            raise JobError("첫 대기 단계에는 목적지가 필요하다. 먼저 이동 단계를 추가하세요.")
        map_id, pose = previous
        resolved.append({"map_id": map_id, "pose": pose.model_dump()})
    return resolved  # 목적지가 여러 층이면 build_plan 이 승강기 단계를 끼운다


# =============================================================================
# Job
# =============================================================================

@dataclass
class Job:
    job_id: str
    site_id: str
    commands: list[MoveCommand | ActionCommand]
    status: JobStatus = JobStatus.PENDING
    robot_id: Optional[str] = None

    auto_assign: bool = False
    preset_name: Optional[str] = None
    resolved_steps: list[dict] = field(default_factory=list)
    order_snapshot: Optional[dict] = None
    traffic: dict = field(default_factory=dict)  # 대기·출구 삽입과 원래 명령의 대응

    # 다층 job: route.build_plan 결과. 비어 있으면 단일 Order(이전 방식).
    plan: list[dict] = field(default_factory=list)
    stage: int = 0
    stage_data: dict = field(default_factory=dict)  # 지금 stage 의 실행 상태 (stage 가 바뀌면 지운다)

    order_id: Optional[str] = None
    order_update_id: int = 0
    done_commands: int = 0        # 완료로 간주한 명령 수 (진행률 표시용)
    acknowledged: bool = False    # 로봇이 우리 order_id 를 한 번이라도 보고했나
    error: Optional[str] = None

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    dispatched_at: Optional[str] = None

    # 운영 기능 (없던 필드는 from_dict 기본값으로 읽는다)
    priority: int = 0                    # 클수록 먼저 배정
    not_before: Optional[str] = None     # 이 시각(ISO) 전에는 배정하지 않는다
    deadline: Optional[str] = None       # 마감(ISO). 넘겨도 실패 처리하지 않고 이벤트만 남긴다
    deadline_missed: bool = False
    robot_group: Optional[str] = None    # 이 그룹 로봇에만 배정
    system: Optional[str] = None         # 시스템 작업: "charge" | "staging" | "evacuation" | "recover"
    retry_of: Optional[str] = None       # 재시도 원본 job_id
    external_id: Optional[str] = None    # 외부 주문 멱등 키
    reroutes: int = 0                    # 막힌 통로 때문에 오더를 다시 만든 횟수 (order_id 접미사 -rN)
    resume_from: int = 0                 # 재경로 뒤 오더가 이어받는 첫 명령 번호 (앞 명령은 이미 끝났다)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "commands": [c.to_dict() for c in self.commands],
            "status": self.status.value,
            "auto_assign": self.auto_assign, "preset_name": self.preset_name,
            "resolved_steps": self.resolved_steps, "order_snapshot": self.order_snapshot,
            "traffic": self.traffic,
            "plan": self.plan, "stage": self.stage, "stage_data": self.stage_data,
            "robot_id": self.robot_id,
            "order_id": self.order_id,
            "order_update_id": self.order_update_id,
            "done_commands": self.done_commands,
            "acknowledged": self.acknowledged,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dispatched_at": self.dispatched_at,
            "priority": self.priority, "not_before": self.not_before, "deadline": self.deadline,
            "deadline_missed": self.deadline_missed, "robot_group": self.robot_group,
            "system": self.system, "retry_of": self.retry_of, "external_id": self.external_id,
            "reroutes": self.reroutes, "resume_from": self.resume_from,
        }

    @classmethod
    def from_dict(cls, job_id: str, data: dict) -> "Job":
        return cls(
            job_id=job_id,
            site_id=data["site_id"],
            commands=[_parse_command(e) for e in data.get("commands", [])],
            status=JobStatus(data.get("status", "pending")),
            auto_assign=data.get("auto_assign", False), preset_name=data.get("preset_name"),
            resolved_steps=data.get("resolved_steps", []), order_snapshot=data.get("order_snapshot"),
            traffic=data.get("traffic", {}),
            plan=data.get("plan", []), stage=data.get("stage", 0), stage_data=data.get("stage_data", {}),
            robot_id=data.get("robot_id"),
            order_id=data.get("order_id"),
            order_update_id=data.get("order_update_id", 0),
            done_commands=data.get("done_commands", 0),
            acknowledged=data.get("acknowledged", False),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            dispatched_at=data.get("dispatched_at"),
            priority=int(data.get("priority", 0)), not_before=data.get("not_before"),
            deadline=data.get("deadline"), deadline_missed=bool(data.get("deadline_missed", False)),
            robot_group=data.get("robot_group"), system=data.get("system"),
            retry_of=data.get("retry_of"), external_id=data.get("external_id"),
            reroutes=int(data.get("reroutes", 0)), resume_from=int(data.get("resume_from", 0)),
        )


# =============================================================================
# JobStore — 영속화 + 메모리 인덱스
# =============================================================================

class JobStore:
    def __init__(self, data_root: Path | str) -> None:
        self._lock = threading.RLock()
        self._sites_root = Path(data_root) / "sites"
        self._jobs: dict[str, Job] = {}
        self._seq: dict[str, int] = {}   # site_id -> 마지막으로 쓴 번호
        self._load_all()

    # -- 영속화 ----------------------------------------------------------

    def _jobs_path(self, site_id: str) -> Path:
        _check_seg(site_id, "site_id")
        return self._sites_root / site_id / JOBS_FILE

    def _load_all(self) -> None:
        if not self._sites_root.is_dir():
            return
        for site_dir in sorted(p for p in self._sites_root.iterdir() if p.is_dir()):
            path = site_dir / JOBS_FILE
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                log.error("jobs.json 로드 실패 %s: %s", site_dir.name, exc)
                continue
            self._seq[site_dir.name] = int(data.get("seq", 0))
            for job_id, entry in (data.get("jobs") or {}).items():
                try:
                    self._jobs[job_id] = Job.from_dict(job_id, entry)
                except (KeyError, ValueError, JobError) as exc:
                    log.error("job %r 엔트리 무시: %s", job_id, exc)
        if self._jobs:
            log.info("job %d개 로드", len(self._jobs))

    def _persist_site(self, site_id: str) -> None:
        path = self._jobs_path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        jobs = {j.job_id: j.to_dict() for j in self._jobs.values() if j.site_id == site_id}
        payload = {"seq": self._seq.get(site_id, 0), "jobs": jobs}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def save(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist_site(job.site_id)

    # -- CRUD ----------------------------------------------------------

    def create(
        self, site_id: str, commands: list[dict], robot_id: Optional[str] = None,
        *, auto_assign=False, preset_name=None, resolved_steps=None,
        priority: int = 0, not_before=None, deadline=None, robot_group=None, system=None,
        retry_of=None, external_id=None,
    ) -> Job:
        _check_seg(site_id, "site_id")
        if not commands:
            raise JobError("job 에 명령이 하나도 없다")
        parsed = [_parse_command(e) for e in commands]
        if robot_id is not None:
            _check_seg(robot_id, "robot_id")
        not_before, deadline = parse_time(not_before, "not_before"), parse_time(deadline, "deadline")
        with self._lock:
            n = self._seq.get(site_id, 0) + 1
            self._seq[site_id] = n
            job_id = f"{site_id}-J{n:04d}"
            job = Job(job_id=job_id, site_id=site_id, commands=parsed, robot_id=robot_id,
                      auto_assign=auto_assign, preset_name=preset_name,
                      resolved_steps=copy.deepcopy(resolved_steps or []),
                      priority=int(priority), not_before=not_before, deadline=deadline,
                      robot_group=robot_group or None, system=system, retry_of=retry_of,
                      external_id=external_id)
            self._jobs[job_id] = job
            self._persist_site(site_id)
        log.info("job 생성: %s (명령 %d개, 로봇 %s)", job_id, len(parsed), robot_id or "-")
        return job

    def create_system(self, site_id: str, robot_id: str, plan: list[dict], system: str, label: str) -> Job:
        """목적지 명령 없이 stage 계획만 가진 시스템 작업 (승강기 칸 내 복구)."""
        _check_seg(site_id, "site_id")
        _check_seg(robot_id, "robot_id")
        with self._lock:
            n = self._seq.get(site_id, 0) + 1
            self._seq[site_id] = n
            job = Job(job_id=f"{site_id}-J{n:04d}", site_id=site_id, commands=[], robot_id=robot_id,
                      plan=copy.deepcopy(plan), system=system, preset_name=label)
            self._jobs[job.job_id] = job
            self._persist_site(site_id)
        return job

    def by_external_id(self, site_id: str, external_id: str) -> Optional[Job]:
        with self._lock:
            return next((j for j in self._jobs.values()
                         if j.site_id == site_id and j.external_id == external_id), None)

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"job {job_id!r} 없음")
        return job

    def list(self, site_id: Optional[str] = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if site_id is not None:
            jobs = [j for j in jobs if j.site_id == site_id]
        return sorted(jobs, key=lambda j: j.job_id)

    def jobs_for_robot(self, robot_id: str, *, status: Optional[JobStatus] = None) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.robot_id == robot_id]
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def delete(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"job {job_id!r} 없음")
            if job.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING):
                raise JobError(f"job {job_id} 는 수행 중이다 — 먼저 취소해라")
            del self._jobs[job_id]
            self._persist_site(job.site_id)
        log.info("job 삭제: %s", job_id)


# =============================================================================
# Order 컴파일
# =============================================================================

def compile_to_order(job: Job, registry: SiteRegistry) -> Order:
    """
    작업 묶음의 이동/대기/도킹/주차 단계를 `Order`로 만든다. 모든 목적지는 같은 맵이어야 하고,
    그 맵은 배정 로봇의 배정 맵과 일치해야 한다. 여기서는 원본 노드를 만들고,
    dispatch에서 traffic.prepare가 대기·출구와 연속 통행 허가를 적용한다.
    """
    if job.robot_id is None:
        raise JobError("배정된 로봇이 없다")
    if not job.commands:
        raise JobError("명령이 없다")

    resolved = job.resolved_steps or resolve_steps(job.commands, job.site_id, registry)
    target_map = resolved[0]["map_id"]

    assignment = registry.assignment_for(job.robot_id)
    if assignment is None:
        raise JobError(f"로봇 {job.robot_id} 가 어느 site 에도 등록돼 있지 않다")
    if assignment.site_id != job.site_id:
        raise JobError(f"로봇 {job.robot_id} 는 다른 site({assignment.site_id})에 등록돼 있다")
    if assignment.map_id != target_map:
        raise JobError(
            f"로봇 {job.robot_id} 배정 맵은 {assignment.map_id!r} 인데 "
            f"job 목적지는 {target_map!r} 맵이다"
        )

    order_id = job.order_id or job.job_id
    nodes = []
    for i, (command, step) in enumerate(zip(job.commands, resolved, strict=True)):
        if i < job.resume_from:
            continue   # 재경로: 이미 끝낸 명령은 새 오더에 넣지 않는다
        action = None
        if isinstance(command, ActionCommand):
            kind = {"wait": ActionType.WAIT, "docking": ActionType.DOCK, "parking": ActionType.PARK,
                    "pickup": ActionType.PICK, "drop": ActionType.DROP}[command.kind]
            action = Action(action_id=f"{order_id}-a{i}", action_type=kind,
                            payload_id=command.params.get("payload_id"),
                            duration=command.duration, params=command.params)
            if command.kind == "docking":
                from fms_server.mapkit.chargers import require_charger
                require_charger(registry.map_store(job.site_id), target_map,
                                Pose(**step["pose"]), command.params["entry"])
        nodes.append(OrderNode(node_id=f"{order_id}-n{i}", sequence_id=len(nodes),
                               released=True, position=Pose(**step["pose"]), action=action))

    return Order(
        header_id=0,
        timestamp=time.time(),
        robot_id=job.robot_id,
        order_id=order_id,
        order_update_id=job.order_update_id or 1,
        task_id=job.job_id,
        nodes=nodes,
    )


# =============================================================================
# JobCoordinator — 디스패치 + 로봇 보고로 상태 되짚기
# =============================================================================

def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class JobCoordinator:
    """
    job 을 로봇에 내려보내고(`dispatch`), 로봇 state 를 보고 job 상태를 옮긴다.
    fleet 리스너는 paho 스레드에서 불린다 — 여기서 하는 건 메모리 갱신 + 파일
    쓰기뿐이라 블로킹은 없다.
    """

    def __init__(
        self,
        fleet: Fleet,
        registry: SiteRegistry,
        store: JobStore,
        mqtt: FmsMqttClient,
        *,
        elevators=None,          # ElevatorStore 또는 그걸 돌려주는 callable (다층 job 용)
        elevator_ctl=None,       # ElevatorController (다층 job 용)
        door_ctl=None,           # DoorController (자동문 존 통과용)
        site_coordinator=None,   # SiteCoordinator: 층을 옮긴 로봇에 바로 SET_MAP 을 보낸다 (없으면 다음 state 때)
        events=None,             # EventLog (없으면 기록하지 않는다)
    ) -> None:
        self._fleet = fleet
        self._registry = registry
        # 경로 실패 게시물 -> (다시 시도할 시각, 그때의 맵 버전). 경로 계산은 락 안에서 수백 ms 걸려
        # 매초 되풀이하면 MQTT State 처리(실시간 맵)가 몇 초씩 멈춘다.
        self._route_backoff = {}
        self._store = store
        self._mqtt = mqtt
        self._elevators_src = elevators
        self._ctl = elevator_ctl
        self._door_ctl = door_ctl
        self._site_coordinator = site_coordinator
        self._events = events
        self.battery = None      # BatteryManager: 충전 정책(min_job_charge)·비켜주기. 없으면 이전 동작
        self._unsubscribe = None
        self._lock = threading.RLock()
        from fms_server.traffic import TrafficController
        self._traffic = TrafficController(registry, fleet, store, events=events, doors=door_ctl)

    @property
    def events(self):
        return self._events

    def _event(self, kind: str, job: Optional["Job"] = None, severity: str = "info", **data) -> None:
        """이벤트 로그에 남긴다 (큐에 넣기만 한다). 로그가 없으면 아무것도 안 한다."""
        if self._events is None:
            return
        if job is not None:
            data.setdefault("site_id", job.site_id)
            data.setdefault("robot_id", job.robot_id)
            data.setdefault("job_id", job.job_id)
            if job.preset_name:
                data.setdefault("preset", job.preset_name)
            if job.system:
                data.setdefault("system", job.system)       # KPI 는 시스템 작업을 따로 센다
        self._events.record(kind, severity=severity, **data)

    def _assign_snapshot(self, job: Job, state: State) -> dict:
        """배정 순간 FMS 가 알던 값 (AI 학습 특징 원본·누출 검사용). 교통 상태 + 최근 3분 게시 수·등록 대수."""
        now = time.time()
        snap = self.traffic.assign_snapshot(job, state, now)
        site_jobs = self._store.list(job.site_id)
        snap["jobs_posted_3m"] = sum(1 for j in site_jobs if (_epoch(j.created_at) or 0) >= now - 180)
        snap["pending"] = sum(1 for j in site_jobs if j.status == JobStatus.PENDING and j.job_id != job.job_id)
        try:
            snap["robots"] = len(self._registry.get_site(job.site_id).robots)
        except Exception:
            snap["robots"] = None
        return snap

    @staticmethod
    def _since(iso: Optional[str]) -> Optional[float]:
        start = _epoch(iso) if iso else None
        return round(time.time() - start, 2) if start else None

    @property
    def traffic(self):
        # 테스트·내장 실행에서 저장소를 교체해도 다른 데이터 루트의 예약을 섞지 않는다.
        if (self._traffic.registry is not self._registry or self._traffic.fleet is not self._fleet
                or self._traffic.jobs is not self._store):
            from fms_server.traffic import TrafficController
            self._traffic = TrafficController(self._registry, self._fleet, self._store, events=self._events,
                                            doors=self._door_ctl)
        return self._traffic

    @property
    def lock(self):
        """작업 상태 변경은 모두 이 락 안에서 한다."""
        return self._lock

    # -- 수명 주기 ----------------------------------------------------

    def start(self) -> None:
        self._unsubscribe = self._fleet.add_listener(self._on_fleet_event)
        self._reconcile_elevator_leases()
        log.info("job 코디네이터 시작")

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _ctl_do(self, name: str, *args, default=None):
        """승강기 제어기의 선택 기능 호출 (테스트용 가짜 제어기에는 없을 수 있다)."""
        method = getattr(self._ctl, name, None) if self._ctl is not None else None
        return method(*args) if method is not None else default

    def _reconcile_elevator_leases(self) -> None:
        """재시작 때 복원한 임대 중 주인 작업이 없거나 이미 끝난 것은 놓는다 ("확인 필요"는 유지)."""
        with self._lock:
            for eid, owner in (self._ctl_do("all_leases", default={}) or {}).items():
                if self._ctl_do("attention", eid):
                    continue
                try:
                    alive = not self._store.get(owner).status.terminal
                except JobNotFound:
                    alive = False
                if not alive:
                    log.warning("승강기 %s 임대 주인 %s 이(가) 없다 — 임대 해제", eid, owner)
                    self._ctl.release_lease(eid, owner)

    def in_car(self, job: Job) -> Optional[str]:
        """다층 작업의 로봇이 승강기 칸 안(탑승 오더를 보낸 뒤 ~ 임대 반납 전)이면 그 승강기 id."""
        if not job.plan or self._ctl is None or job.stage >= len(job.plan):
            return None
        for index, stage in enumerate(job.plan):
            if stage["t"] != "call" or self._ctl.lease_owner(stage["elevator_id"]) != job.job_id:
                continue
            eid = stage["elevator_id"]
            release = next((i for i in range(index + 1, len(job.plan))
                            if job.plan[i]["t"] == "release" and job.plan[i]["elevator_id"] == eid), len(job.plan))
            if job.stage <= index or job.stage > release:
                continue
            if job.stage == index + 1 and "sent_at" not in job.stage_data:
                continue   # 탑승 오더를 아직 안 보냈다 — 승강장 밖
            return eid
        return None

    # -- 디스패치 (API 핸들러) --------------------------------------

    @_serialized
    def dispatch(self, job_id: str, robot_id: Optional[str] = None) -> Job:
        if not getattr(self._mqtt, "connected", True):
            raise JobError("MQTT 연결을 기다리는 중입니다")
        job = self._store.get(job_id)
        if job.status != JobStatus.PENDING:
            raise JobError(f"job {job_id} 는 {job.status.value} 상태다 — pending 만 디스패치 가능")

        target_robot = robot_id or job.robot_id
        if not target_robot:
            raise JobError("배정할 로봇이 없다 (robot_id 를 줘라)")

        if any(j.robot_id == target_robot and j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)
               for j in self._store.list()):
            raise JobError(f"로봇 {target_robot}는 이미 다른 작업에 배정되어 있다")

        record = self._fleet.get(target_robot)
        if record is None or record.state is None:
            raise JobError(f"로봇 {target_robot} 의 state 를 아직 못 받았다")
        if record.state.battery.charge <= 0:
            raise JobError("배터리가 소진된 로봇에는 작업을 배정할 수 없다")
        policy = self._policy(job.site_id)
        if (policy and policy["enabled"] and job.system != "charge"
                and record.state.battery.charge < policy["min_job_charge"]):
            raise JobError(f"배터리 {record.state.battery.charge:.0f}% — 작업 수락 기준 "
                           f"{policy['min_job_charge']}% 미만 (충전 대기)")
        if not record.available_for_job(self._fleet.stale_after):
            raise JobError(
                f"로봇 {target_robot} 가 작업 수락 불가 상태다 ({record.state.display_state})"
            )

        assignment = self._registry.assignment_for(target_robot)
        if assignment is None or assignment.site_id != job.site_id:
            raise JobError(f"로봇 {target_robot} 가 site {job.site_id} 에 등록돼 있지 않다")
        # 점검 중인 로봇은 새 작업을 받지 않는다.
        if not assignment.in_service:
            raise JobError(f"로봇 {target_robot} 는 점검 중이다 (서비스 제외)")

        # 맵 버전 게이팅 — 오래된 금지구역 들고 도는 로봇에 job 안 준다
        try:
            map_store = self._registry.map_store(assignment.site_id)
            active_version = map_store.active_version(assignment.map_id)
        except Exception:
            active_version = None
        if not active_version:
            raise JobError(f"배정 맵 {assignment.map_id} 에 활성 버전이 없다")
        if record.state.map_id != map_store.map_uid(assignment.map_id):
            raise JobError("로봇이 보고한 맵 ID가 배정 맵과 다르다")
        if record.state.map_version != active_version:
            raise JobError(
                f"로봇 {target_robot} 맵 버전({record.state.map_version})이 "
                f"배정 맵 활성 버전({active_version})과 다르다 — SET_MAP 동기화 대기"
            )

        resolved = job.resolved_steps or resolve_steps(job.commands, job.site_id, self._registry)
        if {r["map_id"] for r in resolved} != {assignment.map_id}:
            return self._dispatch_multi(job, target_robot, assignment.map_id, resolved)

        previous_robot = job.robot_id
        job.robot_id = target_robot
        # 막힌 통로로 오더를 다시 만들 때는 order_id 를 바꾼다 (같은 id 를 다시 보내면 로봇이 끝난 오더로 무시한다).
        job.order_id = f"{job.job_id}-r{job.reroutes}" if job.reroutes else job.job_id
        job.order_update_id = 1
        job.acknowledged = False
        job.done_commands = job.resume_from
        job.error = None

        try:
            order = compile_to_order(job, self._registry)
            from fms_server.traffic import TrafficError
            try:
                order = self.traffic.prepare(job, order, assignment.map_id, record.state)
            except TrafficError as exc:
                raise RouteBlocked(str(exc), getattr(exc, "robot_specific", True)) from None
        except JobError:
            job.robot_id = previous_robot
            raise
        job.order_snapshot = order.model_dump(mode="json")
        snapshot = self._assign_snapshot(job, record.state)   # 이 작업이 예약을 잡기 전 상태
        update = self.traffic.update(job)
        if update is not None:
            order = update
            job.order_update_id = order.order_update_id
            job.order_snapshot = order.model_dump(mode="json")
        job.status = JobStatus.ASSIGNED
        job.dispatched_at = _now_iso()
        job.touch()
        self._store.save(job)
        try:
            self._mqtt.publish_order(order)
        except Exception as exc:
            # 전송 성공 여부가 불명확하면 재배정하지 않고 예약 상태를 유지한다.
            job.error = f"오더 전송 확인 필요: {exc}"
            self._store.save(job)
        log.info("job 디스패치: %s -> %s (노드 %d개)", job_id, target_robot, len(order.nodes))
        self._event("job.assigned", job, nodes=len(order.nodes), waited=self._since(job.created_at),
                    reroutes=job.reroutes, snapshot=snapshot)
        return job

    # -- 다층 job ----------------------------------------------------

    def _elevators(self):
        src = self._elevators_src
        return src() if callable(src) else src

    def _dispatch_multi(self, job: Job, robot_id: str, start_map: str, resolved: list[dict]) -> Job:
        from fms_server.route import RouteError, build_plan
        elevators = self._elevators()
        if elevators is None or self._ctl is None:
            raise JobError("다층 이동에는 승강기 설정과 제어기가 필요하다")
        try:
            rec = self._fleet.get(robot_id)
            pose = rec.state.pose if rec and rec.state else None
            busy = frozenset(e for e in (st.get("elevator_id") for j in self._store.list() if j.plan and not j.status.terminal
                                         and j.job_id != job.job_id for st in j.plan) if e)
            plan = build_plan(job.commands, resolved, start_map, job.site_id, elevators, registry=self._registry,
                              start_xy=(pose.x, pose.y) if pose else None, avoid=busy,
                              unavailable=self._ctl_do("unavailable", default=frozenset()))
        except RouteError as exc:
            raise JobError(str(exc)) from None
        job.robot_id = robot_id
        job.resolved_steps = resolved
        job.plan, job.stage, job.stage_data = plan, 0, {}
        job.order_id = job.job_id
        job.order_update_id = 1
        job.acknowledged = False
        job.done_commands = 0
        job.error = None
        job.status = JobStatus.ASSIGNED
        job.dispatched_at = _now_iso()
        job.touch()
        self._store.save(job)
        log.info("다층 job 디스패치: %s -> %s (stage %d개)", job.job_id, robot_id, len(plan))
        self._event("job.assigned", job, stages=len(plan), waited=self._since(job.created_at),
                    snapshot=self._assign_snapshot(job, rec.state) if rec and rec.state else None)
        self._step_multi(job)
        return job

    @_serialized
    def tick_multifloor(self) -> None:
        """진행 중인 다층 job 을 한 걸음씩. 로봇/승강기 이벤트가 없어도 시간 초과를 보게 주기적으로 부른다."""
        for job in self._store.list():
            if job.plan and job.status in (JobStatus.ASSIGNED, JobStatus.WORKING):
                try:
                    self._step_multi(job)
                except Exception:
                    log.exception("다층 job %s 진행 중 오류", job.job_id)
                    self._fail_multi(job, "내부 오류 (서버 로그 확인)")

    def on_elevator_state(self, _car) -> None:
        self.tick_multifloor()

    def _fail_multi(self, job: Job, message: str) -> None:
        self._finish(job, JobStatus.FAILED, message)

    def _release_elevators(self, job: Job) -> None:
        """job 이 잡고 있는 승강기를 놓는다 (실패/취소 때)."""
        if self._ctl is None:
            return
        inside = self.in_car(job) if job.status != JobStatus.SUCCESS else None
        self._ctl_do("forget", job.job_id)
        for st in job.plan:
            eid = st.get("elevator_id")
            if eid and self._ctl.lease_owner(eid) == job.job_id:
                if eid == inside and hasattr(self._ctl, "mark_attention"):
                    # 로봇이 칸 안에 남았을 수 있다. 임대와 문 열림(hold)을 유지하고 운영자 확인을 기다린다.
                    if not self._ctl.attention(eid):
                        self._ctl.mark_attention(eid, job.job_id, job.robot_id,
                                                 f"작업 {job.job_id} {'취소' if job.status == JobStatus.CANCELED else '실패'} — 로봇이 칸 안")
                    continue
                self._ctl.release(eid)
                self._ctl.release_lease(eid, job.job_id)

    def _advance_stage(self, job: Job) -> bool:
        if job.stage < len(job.plan) and job.plan[job.stage]['t'] == 'order':
            # 끝난 오더의 허가를 호출/탑승/맵 전환 단계에서 다시 획득하지 않는다.
            self.traffic.end_wait(job, "stage")
            job.traffic = {}
        job.stage += 1
        job.stage_data = {}
        if job.stage >= len(job.plan):
            job.done_commands = len(job.commands)
            self._finish(job, JobStatus.SUCCESS, None)
            return False
        job.touch()
        self._store.save(job)
        return True

    def _step_multi(self, job: Job) -> None:
        for _ in range(12):  # 한 번에 여러 stage 가 끝날 수 있다 (release 등은 바로 끝난다)
            if job.status not in (JobStatus.ASSIGNED, JobStatus.WORKING) or job.stage >= len(job.plan):
                return
            record = self._fleet.get(job.robot_id)
            if record is None or record.state is None:
                return
            fatal = [e for e in record.state.errors if e.level == ErrorLevel.FATAL]
            if fatal:
                self._fail_multi(job, "; ".join(e.error_type for e in fatal))
                return
            if not self._run_stage(job, job.plan[job.stage], record):
                return

    def _run_stage(self, job: Job, stage: dict, record) -> bool:
        """stage 하나를 진행. 끝나서 다음으로 넘어갔으면 True."""
        now = time.time()
        sd = job.stage_data
        state = record.state
        kind = stage["t"]
        sd.setdefault("t0", now)

        if kind == "order":
            oid = f"{job.job_id}-s{job.stage}"
            if "sent_at" not in sd:
                store = self._registry.map_store(job.site_id)
                if not state.is_available or state.map_id != store.map_uid(stage["map_id"]):
                    return False
                order = self._stage_order(job, stage, oid)
                from fms_server.traffic import TrafficError
                try:
                    order = self.traffic.prepare(job, order, stage["map_id"], state)
                except TrafficError as exc:
                    self._fail_multi(job, str(exc))
                    return False
                job.order_id, job.order_update_id = oid, 1
                job.order_snapshot = order.model_dump(mode="json")
                update = self.traffic.update(job)
                if update is not None:
                    order = update
                    job.order_update_id = order.order_update_id
                    job.order_snapshot = order.model_dump(mode="json")
                sd["sent_at"] = now
                job.touch()
                self._store.save(job)
                try:
                    self._mqtt.publish_order(order)
                except Exception as exc:
                    self._fail_multi(job, f"오더 전송 실패: {exc}")
                return False
            if state.order_id == oid:
                if not sd.get("ack"):
                    sd["ack"] = True
                    if not job.acknowledged:
                        self._event("job.working", job)
                    job.status = JobStatus.WORKING
                    job.acknowledged = True
                self._stage_progress(job, state)
                return False
            if sd.get("ack"):
                if state.order_id is not None:
                    self._fail_multi(job, "로봇이 다른 오더로 전환됨")
                elif state.state == RobotState.ERROR:
                    self._fail_multi(job, "로봇이 ERROR 상태")
                elif state.state in (RobotState.IDLE, RobotState.CHARGING):
                    for n in stage["nodes"]:
                        if n["cmd"] is not None:
                            job.done_commands = max(job.done_commands, n["cmd"] + 1)
                    return self._advance_stage(job)
                return False
            if now - sd["sent_at"] > ORDER_ACK_TIMEOUT:
                self._fail_multi(job, f"로봇이 오더를 받지 않았다 ({ORDER_ACK_TIMEOUT:.0f}초)")
            return False

        ctl = self._ctl
        if ctl is None:
            self._fail_multi(job, "엘리베이터 제어기가 없다")
            return False

        if kind == "call":
            eid, map_id = stage["elevator_id"], stage["map_id"]
            sd.setdefault("wait0", now)
            car_now = ctl.state(eid)
            broken = (car_now is not None and not getattr(car_now, "in_service", True)) or bool(self._ctl_do("attention", eid))
            if broken or now - sd["wait0"] > ELEVATOR_WAIT_TIMEOUT:
                why = "장애" if broken else f"호출 대기 {ELEVATOR_WAIT_TIMEOUT:.0f}초 초과"
                if self._reroute(job, eid, why):
                    return True
                sd["wait0"] = now     # 예비 승강기가 없다 — 계속 기다리고 알린다
                if broken:
                    msg = f"승강기 {eid} 장애 — 예비 승강기 없음, 복구 대기"
                    if job.error != msg:
                        job.error = msg
                        self._store.save(job)
                    sd["t0"] = now
                    return False
            ride = next((st for st in job.plan[job.stage+1:] if st["t"] == "ride"), None)
            if ride and not self.traffic.acquire_floor(job, ride["to_map"], future=True):
                job.error = "도착 층 동시 이동 대수 제한 — 탑승 전 대기"
                sd["t0"] = now
                self._store.save(job)
                return False
            if ctl.lease_owner(eid) != job.job_id and self._registry.get_site(job.site_id).evacuating:
                msg = "대피 중 — 승강기 신규 임대 금지"
                if job.error != msg:
                    job.error = msg
                    self._store.save(job)
                sd["t0"] = now
                return False
            if not ctl.acquire(eid, job.job_id):
                msg = f"승강기 {eid} 를 다른 작업({ctl.lease_owner(eid)})이 쓰는 중"
                if job.error != msg:
                    job.error = msg
                    self._store.save(job)
                sd["t0"] = now
                return False
            if job.error:
                job.error = None
            car = ctl.state(eid)
            if car is None:
                if now - sd["t0"] > 60:
                    self._fail_multi(job, f"승강기 {eid} 제어기 응답이 없다")
                return False
            if car.open_at(map_id) and car.held and sd.get("called"):
                return self._advance_stage(job)
            if not sd.get("called") or (now - sd["called_at"] > 8 and car.target != map_id and car.at != map_id):
                ctl.call(eid, map_id, hold=True)
                sd["called"], sd["called_at"] = True, now
                self._store.save(job)
            elif now - sd["t0"] > 180:
                self._fail_multi(job, f"승강기 {eid} 가 {map_id} 에 오지 않았다")
            return False

        if kind == "ride":
            eid, to_map = stage["elevator_id"], stage["to_map"]
            ctl.acquire(eid, job.job_id)
            car = ctl.state(eid)
            if not sd.get("go"):
                if car is None:
                    return False
                ctl.go(eid, to_map, hold=True)
                ctl.release(eid)  # 출발 층 문 붙잡기를 푼다 (GO 가 먼저 큐에 들어갔다)
                sd["go"], sd["go_at"] = True, now
                self._store.save(job)
                return False
            if car is not None and car.open_at(to_map) and car.held:
                return self._advance_stage(job)
            if now - sd["go_at"] > 300:
                self._fail_multi(job, f"승강기 {eid} 가 {to_map} 에 도착하지 않았다")
            return False

        if kind == "switch":
            # 하차를 마쳤다 (아직 출발 층 맵). 지금 자세를 기록해 두고 배정 맵을 바꾼다 -> SET_MAP
            store = self._registry.map_store(job.site_id)
            to_map = stage["to_map"]
            if not sd.get("switched"):
                nxt = job.plan[job.stage + 1] if job.stage + 1 < len(job.plan) else None
                if nxt is not None and nxt["t"] == "localize":
                    nxt["robot_pose"] = [state.pose.x, state.pose.y, state.pose.theta]
                self._registry.register_robot(job.site_id, job.robot_id, to_map)
                if self._site_coordinator is not None:
                    self._site_coordinator.notify_robot(job.robot_id)
                sd["switched"] = True
                self._store.save(job)
            if state.map_id == store.map_uid(to_map) and state.map_version == store.active_version(to_map):
                return self._advance_stage(job)
            if now - sd["t0"] > 180:
                self._fail_multi(job, f"로봇이 맵 {to_map} 를 받지 못했다")
            return False

        if kind == "localize":
            store = self._registry.map_store(job.site_id)
            uid, version = store.map_uid(stage["map_id"]), store.active_version(stage["map_id"])
            if state.map_id != uid or state.map_version != version:
                if now - sd["t0"] > 180:
                    self._fail_multi(job, f"로봇이 맵 {stage['map_id']} 를 받지 못했다")
                return False
            pose = {"x": stage["x"], "y": stage["y"], "theta": stage["theta"]}  # 시스템이 아는 자세 = 힌트
            if "tp_at" not in sd:
                # 시뮬: 출발 층 맵 위에서 하차한 몸을 도착 층 같은 자리로 옮긴다 (하차 오차는 그대로 옮긴다)
                from common.elevator_proto import topic_sim_teleport
                from fms_server.route import transfer_pose
                body = pose
                if stage.get("robot_pose") and stage.get("ref_from") and stage.get("ref_to"):
                    x, y, th = transfer_pose(stage["robot_pose"], stage["ref_from"], stage["ref_to"])
                    body = {"x": x, "y": y, "theta": th}
                ctl.publish_json(topic_sim_teleport(job.robot_id), body | {"map_id": uid})
                sd["tp_at"] = now
                self._store.save(job)
                return False
            if "hint_at" not in sd:
                if now - sd["tp_at"] < 0.6:
                    return False
                send_instant(self._mqtt, job.robot_id, InstantActionType.INIT_POSITION, pose | {"map_id": uid})
                sd["hint_at"] = now
                sd["tries"] = sd.get("tries", 0) + 1
                self._store.save(job)
                return False
            elapsed = now - sd["hint_at"]
            if record.last_state_at and record.last_state_at > sd["hint_at"] + 1.3:
                if state.localization == LocalizationState.LOCALIZED:
                    return self._advance_stage(job)
                if state.localization == LocalizationState.UNLOCALIZED or elapsed > 10:
                    if sd["tries"] >= 3:
                        self._fail_multi(job, "새 맵에서 로컬라이징 실패 (힌트 3회)")
                    else:
                        del sd["hint_at"]
            return False

        if kind == "release":
            eid = stage["elevator_id"]
            ctl.release(eid)
            ctl.release_lease(eid, job.job_id)
            return self._advance_stage(job)

        self._fail_multi(job, f"알 수 없는 stage {kind!r}")
        return False

    def _reroute(self, job: Job, eid: str, why: str) -> bool:
        """탑승 전(호출 단계)인 작업을 다른 승강기로 돌린다. 계획을 바꿨으면 True."""
        from fms_server.route import RouteError, build_plan
        elevators = self._elevators()
        stage = job.plan[job.stage]
        record = self._fleet.get(job.robot_id)
        if elevators is None or record is None or record.state is None:
            return False
        done = job.done_commands
        commands = job.commands[done:]
        resolved = job.resolved_steps[done:]
        if not commands:
            return False
        busy = frozenset(e for e in (st.get("elevator_id") for j in self._store.list() if j.plan and not j.status.terminal
                                     and j.job_id != job.job_id for st in j.plan) if e)
        unavailable = frozenset(self._ctl_do("unavailable", default=frozenset())) | {eid}
        try:
            plan = build_plan(commands, resolved, stage["map_id"], job.site_id, elevators, registry=self._registry,
                              start_xy=(record.state.pose.x, record.state.pose.y), avoid=busy, unavailable=unavailable)
        except RouteError:
            if not job.stage_data.get("no_backup"):
                job.stage_data["no_backup"] = True
                self._event("elevator.reroute", job, "warning", elevator_id=eid, reason=why, result="예비 승강기 없음")
            return False
        for st in plan:   # 잘라낸 명령 기준 번호를 원래 번호로 되돌린다
            for node in st.get("nodes", []):
                if node.get("cmd") is not None:
                    node["cmd"] += done
        if self._ctl.lease_owner(eid) == job.job_id:
            self._ctl.release(eid)
            self._ctl.release_lease(eid, job.job_id)
        self._ctl_do("forget", job.job_id)
        job.plan = job.plan[:job.stage] + plan
        job.stage_data = {}
        job.error = None
        job.touch()
        self._store.save(job)
        new = next((st["elevator_id"] for st in plan if st["t"] == "call"), None)
        self._event("elevator.reroute", job, "warning", elevator_id=eid, to=new, reason=why)
        log.info("job %s 승강기 %s -> %s 전환 (%s)", job.job_id, eid, new, why)
        return True

    # -- 승강기 칸 내 복구 (운영자) ------------------------------------

    @_serialized
    def recover_elevator(self, site_id: str, elevator_id: str, robot_id: Optional[str], action: str,
                         *, actor: Optional[str] = None) -> dict:
        """'확인 필요' 승강기를 푼다.

        - release: 칸이 비었음을 운영자가 확인했다. 문 열림을 풀고 임대를 놓는다.
        - exit: 지금 층 문이 열려 있을 때 로봇에 후진 하차 STRAIGHT 오더를 보낸다. 칸이 다른 층에 서 있으면
          하차 뒤 그 층 맵으로 바꾸고 위치를 다시 잡는다. 끝나면 임대를 놓는다.
        """
        from fms_server.route import ELEVATOR_STRAIGHT_SPEED, _node
        ctl = self._ctl
        if ctl is None or not hasattr(ctl, "attention"):
            raise JobError("엘리베이터 제어기가 없다")
        attention = ctl.attention(elevator_id)
        if attention is None:
            raise JobConflict(f"승강기 {elevator_id} 는 확인 필요 상태가 아니다")
        owner = ctl.lease_owner(elevator_id)
        robot_id = robot_id or attention.get("robot_id")
        if action == "release":
            ctl.release(elevator_id)
            if owner:
                ctl.release_lease(elevator_id, owner)
            ctl.clear_attention(elevator_id)
            self._event("elevator.recover", site_id=site_id, robot_id=robot_id, elevator_id=elevator_id,
                        action="release", actor=actor)
            return {"elevator_id": elevator_id, "action": "release", "job_id": None}
        if action != "exit":
            raise JobError("action 은 exit 또는 release")
        if not robot_id:
            raise JobError("하차시킬 로봇을 지정하세요")
        elevators = self._elevators()
        elevator = elevators.get(site_id, elevator_id)
        assignment = self._registry.assignment_for(robot_id)
        record = self._fleet.get(robot_id)
        if assignment is None or assignment.site_id != site_id or record is None or record.state is None:
            raise JobError(f"로봇 {robot_id} 의 상태를 알 수 없다")
        if not record.available_for_job(self._fleet.stale_after):
            raise JobConflict(f"로봇 {robot_id} 가 오더를 받을 수 없는 상태다 ({record.state.display_state})")
        if any(j.robot_id == robot_id and j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)
               for j in self._store.list()):
            raise JobConflict(f"로봇 {robot_id} 의 이전 작업이 아직 끝나지 않았다")
        car = ctl.state(elevator_id)
        if car is None or car.at is None or car.door != "OPEN":
            raise JobConflict("승강기 문이 열려 있지 않다 — 문이 열린 층에서만 하차시킬 수 있다")
        stops = {s.map_id: s for s in elevator.stops}
        if assignment.map_id not in stops or car.at not in stops:
            raise JobError("로봇의 맵 또는 승강기가 선 층이 이 승강기의 정지 층이 아니다")
        here = elevators.geometry(site_id, stops[assignment.map_id])
        there = elevators.geometry(site_id, stops[car.at])
        pose = record.state.pose
        plan = [{"t": "order", "map_id": assignment.map_id, "label": f"승강기 {elevator_id} 하차 (후진, 복구)", "nodes": [
            _node((pose.x, pose.y, here.inside[2]), action={
                "type": "straight", "params": {"distance": -there.distance, "speed": ELEVATOR_STRAIGHT_SPEED}})]},
            {"t": "release", "elevator_id": elevator_id}]
        if car.at != assignment.map_id:
            plan += [{"t": "switch", "from_map": assignment.map_id, "to_map": car.at},
                     {"t": "localize", "map_id": car.at, "x": there.outside[0], "y": there.outside[1],
                      "theta": there.outside[2], "ref_from": list(here.outside), "ref_to": list(there.outside)}]
        job = self._store.create_system(site_id, robot_id, plan, "recover", "승강기 하차 복구")
        if owner and not ctl.transfer(elevator_id, owner, job.job_id):
            self._store.delete(job.job_id)
            raise JobConflict("승강기 임대를 넘겨받지 못했다")
        if not owner:
            ctl.clear_attention(elevator_id)
            ctl.acquire(elevator_id, job.job_id)
            ctl.mark_attention(elevator_id, job.job_id, robot_id, "하차 복구 중")
        job.order_id, job.order_update_id = job.job_id, 1
        job.status = JobStatus.ASSIGNED
        job.dispatched_at = _now_iso()
        job.touch()
        self._store.save(job)
        self._event("elevator.recover", job, elevator_id=elevator_id, action="exit", actor=actor, floor=car.at)
        self._step_multi(job)
        return {"elevator_id": elevator_id, "action": "exit", "job_id": job.job_id}

    def _stage_order(self, job: Job, stage: dict, oid: str) -> Order:
        kinds = {"wait": ActionType.WAIT, "docking": ActionType.DOCK, "parking": ActionType.PARK,
                 "pickup": ActionType.PICK, "drop": ActionType.DROP,
                 "straight": ActionType.STRAIGHT}
        nodes = []
        for i, n in enumerate(stage["nodes"]):
            action = None
            if n["action"]:
                a = n["action"]
                action = Action(action_id=f"{oid}-a{i}", action_type=kinds[a["type"]],
                                payload_id=a.get("params", {}).get("payload_id"),
                                duration=a.get("duration", 0.0), params=a.get("params", {}))
            nodes.append(OrderNode(node_id=f"{oid}-n{i}", sequence_id=i, released=True,
                                   position=Pose(x=n["x"], y=n["y"], theta=n["theta"]), action=action))
        return Order(header_id=0, timestamp=time.time(), robot_id=job.robot_id, order_id=oid,
                     order_update_id=1, task_id=job.job_id, nodes=nodes)

    def _stage_progress(self, job: Job, state: State) -> None:
        status = {a.action_id: a.status for a in state.action_states}
        stage = job.plan[job.stage]
        before = job.done_commands
        for node in (job.order_snapshot or {}).get("nodes", []):
            source = job.traffic.get("sources", {}).get(node["node_id"], node["sequence_id"])
            if job.traffic and node["node_id"] not in job.traffic["sources"]:
                continue
            src = stage["nodes"][source]
            if node.get("action"):
                if status.get(node["action"]["action_id"]) != ActionStatus.FINISHED:
                    break
            elif not (state.last_node_id == node["node_id"] or state.last_node_sequence_id > node["sequence_id"]):
                break
            if src["cmd"] is not None:
                job.done_commands = max(job.done_commands, src["cmd"] + 1)
        if job.done_commands != before:
            job.touch()
            self._store.save(job)

    @_serialized
    def cancel(self, job_id: str, *, actor: Optional[str] = None, reason: Optional[str] = None) -> Job:
        job = self._store.get(job_id)
        if job.status.terminal or job.status == JobStatus.CANCELING:
            raise JobError(f"job {job_id} 는 이미 종료됐다 ({job.status.value})")

        dispatched = job.status in (JobStatus.ASSIGNED, JobStatus.WORKING)
        if dispatched and job.robot_id:
            send_instant(self._mqtt, job.robot_id, InstantActionType.CANCEL_ORDER)

        job.status = JobStatus.CANCELING if dispatched else JobStatus.CANCELED
        job.touch()
        self._store.save(job)
        log.info("job 취소: %s", job_id)
        self._event("job.cancel_requested", job, actor=actor, reason=reason)
        if job.status == JobStatus.CANCELED:
            self._event("job.canceled", job, actor=actor)
        return job

    def _policy(self, site_id: str) -> Optional[dict]:
        return self.battery.policy(site_id) if self.battery is not None else None

    @_serialized
    def allocate_pending(self, now: Optional[float] = None):
        """게시판 배정. 작업은 우선순위(높은 것 먼저) → 게시 순, 로봇은 비용(거리·배터리) 순.

        전체 최적 할당은 하지 않는다 (우선순위 순 greedy). 충돌 검사는 dispatch에서 다시 한다.
        """
        if not getattr(self._mqtt, "connected", True):
            return
        self.tick_traffic()
        self.tick_multifloor()
        now = time.time() if now is None else now
        from fms_server.assign import rank_candidates
        pending = sorted((j for j in self._store.list()
                          if j.status == JobStatus.PENDING and j.auto_assign),
                         key=lambda j: (-j.priority, j.created_at, j.job_id))
        for job in pending:
            deadline = _epoch(job.deadline)
            if deadline is not None and now > deadline and not job.deadline_missed:
                # 마감을 넘겨도 실패 처리하지 않는다. 이벤트·알림만 남긴다.
                job.deadline_missed = True
                self._store.save(job)
                self._event("job.deadline_missed", job, "warning", deadline=job.deadline)
            start = _epoch(job.not_before)
            if start is not None and now < start:
                reason = "예약 시각 전"
                if job.error != reason:
                    job.error = reason
                    self._store.save(job)
                continue
            reason = "같은 현장·맵의 가용 로봇을 기다리는 중"
            try:
                site = self._registry.get_site(job.site_id)
            except Exception:
                continue
            versions = self._site_versions(job.site_id)
            backoff = self._route_backoff.get(job.job_id)
            if backoff is not None and now < backoff[0] and backoff[1] == versions:
                continue   # 지난번 경로 실패 이유(job.error)를 그대로 둔다
            self._route_backoff.pop(job.job_id, None)
            blocked = site.allocation_blocked
            if blocked:
                if job.error != blocked:
                    job.error = blocked
                    self._store.save(job)
                continue
            records = []
            for record in self._fleet.available_robots():
                if job.robot_id and record.robot_id != job.robot_id:
                    continue
                assignment = site.robots.get(record.robot_id)
                if assignment is None:
                    continue
                if site.paused_for(assignment.map_id):
                    reason = "현장 정지 중"
                    continue
                records.append(record)
            try:
                order, why = rank_candidates(job, records, self._registry, self._elevators(),
                                             self._policy(job.site_id))
            except Exception:
                log.exception("후보 순위 계산 실패 %s — 로봇 ID 순으로 대체", job.job_id)
                order, why = sorted(r.robot_id for r in records), None
            if why and not order:
                reason = why
            dispatched = False
            for robot_id in order:
                try:
                    self.dispatch(job.job_id, robot_id)
                    dispatched = True
                    break
                except RouteBlocked as exc:
                    reason = str(exc)
                    self._route_backoff[job.job_id] = (now + ROUTE_RETRY_SECONDS, versions)
                    if not exc.robot_specific:
                        break   # 다른 로봇도 같은 구간에서 실패한다
                except JobError as exc:
                    reason = str(exc)
            if dispatched:
                self._route_backoff.pop(job.job_id, None)
            else:
                self._preempt_staging(job, site)
                if job.error != reason:
                    job.error = reason
                    self._store.save(job)

    def _site_versions(self, site_id):
        """현장 맵들의 활성 버전. 바뀌면(구역 편집 등) 경로 실패 대기를 풀고 바로 다시 시도한다."""
        try:
            store = self._registry.map_store(site_id)
            return tuple(sorted((m.map_id, store.active_version(m.map_id)) for m in store.list_maps()))
        except Exception:
            return None

    def _preempt_staging(self, job: Job, site) -> None:
        """일반 작업 후보가 없고 대기 위치로 가는 중인 로봇만 있으면 그 시스템 작업을 취소한다.

        취소 확인(CANCELED) 뒤 다음 tick 에 이 작업이 배정된다. 한 번에 한 대만 선점한다.
        """
        if job.system:
            return
        staging = [j for j in self._store.list(job.site_id)
                   if j.system == "staging" and j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)]
        if any(j.status == JobStatus.CANCELING for j in staging):
            return   # 이미 선점 중 — 취소 확인을 기다린다
        for candidate in sorted(staging, key=lambda j: j.job_id):
            assignment = site.robots.get(candidate.robot_id)
            if assignment is None or not assignment.in_service:
                continue
            if job.robot_id and job.robot_id != candidate.robot_id:
                continue
            if job.robot_group and job.robot_group not in assignment.groups:
                continue
            try:
                self.cancel(candidate.job_id, actor="system", reason=f"선점: {job.job_id}")
            except JobError:
                continue
            self._event("job.preempted", candidate, by=job.job_id)
            return

    @_serialized
    def tick_safety(self, now: Optional[float] = None) -> dict:
        """교착·정체 감지, 안전한 예약 회수, 유휴 로봇 비켜주기, 경로 교차 예고. 배정 루프가 1초마다 부른다."""
        now = time.time() if now is None else now
        traffic = self.traffic
        analysis = traffic.analyze(now, self._ctl)
        traffic.obstacles.update(now)
        self.tick_reroute(now)
        if analysis["deadlocks"]:
            traffic.revoke_safe(analysis)
        # 길을 막고 선 유휴 로봇을 대기 위치로 보낸다 (자동 충전·대기 관리자가 있을 때만).
        requests, traffic.aside_requests = traffic.aside_requests, set()
        if self.battery is not None:
            for site_id, robot_id in sorted(requests):
                try:
                    self.battery.move_aside(site_id, robot_id)
                except Exception:
                    log.exception("비켜주기 실패 %s", robot_id)
        # 경로 교차 예고 (실험). 자동 일시정지는 맵 옵션 conflict_pause 가 켜져 있을 때만.
        if not getattr(self._mqtt, "connected", True):
            return analysis
        for conflict in traffic.path_conflicts(now):
            a, b = conflict["robots"]
            jobs = {rid: next((j for j in self._store.jobs_for_robot(rid)
                               if j.status in (JobStatus.ASSIGNED, JobStatus.WORKING)), None) for rid in (a, b)}
            assignment = self._registry.assignment_for(a)
            if assignment is None:
                continue
            if conflict["new"]:
                self._event("traffic.conflict", severity="warning", site_id=assignment.site_id,
                            robots=[a, b], distance=conflict["distance"])
            entry = traffic._conflicts[(a, b)]
            if entry["paused"] or not traffic.options(assignment.site_id, assignment.map_id)["conflict_pause"]:
                continue
            # 우선순위 낮은 쪽(같으면 나중에 배정된 쪽)을 잠깐 세운다.
            def rank(rid):
                job = jobs[rid]
                return (job.priority if job else -1000, -(_epoch(job.dispatched_at) or 0) if job else 0, rid)
            loser = min((a, b), key=rank)
            record = self._fleet.get(loser)
            if record is None or record.state is None or record.state.paused:
                continue
            send_instant(self._mqtt, loser, InstantActionType.PAUSE)
            entry["paused"] = loser
            self._event("traffic.conflict_pause", site_id=assignment.site_id, robot_id=loser, robots=[a, b])
        for (a, b), entry in traffic.cleared_conflicts(now):
            loser = entry.get("paused")
            if not loser:
                continue
            record = self._fleet.get(loser)
            # 그사이 운영자·현장 정지가 건 일시정지는 풀지 않는다.
            assignment = self._registry.assignment_for(loser)
            held = assignment is not None and self._registry.get_site(assignment.site_id).paused_for(assignment.map_id)
            if (record is not None and record.state is not None and record.state.paused and not held
                    and record.state.pause_source is not None and record.state.pause_source.value == "FMS"):
                send_instant(self._mqtt, loser, InstantActionType.RESUME)
        return analysis

    @_serialized
    def delete(self, job_id):
        if any(v['job'] == job_id for v in self.traffic.data['leases'].values()):
            raise JobError("교통 예약이 남은 작업은 삭제할 수 없습니다. 로봇의 정지·퇴장을 확인하세요.")
        self._store.delete(job_id)

    # -- fleet 이벤트 (paho 스레드) --------------------------------

    @_serialized
    def _on_fleet_event(self, event: FleetEvent) -> None:
        if event.kind != "state":
            return
        working = [j for j in self._store.jobs_for_robot(event.robot_id)
                   if j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)]
        for job in working:
            if job.plan and job.status != JobStatus.CANCELING:
                self._step_multi(job)
            else:
                self._advance(job, event.record.state)
        self.tick_traffic()

    @_serialized
    def tick_traffic(self):
        """주기 루프와 State 수신이 같은 락 아래서 허가를 갱신한다."""
        self.traffic.reconcile()
        if not getattr(self._mqtt, "connected", True):
            return
        for job in sorted(self._store.list(), key=lambda j: (j.dispatched_at or j.created_at, j.job_id)):
            if not job.traffic or job.status not in (JobStatus.ASSIGNED, JobStatus.WORKING):
                continue
            if job.traffic.get("rerouting"):
                continue   # 취소하고 다시 보내는 중이다. 낡은 오더에 허가를 더 내보내지 않는다
            if job.plan and (job.stage >= len(job.plan) or job.plan[job.stage]['t'] != 'order'):
                continue
            order = self.traffic.update(job)
            if order is not None:
                job.order_update_id = order.order_update_id
                job.order_snapshot = order.model_dump(mode="json")
                self._store.save(job)
                try:
                    self._mqtt.publish_order(order)
                except Exception as exc:
                    job.error = f"통행 허가 전송 확인 필요: {exc}"
                    self._store.save(job)

    # -- 막힌 통로 재경로 -----------------------------------------------

    @_serialized
    def tick_reroute(self, now: Optional[float] = None) -> None:
        """장애물에 막혀 서 있는 작업이 맵 전역 장애물을 피하는 다른 통로(자동문 등)로 갈 수 있으면 오더를 다시 만든다.

        로봇은 한 노드 구간 안에서만 우회하고 허가 없는 점유 존(자동문)은 벽으로 본다. 문을 지나는 길은 오더에
        진입·통과 노드 쌍을 넣어야 열리므로, FMS 가 장애물을 반영해 경로를 다시 고르고 오더를 취소 후 재전송한다.
        """
        now = time.time() if now is None else now
        if not getattr(self._mqtt, "connected", True):
            return
        from fms_server.config import settings
        from fms_server.traffic import radius as traffic_radius
        traffic = self.traffic
        for job in self._store.list():
            if job.status != JobStatus.WORKING or job.plan or not job.robot_id or not job.traffic:
                continue
            if job.traffic.get("rerouting") or job.reroutes >= settings.reroute_max:
                continue
            record = self._fleet.get(job.robot_id)
            if not traffic.fresh(record):
                continue
            s = record.state
            if (s.order_id != job.order_id or s.paused or s.state != RobotState.BUSY
                    or s.sub_state is None or s.sub_state.value != "WAITING"
                    or any(a.status == ActionStatus.RUNNING for a in s.action_states)):
                continue
            # 허가된 노드가 남아 있는데 서 있다 = FMS 가 세운 게 아니라 장애물에 막힌 것이다.
            if not any(n.released for n in s.node_states):
                continue
            motion = traffic._motion.get(job.robot_id)
            if not motion or not motion.get("waiting_since") or now - motion["waiting_since"] < settings.reroute_wait:
                continue
            if any(v["job"] == job.job_id and v["kind"] == "zone" for v in traffic.data["leases"].values()):
                continue   # 존 안에 있거나 진입 허가를 받았다. 퇴장 증거 없이 오더를 바꾸지 않는다
            if not traffic.obstacles.rects(job.site_id, job.traffic.get("map_id")):
                continue   # 등록된 장애물이 없으면 판단할 것이 없다 (등록되면 바로 판단하도록 쿨다운도 쓰지 않는다)
            # 그 장애물이 실제로 이 로봇의 길을 막고 있을 때만 (로봇끼리 막힘·허가 후 정지·다른 곳의 장애물 제외).
            blocking = traffic.obstacles.blocking(job.site_id, job.traffic.get("map_id"), s, traffic_radius(s),
                                                  self._paths_ahead(job, s))
            if not blocking:
                continue
            if now - traffic.reroute_tried.get(job.job_id, 0) < 60.0:
                continue
            traffic.reroute_tried[job.job_id] = now
            try:
                self._try_reroute(job, s, now, blocking)
            except Exception:
                log.exception("재경로 판단 실패 %s", job.job_id)

    @staticmethod
    def _paths_ahead(job: Job, state: State) -> list[list[tuple[float, float]]]:
        """로봇 앞 경로 후보: 로봇이 보고한 남은 경유점(local_path), 아직 도달하지 않은 오더 노드 순서."""
        start = (state.pose.x, state.pose.y)
        paths = []
        if state.local_path:
            paths.append([start] + [(p.x, p.y) for p in state.local_path])
        positions = {n["node_id"]: (n["position"]["x"], n["position"]["y"])
                     for n in (job.order_snapshot or {}).get("nodes", []) if n.get("position")}
        ahead = [positions[n.node_id] for n in sorted(state.node_states, key=lambda n: n.sequence_id)
                 if n.node_id in positions]
        if ahead:
            paths.append([start] + ahead)
        return paths

    def _try_reroute(self, job: Job, state: State, now: float, blocking=()) -> None:
        assignment = self._registry.assignment_for(job.robot_id)
        if assignment is None:
            return
        resolved = job.resolved_steps or resolve_steps(job.commands, job.site_id, self._registry)
        # 남은 명령: 로봇이 보고한 "아직 도달하지 않은 노드" 중 원래 명령에 해당하는 것의 첫 번호부터.
        sources = job.traffic.get("sources", {})
        remaining = [job.resume_from + sources[n.node_id] for n in state.node_states if n.node_id in sources]
        if not remaining:
            return
        done = min(remaining)
        positions = [Pose(**step["pose"]) for step in resolved[done:]]
        try:
            gates = self.traffic.preview_gates(job, assignment.map_id, state, positions, with_obstacles=True)
        except Exception as exc:   # 존 안에서 출발하는 경우 등 — 지금은 바꾸지 않는다
            log.info("재경로 판단 보류 %s: %s", job.job_id, exc)
            return
        current = [(c["zone"], None if c.get("automatic") else c["gate"])
                   for c in job.traffic.get("crossings", []) if not c["done"]]
        if gates == current:
            return   # 장애물을 피하는 다른 통로가 없다. 로봇이 스스로 기다리거나 돌아가거나 장애물이 풀리길 기다린다
        job.traffic["rerouting"] = {"at": now, "from": done, "gates": [list(g) for g in gates]}
        self._store.save(job)
        send_instant(self._mqtt, job.robot_id, InstantActionType.CANCEL_ORDER)
        log.info("재경로: %s 현재 %s -> %s", job.job_id, current, gates)
        self._event("traffic.reroute", job, "warning",
                    reason="맵 전역 장애물에 막힘 — 다른 통로로 오더를 다시 만든다",
                    from_gates=[list(g) for g in current], to_gates=[list(g) for g in gates],
                    obstacles=len(self.traffic.obstacles.rects(job.site_id, assignment.map_id)),
                    blocking=[list(b) for b in blocking])

    def _continue_reroute(self, job: Job, state: State) -> None:
        """취소가 확인되면 같은 작업을 새 order_id 로 다시 보낸다. 취소가 안 먹으면 포기하고 원래 오더를 이어 간다."""
        rr = job.traffic["rerouting"]
        now = time.time()
        if (state.order_id is None and state.timestamp >= rr["at"] and abs(state.velocity) < 0.01
                and state.state in (RobotState.IDLE, RobotState.CHARGING)):
            if now - rr["at"] > 90.0:
                job.traffic.pop("rerouting", None)
                self._finish(job, JobStatus.FAILED, "재경로 오더를 보내지 못했다")
                return
            self._reissue(job, rr)
        elif now - rr["at"] > 30.0 and state.order_id == job.order_id:
            job.traffic.pop("rerouting", None)
            self._store.save(job)
            self._event("traffic.reroute_abort", job, "warning", reason="오더 취소가 확인되지 않아 원래 오더를 유지한다")

    def _reissue(self, job: Job, rr: dict) -> None:
        saved = (job.order_id, job.done_commands, job.reroutes, job.resume_from, job.status)
        leases = self.traffic.data["leases"]
        # 낡은 오더의 대기 node·대기열 예약은 새 오더가 다시 잡는다. 점유 존 예약은 이 지점에 오기 전에 걸러진다.
        for key in [k for k, v in leases.items() if v["job"] == job.job_id and v["kind"] in ("wait", "queue")]:
            del leases[key]
        self.traffic.save()
        job.resume_from, job.reroutes, job.status = rr["from"], job.reroutes + 1, JobStatus.PENDING
        try:
            self.dispatch(job.job_id, job.robot_id)
        except JobError as exc:
            job.order_id, job.done_commands, job.reroutes, job.resume_from, job.status = saved
            log.debug("재경로 재전송 대기 %s: %s", job.job_id, exc)   # 다음 State 에서 다시 시도한다
            return
        self._event("traffic.reroute_done", job, order_id=job.order_id, resume_from=job.resume_from)

    def _advance(self, job: Job, state: Optional[State]) -> None:
        if state is None:
            return

        if job.status == JobStatus.CANCELING:
            # 취소 요청 이전의 상태로 예약을 풀지 않는다. 수신 확인 전에는 재배정하지 않는다.
            if (state.order_id is None and state.timestamp >= datetime.fromisoformat(job.updated_at).timestamp()
                    and state.state in (RobotState.IDLE, RobotState.CHARGING)):
                self._finish(job, JobStatus.CANCELED, None)
            return

        if job.traffic.get("rerouting"):
            self._continue_reroute(job, state)
            if job.traffic.get("rerouting"):
                return

        fatal = [e for e in state.errors if e.level == ErrorLevel.FATAL]
        if fatal:
            self._finish(job, JobStatus.FAILED, "; ".join(e.error_type for e in fatal))
            return

        if state.order_id == job.order_id:
            # 로봇이 우리 오더를 물었다. 진행률 갱신.
            first_ack = not job.acknowledged
            job.acknowledged = True
            job.status = JobStatus.WORKING
            done = 0
            action_status = {a.action_id: a.status for a in state.action_states}
            nodes = (job.order_snapshot or {}).get("nodes", [])
            # 새 오더의 첫 노드에 닿기 전에는 last_node_* 가 이전 오더의 값이다. 이번 오더의 노드일 때만 번호를 비교한다.
            reached_any = state.last_node_id in {n["node_id"] for n in nodes}
            for node in nodes:
                if node.get("action"):
                    if action_status.get(node["action"]["action_id"]) != ActionStatus.FINISHED:
                        break
                elif not (state.last_node_id == node["node_id"] or
                          (reached_any and state.last_node_sequence_id > node["sequence_id"])):
                    break
                if not job.traffic or node["node_id"] in job.traffic["sources"]:
                    done += 1
            if first_ack:
                self._store.save(job)
                self._event("job.working", job)
            done += job.resume_from
            if done != job.done_commands:
                job.done_commands = done
                job.touch()
                self._store.save(job)
            return

        if job.acknowledged:
            # 우리 order_id 를 봤었는데 지금은 비었다/딴 오더다.
            if state.order_id is not None:
                self._finish(job, JobStatus.FAILED, "로봇이 다른 오더로 전환됨")
            elif state.state == RobotState.ERROR:
                self._finish(job, JobStatus.FAILED, "로봇이 ERROR 상태")
            elif state.state in (RobotState.IDLE, RobotState.CHARGING):
                job.done_commands = len(job.commands)
                self._finish(job, JobStatus.SUCCESS, None)

    def _finish(self, job: Job, status: JobStatus, error: Optional[str]) -> None:
        job.status = status
        job.error = error
        self.traffic.end_wait(job, status.value)   # 대기 중 끝난 작업의 대기 구간을 닫는다
        if job.plan:
            self._release_elevators(job)
        job.touch()
        self._store.save(job)
        log.info("job %s -> %s%s", job.job_id, status.value, f" ({error})" if error else "")
        self._event(f"job.{status.value}", job,
                    "error" if status == JobStatus.FAILED else "info", error=error,
                    duration=self._since(job.dispatched_at),
                    done_commands=job.done_commands, total_commands=len(job.commands))
