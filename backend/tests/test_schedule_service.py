"""Tests for the scheduling business layer.

Covers two surfaces:

1. :func:`app.scheduling.formatting.format_push_message` — pure sync
   function that turns a run result into the chat text pushed to
   Feishu / IM channels. No DB. Plain pytest.

2. :class:`app.scheduling.service.ScheduleService` — the async
   orchestrator that mediates between :class:`ScheduleRepository`,
   :class:`SchedulerEngine`, and the per-user limits. The executor
   (Task 7) is **not** wired in this task; the service only touches
   the repo + the engine.

The tests use an in-memory SQLite DB and a stub engine. The
``@pytest.mark.anyio`` marker is used for the async service tests
per the project's default convention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from app.scheduling.formatting import format_push_message
from app.scheduling.service import ScheduleService
from deerflow.config.scheduling import LimitsConfig, SchedulingConfig
from deerflow.persistence.models.schedule import (
    Schedule,
    ScheduleKind,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleStatus,
)
from deerflow.persistence.schedule_repo import ScheduleRepository

# ---------------------------------------------------------------------------
# Pure sync tests: format_push_message
# ---------------------------------------------------------------------------


class _FakeSchedule:
    """Minimal duck-type for a Schedule; only `title` is read."""

    def __init__(self, title: str = "Daily") -> None:
        self.title = title


def test_format_push_message_success_starts_with_calendar_emoji():
    s = _FakeSchedule(title="Daily Report")
    out = format_push_message(s, attempt=1, status="success", body="hello", run_url=None, max_length=4000)
    assert out.startswith("📅 Daily Report")
    assert "hello" in out


def test_format_push_message_success_includes_attempt_count():
    s = _FakeSchedule(title="t")
    out = format_push_message(s, attempt=2, status="success", body="x", run_url=None, max_length=4000)
    assert "尝试 2" in out


def test_format_push_message_retry_uses_retry_emoji():
    s = _FakeSchedule(title="Daily")
    out = format_push_message(s, attempt=2, status="retry", body="body", run_url=None, max_length=4000)
    assert out.startswith("🔄")
    assert "重试" in out
    assert "Daily" in out
    assert "body" in out


def test_format_push_message_failed_uses_cross_emoji_and_reason():
    s = _FakeSchedule(title="Daily")
    out = format_push_message(s, attempt=3, status="failed", body="oops", run_url="https://x", max_length=4000)
    assert out.startswith("❌")
    assert "oops" in out
    # Failed: run_url is appended as a link
    assert "https://x" in out


def test_format_push_message_no_status_falls_back_to_generic():
    s = _FakeSchedule(title="Daily")
    out = format_push_message(s, attempt=1, status=None, body="body", run_url=None, max_length=4000)
    assert out.startswith("📅 Daily")
    assert "body" in out


def test_format_push_message_truncates_when_over_max_length():
    s = _FakeSchedule(title="t")
    big_body = "x" * 5000
    out = format_push_message(s, attempt=1, status="success", body=big_body, run_url=None, max_length=200)
    assert len(out) <= 200
    assert "截断" in out


def test_format_push_message_preserves_run_url_when_short_enough():
    s = _FakeSchedule(title="t")
    out = format_push_message(s, attempt=1, status="success", body="x", run_url="https://run/1", max_length=4000)
    assert "https://run/1" in out


# ---------------------------------------------------------------------------
# Static helper tests
# ---------------------------------------------------------------------------


def test_validate_cron_ok():
    # Should not raise.
    ScheduleService.validate_cron("0 9 * * *")


def test_validate_cron_bad_raises_value_error():
    with pytest.raises(ValueError):
        ScheduleService.validate_cron("99 99 99 99 99")


def test_compute_next_retry_first_attempt():
    cfg = SchedulingConfig()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    nxt = ScheduleService.compute_next_retry(attempt=1, now=now, config=cfg)
    assert nxt == now + timedelta(seconds=60)


def test_compute_next_retry_second_attempt():
    cfg = SchedulingConfig()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    nxt = ScheduleService.compute_next_retry(attempt=2, now=now, config=cfg)
    assert nxt == now + timedelta(seconds=300)


def test_compute_next_retry_past_max_returns_none():
    cfg = SchedulingConfig()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert ScheduleService.compute_next_retry(attempt=4, now=now, config=cfg) is None


# ---------------------------------------------------------------------------
# Async service tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path):
    """Spin up an in-memory SQLite repo for service tests."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'service.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    yield ScheduleRepository(get_session_factory())
    await close_engine()


@pytest.fixture
def engine_stub():
    """A stub SchedulerEngine that records add/remove calls.

    The real engine wraps APScheduler; the service only needs
    ``add_schedule(schedule)`` and ``remove_schedule(schedule_id)``.
    """
    eng = MagicMock()
    eng.add_schedule = MagicMock()
    eng.remove_schedule = MagicMock()
    return eng


@pytest.fixture
def service(repo, engine_stub) -> ScheduleService:
    return ScheduleService(
        repo=repo,
        engine=engine_stub,
        config=SchedulingConfig(),
        limits=LimitsConfig(max_active_schedules_per_user=2),
    )


@pytest.mark.anyio
async def test_create_validates_cron_and_persists(service, engine_stub):
    payload = {
        "title": "Daily",
        "kind": ScheduleKind.CRON,
        "cron_expr": "0 9 * * *",
        "prompt": "summarise",
        "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
    }
    s = await service.create(payload=payload, current_user="u1")
    assert s.id is not None
    assert s.owner_user_id == "u1"
    assert s.status == ScheduleStatus.ACTIVE
    # Engine was asked to register the new schedule.
    assert engine_stub.add_schedule.call_count == 1
    assert engine_stub.add_schedule.call_args[0][0].id == s.id


@pytest.mark.anyio
async def test_create_rejects_bad_cron(service, engine_stub):
    payload = {
        "title": "Daily",
        "kind": ScheduleKind.CRON,
        "cron_expr": "99 99 99 99 99",
        "prompt": "x",
        "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
    }
    with pytest.raises(ValueError):
        await service.create(payload=payload, current_user="u1")
    # Nothing was registered with the engine.
    assert engine_stub.add_schedule.call_count == 0


@pytest.mark.anyio
async def test_create_enforces_max_active_schedules_per_user(service, engine_stub):
    payload = {
        "title": "t",
        "kind": ScheduleKind.CRON,
        "cron_expr": "0 9 * * *",
        "prompt": "x",
        "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
    }
    await service.create(payload=payload, current_user="u1")
    await service.create(payload=dict(payload, cron_expr="0 10 * * *"), current_user="u1")
    # The 3rd create should be rejected (limit = 2).
    with pytest.raises(PermissionError):
        await service.create(payload=dict(payload, cron_expr="0 11 * * *"), current_user="u1")
    # The two successful creates registered jobs; the rejected one did not.
    assert engine_stub.add_schedule.call_count == 2


@pytest.mark.anyio
async def test_pause_owner_only(service):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    # Non-owner cannot pause.
    with pytest.raises(PermissionError):
        await service.pause(s.id, current_user="u2")
    # Owner pauses.
    await service.pause(s.id, current_user="u1")
    refreshed = await service.get_for_viewer(s.id, viewer="u1")
    assert refreshed is not None
    assert refreshed.status == ScheduleStatus.PAUSED


@pytest.mark.anyio
async def test_pause_removes_from_engine(service, engine_stub):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    engine_stub.remove_schedule.reset_mock()
    await service.pause(s.id, current_user="u1")
    assert engine_stub.remove_schedule.call_count == 1
    assert engine_stub.remove_schedule.call_args[0][0] == s.id


@pytest.mark.anyio
async def test_resume_re_registers_with_engine(service, engine_stub):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    await service.pause(s.id, current_user="u1")
    engine_stub.add_schedule.reset_mock()
    await service.resume(s.id, current_user="u1")
    assert engine_stub.add_schedule.call_count == 1
    refreshed = await service.get_for_viewer(s.id, viewer="u1")
    assert refreshed is not None
    assert refreshed.status == ScheduleStatus.ACTIVE


@pytest.mark.anyio
async def test_soft_delete_owner_only_and_removes_from_engine(service, engine_stub):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    # Non-owner cannot delete.
    with pytest.raises(PermissionError):
        await service.soft_delete(s.id, current_user="u2")
    # Owner deletes.
    engine_stub.remove_schedule.reset_mock()
    await service.soft_delete(s.id, current_user="u1")
    assert engine_stub.remove_schedule.call_count == 1
    # get_for_viewer still returns the row (soft delete only flips status).
    refreshed = await service.get_for_viewer(s.id, viewer="u1")
    assert refreshed is not None
    assert refreshed.status == ScheduleStatus.DELETED


@pytest.mark.anyio
async def test_update_rejects_owner_pivot(service):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    # Cannot pivot ownership.
    with pytest.raises(ValueError):
        await service.update(s.id, fields={"owner_user_id": "u2"}, current_user="u1")
    # Cannot change source.
    with pytest.raises(ValueError):
        await service.update(s.id, fields={"source": "agent"}, current_user="u1")
    # Non-owner cannot update.
    with pytest.raises(PermissionError):
        await service.update(s.id, fields={"title": "hijack"}, current_user="u2")
    # Owner can change title.
    out = await service.update(s.id, fields={"title": "new"}, current_user="u1")
    assert out.title == "new"


@pytest.mark.anyio
async def test_subscribe_and_unsubscribe_delegate_to_repo(service):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    await service.subscribe(s.id, user_id="u2", target_json='{"channel":"feishu","chat_id":"oc_2"}')
    # u2 can now see the schedule.
    assert await service.get_for_viewer(s.id, viewer="u2") is not None
    # Unsubscribe.
    await service.unsubscribe(s.id, user_id="u2")
    # u2 can no longer see the schedule.
    assert await service.get_for_viewer(s.id, viewer="u2") is None


@pytest.mark.anyio
async def test_list_for_viewer_delegates_to_repo(service):
    await service.create(
        payload={
            "title": "t1",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    await service.create(
        payload={
            "title": "t2",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 10 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u2",
    )
    # u1 sees only their own under scope=mine
    mine = await service.list_for_viewer(viewer="u1", scope="mine", status=None, limit=10)
    assert len(mine) == 1
    assert mine[0].title == "t1"


@pytest.mark.anyio
async def test_list_runs_for_viewer_visibility(service):
    s = await service.create(
        payload={
            "title": "t",
            "kind": ScheduleKind.CRON,
            "cron_expr": "0 9 * * *",
            "prompt": "x",
            "target_json": '{"channel":"feishu","chat_id":"oc_1"}',
        },
        current_user="u1",
    )
    run = await service._repo.create_run(s.id, subscriber_user_id="u1", attempt=1)  # type: ignore[attr-defined]
    assert run.id is not None
    # Owner sees the run.
    runs = await service.list_runs(s.id, viewer="u1", limit=10)
    assert any(r.id == run.id for r in runs)
