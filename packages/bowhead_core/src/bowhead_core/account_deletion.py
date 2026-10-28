"""Global account retirement and durable, deadline-based deletion.

Email is never a cleanup identity. Every mutation binds the original user ID;
re-registering the released address cannot join or revive the retired account.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore


class AccountDeletionError(RuntimeError):
    """Finite public error code with safe resource identifiers."""

    def __init__(self, code: str, *, team_ids: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.team_ids = team_ids


@dataclass(frozen=True)
class AccountDeletion:
    user_id: str
    operation_id: str
    requested_at: str
    purge_after: str


async def retire_account_conn(
    db: aiosqlite.Connection,
    store: UserStore,
    user_id: str,
    *,
    requested_at: datetime,
    time_source: str = "request",
) -> AccountDeletion:
    """Called inside one writer transaction, including the last-owner check."""
    from bowhead_core.user_store import _id

    async with db.execute(
        "SELECT user_id,operation_id,requested_at,purge_after FROM account_deletions WHERE user_id=?",
        (user_id,),
    ) as cursor:
        existing = await cursor.fetchone()
    if existing is not None:
        return AccountDeletion(*existing)
    async with db.execute("SELECT status,email,created_at FROM users WHERE id=?", (user_id,)) as cursor:
        user = await cursor.fetchone()
    if user is None:
        raise AccountDeletionError("account_not_found")
    # Retiring an already-deleted historical identity must not prevent adoption.
    # New requests cannot orphan active Teams or remove their last administrator.
    if time_source == "request":
        async with db.execute(
            """SELECT DISTINCT t.id FROM teams t
               LEFT JOIN team_members m ON m.team_id=t.id AND m.user_id=? AND m.status='active'
               WHERE t.status='active' AND (
                 t.created_by_user_id=? OR (
                   m.role='team_admin' AND NOT EXISTS(
                     SELECT 1 FROM team_members other JOIN users u ON u.id=other.user_id
                     WHERE other.team_id=t.id AND other.status='active'
                       AND other.role='team_admin' AND other.user_id<>? AND u.status='active'
                   )
                 )
               )""",
            (user_id, user_id, user_id),
        ) as cursor:
            blockers = tuple(row[0] for row in await cursor.fetchall())
        if blockers:
            raise AccountDeletionError("account_transfer_required", team_ids=blockers)
        async with db.execute(
            "SELECT DISTINCT w.team_id FROM workspace_members m JOIN workspaces w ON w.id=m.workspace_id "
            "WHERE m.user_id=? AND m.status='active' AND w.status='active' "
            "AND m.role IN ('admin','ws_admin','owner','workspace_admin') AND NOT EXISTS("
            "SELECT 1 FROM workspace_members other JOIN users u ON u.id=other.user_id "
            "WHERE other.workspace_id=m.workspace_id AND other.user_id<>? AND other.status='active' "
            "AND other.role IN ('admin','ws_admin','owner','workspace_admin') AND u.status='active')",
            (user_id, user_id),
        ) as cursor:
            blockers = tuple(row[0] for row in await cursor.fetchall())
        if blockers:
            raise AccountDeletionError("account_space_transfer_required", team_ids=blockers)
        from bowhead_core.team_subagent_store import TeamSubagentStore

        async with db.execute("SELECT DISTINCT team_id FROM team_members WHERE user_id=? AND status='active'", (user_id,)) as cursor:
            team_ids = [row[0] for row in await cursor.fetchall()]
        for team_id in team_ids:
            original_factory = db.row_factory
            db.row_factory = aiosqlite.Row
            try:
                result = await TeamSubagentStore(store.db_path).prepare_member_deactivation_conn(
                    db,
                    team_id=team_id,
                    owner_user_id=user_id,
                    actor_user_id=user_id,
                )
            finally:
                db.row_factory = original_factory
            if result.enabled_shared:
                raise AccountDeletionError("account_resource_transfer_required", team_ids=(team_id,))
    requested_at = requested_at.astimezone(UTC)
    result = AccountDeletion(user_id, _id("adel"), requested_at.isoformat(), (requested_at + timedelta(days=30)).isoformat())
    # Revoke credentials and free the unique address before publishing the job.
    await db.execute("UPDATE users SET status='deleted' WHERE id=?", (user_id,))
    await store._tombstone_identity_conn(db, user_id=user_id, now=result.requested_at)
    await db.execute(
        "UPDATE users SET verify_token_jti=NULL,pwd_version=pwd_version+1,session_token_version=session_token_version+1 WHERE id=?",
        (user_id,),
    )
    for table, column in (
        ("feishu_user_bindings", "bowhead_user_id"),
        ("feishu_workspace_bindings", "bowhead_user_id"),
        ("feishu_binding_sessions", "bound_user_id"),
        ("teams_identity_bindings", "bowhead_user_id"),
        ("slack_identity_bindings", "bowhead_user_id"),
        ("builtin_tool_oauth_state", "user_id"),
        ("personal_slack_oauth_states", "owner_user_id"),
        ("mcp_oauth_pending", "created_by"),
    ):
        await db.execute(f"DELETE FROM {table} WHERE {column}=?", (user_id,))
    await store._bump_user_account_epoch_conn(db, user_id, now=result.requested_at)
    from bowhead_core.session_store import SessionStore

    await SessionStore._invalidate_all_chat_auth_session_generations_conn(db, user_id=user_id)
    await db.execute(
        "UPDATE access_requests SET status='stale',updated_at=? WHERE requester_id=? AND status='pending'", (result.requested_at, user_id)
    )
    await db.execute("UPDATE api_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (result.requested_at, user_id))
    await db.execute(
        "UPDATE team_members SET status='removed',removed_at=?,removed_by=? WHERE user_id=? AND status='active'",
        (result.requested_at, user_id, user_id),
    )
    await db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=? AND status='active'", (user_id,))
    await db.execute("UPDATE scheduled_jobs SET status='paused',updated_at=? WHERE user_id=?", (result.requested_at, user_id))
    await db.execute(
        "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source) VALUES (?,?,?,?,?)",
        (result.user_id, result.operation_id, result.requested_at, result.purge_after, time_source),
    )
    # Freeze email-addressed records now, before a different identity can reuse
    # the address. Later cleanup must never select by email.
    if not user[1].endswith("@deleted.invalid"):
        for table, column in (("smtp_outbox", "to_email"), ("invite_tokens", "invited_email"), ("team_invite_tokens", "invited_email")):
            await db.execute(
                f"INSERT OR IGNORE INTO account_deletion_rows SELECT ?,?,json_array(id) FROM {table} WHERE {column}=? "
                + (
                    "AND julianday(created_at)>=julianday(?)"
                    if table == "smtp_outbox"
                    else "AND (consumed_by_user_id IS NULL OR consumed_by_user_id=?)"
                ),
                (result.operation_id, table, user[1], user[2] if table == "smtp_outbox" else user_id),
            )
        await db.execute(
            "UPDATE smtp_outbox SET status='cancelled',last_error='account_deleted' WHERE to_email=? AND julianday(created_at)>=julianday(?) AND status IN ('pending','failed_retry')",
            (user[1], user[2]),
        )
        for table in ("invite_tokens", "team_invite_tokens"):
            await db.execute(
                f"UPDATE {table} SET status='revoked',revoked_at=? WHERE invited_email=? AND status='active' AND (consumed_by_user_id IS NULL OR consumed_by_user_id=?)",
                (result.requested_at, user[1], user_id),
            )
    await db.execute(
        "UPDATE resource_grants SET revoked_at=? WHERE principal_type='user' AND principal_id=? AND revoked_at IS NULL",
        (result.requested_at, user_id),
    )
    return result


async def request_account_deletion(
    store: UserStore,
    user_id: str,
    *,
    now: datetime | None = None,
    record_intent: Callable[[AccountDeletion], Awaitable[None]] | None = None,
    expected_pwd_version: int | None = None,
) -> AccountDeletion:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if expected_pwd_version is not None:
                async with db.execute("SELECT pwd_version,status FROM users WHERE id=?", (user_id,)) as cursor:
                    current = await cursor.fetchone()
                if current is None or current != (expected_pwd_version, "active"):
                    raise AccountDeletionError("account_credentials_changed")
            result = await retire_account_conn(db, store, user_id, requested_at=now or datetime.now(UTC))
            await db.execute(
                "INSERT OR IGNORE INTO user_audit_log(id,user_id,team_id,event_type,payload_json,created_at) "
                "SELECT ?,id,team_id,'user_delete_request','{}',? FROM users WHERE id=?",
                (result.operation_id, result.requested_at, user_id),
            )
            if record_intent is not None:
                await record_intent(result)
            await db.commit()
            return result
        except BaseException:
            await db.rollback()
            raise


async def adopt_deleted_accounts(store: UserStore, *, now: datetime | None = None, limit: int = 100) -> int:
    """Recover exact audit time; missing/invalid evidence gets a fresh 30 days."""
    adoption_time = now or datetime.now(UTC)
    adopted = 0
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT id FROM users WHERE status IN ('deleted','legacy_deleted','removed') "
                "AND NOT EXISTS(SELECT 1 FROM account_deletions d WHERE d.user_id=users.id) ORDER BY id LIMIT ?",
                (limit,),
            ) as cursor:
                ids = [row[0] for row in await cursor.fetchall()]
            for user_id in ids:
                async with db.execute(
                    "SELECT MAX(julianday(created_at)) FROM user_audit_log WHERE user_id=? AND event_type='user_delete_request'",
                    (user_id,),
                ) as cursor:
                    audit = await cursor.fetchone()
                requested_at = adoption_time
                source = "adoption"
                if audit and audit[0] is not None:
                    # SQLite Julian days also accept legacy space-separated UTC timestamps.
                    try:
                        observed = datetime.fromtimestamp((float(audit[0]) - 2440587.5) * 86400, tz=UTC)
                    except (OverflowError, OSError, ValueError):
                        # SQLite accepts year zero and negative years; Python
                        # cannot represent them as valid account timestamps.
                        observed = None
                    if observed is not None and observed <= adoption_time:
                        requested_at, source = observed, "audit"
                deletion = await retire_account_conn(db, store, user_id, requested_at=requested_at, time_source=source)
                if store.account_deletion_recorder is not None:
                    await store.account_deletion_recorder(deletion)
                adopted += 1
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return adopted
