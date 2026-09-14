"""Harness-side scheduling protocol + provider.

The scheduling subsystem itself lives in ``app.scheduling`` (gateway-
side wiring, APScheduler engine, FastAPI router, REST authz). The
harness layer (:mod:`deerflow`) cannot import from the app layer --
:mod:`tests.test_harness_boundary` enforces this in CI. To let the
agent tools (:mod:`deerflow.tools.builtins.schedule_tool`) talk to
the scheduling service without breaking the boundary, the contract
is declared here as a :class:`Protocol` and a single module-level
provider slot is exposed for the gateway lifespan to register the
real implementation on startup.

Layout
------

- :mod:`deerflow.scheduling.protocol` -- ``ScheduleServiceProtocol``
- :mod:`deerflow.scheduling.provider` -- ``set_schedule_service`` /
  ``get_schedule_service`` registration slot
"""

from __future__ import annotations

from deerflow.scheduling.protocol import ScheduleServiceProtocol
from deerflow.scheduling.provider import get_schedule_service, set_schedule_service

__all__ = [
    "ScheduleServiceProtocol",
    "get_schedule_service",
    "set_schedule_service",
]
