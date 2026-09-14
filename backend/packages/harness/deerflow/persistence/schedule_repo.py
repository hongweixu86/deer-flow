"""SQLAlchemy-backed repository for scheduled tasks (cron + one-shot).

This is the single persistence facade for the scheduler subsystem.
Downstream tasks (scheduler engine, executor, REST router, agent tools)
all read/write schedules via the methods on this class. The methods are
deliberately focused -- each one answers one question or performs one
mutation -- so callers can compose them without needing to know the
table layout.

Visibility rules (security-relevant)
------------------------------------

``get_for_viewer`` returns ``None`` (NOT 403, NOT an empty Schedule)
when the viewer is not the owner and has no subscription row. The
filter is applied at the SQL level via an ``EXISTS`` subquery against
``schedule_subscriptions``; a non-visible row never enters Python
memory. The same pattern is reused by ``list_runs`` and ``get_run``:
non-owner non-subscriber viewers cannot see the underlying runs
either.

Soft delete
-----------

``soft_delete`` flips ``status`` to ``DELETED`` rather than removing
the row. ``count_active_for_user`` and ``list_active`` therefore
exclude ``DELETED`` rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.models.schedule import (
    Schedule,
    ScheduleKind,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleStatus,
    ScheduleSubscription,
)

logger = logging.getLogger(__name__)


# Fields a caller is allowed to pass to ``update``. Anything outside this
# set is rejected so a malicious or buggy caller cannot pivot a schedule
# to another owner via the generic update path.
_UPDATE_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "cron_expr",
        "run_at",
        "cron_tz",
        "prompt",
        "thread_id",
        "target_json",
        "apscheduler_job_id",
        "next_fire_at",
        "last_fire_at",
    }
)

# Fields a caller is allowed to pass to ``update_run``.
_UPDATE_RUN_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "run_id",
        "error_summary",
        "started_at",
        "finished_at",
        "next_retry_at",
        "attempt",
    }
)


class ScheduleRepository:
    """Async persistence facade for the scheduler subsystem."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------
    # Schedule CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        owner_user_id: str,
        kind: ScheduleKind,
        prompt: str,
        target_json: str,
        title: str,
        cron_expr: str | None = None,
        run_at: datetime | None = None,
        cron_tz: str = "Asia/Shanghai",
        thread_id: str | None = None,
        apscheduler_job_id: str | None = None,
        source: str = "api",
        next_fire_at: datetime | None = None,
    ) -> Schedule:
        """Insert a new schedule row and return the persisted instance.

        ``title``, ``owner_user_id``, ``kind``, ``prompt`` and ``target_json``
        are required; the rest are optional and default to nullable
        columns or sensible defaults.
        """
        row = Schedule(
            owner_user_id=owner_user_id,
            title=title,
            kind=kind,
            cron_expr=cron_expr,
            run_at=run_at,
            cron_tz=cron_tz,
            prompt=prompt,
            thread_id=thread_id,
            target_json=target_json,
            status=ScheduleStatus.ACTIVE,
            apscheduler_job_id=apscheduler_job_id,
            next_fire_at=next_fire_at,
            source=source,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        logger.info("schedule created: id=%s owner=%s kind=%s", row.id, owner_user_id, kind)
        return row

    async def get(self, schedule_id: str) -> Schedule | None:
        """Fetch a schedule by id with no viewer filtering.

        Use ``get_for_viewer`` when the caller's identity matters.
        """
        async with self._sf() as session:
            return await session.get(Schedule, schedule_id)

    async def get_for_viewer(self, schedule_id: str, viewer_user_id: str) -> Schedule | None:
        """Return the schedule only if the viewer can see it.

        Visibility = owner OR has a row in ``schedule_subscriptions``.
        Returns ``None`` for non-visible rows (404-on-not-visible, never 403).
        The filter is applied at the SQL level so the row never enters
        Python memory.
        """
        subq = select(ScheduleSubscription.schedule_id).where(
            ScheduleSubscription.schedule_id == schedule_id,
            ScheduleSubscription.user_id == viewer_user_id,
        )
        async with self._sf() as session:
            stmt = select(Schedule).where(
                Schedule.id == schedule_id,
                (Schedule.owner_user_id == viewer_user_id) | Schedule.id.in_(subq),
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list_for_viewer(
        self,
        viewer_user_id: str,
        scope: Literal["all", "mine", "subscribed"] = "all",
        status: ScheduleStatus | None = None,
        limit: int = 100,
    ) -> list[Schedule]:
        """List schedules visible to ``viewer_user_id``.

        ``scope``:
            - ``"all"``        -- owned OR subscribed
            - ``"mine"``       -- owned only
            - ``"subscribed"`` -- subscribed only
        ``status`` filters to a specific lifecycle status when given.
        """
        if limit <= 0:
            return []
        subq = select(ScheduleSubscription.schedule_id).where(ScheduleSubscription.user_id == viewer_user_id)
        async with self._sf() as session:
            stmt = select(Schedule)
            if scope == "mine":
                stmt = stmt.where(Schedule.owner_user_id == viewer_user_id)
            elif scope == "subscribed":
                stmt = stmt.where(Schedule.id.in_(subq))
            else:  # "all"
                stmt = stmt.where((Schedule.owner_user_id == viewer_user_id) | Schedule.id.in_(subq))
            if status is not None:
                stmt = stmt.where(Schedule.status == status)
            stmt = stmt.order_by(Schedule.created_at.desc(), Schedule.id.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars())

    async def update(self, schedule_id: str, fields: dict[str, Any]) -> Schedule:
        """Update a schedule. Only whitelisted fields are accepted.

        ``updated_at`` is bumped automatically by the ORM via ``onupdate``.
        Raises ``ValueError`` if ``fields`` contains a key not in the
        whitelist -- this keeps the generic update path from pivoting
        a schedule's owner or status.
        """
        unknown = set(fields) - _UPDATE_ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"update: cannot modify fields: {sorted(unknown)}")
        async with self._sf() as session:
            row = await session.get(Schedule, schedule_id)
            if row is None:
                raise LookupError(f"schedule {schedule_id!r} not found")
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
        return row

    async def soft_delete(self, schedule_id: str) -> None:
        """Flip ``status`` to ``DELETED``. Idempotent."""
        async with self._sf() as session:
            await session.execute(update(Schedule).where(Schedule.id == schedule_id).values(status=ScheduleStatus.DELETED, updated_at=datetime.now(UTC)))
            await session.commit()

    async def set_status(self, schedule_id: str, status: ScheduleStatus) -> None:
        """Set ``status``. Used by the REST router (pause/resume) and the
        scheduler engine (mark active/paused/deleted)."""
        async with self._sf() as session:
            await session.execute(update(Schedule).where(Schedule.id == schedule_id).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()

    async def list_active(self) -> list[Schedule]:
        """Return all ``ACTIVE`` schedules. Used at startup so the
        scheduler engine can re-register its APScheduler jobs after a
        process restart."""
        async with self._sf() as session:
            stmt = select(Schedule).where(Schedule.status == ScheduleStatus.ACTIVE)
            result = await session.execute(stmt)
            return list(result.scalars())

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def list_subscribers(self, schedule_id: str) -> list[ScheduleSubscription]:
        async with self._sf() as session:
            stmt = select(ScheduleSubscription).where(ScheduleSubscription.schedule_id == schedule_id)
            result = await session.execute(stmt)
            return list(result.scalars())

    async def subscribe(
        self,
        schedule_id: str,
        user_id: str,
        target_json: str | None = None,
    ) -> None:
        """Add a subscriber. Idempotent: re-subscribing the same user
        is a no-op (preserves the existing ``target_json``)."""
        async with self._sf() as session:
            existing = await session.get(ScheduleSubscription, (schedule_id, user_id))
            if existing is not None:
                if target_json is not None:
                    existing.target_json = target_json
                    await session.commit()
                return
            row = ScheduleSubscription(schedule_id=schedule_id, user_id=user_id, target_json=target_json, enabled=True)
            session.add(row)
            await session.commit()

    async def unsubscribe(self, schedule_id: str, user_id: str) -> None:
        async with self._sf() as session:
            sub = await session.get(ScheduleSubscription, (schedule_id, user_id))
            if sub is None:
                return
            await session.delete(sub)
            await session.commit()

    # ------------------------------------------------------------------
    # Counts / limits
    # ------------------------------------------------------------------

    async def count_active_for_user(self, user_id: str) -> int:
        """Count non-deleted schedules owned by ``user_id``.

        Used to enforce ``LimitsConfig.max_active_schedules_per_user``.
        """
        async with self._sf() as session:
            stmt = select(func.count()).select_from(Schedule).where(Schedule.owner_user_id == user_id, Schedule.status != ScheduleStatus.DELETED)
            result = await session.execute(stmt)
            return int(result.scalar_one())

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def create_run(
        self,
        schedule_id: str,
        subscriber_user_id: str | None,
        attempt: int = 1,
    ) -> ScheduleRun:
        """Insert a new run row in QUEUED state. The caller is
        responsible for dispatching it into LangGraph and then calling
        ``attach_run_id`` once a real run id is known."""
        row = ScheduleRun(
            schedule_id=schedule_id,
            subscriber_user_id=subscriber_user_id,
            attempt=attempt,
            status=ScheduleRunStatus.QUEUED,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def attach_run_id(self, schedule_run_id: str, langgraph_run_id: str) -> None:
        """Write the LangGraph run id onto the row, once it is known."""
        async with self._sf() as session:
            await session.execute(update(ScheduleRun).where(ScheduleRun.id == schedule_run_id).values(run_id=langgraph_run_id))
            await session.commit()

    async def update_run(self, schedule_run_id: str, **fields: Any) -> None:
        """Update a run. Only whitelisted fields are accepted."""
        unknown = set(fields) - _UPDATE_RUN_ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"update_run: cannot modify fields: {sorted(unknown)}")
        if not fields:
            return
        async with self._sf() as session:
            await session.execute(update(ScheduleRun).where(ScheduleRun.id == schedule_run_id).values(**fields))
            await session.commit()

    async def list_runs(
        self,
        schedule_id: str,
        viewer_user_id: str,
        limit: int = 50,
    ) -> list[ScheduleRun]:
        """List runs for a schedule, visibility-restricted.

        The viewer must own the schedule OR be a subscriber; non-owners
        who aren't subscribed are denied the run history entirely.
        Subscribers see only their own runs (``subscriber_user_id``
        matching). The owner sees everyone's runs.
        """
        if limit <= 0:
            return []
        # Visibility check first; mirrors ``get_for_viewer`` so a
        # non-visible schedule's runs cannot be enumerated.
        visible = await self.get_for_viewer(schedule_id, viewer_user_id)
        if visible is None:
            return []
        async with self._sf() as session:
            stmt = select(ScheduleRun).where(ScheduleRun.schedule_id == schedule_id)
            if visible.owner_user_id != viewer_user_id:
                stmt = stmt.where(ScheduleRun.subscriber_user_id == viewer_user_id)
            stmt = stmt.order_by(ScheduleRun.started_at.desc().nulls_last(), ScheduleRun.id.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars())

    async def get_run(self, schedule_run_id: str, viewer_user_id: str) -> ScheduleRun | None:
        """Fetch a run if the viewer can see it.

        Visibility = owns the parent schedule OR is a subscriber; for
        non-owner subscribers, ``subscriber_user_id`` on the run must
        also match the viewer.
        """
        async with self._sf() as session:
            run = await session.get(ScheduleRun, schedule_run_id)
            if run is None:
                return None
            schedule = await session.get(Schedule, run.schedule_id)
            if schedule is None:
                return None
            if schedule.owner_user_id == viewer_user_id:
                return run
            sub = await session.execute(
                select(
                    exists().where(
                        ScheduleSubscription.schedule_id == schedule.id,
                        ScheduleSubscription.user_id == viewer_user_id,
                    )
                )
            )
            if not sub.scalar():
                return None
            if run.subscriber_user_id != viewer_user_id:
                return None
            return run


__all__ = ["ScheduleRepository"]
