"""Business layer for the scheduling subsystem.

:class:`ScheduleService` is the single orchestration point between
:class:`ScheduleRepository` (persistence), :class:`SchedulerEngine`
(in-process APScheduler wrapper), and the per-user limits in
:class:`LimitsConfig`. REST routers (Task 8) and the chat-side
agent tool (Task 9) call into this class for every user-visible
operation; the executor (Task 7) reads from the repo and engine
directly, and only uses the service for static helpers like
:func:`compute_next_retry`.

Scope of this task
------------------

The executor (``app.scheduling.executor``) is **not** wired in this
task. The constructor accepts ``executor=None`` so the type stays
stubs of the eventual interface; the service never calls into the
executor in this task. Task 7 will define the real executor and
either replace the constructor or inject the real instance from the
lifespan in Task 8.

Errors
------

- :class:`PermissionError` — caller is not the owner of the schedule
  (or is not allowed to act on it). The REST router maps this to 403.
- :class:`LookupError` — schedule id does not exist. The router maps
  this to 404.
- :class:`ValueError` — bad input (invalid cron, protected field in
  ``update``, etc.). The router maps this to 400.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from croniter import croniter

from deerflow.config.scheduling import LimitsConfig, SchedulingConfig
from deerflow.persistence.models.schedule import (
    Schedule,
    ScheduleKind,
    ScheduleRun,
    ScheduleStatus,
)
from deerflow.persistence.schedule_repo import ScheduleRepository

logger = logging.getLogger(__name__)

# Fields a caller is *not* allowed to mutate through the generic
# ``update`` path. ``status`` has its own dedicated methods (pause /
# resume) so it lives here too. ``apscheduler_job_id`` and
# ``next_fire_at`` are engine-owned metadata and the engine re-writes
# them, so a user must not be able to back-date them through the API.
_UPDATE_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "owner_user_id",
        "source",
        "created_at",
        "status",
        "apscheduler_job_id",
        "next_fire_at",
        "last_fire_at",
    }
)


class _EngineLike(Protocol):
    """Structural type for the engine surface the service needs.

    Kept as a :class:`Protocol` so we don't depend on the concrete
    :class:`app.scheduling.scheduler.SchedulerEngine` import (which
    would risk a circular import with the engine module under test
    wiring). The real engine satisfies this protocol directly.
    """

    def add_schedule(self, schedule: Schedule) -> None: ...
    def remove_schedule(self, schedule_id: str) -> None: ...


class _ExecutorLike(Protocol):
    """Structural type for the executor surface the service may need.

    Not used in this task; kept so the constructor signature is
    forward-compatible with Task 7 without re-touching the wiring
    code.
    """


class ScheduleService:
    """Async business layer for the scheduling subsystem."""

    def __init__(
        self,
        repo: ScheduleRepository,
        engine: _EngineLike,
        config: SchedulingConfig,
        limits: LimitsConfig,
        executor: _ExecutorLike | None = None,
    ) -> None:
        self._repo = repo
        self._engine = engine
        self._config = config
        self._limits = limits
        # TODO(Task 7): wire the executor in. The service does not
        # call any executor methods in this task; the integration is
        # added when Task 7 lands the executor module.
        self._executor = executor

    # ------------------------------------------------------------------
    # Schedule CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        payload: dict[str, Any],
        current_user: str,
    ) -> Schedule:
        """Validate, persist, and register a new schedule.

        The ``payload`` is a dict mirroring the public REST body shape
        (Task 8 will build a pydantic model on top of this). The
        service only consumes a known set of keys; unknown keys are
        ignored so a forward-compatible REST body does not blow up
        the legacy call sites.
        """
        kind: ScheduleKind = payload["kind"]
        title: str = payload["title"]
        prompt: str = payload["prompt"]
        target_json: str = payload["target_json"]
        cron_expr: str | None = payload.get("cron_expr")
        run_at: datetime | None = payload.get("run_at")
        cron_tz: str = payload.get("cron_tz", self._config.timezone)
        thread_id: str | None = payload.get("thread_id")
        source: str = payload.get("source", "api")

        if kind == ScheduleKind.CRON:
            if not cron_expr:
                raise ValueError("cron schedule requires cron_expr")
            self.validate_cron(cron_expr)
        elif kind == ScheduleKind.ONE_SHOT:
            if run_at is None:
                raise ValueError("one_shot schedule requires run_at")
        else:
            raise ValueError(f"unknown schedule kind: {kind!r}")

        # Per-user cap. We check before insert so a rejected create
        # never leaves a row behind. The repo excludes ``DELETED`` so
        # soft-deleted schedules do not count against the cap.
        current_active = await self._repo.count_active_for_user(current_user)
        if current_active >= self._limits.max_active_schedules_per_user:
            raise PermissionError(
                f"max_active_schedules_per_user={self._limits.max_active_schedules_per_user} reached"
            )

        row = await self._repo.create(
            owner_user_id=current_user,
            kind=kind,
            title=title,
            prompt=prompt,
            target_json=target_json,
            cron_expr=cron_expr,
            run_at=run_at,
            cron_tz=cron_tz,
            thread_id=thread_id,
            source=source,
        )

        # Register the job with the engine. ``add_schedule`` is a
        # best-effort sync call; a failure here leaves the row in
        # ``ACTIVE`` so a restart can re-register it (the engine's
        # recovery path picks it up via ``list_active``).
        try:
            self._engine.add_schedule(row)
        except Exception:
            logger.exception("[ScheduleService] engine.add_schedule failed for %s", row.id)

        logger.info("[ScheduleService] create owner=%s id=%s kind=%s", current_user, row.id, kind.value)
        return row

    async def get_for_viewer(
        self,
        schedule_id: str,
        viewer: str,
    ) -> Schedule | None:
        return await self._repo.get_for_viewer(schedule_id, viewer)

    async def list_for_viewer(
        self,
        viewer: str,
        scope: Literal["all", "mine", "subscribed"] = "all",
        status: ScheduleStatus | None = None,
        limit: int = 100,
    ) -> list[Schedule]:
        return await self._repo.list_for_viewer(
            viewer_user_id=viewer,
            scope=scope,
            status=status,
            limit=limit,
        )

    async def update(
        self,
        schedule_id: str,
        *,
        fields: dict[str, Any],
        current_user: str,
    ) -> Schedule:
        """Owner-only update with a field whitelist enforced at the service layer.

        The repo also enforces a whitelist, but the service-level check
        is what the REST router should depend on -- a future
        refactor of the repo's whitelist should not silently widen
        the surface here.
        """
        forbidden = set(fields) & _UPDATE_FORBIDDEN_FIELDS
        if forbidden:
            raise ValueError(f"update: cannot modify fields: {sorted(forbidden)}")

        existing = await self._repo.get(schedule_id)
        if existing is None:
            raise LookupError(f"schedule {schedule_id!r} not found")
        if existing.owner_user_id != current_user:
            raise PermissionError("only the owner can update a schedule")

        return await self._repo.update(schedule_id, fields)

    # ------------------------------------------------------------------
    # Lifecycle: pause / resume / soft_delete
    # ------------------------------------------------------------------

    async def pause(self, schedule_id: str, *, current_user: str) -> None:
        await self._assert_owner(schedule_id, current_user)
        await self._repo.set_status(schedule_id, ScheduleStatus.PAUSED)
        try:
            self._engine.remove_schedule(schedule_id)
        except Exception:
            logger.exception("[ScheduleService] engine.remove_schedule failed for %s", schedule_id)
        logger.info("[ScheduleService] pause id=%s by=%s", schedule_id, current_user)

    async def resume(self, schedule_id: str, *, current_user: str) -> None:
        row = await self._assert_owner(schedule_id, current_user)
        await self._repo.set_status(schedule_id, ScheduleStatus.ACTIVE)
        # Re-register the *refreshed* row so the engine sees the latest
        # fields (cron / one_shot timing may have been updated in
        # between). Fall back to the in-memory copy on read failure.
        try:
            refreshed = await self._repo.get(schedule_id)
            target = refreshed if refreshed is not None else row
            self._engine.add_schedule(target)
        except Exception:
            logger.exception("[ScheduleService] engine.add_schedule failed for %s", schedule_id)
        logger.info("[ScheduleService] resume id=%s by=%s", schedule_id, current_user)

    async def soft_delete(self, schedule_id: str, *, current_user: str) -> None:
        await self._assert_owner(schedule_id, current_user)
        await self._repo.soft_delete(schedule_id)
        try:
            self._engine.remove_schedule(schedule_id)
        except Exception:
            logger.exception("[ScheduleService] engine.remove_schedule failed for %s", schedule_id)
        logger.info("[ScheduleService] soft_delete id=%s by=%s", schedule_id, current_user)

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        schedule_id: str,
        *,
        user_id: str,
        target_json: str | None = None,
    ) -> None:
        await self._repo.subscribe(schedule_id, user_id, target_json)

    async def unsubscribe(self, schedule_id: str, *, user_id: str) -> None:
        await self._repo.unsubscribe(schedule_id, user_id)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def list_runs(
        self,
        schedule_id: str,
        *,
        viewer: str,
        limit: int = 50,
    ) -> list[ScheduleRun]:
        return await self._repo.list_runs(schedule_id, viewer, limit)

    # ------------------------------------------------------------------
    # Static helpers (pure logic)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_cron(expr: str) -> None:
        """Raise :class:`ValueError` if ``expr`` is not a valid cron expression.

        Wraps :func:`croniter.is_valid` so callers can validate at the
        boundary (REST handler, agent tool) without having to import
        croniter themselves.
        """
        if not croniter.is_valid(expr):
            raise ValueError(f"invalid cron expression: {expr!r}")

    @staticmethod
    def compute_next_retry(
        *,
        attempt: int,
        now: datetime,
        config: SchedulingConfig,
    ) -> datetime | None:
        """Return ``now + config.retry.backoff_seconds[attempt-1]`` or ``None``.

        ``attempt`` is 1-based. If ``attempt > max_attempts`` we have
        exhausted the retry budget and return ``None`` so the caller
        can mark the schedule as permanently failed. ``backoff_seconds``
        is a list (not a generator) so the config can be read once
        at startup.
        """
        backoff = config.retry.backoff_seconds
        if attempt < 1 or attempt > config.retry.max_attempts:
            return None
        if attempt - 1 >= len(backoff):
            return None
        return now + timedelta(seconds=backoff[attempt - 1])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _assert_owner(self, schedule_id: str, current_user: str) -> Schedule:
        row = await self._repo.get(schedule_id)
        if row is None:
            raise LookupError(f"schedule {schedule_id!r} not found")
        if row.owner_user_id != current_user:
            raise PermissionError("only the owner can act on a schedule")
        return row


__all__ = ["ScheduleService"]
