"""Module-level registration slot for the schedule service.

The agent tools (:mod:`deerflow.tools.builtins.schedule_tool`) need
to reach the schedule service at request time. The service itself
lives in the app layer and cannot be imported by the harness layer
(:mod:`tests.test_harness_boundary` would fail). The gateway
lifespan therefore *registers* the live service via
:func:`set_schedule_service` on startup, and the tools read it via
:func:`get_schedule_service`.

The slot is a single module-level reference rather than a context
var because:

- there is exactly one service per process (one FastAPI app, one
  scheduler singleton), so a global is sufficient and matches the
  existing ``app.scheduling.lifespan._scheduler_service`` pattern;
- the agent's tool coroutine is invoked on the same asyncio loop
  that the lifespan ran on, so there is no cross-loop sharing risk;
- tests can replace the slot in-process without monkey-patching
  imports.

Threading: registration happens once during ``lifespan`` (single
task, single process), reads happen from any concurrent tool
coroutine. The slot is a plain attribute so a partial read sees a
fully-constructed object or ``None``; there is no torn-write risk
worth ``asyncio.Lock``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deerflow.scheduling.protocol import ScheduleServiceProtocol

_provider: "ScheduleServiceProtocol | None" = None


def set_schedule_service(service: "ScheduleServiceProtocol") -> None:
    """Register the live schedule service.

    Called by :mod:`app.scheduling.lifespan` after the service
    singleton is built. A second call replaces the previous
    registration (the lifespan re-build path is idempotent, so
    callers that want to keep the old reference must hold it
    themselves before the second call lands).
    """
    global _provider
    _provider = service


def get_schedule_service() -> "ScheduleServiceProtocol | None":
    """Return the registered service, or ``None`` if not wired yet.

    Tools use ``None`` as a sentinel to surface
    ``"scheduling service is not running on this replica"`` instead
    of crashing on an ``AttributeError`` -- this is the same
    behaviour the previous ``app.scheduling.lifespan`` lookup gave.
    """
    return _provider


def reset_schedule_service_for_testing() -> None:
    """Clear the slot. Test-only helper; production code never needs this."""
    global _provider
    _provider = None


__all__ = [
    "get_schedule_service",
    "set_schedule_service",
    "reset_schedule_service_for_testing",
]
