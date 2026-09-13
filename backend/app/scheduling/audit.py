"""Structured audit log lines for the scheduling subsystem.

Emits a single ``logger.info`` line per event, with a fixed
``middleware:schedule`` prefix so log shippers can filter on it.
Mirrors the style of the existing ``middleware:skill_activation`` audit
events in :mod:`app.channels.manager`.

The function is intentionally a thin wrapper around the logger so
tests can assert on the structured output by capturing log records
under the ``app.scheduling.audit`` logger.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_AUDIT_PREFIX = "middleware:schedule"


def audit_schedule_event(event: str, **fields: object) -> None:
    """Emit a single ``middleware:schedule`` audit line.

    Parameters
    ----------
    event:
        A short event name (e.g. ``"schedule.create"``, ``"schedule.fire"``).
    **fields:
        Structured key/value pairs to append to the log line. Values are
        stringified via :class:`str`, which is fine for ids, enums,
        timestamps, and counts -- the audit log is for humans, not for
        round-tripping.
    """
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("[Audit] %s %s %s", _AUDIT_PREFIX, event, kv)


__all__ = ["audit_schedule_event"]
