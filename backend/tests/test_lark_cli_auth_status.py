"""Regression tests for the silent-failure modes in lark-cli invocation
and probe. See the diagnostic note in
``deerflow/integrations/lark_cli.py`` (search "debug instrumentation").
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from deerflow.integrations import lark_cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fake_lark_cli_result(*, returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["lark-cli"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def user_id() -> str:
    return "5edd1a34-daca-4316-8c97-a4e5e2b97918"


@pytest.fixture(autouse=True)
def _stub_lark_cli_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lark-cli wrapper writes a debug log under the user bucket and
    hardens permissions on the credential tree. Stub both so tests do not
    require root or a real lark-cli install.
    """
    monkeypatch.setattr(lark_cli, "lark_cli_env", lambda user_id: {})
    monkeypatch.setattr(
        lark_cli,
        "ensure_lark_cli_credential_tree",
        lambda user_id, paths=None: None,
    )


# ---------------------------------------------------------------------------
# Fix 1: _run_lark_cli_json must surface lark-cli business failures
# ---------------------------------------------------------------------------
def test_run_lark_cli_json_raises_on_ok_false_business_failure(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """lark-cli reports business failures with exit 0 and
    ``{"ok": false, "error": {"message": "..."}}``. The wrapper must
    raise ``ValueError`` so the FastAPI layer can surface the verbatim
    lark-cli error to the user instead of silently treating the call as
    success.
    """
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "authentication",
                    "subtype": "unknown",
                    "message": ("authorization failed: The device_code is invalid. Please restart the device authorization flow."),
                },
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)

    with pytest.raises(ValueError) as excinfo:
        lark_cli._run_lark_cli_json(
            ["lark-cli", "auth", "login", "--device-code", "REDACTED-XXXX"],
            user_id=user_id,
            timeout=20,
            allow_empty_success=True,
        )

    msg = str(excinfo.value)
    assert "device_code is invalid" in msg
    assert "restart" in msg.lower()


def test_run_lark_cli_json_passes_through_on_ok_true(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """The ``ok: true`` / non-error case must still return the parsed dict
    unchanged — this regression guard ensures Fix 1 does not regress the
    happy path.
    """
    payload = {
        "ok": True,
        "verification_url": "https://example.com/device",
        "device_code": "real-device-code",
        "expires_in": 600,
    }
    fake = _fake_lark_cli_result(returncode=0, stdout=json.dumps(payload))
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)

    parsed = lark_cli._run_lark_cli_json(
        ["lark-cli", "auth", "login", "--no-wait", "--json"],
        user_id=user_id,
        timeout=20,
    )

    assert parsed == payload


def test_run_lark_cli_json_still_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """The pre-existing nonzero-exit code path must keep working. The
    error message is sourced from the JSON ``error`` envelope.
    """
    fake = _fake_lark_cli_result(
        returncode=2,
        stdout=json.dumps({"error": {"message": "lark-cli is on fire"}}),
        stderr="",
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)

    with pytest.raises(ValueError) as excinfo:
        lark_cli._run_lark_cli_json(
            ["lark-cli", "auth", "status", "--json"],
            user_id=user_id,
            timeout=8,
        )
    assert "lark-cli is on fire" in str(excinfo.value)


def test_run_lark_cli_json_writes_debug_log_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_id: str) -> None:
    """Fix 3: when the wrapper raises, ``.lark-cli-debug.log`` must be
    written to the user bucket with the redacted lark-cli stdout. We
    monkeypatch ``_lark_cli_credential_root`` to redirect to ``tmp_path``
    so the test does not need a real home directory.
    """
    monkeypatch.setattr(
        lark_cli,
        "_lark_cli_credential_root",
        lambda user_id: tmp_path / "lark-cli",
    )
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "ok": False,
                "error": {
                    "message": ("authorization failed: The device_code is invalid. Please restart the device authorization flow."),
                },
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)

    with pytest.raises(ValueError):
        lark_cli._run_lark_cli_json(
            ["lark-cli", "auth", "login", "--device-code", "should-be-redacted"],
            user_id=user_id,
            timeout=20,
        )

    log_path = tmp_path / "lark-cli" / ".lark-cli-debug.log"
    assert log_path.is_file(), f"expected debug log at {log_path}"
    log_path.chmod(0o600)
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(line)
    assert record["returncode"] == 0
    assert "device_code is invalid" in record["stdout"]
    # Device code in argv must be redacted.
    args_blob = json.dumps(record["args"], ensure_ascii=False)
    assert "should-be-redacted" not in args_blob


# ---------------------------------------------------------------------------
# Fix 2: probe_lark_auth must honor identities.user.status
# ---------------------------------------------------------------------------
def test_probe_lark_auth_returns_not_authorized_when_user_status_missing(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """When lark-cli's ``auth status --json`` returns
    ``identities.user.status = "missing"`` with exit 0, ``probe_lark_auth``
    must return ``not_authorized`` — not ``authenticated``. Without Fix 2,
    the user sees "authenticated" in the probe while the lark-cli-side
    view says "missing", and the UI text "用户身份 missing" no longer
    matches the upper layer's status.
    """
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "appId": "cli_aa9942e7f6fa1cbb",
                "identities": {
                    "user": {
                        "status": "missing",
                        "available": False,
                        "message": "User identity: missing (no user logged in)",
                    }
                },
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(
        lark_cli,
        "read_lark_app_config",
        lambda user_id: {"configured": True, "app_id": "cli_aa9942e7f6fa1cbb", "brand": "feishu"},
    )
    monkeypatch.setattr(lark_cli, "_resolve_lark_cli_path", lambda: "/usr/bin/lark-cli")

    probe = lark_cli.probe_lark_auth(user_id, verify=False)
    assert probe.status == "not_authorized"
    assert "missing" in (probe.message or "").lower()


def test_probe_lark_auth_returns_authenticated_when_user_status_ok(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """Regression guard for Fix 2: when the user identity is actually
    ``ok`` (the happy path), the probe must still return ``authenticated``.
    """
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "appId": "cli_aa9942e7f6fa1cbb",
                "identities": {
                    "user": {
                        "status": "ok",
                        "available": True,
                        "userName": "陈某某",
                        "openId": "ou_0407b9e6f3ff76c9335e43aa2ea094c7",
                    }
                },
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(
        lark_cli,
        "read_lark_app_config",
        lambda user_id: {"configured": True, "app_id": "cli_aa9942e7f6fa1cbb", "brand": "feishu"},
    )
    monkeypatch.setattr(lark_cli, "_resolve_lark_cli_path", lambda: "/usr/bin/lark-cli")

    probe = lark_cli.probe_lark_auth(user_id, verify=True)
    assert probe.status == "authenticated"
    assert probe.user == "陈某某"
    assert probe.verified is True


# ---------------------------------------------------------------------------
# Fix 3: probe_lark_auth must accept lark-cli v1.0+ status values
# ---------------------------------------------------------------------------
def test_probe_lark_auth_returns_authenticated_when_user_status_ready(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """Regression for the chenzhongliang@163.com incident: lark-cli
    v1.0.65 (the version this Gateway ships) reports a successful
    authorization as ``identities.user.status = "ready"``, not
    ``"ok"``. The probe must accept the value lark-cli actually emits,
    otherwise a user with a fully-working token is told "授权失败".

    Captured live from a working auth on 2026-10-19: lark-cli returned
    ``status="ready"``, ``available=true``, ``verified=true``,
    ``tokenStatus="valid"`` and the wrapper nevertheless surfaced
    ``not_authorized``.
    """
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "appId": "cli_aa9942e7f6fa1cbb",
                "brand": "feishu",
                "defaultAs": "auto",
                "identities": {
                    "bot": {
                        "status": "ready",
                        "available": True,
                        "verified": True,
                        "openId": "ou_9d95237df5c4805c8f9a6b9de4e28fde",
                        "appName": "虚拟运行值班员",
                    },
                    "user": {
                        "status": "ready",
                        "available": True,
                        "verified": True,
                        "message": "User identity: ready",
                        "openId": "ou_0407b9e6f3ff76c9335e43aa2ea094c7",
                        "userName": "陈忠良",
                        "tokenStatus": "valid",
                        "scope": "im:message offline_access",
                        "expiresAt": "2026-10-19T07:47:49Z",
                        "refreshExpiresAt": "2026-10-26T05:47:49Z",
                        "grantedAt": "2026-10-19T02:45:58Z",
                    },
                },
                "identity": "user",
                "verified": True,
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(
        lark_cli,
        "read_lark_app_config",
        lambda user_id: {"configured": True, "app_id": "cli_aa9942e7f6fa1cbb", "brand": "feishu"},
    )
    monkeypatch.setattr(lark_cli, "_resolve_lark_cli_path", lambda: "/usr/bin/lark-cli")

    probe = lark_cli.probe_lark_auth(user_id, verify=True)
    assert probe.status == "authenticated", f"expected authenticated, got {probe.status!r} with message {probe.message!r}"
    assert probe.user == "陈忠良"
    assert probe.verified is True


def test_probe_lark_auth_returns_authenticated_when_user_status_needs_refresh_and_server_verified(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """lark-cli reports ``status="needs_refresh"`` between the moment a
    fresh token is written and the moment the next call flips the local
    view back to ``"ready"``. The wrapper must not classify that window
    as "not authorized" — when ``verified=True`` is set (i.e. the
    ``--verify`` flag ran and the server confirmed the token), the user
    IS authenticated. The lark-cli message itself reads "server
    verification succeeded after refresh".
    """
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "appId": "cli_aa9942e7f6fa1cbb",
                "identities": {
                    "user": {
                        "status": "needs_refresh",
                        "available": True,
                        "verified": True,
                        "message": "User identity: needs refresh (server verification succeeded after refresh)",
                        "userName": "陈忠良",
                        "openId": "ou_0407b9e6f3ff76c9335e43aa2ea094c7",
                        "tokenStatus": "valid",
                    }
                },
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(
        lark_cli,
        "read_lark_app_config",
        lambda user_id: {"configured": True, "app_id": "cli_aa9942e7f6fa1cbb", "brand": "feishu"},
    )
    monkeypatch.setattr(lark_cli, "_resolve_lark_cli_path", lambda: "/usr/bin/lark-cli")

    probe = lark_cli.probe_lark_auth(user_id, verify=True)
    assert probe.status == "authenticated", f"expected authenticated, got {probe.status!r} with message {probe.message!r}"
    assert probe.user == "陈忠良"
    assert probe.verified is True


def test_probe_lark_auth_returns_not_authorized_when_user_status_expired(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """An explicitly-bad status (``expired``) must still surface as
    ``not_authorized`` even when ``available=True`` slips through. This
    guards the accept-set widening: we accept the unknown / ready /
    needs_refresh family, but explicit terminal states still fail.
    """
    fake = _fake_lark_cli_result(
        returncode=0,
        stdout=json.dumps(
            {
                "appId": "cli_aa9942e7f6fa1cbb",
                "identities": {
                    "user": {
                        "status": "expired",
                        "available": True,
                        "message": "User identity: expired",
                    }
                },
            }
        ),
    )
    monkeypatch.setattr(lark_cli.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(
        lark_cli,
        "read_lark_app_config",
        lambda user_id: {"configured": True, "app_id": "cli_aa9942e7f6fa1cbb", "brand": "feishu"},
    )
    monkeypatch.setattr(lark_cli, "_resolve_lark_cli_path", lambda: "/usr/bin/lark-cli")

    probe = lark_cli.probe_lark_auth(user_id, verify=False)
    assert probe.status == "not_authorized"
    assert "expired" in (probe.message or "").lower()
