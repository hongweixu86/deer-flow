"""REST API for the schedule subsystem (Task 8).

Mounted at ``/api/schedules``. Wraps :class:`app.scheduling.service.ScheduleService`
and enforces the authorization matrix from spec §18.2:

* mutations (PATCH / DELETE / pause / resume) require the caller to be
  the schedule's ``owner_user_id``; otherwise 403.
* reads (GET detail / list) are 404-on-not-visible — never 403 — to avoid
  leaking the existence of a schedule to a non-owner non-subscriber.
* ``target.connection_id`` cross-user is 403 at create time.
* subscribe / unsubscribe accept any logged-in user.

The router reads its dependencies from ``app.state`` (mirrors the
production wiring in ``app.gateway.app.lifespan``) so the test fixture
can drop a stub ``ScheduleService`` in via ``app.state.schedule_service``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.scheduling.service import ScheduleService
from app.scheduling.lifespan import get_scheduler_service
from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository
from deerflow.persistence.engine import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/schedules", tags=["schedules"])


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class TargetPayload(BaseModel):
    channel: str = Field(..., description="Channel name; only 'feishu' in MVP")
    connection_id: str | None = Field(default=None, description="Optional connection id (must belong to caller)")
    chat_id: str = Field(..., description="Channel-specific chat id")


class ScheduleCreateRequest(BaseModel):
    kind: str = Field(..., description="'cron' or 'one_shot'")
    title: str
    prompt: str
    cron_expr: str | None = None
    run_at: datetime | None = None
    cron_tz: str | None = None
    thread_id: str | None = None
    target: TargetPayload
    source: str = Field(default="api")


class ScheduleUpdateRequest(BaseModel):
    title: str | None = None
    prompt: str | None = None
    cron_expr: str | None = None
    cron_tz: str | None = None
    target: TargetPayload | None = None


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_current_user_id(request: Request) -> str:
    """Resolve the current user's id from ``request.state``.

    The auth middleware (``app.gateway.auth_middleware``) stamps
    ``request.state.user`` and ``request.state.auth``; the router reads
    the user id from ``state.user.id``.
    """
    user = getattr(request.state, "user", None)
    if user is None or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(user.id)


def _get_service(request: Request) -> ScheduleService:
    # Prefer the lifespan-attached service; fall back to the module-level
    # singleton so test fixtures (which build a fresh FastAPI app) still
    # resolve the service without re-running the lifespan.
    svc = getattr(request.app.state, "schedule_service", None)
    if svc is None:
        scheduler = get_scheduler_service()
        if scheduler is not None:
            svc = scheduler.service
    if svc is None:
        raise HTTPException(status_code=503, detail="Schedule service unavailable")
    return svc


def _get_engine(request: Request):
    eng = getattr(request.app.state, "scheduler_engine", None)
    if eng is None:
        scheduler = get_scheduler_service()
        if scheduler is not None:
            eng = scheduler.engine
    return eng


def _get_connection_repo(request: Request):
    """Resolve (and lazily build) the ``ChannelConnectionRepository``.

    Mirrors the lazy-init pattern from
    ``app.gateway.routers.channel_connections._get_repository`` so a
    test fixture that never wired the repo can still create one on
    first use via ``get_session_factory()`` rather than 403'ing
    with "Connection registry unavailable".
    """
    repo = getattr(request.app.state, "channel_connection_repo", None)
    if isinstance(repo, ChannelConnectionRepository):
        return repo
    sf = get_session_factory()
    if sf is None:
        raise HTTPException(status_code=503, detail="Channel connection persistence is not available")
    repo = ChannelConnectionRepository(sf)
    request.app.state.channel_connection_repo = repo
    return repo


async def _verify_target_connection(target: TargetPayload, current_user_id: str, request: Request) -> None:
    """Enforce: ``target.connection_id`` (if set) must belong to the caller.

    Returns silently on success. Raises 403 on cross-user use.
    """
    if not target.connection_id:
        return
    repo = _get_connection_repo(request)
    if repo is None:
        # No connection registry wired in (e.g. test stub or future single-tenant mode).
        # Spec §18.2 still requires we don't accept unverified cross-user connection_ids;
        # the safest behavior is to reject.
        raise HTTPException(status_code=403, detail="Connection registry unavailable; cannot verify target.connection_id")

    # Best-effort lookup: the connection repo has either sync or async
    # `get_for_owner(connection_id, owner_user_id)`. We try a few common
    # signatures; on TypeError (wrong signature) we move to the next. Any
    # call that returns ``None`` (or whose coroutine resolves to ``None``)
    # is treated as "not visible to caller" and rejected with 403.
    # Best-effort lookup: the connection repo has either sync or async
    # `get_for_owner(connection_id, owner_user_id)`. The first signature
    # that resolves (sync return, awaited coroutine) determines the
    # outcome -- ``None`` means "not visible to caller" and is rejected
    # with 403; a non-None return is treated as "connection found for
    # this user" and accepted. We try only the ``get_for_owner`` shapes;
    # falling through to ``repo.get(...)`` (which would return any object
    # for the connection id) is unsafe and would silently bypass the
    # ownership check.
    for call in (
        lambda: repo.get_for_owner(target.connection_id, current_user_id),
        lambda: repo.get_for_owner(owner_user_id=current_user_id, connection_id=target.connection_id),
    ):
        try:
            res = call()
            if hasattr(res, "__await__"):
                res = await res
            # Whichever path returns, None means "not visible" -> 403.
            if res is None:
                raise HTTPException(status_code=403, detail="target.connection_id does not belong to caller")
            # Non-None: connection found for this user.
            return
        except TypeError:
            continue
    # No signature worked. Conservative: reject. The production channel
    # connection repo exposes ``get_for_owner``; tests inject a stub.
    raise HTTPException(status_code=403, detail="target.connection_id does not belong to caller")


def _schedule_to_dict(s) -> dict[str, Any]:
    """Serialise a Schedule ORM instance to the API response shape."""
    return {
        "id": s.id,
        "owner_user_id": s.owner_user_id,
        "title": s.title,
        "kind": s.kind.value if hasattr(s.kind, "value") else str(s.kind),
        "cron_expr": s.cron_expr,
        "run_at": s.run_at.isoformat() if s.run_at else None,
        "cron_tz": s.cron_tz,
        "prompt": s.prompt,
        "thread_id": s.thread_id,
        "target_json": s.target_json,
        "status": s.status.value if hasattr(s.status, "value") else str(s.status),
        "apscheduler_job_id": s.apscheduler_job_id,
        "next_fire_at": s.next_fire_at.isoformat() if s.next_fire_at else None,
        "last_fire_at": s.last_fire_at.isoformat() if s.last_fire_at else None,
        "source": s.source,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_schedules(
    request: Request,
    scope: str = Query(default="all", pattern="^(all|mine|subscribed)$"),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    svc = _get_service(request)
    viewer = _get_current_user_id(request)
    rows = await svc.list_for_viewer(viewer, scope=scope, status=status, limit=limit)
    return [_schedule_to_dict(s) for s in rows]


@router.post("")
async def create_schedule(body: ScheduleCreateRequest, request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    # Cross-user connection_id check.
    await _verify_target_connection(body.target, caller, request)
    target_json = body.target.model_dump_json()
    payload = {
        "title": body.title,
        "kind": body.kind,
        "prompt": body.prompt,
        "cron_expr": body.cron_expr,
        "run_at": body.run_at,
        "cron_tz": body.cron_tz or "Asia/Shanghai",
        "thread_id": body.thread_id,
        "target_json": target_json,
        "source": body.source,
    }
    try:
        schedule = await svc.create(payload=payload, current_user=caller)
    except (ValueError, PermissionError) as exc:
        # Validation and per-user cap surface as 422 / 403.
        status = 403 if isinstance(exc, PermissionError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _schedule_to_dict(schedule)


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    viewer = _get_current_user_id(request)
    schedule = await svc.get_for_viewer(schedule_id, viewer)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _schedule_to_dict(schedule)


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleUpdateRequest, request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    # Mutations: 404 only if the schedule does not exist at all.
    # Non-owner is 403 even if the caller happens to be subscribed --
    # the visibility filter is for reads, not for permission.
    existing = await svc._repo.get(schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if existing.owner_user_id != caller:
        raise HTTPException(status_code=403, detail="Only the owner can modify a schedule")

    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.prompt is not None:
        fields["prompt"] = body.prompt
    if body.cron_expr is not None:
        fields["cron_expr"] = body.cron_expr
    if body.cron_tz is not None:
        fields["cron_tz"] = body.cron_tz
    if body.target is not None:
        await _verify_target_connection(body.target, caller, request)
        fields["target_json"] = body.target.model_dump_json()

    updated = await svc.update(schedule_id, fields=fields, current_user=caller)
    return _schedule_to_dict(updated)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str, request: Request) -> Response:
    from fastapi import Response
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    existing = await svc._repo.get(schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if existing.owner_user_id != caller:
        raise HTTPException(status_code=403, detail="Only the owner can delete a schedule")
    await svc.soft_delete(schedule_id, current_user=caller)
    return Response(status_code=204)


@router.post("/{schedule_id}/pause")
async def pause_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    existing = await svc._repo.get(schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if existing.owner_user_id != caller:
        raise HTTPException(status_code=403, detail="Only the owner can pause a schedule")
    await svc.pause(schedule_id, current_user=caller)
    return {"id": schedule_id, "status": "paused"}


@router.post("/{schedule_id}/resume")
async def resume_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    existing = await svc._repo.get(schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if existing.owner_user_id != caller:
        raise HTTPException(status_code=403, detail="Only the owner can resume a schedule")
    await svc.resume(schedule_id, current_user=caller)
    return {"id": schedule_id, "status": "active"}


@router.post("/{schedule_id}/subscribe")
async def subscribe(schedule_id: str, request: Request) -> Response:
    from fastapi import Response
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    # Subscribe accepts any logged-in user. Use the raw repo (bypassing
    # the viewer filter) so a non-owner non-subscriber can opt in.
    # A 404 only fires when the schedule does not exist at all.
    existing = await svc._repo.get(schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await svc.subscribe(schedule_id, user_id=caller, target_json=None)
    return Response(status_code=204)


@router.post("/{schedule_id}/unsubscribe")
async def unsubscribe(schedule_id: str, request: Request) -> Response:
    from fastapi import Response
    svc = _get_service(request)
    caller = _get_current_user_id(request)
    # Unsubscribe is idempotent; we only need to confirm the schedule exists
    # at all so a typo'd id returns 404 instead of silently succeeding.
    existing = await svc._repo.get(schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await svc.unsubscribe(schedule_id, user_id=caller)
    return Response(status_code=204)


@router.get("/{schedule_id}/runs")
async def list_runs(
    schedule_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    svc = _get_service(request)
    viewer = _get_current_user_id(request)
    existing = await svc.get_for_viewer(schedule_id, viewer)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    rows = await svc.list_runs(schedule_id, viewer=viewer, limit=limit)
    return [
        {
            "id": r.id,
            "schedule_id": r.schedule_id,
            "subscriber_user_id": r.subscriber_user_id,
            "run_id": r.run_id,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "attempt": r.attempt,
            "error_summary": r.error_summary,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]
