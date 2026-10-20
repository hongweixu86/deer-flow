"""One-shot ops tool: bulk-create DeerFlow users and bulk-bind Feishu open_ids.

Built for two-pass onboarding: create the DeerFlow user (or look it up by
email), then write a ``channel_connections`` row keyed by that user's id
and the Feishu ``open_id`` / ``chat_id`` pair.  Bypasses the one-time
``/connect <code>`` IM flow entirely.

Usage:
    PYTHONPATH=. python scripts/bulk_bind_feishu.py scripts/bulk_bind_feishu.example.csv \\
        [--dry-run] [--allow-update] [--json] \\
        [--password-source {column,auto,off}] [--password-out PATH]

CSV columns (extra columns are optional; see ``bulk_bind_feishu.example.csv``):
    deerflow_email     - existing or new user email (case-insensitive on read)
    action             - create_user | bind_only | upsert_all  (default upsert_all)
    password           - plaintext; hashed via bcrypt v2 (see auth.password);
                         empty -> password_hash=NULL, needs_setup=True
    system_role        - user | admin (default user)
    needs_setup        - true | false (auto when blank: True if no password)
    oauth_provider     - e.g. "github", "google"; empty for password users
    oauth_id           - OAuth subject for ``oauth_provider``
    feishu_open_id     - Feishu ``ou_xxx``
    feishu_chat_id     - Feishu ``oc_xxx``
    feishu_user_name?  - display name
    feishu_bot_open_id? - Feishu bot ``ou_xxx``
    scopes?            - JSON array (preferred) or comma-separated list

Flags:
    --dry-run         print the plan, do not write
    --allow-update    when the user already exists, update password / role /
                      needs_setup / oauth on the existing row instead of
                      skipping those fields
    --json            read JSONL instead of CSV
    --password-source where the stored password comes from:
                         column (read the ``password`` column, default)
                         auto   (generate a random per-user password, write
                                the cleartext to ``--password-out``)
                         off    (never set a password: password_hash=None,
                                needs_setup=true)
    --password-out    ledger path when --password-source=auto
                      (default ``./bulk_bind_passwords.csv``)

The script uses ``init_engine`` so it picks up the same DATABASE_URL /
config.yaml as the running Gateway.  Run on the same host with the same
``$DEER_FLOW_HOME`` / ``$DATABASE_URL`` and stop the Gateway only if you
want the binding not to race against in-flight inbound messages.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import secrets
import string
import sys
from pathlib import Path
from uuid import UUID, uuid4

from app.gateway.auth.models import User
from app.gateway.auth.password import hash_password
from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository
from deerflow.persistence.engine import get_session_factory, init_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_scopes(value: str | None) -> list[str]:
    if not value:
        return ["im:message", "im:message:send_as_bot", "im:message.group_at_msg"]
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return [s.strip() for s in value.split(",") if s.strip()]
    return list(result) if isinstance(result, list) else []


def _gen_random_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _new_uuid_str() -> str:
    """Local mirror of UserRow.id: 36-char UUID v4 string."""
    return str(UUID(int=uuid4().int, version=4))


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------


async def _ensure_user(
    user_repo: SQLiteUserRepository,
    *,
    row: dict,
    password_source: str,
    allow_update: bool,
    new_passwords: dict[str, str],
    dry_run: bool,
) -> tuple[str | None, dict]:
    """Look up (or create) the target DeerFlow user.  Returns (user_id, plan)."""
    email = (row.get("deerflow_email") or "").strip().lower()
    if not email:
        return None, {"email": "(blank)", "result": "skip", "reason": "missing email"}

    action = (row.get("action") or "upsert_all").strip().lower()
    role = (row.get("system_role") or "user").strip().lower() or "user"
    if role not in {"user", "admin"}:
        return None, {"email": email, "result": "skip", "reason": f"invalid system_role={role!r}"}

    plain_password = (row.get("password") or "").strip()
    oauth_provider = (row.get("oauth_provider") or "").strip() or None
    oauth_id = (row.get("oauth_id") or "").strip() or None
    needs_setup_field = (row.get("needs_setup") or "").strip()

    if password_source == "auto":
        plain_password = _gen_random_password()
        new_passwords[email] = plain_password
    elif password_source == "off":
        plain_password = ""

    password_hash: str | None = hash_password(plain_password) if plain_password else None

    if not needs_setup_field:
        needs_setup = password_hash is None
    else:
        needs_setup = _parse_bool(needs_setup_field, default=False)

    plan: dict = {
        "email": email,
        "action": action,
        "role": role,
        "needs_setup": needs_setup,
        "oauth_provider": oauth_provider,
        "oauth_id": oauth_id,
        "will_set_password": bool(plain_password),
    }

    existing = await user_repo.get_user_by_email(email)
    if existing is not None:
        if action == "create_user":
            plan["result"] = "skip-user-exists"
            return str(existing.id), plan
        # Only rewrite an existing row when a stored, byte-comparable field
        # actually differs.  ``password_hash`` is excluded from this check on
        # purpose: bcrypt salts every hash independently, so the same
        # plaintext always produces a different hash.  Driving the update
        # decision off the hash would either re-write the row on every run
        # (no idempotency) or force us to store plaintext to compare.
        # Password changes are accepted as a side-effect of changing
        # role / needs_setup / oauth / provider, which are the real
        # intent-of-edit signals in this script.
        non_password_changes = existing.system_role != role or existing.needs_setup != needs_setup or existing.oauth_provider != oauth_provider or existing.oauth_id != oauth_id
        if allow_update and non_password_changes:
            if dry_run:
                plan["result"] = "would-update"
                return str(existing.id), plan
            updated = User(
                id=existing.id,
                email=email,
                # Preserve existing hash if the row's ``password`` column was
                # empty (a "leave password alone" signal).  A non-empty
                # ``password`` column is treated as intent to rotate, and the
                # freshly-computed v2 hash is what gets stored.
                password_hash=password_hash or existing.password_hash,
                system_role=role,
                oauth_provider=oauth_provider,
                oauth_id=oauth_id,
                needs_setup=needs_setup,
                token_version=existing.token_version,
            )
            await user_repo.update_user(updated)
            plan["result"] = "updated"
        else:
            plan["result"] = "reused"
        return str(existing.id), plan

    if action == "bind_only":
        plan["result"] = "skip-no-user"
        return None, plan

    new_user = User(
        id=UUID(_new_uuid_str()),
        email=email,
        password_hash=password_hash,
        system_role=role,
        oauth_provider=oauth_provider,
        oauth_id=oauth_id,
        needs_setup=needs_setup,
    )
    if dry_run:
        plan["result"] = "would-create"
        return str(new_user.id), plan
    try:
        created = await user_repo.create_user(new_user)
    except ValueError as exc:
        plan["result"] = "create-failed"
        plan["error"] = str(exc)
        return None, plan
    plan["result"] = "created"
    return str(created.id), plan


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


async def _ensure_binding(
    conn_repo: ChannelConnectionRepository,
    *,
    user_id: str | None,
    row: dict,
    dry_run: bool,
) -> dict:
    open_id = (row.get("feishu_open_id") or "").strip()
    chat_id = (row.get("feishu_chat_id") or "").strip()
    name = (row.get("feishu_user_name") or "").strip() or None
    bot_id = (row.get("feishu_bot_open_id") or "").strip() or None
    scopes = _parse_scopes(row.get("scopes"))

    plan: dict = {
        "open_id": open_id,
        "chat_id": chat_id,
        "name": name,
        "bot_id": bot_id,
        "scopes": scopes,
    }
    if user_id is None:
        plan["result"] = "skip-no-user"
        return plan
    if not open_id or not chat_id:
        plan["result"] = "skip-missing-fields"
        return plan

    if dry_run:
        plan["result"] = "would-bind"
        return plan
    try:
        await conn_repo.upsert_connection(
            owner_user_id=user_id,
            provider="feishu",
            external_account_id=open_id,
            external_account_name=name,
            workspace_id=chat_id,
            bot_user_id=bot_id,
            scopes=scopes,
            capabilities={},
            metadata={"bound_by": "ops-bulk", "source": "bulk_bind_feishu.py"},
            status="connected",
        )
        plan["result"] = "bound"
    except Exception as exc:  # IntegrityError, etc.
        plan["result"] = "bind-failed"
        plan["error"] = repr(exc)
    return plan


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------


def _iter_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _run(
    path: Path,
    *,
    json_mode: bool,
    dry_run: bool,
    allow_update: bool,
    password_source: str,
    password_out: Path,
) -> int:
    await init_engine()
    sf = get_session_factory()
    if sf is None:
        logger.error("DB backend=memory; nothing to persist")
        return 2

    user_repo = SQLiteUserRepository(sf)
    conn_repo = ChannelConnectionRepository(sf)

    new_passwords: dict[str, str] = {}
    plans: list[dict] = []

    rows_iter = _iter_jsonl(path) if json_mode else _iter_csv(path)
    for raw in rows_iter:
        user_id, plan = await _ensure_user(
            user_repo,
            row=raw,
            password_source=password_source,
            allow_update=allow_update,
            new_passwords=new_passwords,
            dry_run=dry_run,
        )
        bind_plan = await _ensure_binding(conn_repo, user_id=user_id, row=raw, dry_run=dry_run)
        plan["binding"] = bind_plan
        plans.append(plan)

    counts: dict[str, int] = {}
    for p in plans:
        key = f"user:{p.get('result', '?')}/bind:{p['binding'].get('result', '?')}"
        counts[key] = counts.get(key, 0) + 1

    prefix = "DRY-RUN " if dry_run else ""
    logger.info("%splan summary:", prefix)
    for k, v in sorted(counts.items()):
        logger.info("  %s: %d", k, v)
    for p in plans:
        bp = p["binding"]
        logger.info(
            "  email=%s user=%s binding=%s open_id=%s chat_id=%s",
            p["email"],
            p.get("result", "?"),
            bp.get("result", "?"),
            bp.get("open_id") or "-",
            bp.get("chat_id") or "-",
        )

    if password_source == "auto" and new_passwords:
        mode = "w" if dry_run else "a"
        password_out.parent.mkdir(parents=True, exist_ok=True)
        with password_out.open(mode, encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for email, pw in sorted(new_passwords.items()):
                w.writerow([email, pw])
        logger.warning("Wrote %d random passwords to %s (treat as secret)", len(new_passwords), password_out)

    failed = sum(1 for p in plans if p["binding"].get("result") == "bind-failed")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-create DeerFlow users and bulk-bind Feishu open_ids.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--json", action="store_true", help="read JSONL instead of CSV")
    parser.add_argument("--dry-run", action="store_true", help="don't write anything")
    parser.add_argument(
        "--allow-update",
        action="store_true",
        help="update existing users (password/role/oauth) instead of skipping",
    )
    parser.add_argument(
        "--password-source",
        choices=("column", "auto", "off"),
        default="column",
        help=("where the stored password comes from: column (read CSV), auto (random + write ledger), off (always empty)"),
    )
    parser.add_argument(
        "--password-out",
        type=Path,
        default=Path("bulk_bind_passwords.csv"),
        help="ledger path when --password-source=auto (default ./bulk_bind_passwords.csv)",
    )
    args = parser.parse_args()
    if not args.csv_path.exists():
        logger.error("file not found: %s", args.csv_path)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    return asyncio.run(
        _run(
            args.csv_path,
            json_mode=args.json,
            dry_run=args.dry_run,
            allow_update=args.allow_update,
            password_source=args.password_source,
            password_out=args.password_out,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
