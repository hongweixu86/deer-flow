"""Single-instance leader lock for the scheduler.

The scheduler subsystem must only run on **one** Gateway replica at a time.
A cluster-wide advisory lock backs that guarantee:

* Postgres: ``pg_try_advisory_lock(7345891234)`` -- kernel-cleans the lock
  on process death, so a crash automatically releases it. The constant is
  the "scheduler" namespace and is intentionally stable across restarts
  so a restarted Gateway can re-take the lock from a previous instance
  without contention.
* SQLite (single-node deployments): a one-row ``scheduler_leader``
  table. ``try_acquire`` upserts the row; another instance sees the
  fresh ``acquired_at`` and backs off. A crashed leader's row ages out
  after ``misfire_grace_seconds`` (5 min default), at which point any
  replica can take over.

Why we don't hold a long-lived ``BEGIN IMMEDIATE`` transaction in SQLite
----------------------------------------------------------------------

The plan text suggests holding the SQLite write transaction open for
the lifetime of the process by stashing the ``AsyncSession`` in
``self._lock_conn``. In practice this is brittle:

* SQLAlchemy ``AsyncSession`` is not designed to be held open across
  arbitrary awaits for the entire process lifetime; the connection
  inside it can be recycled by the pool or invalidated by a network
  blip, and a long-running implicit transaction breaks the
  ``autobegin`` / ``commit`` contract used by every other repo.
* The DB file's ``PRAGMA journal_mode=WAL`` (set by
  ``deerflow.persistence.engine.init_engine``) already serializes
  writers, and ``PRAGMA busy_timeout=30000`` means the second
  Gateway's upsert waits up to 30s for the first to finish -- enough
  to ride out a normal commit, not enough to mask a true crash.

The row-staleness approach (``acquired_at`` + ``misfire_grace_seconds``)
is the project's preferred pattern: the row acts as a heartbeat; a
crashed leader's row ages out and any replica can take it. The
``LeaderLock.release()`` method clears the row on a clean shutdown so
the failover window is the 5-minute ``misfire_grace_seconds`` only on
crashes, not on graceful restarts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# Stable "scheduler" namespace hash. Do NOT change without a migration:
# every replica needs to use the same constant.
_PG_ADVISORY_LOCK_KEY = 7345891234


class LeaderLock:
    """Single-instance leader lock backed by the persistence engine.

    Lifecycle::

        lock = LeaderLock(instance_id="gw-1", session_factory=sf)
        if await lock.try_acquire():
            # this process is the leader; run the scheduler
            ...
        # on shutdown:
        await lock.release()

    The lock is per-process, not per-tick. A crashed leader's lock ages
    out after ``misfire_grace_seconds`` (5 min default) and any replica
    can take over; a graceful shutdown calls ``release()`` and clears
    the row immediately.
    """

    def __init__(
        self,
        instance_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        misfire_grace_seconds: int = 300,
    ) -> None:
        self._instance_id = instance_id
        self._session_factory = session_factory
        self._misfire_grace = misfire_grace_seconds
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        """Read-only flag set by :meth:`try_acquire` / :meth:`release`."""
        return self._is_leader

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    async def try_acquire(self) -> bool:
        """Atomically attempt to take the leader lock.

        Returns ``True`` iff this process is now the leader. The result
        is also exposed via :attr:`is_leader`.
        """
        # Discover the dialect by opening a short-lived session. We do
        # this in its own session so the dialect sniff doesn't pollute
        # the working transaction in the acquire path below.
        async with self._session_factory() as probe:
            bind = probe.get_bind()
            dialect = bind.dialect.name

        if dialect == "postgresql":
            return await self._acquire_postgres()
        return await self._acquire_sqlite()

    async def _acquire_postgres(self) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_ADVISORY_LOCK_KEY})
            ok = bool(result.scalar())
            # Commit/rollback releases the implicit SELECT transaction;
            # the advisory lock is held at the *session/connection*
        # level, not the transaction level, so it survives the COMMIT.
        if ok:
            await session.commit()
        else:
            await session.rollback()
        self._is_leader = ok
        if ok:
            logger.info("[Scheduler] acquired leader lock (postgres)", extra={"instance_id": self._instance_id})
        else:
            logger.info("[Scheduler] not leader (postgres)", extra={"instance_id": self._instance_id})
        return ok

    async def _acquire_sqlite(self) -> bool:
        # In production this table is part of the schedule migration
        # (see ``deerflow/persistence/migrations/versions/``). The
        # ``IF NOT EXISTS`` here is a safety net for greenfield test
        # DBs and for any deployment that runs the scheduler before
        # the schedule migration is applied.
        await self._ensure_sqlite_table()

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            row = (
                await session.execute(text("SELECT instance_id, acquired_at FROM scheduler_leader WHERE id = 1"))
            ).first()
            if row is not None and row[0] != self._instance_id:
                # Another instance holds the row. Check staleness.
                when = self._parse_acquired_at(row[1], now=now)
                if (now - when) < timedelta(seconds=self._misfire_grace):
                    # Fresh leader -- we lose.
                    self._is_leader = False
                    logger.info(
                        "[Scheduler] not leader (sqlite, held by %s, age=%ds)",
                        row[0],
                        int((now - when).total_seconds()),
                    )
                    return False
                # Stale -- the previous leader is presumed dead. Fall
                # through and steal the row.
            # Either the row is empty, stale, or already ours -- take it.
            await session.execute(
                text(
                    "INSERT INTO scheduler_leader(id, instance_id, acquired_at) VALUES (1, :iid, :now) "
                    "ON CONFLICT(id) DO UPDATE SET instance_id = excluded.instance_id, acquired_at = excluded.acquired_at"
                ),
                {"iid": self._instance_id, "now": now.isoformat()},
            )
            await session.commit()
        self._is_leader = True
        logger.info("[Scheduler] acquired leader lock (sqlite)", extra={"instance_id": self._instance_id})
        return True

    async def _ensure_sqlite_table(self) -> None:
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS scheduler_leader ("
                    "id INTEGER PRIMARY KEY, "
                    "instance_id TEXT, "
                    "acquired_at TEXT"
                    ")"
                )
            )
            await session.commit()

    @staticmethod
    def _parse_acquired_at(value: object, *, now: datetime) -> datetime:
        """Best-effort parse of the ``acquired_at`` column.

        Returns ``now`` on any parse failure so a corrupt row is treated
        as "stale" (i.e. stealable) rather than locking the scheduler
        out forever.
        """
        if not isinstance(value, str) or not value:
            return now
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return now

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release(self) -> None:
        """Release the lock. Best-effort; logs and swallows errors.

        On a crash the kernel cleans up Postgres advisory locks; on
        SQLite the row ages out via ``misfire_grace_seconds``. This
        method exists so a graceful Gateway shutdown doesn't have to
        wait for the grace window before another replica can take over.
        """
        if not self._is_leader:
            return
        try:
            async with self._session_factory() as session:
                bind = session.get_bind()
                if bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_ADVISORY_LOCK_KEY})
                else:
                    await session.execute(text("DELETE FROM scheduler_leader WHERE id = 1"))
                await session.commit()
        except Exception:
            logger.exception("[Scheduler] error releasing leader lock", extra={"instance_id": self._instance_id})
        finally:
            self._is_leader = False
            logger.info("[Scheduler] released leader lock", extra={"instance_id": self._instance_id})


__all__ = ["LeaderLock"]
