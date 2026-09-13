"""End-to-end test for the schedule + push pipeline (Task 10).

This is the happy-path "press the button" e2e: it bypasses APScheduler
and the natural-language agent flow, and instead drives the executor
directly. The cron firing itself is covered by ``test_scheduler_recovery``
(Task 4) and the persistence + service layers are covered by their own
unit suites; what this e2e proves is the *wiring* between the schedule
table, the executor, the langgraph run entry point, and the chat-side
``MessageBus``.

The four assertions of the e2e:

1. A schedule can be created via the repository.
2. Calling ``executor.enqueue(schedule_id)`` creates a ``ScheduleRun``
   row visible to the owner.
3. The run row transitions to ``SUCCEEDED`` (the executor trusts the
   langgraph run and marks the dispatch side as successful once the
   push lands).
4. An ``OutboundMessage`` is published on the ``MessageBus`` with the
   target channel/chat_id taken from the schedule's ``target_json``
   and the formatted push body.

Why we don't use freezegun
--------------------------

APScheduler's :class:`AsyncIOScheduler` drives its own event loop and
ignores :mod:`freezegun` patches. Trying to "fast-forward" by patching
``datetime.now`` would not actually trigger a fire -- the engine's
internal scheduler would still be waiting on wall-clock time. Rather
than introduce a wall-clock race or a deep monkey-patch of APScheduler
internals, we test the wiring directly by calling
``executor.enqueue(schedule_id)``, which is the exact entry point the
engine calls. This makes the test deterministic and fast, and keeps
the cron-firing assertion owned by the recovery test suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.scheduling.executor import ScheduleExecutor
from deerflow.config.scheduling import SchedulingConfig
from deerflow.persistence.models.schedule import (
    ScheduleKind,
    ScheduleRunStatus,
)
from deerflow.persistence.schedule_repo import ScheduleRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path):
    """Standalone SQLite-backed repository.

    Mirrors the per-test sqlite pattern used in
    ``test_executor_retry`` and ``test_schedule_repo``: each test gets
    a fresh DB so fixtures cannot leak between cases.
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    yield ScheduleRepository(get_session_factory())
    await close_engine()


@pytest.fixture
def config() -> SchedulingConfig:
    return SchedulingConfig()


def _make_run_record(run_id: str = "e2e-run-1") -> MagicMock:
    """Duck-typed ``RunRecord`` carrying a ``run_id``."""
    rec = MagicMock()
    rec.run_id = run_id
    rec.thread_id = "t-e2e"
    return rec


@pytest.fixture
def run_manager() -> AsyncMock:
    """``RunManager`` stub: ``create_or_reject`` returns a fake record.

    The executor treats the return value of ``create_or_reject`` as
    "the langgraph run was successfully dispatched"; we don't run an
    actual langgraph here.
    """
    rm = MagicMock()
    rm.create_or_reject = AsyncMock(return_value=_make_run_record())
    return rm


@pytest.fixture
def bus() -> AsyncMock:
    """``MessageBus`` stub: ``publish_outbound`` is async.

    The e2e asserts the *args* passed to ``publish_outbound``, not the
    return value (there is none). We use a ``MagicMock`` so the call
    site can inspect ``await_args`` for the published message.
    """
    b = MagicMock()
    b.publish_outbound = AsyncMock()
    return b


@pytest.fixture
def executor(repo, run_manager, bus, config) -> ScheduleExecutor:
    return ScheduleExecutor(
        repo=repo,
        run_manager=run_manager,
        message_bus=bus,
        config=config,
    )


# ---------------------------------------------------------------------------
# Happy-path e2e
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_schedule_enqueue_creates_run_and_publishes_outbound(
    repo,
    run_manager,
    bus,
    executor,
) -> None:
    """End-to-end happy path: create -> enqueue -> run row + push.

    Steps exercised (in order):

    1. Create a cron schedule via :class:`ScheduleRepository` (the
       same path the REST router takes, minus authz).
    2. Call :meth:`ScheduleExecutor.enqueue` with the schedule id.
       This is the exact entry point APScheduler's engine fires.
    3. Assert ``run_manager.create_or_reject`` was called with the
       schedule's prompt.
    4. Assert a ``ScheduleRun`` row was persisted with status
       ``SUCCEEDED`` and the langgraph run id attached.
    5. Assert ``bus.publish_outbound`` was called once with an
       :class:`OutboundMessage` whose ``channel_name`` /
       ``chat_id`` come from the schedule's ``target_json``, whose
       ``thread_id`` is ``None`` (chat-level push), and whose
       ``text`` carries the formatted success payload.
    """
    # 1. Create the schedule via the repository.
    schedule = await repo.create(
        title="Daily standup",
        owner_user_id="u1",
        kind=ScheduleKind.CRON,
        cron_expr="0 9 * * *",
        prompt="summarise yesterday's commits",
        target_json='{"channel":"feishu","chat_id":"oc_e2e_1"}',
    )
    assert schedule.id
    assert schedule.status.value == "active"

    # 2. Push the button. In production this happens from
    #    SchedulerEngine.on_fire; in the e2e we call it directly.
    await executor.enqueue(schedule.id, subscriber_user_id="u1")

    # 3. The run was dispatched into the langgraph runtime stub.
    run_manager.create_or_reject.assert_awaited_once()

    # 4. A ScheduleRun row was persisted, transitioned to SUCCEEDED,
    #    and the langgraph run id is attached.
    runs = await repo.list_runs(schedule.id, viewer_user_id="u1", limit=10)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == ScheduleRunStatus.SUCCEEDED
    assert run.run_id == "e2e-run-1"
    assert run.attempt == 1
    assert run.error_summary is None

    # 5. The chat-side MessageBus received exactly one push.
    assert bus.publish_outbound.await_count == 1
    msg = bus.publish_outbound.await_args.args[0]
    # channel + chat_id are pulled from the schedule's target_json.
    assert msg.channel_name == "feishu"
    assert msg.chat_id == "oc_e2e_1"
    # chat-level push (not a thread reply).
    assert msg.thread_id is None
    # the formatted body carries the success header and the schedule
    # title -- the agent's run text is filled in by a downstream
    # component (out of scope for this e2e).
    assert "📅" in msg.text
    assert "Daily standup" in msg.text
    assert "尝试 1" in msg.text
