"""ORM models for scheduled tasks (cron + one-shot).

Three tables:

- ``schedules``               -- the schedule definition (owner, prompt, target)
- ``schedule_subscriptions``  -- fan-out: a user receives the run result
- ``schedule_runs``           -- one row per execution attempt (queued ->
                                  running -> succeeded/failed/canceled)

The downstream scheduler engine, executor, REST router, and agent tools
all read/write these tables via :class:`ScheduleRepository`. ULID
primary keys (26 chars, Crockford base32, time-ordered) -- see
``_new_id`` for the stdlib implementation.
"""

from __future__ import annotations

import enum
import os
import time
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base

# Crockford base32 alphabet (no I, L, O, U) -- the canonical ULID
# encoding. Lowercase matches the reference python-ulid output and
# every public ULID library.
_ULID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
# Reverse lookup for decoding; ``0xFF`` is a sentinel for "invalid".
_ULID_DECODE = {ch: i for i, ch in enumerate(_ULID_ALPHABET)}
_ULID_DECODE.update({ch.upper(): i for i, ch in enumerate(_ULID_ALPHABET)})
# Monotonic seed: last (ms, rand) seen, used to guarantee strictly
# increasing ULIDs within the same millisecond.
_ULID_LAST_MS: int = -1
_ULID_LAST_RAND: int = 0


def _new_id() -> str:
    """Generate a 26-char ULID using only the standard library.

    The 48-bit timestamp is filled from the wall clock (ms since
    epoch); the 80-bit randomness comes from ``os.urandom``. A
    monotonic guard (``_ULID_LAST_MS`` / ``_ULID_LAST_RAND``) makes
    successive calls within the same millisecond strictly increasing,
    matching the ULID spec's monotonicity rule. Process-local state is
    fine here: ULIDs are unique-per-row, not unique-per-cluster, and
    the DB primary key constraint is the real safety net.
    """
    global _ULID_LAST_MS, _ULID_LAST_RAND
    ms = time.time_ns() // 1_000_000
    rand_bytes = bytearray(os.urandom(10))
    if ms == _ULID_LAST_MS:
        # Bump the random portion by one. ULID spec: increment the
        # 80-bit random part as a big-endian integer, then re-encode.
        rand_int = int.from_bytes(rand_bytes, "big")
        if rand_int <= _ULID_LAST_RAND:
            rand_int = _ULID_LAST_RAND + 1
        rand_bytes = bytearray(rand_int.to_bytes(10, "big"))
        _ULID_LAST_RAND = rand_int
    else:
        _ULID_LAST_MS = ms
        _ULID_LAST_RAND = int.from_bytes(rand_bytes, "big")

    out = ["0"] * 26
    # Encode 48-bit timestamp into 10 base32 chars.
    for i in range(9, -1, -1):
        out[i] = _ULID_ALPHABET[ms & 0x1F]
        ms >>= 5
    # Encode 80-bit random into the remaining 16 chars.
    bit_buffer = 0
    bits_in_buffer = 0
    out_idx = 10
    for byte in rand_bytes:
        bit_buffer = (bit_buffer << 8) | byte
        bits_in_buffer += 8
        while bits_in_buffer >= 5:
            bits_in_buffer -= 5
            out[out_idx] = _ULID_ALPHABET[(bit_buffer >> bits_in_buffer) & 0x1F]
            out_idx += 1
    # bits_in_buffer may be 0..4 here; ULID truncation is fine.
    return "".join(out)


class ScheduleKind(str, enum.Enum):
    CRON = "cron"
    ONE_SHOT = "one_shot"


class ScheduleStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class ScheduleRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=_new_id)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[ScheduleKind] = mapped_column(Enum(ScheduleKind), nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cron_tz: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ScheduleStatus] = mapped_column(Enum(ScheduleStatus), nullable=False, default=ScheduleStatus.ACTIVE, index=True)
    apscheduler_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="api")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class ScheduleSubscription(Base):
    __tablename__ = "schedule_subscriptions"

    schedule_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("schedules.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("ix_schedule_subscriptions_user_id", "user_id"),
    )


class ScheduleRun(Base):
    __tablename__ = "schedule_runs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=_new_id)
    schedule_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("schedules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscriber_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Populated after the run is dispatched into LangGraph; null while still
    # queued. ``attach_run_id`` writes this once the run is registered.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[ScheduleRunStatus] = mapped_column(
        Enum(ScheduleRunStatus),
        nullable=False,
        default=ScheduleRunStatus.QUEUED,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "Schedule",
    "ScheduleKind",
    "ScheduleRun",
    "ScheduleRunStatus",
    "ScheduleStatus",
    "ScheduleSubscription",
]
