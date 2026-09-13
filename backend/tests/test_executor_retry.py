"""Tests for the schedule executor (Task 7).

Covers the wiring between :class:`ScheduleRepository`, the
:class:`RunManager` (the langgraph runtime), and the chat-side
:class:`MessageBus` (the chat-level pushback path used when
``OutboundMessage.thread_id is None``).

The executor is the simplest thing that satisfies the plan:

- ``enqueue(schedule_id, subscriber_user_id)`` calls ``run_one`` in the
  caller's task. No worker pool, no per-(schedule, subscriber) queue.
- ``start()`` and ``stop()`` are no-ops.
- The "per-subscriber" loop is dropped for MVP: the executor always
  uses the schedule's own ``target_json`` for the push target. The
  test in the brief doesn't exercise per-subscriber iteration.
- The push goes through ``MessageBus.publish_outbound`` with an
  :class:`OutboundMessage` whose ``thread_id`` is ``None`` so the
  chat-level path (Task 5) handles it.

The run lifecycle is mocked: ``run_manager.create_or_reject`` returns
a stub ``RunRecord`` with a ``run_id``; whether the run "succeeds" or
"fails" is decided by the test fixture. This keeps the test free of
the langgraph runtime.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.scheduling.audit import audit_schedule_event
from app.scheduling.executor import ScheduleExecutor
from app.scheduling.observability import Metrics
from deerflow.config.scheduling import SchedulingConfig
from deerflow.persistence.models.schedule import (
    Schedule,
    ScheduleKind,
    ScheduleRunStatus,
    ScheduleStatus,
)
from deerflow.persistence.schedule_repo import ScheduleRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'exec.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    yield ScheduleRepository(get_session_factory())
    await close_engine()


@pytest.fixture
def config() -> SchedulingConfig:
    return SchedulingConfig()


def _make_run_record(run_id: str = "run-1") -> MagicMock:
    """A duck-type :class:`RunRecord` carrying a ``run_id``."""
    rec = MagicMock()
    rec.run_id = run_id
    rec.thread_id = "t-1"
    return rec


@pytest.fixture
def run_manager() -> AsyncMock:
    """``RunManager`` stub: ``create_or_reject`` returns a fake record."""
    rm = MagicMock()
    rm.create_or_reject = AsyncMock(return_value=_make_run_record())
    return rm


@pytest.fixture
def bus() -> AsyncMock:
    """``MessageBus`` stub: ``publish_outbound`` is async."""
    b = MagicMock()
    b.publish_outbound = AsyncMock()
    return b


@pytest_asyncio.fixture
async def active_schedule(repo) -> Schedule:
    return await repo.create(
        title="Daily",
        owner_user_id="u1",
        kind=ScheduleKind.CRON,
        cron_expr="0 9 * * *",
        prompt="summarise",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )


@pytest.fixture
def executor(repo, run_manager, bus, config) -> ScheduleExecutor:
    return ScheduleExecutor(
        repo=repo,
        run_manager=run_manager,
        message_bus=bus,
        config=config,
    )


# ---------------------------------------------------------------------------
# Audit + Metrics (sync)
# ---------------------------------------------------------------------------


def test_audit_schedule_event_emits_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.audit"):
        audit_schedule_event("schedule.create", schedule_id="abc", kind="cron")
    # At least one record was emitted under the audit logger.
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.audit"]
    assert msgs
    assert any("schedule.create" in m for m in msgs)
    assert any("schedule_id=abc" in m for m in msgs)


def test_metrics_fire_emits_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.observability"):
        Metrics.fire("sched-1", kind="cron", status="ok")
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.observability"]
    assert msgs
    assert any("fire" in m for m in msgs)


def test_metrics_retry_emits_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.observability"):
        Metrics.retry(attempt=2)
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.observability"]
    assert any("retry" in m for m in msgs)


def test_metrics_push_failure_emits_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.observability"):
        Metrics.push_failure(reason="bus down")
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.observability"]
    assert any("push_failure" in m for m in msgs)


def test_metrics_run_duration_emits_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.observability"):
        Metrics.run_duration(kind="cron", seconds=1.23)
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.observability"]
    assert any("run_duration" in m for m in msgs)


# ---------------------------------------------------------------------------
# Executor lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_and_stop_are_noops(executor):
    # Should not raise.
    await executor.start()
    await executor.stop()


# ---------------------------------------------------------------------------
# run_one: success path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_enqueue_dispatches_to_run_one(executor, bus, active_schedule):
    """``enqueue`` is the fire-and-forget entry point. The simplest path
    is: just call ``run_one`` synchronously in the caller's task.
    """
    await executor.enqueue(active_schedule.id, subscriber_user_id="u1")
    # The bus received exactly one push (success).
    assert bus.publish_outbound.await_count == 1
    msg = bus.publish_outbound.await_args.args[0]
    # thread_id is None -- the chat-level path is used.
    assert msg.thread_id is None
    # success header (📅)
    assert "📅" in msg.text


@pytest.mark.anyio
async def test_run_one_success_creates_run_and_pushes(executor, bus, run_manager, repo, active_schedule):
    """Happy path: schedule is active, run is created, the executor
    trusts the run (no real langgraph), pushes the formatted success
    message via the bus, and marks the run row as SUCCEEDED.
    """
    run_id = "r-success-1"
    run_manager.create_or_reject.return_value = _make_run_record(run_id=run_id)
    await executor.run_one(active_schedule.id, subscriber_user_id="u1")
    # run_manager.create_or_reject was called.
    run_manager.create_or_reject.assert_awaited()
    # A run row was created in the repo, then marked SUCCEEDED.
    runs = await repo.list_runs(active_schedule.id, viewer_user_id="u1", limit=10)
    assert len(runs) == 1
    assert runs[0].status == ScheduleRunStatus.SUCCEEDED
    assert runs[0].run_id == run_id
    # The bus received one push.
    assert bus.publish_outbound.await_count == 1
    msg = bus.publish_outbound.await_args.args[0]
    # success format
    assert "📅" in msg.text
    assert "尝试 1" in msg.text
    # OutboundMessage is chat-level: thread_id is None.
    assert msg.thread_id is None
    # channel_name and chat_id come from the schedule's target_json.
    assert msg.channel_name == "feishu"
    assert msg.chat_id == "oc_1"


# ---------------------------------------------------------------------------
# run_one: skip if not active
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_one_skips_paused_schedule(executor, bus, run_manager, repo):
    """If the schedule has been paused/deleted, ``run_one`` is a no-op:
    no run is created, nothing is pushed."""
    s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.CRON,
        cron_expr="0 9 * * *",
        prompt="x",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    await repo.set_status(s.id, ScheduleStatus.PAUSED)
    await executor.run_one(s.id, subscriber_user_id="u1")
    run_manager.create_or_reject.assert_not_awaited()
    bus.publish_outbound.assert_not_awaited()


@pytest.mark.anyio
async def test_run_one_skips_missing_schedule(executor, bus, run_manager):
    """Unknown schedule id: no run, no push."""
    await executor.run_one("nonexistent", subscriber_user_id="u1")
    run_manager.create_or_reject.assert_not_awaited()
    bus.publish_outbound.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_one: retry path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_one_retries_on_run_manager_conflict(executor, bus, run_manager, repo, active_schedule):
    """When ``run_manager.create_or_reject`` raises (conflict / rejected),
    the executor marks the run row as FAILED with ``error_summary="run_rejected"``
    and pushes a single ❌ alert. (No retry path on the "create" failure --
    rejection from langgraph means the thread already has a run, so the
    next fire is the right thing to do.)
    """
    run_manager.create_or_reject.side_effect = RuntimeError("Thread already has an active run")
    await executor.run_one(active_schedule.id, subscriber_user_id="u1")
    runs = await repo.list_runs(active_schedule.id, viewer_user_id="u1", limit=10)
    assert len(runs) == 1
    assert runs[0].status == ScheduleRunStatus.FAILED
    assert "run_rejected" in (runs[0].error_summary or "")
    # Single push (the failure alert).
    assert bus.publish_outbound.await_count == 1
    msg = bus.publish_outbound.await_args.args[0]
    assert "❌" in msg.text


@pytest.mark.anyio
async def test_run_one_retries_on_publish_failure(executor, bus, run_manager, repo, active_schedule):
    """If ``bus.publish_outbound`` raises, the run is marked FAILED and a
    ``push_failure`` metric is recorded. The test mocks the bus to raise
    on the first call.
    """
    bus.publish_outbound.side_effect = RuntimeError("bus down")
    await executor.run_one(active_schedule.id, subscriber_user_id="u1")
    # The run row exists, marked FAILED with the bus error.
    runs = await repo.list_runs(active_schedule.id, viewer_user_id="u1", limit=10)
    assert len(runs) == 1
    assert runs[0].status == ScheduleRunStatus.FAILED
    assert "bus" in (runs[0].error_summary or "").lower()


# ---------------------------------------------------------------------------
# run_one: audit / metrics on every fire
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_one_emits_audit_line(executor, active_schedule, caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.audit"):
        await executor.run_one(active_schedule.id, subscriber_user_id="u1")
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.audit"]
    # At least one fire line was emitted.
    assert any("schedule.fire" in m for m in msgs)


@pytest.mark.anyio
async def test_run_one_emits_metrics_line(executor, active_schedule, caplog):
    with caplog.at_level(logging.INFO, logger="app.scheduling.observability"):
        await executor.run_one(active_schedule.id, subscriber_user_id="u1")
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.scheduling.observability"]
    # At least one fire line.
    assert any("fire" in m for m in msgs)
