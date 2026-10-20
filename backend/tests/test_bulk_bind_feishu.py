"""Tests for the one-shot ``scripts/bulk_bind_feishu.py`` ops tool.

These tests bootstrap an in-memory SQLite database via ``init_engine`` (the
same call the script uses) and drive ``_ensure_user`` / ``_ensure_binding``
directly — no subprocess, no real Gateway.  The partial unique index
``uq_channel_connection_active_identity`` is exercised end-to-end to prove
ownership transfer works through the same repository the script uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelConnectionRow,
)
from deerflow.persistence.channel_connections.sql import (
    ChannelConnectionRepository as _ChanRepoShim,  # noqa: F401  (consistency)
)
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.user.model import UserRow
from scripts.bulk_bind_feishu import (
    _ensure_binding,
    _ensure_user,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'bulk.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield
    finally:
        await close_engine()


@pytest.fixture
def user_repo(db) -> SQLiteUserRepository:
    return SQLiteUserRepository(get_session_factory())


@pytest.fixture
def conn_repo(db) -> ChannelConnectionRepository:
    return ChannelConnectionRepository(get_session_factory())


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------


class TestEnsureUser:
    @pytest.mark.anyio
    async def test_creates_new_user_with_hashed_password(self, user_repo: SQLiteUserRepository):
        row = {
            "deerflow_email": "alice@example.com",
            "action": "upsert_all",
            "password": "S3cret!",
            "system_role": "user",
            "needs_setup": "",
            "oauth_provider": "",
            "oauth_id": "",
        }
        user_id, plan = await _ensure_user(
            user_repo,
            row=row,
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        assert user_id is not None
        assert plan["result"] == "created"
        assert plan["will_set_password"] is True

        persisted = await user_repo.get_user_by_email("alice@example.com")
        assert persisted is not None
        assert persisted.system_role == "user"
        assert persisted.password_hash is not None
        assert persisted.password_hash.startswith("$dfv2$")
        # bcrypt v2 verifies back to the original plaintext.
        from app.gateway.auth.password import verify_password

        assert verify_password("S3cret!", persisted.password_hash)

    @pytest.mark.anyio
    async def test_existing_user_reused_without_allow_update(self, user_repo: SQLiteUserRepository):
        first, _ = await _ensure_user(
            user_repo,
            row={"deerflow_email": "bob@example.com", "password": "original", "action": "upsert_all"},
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        second, plan = await _ensure_user(
            user_repo,
            row={"deerflow_email": "bob@example.com", "password": "different", "action": "upsert_all"},
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        assert second == first
        assert plan["result"] == "reused"
        # ``password_hash`` was NOT overwritten.
        from app.gateway.auth.password import verify_password

        persisted = await user_repo.get_user_by_email("bob@example.com")
        assert verify_password("original", persisted.password_hash)
        assert not verify_password("different", persisted.password_hash)

    @pytest.mark.anyio
    async def test_allow_update_only_changes_differing_fields(self, user_repo: SQLiteUserRepository):
        _, _ = await _ensure_user(
            user_repo,
            row={
                "deerflow_email": "carol@example.com",
                "system_role": "user",
                "password": "Initial!",
                "action": "upsert_all",
            },
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )

        # Promote to admin — different ``system_role`` triggers an update
        # under ``--allow-update``.  Password follows as a side-effect.
        _, plan = await _ensure_user(
            user_repo,
            row={
                "deerflow_email": "carol@example.com",
                "system_role": "admin",
                "password": "Rotated!",
                "action": "upsert_all",
            },
            password_source="column",
            allow_update=True,
            new_passwords={},
            dry_run=False,
        )
        assert plan["result"] == "updated"
        after = await user_repo.get_user_by_email("carol@example.com")
        assert after.system_role == "admin"
        from app.gateway.auth.password import verify_password

        assert verify_password("Rotated!", after.password_hash)

        # Empty password column + same role + same needs_setup must be a
        # no-op ("reused"): the stored hash is preserved and no UPDATE is
        # issued.  Idempotency across re-runs.  ``needs_setup`` is pinned
        # explicitly to ``false`` because the row was created with a
        # password and so has ``needs_setup=false``; letting it auto-resolve
        # from the empty password would flip the bit and defeat the test.
        _, plan = await _ensure_user(
            user_repo,
            row={
                "deerflow_email": "carol@example.com",
                "system_role": "admin",
                "password": "",
                "needs_setup": "false",
                "action": "upsert_all",
            },
            password_source="off",
            allow_update=True,
            new_passwords={},
            dry_run=False,
        )
        assert plan["result"] == "reused"
        persisted = await user_repo.get_user_by_email("carol@example.com")
        # Original "Rotated!" hash is still verifiable — empty password column
        # is interpreted as "leave the password alone".
        assert verify_password("Rotated!", persisted.password_hash)

    @pytest.mark.anyio
    async def test_bind_only_skips_when_user_missing(self, user_repo: SQLiteUserRepository):
        user_id, plan = await _ensure_user(
            user_repo,
            row={"deerflow_email": "ghost@example.com", "action": "bind_only"},
            password_source="off",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        assert user_id is None
        assert plan["result"] == "skip-no-user"

    @pytest.mark.anyio
    async def test_password_source_off_creates_passwordless_user(self, user_repo: SQLiteUserRepository):
        user_id, plan = await _ensure_user(
            user_repo,
            row={"deerflow_email": "ops@example.com", "password": "", "action": "upsert_all", "needs_setup": ""},
            password_source="off",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        assert plan["will_set_password"] is False
        assert plan["needs_setup"] is True
        persisted = await user_repo.get_user_by_email("ops@example.com")
        assert persisted.password_hash is None
        assert persisted.needs_setup is True

    @pytest.mark.anyio
    async def test_password_source_auto_generates_password_per_user(self, user_repo: SQLiteUserRepository):
        generated: dict[str, str] = {}
        for email in ("a@example.com", "b@example.com"):
            await _ensure_user(
                user_repo,
                row={"deerflow_email": email, "password": "", "action": "upsert_all"},
                password_source="auto",
                allow_update=False,
                new_passwords=generated,
                dry_run=False,
            )
        assert len(generated) == 2
        assert all(len(pw) >= 20 for pw in generated.values())
        # Same email → same email key in ledger; different users must not collide.
        assert set(generated.keys()) == {"a@example.com", "b@example.com"}

    @pytest.mark.anyio
    async def test_dry_run_creates_nothing(self, user_repo: SQLiteUserRepository):
        user_id, plan = await _ensure_user(
            user_repo,
            row={"deerflow_email": "phantom@example.com", "password": "x", "action": "upsert_all"},
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=True,
        )
        # Dry-run assigns a synthetic id for downstream reference but never writes.
        assert user_id is not None
        assert plan["result"] == "would-create"

        async with get_session_factory()() as session:
            result = await session.execute(select(UserRow).where(UserRow.email == "phantom@example.com"))
            assert result.scalar_one_or_none() is None

    @pytest.mark.anyio
    async def test_email_lookup_is_case_insensitive(self, user_repo: SQLiteUserRepository):
        await _ensure_user(
            user_repo,
            row={"deerflow_email": "Mixed@Example.com", "password": "p", "action": "upsert_all"},
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        # Re-listing the same user with a different case must hit the existing row.
        _, plan = await _ensure_user(
            user_repo,
            row={"deerflow_email": "mixed@example.com", "password": "p", "action": "upsert_all"},
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        assert plan["result"] == "reused"


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


class TestEnsureBinding:
    @pytest.mark.anyio
    async def test_writes_a_feishu_connection(self, conn_repo: ChannelConnectionRepository):
        row = {
            "feishu_open_id": "ou_alice0000000001",
            "feishu_chat_id": "oc_chat0001",
            "feishu_user_name": "Alice",
            "feishu_bot_open_id": "ou_bot000000000001",
            "scopes": '["im:message"]',
        }
        plan = await _ensure_binding(conn_repo, user_id="user-alice", row=row, dry_run=False)
        assert plan["result"] == "bound"

        async with get_session_factory()() as session:
            db_row = (
                await session.execute(
                    select(ChannelConnectionRow).where(
                        ChannelConnectionRow.provider == "feishu",
                        ChannelConnectionRow.external_account_id == "ou_alice0000000001",
                        ChannelConnectionRow.workspace_id == "oc_chat0001",
                    )
                )
            ).scalar_one()
            assert db_row.owner_user_id == "user-alice"
            assert db_row.status == "connected"
            assert db_row.external_account_name == "Alice"
            assert db_row.bot_user_id == "ou_bot000000000001"
            # ``scopes_json`` is mapped as SQLAlchemy ``JSON``, so SQLite
            # round-trips it to a Python list on read.
            assert db_row.scopes_json == ["im:message"]
            assert db_row.metadata_json == {
                "bound_by": "ops-bulk",
                "source": "bulk_bind_feishu.py",
            }

    @pytest.mark.anyio
    async def test_skips_when_chat_or_user_missing(self, conn_repo: ChannelConnectionRepository):
        plan = await _ensure_binding(
            conn_repo,
            user_id=None,
            row={"feishu_open_id": "ou_x", "feishu_chat_id": "oc_y"},
            dry_run=False,
        )
        assert plan["result"] == "skip-no-user"

        plan = await _ensure_binding(
            conn_repo,
            user_id="user-bob",
            row={"feishu_open_id": "", "feishu_chat_id": "oc_y"},
            dry_run=False,
        )
        assert plan["result"] == "skip-missing-fields"

    @pytest.mark.anyio
    async def test_dry_run_writes_nothing(self, conn_repo: ChannelConnectionRepository):
        plan = await _ensure_binding(
            conn_repo,
            user_id="user-x",
            row={"feishu_open_id": "ou_dry01", "feishu_chat_id": "oc_dry_chat"},
            dry_run=True,
        )
        assert plan["result"] == "would-bind"
        async with get_session_factory()() as session:
            result = await session.execute(
                select(ChannelConnectionRow).where(
                    ChannelConnectionRow.provider == "feishu",
                    ChannelConnectionRow.external_account_id == "ou_dry01",
                )
            )
            assert result.scalar_one_or_none() is None

    @pytest.mark.anyio
    async def test_ownership_transfer_revokes_previous_active_owner(self, conn_repo: ChannelConnectionRepository):
        # Alice binds first.
        await _ensure_binding(
            conn_repo,
            user_id="user-alice",
            row={"feishu_open_id": "ou_shared", "feishu_chat_id": "oc_chat_shared"},
            dry_run=False,
        )
        # Bob's row for the same (provider, external, workspace) takes over.
        await _ensure_binding(
            conn_repo,
            user_id="user-bob",
            row={"feishu_open_id": "ou_shared", "feishu_chat_id": "oc_chat_shared"},
            dry_run=False,
        )
        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(ChannelConnectionRow).where(
                            ChannelConnectionRow.provider == "feishu",
                            ChannelConnectionRow.external_account_id == "ou_shared",
                            ChannelConnectionRow.workspace_id == "oc_chat_shared",
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_owner = {r.owner_user_id: r.status for r in rows}
        assert by_owner == {"user-alice": "revoked", "user-bob": "connected"}


# ---------------------------------------------------------------------------
# End-to-end on a CSV via the file readers (no subprocess)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.anyio
    async def test_csv_create_and_bind(self, tmp_path: Path, user_repo: SQLiteUserRepository, conn_repo: ChannelConnectionRepository):
        from scripts.bulk_bind_feishu import _ensure_binding, _ensure_user, _iter_csv

        csv_path = tmp_path / "fixture.csv"
        csv_path.write_text(
            "deerflow_email,action,password,system_role,needs_setup,oauth_provider,oauth_id,"
            "feishu_open_id,feishu_chat_id,feishu_user_name,feishu_bot_open_id,scopes\n"
            "newby@example.com,upsert_all,H0tStart!2026,user,false,,,ou_xx,oc_zz,Newby,ou_bot,im:message\n",
            encoding="utf-8",
        )

        rows = list(_iter_csv(csv_path))
        assert len(rows) == 1
        user_id, plan = await _ensure_user(
            user_repo,
            row=rows[0],
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        bind_plan = await _ensure_binding(conn_repo, user_id=user_id, row=rows[0], dry_run=False)
        assert plan["result"] == "created"
        assert bind_plan["result"] == "bound"

        async with get_session_factory()() as session:
            row = (
                await session.execute(
                    select(ChannelConnectionRow).where(
                        ChannelConnectionRow.provider == "feishu",
                        ChannelConnectionRow.external_account_id == "ou_xx",
                    )
                )
            ).scalar_one()
            assert row.owner_user_id == user_id
            assert row.status == "connected"

    @pytest.mark.anyio
    async def test_jsonl_create_and_bind(self, tmp_path: Path, user_repo: SQLiteUserRepository, conn_repo: ChannelConnectionRepository):
        from scripts.bulk_bind_feishu import _iter_jsonl

        jsonl_path = tmp_path / "fixture.jsonl"
        jsonl_path.write_text(
            json.dumps(
                {
                    "deerflow_email": "jsonby@example.com",
                    "action": "upsert_all",
                    "password": "S3cr3t",
                    "system_role": "user",
                    "needs_setup": "",
                    "feishu_open_id": "ou_json",
                    "feishu_chat_id": "oc_json",
                    "feishu_user_name": "Jsonby",
                    "feishu_bot_open_id": "ou_bot_json",
                    "scopes": '["im:message"]',
                }
            )
            + "\n",
            encoding="utf-8",
        )

        rows = list(_iter_jsonl(jsonl_path))
        assert len(rows) == 1
        user_id, _ = await _ensure_user(
            user_repo,
            row=rows[0],
            password_source="column",
            allow_update=False,
            new_passwords={},
            dry_run=False,
        )
        bind_plan = await _ensure_binding(conn_repo, user_id=user_id, row=rows[0], dry_run=False)
        assert bind_plan["result"] == "bound"
