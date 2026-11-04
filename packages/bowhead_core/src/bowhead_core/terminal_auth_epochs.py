"""Transaction-local Terminal Protocol authorization epoch mutations.

The caller owns the SQLite transaction.  These helpers intentionally perform
no schema discovery and no commit: once the production fence is enabled, a
missing migration aborts the authorization mutation instead of silently
leaving an already-open stream authorized against stale state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bowhead_core.sqlite_domains import domain_layout_active
from bowhead_core.terminal_protocol import AuthFenceScopeKind


async def terminal_auth_epoch_table(connection: aiosqlite.Connection, scope_kind: AuthFenceScopeKind) -> str:
    """Resolve the fixed epoch authority without opening another connection."""
    if scope_kind is AuthFenceScopeKind.SESSION_VISIBILITY:
        return "terminal_auth_scope_epochs"
    async with connection.execute("PRAGMA database_list") as cursor:
        databases = await cursor.fetchall()
    path = next((Path(row[2]) for row in databases if row[1] == "main" and row[2]), None)
    if path is not None and domain_layout_active(path):
        return "terminal_authority_scope_epochs"
    return "terminal_auth_scope_epochs"


async def bump_terminal_auth_epoch(
    connection: aiosqlite.Connection,
    *,
    scope_kind: AuthFenceScopeKind,
    team_id: str,
    scope_ref: str,
    now: str | None = None,
) -> None:
    """Insert or monotonically bump one coarse authorization scope."""

    if not isinstance(scope_kind, AuthFenceScopeKind):
        raise TypeError("terminal auth epoch scope kind is invalid")
    if not team_id or not scope_ref:
        raise ValueError("terminal auth epoch identity is invalid")
    if scope_kind is AuthFenceScopeKind.USER_ACCOUNT:
        if team_id != "__global__":
            raise ValueError("user account auth epoch must use global team scope")
    elif team_id == "__global__":
        raise ValueError("team auth epoch cannot use global team scope")
    updated_at = now or datetime.now(UTC).isoformat()
    table = await terminal_auth_epoch_table(connection, scope_kind)
    await connection.execute(
        f"""
        INSERT INTO {table}(
          scope_kind,team_id,scope_ref,epoch,row_version,updated_at
        ) VALUES (?,?,?,1,1,?)
        ON CONFLICT(scope_kind,team_id,scope_ref) DO UPDATE SET
          epoch={table}.epoch+1,
          row_version={table}.row_version+1,
          updated_at=excluded.updated_at
        """,  # noqa: S608 -- fixed epoch authority table selected above
        (scope_kind.value, team_id, scope_ref, updated_at),
    )


__all__ = ["bump_terminal_auth_epoch"]
