"""APScheduler-backed engine for scheduled agent runs.

This module wraps APScheduler so a leader Gateway replica can
auto-load ``status=active`` rows from :class:`ScheduleRepository` and
fire them via an injected callback. The engine itself does not know
how to run a langgraph run -- the executor (Task 7) injects an
``on_fire(schedule_id)`` coroutine and the engine merely forwards
the schedule id to it.

Recovery contract
-----------------

A leader startup calls :meth:`SchedulerEngine.start`, which:

1. bails out if the engine is not the leader (multi-replica safety
   net) or if scheduling is disabled in :class:`SchedulingConfig`;
2. builds an :class:`AsyncIOScheduler` backed by
   :class:`SQLAlchemyJobStore` so jobs survive a process restart;
3. loads every ``ACTIVE`` row from the repo and registers it under a
   stable ``sched:<id>`` APScheduler id;
4. writes the new ``apscheduler_job_id`` and ``next_fire_at`` back
   to the row so a subsequent restart can re-register the same job.

The fire callback
-----------------

:meth:`SchedulerEngine.__init__` accepts an optional ``on_fire``
callable. When ``on_fire is None`` (the early-recovery case where
the executor has not been wired in yet) the engine installs a no-op
coroutine so a schedule that fires during the recovery window does
not blow up -- it just becomes a dropped trigger, which is logged
at WARNING for the audit trail.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.scheduling.leader import LeaderLock
from deerflow.config.scheduling import SchedulingConfig
from deerflow.persistence.models.schedule import Schedule, ScheduleKind
from deerflow.persistence.schedule_repo import ScheduleRepository

if TYPE_CHECKING:
    from apscheduler.jobstores.base import BaseJobStore

logger = logging.getLogger(__name__)

# APScheduler job-id prefix. Used by ``list_due`` to recover the
# schedule id from the jobstore and by ``remove_schedule`` to find
# the right job. Keep in sync with the test helpers.
_JOB_ID_PREFIX = "sched:"


async def _noop_on_fire(schedule_id: str) -> None:
    """Default fire callback used when the executor has not been
    injected yet. Logged at WARNING so dropped triggers are visible
    in the audit trail.
    """
    logger.warning("[Scheduler] fire dropped: no on_fire callback wired (schedule_id=%s)", schedule_id)


class SchedulerEngine:
    """Wraps APScheduler with the leader-recovery contract.

    Lifecycle::

        engine = SchedulerEngine(config, repo, leader, on_fire=on_fire)
        await engine.start()         # no-op if not leader / disabled
        engine.add_schedule(row)     # add a new schedule
        engine.remove_schedule(id)   # remove a schedule
        engine.trigger_now(id)       # bump next_run_time to now
        await engine.stop()          # shutdown
    """

    def __init__(
        self,
        config: SchedulingConfig,
        repo: ScheduleRepository,
        leader: LeaderLock,
        *,
        on_fire: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._repo = repo
        self._leader = leader
        self._on_fire: Callable[[str], Awaitable[None]] = on_fire if on_fire is not None else _noop_on_fire
        self._sched: AsyncIOScheduler | None = None
        # Sync engine dedicated to APScheduler's SQLAlchemyJobStore.
        # Created in ``_build_jobstore``; disposed in ``stop`` so
        # the connection pool doesn't outlive the scheduler.
        self._sync_engine = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._leader.is_leader:
            logger.info("[Scheduler] not leader; skipping start")
            return
        if not self._config.enabled:
            logger.info("[Scheduler] disabled in config; skipping start")
            return

        jobstore: BaseJobStore = self._build_jobstore()
        self._sched = AsyncIOScheduler(
            jobstores={"default": jobstore},
            timezone=self._config.timezone,
        )

        schedules = await self._repo.list_active()
        # Register jobs first, then write metadata so a failure in
        # the DB write does not leave APScheduler without a job.
        pending: list[tuple[str, datetime | None]] = []
        for s in schedules:
            next_fire = self._register_in_apscheduler(s)
            pending.append((s.id, next_fire))
        self._sched.start()
        # Awaits the metadata writes *after* ``start`` so the
        # scheduler is running while we update the rows. If any
        # write fails the next ``add_schedule`` call (or the next
        # restart) will reconcile the row.
        for schedule_id, next_fire in pending:
            await self._write_job_metadata(schedule_id, next_fire)
        logger.info("[Scheduler] started with %d active schedules", len(schedules))

    async def stop(self) -> None:
        if self._sched is not None:
            self._sched.shutdown(wait=False)
            self._sched = None
        if self._sync_engine is not None:
            # ``dispose`` is sync; it returns once the connection
            # pool is closed. Aiosqlite worker threads may still
            # have a pending result to deliver, but at this point
            # the engine itself is gone so the only failure mode is
            # an event-loop teardown warning -- acceptable.
            self._sync_engine.dispose()
            self._sync_engine = None

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add_schedule(self, schedule: Schedule) -> None:
        """Register a new schedule (or replace an existing one).

        Sync because the brief's public API is sync. The metadata
        write is fire-and-forget -- the in-memory job is the source
        of truth for the current process, and a transient DB blip is
        repaired on the next ``add_schedule`` call.
        """
        if self._sched is None:
            return
        next_fire = self._register_in_apscheduler(schedule)
        self._fire_and_forget_metadata_write(schedule.id, next_fire)

    def remove_schedule(self, schedule_id: str) -> None:
        if self._sched is None:
            return
        job_id = self._job_id(schedule_id)
        try:
            self._sched.remove_job(job_id)
        except Exception:
            # Job not present is fine; any other error is logged so
            # a misconfigured job doesn't take the engine down.
            logger.exception("[Scheduler] remove_job failed for %s", schedule_id)

    def trigger_now(self, schedule_id: str) -> None:
        if self._sched is None:
            return
        try:
            self._sched.modify_job(self._job_id(schedule_id), next_run_time=datetime.now(UTC))
        except Exception:
            logger.exception("[Scheduler] trigger_now failed for %s", schedule_id)

    def list_due(self, now: datetime) -> list[str]:
        if self._sched is None:
            return []
        out: list[str] = []
        for job in self._sched.get_jobs():
            # ``next_run_time`` is only set once the scheduler has
            # processed the job; before that it is unset and we
            # treat the job as "not due" (which is the safe answer).
            nrt = getattr(job, "next_run_time", None)
            if nrt is not None and nrt <= now:
                out.append(job.id.removeprefix(_JOB_ID_PREFIX))
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _job_id(self, schedule_id: str) -> str:
        return f"{_JOB_ID_PREFIX}{schedule_id}"

    def _build_trigger(self, s: Schedule):
        if s.kind == ScheduleKind.CRON:
            tz = ZoneInfo(s.cron_tz or self._config.timezone)
            assert s.cron_expr is not None, f"cron schedule {s.id} missing cron_expr"
            return CronTrigger.from_crontab(s.cron_expr, timezone=tz)
        assert s.run_at is not None, f"one_shot schedule {s.id} missing run_at"
        # ``run_at`` is stored as a UTC-aware datetime (the
        # ``DateTime(timezone=True)`` column coerces to UTC on read).
        # We must NOT tell APScheduler to re-localize it via
        # ``timezone=Asia/Shanghai``: ``Job._modify`` calls
        # ``convert_to_datetime(value, self._scheduler.timezone, ...)``
        # which would re-write the stored value as the *same wall-clock
        # time* in the scheduler's timezone, producing a 7h misfire
        # on a Shanghai-timezone scheduler. Pinning the trigger to
        # UTC keeps the comparison correct regardless of the
        # scheduler's display timezone.
        return DateTrigger(run_date=s.run_at, timezone=ZoneInfo("UTC"))

    def _register_in_apscheduler(self, s: Schedule) -> datetime | None:
        """Register a single schedule in APScheduler and return the
        computed ``next_fire`` time.

        The next fire time is computed via ``trigger.get_next_fire_time``
        rather than ``job.next_run_time`` because APScheduler 3.x only
        sets the latter after the scheduler has processed the job at
        least once. Computing it here keeps the in-memory job
        registration and the DB write in sync.
        """
        assert self._sched is not None, "_register_in_apscheduler called before start()"
        trigger = self._build_trigger(s)
        self._sched.add_job(
            self._on_fire,
            trigger=trigger,
            args=[s.id],
            id=self._job_id(s.id),
            replace_existing=True,
            coalesce=self._config.apscheduler.coalesce,
            max_instances=self._config.apscheduler.max_instances,
            misfire_grace_time=self._config.apscheduler.misfire_grace_seconds,
        )
        return trigger.get_next_fire_time(None, datetime.now(UTC))

    def _build_jobstore(self) -> BaseJobStore:
        """Build the :class:`SQLAlchemyJobStore` from the persistence
        engine.

        The async engine uses ``sqlite+aiosqlite`` (or
        ``postgresql+asyncpg``); APScheduler's :class:`SQLAlchemyJobStore`
        wants a sync URL, so we strip the async driver prefix and
        build a small sync engine dedicated to the jobstore. The
        sync engine is stashed on ``self`` so :meth:`stop` can
        dispose it cleanly -- otherwise the engine's connection
        pool outlives the scheduler and aiosqlite-style workers
        emit teardown warnings.
        """
        from sqlalchemy import create_engine

        from deerflow.persistence.engine import get_engine

        async_eng = get_engine()
        if async_eng is None:
            raise RuntimeError("SchedulerEngine cannot start with backend=memory: no SQLAlchemy URL")
        sync_url = str(async_eng.url).replace("+aiosqlite", "").replace("+asyncpg", "")
        self._sync_engine = create_engine(sync_url)
        return SQLAlchemyJobStore(engine=self._sync_engine)

    def _fire_and_forget_metadata_write(self, schedule_id: str, next_fire: datetime | None) -> None:
        """Schedule a best-effort async write of the APScheduler job
        metadata back to the row.

        Used by the sync :meth:`add_schedule` path; the recovery path
        in :meth:`start` awaits the write directly so a subsequent
        ``repo.get`` observes the metadata.
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            logger.warning("[Scheduler] no event loop; skipping metadata write for %s", schedule_id)
            return
        if loop.is_running():
            loop.create_task(self._write_job_metadata(schedule_id, next_fire))
        else:
            # No event loop -- nothing we can do. The next call to
            # ``add_schedule`` or a restart will reconcile the row.
            logger.warning("[Scheduler] no running event loop; metadata write deferred for %s", schedule_id)

    async def _write_job_metadata(self, schedule_id: str, next_fire: datetime | None) -> None:
        """Async write of the APScheduler job id + next fire time to
        the row. Best-effort: errors are logged, not raised.
        """
        try:
            await self._repo.update(
                schedule_id,
                {
                    "apscheduler_job_id": self._job_id(schedule_id),
                    "next_fire_at": next_fire,
                },
            )
        except LookupError:
            # Row vanished between ``list_active`` and the write --
            # benign; the cron / one_shot is gone.
            logger.info("[Scheduler] schedule %s disappeared before metadata write", schedule_id)
        except Exception:
            logger.exception("[Scheduler] metadata write failed for %s", schedule_id)


__all__ = ["SchedulerEngine"]
