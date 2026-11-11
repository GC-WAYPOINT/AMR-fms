"""현장별 작업 프리셋. 게시/직접 실행 시 작업에 명령과 좌표의 스냅샷을 복사한다."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from uuid import uuid4

from fms_server.task import JobError, JobNotFound, _now_iso, _parse_command, resolve_steps


class PresetStore:
    def __init__(self, data_root):
        self.root = Path(data_root) / 'sites'
        self._lock = threading.RLock()

    def _path(self, site):
        from fms_server.task import _check_seg
        _check_seg(site, 'site_id')
        return self.root / site / 'presets.json'

    def list(self, site):
        with self._lock:
            path = self._path(site)
            return json.loads(path.read_text()) if path.is_file() else []

    def get(self, site, preset_id):
        for p in self.list(site):
            if p['preset_id'] == preset_id:
                return p
        raise JobNotFound('프리셋을 찾을 수 없습니다')

    def save(self, site, name, commands, registry, preset_id=None):
        if not name.strip() or len(name) > 128:
            raise JobError('프리셋 이름은 1~128자여야 합니다')
        if not commands:
            raise JobError('프리셋에 단계를 하나 이상 추가하세요')
        parsed = [_parse_command(c) for c in commands]
        resolve_steps(parsed, site, registry)
        with self._lock:
            rows = self.list(site)
            previous = self.get(site,preset_id) if preset_id else None
            now = _now_iso()
            result = dict(preset_id=preset_id or uuid4().hex[:16], name=name.strip(),
                          commands=[c.to_dict() for c in parsed],
                          created_at=previous['created_at'] if previous else now, updated_at=now)
            rows = [p for p in rows if p['preset_id'] != result['preset_id']] + [result]
            self._write(site,rows)
            return copy.deepcopy(result)

    def delete(self, site, preset_id):
        with self._lock:
            self.get(site,preset_id)
            self._write(site,[p for p in self.list(site) if p['preset_id'] != preset_id])

    def _write(self, site, rows):
        path = self._path(site)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n')
        temp.replace(path)


def publish(store, site_id, preset_id, *, registry, jobs, coordinator, robot_id=None, priority=0,
            robot_group=None, not_before=None, deadline=None):
    """프리셋을 작업으로 게시한다. 로봇을 지정하면 바로 배정하고, 아니면 게시판(자동 배정)에 올린다.

    명령·좌표는 이 시점의 스냅샷이다. API 게시와 예약 실행(`scheduler.py`)이 같이 쓴다.
    """
    from fms_server.task import Job, compile_to_order
    preset = store.get(site_id, preset_id)
    commands = [_parse_command(c) for c in preset['commands']]
    resolved = resolve_steps(commands, site_id, registry)
    # 충전기 연결과 맵 일치도 게시 전에 검사한다. 직접 실행은 dispatch에서 가용성도 검사한다.
    robots = registry.get_site(site_id).robots
    candidate = robot_id or next((r.robot_id for r in robots.values()
                                  if r.map_id == resolved[0]['map_id']), None)
    start = registry.assignment_for(candidate) if candidate else None
    if start and {r['map_id'] for r in resolved} != {start.map_id}:
        # 여러 층: 승강기 연결·문 방향·충전기를 미리 확인한다
        from fms_server.route import RouteError, build_plan
        try:
            build_plan(commands, resolved, start.map_id, site_id, coordinator._elevators(), registry=registry)
        except RouteError as exc:
            raise JobError(str(exc)) from None
    elif candidate:
        compile_to_order(Job('validate', site_id, commands, robot_id=candidate, resolved_steps=resolved), registry)
    elif any(c.kind == 'docking' for c in commands):
        from common.schemas import Pose
        from fms_server.mapkit.chargers import require_charger
        for c, step in zip(commands, resolved):
            if c.kind == 'docking':
                require_charger(registry.map_store(site_id), step['map_id'], Pose(**step['pose']), c.params['entry'])
    job = jobs.create(site_id, preset['commands'], robot_id, auto_assign=not bool(robot_id),
                      preset_name=preset['name'], resolved_steps=resolved, priority=priority,
                      robot_group=robot_group, not_before=not_before, deadline=deadline)
    if robot_id:
        try:
            coordinator.dispatch(job.job_id)
        except JobError:
            jobs.delete(job.job_id)
            raise
    return job
