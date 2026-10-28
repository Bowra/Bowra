"""Current request scope shared by reference discovery and source preparation."""

from __future__ import annotations

import aiosqlite
from pydantic import BaseModel, ConfigDict

from bowhead_core.access_control import AccessContext, AccessControlStore, _require_read_transaction, normalize_workspace_role
from bowhead_core.chat_references import Identity


class ReferenceRequestScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    team_id: Identity
    user_id: Identity
    workspace_id: Identity
    session_id: Identity | None = None


async def read_reference_access_context_in_connection(
    db: aiosqlite.Connection, *, scope: ReferenceRequestScope, access_store: AccessControlStore
) -> AccessContext:
    """Project current reference principals on the caller's existing transaction.

    Reuses the canonical active reference scope and team-role reader; no current
    team/workspace fallback or caller-supplied role/group metadata is accepted.
    The connection is authoritative and its lifecycle belongs to the caller.
    """
    from bowhead_core.user_store import _read_team_role_in_tx

    _require_read_transaction(db)
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python"))
    epoch = await require_reference_request_scope(db, scope)
    previous_factory = db.row_factory
    try:
        db.row_factory = aiosqlite.Row
        team_role = await _read_team_role_in_tx(db, user_id=scope.user_id, team_id=scope.team_id, require_active_principal=True)
    finally:
        db.row_factory = previous_factory
    async with db.execute(
        "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=? AND status='active'", (scope.workspace_id, scope.user_id)
    ) as cursor:
        cursor.row_factory = None
        row = await cursor.fetchone()
    workspace_role = normalize_workspace_role(row[0] if row else None)
    if not workspace_role or not team_role:
        raise ValueError("reference_scope_unavailable")
    group_ids = await access_store.principal_group_ids(scope.team_id, scope.user_id, scope.workspace_id, connection=db)
    return AccessContext(
        user_id=scope.user_id,
        team_id=scope.team_id,
        workspace_id=scope.workspace_id,
        workspace_role=workspace_role,
        team_role=team_role,
        group_ids=group_ids,
        policy_epoch=epoch,
        is_team_admin=team_role in {"owner", "team_admin"},
    )


async def require_reference_request_scope(db: aiosqlite.Connection, scope: ReferenceRequestScope, *, allow_archived: bool = False) -> int:
    """Require current membership and an owned session; archive access is read-only."""
    async with db.execute(
        """
        SELECT COALESCE(p.epoch,0) AS policy_epoch
        FROM workspaces w JOIN users u ON u.id=? AND u.status='active'
        JOIN teams t ON t.id=w.team_id AND t.status='active'
        JOIN team_members tm ON tm.team_id=t.id AND tm.user_id=u.id AND tm.status='active'
        JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=u.id AND wm.status='active'
        LEFT JOIN access_policy_state p ON p.team_id=t.id
        WHERE w.id=? AND w.team_id=? AND (w.status='active' OR (? AND w.status='archived'))
        """,
        (scope.user_id, scope.workspace_id, scope.team_id, allow_archived),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        row = await cursor.fetchone()
    if row is None:
        raise ValueError("reference_scope_unavailable")
    if scope.session_id is not None:
        async with db.execute(
            "SELECT 1 FROM sessions WHERE id=? AND user_id=? AND workspace_id=? AND deleted_at IS NULL",
            (scope.session_id, scope.user_id, scope.workspace_id),
        ) as cursor:
            if await cursor.fetchone() is None:
                raise ValueError("reference_scope_unavailable")
    return int(row["policy_epoch"])
