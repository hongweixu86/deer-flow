"""Tests for :class:`app.scheduling.scheduler.SchedulerEngine`.

TDD: written before the implementation. The scheduler engine wraps
APScheduler so that a leader Gateway replica recovers its scheduled
jobs after a process restart by reading ``status=active`` rows from
``ScheduleRepository``. The fire callback is **injected** by the
executor (Task 7); this task only verifies the recovery / lifecycle
contract.

What we pin here
----------------

1. ``start()`` is a no-op when the engine is not the leader.
2. ``start()`` on a leader builds an ``AsyncIOScheduler``, loads every
   ``ACTIVE`` schedule, and ``list_due(now)`` returns the id of a
   one-shot whose ``run_at`` is in the past.
3. ``stop()`` shuts the scheduler down cleanly so the engine can be
   re-used.
4. ``add_schedule`` / ``remove_schedule`` keep the in-memory jobstore
   in sync; ``trigger_now`` schedules an immediate fire.
5. The injected ``on_fire`` callback is awaited with the schedule id.

These cover the acceptance criteria the brief calls out, plus the
extras downstream tasks will rely on (in particular the
``add_schedule`` after-restart path used by the REST router when a
user creates a new schedule).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

# The async engine's aiosqlite worker thread can race with the test
# loop's teardown: when the sync SQLAlchemyJobStore writes a job
# pickle and the async engine reads it back, an in-flight result
# may try to deliver on a closed loop. The warning is benign (the
# DB is about to be GC'd into ``tmp_path``), so we filter it at
# the module level. ``pytestmark`` is applied to every test in
# the module.
pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning"),
]

# Module-level recorder used by the on-fire test. APScheduler's
# SQLAlchemyJobStore pickles the job target so it can survive a
# process restart, which means a closure (a test-local async
# function) cannot be used -- APScheduler rejects it with
# ``ValueError: This Job cannot be serialized``. The recorder is
# therefore a module-level dict; tests ``clear()`` it before use.
_FIRE_RECORDED: list[str] = []


async def _record_on_fire(schedule_id: str) -> None:
    """Module-level on_fire used by the wiring test.

    Lives at module scope so APScheduler's jobstore can pickle the
    reference (a closure would be rejected).
    """
    _FIRE_RECORDED.append(schedule_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def factory_pair(tmp_path):
    """Yield ``(get_session_factory,)`` and tear down the engine at the
    end of each test.

    The persistence engine is a process-wide singleton, so every test
    must spin it up / tear it down to avoid cross-test contamination
    of the ``schedules`` and ``apscheduler_jobs`` tables.
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield (get_session_factory,)
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# Acceptance: leader startup loads ACTIVE schedules
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_as_non_leader_is_noop(factory_pair):
    """If the engine does not hold the leader lock, ``start()`` must
    NOT start a scheduler and ``list_due()`` must be a safe no-op.

    This is the multi-replica safety net: a follower Gateway that
    receives the same lifespan call must not double-fire schedules.
    """
    (get_session_factory,) = factory_pair
    from app.scheduling.leader import LeaderLock
    from app.scheduling.scheduler import SchedulerEngine
    from deerflow.config.scheduling import SchedulingConfig
    from deerflow.persistence.schedule_repo import ScheduleRepository

    repo = ScheduleRepository(get_session_factory())
    # NB: we deliberately do NOT call ``try_acquire`` so ``is_leader``
    # stays False.
    leader = LeaderLock(instance_id="follower", session_factory=get_session_factory())
    eng = SchedulerEngine(SchedulingConfig(), repo, leader)
    await eng.start()
    try:
        # list_due on a never-started engine is a safe no-op.
        assert eng.list_due(datetime.now(UTC)) == []
    finally:
        await eng.stop()


@pytest.mark.anyio
async def test_start_loads_active_schedules(factory_pair):
    """Leader startup re-registers every ``status=active`` schedule.

    Covers the brief's primary acceptance: after a process restart
    the leader replica must rebuild the APScheduler job map from the
    database. We create two schedules (cron + one_shot) and assert
    that the one_shot's id appears in ``list_due`` once ``now`` is
    past its ``run_at``.
    """
    (get_session_factory,) = factory_pair
    from app.scheduling.leader import LeaderLock
    from app.scheduling.scheduler import SchedulerEngine
    from deerflow.config.scheduling import SchedulingConfig
    from deerflow.persistence.models.schedule import ScheduleKind
    from deerflow.persistence.schedule_repo import ScheduleRepository

    repo = ScheduleRepository(get_session_factory())
    cron_s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.CRON,
        cron_expr="0 9 * * *",
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    one_shot = await repo.create(
        title="t2",
        owner_user_id="u1",
        kind=ScheduleKind.ONE_SHOT,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )

    leader = LeaderLock(instance_id="leader-1", session_factory=get_session_factory())
    assert await leader.try_acquire() is True

    eng = SchedulerEngine(SchedulingConfig(), repo, leader)
    await eng.start()
    try:
        # Push "now" 2h into the future to make the one_shot due.
        # The cron is "0 9 * * *" in Asia/Shanghai -- we don't make
        # any claim about whether it is or isn't due at +2h, since
        # that depends on the wall clock the test happens to run at;
        # the brief's contract is that the job is *registered*, not
        # that it's due at a particular instant. The presence of the
        # one_shot in ``due`` proves the engine wired the recovery
        # path through to the scheduler.
        future = datetime.now(UTC) + timedelta(hours=2)
        due = eng.list_due(future)
        assert one_shot.id in due, f"one_shot {one_shot.id} not in due list {due!r}"
        # And the cron *is* registered (we can find it in the
        # scheduler's job list), even if we don't assert on its
        # due-ness.
        all_ids = {j.id for j in eng._sched.get_jobs()}  # type: ignore[union-attr]
        assert f"sched:{cron_s.id}" in all_ids
    finally:
        await eng.stop()
        await leader.release()


@pytest.mark.anyio
async def test_persists_apscheduler_job_id_and_next_fire(factory_pair):
    """After ``start()``, every recovered schedule has its
    ``apscheduler_job_id`` and ``next_fire_at`` written back to the DB.

    This is the recovery contract the REST router and the executor
    both depend on: if the leader restarts again, the next instance
    can re-read the rows with the same ``apscheduler_job_id`` so
    APScheduler's SQLAlchemy jobstore is consistent across the
    restart.
    """
    (get_session_factory,) = factory_pair
    from app.scheduling.leader import LeaderLock
    from app.scheduling.scheduler import SchedulerEngine
    from deerflow.config.scheduling import SchedulingConfig
    from deerflow.persistence.models.schedule import ScheduleKind
    from deerflow.persistence.schedule_repo import ScheduleRepository

    repo = ScheduleRepository(get_session_factory())
    s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.ONE_SHOT,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )

    leader = LeaderLock(instance_id="leader-2", session_factory=get_session_factory())
    assert await leader.try_acquire() is True

    eng = SchedulerEngine(SchedulingConfig(), repo, leader)
    await eng.start()
    try:
        refreshed = await repo.get(s.id)
        assert refreshed is not None
        assert refreshed.apscheduler_job_id == f"sched:{s.id}"
        assert refreshed.next_fire_at is not None
    finally:
        await eng.stop()
        await leader.release()


# ---------------------------------------------------------------------------
# Acceptance: add / remove / trigger_now on a running engine
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_add_and_remove_schedule(factory_pair):
    """``add_schedule`` registers a new job; ``remove_schedule`` takes
    it out. The engine's internal jobstore is the source of truth for
    in-flight triggers between DB writes.
    """
    (get_session_factory,) = factory_pair
    from app.scheduling.leader import LeaderLock
    from app.scheduling.scheduler import SchedulerEngine
    from deerflow.config.scheduling import SchedulingConfig
    from deerflow.persistence.models.schedule import ScheduleKind
    from deerflow.persistence.schedule_repo import ScheduleRepository

    repo = ScheduleRepository(get_session_factory())
    leader = LeaderLock(instance_id="leader-3", session_factory=get_session_factory())
    assert await leader.try_acquire() is True

    eng = SchedulerEngine(SchedulingConfig(), repo, leader)
    await eng.start()
    try:
        s = await repo.create(
            title="t",
            owner_user_id="u1",
            kind=ScheduleKind.ONE_SHOT,
            run_at=datetime.now(UTC) + timedelta(hours=5),
            prompt="p",
            target_json='{"channel":"feishu","chat_id":"oc_1"}',
        )
        eng.add_schedule(s)
        # job is registered
        assert any(j.id == f"sched:{s.id}" for j in eng._sched.get_jobs())  # type: ignore[union-attr]
        eng.remove_schedule(s.id)
        # job is gone
        assert not any(j.id == f"sched:{s.id}" for j in eng._sched.get_jobs())  # type: ignore[union-attr]
    finally:
        await eng.stop()
        await leader.release()


@pytest.mark.anyio
async def test_trigger_now_bumps_next_run_time(factory_pair):
    """``trigger_now`` forces the next fire to be in the past, so the
    job is immediately due on the next scheduler tick.
    """
    (get_session_factory,) = factory_pair
    from app.scheduling.leader import LeaderLock
    from app.scheduling.scheduler import SchedulerEngine
    from deerflow.config.scheduling import SchedulingConfig
    from deerflow.persistence.models.schedule import ScheduleKind
    from deerflow.persistence.schedule_repo import ScheduleRepository

    repo = ScheduleRepository(get_session_factory())
    leader = LeaderLock(instance_id="leader-4", session_factory=get_session_factory())
    assert await leader.try_acquire() is True

    eng = SchedulerEngine(SchedulingConfig(), repo, leader)
    await eng.start()
    try:
        s = await repo.create(
            title="t",
            owner_user_id="u1",
            kind=ScheduleKind.ONE_SHOT,
            run_at=datetime.now(UTC) + timedelta(hours=5),
            prompt="p",
            target_json='{"channel":"feishu","chat_id":"oc_1"}',
        )
        eng.add_schedule(s)
        before = datetime.now(UTC)
        eng.trigger_now(s.id)
        # The job is now due (next_run_time <= before).
        due = eng.list_due(before + timedelta(seconds=1))
        assert s.id in due
    finally:
        await eng.stop()
        await leader.release()


# ---------------------------------------------------------------------------
# Acceptance: on_fire callback wiring
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_on_fire_receives_schedule_id(factory_pair):
    """When a schedule fires, the injected ``on_fire`` callable is
    awaited with the schedule id. We verify this by wiring a
    recorder-style ``on_fire`` and triggering a job manually.

    APScheduler's :class:`SQLAlchemyJobStore` pickles the job
    target so it can survive a process restart -- which means the
    callback must be module-level (a closure would be rejected with
    ``ValueError: This Job cannot be serialized``). The recorder
    helper at the top of this module provides that.
    """
    _FIRE_RECORDED.clear()
    (get_session_factory,) = factory_pair
    from app.scheduling.leader import LeaderLock
    from app.scheduling.scheduler import SchedulerEngine
    from deerflow.config.scheduling import SchedulingConfig
    from deerflow.persistence.models.schedule import ScheduleKind
    from deerflow.persistence.schedule_repo import ScheduleRepository

    repo = ScheduleRepository(get_session_factory())
    leader = LeaderLock(instance_id="leader-5", session_factory=get_session_factory())
    assert await leader.try_acquire() is True

    eng = SchedulerEngine(SchedulingConfig(), repo, leader, on_fire=_record_on_fire)
    await eng.start()
    try:
        s = await repo.create(
            title="t",
            owner_user_id="u1",
            kind=ScheduleKind.ONE_SHOT,
            run_at=datetime.now(UTC) + timedelta(hours=1),
            prompt="p",
            target_json='{"channel":"feishu","chat_id":"oc_1"}',
        )
        eng.add_schedule(s)
        # The APScheduler AsyncIO scheduler jobs expose ``func`` which
        # was bound to ``_record_on_fire`` with ``[s.id]`` -- invoking
        # it directly simulates a fire without waiting for the
        # trigger.
        job = eng._sched.get_job(f"sched:{s.id}")  # type: ignore[union-attr]
        assert job is not None
        await job.func(*job.args)
        assert _FIRE_RECORDED == [s.id]
    finally:
        await eng.stop()
        await leader.release()
