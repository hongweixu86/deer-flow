"""Tests for app.scheduling.leader.LeaderLock (single-instance leader lock).

TDD: written before implementation. These tests pin the contract used by the
``SchedulerEngine`` (Task 4) and the multi-replica acceptance suite
(spec §18.6, exercised by Task 10).

The leader lock must guarantee that, in a multi-Gateway deployment, only
one replica reports ``is_leader == True`` against the shared persistence
backend. The brief scopes us to the SQLite path in unit tests; the
Postgres path is covered by a dialect-dispatch test plus a
``pg_try_advisory_lock`` arg-shape test.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def factory_pair(tmp_path):
    """Yield ``(init_engine, get_session_factory, close_engine)`` and tear
    down the engine at the end so each test gets a fresh, isolated DB.

    Mirrors the pattern used by ``test_schedule_repo.py`` and
    ``test_channel_connections_repository.py`` -- the engine is a
    *process-wide* singleton, so we have to spin up / tear down around
    every test to avoid cross-test contamination of the
    ``scheduler_leader`` table.
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'leaders.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield (init_engine, get_session_factory, close_engine)
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# SQLite path -- the two tests from the brief
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_wins_second_loses(factory_pair):
    """Two ``LeaderLock`` instances pointing at the same DB: the first
    call to ``try_acquire`` returns True; the second returns False. After
    the first releases, the second can take the lock."""
    _init_engine, get_session_factory, _close = factory_pair
    from app.scheduling.leader import LeaderLock

    a = LeaderLock(instance_id="a", session_factory=get_session_factory())
    b = LeaderLock(instance_id="b", session_factory=get_session_factory())
    assert await a.try_acquire() is True
    assert a.is_leader is True
    assert await b.try_acquire() is False
    assert b.is_leader is False
    await a.release()
    assert await b.try_acquire() is True


@pytest.mark.asyncio
async def test_release_then_other_can_acquire(factory_pair):
    """After ``a`` releases, ``b`` can take the lock in the same session."""
    _init_engine, get_session_factory, _close = factory_pair
    from app.scheduling.leader import LeaderLock

    a = LeaderLock(instance_id="a", session_factory=get_session_factory())
    b = LeaderLock(instance_id="b", session_factory=get_session_factory())
    assert await a.try_acquire() is True
    await a.release()
    assert a.is_leader is False
    assert await b.try_acquire() is True
    assert b.is_leader is True


# ---------------------------------------------------------------------------
# Extra coverage (still SQLite) -- pin the contract the SchedulerEngine
# and the multi-replica acceptance suite will rely on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_instance_is_not_leader(factory_pair):
    """A freshly-constructed ``LeaderLock`` must report ``is_leader ==
    False`` until ``try_acquire`` succeeds. The spec says: 'A fresh
    ``LeaderLock`` is ``is_leader == False`` until ``try_acquire``
    succeeds.'"""
    _init_engine, get_session_factory, _close = factory_pair
    from app.scheduling.leader import LeaderLock

    lock = LeaderLock(instance_id="x", session_factory=get_session_factory())
    assert lock.is_leader is False


@pytest.mark.asyncio
async def test_same_instance_can_reacquire(factory_pair):
    """Re-calling ``try_acquire`` from the same instance (e.g. after a
    health-check tick) must not lose the lock just because the row
    already says our name."""
    _init_engine, get_session_factory, _close = factory_pair
    from app.scheduling.leader import LeaderLock

    a = LeaderLock(instance_id="a", session_factory=get_session_factory())
    assert await a.try_acquire() is True
    # The spec acceptance says 'A fresh LeaderLock is is_leader == False
    # until try_acquire succeeds' -- it does NOT require try_acquire to
    # be a one-shot. The scheduler engine will call it once at startup
    # but defensive re-acquires should not flip is_leader back to False.
    assert await a.try_acquire() is True
    assert a.is_leader is True
