"""Workspace/space access-control primitives.

This module intentionally stays below the gateway layer: it owns SQLite-backed
groups/resource grants plus pure decision helpers, while HTTP auth, request
parsing, and FastAPI exceptions live in ``bowhead_gateway``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core.subagent_write_boundary import (
    TEAM_SUBAGENT_RESOURCE_KIND,
    SubagentTransactionToken,
    require_team_subagent_transaction_token,
)

_RESOURCE_GRANTS_ENFORCE_ENV = "BOWHEAD_RESOURCE_GRANTS_ENFORCE"

_WORKSPACE_ROLE_ALIASES: dict[str, str] = {
    "admin": "ws_admin",
    "owner": "ws_admin",
    "workspace_admin": "ws_admin",
    "ws_admin": "ws_admin",
    "member": "member",
    "user": "member",
    "viewer": "viewer",
    "read_only": "viewer",
    "readonly": "viewer",
}
_WORKSPACE_ROLE_RANK: dict[str, int] = {
    "viewer": 1,
    "member": 2,
    "ws_admin": 3,
}

_VALID_PRINCIPAL_TYPES = {"user", "group", "workspace", "team"}
_VALID_EFFECTS = {"allow", "deny"}
_VALID_PERMISSIONS = {"read", "use", "invoke", "write", "admin"}

# Content sharing (docs/sprints/content-permissions/tech-design.md §2.2/§2.3)
_AUDIENCE_TARGETS = {"private", "custom", "team_wide"}
# Whitelist of tables carrying a write-through ``visibility`` column. The table
# name is interpolated into SQL — it MUST come from this set, never from input.
_VISIBILITY_TABLES = {"team_subagent", "team_skill"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _require_read_transaction(connection: aiosqlite.Connection | None) -> None:
    if connection is not None:
        try:
            active = connection.in_transaction
        except ValueError:
            active = False
        if not active:
            raise ValueError("access_read_transaction_required")


def resource_grants_enforce_enabled() -> bool:
    """True iff fine-grained resource grants should hard-block.

    Production default is shadow mode: decisions can be explained and logged, but
    legacy access remains unchanged until this env var is enabled.
    """
    return os.environ.get(_RESOURCE_GRANTS_ENFORCE_ENV, "0").strip() == "1"


def normalize_workspace_role(role: str | None) -> str | None:
    if not role:
        return None
    return _WORKSPACE_ROLE_ALIASES.get(str(role).strip().lower())


def workspace_role_rank(role: str | None) -> int:
    return _WORKSPACE_ROLE_RANK.get(normalize_workspace_role(role) or "", 0)


def workspace_role_satisfies(role: str | None, required: str | None) -> bool:
    if not required:
        return True
    return workspace_role_rank(role) >= workspace_role_rank(required)


def permission_satisfies(granted: str, requested: str) -> bool:
    """Return whether a grant permission covers a requested action."""
    g = (granted or "").strip().lower()
    r = (requested or "").strip().lower()
    if g == "admin":
        return True
    if r == "read":
        return g in {"read", "write"}
    if r == "use":
        return g in {"use", "write"}
    if r == "invoke":
        return g in {"invoke", "write"}
    if r == "write":
        return g == "write"
    return g == r


def _resource_permission_satisfies(
    resource_kind: str,
    *,
    granted: str,
    requested: str,
    effect: str,
) -> bool:
    """Apply the conservative legacy bridge for operation-specific Skill grants.

    Existing Skill ``use`` grants predate the read/invoke distinction.  Treat an
    old allow as read-only, never as permission to execute package code.  An old
    deny remains a narrowing rule for both operations so introducing a new
    permission name cannot revive a resource an administrator explicitly denied.
    Other resource kinds keep the original permission lattice unchanged.
    """

    if resource_kind in {"skill", "team_skill"} and granted == "use":
        if requested == "read":
            return True
        if requested == "invoke" and effect == "deny":
            return True
    return permission_satisfies(granted, requested)


@dataclass(frozen=True)
class AccessContext:
    user_id: str
    team_id: str
    workspace_id: str
    workspace_role: str
    team_role: str | None = None
    group_ids: tuple[str, ...] = field(default_factory=tuple)
    policy_epoch: int = 0
    is_team_admin: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["group_ids"] = list(self.group_ids)
        return data


@dataclass(frozen=True)
class GroupRecord:
    id: str
    team_id: str
    workspace_id: str | None
    name: str
    description: str = ""
    status: str = "active"
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    member_count: int = 0


@dataclass(frozen=True)
class GroupMemberRecord:
    group_id: str
    team_id: str
    principal_type: str
    principal_id: str
    role: str
    status: str
    created_by: str | None
    created_at: str
    removed_at: str | None = None
    email: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class ResourceGrantRecord:
    id: str
    team_id: str
    resource_kind: str
    resource_id: str
    principal_type: str
    principal_id: str
    permission: str
    effect: str = "allow"
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    expires_at: str | None = None
    revoked_at: str | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  id             TEXT PRIMARY KEY,
  team_id         TEXT NOT NULL,
  workspace_id   TEXT NOT NULL DEFAULT '',
  name           TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'active',
  created_by     TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(team_id, workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_groups_team_ws ON groups(team_id, workspace_id, status);

CREATE TABLE IF NOT EXISTS group_memberships (
  team_id          TEXT NOT NULL,
  group_id        TEXT NOT NULL,
  principal_type  TEXT NOT NULL DEFAULT 'user',
  principal_id    TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'member',
  status          TEXT NOT NULL DEFAULT 'active',
  created_by      TEXT,
  created_at      TEXT NOT NULL,
  removed_at      TEXT,
  PRIMARY KEY(group_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_group_memberships_principal
  ON group_memberships(team_id, principal_type, principal_id, status);
CREATE INDEX IF NOT EXISTS idx_group_memberships_group
  ON group_memberships(group_id, status);

CREATE TABLE IF NOT EXISTS resource_grants (
  id              TEXT PRIMARY KEY,
  team_id          TEXT NOT NULL,
  resource_kind   TEXT NOT NULL,
  resource_id     TEXT NOT NULL,
  principal_type  TEXT NOT NULL,
  principal_id    TEXT NOT NULL,
  permission      TEXT NOT NULL,
  effect          TEXT NOT NULL DEFAULT 'allow',
  created_by      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  expires_at      TEXT,
  revoked_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_grants_active_unique
  ON resource_grants(
    team_id, resource_kind, resource_id, principal_type, principal_id, permission, effect
  )
  WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_resource_grants_lookup
  ON resource_grants(team_id, resource_kind, resource_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_resource_grants_principal
  ON resource_grants(team_id, principal_type, principal_id, revoked_at);

CREATE TABLE IF NOT EXISTS access_policy_state (
  team_id      TEXT PRIMARY KEY,
  epoch       INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL
);
"""


def _row_to_group(row: Any) -> GroupRecord:
    return GroupRecord(
        id=row["id"],
        team_id=row["team_id"],
        workspace_id=row["workspace_id"] or None,
        name=row["name"],
        description=row["description"] or "",
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        member_count=int(row["member_count"] or 0) if "member_count" in row else 0,
    )


def _row_to_member(row: Any) -> GroupMemberRecord:
    return GroupMemberRecord(
        group_id=row["group_id"],
        team_id=row["team_id"],
        principal_type=row["principal_type"],
        principal_id=row["principal_id"],
        role=row["role"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        removed_at=row["removed_at"],
        # sqlite3.Row has no .get, and `x in row` searches **values**, not keys; `in row.keys()`
        # is the only correct form. Do not "simplify" under SIM118 (06ba6e19 did so, causing runtime AttributeError).
        email=row["email"] if "email" in row.keys() else None,  # noqa: SIM118
        display_name=row["display_name"] if "display_name" in row.keys() else None,  # noqa: SIM118
    )


def _row_to_grant(row: Any) -> ResourceGrantRecord:
    return ResourceGrantRecord(
        id=row["id"],
        team_id=row["team_id"],
        resource_kind=row["resource_kind"],
        resource_id=row["resource_id"],
        principal_type=row["principal_type"],
        principal_id=row["principal_id"],
        permission=row["permission"],
        effect=row["effect"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def _ctx_attr(ctx: AccessContext | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


# List-request denial summaries keep at most this many resource ids.
DENIAL_SUMMARY_SAMPLE_SIZE = 20

# Keep IN (...) lists well below SQLite's bound-parameter limit.
_BATCH_ID_CHUNK = 400


def _decision_from_matches(matches: list[ResourceGrantRecord]) -> dict[str, Any] | None:
    """Decision when a matching grant exists (deny wins over allow), else None."""
    deny = next((g for g in matches if g.effect == "deny"), None)
    if deny:
        return {
            "allowed": False,
            "reason": "denied_by_grant",
            "grant_id": deny.id,
            "shadow": not resource_grants_enforce_enabled(),
        }
    allow = next((g for g in matches if g.effect == "allow"), None)
    if allow:
        return {
            "allowed": True,
            "reason": "allowed_by_grant",
            "grant_id": allow.id,
            "shadow": False,
        }
    return None


def _decision_without_matches(*, has_allowlist: bool) -> dict[str, Any]:
    if has_allowlist:
        return {
            "allowed": False,
            "reason": "not_in_allowlist",
            "grant_id": None,
            "shadow": not resource_grants_enforce_enabled(),
        }
    return {
        "allowed": True,
        "reason": "allowed_default_no_allowlist",
        "grant_id": None,
        "shadow": False,
    }


class AccessControlStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        terminal_auth_fence_enabled: bool = False,
    ) -> None:
        self.db_path = str(db_path)
        self._terminal_auth_fence_enabled = terminal_auth_fence_enabled

    @asynccontextmanager
    async def _read_connection(self, connection: aiosqlite.Connection | None) -> AsyncIterator[aiosqlite.Connection]:
        """Use a caller's transaction without owning its lifetime or row factory.

        An explicit connection is authoritative; never reopen the configured DB
        or silently fall back when it is closed/not in a transaction.

        Without one, borrow a pooled read-only connection: grant checks run for
        every capability on every request, and a fresh connection per check paid a
        full schema parse (two per ``explain_decision``) for a sub-millisecond read.
        """
        if connection is not None:
            _require_read_transaction(connection)
            yield connection
        else:
            async with open_pooled_read_ctx(self.db_path) as db:
                yield db

    def set_terminal_auth_fence_enabled(self, enabled: bool) -> None:
        """Enable only after Terminal Protocol schema/readiness validation."""

        self._terminal_auth_fence_enabled = bool(enabled)

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def get_policy_epoch(self, team_id: str, *, connection: aiosqlite.Connection | None = None) -> int:
        async with (
            self._read_connection(connection) as db,
            db.execute(
                "SELECT epoch FROM access_policy_state WHERE team_id=?",
                (team_id,),
            ) as cur,
        ):
            cur.row_factory = None
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def bump_policy_epoch(self, team_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await self._bump_policy_epoch_conn(db, team_id)
            await db.commit()
        return await self.get_policy_epoch(team_id)

    async def _bump_policy_epoch_conn(self, db: aiosqlite.Connection, team_id: str) -> None:
        now = _now()
        await db.execute(
            "INSERT INTO access_policy_state(team_id, epoch, updated_at) VALUES (?, 1, ?) "
            "ON CONFLICT(team_id) DO UPDATE SET epoch=epoch+1, updated_at=excluded.updated_at",
            (team_id, now),
        )
        if self._terminal_auth_fence_enabled:
            from bowhead_core.terminal_auth_epochs import bump_terminal_auth_epoch
            from bowhead_core.terminal_protocol import AuthFenceScopeKind

            await bump_terminal_auth_epoch(
                db,
                scope_kind=AuthFenceScopeKind.RESOURCE_GRANT,
                team_id=team_id,
                scope_ref=team_id,
                now=now,
            )

    async def _bump_membership_epoch_conn(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        *,
        now: str,
    ) -> None:
        if not self._terminal_auth_fence_enabled:
            return
        from bowhead_core.terminal_auth_epochs import bump_terminal_auth_epoch
        from bowhead_core.terminal_protocol import AuthFenceScopeKind

        await bump_terminal_auth_epoch(
            db,
            scope_kind=AuthFenceScopeKind.TEAM_MEMBERSHIP,
            team_id=team_id,
            scope_ref=team_id,
            now=now,
        )

    async def create_group(
        self,
        *,
        team_id: str,
        name: str,
        workspace_id: str | None = None,
        description: str = "",
        created_by: str | None = None,
    ) -> GroupRecord:
        now = _now()
        group = GroupRecord(
            id=_id("grp"),
            team_id=team_id,
            workspace_id=workspace_id or None,
            name=name.strip(),
            description=description.strip(),
            status="active",
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        if not group.name:
            raise ValueError("group name required")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if group.workspace_id:
                await self._assert_workspace_in_team_conn(db, group.workspace_id, team_id)
            await db.execute(
                "INSERT INTO groups(id,team_id,workspace_id,name,description,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    group.id,
                    group.team_id,
                    group.workspace_id or "",
                    group.name,
                    group.description,
                    group.status,
                    group.created_by,
                    group.created_at,
                    group.updated_at,
                ),
            )
            await self._bump_policy_epoch_conn(db, team_id)
            await db.commit()
        return group

    async def list_groups(
        self,
        team_id: str,
        *,
        workspace_id: str | None = None,
    ) -> list[GroupRecord]:
        where = ["g.team_id=?"]
        params: list[Any] = [team_id]
        if workspace_id is not None:
            where.append("g.workspace_id=?")
            params.append(workspace_id or "")
        sql = (
            "SELECT g.*, COUNT(CASE WHEN gm.status='active' THEN 1 END) AS member_count "
            "FROM groups g "
            "LEFT JOIN group_memberships gm ON gm.group_id=g.id "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY g.id "
            "ORDER BY g.workspace_id ASC, g.name ASC"
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_group(r) for r in rows]

    async def get_group(self, team_id: str, group_id: str) -> GroupRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT g.*, COUNT(CASE WHEN gm.status='active' THEN 1 END) AS member_count "
                "FROM groups g LEFT JOIN group_memberships gm ON gm.group_id=g.id "
                "WHERE g.team_id=? AND g.id=? GROUP BY g.id",
                (team_id, group_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_group(row) if row else None

    async def add_group_member(
        self,
        *,
        team_id: str,
        group_id: str,
        user_id: str,
        role: str = "member",
        created_by: str | None = None,
    ) -> GroupMemberRecord:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            group = await self._get_group_conn(db, team_id, group_id)
            if group is None:
                await db.rollback()
                raise KeyError("group_not_found")
            await self._assert_user_in_team_conn(db, user_id, team_id)
            if group.workspace_id:
                await self._assert_workspace_member_conn(db, group.workspace_id, user_id)
            await db.execute(
                "INSERT INTO group_memberships(team_id,group_id,principal_type,principal_id,"
                "role,status,created_by,created_at,removed_at) VALUES (?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(group_id, principal_type, principal_id) DO UPDATE SET "
                "role=excluded.role,status='active',created_by=excluded.created_by,"
                "created_at=excluded.created_at,removed_at=NULL",
                (team_id, group_id, "user", user_id, role, "active", created_by, now),
            )
            await self._bump_policy_epoch_conn(db, team_id)
            await self._bump_membership_epoch_conn(db, team_id, now=now)
            await db.commit()
        member = await self.get_group_member(team_id, group_id, user_id)
        assert member is not None
        return member

    async def remove_group_member(self, team_id: str, group_id: str, user_id: str) -> bool:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE group_memberships SET status='removed', removed_at=? "
                "WHERE team_id=? AND group_id=? AND principal_type='user' "
                "AND principal_id=? AND status='active'",
                (now, team_id, group_id, user_id),
            )
            changed = cur.rowcount > 0
            if changed:
                await self._bump_policy_epoch_conn(db, team_id)
                await self._bump_membership_epoch_conn(db, team_id, now=now)
            await db.commit()
        return changed

    async def list_group_members(self, team_id: str, group_id: str) -> list[GroupMemberRecord]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT gm.*, u.email, u.display_name "
                "FROM group_memberships gm "
                "LEFT JOIN users u ON u.id=gm.principal_id AND gm.principal_type='user' "
                "WHERE gm.team_id=? AND gm.group_id=? AND gm.status='active' "
                "ORDER BY u.email ASC, gm.principal_id ASC",
                (team_id, group_id),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_member(r) for r in rows]

    async def get_group_member(
        self,
        team_id: str,
        group_id: str,
        user_id: str,
    ) -> GroupMemberRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT gm.*, u.email, u.display_name "
                "FROM group_memberships gm "
                "LEFT JOIN users u ON u.id=gm.principal_id AND gm.principal_type='user' "
                "WHERE gm.team_id=? AND gm.group_id=? AND gm.principal_type='user' "
                "AND gm.principal_id=? AND gm.status='active'",
                (team_id, group_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_member(row) if row else None

    async def principal_group_scopes(self, team_id: str, user_id: str) -> tuple[tuple[str, str | None], ...]:
        """`principal_group_ids` 的批量底料：该用户在团队里全部有效组 → ``((group_id, 组所属空间), ...)``，按 group_id 升序。

        单条版对空间 ``ws`` 的结果＝其中「组所属空间为 "" 或等于 ws」的 group_id（顺序不变）。同一个 JOIN 与状态条件。
        组所属空间原样返回（老库里可能是 NULL，单条版的条件不匹配它，调用方也不该匹配）。
        """
        sql = (
            "SELECT gm.group_id, g.workspace_id FROM group_memberships gm "
            "JOIN groups g ON g.id=gm.group_id AND g.team_id=gm.team_id "
            "WHERE gm.team_id=? AND gm.principal_type='user' AND gm.principal_id=? "
            "AND gm.status='active' AND g.status='active' "
            "ORDER BY gm.group_id ASC"
        )
        async with self._read_connection(None) as db, db.execute(sql, (team_id, user_id)) as cur:
            cur.row_factory = None
            rows = await cur.fetchall()
        return tuple((str(group_id), None if group_workspace is None else str(group_workspace)) for group_id, group_workspace in rows)

    async def principal_group_ids(
        self,
        team_id: str,
        user_id: str,
        workspace_id: str | None = None,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> tuple[str, ...]:
        sql = (
            "SELECT gm.group_id FROM group_memberships gm "
            "JOIN groups g ON g.id=gm.group_id AND g.team_id=gm.team_id "
            "WHERE gm.team_id=? AND gm.principal_type='user' AND gm.principal_id=? "
            "AND gm.status='active' AND g.status='active' "
            "AND (g.workspace_id='' OR g.workspace_id=?) "
            "ORDER BY gm.group_id ASC"
        )
        async with self._read_connection(connection) as db, db.execute(sql, (team_id, user_id, workspace_id or "")) as cur:
            cur.row_factory = None
            rows = await cur.fetchall()
        return tuple(str(r[0]) for r in rows)

    async def create_or_update_resource_grant(
        self,
        *,
        team_id: str,
        resource_kind: str,
        resource_id: str,
        principal_type: str,
        principal_id: str,
        permission: str,
        effect: str = "allow",
        created_by: str | None = None,
        expires_at: str | None = None,
        subagent_transaction_token: SubagentTransactionToken | None = None,
    ) -> ResourceGrantRecord:
        resource_kind = resource_kind.strip()
        resource_id = resource_id.strip()
        principal_type = principal_type.strip()
        principal_id = principal_id.strip()
        permission = permission.strip().lower()
        effect = effect.strip().lower()
        if not resource_kind or not resource_id or not principal_id:
            raise ValueError("resource_kind, resource_id, and principal_id are required")
        if principal_type not in _VALID_PRINCIPAL_TYPES:
            raise ValueError(f"invalid principal_type: {principal_type}")
        if permission not in _VALID_PERMISSIONS:
            raise ValueError(f"invalid permission: {permission}")
        if permission == "invoke" and resource_kind not in {"skill", "team_skill"}:
            raise ValueError("invoke permission is only valid for Skill resources")
        if effect not in _VALID_EFFECTS:
            raise ValueError(f"invalid effect: {effect}")
        require_team_subagent_transaction_token(
            resource_kind,
            subagent_transaction_token,
        )

        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM resource_grants WHERE team_id=? AND resource_kind=? "
                "AND resource_id=? AND principal_type=? AND principal_id=? "
                "AND permission=? AND effect=? AND revoked_at IS NULL",
                (
                    team_id,
                    resource_kind,
                    resource_id,
                    principal_type,
                    principal_id,
                    permission,
                    effect,
                ),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                grant_id = existing["id"]
                await db.execute(
                    "UPDATE resource_grants SET expires_at=?, updated_at=?, created_by=? WHERE id=? AND team_id=?",
                    (expires_at, now, created_by, grant_id, team_id),
                )
            else:
                grant_id = _id("grant")
                await db.execute(
                    "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,"
                    "principal_type,principal_id,permission,effect,created_by,created_at,"
                    "updated_at,expires_at,revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        grant_id,
                        team_id,
                        resource_kind,
                        resource_id,
                        principal_type,
                        principal_id,
                        permission,
                        effect,
                        created_by,
                        now,
                        now,
                        expires_at,
                    ),
                )
            await self._bump_policy_epoch_conn(db, team_id)
            await db.commit()
        grant = await self.get_resource_grant(team_id, grant_id)
        assert grant is not None
        return grant

    async def get_resource_grant(
        self,
        team_id: str,
        grant_id: str,
    ) -> ResourceGrantRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM resource_grants WHERE team_id=? AND id=?",
                (team_id, grant_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_grant(row) if row else None

    async def list_resource_grants(
        self,
        team_id: str,
        *,
        resource_kind: str | None = None,
        resource_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ResourceGrantRecord]:
        where = ["team_id=?"]
        params: list[Any] = [team_id]
        if resource_kind:
            where.append("resource_kind=?")
            params.append(resource_kind)
        if resource_id:
            where.append("resource_id=?")
            params.append(resource_id)
        if not include_revoked:
            where.append("revoked_at IS NULL")
        sql = f"SELECT * FROM resource_grants WHERE {' AND '.join(where)} ORDER BY updated_at DESC, created_at DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_grant(r) for r in rows]

    async def revoke_resource_grant(
        self,
        team_id: str,
        grant_id: str,
        *,
        subagent_transaction_token: SubagentTransactionToken | None = None,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """
                SELECT resource_kind
                FROM resource_grants
                WHERE team_id=? AND id=? AND revoked_at IS NULL
                """,
                (team_id, grant_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                require_team_subagent_transaction_token(
                    str(existing["resource_kind"]),
                    subagent_transaction_token,
                )
            cur = await db.execute(
                "UPDATE resource_grants SET revoked_at=?, updated_at=? WHERE team_id=? AND id=? AND revoked_at IS NULL",
                (_now(), _now(), team_id, grant_id),
            )
            changed = cur.rowcount > 0
            if changed:
                await self._bump_policy_epoch_conn(db, team_id)
            await db.commit()
        return changed

    # ── Content sharing primitives (docs/sprints/content-permissions/tech-design.md §2.2) ──

    async def granted_resource_ids(
        self,
        team_id: str,
        *,
        user_id: str,
        group_ids: tuple[str, ...] | list[str] = (),
        workspace_id: str | None = None,
        kind: str,
        connection: aiosqlite.Connection | None = None,
    ) -> frozenset[str]:
        """Resource ids of ``kind`` reachable by this principal chain via grants.

        Active (un-revoked, un-expired) ``allow`` rows matched by the user / its
        groups / its workspace (when ``workspace_id`` is supplied) / the team
        principal, MINUS ids hit by a matching ``deny`` row
        (deny-wins, mirroring ``explain_decision``). One SQL per call — share
        checkpoints prefetch this once per request/turn and test membership
        in-memory afterwards.
        """
        _require_read_transaction(connection)
        if not team_id or not user_id or not kind:
            return frozenset()
        principals: list[tuple[str, str]] = [("user", user_id)]
        principals.extend(("group", str(g)) for g in group_ids if g)
        # file-management-v2 P3: `workspace` has always been a valid principal_type accepted by
        # resource_grants, but was absent from the lookup chain, so persisted workspace grants could not be found
        # and silently failed. Passing workspace_id activates space-level grants; omitting it preserves
        # existing callers' behavior byte for byte (additive, with no regression surface).
        if workspace_id:
            principals.append(("workspace", str(workspace_id)))
        principals.append(("team", team_id))
        principal_clause = " OR ".join(["(principal_type=? AND principal_id=?)"] * len(principals))
        params: list[Any] = [team_id, kind, _now()]
        for ptype, pid in principals:
            params.extend([ptype, pid])
        sql = (
            "SELECT resource_id, effect FROM resource_grants "
            "WHERE team_id=? AND resource_kind=? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            f"AND ({principal_clause})"
        )
        async with self._read_connection(connection) as db, db.execute(sql, params) as cur:
            cur.row_factory = None
            rows = await cur.fetchall()
        allow: set[str] = set()
        deny: set[str] = set()
        for resource_id, effect in rows:
            (deny if effect == "deny" else allow).add(str(resource_id))
        return frozenset(allow - deny)

    async def revoke_grants_for_resource(
        self,
        team_id: str,
        resource_kind: str,
        resource_id: str,
        *,
        effects: tuple[str, ...] = ("allow",),
        principal_types: tuple[str, ...] | None = None,
        subagent_transaction_token: SubagentTransactionToken | None = None,
    ) -> list[str]:
        """Revoke all active grants on one resource (resource-deletion cascade /
        audience transitions). Defaults to allow-only so an admin ``deny``
        survives an owner going private. Returns revoked ids for per-row audit.
        """
        require_team_subagent_transaction_token(
            resource_kind,
            subagent_transaction_token,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            ids = await self._revoke_grants_for_resource_conn(
                db,
                team_id,
                resource_kind,
                resource_id,
                effects=effects,
                principal_types=principal_types,
                subagent_transaction_token=subagent_transaction_token,
            )
            if ids:
                await self._bump_policy_epoch_conn(db, team_id)
            await db.commit()
        return ids

    async def _revoke_grants_for_resource_conn(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        resource_kind: str,
        resource_id: str,
        *,
        effects: tuple[str, ...] = ("allow",),
        principal_types: tuple[str, ...] | None = None,
        subagent_transaction_token: SubagentTransactionToken | None = None,
    ) -> list[str]:
        require_team_subagent_transaction_token(
            resource_kind,
            subagent_transaction_token,
        )
        where = ["team_id=?", "resource_kind=?", "resource_id=?", "revoked_at IS NULL"]
        params: list[Any] = [team_id, resource_kind, resource_id]
        if effects:
            where.append(f"effect IN ({','.join('?' * len(effects))})")
            params.extend(effects)
        if principal_types:
            where.append(f"principal_type IN ({','.join('?' * len(principal_types))})")
            params.extend(principal_types)
        async with db.execute(
            f"SELECT id FROM resource_grants WHERE {' AND '.join(where)}",  # noqa: S608 — clauses from fixed set, values bound
            params,
        ) as cur:
            rows = await cur.fetchall()
        ids = [str(r["id"]) for r in rows]
        if ids:
            now = _now()
            await db.execute(
                f"UPDATE resource_grants SET revoked_at=?, updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
                [now, now, *ids],
            )
        return ids

    async def _replace_team_subagent_audience_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        resource_id: str,
        principals: tuple[tuple[str, str], ...],
        created_by: str,
        timestamp: str,
        subagent_transaction_token: SubagentTransactionToken | None,
    ) -> dict[str, tuple[str, ...]]:
        """Replace every Subagent allow principal on a caller-owned transaction.

        This primitive deliberately does not commit or bump the policy epoch:
        ``TeamSubagentStore`` owns those aggregate-level steps together with the
        row version, compatibility projection, and audit record. Security deny
        grants are outside this mutation and remain untouched.
        """
        require_team_subagent_transaction_token(
            TEAM_SUBAGENT_RESOURCE_KIND,
            subagent_transaction_token,
        )
        if len(set(principals)) != len(principals):
            raise ValueError("duplicate Subagent audience principal")
        if any(principal_type not in {"team", "user", "group"} or not principal_id for principal_type, principal_id in principals):
            raise ValueError("invalid Subagent audience principal")

        revoked = await self._revoke_grants_for_resource_conn(
            db,
            team_id,
            TEAM_SUBAGENT_RESOURCE_KIND,
            resource_id,
            effects=("allow",),
            principal_types=("team", "user", "group"),
            subagent_transaction_token=subagent_transaction_token,
        )
        created: list[str] = []
        for principal_type, principal_id in principals:
            grant_id = _id("grant")
            await db.execute(
                """
                INSERT INTO resource_grants(
                    id,
                    team_id,
                    resource_kind,
                    resource_id,
                    principal_type,
                    principal_id,
                    permission,
                    effect,
                    created_by,
                    created_at,
                    updated_at,
                    expires_at,
                    revoked_at
                )
                VALUES (?, ?, 'team_subagent', ?, ?, ?, 'use', 'allow', ?, ?, ?, NULL, NULL)
                """,
                (
                    grant_id,
                    team_id,
                    resource_id,
                    principal_type,
                    principal_id,
                    created_by,
                    timestamp,
                    timestamp,
                ),
            )
            created.append(grant_id)
        return {
            "revoked_grant_ids": tuple(revoked),
            "created_grant_ids": tuple(created),
        }

    async def _append_team_subagent_audience_user_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        resource_id: str,
        user_id: str,
        created_by: str,
        timestamp: str,
        subagent_transaction_token: SubagentTransactionToken | None,
    ) -> str:
        """Insert one canonical ``use`` allow for a user on a caller-owned transaction.

        Unlike ``_replace_team_subagent_audience_conn`` this leaves every existing grant row
        (ids, creators, timestamps) untouched. It does not commit or bump the policy epoch;
        ``TeamSubagentStore`` owns those steps together with the row version and audit.
        """
        require_team_subagent_transaction_token(
            TEAM_SUBAGENT_RESOURCE_KIND,
            subagent_transaction_token,
        )
        if not user_id:
            raise ValueError("invalid Subagent audience principal")
        grant_id = _id("grant")
        await db.execute(
            """
            INSERT INTO resource_grants(
                id,
                team_id,
                resource_kind,
                resource_id,
                principal_type,
                principal_id,
                permission,
                effect,
                created_by,
                created_at,
                updated_at,
                expires_at,
                revoked_at
            )
            VALUES (?, ?, 'team_subagent', ?, 'user', ?, 'use', 'allow', ?, ?, ?, NULL, NULL)
            """,
            (grant_id, team_id, resource_id, user_id, created_by, timestamp, timestamp),
        )
        return grant_id

    async def set_audience_tx(
        self,
        *,
        team_id: str,
        resource_kind: str,
        resource_id: str,
        target: str,
        permission: str,
        created_by: str | None = None,
        visibility_table: str | None = None,
        subagent_transaction_token: SubagentTransactionToken | None = None,
    ) -> dict[str, Any]:
        """Atomic audience transition (content sharing §2.3 matrix).

        ONE connection, ONE transaction: the grant revokes/upsert plus — for
        capability assets — the row's ``visibility`` write-through, so a failure
        can never leave "team grant revoked but visibility still team_wide".
        ``visibility_table`` must be in the whitelist; ``None`` for kinds
        without a visibility column (report).
        """
        target = (target or "").strip().lower()
        self._validate_audience_mutation(
            resource_kind=resource_kind,
            target=target,
            permission=permission,
            visibility_table=visibility_table,
        )
        require_team_subagent_transaction_token(
            resource_kind,
            subagent_transaction_token,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            result = await self._set_audience_conn(
                db,
                team_id=team_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                target=target,
                permission=permission,
                created_by=created_by,
                visibility_table=visibility_table,
                subagent_transaction_token=subagent_transaction_token,
            )
            await db.commit()
        return result

    async def _set_audience_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        resource_kind: str,
        resource_id: str,
        target: str,
        permission: str,
        created_by: str | None,
        visibility_table: str | None,
        subagent_transaction_token: SubagentTransactionToken | None,
    ) -> dict[str, Any]:
        """Apply an audience mutation on the caller-owned transaction."""
        self._validate_audience_mutation(
            resource_kind=resource_kind,
            target=target,
            permission=permission,
            visibility_table=visibility_table,
        )
        require_team_subagent_transaction_token(
            resource_kind,
            subagent_transaction_token,
        )
        now = _now()
        revoked: list[str] = []
        team_grant_id: str | None = None
        if target == "private":
            revoked = await self._revoke_grants_for_resource_conn(
                db,
                team_id,
                resource_kind,
                resource_id,
                subagent_transaction_token=subagent_transaction_token,
            )
        elif target == "custom":
            revoked = await self._revoke_grants_for_resource_conn(
                db,
                team_id,
                resource_kind,
                resource_id,
                principal_types=("team",),
                subagent_transaction_token=subagent_transaction_token,
            )
        else:  # team_wide — upsert the team-principal allow grant (write-through)
            async with db.execute(
                "SELECT id FROM resource_grants WHERE team_id=? AND resource_kind=? "
                "AND resource_id=? AND principal_type='team' AND principal_id=? "
                "AND permission=? AND effect='allow' AND revoked_at IS NULL",
                (team_id, resource_kind, resource_id, team_id, permission),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                team_grant_id = str(existing["id"])
                await db.execute(
                    "UPDATE resource_grants SET updated_at=?, created_by=? WHERE id=? AND team_id=?",
                    (now, created_by, team_grant_id, team_id),
                )
            else:
                team_grant_id = _id("grant")
                await db.execute(
                    "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,"
                    "principal_type,principal_id,permission,effect,created_by,"
                    "created_at,updated_at,expires_at,revoked_at) "
                    "VALUES (?,?,?,?,'team',?,?,'allow',?,?,?,NULL,NULL)",
                    (
                        team_grant_id,
                        team_id,
                        resource_kind,
                        resource_id,
                        team_id,
                        permission,
                        created_by,
                        now,
                        now,
                    ),
                )
        if visibility_table is not None:
            visibility = "team_wide" if target == "team_wide" else "creator_only"
            await db.execute(
                f"UPDATE {visibility_table} SET visibility=?, updated_at=? "  # noqa: S608 — table from whitelist
                "WHERE team_id=? AND id=?",
                (visibility, now, team_id, resource_id),
            )
        await self._bump_policy_epoch_conn(db, team_id)
        return {
            "revoked_grant_ids": revoked,
            "team_grant_id": team_grant_id,
            "target": target,
        }

    def _validate_audience_mutation(
        self,
        *,
        resource_kind: str,
        target: str,
        permission: str,
        visibility_table: str | None,
    ) -> None:
        if target not in _AUDIENCE_TARGETS:
            raise ValueError(f"invalid audience target: {target}")
        if visibility_table is not None and visibility_table not in _VISIBILITY_TABLES:
            raise ValueError(f"invalid visibility table: {visibility_table}")
        if visibility_table is not None and visibility_table != resource_kind:
            raise ValueError(
                "visibility table must match resource_kind",
            )
        if permission not in _VALID_PERMISSIONS:
            raise ValueError(f"invalid permission: {permission}")

    async def matching_grants(
        self,
        ctx: AccessContext | dict[str, Any],
        resource_kind: str,
        resource_id: str,
        permission: str,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> list[ResourceGrantRecord]:
        _require_read_transaction(connection)
        principals = self._principal_refs(ctx)
        if not principals:
            return []
        principal_clause = " OR ".join(["(principal_type=? AND principal_id=?)"] * len(principals))
        params: list[Any] = [
            _ctx_attr(ctx, "team_id", ""),
            resource_kind,
            resource_id,
            _now(),
        ]
        for ptype, pid in principals:
            params.extend([ptype, pid])
        sql = (
            "SELECT * FROM resource_grants "
            "WHERE team_id=? AND resource_kind=? AND resource_id=? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            f"AND ({principal_clause}) "
            "ORDER BY CASE effect WHEN 'deny' THEN 0 ELSE 1 END, updated_at DESC"
        )
        async with self._read_connection(connection) as db, db.execute(sql, params) as cur:
            cur.row_factory = aiosqlite.Row
            rows = await cur.fetchall()
        return [
            grant
            for grant in (_row_to_grant(row) for row in rows)
            if _resource_permission_satisfies(
                resource_kind,
                granted=grant.permission,
                requested=permission,
                effect=grant.effect,
            )
        ]

    async def has_active_allowlist(
        self,
        team_id: str,
        resource_kind: str,
        resource_id: str,
        permission: str,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> bool:
        async with (
            self._read_connection(connection) as db,
            db.execute(
                "SELECT permission FROM resource_grants WHERE team_id=? AND resource_kind=? "
                "AND resource_id=? AND effect='allow' AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (team_id, resource_kind, resource_id, _now()),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return bool(rows)

    async def explain_decision(
        self,
        ctx: AccessContext | dict[str, Any],
        resource_kind: str,
        resource_id: str,
        permission: str,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> dict[str, Any]:
        team_id = str(_ctx_attr(ctx, "team_id", "") or "")
        matches = await self.matching_grants(ctx, resource_kind, resource_id, permission, connection=connection)
        decision = _decision_from_matches(matches)
        if decision is not None:
            return decision
        has_allowlist = await self.has_active_allowlist(team_id, resource_kind, resource_id, permission, connection=connection)
        return _decision_without_matches(has_allowlist=has_allowlist)

    async def explain_decisions(
        self,
        ctx: AccessContext | dict[str, Any],
        resource_kind: str,
        resource_ids: list[str],
        permission: str,
    ) -> dict[str, dict[str, Any]]:
        """Batch form of :meth:`explain_decision` for list endpoints.

        Returns one decision per distinct id with exactly the semantics of calling
        ``explain_decision`` for each id, but with two queries per chunk of ids
        instead of two queries per id.
        """
        ids = list(dict.fromkeys(str(rid) for rid in resource_ids if rid))
        if not ids:
            return {}
        team_id = str(_ctx_attr(ctx, "team_id", "") or "")
        principals = self._principal_refs(ctx)
        principal_clause = " OR ".join(["(principal_type=? AND principal_id=?)"] * len(principals))
        now = _now()
        matches: dict[str, list[ResourceGrantRecord]] = {rid: [] for rid in ids}
        allowlisted: set[str] = set()
        async with self._read_connection(None) as db:
            for start in range(0, len(ids), _BATCH_ID_CHUNK):
                chunk = ids[start : start + _BATCH_ID_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                if principals:
                    params: list[Any] = [team_id, resource_kind, *chunk, now]
                    for ptype, pid in principals:
                        params.extend([ptype, pid])
                    sql = (
                        "SELECT * FROM resource_grants "
                        f"WHERE team_id=? AND resource_kind=? AND resource_id IN ({placeholders}) "
                        "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
                        f"AND ({principal_clause}) "
                        "ORDER BY CASE effect WHEN 'deny' THEN 0 ELSE 1 END, updated_at DESC"
                    )
                    async with db.execute(sql, params) as cur:
                        cur.row_factory = aiosqlite.Row
                        rows = await cur.fetchall()
                    for grant in (_row_to_grant(row) for row in rows):
                        if _resource_permission_satisfies(
                            resource_kind,
                            granted=grant.permission,
                            requested=permission,
                            effect=grant.effect,
                        ):
                            matches[grant.resource_id].append(grant)
                async with db.execute(
                    "SELECT DISTINCT resource_id FROM resource_grants WHERE team_id=? AND resource_kind=? "
                    f"AND resource_id IN ({placeholders}) AND effect='allow' AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (team_id, resource_kind, *chunk, now),
                ) as cur:
                    allowlisted.update(str(row[0]) for row in await cur.fetchall())
        out: dict[str, dict[str, Any]] = {}
        for rid in ids:
            decision = _decision_from_matches(matches[rid])
            out[rid] = decision if decision is not None else _decision_without_matches(has_allowlist=rid in allowlisted)
        return out

    async def is_allowed(
        self,
        ctx: AccessContext | dict[str, Any],
        resource_kind: str,
        resource_id: str,
        permission: str,
        *,
        enforce: bool | None = None,
    ) -> bool:
        decision = await self.explain_decision(ctx, resource_kind, resource_id, permission)
        if decision["allowed"]:
            return True
        if enforce is None:
            enforce = resource_grants_enforce_enabled()
        return not enforce

    async def record_runtime_denied(
        self,
        ctx: AccessContext | dict[str, Any],
        *,
        resource_kind: str,
        resource_id: str,
        permission: str,
        reason: str,
        target_kind: str = "resource",
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort admin-audit row for runtime permission denials.

        ``AccessControlStore`` shares the gateway DB path with ``UserStore`` in
        production. Some narrow unit tests initialize only the access tables; in
        those cases the audit table is absent and this method silently no-ops.
        """
        team_id = str(_ctx_attr(ctx, "team_id", "") or "")
        user_id = str(_ctx_attr(ctx, "user_id", "") or "")
        if not team_id or not user_id:
            return
        try:
            from bowhead_core.audit_actions import AuditAction

            payload_dict = {
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "permission": permission,
                "reason": reason,
                "workspace_id": str(_ctx_attr(ctx, "workspace_id", "") or ""),
                "shadow": not resource_grants_enforce_enabled(),
                **(payload or {}),
            }
            payload_json = json.dumps(payload_dict, ensure_ascii=False)
            if len(payload_json) > 5000:
                payload_dict["_truncated"] = True
                payload_json = json.dumps(payload_dict, ensure_ascii=False)[:5000]
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO admin_audit_log "
                    "(id, actor_user_id, team_id, action, target_kind, target_id, target_team_id, "
                    " scope, payload_json, ts, ip, user_agent) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (
                        _id("aud"),
                        user_id,
                        team_id,
                        AuditAction.RESOURCE_ACCESS_DENIED,
                        target_kind,
                        target_id or resource_id,
                        team_id,
                        "runtime",
                        payload_json,
                        _now(),
                    ),
                )
                await db.commit()
        except Exception:
            return

    async def record_runtime_denial_summary(
        self,
        ctx: AccessContext | dict[str, Any],
        *,
        resource_kind: str,
        permission: str,
        denied: list[tuple[str, str]],
        target_kind: str,
        target_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """One audit row for a list request that filtered out ``denied`` resources.

        ``denied`` holds ``(resource_id, reason)`` pairs. Per-id routes keep writing one
        ``resource_access_denied`` row per resource; list endpoints write this single
        ``resource_list_access_filtered`` row with the count, the counts by reason and a
        capped sample of ids, so audit volume does not grow with list size or polling.

        Unlike :meth:`record_runtime_denied` this raises on write failure; the caller
        decides how to report it.
        """
        team_id = str(_ctx_attr(ctx, "team_id", "") or "")
        user_id = str(_ctx_attr(ctx, "user_id", "") or "")
        if not team_id or not user_id or not denied:
            return
        from bowhead_core.audit_actions import AuditAction

        reasons: dict[str, int] = {}
        for _, reason in denied:
            reasons[reason] = reasons.get(reason, 0) + 1
        sample = [resource_id for resource_id, _ in denied[:DENIAL_SUMMARY_SAMPLE_SIZE]]
        payload_dict = {
            "resource_kind": resource_kind,
            "permission": permission,
            "denied_count": len(denied),
            "reasons": dict(sorted(reasons.items())),
            "sample_resource_ids": sample,
            "sample_truncated": len(denied) > len(sample),
            "workspace_id": str(_ctx_attr(ctx, "workspace_id", "") or ""),
            "mode": "enforce" if resource_grants_enforce_enabled() else "shadow",
            **(payload or {}),
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO admin_audit_log "
                "(id, actor_user_id, team_id, action, target_kind, target_id, target_team_id, "
                " scope, payload_json, ts, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    _id("aud"),
                    user_id,
                    team_id,
                    AuditAction.RESOURCE_LIST_ACCESS_FILTERED,
                    target_kind,
                    target_id,
                    team_id,
                    "runtime",
                    json.dumps(payload_dict, ensure_ascii=False),
                    _now(),
                ),
            )
            await db.commit()

    def _principal_refs(self, ctx: AccessContext | dict[str, Any]) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        user_id = str(_ctx_attr(ctx, "user_id", "") or "")
        team_id = str(_ctx_attr(ctx, "team_id", "") or "")
        workspace_id = str(_ctx_attr(ctx, "workspace_id", "") or "")
        if user_id:
            refs.append(("user", user_id))
        group_ids = _ctx_attr(ctx, "group_ids", ()) or ()
        for group_id in group_ids:
            if group_id:
                refs.append(("group", str(group_id)))
        if workspace_id:
            refs.append(("workspace", workspace_id))
        if team_id:
            refs.append(("team", team_id))
        return refs

    async def _get_group_conn(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        group_id: str,
    ) -> GroupRecord | None:
        async with db.execute(
            "SELECT g.*, 0 AS member_count FROM groups g WHERE g.team_id=? AND g.id=?",
            (team_id, group_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_group(row) if row else None

    async def _assert_user_in_team_conn(
        self,
        db: aiosqlite.Connection,
        user_id: str,
        team_id: str,
    ) -> None:
        async with db.execute(
            "SELECT 1 FROM users WHERE id=? AND team_id=? AND status='active'",
            (user_id, team_id),
        ) as cur:
            if await cur.fetchone():
                return
        raise ValueError("user_not_in_team")

    async def _assert_workspace_in_team_conn(
        self,
        db: aiosqlite.Connection,
        workspace_id: str,
        team_id: str,
    ) -> None:
        async with db.execute(
            "SELECT 1 FROM workspaces WHERE id=? AND team_id=? AND status='active'",
            (workspace_id, team_id),
        ) as cur:
            if await cur.fetchone():
                return
        raise ValueError("workspace_not_in_team")

    async def _assert_workspace_member_conn(
        self,
        db: aiosqlite.Connection,
        workspace_id: str,
        user_id: str,
    ) -> None:
        async with db.execute(
            "SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=? AND status='active'",
            (workspace_id, user_id),
        ) as cur:
            if await cur.fetchone():
                return
        raise ValueError("not_workspace_member")
