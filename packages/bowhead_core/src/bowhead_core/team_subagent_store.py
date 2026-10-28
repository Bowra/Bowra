"""Release A compatibility boundary for team-managed Subagents.

This store owns the additive, non-activating Stage 0B migration and the sole
Release A compatibility writer:

- extend the legacy ``team_subagent`` table without rewriting existing rows;
- create schema/application compatibility metadata;
- install database guards that remain dormant until a later explicit
  Release B activation command sets the semantic activation timestamps.
- preserve legacy CRUD behavior while keeping definition, initial audience,
  grant cleanup, optimistic concurrency, and route audit work on one SQLite
  transaction.
- expose the versioned exact-audience aggregate writer while synchronizing the
  dormant ``audience_mode`` field and legacy ``visibility`` projection.

It intentionally exposes no activation, backfill, Owner-transfer, or general
lifecycle API. The one lifecycle exception is the always-on R0 member
deactivation guard: it blocks enabled shared Agents and quarantines private
Agents inside the caller-owned membership transaction. The R0 HTTP write
boundary uses ``row_version`` for ETag compare-and-swap while legacy
``UserStore`` method signatures keep working throughout Release A by delegating
here.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import bootstrap_shared_db, open_shared_ctx
from bowhead_core.subagent_write_boundary import (
    TEAM_SUBAGENT_RESOURCE_KIND,
    _issue_team_subagent_transaction_token,
)

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionStore
    from bowhead_core.user_store import TeamSubagent

RELEASE_A_SCHEMA_VERSION = 2
RELEASE_A_COMPATIBILITY_VERSION = "0001"
CONVERSATIONAL_DRAFT_COMPATIBILITY_VERSION = "0002"
AGENT_RELEASE_COMPATIBILITY_VERSION = "0003"
PRIVATE_QUARANTINE_DAYS_ENV = "BOWHEAD_SUBAGENT_PRIVATE_QUARANTINE_DAYS"
MINIMUM_PRIVATE_QUARANTINE_DAYS = 30
_MEMBER_DEACTIVATION_QUARANTINE_REASON = "owner_member_deactivated"


def _effective_name_key(value: str, *, allow_legacy_controls: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if not allow_legacy_controls and any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized):
        raise TeamSubagentNameConflictError(
            "display name contains unsupported control characters",
            code="display_name_unavailable",
        )
    collapsed = " ".join(normalized.strip().split()).casefold()
    if not collapsed:
        raise TeamSubagentNameConflictError(
            "display name is empty after normalization",
            code="display_name_unavailable",
        )
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def effective_agent_name_key(value: str, *, allow_legacy_controls: bool = False) -> str:
    """Return the canonical team Agent display-name claim key."""
    return _effective_name_key(value, allow_legacy_controls=allow_legacy_controls)


async def release_semantics_active_conn(db: aiosqlite.Connection) -> bool:
    """Return whether this deployment has cut over to immutable release semantics.

    ``team_subagent_schema_meta.release_semantics_activated_at`` is the single
    switch that decides whether every ``team_subagent`` row is guaranteed to carry
    a revisioned definition. Startup runs the release baseline import; it may skip
    unsafe data-integrity cases, leaving this marker unset.

    Every **writer** honours it (create, edit, archive, restore, delete, Fork).
    Readers must consult the same switch: a surface that hard-requires
    ``definition_mode='revisioned'`` without it rejects every Agent on a
    deployment whose baseline import has not run.  Missing table or column means
    a pre-M2 schema, which is by definition not cut over.

    This is the connection-scoped form, so a caller already inside a transaction
    does not need a second connection.
    """
    async with db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='team_subagent_schema_meta'",
    ) as cursor:
        if await cursor.fetchone() is None:
            return False
    async with db.execute("PRAGMA table_info(team_subagent_schema_meta)") as cursor:
        columns = await cursor.fetchall()
    if not any(str(column[1]) == "release_semantics_activated_at" for column in columns):
        return False
    async with db.execute(
        "SELECT release_semantics_activated_at FROM team_subagent_schema_meta WHERE singleton=1",
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None and row[0] is not None


def private_subagent_quarantine_days() -> int:
    """Return the deployment retention policy, rejecting unsafe values."""
    raw = os.environ.get(
        PRIVATE_QUARANTINE_DAYS_ENV,
        str(MINIMUM_PRIVATE_QUARANTINE_DAYS),
    ).strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{PRIVATE_QUARANTINE_DAYS_ENV} must be an integer >= {MINIMUM_PRIVATE_QUARANTINE_DAYS}",
        ) from exc
    if days < MINIMUM_PRIVATE_QUARANTINE_DAYS:
        raise RuntimeError(
            f"{PRIVATE_QUARANTINE_DAYS_ENV} must be >= {MINIMUM_PRIVATE_QUARANTINE_DAYS}",
        )
    return days


class Clock(Protocol):
    """Clock used for persisted RFC 3339 UTC timestamps."""

    def now(self) -> datetime: ...


class SystemClock:
    """Production UTC clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class TeamSubagentNameConflictError(ValueError):
    """A team already owns the requested immutable Subagent routing name."""

    def __init__(self, message: str, *, code: str = "subagent_name_unavailable") -> None:
        super().__init__(message)
        self.code = code


class TeamSubagentVersionConflictError(RuntimeError):
    """A Subagent mutation used a stale optimistic-concurrency version."""

    def __init__(self, *, expected_version: int, current_version: int) -> None:
        super().__init__("team_subagent_version_conflict")
        self.expected_version = expected_version
        self.current_version = current_version


class TeamSubagentAudienceInvalidError(ValueError):
    """An exact audience references invalid principals or violates its shape."""

    def __init__(
        self,
        message: str,
        *,
        invalid_user_ids: tuple[str, ...] = (),
        invalid_group_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.invalid_user_ids = invalid_user_ids
        self.invalid_group_ids = invalid_group_ids


class TeamSubagentAudienceForbiddenError(PermissionError):
    """The transaction-time actor is no longer the active current Owner."""


class TeamSubagentOwnerInactiveError(PermissionError):
    """Create actor is not the active member recorded as the resource Owner."""


class TeamSubagentDraftNotConsumableError(RuntimeError):
    """A draft create lost ownership, freshness, version, or ready state."""

    def __init__(self, code: str = "team_subagent_draft_not_consumable") -> None:
        super().__init__(code)
        self.code = code


class TeamSubagentDeleteDependencyError(RuntimeError):
    """Hard delete is unsafe until sharing or scheduled references are removed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TeamSubagentRevisionWriterRequiredError(RuntimeError):
    """A post-cutover definition or Audience write bypassed release governance."""

    def __init__(self, code: str = "agent_revision_writer_required") -> None:
        super().__init__(code)
        self.code = code


class TeamSubagentForkAdmissionError(RuntimeError):
    """A Fork lost source version, policy, access, or ownership at commit time."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TeamSubagentMutationResult:
    """Result of a version-checked mutation."""

    changed: bool
    version: int


@dataclass(frozen=True, slots=True)
class TeamSubagentAudienceMutationResult:
    """Result of an exact audience compare-and-swap."""

    changed: bool
    version: int
    mode: str
    user_ids: tuple[str, ...]
    group_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TeamSubagentMemberDeactivationResult:
    """Safe aggregate result for the caller-owned membership transaction."""

    enabled_shared: int
    quarantined_private: int
    revoked_private_allow_grants: int


@dataclass(frozen=True, slots=True)
class TeamSubagentSchemaMetadata:
    schema_version: int
    minimum_reader_version: str
    minimum_writer_version: str
    owner_semantics_activated_at: str | None
    lifecycle_semantics_activated_at: str | None
    audience_semantics_activated_at: str | None
    release_semantics_activated_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class TeamSubagentForkCommit:
    """Server-derived provenance admitted inside the aggregate create transaction."""

    lineage_id: str
    source_agent_id: str
    source_revision_ref: str
    source_release_id: str
    source_owner_user_id: str
    source_display_name: str
    source_snapshot_hash: str
    policy: str
    policy_row_version: int
    dependency_gaps: tuple[Mapping[str, Any], ...]
    idempotency_hash: str
    request_hash: str


def agent_fork_source_snapshot_hash(
    *,
    source_agent_id: str,
    source_revision_ref: str,
    source_release_id: str,
    definition: Mapping[str, Any],
    listing: Mapping[str, Any],
) -> str:
    """Hash the immutable source selected by the Fork flow."""
    payload = json.dumps(
        {
            "definition": definition,
            "listing": listing,
            "source_agent_id": source_agent_id,
            "source_release_id": source_release_id,
            "source_revision_ref": source_revision_ref,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class _ColumnSpec:
    name: str
    definition: str
    data_type: str
    not_null: bool = False
    default: str | None = None
    primary_key: bool = False


_TEAM_SUBAGENT_COLUMNS = (
    _ColumnSpec("owner_user_id", "TEXT", "TEXT"),
    _ColumnSpec(
        "row_version",
        "INTEGER NOT NULL DEFAULT 1",
        "INTEGER",
        not_null=True,
        default="1",
    ),
    _ColumnSpec(
        "audience_mode",
        "TEXT NOT NULL DEFAULT 'private'",
        "TEXT",
        not_null=True,
        default="private",
    ),
    _ColumnSpec(
        "lifecycle_state",
        "TEXT NOT NULL DEFAULT 'active'",
        "TEXT",
        not_null=True,
        default="active",
    ),
    _ColumnSpec("quarantine_started_at", "TEXT", "TEXT"),
    _ColumnSpec("quarantine_expires_at", "TEXT", "TEXT"),
    _ColumnSpec("quarantine_reason", "TEXT", "TEXT"),
    _ColumnSpec("quarantined_owner_user_id", "TEXT", "TEXT"),
)

_SCHEMA_META_COLUMNS = (
    _ColumnSpec("singleton", "INTEGER PRIMARY KEY", "INTEGER", primary_key=True),
    _ColumnSpec("schema_version", "INTEGER NOT NULL", "INTEGER", not_null=True),
    _ColumnSpec(
        "minimum_reader_version",
        "TEXT NOT NULL",
        "TEXT",
        not_null=True,
    ),
    _ColumnSpec(
        "minimum_writer_version",
        "TEXT NOT NULL",
        "TEXT",
        not_null=True,
    ),
    _ColumnSpec("owner_semantics_activated_at", "TEXT", "TEXT"),
    _ColumnSpec("lifecycle_semantics_activated_at", "TEXT", "TEXT"),
    _ColumnSpec("audience_semantics_activated_at", "TEXT", "TEXT"),
    _ColumnSpec("release_semantics_activated_at", "TEXT", "TEXT"),
    _ColumnSpec("updated_at", "TEXT NOT NULL", "TEXT", not_null=True),
)

_APP_COMPATIBILITY_COLUMNS = (
    _ColumnSpec("instance_id", "TEXT PRIMARY KEY", "TEXT", primary_key=True),
    _ColumnSpec("reader_version", "TEXT NOT NULL", "TEXT", not_null=True),
    _ColumnSpec("writer_version", "TEXT NOT NULL", "TEXT", not_null=True),
    _ColumnSpec("started_at", "TEXT NOT NULL", "TEXT", not_null=True),
    _ColumnSpec("heartbeat_at", "TEXT NOT NULL", "TEXT", not_null=True),
)

_METADATA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS team_subagent_schema_meta (
      singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
      schema_version INTEGER NOT NULL,
      minimum_reader_version TEXT NOT NULL,
      minimum_writer_version TEXT NOT NULL,
      owner_semantics_activated_at TEXT,
      lifecycle_semantics_activated_at TEXT,
      audience_semantics_activated_at TEXT,
      release_semantics_activated_at TEXT,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_subagent_app_compatibility (
      instance_id TEXT PRIMARY KEY,
      reader_version TEXT NOT NULL,
      writer_version TEXT NOT NULL,
      started_at TEXT NOT NULL,
      heartbeat_at TEXT NOT NULL
    )
    """,
)

_INDEX_DDL = (
    """
    CREATE INDEX IF NOT EXISTS idx_team_subagent_team_owner
      ON team_subagent(team_id, owner_user_id, lifecycle_state, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_team_subagent_team_updated
      ON team_subagent(team_id, updated_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_team_subagent_quarantine_expiry
      ON team_subagent(lifecycle_state, quarantine_expires_at)
      WHERE lifecycle_state = 'quarantined'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_team_subagent_team_audience
      ON team_subagent(team_id, audience_mode, lifecycle_state, status)
    """,
)

_TRIGGER_DDL = (
    """
    CREATE TRIGGER IF NOT EXISTS team_subagent_invariant_guard_update
    BEFORE UPDATE ON team_subagent
    WHEN (
      (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND audience_semantics_activated_at IS NOT NULL
        )
        AND NEW.audience_mode NOT IN ('private', 'custom', 'teamwide')
      )
      OR (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND lifecycle_semantics_activated_at IS NOT NULL
        )
        AND (
          NEW.lifecycle_state NOT IN ('active', 'quarantined')
          OR (
            NEW.lifecycle_state = 'quarantined'
            AND NEW.status <> 'disabled'
          )
          OR NEW.row_version < OLD.row_version
        )
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'team_subagent_invariant_violation');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS team_subagent_version_guard_update
    BEFORE UPDATE ON team_subagent
    WHEN EXISTS (
      SELECT 1
      FROM team_subagent_schema_meta
      WHERE singleton = 1
        AND owner_semantics_activated_at IS NOT NULL
    )
    AND (
      NEW.owner_user_id IS NOT OLD.owner_user_id
      OR NEW.status IS NOT OLD.status
      OR NEW.audience_mode IS NOT OLD.audience_mode
      OR NEW.visibility IS NOT OLD.visibility
      OR NEW.lifecycle_state IS NOT OLD.lifecycle_state
      OR NEW.quarantine_started_at IS NOT OLD.quarantine_started_at
      OR NEW.quarantine_expires_at IS NOT OLD.quarantine_expires_at
      OR NEW.quarantine_reason IS NOT OLD.quarantine_reason
      OR NEW.quarantined_owner_user_id IS NOT OLD.quarantined_owner_user_id
      OR NEW.name IS NOT OLD.name
      OR NEW.display_name IS NOT OLD.display_name
      OR NEW.description IS NOT OLD.description
      OR NEW.system_prompt IS NOT OLD.system_prompt
      OR NEW.tool_refs_json IS NOT OLD.tool_refs_json
      OR NEW.model IS NOT OLD.model
      OR NEW.max_iterations IS NOT OLD.max_iterations
      OR NEW.skills_json IS NOT OLD.skills_json
      OR NEW.output_schema_json IS NOT OLD.output_schema_json
    )
    AND NOT (
      (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND audience_semantics_activated_at IS NOT NULL
        )
        AND NEW.audience_mode NOT IN ('private', 'custom', 'teamwide')
      )
      OR (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND lifecycle_semantics_activated_at IS NOT NULL
        )
        AND (
          NEW.lifecycle_state NOT IN ('active', 'quarantined')
          OR (
            NEW.lifecycle_state = 'quarantined'
            AND NEW.status <> 'disabled'
          )
          OR NEW.row_version < OLD.row_version
        )
      )
    )
    AND NEW.row_version <> OLD.row_version + 1
    BEGIN
      SELECT RAISE(ABORT, 'team_subagent_version_required');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS team_subagent_creator_immutable_update
    BEFORE UPDATE ON team_subagent
    WHEN EXISTS (
      SELECT 1
      FROM team_subagent_schema_meta
      WHERE singleton = 1
        AND owner_semantics_activated_at IS NOT NULL
    )
    AND NEW.created_by IS NOT OLD.created_by
    BEGIN
      SELECT RAISE(ABORT, 'team_subagent_created_by_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS team_subagent_invariant_guard_insert
    BEFORE INSERT ON team_subagent
    WHEN (
      (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND owner_semantics_activated_at IS NOT NULL
        )
        AND NEW.owner_user_id IS NULL
      )
      OR (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND audience_semantics_activated_at IS NOT NULL
        )
        AND NEW.audience_mode NOT IN ('private', 'custom', 'teamwide')
      )
      OR (
        EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND lifecycle_semantics_activated_at IS NOT NULL
        )
        AND (
          NEW.lifecycle_state NOT IN ('active', 'quarantined')
          OR (
            NEW.lifecycle_state = 'quarantined'
            AND NEW.status <> 'disabled'
          )
        )
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'team_subagent_invariant_violation');
    END
    """,
)


class TeamSubagentStore:
    """Own the additive Release A schema compatibility boundary."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self.db_path = str(db_path)
        self._clock = clock or SystemClock()
        self._transaction_token = _issue_team_subagent_transaction_token()

    async def initialize(self) -> None:
        """Apply the idempotent migration without activating new semantics."""
        private_subagent_quarantine_days()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        await bootstrap_shared_db(self.db_path)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_legacy_table(db)
                await self._add_missing_team_subagent_columns(db)
                for ddl in _METADATA_DDL:
                    await db.execute(ddl)
                await self._validate_table_shape(
                    db,
                    "team_subagent_schema_meta",
                    _SCHEMA_META_COLUMNS,
                )
                await self._validate_table_shape(
                    db,
                    "team_subagent_app_compatibility",
                    _APP_COMPATIBILITY_COLUMNS,
                )
                await self._initialize_metadata(db)
                for ddl in _INDEX_DDL:
                    await db.execute(ddl)
                for ddl in _TRIGGER_DDL:
                    await db.execute(ddl)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def release_semantics_active(self) -> bool:
        """Return whether immutable Agent release writers are mandatory."""
        async with open_shared_ctx(self.db_path) as db:
            return await self._release_semantics_active_conn(db)

    async def release_definition_state(
        self,
        *,
        team_id: str,
        subagent_id: str,
    ) -> tuple[str, str] | None:
        """``(definition_mode, lifecycle_state)``，release 语义未启用或行不在时返 ``None``。

        给**调用方在下手之前**问一句「这条路还通不通」用的。`compare_and_update_team_subagent`
        内部本来就先判这个再比版本（见该方法），但那时调用方已经过了自己的版本闸——
        闸先炸的话，用户拿到的是「版本冲突，请刷新重试」，而真相是**这条路已经关了，
        刷新一万次也不会成功**。见 `routes/team_subagents._require_release_writer_path`。
        """
        async with open_shared_ctx(self.db_path) as db:
            if not await self._release_semantics_active_conn(db):
                return None
            try:
                return await self._release_state_conn(db, team_id=team_id, subagent_id=subagent_id)
            except TeamSubagentRevisionWriterRequiredError:
                return None

    async def prepare_member_deactivation_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        owner_user_id: str,
        actor_user_id: str,
    ) -> TeamSubagentMemberDeactivationResult:
        """Block shared or quarantine private owned Agents without committing.

        The caller must hold the same ``BEGIN IMMEDIATE`` transaction used to
        remove the team membership. This method deliberately returns before any
        mutation when an enabled shared Agent exists.
        """
        from bowhead_core.access_control import AccessControlStore
        from bowhead_core.audit_actions import AuditAction
        from bowhead_core.user_store import UserStore

        timestamp = self._legacy_now()
        async with db.execute(
            """
            WITH semantics AS (
              SELECT
                COALESCE(MAX(owner_semantics_activated_at IS NOT NULL), 0)
                  AS owner_activated,
                COALESCE(MAX(lifecycle_semantics_activated_at IS NOT NULL), 0)
                  AS lifecycle_activated,
                COALESCE(MAX(audience_semantics_activated_at IS NOT NULL), 0)
                  AS audience_activated
              FROM team_subagent_schema_meta
              WHERE singleton = 1
            )
            SELECT
              sa.id,
              sa.status,
              sa.row_version,
              CASE
                WHEN semantics.lifecycle_activated
                  THEN sa.lifecycle_state
                ELSE 'active'
              END AS effective_lifecycle_state,
              CASE
                WHEN semantics.audience_activated
                  THEN sa.audience_mode
                WHEN sa.visibility = 'team_wide'
                  THEN 'teamwide'
                WHEN EXISTS (
                  SELECT 1
                  FROM resource_grants legacy_allow
                  WHERE legacy_allow.team_id = sa.team_id
                    AND legacy_allow.resource_kind = 'team_subagent'
                    AND legacy_allow.resource_id = sa.id
                    AND legacy_allow.principal_type IN ('user', 'group')
                    AND legacy_allow.permission IN ('use', 'write', 'admin')
                    AND legacy_allow.effect = 'allow'
                    AND legacy_allow.revoked_at IS NULL
                    AND (
                      legacy_allow.expires_at IS NULL
                      OR legacy_allow.expires_at > ?
                    )
                )
                  THEN 'custom'
                ELSE 'private'
              END AS effective_audience_mode
            FROM team_subagent sa
            CROSS JOIN semantics
            WHERE sa.team_id = ?
              AND (
                CASE
                  WHEN semantics.owner_activated
                    THEN sa.owner_user_id
                  ELSE sa.created_by
                END
              ) = ?
            ORDER BY sa.id
            """,
            (timestamp, team_id, owner_user_id),
        ) as cursor:
            owned_rows = await cursor.fetchall()

        enabled_shared = sum(
            row["status"] == "enabled" and row["effective_lifecycle_state"] == "active" and row["effective_audience_mode"] in {"custom", "teamwide"}
            for row in owned_rows
        )
        if enabled_shared:
            return TeamSubagentMemberDeactivationResult(
                enabled_shared=enabled_shared,
                quarantined_private=0,
                revoked_private_allow_grants=0,
            )

        quarantine_started_at = timestamp
        quarantine_expires_at = (self._clock.now() + timedelta(days=private_subagent_quarantine_days())).astimezone(UTC).isoformat()
        access_store = AccessControlStore(self.db_path)
        has_resource_grants = await self._table_exists_conn(
            db,
            "resource_grants",
        )
        quarantined_private = 0
        revoked_private_allow_grants = 0
        for row in owned_rows:
            if row["effective_audience_mode"] != "private":
                continue
            subagent_id = str(row["id"])
            if has_resource_grants:
                revoked = await access_store._revoke_grants_for_resource_conn(
                    db,
                    team_id,
                    TEAM_SUBAGENT_RESOURCE_KIND,
                    subagent_id,
                    effects=("allow",),
                    principal_types=("team", "user", "group"),
                    subagent_transaction_token=self._transaction_token,
                )
                revoked_private_allow_grants += len(revoked)
            if row["effective_lifecycle_state"] != "active":
                continue
            current_version = int(row["row_version"])
            next_version = current_version + 1
            updated = await db.execute(
                """
                UPDATE team_subagent
                SET status = 'disabled',
                    lifecycle_state = 'quarantined',
                    quarantine_started_at = ?,
                    quarantine_expires_at = ?,
                    quarantine_reason = ?,
                    quarantined_owner_user_id = ?,
                    row_version = ?,
                    updated_at = ?
                WHERE id = ?
                  AND team_id = ?
                  AND row_version = ?
                """,
                (
                    quarantine_started_at,
                    quarantine_expires_at,
                    _MEMBER_DEACTIVATION_QUARANTINE_REASON,
                    owner_user_id,
                    next_version,
                    timestamp,
                    subagent_id,
                    team_id,
                    current_version,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("team_subagent_member_deactivation_conflict")
            await UserStore(self.db_path).write_admin_audit_log(
                actor_user_id=actor_user_id,
                team_id=team_id,
                action=AuditAction.TEAM_SUBAGENT_QUARANTINED,
                target_kind=TEAM_SUBAGENT_RESOURCE_KIND,
                target_id=subagent_id,
                target_team_id=team_id,
                scope="team",
                payload={
                    "reason": _MEMBER_DEACTIVATION_QUARANTINE_REASON,
                    "owner_user_id": owner_user_id,
                    "before": {
                        "status": str(row["status"]),
                        "lifecycle_state": str(
                            row["effective_lifecycle_state"],
                        ),
                    },
                    "after": {
                        "status": "disabled",
                        "lifecycle_state": "quarantined",
                        "quarantine_expires_at": quarantine_expires_at,
                    },
                    "version": next_version,
                },
                conn=db,
            )
            quarantined_private += 1

        if revoked_private_allow_grants:
            await access_store._bump_policy_epoch_conn(db, team_id)
        return TeamSubagentMemberDeactivationResult(
            enabled_shared=0,
            quarantined_private=quarantined_private,
            revoked_private_allow_grants=revoked_private_allow_grants,
        )

    async def create_team_subagent(
        self,
        subagent: TeamSubagent,
        *,
        initial_audience_access_store: AccessControlStore | None = None,
        discover_access_store: bool = False,
        actor_user_id: str | None = None,
        audit_action: str | None = None,
        audit_payload: dict[str, Any] | None = None,
        require_active_owner: bool = False,
        origin_mode: str = "manual",
        source_draft_id: str | None = None,
        source_draft_expected_version: int | None = None,
        source_definition_fingerprint: str | None = None,
        source_draft_workspace_id: str | None = None,
        source_dependency_check: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
        revision_store: AgentRevisionStore | None = None,
        revision_definition: Mapping[str, Any] | None = None,
        revision_listing: Mapping[str, Any] | None = None,
        revision_target_audience: AgentAudience | None = None,
        fork_commit: TeamSubagentForkCommit | None = None,
    ) -> TeamSubagent:
        """Create a legacy row through the Release A aggregate boundary."""
        self._validate_audit_arguments(actor_user_id, audit_action)
        self._validate_access_store(initial_audience_access_store)
        if origin_mode not in {"manual", "intent_draft", "turn_draft"}:
            raise ValueError("invalid team_subagent origin_mode")
        if origin_mode != "manual" and source_draft_id is None:
            raise ValueError("draft origin requires source_draft_id")
        if origin_mode == "manual" and source_draft_id is not None and not source_draft_workspace_id:
            raise ValueError("manual draft requires a source space")
        if source_draft_id is not None and (
            source_draft_expected_version is None
            or source_definition_fingerprint is None
            or actor_user_id is None
            or subagent.created_by != actor_user_id
            or subagent.visibility != "creator_only"
            or subagent.status != "enabled"
            or initial_audience_access_store is not None
        ):
            raise ValueError("invalid source draft create contract")
        if initial_audience_access_store is not None and subagent.visibility != "team_wide":
            raise ValueError(
                "initial audience store requires team_wide visibility",
            )
        if fork_commit is not None and (
            actor_user_id is None
            or subagent.created_by != actor_user_id
            or subagent.visibility != "creator_only"
            or subagent.status != "enabled"
            or revision_target_audience is None
            or revision_target_audience.mode != "private"
            or source_draft_id is not None
            or initial_audience_access_store is not None
            or fork_commit.source_agent_id == subagent.id
        ):
            raise ValueError("invalid Agent Fork create contract")
        async with open_shared_ctx(self.db_path) as db:
            # The legacy UserStore writer did not enforce SQLite foreign keys.
            # Release A must remain compatible with existing rows/writers; the
            # later semantic activation migration owns FK/data cleanup.
            await db.execute("PRAGMA foreign_keys=OFF")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                release_semantics_active = await self._release_semantics_active_conn(db)
                if fork_commit is not None and not release_semantics_active:
                    raise TeamSubagentForkAdmissionError(
                        "agent_fork_release_cutover_required",
                    )
                if release_semantics_active and (
                    revision_store is None
                    or revision_definition is None
                    or revision_listing is None
                    or revision_target_audience is None
                    or actor_user_id is None
                ):
                    raise TeamSubagentRevisionWriterRequiredError()
                if revision_target_audience is not None:
                    expected_visibility = "team_wide" if revision_target_audience.mode == "teamwide" else "creator_only"
                    if subagent.visibility != expected_visibility:
                        raise ValueError("revision audience does not match requested visibility")
                initial_visibility = "creator_only" if release_semantics_active else subagent.visibility
                initial_audience_mode = "private" if release_semantics_active else ("teamwide" if subagent.visibility == "team_wide" else "private")
                source_draft: aiosqlite.Row | None = None
                if source_draft_id is not None:
                    if not await self._column_exists_conn(
                        db,
                        table_name="team_subagent",
                        column_name="source_draft_id",
                    ):
                        raise TeamSubagentDraftNotConsumableError(
                            "team_subagent_draft_schema_unavailable",
                        )
                    async with db.execute(
                        """
                        SELECT source_mode, workspace_id, status, validation_status,
                               definition_fingerprint, row_version, expires_at
                        FROM team_subagent_draft
                        WHERE id=? AND team_id=? AND owner_user_id=?
                        """,
                        (
                            source_draft_id,
                            subagent.team_id,
                            actor_user_id,
                        ),
                    ) as cursor:
                        source_draft = await cursor.fetchone()
                    if source_draft is None:
                        raise TeamSubagentDraftNotConsumableError()
                    expected_origin = {
                        "intent": "intent_draft",
                        "turn": "turn_draft",
                        "manual": "manual",
                    }.get(str(source_draft["source_mode"]))
                    if (
                        origin_mode != expected_origin
                        or (source_draft_workspace_id is not None and str(source_draft["workspace_id"]) != source_draft_workspace_id)
                        or str(source_draft["status"]) != "ready"
                        or str(source_draft["validation_status"]) not in {"valid", "warning"}
                        or int(source_draft["row_version"]) != source_draft_expected_version
                        or str(source_draft["definition_fingerprint"]) != source_definition_fingerprint
                        or str(source_draft["expires_at"]) <= self._legacy_now()
                    ):
                        raise TeamSubagentDraftNotConsumableError()
                if require_active_owner and (
                    actor_user_id is None
                    or subagent.created_by != actor_user_id
                    or not await self._is_active_team_member_conn(
                        db,
                        team_id=subagent.team_id,
                        user_id=actor_user_id,
                    )
                ):
                    raise TeamSubagentOwnerInactiveError(
                        "team_subagent_owner_inactive",
                    )
                if source_dependency_check is not None:
                    await source_dependency_check(db)
                if fork_commit is not None:
                    await self._admit_fork_conn(
                        db,
                        team_id=subagent.team_id,
                        actor_user_id=str(actor_user_id),
                        fork_commit=fork_commit,
                    )
                async with db.execute(
                    "SELECT 1 FROM team_subagent WHERE team_id=? AND name=?",
                    (subagent.team_id, subagent.name),
                ) as cursor:
                    if await cursor.fetchone():
                        raise TeamSubagentNameConflictError(
                            f"subagent name already exists in team: {subagent.name!r}",
                        )
                if await self._table_exists_conn(db, "agent_name_claim"):
                    await self._claim_effective_name_conn(
                        db,
                        team_id=subagent.team_id,
                        agent_id=subagent.id,
                        display_name=subagent.display_name or subagent.name,
                        timestamp=subagent.created_at,
                    )
                columns = (
                    "id,team_id,name,display_name,description,system_prompt,"
                    "tool_refs_json,model,max_iterations,skills_json,"
                    "output_schema_json,visibility,created_by,status,"
                    "created_at,updated_at"
                )
                values: list[Any] = [
                    subagent.id,
                    subagent.team_id,
                    subagent.name,
                    subagent.display_name,
                    subagent.description,
                    subagent.system_prompt,
                    json.dumps(subagent.tool_refs),
                    subagent.model,
                    subagent.max_iterations,
                    json.dumps(subagent.skills),
                    (
                        json.dumps(
                            subagent.output_schema,
                            ensure_ascii=False,
                        )
                        if subagent.output_schema is not None
                        else None
                    ),
                    initial_visibility,
                    subagent.created_by,
                    subagent.status,
                    subagent.created_at,
                    subagent.updated_at,
                ]
                if await self._column_exists_conn(
                    db,
                    table_name="team_subagent",
                    column_name="owner_user_id",
                ):
                    columns += ",owner_user_id"
                    values.append(subagent.created_by)
                if await self._column_exists_conn(
                    db,
                    table_name="team_subagent",
                    column_name="audience_mode",
                ):
                    columns += ",audience_mode"
                    values.append(initial_audience_mode)
                if source_draft_id is not None:
                    columns += ",origin_mode,source_draft_id"
                    values.extend((origin_mode, source_draft_id))
                placeholders = ",".join("?" for _ in values)
                await db.execute(
                    f"INSERT INTO team_subagent({columns}) VALUES ({placeholders})",
                    values,
                )
                effective_access_store = initial_audience_access_store
                if (
                    effective_access_store is None
                    and discover_access_store
                    and subagent.visibility == "team_wide"
                    and not release_semantics_active
                    and await self._table_exists_conn(db, "resource_grants")
                ):
                    from bowhead_core.access_control import AccessControlStore

                    effective_access_store = AccessControlStore(self.db_path)
                if effective_access_store is not None and not release_semantics_active:
                    await effective_access_store._set_audience_conn(
                        db,
                        team_id=subagent.team_id,
                        resource_kind=TEAM_SUBAGENT_RESOURCE_KIND,
                        resource_id=subagent.id,
                        target="team_wide",
                        permission="use",
                        created_by=actor_user_id,
                        visibility_table="team_subagent",
                        subagent_transaction_token=self._transaction_token,
                    )
                if source_draft_id is not None:
                    empty_assumptions = json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "subagent_draft_assumptions",
                            "value": [],
                        },
                        separators=(",", ":"),
                    )
                    empty_warnings = json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "subagent_draft_warnings",
                            "value": [],
                        },
                        separators=(",", ":"),
                    )
                    empty_issues = json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "subagent_draft_validation_issues",
                            "value": [],
                        },
                        separators=(",", ":"),
                    )
                    consumed_at = self._legacy_now()
                    cursor = await db.execute(
                        """
                        UPDATE team_subagent_draft
                        SET status='consumed',
                            purpose_text=NULL,
                            goal_summary=NULL,
                            definition_json=NULL,
                            recommendations_json=NULL,
                            assumptions_json=?,
                            warnings_json=?,
                            clarification_json=NULL,
                            validation_issues_json=?,
                            consumed_definition_id=?,
                            consumed_at=?,
                            row_version=row_version+1,
                            updated_at=?
                        WHERE id=? AND team_id=? AND owner_user_id=?
                          AND status='ready'
                          AND row_version=?
                          AND definition_fingerprint=?
                        """,
                        (
                            empty_assumptions,
                            empty_warnings,
                            empty_issues,
                            subagent.id,
                            consumed_at,
                            consumed_at,
                            source_draft_id,
                            subagent.team_id,
                            actor_user_id,
                            source_draft_expected_version,
                            source_definition_fingerprint,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise TeamSubagentDraftNotConsumableError()
                    await db.execute(
                        "DELETE FROM team_subagent_draft_suggestion WHERE draft_id=?",
                        (source_draft_id,),
                    )
                # Marketplace M1: every newly created Agent is immediately in
                # its Owner's personal roster. This is in the same transaction
                # as definition + draft consumption so create cannot succeed
                # with an unusable Owner Agent. Assignment never grants access;
                # runtime still applies the canonical access resolver.
                if actor_user_id is not None and subagent.created_by == actor_user_id and await self._table_exists_conn(db, "agent_assignment"):
                    timestamp = self._legacy_now()
                    await db.execute(
                        """
                        INSERT INTO agent_assignment(
                          id,team_id,user_id,agent_id,agent_source_kind,personal_state,access_state,
                          source,row_version,hired_at,current_cycle_started_at,personal_state_changed_at,
                          access_state_changed_at,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'active','granted','user_hired',1,?,?,?,?,?,?)
                        ON CONFLICT(team_id,user_id,agent_id) DO UPDATE SET
                          personal_state='active',access_state='granted',source='user_hired',
                          source_policy_version=NULL,row_version=agent_assignment.row_version+1,
                          current_cycle_started_at=excluded.current_cycle_started_at,
                          personal_state_changed_at=excluded.personal_state_changed_at,
                          access_state_changed_at=excluded.access_state_changed_at,
                          removed_at=NULL,updated_at=excluded.updated_at
                        """,
                        (
                            f"asa_{uuid4().hex[:20]}",
                            subagent.team_id,
                            actor_user_id,
                            subagent.id,
                            "team",
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                if release_semantics_active:
                    assert revision_store is not None
                    assert revision_definition is not None
                    assert revision_listing is not None
                    assert revision_target_audience is not None
                    publication = await revision_store.initialize_created_agent_conn(
                        db,
                        team_id=subagent.team_id,
                        agent_id=subagent.id,
                        actor_user_id=actor_user_id,
                        definition=revision_definition,
                        listing=revision_listing,
                        target_audience=revision_target_audience,
                    )
                    subagent.visibility = "creator_only"
                    subagent.row_version = publication.aggregate_row_version
                if fork_commit is not None:
                    if fork_commit.dependency_gaps:
                        disabled = await db.execute(
                            """
                            UPDATE team_subagent
                            SET status='disabled',row_version=row_version+1,updated_at=?
                            WHERE team_id=? AND id=? AND status='enabled' AND row_version=?
                            """,
                            (
                                self._legacy_now(),
                                subagent.team_id,
                                subagent.id,
                                subagent.row_version,
                            ),
                        )
                        if disabled.rowcount != 1:
                            raise TeamSubagentForkAdmissionError(
                                "agent_fork_disable_conflict",
                            )
                        subagent.status = "disabled"
                        subagent.row_version += 1
                    await db.execute(
                        """
                        INSERT INTO team_subagent_fork_lineage(
                          id,team_id,fork_agent_id,source_agent_id,source_kind,
                          source_revision_ref,source_release_id,source_owner_user_id,
                          source_display_name,policy_snapshot_json,source_snapshot_hash,
                          fork_revision_ref,dependency_gaps_json,idempotency_hash,request_hash,
                          created_by,created_at
                        ) VALUES (?,?,?,?,'team',?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            fork_commit.lineage_id,
                            subagent.team_id,
                            subagent.id,
                            fork_commit.source_agent_id,
                            fork_commit.source_revision_ref,
                            fork_commit.source_release_id,
                            fork_commit.source_owner_user_id,
                            fork_commit.source_display_name,
                            json.dumps(
                                {
                                    "policy": fork_commit.policy,
                                    "row_version": fork_commit.policy_row_version,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            fork_commit.source_snapshot_hash,
                            publication.revision_id,
                            json.dumps(
                                list(fork_commit.dependency_gaps),
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            fork_commit.idempotency_hash,
                            fork_commit.request_hash,
                            actor_user_id,
                            self._legacy_now(),
                        ),
                    )
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=subagent.team_id,
                    subagent_id=subagent.id,
                    subagent_name=subagent.name,
                    payload=audit_payload,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return subagent

    async def _admit_fork_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        fork_commit: TeamSubagentForkCommit,
    ) -> None:
        """Recheck the exact source and current policy under the create lock."""
        if fork_commit.policy not in {"recipients", "team_members"}:
            raise TeamSubagentForkAdmissionError("agent_fork_not_allowed")
        async with db.execute(
            """
            SELECT a.status,a.lifecycle_state,a.owner_user_id,a.created_by,
                   a.live_revision_id,a.live_release_id,
                   r.definition_json,r.listing_json
            FROM team_subagent a
            JOIN team_subagent_release release
              ON release.team_id=a.team_id AND release.subagent_id=a.id
             AND release.id=a.live_release_id
            JOIN team_subagent_revision r
              ON r.team_id=a.team_id AND r.subagent_id=a.id
             AND r.id=release.revision_id
            WHERE a.team_id=? AND a.id=?
            """,
            (team_id, fork_commit.source_agent_id),
        ) as cursor:
            source = await cursor.fetchone()
        if source is None:
            raise TeamSubagentForkAdmissionError("agent_fork_source_not_found")
        if (
            str(source["status"]) != "enabled"
            or str(source["lifecycle_state"] or "active") != "active"
            or str(source["live_revision_id"] or "") != fork_commit.source_revision_ref
            or str(source["live_release_id"] or "") != fork_commit.source_release_id
        ):
            raise TeamSubagentForkAdmissionError("agent_fork_source_changed")
        source_owner = str(source["owner_user_id"] or source["created_by"] or "")
        if not source_owner or source_owner != fork_commit.source_owner_user_id:
            raise TeamSubagentForkAdmissionError("agent_fork_source_changed")
        if source_owner == actor_user_id:
            raise TeamSubagentForkAdmissionError("agent_fork_use_duplicate")

        async with db.execute(
            """
            SELECT policy,row_version FROM team_subagent_fork_policy
            WHERE team_id=? AND source_agent_id=?
            """,
            (team_id, fork_commit.source_agent_id),
        ) as cursor:
            policy = await cursor.fetchone()
        if policy is None or str(policy["policy"]) != fork_commit.policy or int(policy["row_version"]) != fork_commit.policy_row_version:
            raise TeamSubagentForkAdmissionError("agent_fork_policy_changed")

        from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

        decision = (
            await TeamSubagentAccessResolver(self.db_path).resolve_many_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                subagent_id=fork_commit.source_agent_id,
            )
        ).get(fork_commit.source_agent_id)
        if decision is None or not decision.resolution.can_consume:
            raise TeamSubagentForkAdmissionError("agent_fork_access_revoked")

        try:
            definition = json.loads(str(source["definition_json"]))
            listing = json.loads(str(source["listing_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TeamSubagentForkAdmissionError("agent_fork_source_invalid") from exc
        if not isinstance(definition, dict) or not isinstance(listing, dict):
            raise TeamSubagentForkAdmissionError("agent_fork_source_invalid")
        current_hash = agent_fork_source_snapshot_hash(
            source_agent_id=fork_commit.source_agent_id,
            source_revision_ref=fork_commit.source_revision_ref,
            source_release_id=fork_commit.source_release_id,
            definition=definition,
            listing=listing,
        )
        if current_hash != fork_commit.source_snapshot_hash:
            raise TeamSubagentForkAdmissionError("agent_fork_source_changed")

    async def update_team_subagent_meta(
        self,
        team_id: str,
        subagent_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        tool_refs: list[str] | None = None,
        model: str | None = None,
        model_is_set: bool = False,
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        output_schema: dict[str, Any] | None = None,
        output_schema_is_set: bool = False,
        status: str | None = None,
        actor_user_id: str | None = None,
        audit_action: str | None = None,
        audit_payload: dict[str, Any] | None = None,
    ) -> None:
        """Preserve the legacy partial-update contract inside one transaction."""
        self._validate_audit_arguments(actor_user_id, audit_action)
        sets: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            sets.append("display_name=?")
            params.append(display_name)
        if description is not None:
            sets.append("description=?")
            params.append(description)
        if system_prompt is not None:
            sets.append("system_prompt=?")
            params.append(system_prompt)
        if tool_refs is not None:
            sets.append("tool_refs_json=?")
            params.append(json.dumps(tool_refs))
        if model_is_set:
            sets.append("model=?")
            params.append(model)
        if max_iterations is not None:
            sets.append("max_iterations=?")
            params.append(max_iterations)
        if skills is not None:
            sets.append("skills_json=?")
            params.append(json.dumps(skills))
        if output_schema_is_set:
            sets.append("output_schema_json=?")
            params.append(
                (json.dumps(output_schema, ensure_ascii=False) if output_schema is not None else None),
            )
        if status is not None:
            sets.append("status=?")
            params.append(status)

        async with open_shared_ctx(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=OFF")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT name,display_name
                    FROM team_subagent WHERE id=? AND team_id=?
                    """,
                    (subagent_id, team_id),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    await db.commit()
                    return
                definition_change_requested = any(
                    (
                        display_name is not None,
                        description is not None,
                        system_prompt is not None,
                        tool_refs is not None,
                        model_is_set,
                        max_iterations is not None,
                        skills is not None,
                        output_schema_is_set,
                    ),
                )
                if await self._release_semantics_active_conn(db):
                    definition_mode, lifecycle_state = await self._release_state_conn(
                        db,
                        team_id=team_id,
                        subagent_id=subagent_id,
                    )
                    if definition_mode == "revisioned" and definition_change_requested:
                        raise TeamSubagentRevisionWriterRequiredError()
                    if status == "enabled" and lifecycle_state == "archived":
                        raise TeamSubagentRevisionWriterRequiredError("agent_restore_required")
                if display_name is not None and await self._table_exists_conn(db, "agent_name_claim"):
                    await self._move_effective_name_conn(
                        db,
                        team_id=team_id,
                        agent_id=subagent_id,
                        old_display_name=str(existing["display_name"] or existing["name"]),
                        new_display_name=display_name or str(existing["name"]),
                        timestamp=self._legacy_now(),
                    )
                if sets:
                    sets.append("updated_at=?")
                    params.append(self._legacy_now())
                    await db.execute(
                        f"UPDATE team_subagent SET {', '.join(sets)} WHERE id=? AND team_id=?",
                        [*params, subagent_id, team_id],
                    )
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=team_id,
                    subagent_id=subagent_id,
                    subagent_name=str(existing["name"]),
                    payload=audit_payload,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def compare_and_update_team_subagent(
        self,
        team_id: str,
        subagent_id: str,
        *,
        expected_version: int,
        display_name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        tool_refs: list[str] | None = None,
        model: str | None = None,
        model_is_set: bool = False,
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        output_schema: dict[str, Any] | None = None,
        output_schema_is_set: bool = False,
        status: str | None = None,
        actor_user_id: str | None = None,
        audit_action: str | None = None,
        audit_payload: dict[str, Any] | None = None,
    ) -> TeamSubagentMutationResult | None:
        """Apply one semantic update when ``expected_version`` is current.

        The version comparison, update, and audit share one immediate
        transaction. Semantically identical retries are successful no-ops:
        they preserve ``row_version``/``updated_at`` and do not write an audit
        success event.
        """
        self._validate_audit_arguments(actor_user_id, audit_action)
        if expected_version < 1:
            raise ValueError("expected_version must be positive")

        async with open_shared_ctx(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=OFF")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT name,
                           row_version,
                           display_name,
                           description,
                           system_prompt,
                           tool_refs_json,
                           model,
                           max_iterations,
                           skills_json,
                           output_schema_json,
                           status
                    FROM team_subagent
                    WHERE id=? AND team_id=?
                    """,
                    (subagent_id, team_id),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    await db.commit()
                    return None

                definition_change_requested = any(
                    (
                        display_name is not None,
                        description is not None,
                        system_prompt is not None,
                        tool_refs is not None,
                        model_is_set,
                        max_iterations is not None,
                        skills is not None,
                        output_schema_is_set,
                    ),
                )
                if await self._release_semantics_active_conn(db):
                    definition_mode, lifecycle_state = await self._release_state_conn(
                        db,
                        team_id=team_id,
                        subagent_id=subagent_id,
                    )
                    if definition_mode == "revisioned" and definition_change_requested:
                        raise TeamSubagentRevisionWriterRequiredError()
                    if status == "enabled" and lifecycle_state == "archived":
                        raise TeamSubagentRevisionWriterRequiredError("agent_restore_required")

                current_version = int(existing["row_version"])
                if current_version != expected_version:
                    raise TeamSubagentVersionConflictError(
                        expected_version=expected_version,
                        current_version=current_version,
                    )

                sets: list[str] = []
                params: list[Any] = []

                def add_if_changed(column: str, value: Any) -> None:
                    if existing[column] != value:
                        sets.append(f"{column}=?")
                        params.append(value)

                if display_name is not None:
                    add_if_changed("display_name", display_name)
                if description is not None:
                    add_if_changed("description", description)
                if system_prompt is not None:
                    add_if_changed("system_prompt", system_prompt)
                if tool_refs is not None and self._decode_json(existing["tool_refs_json"]) != tool_refs:
                    sets.append("tool_refs_json=?")
                    params.append(json.dumps(tool_refs))
                if model_is_set:
                    add_if_changed("model", model)
                if max_iterations is not None:
                    add_if_changed("max_iterations", max_iterations)
                if skills is not None and self._decode_json(existing["skills_json"]) != skills:
                    sets.append("skills_json=?")
                    params.append(json.dumps(skills))
                if output_schema_is_set:
                    encoded_schema = json.dumps(output_schema, ensure_ascii=False) if output_schema is not None else None
                    if self._decode_json(existing["output_schema_json"]) != output_schema:
                        sets.append("output_schema_json=?")
                        params.append(encoded_schema)
                if status is not None:
                    add_if_changed("status", status)

                if not sets:
                    await db.commit()
                    return TeamSubagentMutationResult(
                        changed=False,
                        version=current_version,
                    )

                mutation_timestamp = self._legacy_now()
                if display_name is not None and existing["display_name"] != display_name and await self._table_exists_conn(db, "agent_name_claim"):
                    await self._move_effective_name_conn(
                        db,
                        team_id=team_id,
                        agent_id=subagent_id,
                        old_display_name=str(existing["display_name"] or existing["name"]),
                        new_display_name=display_name or str(existing["name"]),
                        timestamp=mutation_timestamp,
                    )
                next_version = current_version + 1
                sets.extend(("row_version=?", "updated_at=?"))
                params.extend((next_version, mutation_timestamp))
                updated = await db.execute(
                    f"""
                    UPDATE team_subagent
                    SET {", ".join(sets)}
                    WHERE id=? AND team_id=? AND row_version=?
                    """,
                    [
                        *params,
                        subagent_id,
                        team_id,
                        current_version,
                    ],
                )
                if updated.rowcount != 1:
                    raise TeamSubagentVersionConflictError(
                        expected_version=expected_version,
                        current_version=current_version,
                    )
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=team_id,
                    subagent_id=subagent_id,
                    subagent_name=str(existing["name"]),
                    payload=audit_payload,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return TeamSubagentMutationResult(changed=True, version=next_version)

    async def replace_team_subagent_audience(
        self,
        team_id: str,
        subagent_id: str,
        *,
        mode: str,
        user_ids: tuple[str, ...] | list[str] = (),
        group_ids: tuple[str, ...] | list[str] = (),
        access_store: AccessControlStore,
        actor_user_id: str,
        audit_action: str,
        expected_version: int | None = None,
    ) -> TeamSubagentAudienceMutationResult | None:
        """Atomically replace the complete Subagent allow audience.

        The current Owner check is repeated after ``BEGIN IMMEDIATE`` to close
        the authorization/commit race. User and group principals are validated
        against active canonical team membership in that same transaction.
        Semantically identical retries preserve the version, timestamps, policy
        epoch, and audit log.
        """
        self._validate_access_store(access_store)
        if expected_version is not None and expected_version < 1:
            raise ValueError("expected_version must be positive")
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"private", "custom", "teamwide"}:
            raise TeamSubagentAudienceInvalidError(
                "invalid Subagent audience mode",
            )
        if any(not principal_id.strip() for principal_id in [*user_ids, *group_ids]):
            raise TeamSubagentAudienceInvalidError(
                "Subagent audience principal ids must be non-empty",
            )
        normalized_users = tuple(
            dict.fromkeys(user_id.strip() for user_id in user_ids),
        )
        normalized_groups = tuple(
            dict.fromkeys(group_id.strip() for group_id in group_ids),
        )
        principal_count = len(normalized_users) + len(normalized_groups)
        if normalized_mode == "custom" and principal_count == 0:
            raise TeamSubagentAudienceInvalidError(
                "custom Subagent audience requires at least one principal",
            )
        if normalized_mode != "custom" and principal_count:
            raise TeamSubagentAudienceInvalidError(
                "private and teamwide Subagent audiences require an empty roster",
            )
        if principal_count > 200:
            raise TeamSubagentAudienceInvalidError(
                "Subagent audience exceeds 200 principals",
            )

        async with open_shared_ctx(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=OFF")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT sa.name,
                           sa.created_by,
                           sa.owner_user_id,
                           sa.row_version,
                           sa.audience_mode,
                           sa.visibility,
                           (
                               SELECT owner_semantics_activated_at
                               FROM team_subagent_schema_meta
                               WHERE singleton=1
                           ) AS owner_semantics_activated_at
                    FROM team_subagent AS sa
                    WHERE sa.id=? AND sa.team_id=?
                    """,
                    (subagent_id, team_id),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    await db.commit()
                    return None

                if await self._release_semantics_active_conn(db):
                    raise TeamSubagentRevisionWriterRequiredError()

                current_version = int(existing["row_version"])
                if expected_version is not None and current_version != expected_version:
                    raise TeamSubagentVersionConflictError(
                        expected_version=expected_version,
                        current_version=current_version,
                    )

                owner_user_id = existing["owner_user_id"] if existing["owner_semantics_activated_at"] is not None else existing["created_by"]
                if owner_user_id != actor_user_id or not await self._is_active_team_member_conn(
                    db,
                    team_id=team_id,
                    user_id=actor_user_id,
                ):
                    raise TeamSubagentAudienceForbiddenError(
                        "current active Owner required",
                    )

                invalid_users = await self._invalid_audience_user_ids_conn(
                    db,
                    team_id=team_id,
                    user_ids=normalized_users,
                )
                invalid_groups = await self._invalid_audience_group_ids_conn(
                    db,
                    team_id=team_id,
                    group_ids=normalized_groups,
                )
                if invalid_users or invalid_groups:
                    raise TeamSubagentAudienceInvalidError(
                        "Subagent audience contains inactive or cross-team principals",
                        invalid_user_ids=invalid_users,
                        invalid_group_ids=invalid_groups,
                    )

                async with db.execute(
                    """
                    SELECT principal_type,
                           principal_id,
                           permission,
                           expires_at
                    FROM resource_grants
                    WHERE team_id=?
                      AND resource_kind='team_subagent'
                      AND resource_id=?
                      AND effect='allow'
                      AND principal_type IN ('team', 'user', 'group')
                      AND revoked_at IS NULL
                    ORDER BY principal_type, principal_id, permission
                    """,
                    (team_id, subagent_id),
                ) as cursor:
                    existing_grants = await cursor.fetchall()

                desired_principals: tuple[tuple[str, str], ...]
                if normalized_mode == "teamwide":
                    desired_principals = (("team", team_id),)
                elif normalized_mode == "custom":
                    desired_principals = tuple(("user", user_id) for user_id in normalized_users) + tuple(
                        ("group", group_id) for group_id in normalized_groups
                    )
                else:
                    desired_principals = ()

                desired_visibility = "team_wide" if normalized_mode == "teamwide" else "creator_only"
                current_principals = tuple(
                    (
                        str(grant["principal_type"]),
                        str(grant["principal_id"]),
                    )
                    for grant in existing_grants
                )
                grants_are_canonical = all(grant["permission"] == "use" and grant["expires_at"] is None for grant in existing_grants)
                if (
                    str(existing["audience_mode"]) == normalized_mode
                    and str(existing["visibility"]) == desired_visibility
                    and current_principals == tuple(sorted(desired_principals))
                    and grants_are_canonical
                ):
                    await db.commit()
                    return TeamSubagentAudienceMutationResult(
                        changed=False,
                        version=current_version,
                        mode=normalized_mode,
                        user_ids=normalized_users,
                        group_ids=normalized_groups,
                    )

                old_mode = self._effective_legacy_audience_mode(
                    visibility=str(existing["visibility"]),
                    grants=existing_grants,
                )
                timestamp = self._legacy_now()
                await access_store._replace_team_subagent_audience_conn(
                    db,
                    team_id=team_id,
                    resource_id=subagent_id,
                    principals=tuple(sorted(desired_principals)),
                    created_by=actor_user_id,
                    timestamp=timestamp,
                    subagent_transaction_token=self._transaction_token,
                )
                next_version = current_version + 1
                updated = await db.execute(
                    """
                    UPDATE team_subagent
                    SET audience_mode=?,
                        visibility=?,
                        row_version=?,
                        updated_at=?
                    WHERE id=? AND team_id=? AND row_version=?
                    """,
                    (
                        normalized_mode,
                        desired_visibility,
                        next_version,
                        timestamp,
                        subagent_id,
                        team_id,
                        current_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise TeamSubagentVersionConflictError(
                        expected_version=expected_version,
                        current_version=current_version,
                    )
                await access_store._bump_policy_epoch_conn(db, team_id)
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=team_id,
                    subagent_id=subagent_id,
                    subagent_name=str(existing["name"]),
                    payload={
                        "before": {
                            "mode": old_mode,
                            "user_count": sum(grant["principal_type"] == "user" for grant in existing_grants),
                            "group_count": sum(grant["principal_type"] == "group" for grant in existing_grants),
                        },
                        "after": {
                            "mode": normalized_mode,
                            "user_count": len(normalized_users),
                            "group_count": len(normalized_groups),
                        },
                        "version": next_version,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return TeamSubagentAudienceMutationResult(
            changed=True,
            version=next_version,
            mode=normalized_mode,
            user_ids=normalized_users,
            group_ids=normalized_groups,
        )

    async def add_team_subagent_audience_user(
        self,
        team_id: str,
        subagent_id: str,
        *,
        user_id: str,
        access_store: AccessControlStore,
        actor_user_id: str,
        audit_action: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> TeamSubagentAudienceMutationResult | None:
        """Append one member to the Owner-managed usage audience (access-request approval).

        This is deliberately not the generic grant path (``reject_obsolete_team_subagent_write``)
        and not a roster replacement: existing user/group grants keep their rows and only one
        canonical ``use`` allow is inserted. ``private`` becomes ``custom`` with just this user;
        ``teamwide`` or an existing user allow is a no-op. The same transaction re-checks that
        the actor is the active current Owner and the user an active team member.

        After the release cutover the Audience is part of the immutable release snapshot
        (shared release, listing); appending here would silently diverge from it, so this
        raises ``TeamSubagentRevisionWriterRequiredError`` and leaves every release and grant
        untouched — the Owner must use the release sharing flow instead.
        """
        self._validate_access_store(access_store)
        member = user_id.strip()
        if not member:
            raise TeamSubagentAudienceInvalidError("Subagent audience principal ids must be non-empty")
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=OFF")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT sa.name,
                           sa.created_by,
                           sa.owner_user_id,
                           sa.row_version,
                           sa.audience_mode,
                           sa.visibility,
                           (
                               SELECT owner_semantics_activated_at
                               FROM team_subagent_schema_meta
                               WHERE singleton=1
                           ) AS owner_semantics_activated_at,
                           (
                               SELECT audience_semantics_activated_at
                               FROM team_subagent_schema_meta
                               WHERE singleton=1
                           ) AS audience_semantics_activated_at
                    FROM team_subagent AS sa
                    WHERE sa.id=? AND sa.team_id=?
                    """,
                    (subagent_id, team_id),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    await db.commit()
                    return None

                if await self._release_semantics_active_conn(db):
                    raise TeamSubagentRevisionWriterRequiredError()

                owner_user_id = existing["owner_user_id"] if existing["owner_semantics_activated_at"] is not None else existing["created_by"]
                if owner_user_id != actor_user_id or not await self._is_active_team_member_conn(
                    db,
                    team_id=team_id,
                    user_id=actor_user_id,
                ):
                    raise TeamSubagentAudienceForbiddenError("current active Owner required")
                if await self._invalid_audience_user_ids_conn(db, team_id=team_id, user_ids=(member,)):
                    raise TeamSubagentAudienceInvalidError(
                        "Subagent audience contains inactive or cross-team principals",
                        invalid_user_ids=(member,),
                    )

                async with db.execute(
                    """
                    SELECT principal_type, principal_id
                    FROM resource_grants
                    WHERE team_id=?
                      AND resource_kind='team_subagent'
                      AND resource_id=?
                      AND effect='allow'
                      AND principal_type IN ('team', 'user', 'group')
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (team_id, subagent_id, self._legacy_now()),
                ) as cursor:
                    existing_grants = await cursor.fetchall()
                current_version = int(existing["row_version"])
                # Same projection as the access resolver: the stored mode only counts once the
                # audience semantics are activated; before that, visibility + grants decide.
                current_mode = (
                    str(existing["audience_mode"])
                    if existing["audience_semantics_activated_at"] is not None
                    else self._effective_legacy_audience_mode(visibility=str(existing["visibility"]), grants=existing_grants)
                )
                user_ids = tuple(str(g["principal_id"]) for g in existing_grants if g["principal_type"] == "user")
                group_ids = tuple(str(g["principal_id"]) for g in existing_grants if g["principal_type"] == "group")
                if current_mode == "teamwide" or member in user_ids:
                    await db.commit()
                    return TeamSubagentAudienceMutationResult(
                        changed=False,
                        version=current_version,
                        mode=current_mode,
                        user_ids=user_ids,
                        group_ids=group_ids,
                    )

                timestamp = self._legacy_now()
                await access_store._append_team_subagent_audience_user_conn(
                    db,
                    team_id=team_id,
                    resource_id=subagent_id,
                    user_id=member,
                    created_by=actor_user_id,
                    timestamp=timestamp,
                    subagent_transaction_token=self._transaction_token,
                )
                next_version = current_version + 1
                updated = await db.execute(
                    """
                    UPDATE team_subagent
                    SET audience_mode='custom',
                        visibility='creator_only',
                        row_version=?,
                        updated_at=?
                    WHERE id=? AND team_id=? AND row_version=?
                    """,
                    (next_version, timestamp, subagent_id, team_id, current_version),
                )
                if updated.rowcount != 1:
                    raise TeamSubagentVersionConflictError(
                        expected_version=None,
                        current_version=current_version,
                    )
                await access_store._bump_policy_epoch_conn(db, team_id)
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=team_id,
                    subagent_id=subagent_id,
                    subagent_name=str(existing["name"]),
                    payload={
                        **(audit_payload or {}),
                        "before": {"mode": current_mode, "user_count": len(user_ids), "group_count": len(group_ids)},
                        "after": {"mode": "custom", "user_count": len(user_ids) + 1, "group_count": len(group_ids)},
                        "version": next_version,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return TeamSubagentAudienceMutationResult(
            changed=True,
            version=next_version,
            mode="custom",
            user_ids=(*user_ids, member),
            group_ids=group_ids,
        )

    async def delete_team_subagent(
        self,
        team_id: str,
        subagent_id: str,
        *,
        expected_version: int | None = None,
        access_store: AccessControlStore | None = None,
        discover_access_store: bool = False,
        actor_user_id: str | None = None,
        audit_action: str | None = None,
        audit_payload: dict[str, Any] | None = None,
    ) -> bool:
        """Delete a legacy row, its grants, and its route audit atomically."""
        self._validate_audit_arguments(actor_user_id, audit_action)
        self._validate_access_store(access_store)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=OFF")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                selected_columns = "name, display_name, row_version" if expected_version is not None else "name, display_name"
                async with db.execute(
                    f"""
                    SELECT {selected_columns}
                    FROM team_subagent
                    WHERE id=? AND team_id=?
                    """,
                    (subagent_id, team_id),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    await db.commit()
                    return False
                if await self._release_semantics_active_conn(db):
                    raise TeamSubagentDeleteDependencyError("agent_archive_required")
                if expected_version is not None:
                    current_version = int(existing["row_version"])
                    if current_version != expected_version:
                        raise TeamSubagentVersionConflictError(
                            expected_version=expected_version,
                            current_version=current_version,
                        )
                if await self._table_exists_conn(db, "scheduled_jobs") and await self._column_exists_conn(
                    db,
                    table_name="scheduled_jobs",
                    column_name="recipe_ref",
                ):
                    async with db.execute(
                        """
                        SELECT 1 FROM scheduled_jobs
                        WHERE json_valid(recipe_ref)
                          AND EXISTS(
                            SELECT 1 FROM workspaces AS schedule_workspace
                            WHERE schedule_workspace.id=scheduled_jobs.workspace_id
                              AND schedule_workspace.team_id=?
                          )
                          AND json_extract(recipe_ref,'$.form')='team_subagent'
                          AND json_extract(recipe_ref,'$.id_or_name') IN (?,?)
                          AND status IN ('active','paused')
                        LIMIT 1
                        """,
                        (team_id, subagent_id, str(existing["name"])),
                    ) as cursor:
                        if await cursor.fetchone() is not None:
                            raise TeamSubagentDeleteDependencyError(
                                "agent_delete_schedule_conflict",
                            )
                effective_access_store = access_store
                if effective_access_store is None and discover_access_store and await self._table_exists_conn(db, "resource_grants"):
                    from bowhead_core.access_control import AccessControlStore

                    effective_access_store = AccessControlStore(self.db_path)
                if effective_access_store is not None:
                    revoked = await effective_access_store._revoke_grants_for_resource_conn(
                        db,
                        team_id,
                        TEAM_SUBAGENT_RESOURCE_KIND,
                        subagent_id,
                        effects=("allow", "deny"),
                        subagent_transaction_token=self._transaction_token,
                    )
                    if revoked:
                        await effective_access_store._bump_policy_epoch_conn(
                            db,
                            team_id,
                        )
                mutation_timestamp = self._legacy_now()
                if await self._table_exists_conn(db, "agent_name_claim"):
                    await self._release_effective_name_conn(
                        db,
                        team_id=team_id,
                        agent_id=subagent_id,
                        display_name=str(existing["display_name"] or existing["name"]),
                        timestamp=mutation_timestamp,
                    )
                if await self._table_exists_conn(db, "agent_assignment"):
                    await db.execute(
                        """
                        UPDATE agent_assignment
                        SET personal_state='removed',access_state='revoked',removed_at=?,
                            personal_state_changed_at=?,access_state_changed_at=?,
                            row_version=row_version+1,updated_at=?
                        WHERE team_id=? AND agent_id=?
                          AND (personal_state<>'removed' OR access_state<>'revoked')
                        """,
                        (
                            mutation_timestamp,
                            mutation_timestamp,
                            mutation_timestamp,
                            mutation_timestamp,
                            team_id,
                            subagent_id,
                        ),
                    )
                    await db.execute(
                        "DELETE FROM agent_catalog_projection WHERE team_id=? AND agent_id=?",
                        (team_id, subagent_id),
                    )
                if await self._table_exists_conn(db, "agent_default_policy"):
                    await db.execute(
                        """
                        UPDATE agent_default_policy
                        SET enabled=0,policy_version=policy_version+1,
                            row_version=row_version+1,
                            changed_by=COALESCE(?,changed_by),updated_at=?
                        WHERE team_id=? AND agent_id=? AND enabled=1
                        """,
                        (actor_user_id, mutation_timestamp, team_id, subagent_id),
                    )
                await db.execute(
                    """
                    DELETE FROM team_subagent
                    WHERE id=? AND team_id=?
                    """,
                    (subagent_id, team_id),
                )
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=team_id,
                    subagent_id=subagent_id,
                    subagent_name=str(existing["name"]),
                    payload=audit_payload,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return True

    async def _is_active_team_member_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
    ) -> bool:
        async with db.execute(
            """
            SELECT 1
            FROM users AS u
            JOIN team_members AS tm
              ON tm.user_id=u.id
             AND tm.team_id=?
             AND tm.status='active'
            WHERE u.id=? AND u.status='active'
            """,
            (team_id, user_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _release_semantics_active_conn(self, db: aiosqlite.Connection) -> bool:
        # Single implementation lives at module scope so admission, the schedule
        # binding and the release read stores consult the same switch as writers.
        return await release_semantics_active_conn(db)

    async def _release_state_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        subagent_id: str,
    ) -> tuple[str, str]:
        async with db.execute(
            """
            SELECT definition_mode,lifecycle_state FROM team_subagent
            WHERE team_id=? AND id=?
            """,
            (team_id, subagent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise TeamSubagentRevisionWriterRequiredError("agent_not_found")
        return str(row[0]), str(row[1])

    async def _invalid_audience_user_ids_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not user_ids:
            return ()
        placeholders = ",".join("?" * len(user_ids))
        async with db.execute(
            f"""
            SELECT u.id
            FROM users AS u
            JOIN team_members AS tm
              ON tm.user_id=u.id
             AND tm.team_id=?
             AND tm.status='active'
            WHERE u.id IN ({placeholders})
              AND u.status='active'
            """,  # noqa: S608 -- placeholder count is derived from a bounded tuple
            [team_id, *user_ids],
        ) as cursor:
            valid = {str(row["id"]) for row in await cursor.fetchall()}
        return tuple(user_id for user_id in user_ids if user_id not in valid)

    async def _invalid_audience_group_ids_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        group_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not group_ids:
            return ()
        placeholders = ",".join("?" * len(group_ids))
        async with db.execute(
            f"""
            SELECT id
            FROM groups
            WHERE team_id=?
              AND id IN ({placeholders})
              AND status='active'
            """,  # noqa: S608 -- placeholder count is derived from a bounded tuple
            [team_id, *group_ids],
        ) as cursor:
            valid = {str(row["id"]) for row in await cursor.fetchall()}
        return tuple(group_id for group_id in group_ids if group_id not in valid)

    @staticmethod
    def _effective_legacy_audience_mode(
        *,
        visibility: str,
        grants: list[aiosqlite.Row],
    ) -> str:
        if visibility == "team_wide":
            return "teamwide"
        if any(grant["principal_type"] in {"user", "group"} for grant in grants):
            return "custom"
        return "private"

    @staticmethod
    def _decode_json(value: Any) -> Any:
        if value is None:
            return None
        try:
            return json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return value

    async def get_schema_metadata(self) -> TeamSubagentSchemaMetadata:
        """Return the schema state without changing compatibility semantics."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT schema_version,
                       minimum_reader_version,
                       minimum_writer_version,
                       owner_semantics_activated_at,
                       lifecycle_semantics_activated_at,
                       audience_semantics_activated_at,
                       release_semantics_activated_at,
                       updated_at
                FROM team_subagent_schema_meta
                WHERE singleton = 1
                """,
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("team_subagent_schema_meta_missing")
        return TeamSubagentSchemaMetadata(
            schema_version=int(row["schema_version"]),
            minimum_reader_version=str(row["minimum_reader_version"]),
            minimum_writer_version=str(row["minimum_writer_version"]),
            owner_semantics_activated_at=row["owner_semantics_activated_at"],
            lifecycle_semantics_activated_at=row["lifecycle_semantics_activated_at"],
            audience_semantics_activated_at=row["audience_semantics_activated_at"],
            release_semantics_activated_at=row["release_semantics_activated_at"],
            updated_at=str(row["updated_at"]),
        )

    async def _write_audit_conn(
        self,
        db: aiosqlite.Connection,
        *,
        actor_user_id: str | None,
        audit_action: str | None,
        team_id: str,
        subagent_id: str,
        subagent_name: str,
        payload: dict[str, Any] | None,
    ) -> None:
        if actor_user_id is None:
            return
        from bowhead_core.user_store import UserStore

        await UserStore(self.db_path).write_admin_audit_log(
            actor_user_id=actor_user_id,
            team_id=team_id,
            action=str(audit_action),
            target_kind=TEAM_SUBAGENT_RESOURCE_KIND,
            target_id=subagent_id,
            target_team_id=team_id,
            scope="team",
            payload={"name": subagent_name, **(payload or {})},
            conn=db,
        )

    async def claim_effective_name_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        display_name: str,
        timestamp: str,
    ) -> None:
        """Claim a live Agent name inside a caller-owned aggregate transaction."""
        await self._claim_effective_name_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            display_name=display_name,
            timestamp=timestamp,
        )

    async def _claim_effective_name_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        display_name: str,
        timestamp: str,
    ) -> None:
        key = _effective_name_key(display_name)
        if key in await self._system_effective_name_keys_conn(db, team_id=team_id):
            raise TeamSubagentNameConflictError(
                "display name is unavailable in this team",
                code="display_name_unavailable",
            )
        async with db.execute(
            "SELECT locale,holder_kind,holder_id,agent_id FROM agent_name_claim WHERE team_id=? AND normalized_key=? ORDER BY locale",
            (team_id, key),
        ) as cursor:
            existing = await cursor.fetchall()
        if existing:
            if any(str(row["holder_kind"]) != "agent" or str(row["agent_id"] or "") != agent_id for row in existing):
                raise TeamSubagentNameConflictError(
                    "display name is unavailable in this team",
                    code="display_name_unavailable",
                )
            if any(str(row["locale"]) == "*" for row in existing):
                return
        await db.execute(
            """
            INSERT INTO agent_name_claim(
              team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
            ) VALUES (?,'*',?,'agent',?,?,?)
            """,
            (team_id, key, agent_id, agent_id, timestamp),
        )

    async def _system_effective_name_keys_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
    ) -> set[str]:
        from bowhead_core.system_agent_marketplace import (
            system_agent_default_display_name,
            system_reserved_display_names,
        )

        try:
            from bowhead_core.agents.registry import AgentRegistry

            names = list(AgentRegistry.bootstrap().list_all())
        except Exception:
            names = []
        async with db.execute(
            """
            SELECT name,display_name FROM team_builtin_subagent_override
            WHERE team_id=? AND display_name IS NOT NULL
            """,
            (team_id,),
        ) as cursor:
            overrides = {str(row["name"]): str(row["display_name"]) for row in await cursor.fetchall() if str(row["display_name"] or "").strip()}
        reserved = set(system_reserved_display_names())
        reserved.update(names)
        reserved.update(name.replace("-", " ").title() for name in names)
        reserved.update(system_agent_default_display_name(name) for name in names)
        reserved.update(value for value in overrides.values() if value.strip())
        return {_effective_name_key(value) for value in reserved}

    async def _move_effective_name_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        old_display_name: str,
        new_display_name: str,
        timestamp: str,
    ) -> None:
        old_key = _effective_name_key(old_display_name, allow_legacy_controls=True)
        new_key = _effective_name_key(new_display_name)
        if old_key == new_key:
            return
        await self._claim_effective_name_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            display_name=new_display_name,
            timestamp=timestamp,
        )
        await self._release_effective_name_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            display_name=old_display_name,
            timestamp=timestamp,
        )

    async def release_effective_name_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        display_name: str,
        timestamp: str,
    ) -> None:
        """Release a live Agent name inside a caller-owned aggregate transaction."""
        await self._release_effective_name_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            display_name=display_name,
            timestamp=timestamp,
        )

    async def _release_effective_name_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        display_name: str,
        timestamp: str,
    ) -> None:
        key = _effective_name_key(display_name, allow_legacy_controls=True)
        async with db.execute(
            "SELECT holder_kind,agent_id FROM agent_name_claim WHERE team_id=? AND locale='*' AND normalized_key=?",
            (team_id, key),
        ) as cursor:
            claim = await cursor.fetchone()
        if claim is None:
            return
        holder_kind = str(claim["holder_kind"])
        if holder_kind == "agent" and str(claim["agent_id"] or "") == agent_id:
            await db.execute(
                "DELETE FROM agent_name_claim WHERE team_id=? AND locale='*' AND normalized_key=?",
                (team_id, key),
            )
            return
        await db.execute(
            """
            UPDATE agent_name_conflict_group SET resolved_at=?
            WHERE team_id=? AND normalized_key=? AND agent_id=? AND resolved_at IS NULL
            """,
            (timestamp, team_id, key, agent_id),
        )
        if holder_kind != "legacy_conflict":
            return
        async with db.execute(
            """
            SELECT agent_id FROM agent_name_conflict_group
            WHERE team_id=? AND normalized_key=? AND resolved_at IS NULL
            ORDER BY agent_id
            """,
            (team_id, key),
        ) as cursor:
            remaining = [str(row[0]) for row in await cursor.fetchall()]
        if len(remaining) > 1:
            return
        await db.execute(
            "DELETE FROM agent_name_claim WHERE team_id=? AND locale='*' AND normalized_key=? AND holder_kind='legacy_conflict'",
            (team_id, key),
        )
        if remaining:
            await db.execute(
                """
                INSERT INTO agent_name_claim(
                  team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                ) VALUES (?,'*',?,'agent',?,?,?)
                """,
                (team_id, key, remaining[0], remaining[0], timestamp),
            )

    async def _table_exists_conn(
        self,
        db: aiosqlite.Connection,
        table_name: str,
    ) -> bool:
        async with db.execute(
            """
            SELECT 1
            FROM sqlite_schema
            WHERE type='table' AND name=?
            """,
            (table_name,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _column_exists_conn(
        self,
        db: aiosqlite.Connection,
        *,
        table_name: str,
        column_name: str,
    ) -> bool:
        async with db.execute(
            f"PRAGMA table_info({table_name})",  # noqa: S608 -- internal fixed table name
        ) as cursor:
            rows = await cursor.fetchall()
        return any(str(row[1]) == column_name for row in rows)

    def _validate_audit_arguments(
        self,
        actor_user_id: str | None,
        audit_action: str | None,
    ) -> None:
        if (actor_user_id is None) != (audit_action is None):
            raise ValueError(
                "actor_user_id and audit_action must be provided together",
            )

    def _validate_access_store(
        self,
        access_store: AccessControlStore | None,
    ) -> None:
        if access_store is not None and access_store.db_path != self.db_path:
            raise ValueError(
                "team_subagent aggregate transaction requires one shared database",
            )

    def _legacy_now(self) -> str:
        value = self._clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("team_subagent_clock_must_return_aware_datetime")
        return value.astimezone(UTC).isoformat()

    async def _require_legacy_table(self, db: aiosqlite.Connection) -> None:
        async with db.execute(
            """
            SELECT 1
            FROM sqlite_schema
            WHERE type = 'table' AND name = 'team_subagent'
            """,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "team_subagent_base_schema_missing: initialize UserStore first",
            )

    async def _add_missing_team_subagent_columns(
        self,
        db: aiosqlite.Connection,
    ) -> None:
        existing = await self._table_columns(db, "team_subagent")
        for spec in _TEAM_SUBAGENT_COLUMNS:
            row = existing.get(spec.name)
            if row is None:
                await db.execute(
                    f"ALTER TABLE team_subagent ADD COLUMN {spec.name} {spec.definition}",
                )
            else:
                self._validate_column("team_subagent", row, spec)
        await self._validate_table_shape(
            db,
            "team_subagent",
            _TEAM_SUBAGENT_COLUMNS,
        )

    async def _initialize_metadata(self, db: aiosqlite.Connection) -> None:
        now = _rfc3339_utc(self._clock.now())
        await db.execute(
            """
            INSERT OR IGNORE INTO team_subagent_schema_meta (
              singleton,
              schema_version,
              minimum_reader_version,
              minimum_writer_version,
              owner_semantics_activated_at,
              lifecycle_semantics_activated_at,
              audience_semantics_activated_at,
              updated_at
            ) VALUES (1, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                RELEASE_A_SCHEMA_VERSION,
                RELEASE_A_COMPATIBILITY_VERSION,
                RELEASE_A_COMPATIBILITY_VERSION,
                now,
            ),
        )
        await db.execute(
            """
            UPDATE team_subagent_schema_meta
            SET schema_version = ?,
                updated_at = ?
            WHERE singleton = 1
              AND schema_version < ?
            """,
            (
                RELEASE_A_SCHEMA_VERSION,
                now,
                RELEASE_A_SCHEMA_VERSION,
            ),
        )

    async def _validate_table_shape(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        specs: tuple[_ColumnSpec, ...],
    ) -> None:
        columns = await self._table_columns(db, table_name)
        for spec in specs:
            row = columns.get(spec.name)
            if row is None:
                raise RuntimeError(
                    f"{table_name}_schema_incompatible: missing column {spec.name}",
                )
            self._validate_column(table_name, row, spec)

    async def _table_columns(
        self,
        db: aiosqlite.Connection,
        table_name: str,
    ) -> dict[str, tuple[object, ...]]:
        async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
            rows = await cursor.fetchall()
        return {str(row[1]): tuple(row) for row in rows}

    def _validate_column(
        self,
        table_name: str,
        row: tuple[object, ...],
        spec: _ColumnSpec,
    ) -> None:
        actual_type = str(row[2]).upper()
        actual_not_null = bool(row[3])
        actual_default = _normalize_default(row[4])
        actual_primary_key = bool(row[5])
        if (
            actual_type != spec.data_type
            or actual_not_null != spec.not_null
            or actual_default != spec.default
            or actual_primary_key != spec.primary_key
        ):
            raise RuntimeError(
                f"{table_name}_schema_incompatible: column {spec.name}",
            )


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        return normalized[1:-1]
    return normalized


def _rfc3339_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("team_subagent_clock_must_return_aware_datetime")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
