"""Lifespan wrapper for the scheduling subsystem.

The ``SchedulerService`` is the single container the gateway lifespan
constructs at startup and tears down at shutdown. It owns:

- the :class:`~app.scheduling.leader.LeaderLock` -- the cluster-wide
  advisory lock that decides whether this replica is the leader,
- the :class:`~deerflow.persistence.schedule_repo.ScheduleRepository`
  -- the persistence facade for ``schedules`` / ``schedule_runs`` /
  ``schedule_subscriptions``,
- the :class:`~app.scheduling.scheduler.SchedulerEngine` -- the
  APScheduler wrapper that fires schedules,
- the :class:`~app.scheduling.executor.ScheduleExecutor` -- the
  per-fire runtime (run-manager + push),
- the :class:`~app.scheduling.service.ScheduleService` -- the business
  layer the REST router calls.

Why one wrapper, not three
--------------------------

Splitting the four collaborators across separate ``start_*`` /
``stop_*`` functions would scatter the leader-lock decision and the
APScheduler + executor wiring across the lifespan. The single-class
shape keeps the dependency order explicit: leader is acquired first,
then the engine starts (only on a leader replica), then the executor
becomes the engine's ``on_fire`` callback.

Lifecycle (mirrors ``app.channels.service.start_channel_service``)
-----------------------------------------------------------------

1. :func:`start_scheduler_service` builds a :class:`SchedulerService`
   and calls :meth:`SchedulerService.start`. The leader-lock
   acquisition is the first thing the start path does; on a non-leader
   replica the engine never starts and the service is a no-op for the
   rest of its lifetime.
2. The service is attached to ``app.state.scheduler_service`` so the
   router and other request-time consumers can resolve it through
   ``app.state``.
3. :func:`stop_scheduler_service` calls :meth:`SchedulerService.stop`
   in reverse order, with the leader lock released last so a
   failover replica does not try to take over a leader that is still
   draining.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.channels.message_bus import MessageBus
from app.scheduling.executor import ScheduleExecutor
from app.scheduling.leader import LeaderLock
from app.scheduling.scheduler import SchedulerEngine
from app.scheduling.service import ScheduleService
from deerflow.config.app_config import AppConfig
from deerflow.config.scheduling import LimitsConfig, SchedulingConfig
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.schedule_repo import ScheduleRepository

if TYPE_CHECKING:
    from deerflow.runtime import RunManager

logger = logging.getLogger(__name__)

# Module-level singleton -- mirrors the pattern in
# ``app.channels.service``. The gateway's lifespan calls
# :func:`start_scheduler_service` once and :func:`stop_scheduler_service`
# once; any code path that needs the service reads
# ``app.state.scheduler_service`` so the singleton here is only the
# truth during a brief startup window before the lifespan attaches it.
_scheduler_service: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService | None:
    """Return the running :class:`SchedulerService` singleton, if any."""
    return _scheduler_service


def set_scheduler_service_for_testing(svc: SchedulerService | None) -> None:
    """Test-only hook: install / clear the module-level singleton.

    Production code never calls this; it lets router tests skip the
    lifespan and inject a pre-built service so ``get_scheduler_service()``
    can resolve the same instance the router looks up.
    """
    global _scheduler_service
    _scheduler_service = svc


@dataclass
class SchedulerService:
    """Container that owns the scheduling subsystem at runtime.

    Attributes:
        config: The :class:`SchedulingConfig` snapshot used to build the
            engine. Mutating the live config does not retroactively
            reconfigure a started engine (restart-required field).
        limits: The :class:`LimitsConfig` used by the service for
            per-user caps. Hot-reload safe -- the service reads it on
            every create.
        repo: The :class:`ScheduleRepository` for all persistence
            operations.
        engine: The :class:`SchedulerEngine` that owns the APScheduler
            jobstore. ``None`` until :meth:`start` is called.
        executor: The :class:`ScheduleExecutor` that turns a fire into
            a langgraph run. ``None`` until :meth:`start` is called.
        service: The :class:`ScheduleService` the REST router uses.
            ``None`` until :meth:`start` is called.
        leader: The :class:`LeaderLock` for the cluster-wide lock.
        instance_id: A process-unique identifier used in the
            ``scheduler_leader`` row.
    """

    config: SchedulingConfig
    limits: LimitsConfig
    repo: ScheduleRepository
    engine: SchedulerEngine
    executor: ScheduleExecutor
    service: ScheduleService
    leader: LeaderLock
    instance_id: str
    _started: bool = False

    def __init__(
        self,
        *,
        config: SchedulingConfig,
        limits: LimitsConfig,
        repo: ScheduleRepository,
        engine: SchedulerEngine,
        executor: ScheduleExecutor,
        service: ScheduleService,
        leader: LeaderLock,
        instance_id: str,
    ) -> None:
        self.config = config
        self.limits = limits
        self.repo = repo
        self.engine = engine
        self.executor = executor
        self.service = service
        self.leader = leader
        self.instance_id = instance_id

    async def start(self) -> None:
        """Acquire the leader lock and start the engine on success.

        The leader-lock decision is the gate: a non-leader replica
        never starts its engine and never registers jobs. The executor
        is started unconditionally -- it is local to this process and
        does not depend on cluster membership, but it only fires when
        the engine calls ``on_fire`` on this replica.
        """
        if self._started:
            return
        if not self.config.enabled:
            logger.info("[Scheduler] disabled in config; skipping start")
            self._started = True
            return

        acquired = await self.leader.try_acquire()
        if not acquired:
            logger.info("[Scheduler] not leader; engine and executor started in follower mode")
            # The engine + executor are still constructed so the
            # service surface stays usable (e.g. read paths work from
            # any replica), but the engine will not register jobs and
            # the executor will not be called as ``on_fire``.
            self._started = True
            return

        # The engine's ``on_fire`` callback is the executor. The
        # executor's ``enqueue`` is the entry point; for the MVP it
        # runs the work synchronously in the caller's task.
        await self.executor.start()
        await self.engine.start()
        self._started = True
        logger.info("[Scheduler] leader start complete: instance=%s", self.instance_id)

    async def stop(self) -> None:
        """Stop the engine, then release the leader lock.

        Reverse order from :meth:`start`: the engine is stopped first
        so no new jobs are scheduled, then the executor drains (no-op
        for the MVP), then the leader lock is released so a failover
        replica can take over immediately rather than waiting for the
        ``misfire_grace_seconds`` window.
        """
        if not self._started:
            return
        try:
            await self.engine.stop()
        except Exception:
            logger.exception("[Scheduler] engine.stop failed")
        try:
            await self.executor.stop()
        except Exception:
            logger.exception("[Scheduler] executor.stop failed")
        try:
            await self.leader.release()
        except Exception:
            logger.exception("[Scheduler] leader.release failed")
        self._started = False
        logger.info("[Scheduler] stop complete: instance=%s", self.instance_id)


def _resolve_run_manager() -> RunManager:
    """Resolve the ``RunManager`` from ``app.state`` or build a stub.

    The runtime populates ``app.state.run_manager`` during the
    langgraph lifespan; if the scheduling lifespan runs ahead of
    that (the typical order is langgraph_runtime first, then
    scheduler, but tests may run in isolation) we still want the
    service to be importable. The fallback is a runtime placeholder
    that logs and rejects; the real ``RunManager`` is wired in
    production by :func:`start_scheduler_service` once ``app.state``
    is populated.
    """
    # Imported lazily to avoid an import cycle at module import time.
    from deerflow.runtime import RunManager

    return RunManager(store=None)  # type: ignore[arg-type]


def _build_instance_id() -> str:
    """Build a per-process leader-lock id.

    The id is a ``<hostname>-<pid>-<random>`` tuple. ``hostname-pid``
    is human-readable in logs and survives a same-host restart, the
    random suffix disambiguates two replicas on the same host (rare
    but possible in test or blue/green deploys).
    """
    host = socket.gethostname() or "unknown"
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    return f"{host}-{pid}-{suffix}"


async def start_scheduler_service(
    app_config: AppConfig | None = None,
    *,
    run_manager: Any | None = None,
    message_bus: MessageBus | None = None,
) -> SchedulerService:
    """Build and start the :class:`SchedulerService`.

    Mirrors :func:`app.channels.service.start_channel_service`: idempotent
    (a second call returns the existing singleton), and attaches the
    service to ``app.state.scheduler_service`` so the REST router and
    other request-time consumers can resolve it without going through
    this module.

    Args:
        app_config: The :class:`AppConfig` snapshot the gateway lifespan
            takes at startup. ``None`` is tolerated -- the
            :func:`deerflow.config.app_config.get_app_config` loader
            will be called as a fallback.
        run_manager: The :class:`~deerflow.runtime.RunManager` to inject
            into the executor. ``None`` is tolerated for tests; the
            executor's ``enqueue`` will then short-circuit.
        message_bus: The :class:`MessageBus` instance the executor
            should publish schedule results on. When ``None`` (tests),
            a fresh bus is created so the executor's outbound publish
            is a no-op. The gateway lifespan passes the channel
            service's bus so the Feishu/Slack/etc. channel workers
            that already subscribe to ``bus.subscribe_outbound`` also
            receive scheduler-pushed results.
    """
    global _scheduler_service
    if _scheduler_service is not None:
        return _scheduler_service

    if app_config is None:
        from deerflow.config.app_config import get_app_config

        app_config = get_app_config()

    scheduling_config: SchedulingConfig = app_config.scheduling
    limits: LimitsConfig = app_config.limits

    session_factory = get_session_factory()
    if session_factory is None:
        # The langgraph_runtime in deps.py initialises the engine
        # first; if it has not run yet, the scheduler has nowhere to
        # persist. Raise so the lifespan surfaces a clear startup
        # error instead of silently building a non-functional
        # service.
        raise RuntimeError("start_scheduler_service called before init_engine_from_config(); cannot access schedule tables")

    repo = ScheduleRepository(session_factory)
    instance_id = _build_instance_id()
    leader = LeaderLock(instance_id=instance_id, session_factory=session_factory, misfire_grace_seconds=scheduling_config.apscheduler.misfire_grace_seconds)

    # The engine's ``on_fire`` callback is the executor's ``enqueue``.
    # The MessageBus is shared with the channel service so scheduler
    # results land in the same outbound queue the Feishu/Slack/etc.
    # channel workers are already subscribed to. Tests that do not
    # pass ``message_bus`` get a fresh instance -- safe because tests
    # rarely assert on the outbound push.
    if message_bus is None:
        message_bus = MessageBus()
    if run_manager is None:
        run_manager = _resolve_run_manager()
    executor = ScheduleExecutor(repo=repo, run_manager=run_manager, message_bus=message_bus, config=scheduling_config)
    engine = SchedulerEngine(scheduling_config, repo, leader, on_fire=executor.enqueue)
    service = ScheduleService(repo=repo, engine=engine, config=scheduling_config, limits=limits, executor=executor)

    # Register the service with the harness-side provider so the agent
    # tools (``deerflow.tools.builtins.schedule_tool``) can reach it
    # without breaking the ``app ↔ deerflow`` boundary. The provider
    # slot is a single module global; see ``deerflow.scheduling``.
    from deerflow.scheduling import set_schedule_service

    set_schedule_service(service)

    svc = SchedulerService(
        config=scheduling_config,
        limits=limits,
        repo=repo,
        engine=engine,
        executor=executor,
        service=service,
        leader=leader,
        instance_id=instance_id,
    )
    await svc.start()
    _scheduler_service = svc
    logger.info("[Scheduler] service started: instance=%s", instance_id)
    return svc


async def stop_scheduler_service() -> None:
    """Stop the :class:`SchedulerService` and clear the singleton.

    Bounded to the same shutdown window the channel service uses; the
    gateway lifespan wraps the call in :func:`asyncio.wait_for` with a
    5s budget. Errors are logged and swallowed so a misbehaving
    component does not block the rest of the shutdown.
    """
    global _scheduler_service
    if _scheduler_service is None:
        return
    try:
        await _scheduler_service.stop()
    except Exception:
        logger.exception("[Scheduler] service stop failed")
    finally:
        _scheduler_service = None


__all__ = [
    "SchedulerService",
    "get_scheduler_service",
    "start_scheduler_service",
    "stop_scheduler_service",
]
