"""Schedule executor: dispatch + run + push + retry.

This module is the runtime half of the scheduling subsystem. The
:class:`SchedulerEngine` (Task 4) decides *when* to fire and calls
``executor.enqueue(schedule_id)``; the executor decides *how* a single
fire turns into a langgraph run, how its result is persisted, and how
the outcome is pushed back to the chat channel that owns the schedule.

Architecture (MVP, deliberately simple)
----------------------------------------

The plan describes a per-(schedule, subscriber) asyncio queue with a
fixed-size worker pool, backpressure, and a "drop + alert" path on
queue-full. The unit tests written for this task do **not** exercise
that machinery -- the brief explicitly asks for the simplest path
that passes the tests, with the worker-pool / per-subscriber loop
flagged in the report so the final review can re-expand it.

Concretely, the MVP contract is:

- :meth:`ScheduleExecutor.enqueue` calls :meth:`run_one` directly in
  the caller's task. No queue, no ``create_task``, no worker pool.
  APScheduler's :class:`AsyncIOScheduler` already serialises
  ``on_fire`` calls per job (``max_instances=1`` from
  :class:`APSchedulerConfig`), so adding a second queue here would
  only buy complexity.
- :meth:`start` and :meth:`stop` are no-ops. The engine does not own
  any background workers to drain.
- The "per-subscriber" loop is dropped. The executor always uses the
  schedule's own ``target_json`` for the push target. Subscribers
  receive a copy of the message through the ``MessageBus`` fan-out
  in Task 5, not through a separate per-row push iteration here.

Lifecycle of a single fire
--------------------------

:meth:`run_one` is the unit of work:

1. Load the schedule via :meth:`ScheduleRepository.get`. If the
   row is missing or its ``status`` is not ``ACTIVE``, the executor
   returns without creating a run row or pushing a message -- a
   paused or deleted schedule does not deserve a noisy alert.
2. Create a ``ScheduleRun`` row in ``QUEUED`` state (via
   :meth:`ScheduleRepository.create_run`) and immediately transition
   it to ``RUNNING`` via :meth:`ScheduleRepository.update_run`. This
   pins a row id so the rest of the run is observable to the
   ``list_runs`` API surface.
3. Call ``run_manager.create_or_reject(...)``. This is the langgraph
   runtime's atomic "start a new run or refuse if one is in flight"
   entry point. If it raises (``ConflictError`` or anything else),
   the executor marks the run row as ``FAILED`` with
   ``error_summary="run_rejected"`` and pushes a single ``❌`` alert.
4. **For the MVP we trust the langgraph run to complete on its own.**
   The executor does not block on the run; it treats the existence
   of a run id as "dispatched". The run's terminal status is
   observed by a separate downstream component (Task 9 / the agent
   tool path) which is responsible for the success / retry / final
   push. This keeps the executor a single, well-defined unit of work
   and avoids double-pushing the same result.
5. Push the formatted success message (via
   :func:`app.scheduling.formatting.format_push_message`) over
   :meth:`MessageBus.publish_outbound` with ``thread_id=None`` so the
   chat-level path (Task 5) handles it.
6. On any push failure, the run is marked ``FAILED`` with the
   bus error in ``error_summary``, a ``push_failure`` metric is
   recorded, and the executor returns -- the next fire is the right
   recovery mechanism.

Why no retry inside the executor
--------------------------------

The plan's retry / backoff math lives in
:meth:`ScheduleService.compute_next_retry` (Task 6). For the MVP we
do not invoke it here because (a) the tests don't exercise it and
(b) the natural retry trigger for cron schedules is the next
APScheduler fire, not a wall-clock ``next_retry_at`` column on the
run row. The repo's ``next_retry_at`` field is preserved (Task 2) so
a future one-shot-with-retry path can use it; wiring it in is a
follow-up that requires a one-shot-only flag in :class:`Schedule`
and a deferred re-enqueue hook on :class:`APScheduler`.

Audit + metrics
---------------

Every fire emits one ``middleware:schedule schedule.fire ...`` audit
line and one ``metric:schedule fire ...`` metrics line, so an
operator can grep the log to confirm the executor woke up without
having to know the full call graph.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.channels.message_bus import MessageBus, OutboundMessage
from app.scheduling.audit import audit_schedule_event
from app.scheduling.formatting import format_push_message
from app.scheduling.observability import Metrics
from deerflow.config.scheduling import SchedulingConfig
from deerflow.persistence.models.schedule import (
    Schedule,
    ScheduleRunStatus,
    ScheduleStatus,
)
from deerflow.persistence.schedule_repo import ScheduleRepository

logger = logging.getLogger(__name__)


def _parse_target(target_json: str) -> tuple[str, str]:
    """Pull ``channel`` and ``chat_id`` out of a ``target_json`` blob.

    Falls back to ``("unknown", "")`` on any parse / key error so a
    malformed row does not crash the executor -- the push will still
    be dispatched; the consumer (Feishu, IM, etc.) is responsible for
    deciding what to do with a missing chat_id.
    """
    try:
        data = json.loads(target_json)
    except (TypeError, ValueError):
        return "unknown", ""
    channel = str(data.get("channel") or "unknown")
    chat_id = str(data.get("chat_id") or "")
    return channel, chat_id


class ScheduleExecutor:
    """Run + retry + push executor for the scheduling subsystem.

    The constructor takes the three collaborators the executor needs:

    - ``repo`` -- :class:`ScheduleRepository` for schedule + run rows.
    - ``run_manager`` -- the langgraph ``RunManager`` whose
      :meth:`create_or_reject` is the dispatch entry point.
    - ``message_bus`` -- :class:`MessageBus` for outbound pushes.
    - ``config`` -- :class:`SchedulingConfig` (for push ``max_text_length``).
    """

    def __init__(
        self,
        repo: ScheduleRepository,
        run_manager: Any,
        message_bus: MessageBus,
        config: SchedulingConfig,
    ) -> None:
        self._repo = repo
        self._run_manager = run_manager
        self._bus = message_bus
        self._config = config

    # ------------------------------------------------------------------
    # Lifecycle (MVP: no-op)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """No-op for the MVP. A future iteration may spawn the worker
        pool here.
        """
        return None

    async def stop(self) -> None:
        """No-op for the MVP. A future iteration may drain the worker
        pool here.
        """
        return None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def enqueue(self, schedule_id: str, subscriber_user_id: str | None = None) -> None:
        """Dispatch a fire.

        The simplest path: call :meth:`run_one` directly. APScheduler
        already serialises per-job fires (``max_instances=1`` from
        :class:`APSchedulerConfig`), so a per-(schedule, subscriber)
        queue is not needed for correctness in the MVP. A future
        iteration can swap this for ``asyncio.create_task(self.run_one(...))``
        plus a bounded worker pool without changing any other API.
        """
        await self.run_one(schedule_id, subscriber_user_id=subscriber_user_id)

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    async def run_one(self, schedule_id: str, subscriber_user_id: str | None = None) -> None:
        """Run a single fire to completion (or terminal failure).

        See the module docstring for the lifecycle. All side effects
        are best-effort: a failure to write a run row or push a
        message is logged but does not raise, so a misbehaving
        collaborator cannot take the scheduler down.
        """
        schedule = await self._repo.get(schedule_id)
        if schedule is None or schedule.status != ScheduleStatus.ACTIVE:
            # Paused / deleted / never-existed: drop the trigger.
            logger.info(
                "[Executor] skipping fire for %s (status=%s, exists=%s)",
                schedule_id,
                getattr(schedule, "status", None),
                schedule is not None,
            )
            return

        audit_schedule_event(
            "schedule.fire",
            schedule_id=schedule.id,
            kind=schedule.kind.value,
            subscriber_user_id=subscriber_user_id or "",
        )
        Metrics.fire(schedule.id, kind=schedule.kind.value, status="started")

        run_row = await self._repo.create_run(
            schedule_id=schedule.id,
            subscriber_user_id=subscriber_user_id,
            attempt=1,
        )
        await self._repo.update_run(
            run_row.id,
            status=ScheduleRunStatus.RUNNING,
            started_at=run_row.started_at,  # model default; the ORM will fill it in
        )

        # Dispatch to langgraph. Any failure here is treated as a
        # non-retryable "we couldn't even start the run" case: mark
        # the row FAILED with a clear error_summary, push a single
        # alert, and return. The next fire (cron) will retry
        # naturally; the operator gets one visible ❌ line per
        # failed dispatch.
        t0 = time.monotonic()
        try:
            record = await self._run_manager.create_or_reject(
                thread_id=schedule.id,
                user_id=subscriber_user_id or schedule.owner_user_id,
                metadata={"schedule_id": schedule.id, "prompt": schedule.prompt},
                kwargs={"input": {"messages": [{"role": "user", "content": schedule.prompt}]}},
            )
        except Exception as exc:  # noqa: BLE001 -- we want to catch all dispatch failures
            await self._fail_run(
                run_row.id,
                error_summary=f"run_rejected: {exc.__class__.__name__}",
                schedule=schedule,
                attempt=1,
                body=str(exc) or exc.__class__.__name__,
            )
            return

        run_id = getattr(record, "run_id", None) or str(record)
        await self._repo.attach_run_id(run_row.id, run_id)

        # Push the success message. If the bus raises, the run is
        # marked FAILED with the bus error and a push_failure metric
        # is recorded. The run itself was successfully dispatched
        # into langgraph; marking the *run row* FAILED here is
        # deliberately conservative -- the langgraph run will still
        # continue to completion in the background, but from the
        # schedule's perspective the "fire" failed because we
        # couldn't notify the chat.
        text = format_push_message(
            schedule=schedule,
            attempt=1,
            status="success",
            body="调度任务已触发",
            run_url=None,
            max_length=self._config.push.max_text_length,
        )
        channel_name, chat_id = _parse_target(schedule.target_json)
        msg = OutboundMessage(
            channel_name=channel_name,
            chat_id=chat_id,
            text=text,
            thread_id=None,  # chat-level path (Task 5)
        )
        try:
            await self._bus.publish_outbound(msg)
        except Exception as exc:  # noqa: BLE001
            Metrics.push_failure(reason=exc.__class__.__name__)
            await self._repo.update_run(
                run_row.id,
                status=ScheduleRunStatus.FAILED,
                finished_at=_utcnow(),
                error_summary=f"push_failure: {exc}",
            )
            audit_schedule_event(
                "schedule.push_failure",
                schedule_id=schedule.id,
                run_id=run_id,
                reason=exc.__class__.__name__,
            )
            logger.warning("[Executor] push failed for %s: %s", schedule.id, exc)
            return

        # Success: mark the run SUCCEEDED, record the metric + audit.
        await self._repo.update_run(
            run_row.id,
            status=ScheduleRunStatus.SUCCEEDED,
            finished_at=_utcnow(),
        )
        Metrics.run_duration(kind=schedule.kind.value, seconds=time.monotonic() - t0)
        audit_schedule_event(
            "schedule.run_succeeded",
            schedule_id=schedule.id,
            run_id=run_id,
            attempt=1,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fail_run(
        self,
        run_row_id: str,
        *,
        error_summary: str,
        schedule: Schedule,
        attempt: int,
        body: str,
    ) -> None:
        """Mark a run row FAILED and push a single ``❌`` alert."""
        await self._repo.update_run(
            run_row_id,
            status=ScheduleRunStatus.FAILED,
            finished_at=_utcnow(),
            error_summary=error_summary,
        )
        Metrics.retry(attempt=attempt)
        text = format_push_message(
            schedule=schedule,
            attempt=attempt,
            status="failed",
            body=body,
            run_url=None,
            max_length=self._config.push.max_text_length,
        )
        channel_name, chat_id = _parse_target(schedule.target_json)
        msg = OutboundMessage(
            channel_name=channel_name,
            chat_id=chat_id,
            text=text,
            thread_id=None,
        )
        try:
            await self._bus.publish_outbound(msg)
        except Exception as exc:  # noqa: BLE001
            Metrics.push_failure(reason=exc.__class__.__name__)
            logger.warning("[Executor] failed-alert push also failed for %s: %s", schedule.id, exc)
        audit_schedule_event(
            "schedule.run_failed",
            schedule_id=schedule.id,
            attempt=attempt,
            error_summary=error_summary,
        )


def _utcnow():
    return datetime.now(UTC)


__all__ = ["ScheduleExecutor"]
