"""Regression tests for Fix A (wait_timeout constants) and Fix B
(blocking=True subprocess handling) in lark_cli.py.

Context — chenzhongliang@163.com hit a hard loop on 2026-10-15 where
``lark-cli auth login --device-code`` was SIGKILLed by the wrapper
``subprocess.run(timeout=8)`` on every frontend auto-poll. By the time
the user clicked Authorize, the device_code had been polled so many
times that Lark's replay protection surfaced ``device_code is invalid``
on every subsequent retry. The two changes here together let the
wrapper (a) bound a human-OAuth-friendly wait window and (b) hand
lark-cli a graceful SIGTERM with a 5s flush window before escalating
to SIGKILL.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from deerflow.integrations import lark_cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_stub_popen(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    first_communicate_times_out: bool = False,
    second_communicate_also_times_out: bool = False,
):
    """Build a ``subprocess.Popen`` stub for the cases
    ``_run_lark_cli_json`` exercises under ``blocking=True``.

    The stub records every instance on the class so tests can inspect
    ``terminated`` / ``killed`` flags after the wrapper runs.
    """
    # Lift the response strings into closure cells so Popen's
    # ``stdout=PIPE`` / ``stderr=PIPE`` kwargs do not shadow them.
    _response_stdout = stdout
    _response_stderr = stderr

    class _StubPopen:
        instances: list[_StubPopen] = []

        def __init__(self, args, *, stdin=None, stdout=None, stderr=None, text=None, env=None, **kwargs):
            self.args = args
            # Popen passes ``stdout=PIPE`` / ``stderr=PIPE`` (ints);
            # the actual response content comes from the closure so
            # the kwargs do not shadow the response we want to return.
            self._stdout_response = _response_stdout
            self._stderr_response = _response_stderr
            self._returncode = returncode
            self.returncode = returncode  # exposed for the wrapper's CompletedProcess construction
            self._first_timeout = first_communicate_times_out
            self._second_timeout = second_communicate_also_times_out
            self._comm_count = 0
            self.terminated = False
            self.killed = False
            self._poll = None  # running until killed (terminate() leaves it running)
            type(self).instances.append(self)

        def communicate(self, timeout=None):
            self._comm_count += 1
            if self._first_timeout and self._comm_count == 1 and timeout is not None:
                raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
            if self._second_timeout and self._comm_count == 2 and timeout is not None:
                raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
            # Mirror real ``Popen.communicate``: once it returns, the
            # process is reaped and ``poll()`` reports the exit code.
            self._poll = self._returncode
            return (self._stdout_response, self._stderr_response)

        def terminate(self):
            self.terminated = True
            # Simulate lark-cli not exiting immediately on SIGTERM
            # so the test can verify the SIGKILL escalation path.
            # ``poll()`` stays None until ``kill()`` is called.

        def kill(self):
            self.killed = True
            self._poll = 0
            self._returncode = -9

        def poll(self):
            return self._poll

    return _StubPopen


@pytest.fixture
def user_id() -> str:
    return "5edd1a34-daca-4316-8c97-a4e5e2b97918"


@pytest.fixture(autouse=True)
def _stub_lark_cli_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lark-cli wrapper writes a debug log under the user bucket and
    hardens permissions on the credential tree. Stub both so tests do
    not require root or a real lark-cli install.
    """
    monkeypatch.setattr(lark_cli, "lark_cli_env", lambda user_id: {})
    monkeypatch.setattr(
        lark_cli,
        "ensure_lark_cli_credential_tree",
        lambda user_id, paths=None: None,
    )


# ---------------------------------------------------------------------------
# Fix A: LARK_AUTH_COMPLETE_*_WAIT_SECONDS must allow human OAuth
# ---------------------------------------------------------------------------
def test_lark_auth_complete_constants_allow_human_oauth_window() -> None:
    """The default wait must give a real human (open verification URL,
    click Authorize, optionally confirm a long scope list) enough time
    to complete the browser dance. The previous 45s default + 45s
    ceiling caused every interactive OAuth flow to time out before the
    user finished — and the wrapper was SIGKILLing lark-cli on every
    auto-poll, losing the device-code to Lark's replay protection.
    """
    default = lark_cli.LARK_AUTH_COMPLETE_DEFAULT_WAIT_SECONDS
    minimum = lark_cli.LARK_AUTH_COMPLETE_MIN_WAIT_SECONDS
    maximum = lark_cli.LARK_AUTH_COMPLETE_MAX_WAIT_SECONDS

    # Default must comfortably fit a slow human OAuth.
    assert default >= 60, f"DEFAULT={default}s is too short for a real human OAuth flow"
    # The default must remain inside the legal range.
    assert minimum <= default <= maximum
    # MAX must allow at least one slow attempt per call.
    assert maximum >= 120, f"MAX={maximum}s is too low; the ceiling must allow a full attempt"
    # MIN must stay small enough that an auto-poll pattern (UI side) is
    # not blocked at FastAPI validation.
    assert minimum <= 10, f"MIN={minimum}s is too high; auto-poll requests below MIN would 422"


# ---------------------------------------------------------------------------
# Fix B: blocking=True graceful shutdown via Popen
# ---------------------------------------------------------------------------
def test_run_lark_cli_json_blocking_uses_popen(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """``blocking=True`` must spawn lark-cli via ``Popen`` (not
    ``subprocess.run``) so the wrapper can SIGTERM gracefully on
    timeout. ``subprocess.run`` does an unconditional SIGKILL on
    timeout, which loses any in-flight token writes.
    """
    stub = _make_stub_popen(stdout=json.dumps({"ok": True, "verification_url": "https://x", "device_code": "abc"}))
    monkeypatch.setattr(lark_cli.subprocess, "Popen", stub)
    monkeypatch.setattr(
        lark_cli.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("subprocess.run must not be used when blocking=True")),
    )

    parsed = lark_cli._run_lark_cli_json(
        ["lark-cli", "auth", "login", "--no-wait", "--json"],
        user_id=user_id,
        timeout=5,
        blocking=True,
    )

    assert parsed["ok"] is True
    assert parsed["device_code"] == "abc"
    assert len(stub.instances) == 1
    assert stub.instances[0].terminated is False
    assert stub.instances[0].killed is False


def test_run_lark_cli_json_blocking_sigterm_only_on_graceful_exit(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """On timeout with ``blocking=True`` and a cooperative lark-cli,
    the wrapper must SIGTERM (giving lark-cli 5s to flush) but never
    escalate to SIGKILL. Hard-killing lark-cli mid-poll is the root
    cause of "device_code is invalid" after a successful authorize.
    """
    stub = _make_stub_popen(
        first_communicate_times_out=True,
        second_communicate_also_times_out=False,
    )
    monkeypatch.setattr(lark_cli.subprocess, "Popen", stub)

    with pytest.raises(TimeoutError):
        lark_cli._run_lark_cli_json(
            ["lark-cli", "auth", "login", "--device-code", "X", "--json"],
            user_id=user_id,
            timeout=1,
            blocking=True,
        )

    instance = stub.instances[0]
    assert instance.terminated is True
    assert instance.killed is False


def test_run_lark_cli_json_blocking_escalates_to_sigkill_after_grace(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """If lark-cli ignores the graceful SIGTERM within 5s, the wrapper
    must escalate to SIGKILL so we do not leak a zombie subprocess
    that holds the user bucket's data dir.
    """
    stub = _make_stub_popen(
        first_communicate_times_out=True,
        second_communicate_also_times_out=True,
    )
    monkeypatch.setattr(lark_cli.subprocess, "Popen", stub)

    with pytest.raises(TimeoutError):
        lark_cli._run_lark_cli_json(
            ["lark-cli", "auth", "login", "--device-code", "X", "--json"],
            user_id=user_id,
            timeout=1,
            blocking=True,
        )

    instance = stub.instances[0]
    assert instance.terminated is True
    assert instance.killed is True


def test_run_lark_cli_json_non_blocking_preserves_subprocess_run(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """Backward compat: ``blocking=False`` (default) must keep using
    ``subprocess.run`` so existing call sites (e.g. ``auth status``,
    ``config init``) and the existing test suite do not change.
    """
    seen = {"subprocess_run_called": False, "popen_called": False}

    def _fake_run(*args, **kwargs):
        seen["subprocess_run_called"] = True
        return subprocess.CompletedProcess(
            args=kwargs.get("args") or (args[0] if args else []),
            returncode=0,
            stdout=json.dumps({"ok": True, "appId": "cli_x"}),
            stderr="",
        )

    def _fake_popen(*args, **kwargs):
        seen["popen_called"] = True
        raise AssertionError("Popen must not be used when blocking=False")

    monkeypatch.setattr(lark_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(lark_cli.subprocess, "Popen", _fake_popen)

    parsed = lark_cli._run_lark_cli_json(
        ["lark-cli", "auth", "status", "--json"],
        user_id=user_id,
        timeout=5,
    )

    assert parsed["ok"] is True
    assert seen["subprocess_run_called"] is True
    assert seen["popen_called"] is False


# ---------------------------------------------------------------------------
# Fix B integration: complete_lark_auth must pass blocking=True
# ---------------------------------------------------------------------------
def test_complete_lark_auth_passes_blocking_true(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """``complete_lark_auth`` is the only call site for
    ``auth login --device-code`` and is a human-in-the-loop flow.
    Verify it routes through ``_run_lark_cli_json(..., blocking=True)``
    so the wrapper gives lark-cli a graceful SIGTERM window.

    The precheck short-circuits when the user is already authenticated,
    so we stub ``probe_lark_auth`` to return ``not_authorized`` and
    exercise the real call path.
    """
    monkeypatch.setattr(
        lark_cli,
        "probe_lark_auth",
        lambda uid, verify=True: lark_cli.LarkAuthProbe(status="not_authorized", message="not yet"),
    )

    @contextmanager
    def _noop_lock(uid):
        yield

    monkeypatch.setattr(lark_cli, "_lark_credential_lock", _noop_lock)
    monkeypatch.setattr(lark_cli, "_require_lark_flow_generation_locked", lambda uid, gen: gen)
    monkeypatch.setattr(lark_cli, "_require_lark_cli_path", lambda: "/usr/bin/lark-cli")

    captured_kwargs: dict = {}

    def _capture(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return {}

    monkeypatch.setattr(lark_cli, "_run_lark_cli_json", _capture)

    @dataclass
    class _FakeAuth:
        status: str = "authenticated"
        message: str = "ok"
        user: str | None = "陈忠良"
        verified: bool = True

    @dataclass
    class _FakeStatus:
        auth: _FakeAuth | None = None

        def __post_init__(self):
            if self.auth is None:
                self.auth = _FakeAuth()

    monkeypatch.setattr(
        lark_cli,
        "get_lark_integration_status",
        lambda uid, config, verify_auth=False: _FakeStatus(),
    )

    result = lark_cli.complete_lark_auth(
        user_id,
        config=None,  # type: ignore[arg-type]
        device_code="dev123",
        generation="g1",
        wait_timeout_seconds=10,
    )

    assert captured_kwargs.get("blocking") is True, f"expected blocking=True, got {captured_kwargs!r}"
    assert captured_kwargs.get("user_id") == user_id
    assert captured_kwargs.get("timeout") == 10
    assert captured_kwargs.get("allow_empty_success") is True
    # The user is treated as authenticated by the fake status, so the
    # post-call status should bubble up as success.
    assert result.success is True
