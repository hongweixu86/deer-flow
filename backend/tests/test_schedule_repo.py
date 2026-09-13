"""Tests for ScheduleRepository (SQLAlchemy-backed).

TDD: written before implementation. These tests pin the persistence
interface used by the scheduler engine, executor, REST router, and
agent tools in later tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deerflow.persistence.models.schedule import (
    ScheduleKind,
    ScheduleRunStatus,
    ScheduleStatus,
)


@pytest.fixture
async def repo(tmp_path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    yield ScheduleRepositoryFactory(get_session_factory())  # type: ignore[name-defined]
    await close_engine()


def ScheduleRepositoryFactory(*args, **kwargs):  # placeholder for IDEs; replaced at runtime
    from deerflow.persistence.schedule_repo import ScheduleRepository

    return ScheduleRepository(*args, **kwargs)


@pytest.mark.anyio
async def test_create_and_get(repo):
    s = await repo.create(
        title="daily",
        owner_user_id="u1",
        kind=ScheduleKind.CRON,
        cron_expr="0 9 * * *",
        prompt="summarise",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    assert s.id is not None
    fetched = await repo.get(s.id)
    assert fetched is not None
    assert fetched.title == "daily"
    assert fetched.owner_user_id == "u1"
    assert fetched.kind == ScheduleKind.CRON
    assert fetched.status == ScheduleStatus.ACTIVE


@pytest.mark.anyio
async def test_get_for_viewer_owner_sees(repo):
    s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.ONE_SHOT,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    assert await repo.get_for_viewer(s.id, "u1") is not None


@pytest.mark.anyio
async def test_get_for_viewer_other_returns_none(repo):
    """404-on-not-visible, never 403. Non-owner non-subscriber gets None.

    This is a security-relevant detail: the filter is applied at the SQL
    layer, not in Python post-filter, so the row never leaks to a viewer
    who should not see it.
    """
    s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.ONE_SHOT,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    assert await repo.get_for_viewer(s.id, "u2") is None


@pytest.mark.anyio
async def test_count_active_for_user(repo):
    s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.ONE_SHOT,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    assert await repo.count_active_for_user("u1") == 1
    await repo.soft_delete(s.id)
    assert await repo.count_active_for_user("u1") == 0


@pytest.mark.anyio
async def test_get_for_viewer_subscriber_sees(repo):
    """A subscriber (non-owner) must see the schedule."""
    s = await repo.create(
        title="t",
        owner_user_id="u1",
        kind=ScheduleKind.ONE_SHOT,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        prompt="p",
        target_json='{"channel":"feishu","chat_id":"oc_1"}',
    )
    await repo.subscribe(s.id, "u2")
    assert await repo.get_for_viewer(s.id, "u2") is not None
