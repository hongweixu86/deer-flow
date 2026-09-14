"""Built-in chat-side tools for the scheduling subsystem (Task 9).

The eight tools in this module are thin async wrappers around the
schedule service. They are exposed to the lead agent only when the
call site sets ``scheduling_enabled=True`` in
:func:`deerflow.tools.get_available_tools`; the default is off so
channels that do not need scheduling cannot accidentally invoke them.

The service lives in the app layer, but this module is in the harness
layer and must not import from ``app.*`` (enforced by
:mod:`tests.test_harness_boundary`). The contract is declared as
:class:`deerflow.scheduling.ScheduleServiceProtocol` and the live
implementation is registered with
:func:`deerflow.scheduling.set_schedule_service` at gateway startup.
Tools reach it through :func:`deerflow.scheduling.get_schedule_service`.

Tools
-----

- :func:`schedule_create` — create a schedule
- :func:`schedule_list` — list visible schedules
- :func:`schedule_get` — get a single schedule
- :func:`schedule_update` — owner-only field update
- :func:`schedule_pause` / :func:`schedule_resume` — lifecycle
- :func:`schedule_delete` — soft delete
- :func:`schedule_subscribe` / :func:`schedule_unsubscribe` — fan-out
- :func:`schedule_runs` — recent run history

Async shape
-----------

Each tool is a :class:`langchain_core.tools.StructuredTool` whose
``coroutine`` is an ``async def``. The lead agent already runs inside
an asyncio event loop, so the tools do **not** need a
``asyncio.run`` shim (which would deadlock). The ``_ensure_sync_invocable_tool``
helper in :mod:`deerflow.tools.tools` adds a sync ``func`` wrapper for
sync caller paths, so both interfaces work.

Error contract
--------------

Service-level exceptions are caught and converted to human-readable
strings so the agent can see the failure and report it back to the
user:

- :class:`ValueError` → ``"validation failed: <message>"``
- :class:`PermissionError` → ``"permission denied: <message>"``
- :class:`LookupError` → ``"not found: <message>"``
- Other :class:`Exception` → ``"error: <ExcClass>: <message>"``

The agent sees the string and can decide whether to retry, ask for
clarification, or surface the error.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from deerflow.scheduling import get_schedule_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error → string conversion
# ---------------------------------------------------------------------------


def _error_to_message(exc: BaseException) -> str:
    """Convert a service-level exception to a tool-result string.

    The mapping mirrors the brief:

    - ``ValueError`` → ``"validation failed: <message>"``
    - ``PermissionError`` → ``"permission denied: <message>"``
    - ``LookupError`` → ``"not found: <message>"``
    - anything else → ``"error: <ExcClass>: <message>"``

    The agent sees the string and can decide how to react.
    """
    if isinstance(exc, ValueError):
        return f"validation failed: {exc}"
    if isinstance(exc, PermissionError):
        return f"permission denied: {exc}"
    if isinstance(exc, LookupError):
        return f"not found: {exc}"
    return f"error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# target_json serialisation
# ---------------------------------------------------------------------------


def _build_target_json(chat_id: str, channel: str = "feishu", *, connection_id: str | None = None) -> str:
    """Build the JSON string stored in ``schedules.target_json``.

    The shape is a small dict that the executor and the push pipeline
    can read without a schema round-trip:

    .. code-block:: json

        {"channel": "feishu", "chat_id": "oc_...", "connection_id": "conn_..."}

    ``connection_id`` is optional — the chat-level fallback resolves
    the user's default connection when it is ``None``.
    """
    payload: dict[str, Any] = {"channel": channel, "chat_id": chat_id}
    if connection_id is not None:
        payload["connection_id"] = connection_id
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Row formatting helpers
# ---------------------------------------------------------------------------


def _row_to_text(row: Any) -> str:
    """Render a :class:`~deerflow.persistence.models.schedule.Schedule` as text."""
    return (
        f"- id={row.id} title={row.title!r} "
        f"kind={getattr(row.kind, 'value', row.kind)} "
        f"status={getattr(row.status, 'value', row.status)}"
    )


def _run_to_text(run: Any) -> str:
    """Render a :class:`~deerflow.persistence.models.schedule.ScheduleRun` as text."""

    def _fmt_dt(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value) if value is not None else "—"

    status = getattr(run.status, "value", run.status)
    return (
        f"- id={run.id} status={status} attempt={run.attempt} "
        f"started={_fmt_dt(getattr(run, 'started_at', None))} "
        f"finished={_fmt_dt(getattr(run, 'finished_at', None))}"
        + (f" error={run.error_summary!r}" if getattr(run, "error_summary", None) else "")
    )


# ---------------------------------------------------------------------------
# Pydantic input schemas
# ---------------------------------------------------------------------------


class _ScheduleCreateInput(BaseModel):
    title: str = Field(description="Human-readable title, e.g. 'Daily report'")
    kind: str = Field(description="Either 'cron' (recurring) or 'one_shot' (single fire)")
    prompt: str = Field(description="The natural-language instruction the agent will run on every fire")
    target_chat_id: str = Field(description="Feishu chat id where the run result is pushed")
    cron_expr: str | None = Field(default=None, description="Cron expression, required when kind='cron' (e.g. '0 9 * * *')")
    run_at: str | None = Field(default=None, description="ISO-8601 datetime, required when kind='one_shot'")
    cron_tz: str | None = Field(default=None, description="IANA timezone for the cron (default: app config)")
    thread_id: str | None = Field(default=None, description="Optional langgraph thread id to reuse across fires")
    target_connection_id: str | None = Field(default=None, description="Optional Feishu connection id; falls back to the user's default")
    current_user: str = Field(description="The acting user id; the tool resolves it from the runtime context but the field is exposed for testability")


class _ScheduleListInput(BaseModel):
    current_user: str = Field(description="The acting user id")
    scope: str = Field(default="mine", description="'mine' (only own), 'subscribed' (subscribed + own), or 'all' (everything visible)")
    status: str | None = Field(default=None, description="Optional status filter: 'active', 'paused', or 'deleted'")
    limit: int = Field(default=20, description="Max number of rows to return (default 20)")


class _ScheduleGetInput(BaseModel):
    schedule_id: str = Field(description="Schedule id (ULID)")
    current_user: str = Field(description="The acting user id")


class _ScheduleUpdateInput(BaseModel):
    schedule_id: str = Field(description="Schedule id (ULID)")
    current_user: str = Field(description="The acting user id; must be the owner")
    title: str | None = Field(default=None, description="New title")
    prompt: str | None = Field(default=None, description="New prompt")
    cron_expr: str | None = Field(default=None, description="New cron expression")
    run_at: str | None = Field(default=None, description="New one-shot fire time (ISO-8601)")
    cron_tz: str | None = Field(default=None, description="New cron timezone")
    target_chat_id: str | None = Field(default=None, description="New target chat id; updates target_json")
    target_connection_id: str | None = Field(default=None, description="New target connection id; updates target_json")


class _ScheduleIdOnlyInput(BaseModel):
    schedule_id: str = Field(description="Schedule id (ULID)")
    current_user: str = Field(description="The acting user id")


class _ScheduleSubscribeInput(BaseModel):
    schedule_id: str = Field(description="Schedule id (ULID)")
    current_user: str = Field(description="The acting user id; becomes a subscriber")
    target_chat_id: str | None = Field(default=None, description="Optional override chat id for this subscriber's push target")
    target_connection_id: str | None = Field(default=None, description="Optional override connection id for this subscriber's push target")


class _ScheduleRunsInput(BaseModel):
    schedule_id: str = Field(description="Schedule id (ULID)")
    current_user: str = Field(description="The acting user id; must be the owner or a subscriber")
    limit: int = Field(default=10, description="Max number of run rows to return (default 10)")


# ---------------------------------------------------------------------------
# Async coroutines (the actual tool logic)
# ---------------------------------------------------------------------------


async def _schedule_create(**kwargs: Any) -> str:
    """Create a schedule. Returns a confirmation string on success."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    target_chat_id: str = kwargs["target_chat_id"]
    target_connection_id: str | None = kwargs.get("target_connection_id")
    target_json = _build_target_json(target_chat_id, connection_id=target_connection_id)

    payload: dict[str, Any] = {
        "title": kwargs["title"],
        "kind": kwargs["kind"],
        "prompt": kwargs["prompt"],
        "target_json": target_json,
        "source": "im",
    }
    if kwargs.get("cron_expr") is not None:
        payload["cron_expr"] = kwargs["cron_expr"]
    if kwargs.get("run_at") is not None:
        payload["run_at"] = kwargs["run_at"]
    if kwargs.get("cron_tz") is not None:
        payload["cron_tz"] = kwargs["cron_tz"]
    if kwargs.get("thread_id") is not None:
        payload["thread_id"] = kwargs["thread_id"]

    try:
        row = await svc.create(payload=payload, current_user=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001 — error contract requires catching everything
        logger.warning("schedule_create failed: %s", exc)
        return _error_to_message(exc)

    return f"created schedule id={row.id} title={row.title!r} kind={getattr(row.kind, 'value', row.kind)}"


async def _schedule_list(**kwargs: Any) -> str:
    """List visible schedules for the current user."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    from deerflow.persistence.models.schedule import ScheduleStatus

    scope = kwargs.get("scope", "mine")
    status_str: str | None = kwargs.get("status")
    status = ScheduleStatus(status_str) if status_str else None
    limit: int = int(kwargs.get("limit") or 20)

    try:
        rows = await svc.list_for_viewer(viewer=kwargs["current_user"], scope=scope, status=status, limit=limit)
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_list failed: %s", exc)
        return _error_to_message(exc)

    if not rows:
        return "no schedules found"
    return "\n".join(_row_to_text(r) for r in rows)


async def _schedule_get(**kwargs: Any) -> str:
    """Get a single schedule by id."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    try:
        row = await svc.get_for_viewer(kwargs["schedule_id"], viewer=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_get failed: %s", exc)
        return _error_to_message(exc)

    if row is None:
        return f"not found: schedule {kwargs['schedule_id']!r}"
    return _row_to_text(row)


async def _schedule_update(**kwargs: Any) -> str:
    """Owner-only field update. Only the supplied fields are forwarded."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    schedule_id: str = kwargs["schedule_id"]
    fields: dict[str, Any] = {}

    # Scalar fields. ``None`` means "do not change"; only forward values
    # that were explicitly set, so the repo's whitelist never has to
    # guess intent from absence.
    for key in ("title", "prompt", "cron_expr", "cron_tz"):
        if kwargs.get(key) is not None:
            fields[key] = kwargs[key]
    if kwargs.get("run_at") is not None:
        fields["run_at"] = kwargs["run_at"]

    # target_json: rebuild from the new chat id (and optional connection
    # id). If neither is supplied, the existing target_json is left
    # untouched (the caller did not intend to change it).
    if kwargs.get("target_chat_id") is not None:
        fields["target_json"] = _build_target_json(
            kwargs["target_chat_id"],
            connection_id=kwargs.get("target_connection_id"),
        )

    if not fields:
        return f"no fields to update for schedule {schedule_id!r}"

    try:
        row = await svc.update(schedule_id, fields=fields, current_user=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_update failed: %s", exc)
        return _error_to_message(exc)

    return f"updated schedule id={row.id} title={row.title!r}"


async def _schedule_pause(**kwargs: Any) -> str:
    """Pause a schedule. Owner-only."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    try:
        await svc.pause(kwargs["schedule_id"], current_user=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_pause failed: %s", exc)
        return _error_to_message(exc)
    return f"paused schedule id={kwargs['schedule_id']}"


async def _schedule_resume(**kwargs: Any) -> str:
    """Resume a paused schedule. Owner-only."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    try:
        await svc.resume(kwargs["schedule_id"], current_user=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_resume failed: %s", exc)
        return _error_to_message(exc)
    return f"resumed schedule id={kwargs['schedule_id']}"


async def _schedule_delete(**kwargs: Any) -> str:
    """Soft-delete a schedule. Owner-only."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    try:
        await svc.soft_delete(kwargs["schedule_id"], current_user=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_delete failed: %s", exc)
        return _error_to_message(exc)
    return f"deleted schedule id={kwargs['schedule_id']}"


async def _schedule_subscribe(**kwargs: Any) -> str:
    """Subscribe the current user to a schedule's push output."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    target_json: str | None = None
    if kwargs.get("target_chat_id") is not None:
        target_json = _build_target_json(
            kwargs["target_chat_id"],
            connection_id=kwargs.get("target_connection_id"),
        )

    try:
        await svc.subscribe(
            kwargs["schedule_id"],
            user_id=kwargs["current_user"],
            target_json=target_json,
        )
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_subscribe failed: %s", exc)
        return _error_to_message(exc)
    return f"subscribed to schedule id={kwargs['schedule_id']}"


async def _schedule_unsubscribe(**kwargs: Any) -> str:
    """Unsubscribe the current user from a schedule's push output."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    try:
        await svc.unsubscribe(kwargs["schedule_id"], user_id=kwargs["current_user"])
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_unsubscribe failed: %s", exc)
        return _error_to_message(exc)
    return f"unsubscribed from schedule id={kwargs['schedule_id']}"


async def _schedule_runs(**kwargs: Any) -> str:
    """List recent run history for a schedule (owner / subscriber only)."""
    svc = get_schedule_service()
    if svc is None:
        return "error: scheduling service is not running on this replica"

    limit: int = int(kwargs.get("limit") or 10)
    try:
        runs = await svc.list_runs(kwargs["schedule_id"], viewer=kwargs["current_user"], limit=limit)
    except BaseException as exc:  # noqa: BLE001
        logger.warning("schedule_runs failed: %s", exc)
        return _error_to_message(exc)

    if not runs:
        return f"no runs for schedule id={kwargs['schedule_id']}"
    return "\n".join(_run_to_text(r) for r in runs)


# ---------------------------------------------------------------------------
# Tool exports (StructuredTool with async coroutine)
# ---------------------------------------------------------------------------


_COMMON_DESCRIPTION_INTRO = (
    "Use this when the user is in a Feishu / IM chat and wants to create, "
    "inspect, or manage a scheduled task. The tools are thin wrappers over "
    "the scheduling service; each one accepts already-structured fields "
    "(no NL extraction is done here — the agent must resolve cron / "
    "datetime / target up front, typically via ask_clarification)."
)

_DESCRIPTIONS: dict[str, str] = {
    "schedule_create": (
        "Create a new schedule. Required: title, kind ('cron' | 'one_shot'), "
        "prompt, target_chat_id. If kind='cron' then cron_expr is required "
        "(e.g. '0 9 * * *'); if kind='one_shot' then run_at is required "
        "(ISO-8601). The current_user is the acting user's id (the tool "
        "requires it explicitly; the agent typically reads it from the "
        "runtime context). The tool returns a confirmation string with the "
        "new schedule id."
    ),
    "schedule_list": (
        "List schedules visible to the current user. scope is 'mine' "
        "(default — only owned), 'subscribed' (own + subscribed), or 'all' "
        "(everything visible). status filters by ScheduleStatus (active | "
        "paused | deleted). limit defaults to 20."
    ),
    "schedule_get": (
        "Get a single schedule by id. Returns a one-line text summary; "
        "404 / not-visible is rendered as 'not found:'."
    ),
    "schedule_update": (
        "Owner-only field update. Only the fields that are explicitly "
        "supplied are forwarded — pass None (or omit) for fields that "
        "should not change. The whitelist of mutable fields is enforced "
        "by the service (see service.update). Setting target_chat_id "
        "(and optionally target_connection_id) rebuilds target_json."
    ),
    "schedule_pause": "Pause a schedule. Owner-only. The next fire is skipped until resume().",
    "schedule_resume": "Resume a paused schedule. Owner-only. The next fire is recomputed from the current cron / one_shot fields.",
    "schedule_delete": "Soft-delete a schedule. Owner-only. The row is preserved for audit; no further fires will happen.",
    "schedule_subscribe": (
        "Subscribe the current user to a schedule's push output. The "
        "user will receive the formatted result of every fire at the "
        "target (default: the schedule's own target, or this user's "
        "override if target_chat_id is supplied)."
    ),
    "schedule_unsubscribe": "Unsubscribe the current user from a schedule's push output.",
    "schedule_runs": "List recent run history for a schedule (owner / subscriber only). limit defaults to 10.",
}


def _make_tool(name: str, description: str, args_schema: type[BaseModel], coroutine: Any) -> StructuredTool:
    return StructuredTool.from_function(
        name=name,
        description=f"{_COMMON_DESCRIPTION_INTRO}\n\n{description}",
        coroutine=coroutine,
        args_schema=args_schema,
    )


schedule_create = _make_tool("schedule_create", _DESCRIPTIONS["schedule_create"], _ScheduleCreateInput, _schedule_create)
schedule_list = _make_tool("schedule_list", _DESCRIPTIONS["schedule_list"], _ScheduleListInput, _schedule_list)
schedule_get = _make_tool("schedule_get", _DESCRIPTIONS["schedule_get"], _ScheduleGetInput, _schedule_get)
schedule_update = _make_tool("schedule_update", _DESCRIPTIONS["schedule_update"], _ScheduleUpdateInput, _schedule_update)
schedule_pause = _make_tool("schedule_pause", _DESCRIPTIONS["schedule_pause"], _ScheduleIdOnlyInput, _schedule_pause)
schedule_resume = _make_tool("schedule_resume", _DESCRIPTIONS["schedule_resume"], _ScheduleIdOnlyInput, _schedule_resume)
schedule_delete = _make_tool("schedule_delete", _DESCRIPTIONS["schedule_delete"], _ScheduleIdOnlyInput, _schedule_delete)
schedule_subscribe = _make_tool("schedule_subscribe", _DESCRIPTIONS["schedule_subscribe"], _ScheduleSubscribeInput, _schedule_subscribe)
schedule_unsubscribe = _make_tool("schedule_unsubscribe", _DESCRIPTIONS["schedule_unsubscribe"], _ScheduleIdOnlyInput, _schedule_unsubscribe)
schedule_runs = _make_tool("schedule_runs", _DESCRIPTIONS["schedule_runs"], _ScheduleRunsInput, _schedule_runs)


SCHEDULE_TOOLS: list[StructuredTool] = [
    schedule_create,
    schedule_list,
    schedule_get,
    schedule_update,
    schedule_pause,
    schedule_resume,
    schedule_delete,
    schedule_subscribe,
    schedule_unsubscribe,
    schedule_runs,
]


__all__ = [
    "SCHEDULE_TOOLS",
    "get_schedule_service",
    "schedule_create",
    "schedule_delete",
    "schedule_get",
    "schedule_list",
    "schedule_pause",
    "schedule_resume",
    "schedule_runs",
    "schedule_subscribe",
    "schedule_unsubscribe",
    "schedule_update",
]
