"""Harness-side protocol for the scheduling service.

The harness layer (:mod:`deerflow`) is publishable as a standalone
agent framework; it must not import from the app layer
(:mod:`app.scheduling.service`). To let the agent tools reach the
schedule service without breaking the boundary, the contract is
declared here as a :class:`Protocol`. The real implementation
(:class:`app.scheduling.service.ScheduleService`) is structurally
compatible -- it is registered with :func:`deerflow.scheduling.set_schedule_service`
at gateway startup, then reached via :func:`deerflow.scheduling.get_schedule_service`.

The method set mirrors exactly the public surface the agent tools
use; no extras are declared. Adding a new method here is a forcing
function for the app side to keep the contract honest.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ScheduleServiceProtocol(Protocol):
    """Public surface of the schedule service the agent tools depend on.

    Implementations are duck-typed; :func:`typing.runtime_checkable`
    only enforces the *method names* (and a structural ``isinstance``
    check on the class), so a stub used in tests does not have to
    actually subclass this -- it just needs the same method names
    with compatible call shapes.
    """

    async def create(self, *, payload: dict[str, Any], current_user: str) -> Any:
        """Insert a new schedule row and return the persisted ORM instance."""
        ...

    async def get_for_viewer(self, schedule_id: str, viewer: str) -> Any | None:
        """Return the schedule only if the viewer can see it (owner or subscriber)."""
        ...

    async def list_for_viewer(
        self,
        viewer: str,
        *,
        scope: str = "all",
        status: Any | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """List schedules visible to ``viewer``."""
        ...

    async def update(self, schedule_id: str, *, fields: dict[str, Any], current_user: str) -> Any:
        """Owner-only field update; whitelist enforced by the implementation."""
        ...

    async def pause(self, schedule_id: str, *, current_user: str) -> None:
        """Pause a schedule; owner-only."""
        ...

    async def resume(self, schedule_id: str, *, current_user: str) -> None:
        """Resume a paused schedule; owner-only."""
        ...

    async def soft_delete(self, schedule_id: str, *, current_user: str) -> None:
        """Soft-delete a schedule; owner-only."""
        ...

    async def subscribe(
        self,
        schedule_id: str,
        *,
        user_id: str,
        target_json: str | None = None,
    ) -> None:
        """Subscribe ``user_id`` to a schedule's push output."""
        ...

    async def unsubscribe(self, schedule_id: str, *, user_id: str) -> None:
        """Drop a subscription; idempotent."""
        ...

    async def list_runs(
        self,
        schedule_id: str,
        *,
        viewer: str,
        limit: int = 50,
    ) -> list[Any]:
        """Recent run history (visibility-filtered)."""
        ...


__all__ = ["ScheduleServiceProtocol"]
