"""이벤트 이력 조회.

    GET /api/v1/events?site=&robot=&job=&kind=&severity=&since=&until=&after_id=&limit=

`kind` 는 쉼표로 여러 개, `job.` 처럼 점으로 끝나면 접두어. `since`/`until` 은 unix 초.
기록은 각 구성요소가 `EventLog.record` 로 한다 (`../events.py`).
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/v1", tags=["events"])


def actor_of(request: Request) -> str:
    """운영자 조작의 주체. 인증이 켜져 있으면 API 키 이름, 아니면 요청 헤더 `X-FMS-Actor` 또는 'operator'."""
    key = getattr(request.state, "api_key", None)
    if key:
        return key["name"]
    return request.headers.get("X-FMS-Actor") or "operator"


def record(request: Request, kind: str, **fields) -> None:
    """API 계층의 운영자 조작 기록. 이벤트 로그가 없으면(구형 테스트) 아무것도 안 한다."""
    events = getattr(request.app.state, "events", None)
    if events is not None:
        fields.setdefault("actor", actor_of(request))
        events.record(kind, **fields)


@router.get("/events")
def list_events(request: Request, site: str | None = None, robot: str | None = None,
                job: str | None = None, kind: str | None = None, severity: str | None = None,
                since: float | None = None, until: float | None = None, after_id: int | None = None,
                limit: int = Query(default=200, ge=1, le=5000)):
    events = request.app.state.events
    rows = events.query(site=site, robot=robot, job=job, kind=kind, severity=severity,
                        since=since, until=until, after_id=after_id, limit=limit)
    return {"events": rows, "count": len(rows)}
