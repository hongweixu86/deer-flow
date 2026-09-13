"""Tests for the chat-side schedule tools (Task 9).

The tools live in :mod:`deerflow.tools.builtins.schedule_tool` and are
thin wrappers around :class:`app.scheduling.service.ScheduleService`.
They are gated on a ``scheduling_enabled`` kwarg in
:func:`deerflow.tools.get_available_tools` so the agent does not expose
scheduling to every channel by default.

These tests use ``asyncio_mode = auto`` (the project default) so async
test bodies do not need an explicit ``@pytest.mark.anyio`` marker.

Boundary note
-------------

The tools import :mod:`app.scheduling.service` directly. This violates
the project-wide ``app ↔ deerflow`` boundary (harness should not import
app). For the MVP this is accepted; a follow-up can break the cycle by
having the service expose a Protocol in ``deerflow`` and the tool call
it through that Protocol.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import BaseTool

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------


def test_schedule_tools_module_exports_all_eight_tools() -> None:
    """The 8 schedule tools must all be importable as attributes.

    Names are pinned so the LLM-facing tool schema is stable across
    refactors. If you rename one, update the spec and the
    ``get_available_tools`` filter at the same time.
    """
    from deerflow.tools.builtins import schedule_tool

    expected = {
        "schedule_create",
        "schedule_list",
        "schedule_get",
        "schedule_update",
        "schedule_pause",
        "schedule_resume",
        "schedule_delete",
        "schedule_subscribe",
        "schedule_unsubscribe",
        "schedule_runs",
    }
    actual = set(dir(schedule_tool))
    missing = expected - actual
    assert not missing, f"missing tools: {missing}"


def test_schedule_tools_are_base_tool_instances() -> None:
    """Each schedule tool is a LangChain BaseTool so it can be wired into an agent."""
    from deerflow.tools.builtins.schedule_tool import (
        schedule_create,
        schedule_delete,
        schedule_get,
        schedule_list,
        schedule_pause,
        schedule_resume,
        schedule_runs,
        schedule_subscribe,
        schedule_unsubscribe,
        schedule_update,
    )

    tools = [
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
    for tool in tools:
        assert isinstance(tool, BaseTool), f"{tool.name} is not a BaseTool"


# ---------------------------------------------------------------------------
# Helper: stub ScheduleService
# ---------------------------------------------------------------------------


def _make_stub_service() -> MagicMock:
    """Build a MagicMock that quacks like ScheduleService.

    All async methods are wrapped in :class:`AsyncMock` so the tools can
    ``await`` them. The mock's ``__class__`` is patched to satisfy any
    runtime ``isinstance`` check (none expected, but defensive).
    """
    svc = MagicMock()
    svc.create = AsyncMock()
    svc.list_for_viewer = AsyncMock()
    svc.get_for_viewer = AsyncMock()
    svc.update = AsyncMock()
    svc.pause = AsyncMock()
    svc.resume = AsyncMock()
    svc.soft_delete = AsyncMock()
    svc.subscribe = AsyncMock()
    svc.unsubscribe = AsyncMock()
    svc.list_runs = AsyncMock()
    return svc


def _install_service(svc: MagicMock) -> Any:
    """Patch the module-level service accessor for the duration of a test.

    Returns the patcher so the caller can ``.stop()`` it (pytest fixtures
    with ``yield`` would do the same, but a context-manager-free patch
    keeps the test bodies linear).
    """
    return patch("deerflow.tools.builtins.schedule_tool.get_schedule_service", return_value=svc)


# ---------------------------------------------------------------------------
# schedule_create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_create_tool_uses_service() -> None:
    """schedule_create forwards a structured payload to ScheduleService.create."""
    from deerflow.tools.builtins.schedule_tool import schedule_create

    svc = _make_stub_service()
    expected_row = MagicMock()
    expected_row.id = "sched_abc"
    expected_row.title = "Daily Report"
    svc.create.return_value = expected_row

    with _install_service(svc):
        result = await schedule_create.coroutine(  # type: ignore[attr-defined]
            title="Daily Report",
            kind="cron",
            cron_expr="0 9 * * *",
            prompt="summarise",
            target_chat_id="oc_1",
            current_user="u1",
        )

    # Service was called with a dict payload + the current user.
    svc.create.assert_awaited_once()
    kwargs = svc.create.await_args.kwargs
    assert kwargs["current_user"] == "u1"
    payload = kwargs["payload"]
    assert payload["title"] == "Daily Report"
    assert payload["kind"] == "cron"
    assert payload["cron_expr"] == "0 9 * * *"
    assert payload["prompt"] == "summarise"
    # The tool returned a friendly confirmation, not the ORM row.
    assert "sched_abc" in result
    assert "Daily Report" in result


@pytest.mark.asyncio
async def test_schedule_create_tool_returns_error_on_value_error() -> None:
    """Validation errors become a 'validation failed:' string, not a raise."""
    from deerflow.tools.builtins.schedule_tool import schedule_create

    svc = _make_stub_service()
    svc.create.side_effect = ValueError("invalid cron expression: '99 99 99 99 99'")

    with _install_service(svc):
        result = await schedule_create.coroutine(  # type: ignore[attr-defined]
            title="t",
            kind="cron",
            cron_expr="99 99 99 99 99",
            prompt="x",
            target_chat_id="oc_1",
            current_user="u1",
        )

    assert "validation failed" in result.lower()
    assert "invalid cron" in result


@pytest.mark.asyncio
async def test_schedule_create_tool_returns_error_on_permission_error() -> None:
    """Per-user cap errors become a 'permission denied:' string, not a raise."""
    from deerflow.tools.builtins.schedule_tool import schedule_create

    svc = _make_stub_service()
    svc.create.side_effect = PermissionError("max_active_schedules_per_user=2 reached")

    with _install_service(svc):
        result = await schedule_create.coroutine(  # type: ignore[attr-defined]
            title="t",
            kind="cron",
            cron_expr="0 9 * * *",
            prompt="x",
            target_chat_id="oc_1",
            current_user="u1",
        )

    assert "permission denied" in result.lower()
    assert "max_active_schedules_per_user" in result


# ---------------------------------------------------------------------------
# schedule_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_list_tool_returns_text_summary() -> None:
    """schedule_list formats rows as a multi-line text block."""
    from deerflow.tools.builtins.schedule_tool import schedule_list

    svc = _make_stub_service()
    rows = [
        MagicMock(id="s1", title="Daily", status=MagicMock(value="active"), kind=MagicMock(value="cron")),
        MagicMock(id="s2", title="OneShot", status=MagicMock(value="paused"), kind=MagicMock(value="one_shot")),
    ]
    svc.list_for_viewer.return_value = rows

    with _install_service(svc):
        result = await schedule_list.coroutine(  # type: ignore[attr-defined]
            current_user="u1",
            scope="mine",
            limit=20,
        )

    svc.list_for_viewer.assert_awaited_once_with(viewer="u1", scope="mine", status=None, limit=20)
    assert "Daily" in result
    assert "OneShot" in result
    assert "s1" in result
    assert "s2" in result


# ---------------------------------------------------------------------------
# schedule_get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_get_tool_returns_not_found_on_lookup_error() -> None:
    """Missing schedule id becomes a 'not found:' message."""
    from deerflow.tools.builtins.schedule_tool import schedule_get

    svc = _make_stub_service()
    svc.get_for_viewer.return_value = None

    with _install_service(svc):
        result = await schedule_get.coroutine(  # type: ignore[attr-defined]
            schedule_id="missing",
            current_user="u1",
        )

    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_schedule_get_tool_returns_row_summary() -> None:
    """Found row is summarised with id, title, status, kind."""
    from deerflow.tools.builtins.schedule_tool import schedule_get

    svc = _make_stub_service()
    row = MagicMock()
    row.id = "sched_xyz"
    row.title = "Daily"
    row.status = MagicMock(value="active")
    row.kind = MagicMock(value="cron")
    row.cron_expr = "0 9 * * *"
    svc.get_for_viewer.return_value = row

    with _install_service(svc):
        result = await schedule_get.coroutine(  # type: ignore[attr-defined]
            schedule_id="sched_xyz",
            current_user="u1",
        )

    assert "sched_xyz" in result
    assert "Daily" in result


# ---------------------------------------------------------------------------
# schedule_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_update_tool_passes_fields() -> None:
    """schedule_update forwards only the mutated fields to ScheduleService.update."""
    from deerflow.tools.builtins.schedule_tool import schedule_update

    svc = _make_stub_service()
    expected_row = MagicMock(id="s1", title="new")
    svc.update.return_value = expected_row

    with _install_service(svc):
        result = await schedule_update.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
            title="new",
        )

    svc.update.assert_awaited_once()
    # ``schedule_id`` is the positional parameter on ScheduleService.update.
    args, kwargs = svc.update.await_args
    assert args[0] == "s1"
    assert kwargs["current_user"] == "u1"
    assert kwargs["fields"] == {"title": "new"}
    assert "s1" in result


@pytest.mark.asyncio
async def test_schedule_update_tool_ignores_unknown_fields() -> None:
    """The Pydantic schema drops unknown fields, so the service is not called when nothing else was supplied.

    ``owner_user_id`` is not part of the tool's input schema (the
    service enforces a strict whitelist anyway), so the Pydantic
    layer silently drops it. With nothing left to forward, the tool
    short-circuits with a friendly message instead of calling the
    service.
    """
    from deerflow.tools.builtins.schedule_tool import schedule_update

    svc = _make_stub_service()
    expected_row = MagicMock(id="s1", title="t")
    svc.update.return_value = expected_row

    with _install_service(svc):
        result = await schedule_update.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
            owner_user_id="u2",  # not in the tool's schema
        )

    # Service was NOT called (no fields to forward).
    svc.update.assert_not_awaited()
    # Empty fields is a tool-level no-op, not a service error.
    assert "no fields" in result.lower()


@pytest.mark.asyncio
async def test_schedule_update_tool_returns_error_on_value_error() -> None:
    """If the service raises ValueError, the tool returns 'validation failed:'."""
    from deerflow.tools.builtins.schedule_tool import schedule_update

    svc = _make_stub_service()
    svc.update.side_effect = ValueError("update: cannot modify fields: ['source']")

    with _install_service(svc):
        result = await schedule_update.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
            title="new",  # legitimate
        )

    assert "validation failed" in result.lower()


# ---------------------------------------------------------------------------
# Lifecycle: pause / resume / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_pause_tool_returns_confirmation() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_pause

    svc = _make_stub_service()
    with _install_service(svc):
        result = await schedule_pause.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
        )
    svc.pause.assert_awaited_once_with("s1", current_user="u1")
    assert "s1" in result
    assert "pause" in result.lower() or "paused" in result.lower()


@pytest.mark.asyncio
async def test_schedule_pause_tool_returns_permission_error_message() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_pause

    svc = _make_stub_service()
    svc.pause.side_effect = PermissionError("only the owner can act on a schedule")

    with _install_service(svc):
        result = await schedule_pause.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u2",
        )

    assert "permission denied" in result.lower()


@pytest.mark.asyncio
async def test_schedule_resume_tool_returns_confirmation() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_resume

    svc = _make_stub_service()
    with _install_service(svc):
        result = await schedule_resume.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
        )
    svc.resume.assert_awaited_once_with("s1", current_user="u1")
    assert "s1" in result


@pytest.mark.asyncio
async def test_schedule_delete_tool_returns_confirmation() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_delete

    svc = _make_stub_service()
    with _install_service(svc):
        result = await schedule_delete.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
        )
    svc.soft_delete.assert_awaited_once_with("s1", current_user="u1")
    assert "s1" in result


@pytest.mark.asyncio
async def test_schedule_delete_tool_returns_not_found_on_lookup_error() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_delete

    svc = _make_stub_service()
    svc.soft_delete.side_effect = LookupError("schedule 'missing' not found")

    with _install_service(svc):
        result = await schedule_delete.coroutine(  # type: ignore[attr-defined]
            schedule_id="missing",
            current_user="u1",
        )

    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_subscribe_tool_delegates_to_service() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_subscribe

    svc = _make_stub_service()
    with _install_service(svc):
        result = await schedule_subscribe.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u2",
        )
    svc.subscribe.assert_awaited_once()
    # ``schedule_id`` is the positional parameter on ScheduleService.subscribe.
    args, kwargs = svc.subscribe.await_args
    assert args[0] == "s1"
    assert kwargs["user_id"] == "u2"
    assert "s1" in result


@pytest.mark.asyncio
async def test_schedule_unsubscribe_tool_delegates_to_service() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_unsubscribe

    svc = _make_stub_service()
    with _install_service(svc):
        result = await schedule_unsubscribe.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u2",
        )
    svc.unsubscribe.assert_awaited_once_with("s1", user_id="u2")
    assert "s1" in result


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_runs_tool_returns_history_summary() -> None:
    from deerflow.tools.builtins.schedule_tool import schedule_runs

    svc = _make_stub_service()
    run = MagicMock()
    run.id = "r1"
    run.status = MagicMock(value="succeeded")
    run.attempt = 1
    run.error_summary = None
    run.started_at = None
    run.finished_at = None
    svc.list_runs.return_value = [run]

    with _install_service(svc):
        result = await schedule_runs.coroutine(  # type: ignore[attr-defined]
            schedule_id="s1",
            current_user="u1",
            limit=10,
        )

    svc.list_runs.assert_awaited_once_with("s1", viewer="u1", limit=10)
    assert "r1" in result
    assert "succeeded" in result.lower()


# ---------------------------------------------------------------------------
# Gate: scheduling_enabled kwarg
# ---------------------------------------------------------------------------


def _explicit_config() -> Any:
    """Build a minimal ``AppConfig``-shaped namespace for the gate tests.

    The real config may try to load MCP / extensions from the live
    filesystem; the gate tests are about the ``scheduling_enabled``
    kwarg plumbing, not the config loader. A ``SimpleNamespace`` with
    the minimum attributes ``get_available_tools`` reads is enough.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        tools=[],
        models=[],
        tool_search=SimpleNamespace(enabled=False),
        skill_evolution=SimpleNamespace(enabled=False),
        sandbox=SimpleNamespace(),
        get_model_config=lambda name: None,
        acp_agents={},
    )


def test_get_available_tools_excludes_schedule_tools_by_default() -> None:
    """scheduling_enabled=False (the default) does NOT include the schedule tools."""
    from deerflow.tools import get_available_tools

    config = _explicit_config()
    tools = get_available_tools(
        include_mcp=False,
        subagent_enabled=False,
        scheduling_enabled=False,
        app_config=config,
    )
    names = {t.name for t in tools}
    assert "schedule_create" not in names
    assert "schedule_list" not in names


def test_get_available_tools_includes_schedule_tools_when_enabled() -> None:
    """scheduling_enabled=True includes the schedule tools in the list."""
    from deerflow.tools import get_available_tools

    config = _explicit_config()
    tools = get_available_tools(
        include_mcp=False,
        subagent_enabled=False,
        scheduling_enabled=True,
        app_config=config,
    )
    names = {t.name for t in tools}
    assert "schedule_create" in names
    assert "schedule_list" in names
    assert "schedule_get" in names
    assert "schedule_update" in names
    assert "schedule_pause" in names
    assert "schedule_resume" in names
    assert "schedule_delete" in names
    assert "schedule_subscribe" in names
    assert "schedule_unsubscribe" in names
    assert "schedule_runs" in names


# ---------------------------------------------------------------------------
# Service accessor contract
# ---------------------------------------------------------------------------


def test_schedule_tool_uses_module_level_service_accessor() -> None:
    """The tools read the service through ``get_schedule_service``.

    This is the seam the test suite uses to inject a stub. The accessor
    lives in :mod:`deerflow.tools.builtins.schedule_tool` (a re-export
    of the lifespan getter) so the test can patch it in one place.
    """
    from deerflow.tools.builtins import schedule_tool

    assert hasattr(schedule_tool, "get_schedule_service")
    assert callable(schedule_tool.get_schedule_service)


# ---------------------------------------------------------------------------
# target_json serialisation
# ---------------------------------------------------------------------------


def test_schedule_create_serialises_target_to_json() -> None:
    """The tool builds target_json as a JSON string in the Feishu channel shape."""
    from deerflow.tools.builtins.schedule_tool import _build_target_json

    out = _build_target_json("oc_abc", "feishu", connection_id="conn_1")
    parsed = json.loads(out)
    assert parsed["channel"] == "feishu"
    assert parsed["chat_id"] == "oc_abc"
    assert parsed["connection_id"] == "conn_1"
