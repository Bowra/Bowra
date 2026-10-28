"""Personal Agent assignments for Marketplace M1.

An assignment is a user preference and runtime narrowing gate.  It never grants
access: catalog and runtime callers must still evaluate the canonical Team
Subagent access resolver on every request.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.audit_actions import AuditAction
from bowhead_core.user_store import UserStore

PersonalState = Literal["active", "disabled", "removed"]
AssignmentRuntimeSchemaState = Literal["pre_0025", "pre_cutover", "cutover", "unavailable"]
AssignmentSource = Literal[
    "user_hired",
    "migration_existing_access",
    "team_default",
    "former_team_default",
]

AGENT_MARKETPLACE_SCHEMA_VERSION = 2
AGENT_MARKETPLACE_READER_VERSION = "0002"
AGENT_MARKETPLACE_WRITER_VERSION = "0002"
_BACKFILL_LEASE_SECONDS = 300

# The one definition of "this assignment lets the Agent run for this user". Execution admission
# (``agent_execution_binding._require_assignment``), the Lead roster and the effective-authorization
# explainer must all agree on it; ``test_effective_authorization`` drives the real admission
# transaction to keep them aligned. A hire that lost access (audience narrowed) stays
# ``personal_state='active'`` with ``access_state='revoked'`` until it is re-hired.
ADMISSIBLE_ASSIGNMENT_SQL = "personal_state='active' AND access_state='granted'"

_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "agent_assignment": frozenset(
        {
            "id",
            "team_id",
            "user_id",
            "agent_id",
            "agent_source_kind",
            "personal_state",
            "access_state",
            "source",
            "source_policy_version",
            "row_version",
            "hired_at",
            "current_cycle_started_at",
            "personal_state_changed_at",
            "access_state_changed_at",
            "removed_at",
            "created_at",
            "updated_at",
        },
    ),
    "agent_default_policy": frozenset(
        {
            "team_id",
            "agent_id",
            "agent_source_kind",
            "enabled",
            "policy_version",
            "row_version",
            "changed_by",
            "created_at",
            "updated_at",
        },
    ),
    "agent_default_opt_out": frozenset(
        {
            "team_id",
            "user_id",
            "agent_id",
            "policy_version_at_opt_out",
            "opted_out_at",
            "cleared_at",
            "row_version",
        },
    ),
    "agent_catalog_projection": frozenset(
        {
            "team_id",
            "agent_id",
            "source_kind",
            "source_version",
            "compiler_version",
            "canonical_name",
            "canonical_summary",
            "owner_label",
            "capability_summary_json",
            "facet_json",
            "risk_summary_json",
            "safety_summary_json",
            "dependency_json",
            "search_text",
            "projection_hash",
            "built_at",
        },
    ),
    "agent_name_claim": frozenset(
        {
            "team_id",
            "locale",
            "normalized_key",
            "holder_kind",
            "holder_id",
            "agent_id",
            "claimed_at",
        },
    ),
    "agent_name_conflict_group": frozenset(
        {"team_id", "normalized_key", "agent_id", "detected_at", "resolved_at"},
    ),
    "agent_marketplace_schema_meta": frozenset(
        {
            "singleton",
            "schema_version",
            "assignment_cutover_complete_at",
            "assignment_backfill_expected",
            "assignment_backfill_inserted",
            "assignment_backfill_digest",
            "minimum_reader_version",
            "minimum_writer_version",
            "updated_at",
        },
    ),
    "agent_assignment_backfill_run": frozenset(
        {
            "id",
            "status",
            "cursor_team_id",
            "cursor_user_id",
            "cursor_agent_id",
            "snapshot_at",
            "snapshot_completed_at",
            "stage_cursor_team_id",
            "stage_cursor_user_id",
            "expected_digest",
            "owner_token",
            "lease_expires_at",
            "expected_count",
            "inserted_count",
            "conflict_count",
            "error_count",
            "source_build",
            "started_at",
            "updated_at",
            "finished_at",
        },
    ),
    "agent_assignment_backfill_expected": frozenset(
        {"run_id", "team_id", "user_id", "agent_id"},
    ),
}

_REQUIRED_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "agent_assignment": ("id",),
    "agent_default_policy": ("team_id", "agent_id"),
    "agent_default_opt_out": ("team_id", "user_id", "agent_id"),
    "agent_catalog_projection": ("team_id", "agent_id"),
    "agent_name_claim": ("team_id", "locale", "normalized_key"),
    "agent_name_conflict_group": ("team_id", "normalized_key", "agent_id"),
    "agent_marketplace_schema_meta": ("singleton",),
    "agent_assignment_backfill_run": ("id",),
    "agent_assignment_backfill_expected": ("run_id", "team_id", "user_id", "agent_id"),
}

_REQUIRED_UNIQUE_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "agent_assignment": (("team_id", "user_id", "agent_id"),),
}


class AgentAssignmentError(RuntimeError):
    """Base class for assignment command failures."""


class AgentAssignmentUnavailableError(AgentAssignmentError):
    """The Marketplace schema has not been migrated yet."""


class AgentAssignmentCutoverCompleteError(AgentAssignmentError):
    """The one-time legacy assignment cutover has already completed."""


class AgentAssignmentBackfillLeaseError(AgentAssignmentError):
    """Another process owns the active backfill lease."""


class _LegacyV1RepairAlreadyCompleted(RuntimeError):
    """A concurrent ensure committed the v2 receipt under the write lock."""


class AgentAssignmentAccessError(AgentAssignmentError):
    """The caller no longer has live access to the Agent."""


class AgentAssignmentNotFoundError(AgentAssignmentError):
    """No mutable assignment exists for this user and Agent."""


class AgentAssignmentVersionConflictError(AgentAssignmentError):
    """The submitted row_version is stale."""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"assignment version conflict; current={current_version}")
        self.current_version = current_version


class AgentAssignmentScheduleConflictError(AgentAssignmentError):
    """Unhire needs schedule handling or has an in-flight schedule run."""

    def __init__(self, reason: str, impact: AgentUnhireImpact) -> None:
        super().__init__(reason)
        self.reason = reason
        self.impact = impact


@dataclass(frozen=True, slots=True)
class AgentAssignment:
    id: str
    team_id: str
    user_id: str
    agent_id: str
    agent_source_kind: str
    personal_state: PersonalState
    access_state: str
    source: AssignmentSource
    source_policy_version: int | None
    row_version: int
    hired_at: str
    current_cycle_started_at: str
    personal_state_changed_at: str
    access_state_changed_at: str
    removed_at: str | None
    updated_at: str

    def effective_status(self, *, live_access: bool) -> str:
        if self.personal_state == "removed":
            return "removed"
        # Either signal blocks: the request-time access decision, or the stored
        # access_state. Archiving an Agent writes access_state='revoked' and
        # restoring it re-grants in the same transaction, so the stored value is
        # authoritative too — execution admission refuses on it
        # (ADMISSIBLE_ASSIGNMENT_SQL), and the market must not show such a hire as
        # "active" with no way back. access_revoked offers re-hire (PRD §8.5).
        if not live_access or self.access_state == "revoked":
            return "access_revoked"
        if self.personal_state == "disabled":
            return "personal_disabled"
        return "active"


@dataclass(frozen=True, slots=True)
class AgentScheduleImpactItem:
    id: str
    name: str
    status: str
    updated_at: str
    has_in_flight_run: bool


@dataclass(frozen=True, slots=True)
class AgentUnhireImpact:
    assignment_id: str
    assignment_row_version: int
    jobs: tuple[AgentScheduleImpactItem, ...]
    digest: str

    @property
    def active_count(self) -> int:
        return sum(item.status == "active" for item in self.jobs)

    @property
    def paused_count(self) -> int:
        return sum(item.status == "paused" for item in self.jobs)

    @property
    def running_count(self) -> int:
        return sum(item.has_in_flight_run for item in self.jobs)


@dataclass(frozen=True, slots=True)
class AgentAssignmentMutation:
    assignment: AgentAssignment
    paused_schedule_ids: tuple[str, ...] = ()
    expired_schedule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentAssignmentBackfillResult:
    expected: int
    inserted: int
    conflict: int
    digest: str
    cutover_completed_at: str | None


@dataclass(frozen=True, slots=True)
class AgentMarketplaceDeploymentAdmission:
    schema_version: int | None
    candidate_reader_version: str
    candidate_writer_version: str
    minimum_reader_version: str | None
    minimum_writer_version: str | None
    candidate_compatible: bool | None
    migration_pending: bool
    cutover_complete: bool | None
    ready: bool


@dataclass(frozen=True, slots=True)
class _BackfillLease:
    run_id: str
    owner_token: str
    snapshot_at: str
    snapshot_completed: bool
    legacy_v1_repair: bool = False


@dataclass(frozen=True, slots=True)
class AgentDefaultPolicy:
    team_id: str
    agent_id: str
    enabled: bool
    policy_version: int
    row_version: int
    changed_by: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AgentDefaultPreview:
    agent_id: str
    action: Literal["set", "remove"]
    active_member_count: int
    assignment_count: int
    opted_out_count: int
    affected_count: int
    policy: AgentDefaultPolicy | None


def _default_policy_from_row(row: aiosqlite.Row) -> AgentDefaultPolicy:
    return AgentDefaultPolicy(
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        enabled=bool(row["enabled"]),
        policy_version=int(row["policy_version"]),
        row_version=int(row["row_version"]),
        changed_by=str(row["changed_by"]),
        updated_at=str(row["updated_at"]),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _monotonic_now(*floor_values: str | None) -> str:
    value = datetime.now(UTC)
    for floor_value in floor_values:
        if floor_value:
            value = max(value, _parse_timestamp(floor_value))
    return value.isoformat()


def _lease_deadline(timestamp: str) -> str:
    return (_parse_timestamp(timestamp) + timedelta(seconds=_BACKFILL_LEASE_SECONDS)).isoformat()


def _update_backfill_digest(hasher: Any, pair: tuple[str, str, str]) -> None:
    hasher.update(json.dumps(pair, separators=(",", ":"), ensure_ascii=False).encode())
    hasher.update(b"\n")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def _assignment_from_row(row: aiosqlite.Row) -> AgentAssignment:
    return AgentAssignment(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        user_id=str(row["user_id"]),
        agent_id=str(row["agent_id"]),
        agent_source_kind=str(row["agent_source_kind"]),
        personal_state=str(row["personal_state"]),  # type: ignore[arg-type]
        access_state=str(row["access_state"]),
        source=str(row["source"]),  # type: ignore[arg-type]
        source_policy_version=(int(row["source_policy_version"]) if row["source_policy_version"] is not None else None),
        row_version=int(row["row_version"]),
        hired_at=str(row["hired_at"]),
        current_cycle_started_at=str(row["current_cycle_started_at"]),
        personal_state_changed_at=str(row["personal_state_changed_at"]),
        access_state_changed_at=str(row["access_state_changed_at"]),
        removed_at=(str(row["removed_at"]) if row["removed_at"] is not None else None),
        updated_at=str(row["updated_at"]),
    )


def _is_pre_marketplace_revision(value: str) -> bool:
    """Recognize only an explicit Alembic revision older than Marketplace 0025."""
    prefix, separator, _suffix = value.partition("_")
    return bool(separator and prefix.isdigit() and int(prefix) < 25)


class AgentAssignmentStore:
    """SQLite command/read store for a user's hired Agent roster."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def initialize(self) -> None:
        """Verify Alembic-owned schema; never create post-baseline tables here."""
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute("SELECT name FROM sqlite_schema WHERE type='table'") as cursor:
                tables = {str(row[0]) for row in await cursor.fetchall()}
            missing_tables = sorted(set(_REQUIRED_SCHEMA) - tables)
            if missing_tables:
                raise AgentAssignmentUnavailableError(
                    f"Agent Marketplace schema missing tables: {missing_tables}",
                )
            for table_name, required_columns in _REQUIRED_SCHEMA.items():
                async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                    table_info = await cursor.fetchall()
                columns = {str(row[1]) for row in table_info}
                missing_columns = sorted(required_columns - columns)
                if missing_columns:
                    raise AgentAssignmentUnavailableError(
                        f"Agent Marketplace schema missing {table_name} columns: {missing_columns}",
                    )
                primary_key = tuple(str(row[1]) for row in sorted(table_info, key=lambda item: int(item[5])) if int(row[5]) > 0)
                if primary_key != _REQUIRED_PRIMARY_KEYS[table_name]:
                    raise AgentAssignmentUnavailableError(
                        f"Agent Marketplace schema invalid {table_name} primary key: {primary_key}",
                    )
                required_unique = set(_REQUIRED_UNIQUE_KEYS.get(table_name, ()))
                if required_unique:
                    async with db.execute(f"PRAGMA index_list({table_name})") as cursor:
                        indexes = await cursor.fetchall()
                    actual_unique: set[tuple[str, ...]] = set()
                    for index in indexes:
                        if not bool(index[2]):
                            continue
                        async with db.execute(f"PRAGMA index_info({index[1]})") as cursor:
                            index_columns = await cursor.fetchall()
                        actual_unique.add(tuple(str(row[2]) for row in index_columns))
                    missing_unique = sorted(required_unique - actual_unique)
                    if missing_unique:
                        raise AgentAssignmentUnavailableError(
                            f"Agent Marketplace schema missing {table_name} unique keys: {missing_unique}",
                        )
            async with db.execute(
                """
                SELECT sql FROM sqlite_schema
                WHERE type='index' AND tbl_name='agent_assignment_backfill_run'
                  AND name='uq_agent_assignment_backfill_single_running'
                """,
            ) as cursor:
                single_running_index = await cursor.fetchone()
            expected_index_sql = (
                "createuniqueindexuq_agent_assignment_backfill_single_runningonagent_assignment_backfill_run((1))wherestatus='running'"
            )
            actual_index_sql = (
                "".join(str(single_running_index[0]).lower().split()) if single_running_index is not None and single_running_index[0] else ""
            )
            if actual_index_sql != expected_index_sql:
                raise AgentAssignmentUnavailableError(
                    "Agent Marketplace schema has invalid single-running-backfill constraint",
                )
            async with db.execute(
                """
                SELECT schema_version,minimum_reader_version,minimum_writer_version
                FROM agent_marketplace_schema_meta WHERE singleton=1
                """,
            ) as cursor:
                meta = await cursor.fetchone()
        if meta is None or int(meta[0]) < AGENT_MARKETPLACE_SCHEMA_VERSION:
            raise AgentAssignmentUnavailableError(
                "Agent Marketplace schema metadata is missing or unsupported",
            )
        minimum_reader = str(meta[1])
        minimum_writer = str(meta[2])
        if not (len(minimum_reader) == 4 and minimum_reader.isdigit() and len(minimum_writer) == 4 and minimum_writer.isdigit()):
            raise AgentAssignmentUnavailableError(
                "Agent Marketplace compatibility metadata is malformed",
            )
        if minimum_reader > AGENT_MARKETPLACE_READER_VERSION or minimum_writer > AGENT_MARKETPLACE_WRITER_VERSION:
            raise AgentAssignmentUnavailableError(
                "Agent Marketplace application is below the persisted reader/writer compatibility floor",
            )

    async def cutover_complete(self) -> bool:
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta WHERE singleton=1",
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return bool(row and row[0])

    async def marketplace_ready(self) -> bool:
        """Return the GA admission state after full schema/compatibility validation."""
        await self.initialize()
        return await self._completed_backfill_result() is not None

    async def ensure_marketplace_cutover(
        self,
        *,
        source_build: str | None = None,
        batch_size: int = 100,
    ) -> AgentAssignmentBackfillResult:
        """Idempotently apply or verify the one-time Marketplace cutover."""
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        await self.initialize()
        completed = await self._completed_backfill_result()
        if completed is not None:
            return completed
        if await self.cutover_complete():
            if not await self._legacy_v1_cutover_receipt_exists():
                raise AgentAssignmentUnavailableError(
                    "Agent Marketplace cutover marker has no verifiable v1 or v2 receipt",
                )
            return await self._reconcile_legacy_v1_cutover(
                source_build=source_build,
                batch_size=batch_size,
            )
        return await self.backfill_existing_access(
            apply=True,
            source_build=source_build,
            batch_size=batch_size,
        )

    async def verify_marketplace_cutover(self) -> AgentAssignmentBackfillResult:
        """Verify schema/compatibility and return the immutable cutover receipt."""
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        await self.initialize()
        completed = await self._completed_backfill_result()
        if completed is None:
            raise AgentAssignmentUnavailableError("Agent Marketplace cutover marker is incomplete")
        return completed

    async def deployment_admission(self) -> AgentMarketplaceDeploymentAdmission:
        """Read-only candidate compatibility verdict for deploy and rollback."""
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            await db.execute("PRAGMA query_only=ON")
            async with db.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='alembic_version'",
            ) as cursor:
                has_alembic_version = await cursor.fetchone() is not None
            revision = ""
            if has_alembic_version:
                async with db.execute("SELECT version_num FROM alembic_version") as cursor:
                    revisions = await cursor.fetchall()
                if len(revisions) == 1:
                    revision = str(revisions[0][0])
            revision_prefix = revision.partition("_")[0]
            safety_revision_installed = revision_prefix.isdigit() and int(revision_prefix) >= 26
            async with db.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='agent_marketplace_schema_meta'",
            ) as cursor:
                if await cursor.fetchone() is None:
                    if safety_revision_installed:
                        raise AgentAssignmentUnavailableError(
                            "Agent Marketplace metadata is missing after the safety revision",
                        )
                    return AgentMarketplaceDeploymentAdmission(
                        schema_version=None,
                        candidate_reader_version=AGENT_MARKETPLACE_READER_VERSION,
                        candidate_writer_version=AGENT_MARKETPLACE_WRITER_VERSION,
                        minimum_reader_version=None,
                        minimum_writer_version=None,
                        candidate_compatible=None,
                        migration_pending=True,
                        cutover_complete=None,
                        ready=True,
                    )
            async with db.execute("PRAGMA table_info(agent_marketplace_schema_meta)") as cursor:
                columns = {str(row[1]) for row in await cursor.fetchall()}
            required = {"minimum_reader_version", "minimum_writer_version"}
            if not required <= columns:
                if safety_revision_installed:
                    raise AgentAssignmentUnavailableError(
                        "Agent Marketplace compatibility columns are missing after the safety revision",
                    )
                return AgentMarketplaceDeploymentAdmission(
                    schema_version=None,
                    candidate_reader_version=AGENT_MARKETPLACE_READER_VERSION,
                    candidate_writer_version=AGENT_MARKETPLACE_WRITER_VERSION,
                    minimum_reader_version=None,
                    minimum_writer_version=None,
                    candidate_compatible=None,
                    migration_pending=True,
                    cutover_complete=None,
                    ready=True,
                )
            async with db.execute(
                """
                SELECT schema_version,minimum_reader_version,minimum_writer_version,
                       assignment_cutover_complete_at
                FROM agent_marketplace_schema_meta WHERE singleton=1
                """,
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise AgentAssignmentUnavailableError("Agent Marketplace compatibility metadata is missing")
        minimum_reader = str(row[1])
        minimum_writer = str(row[2])
        if not (len(minimum_reader) == 4 and minimum_reader.isdigit() and len(minimum_writer) == 4 and minimum_writer.isdigit()):
            raise AgentAssignmentUnavailableError("Agent Marketplace compatibility metadata is malformed")
        compatible = minimum_reader <= AGENT_MARKETPLACE_READER_VERSION and minimum_writer <= AGENT_MARKETPLACE_WRITER_VERSION
        return AgentMarketplaceDeploymentAdmission(
            schema_version=int(row[0]),
            candidate_reader_version=AGENT_MARKETPLACE_READER_VERSION,
            candidate_writer_version=AGENT_MARKETPLACE_WRITER_VERSION,
            minimum_reader_version=minimum_reader,
            minimum_writer_version=minimum_writer,
            candidate_compatible=compatible,
            migration_pending=False,
            cutover_complete=bool(row[3]),
            ready=compatible,
        )

    async def runtime_schema_state(self) -> AssignmentRuntimeSchemaState:
        """Return the sticky runtime cutover state without fail-open inference.

        A rolling reader may preserve legacy semantics only when the database
        explicitly reports an Alembic revision older than 0025.  Once 0025 is
        installed, missing tables, columns, or the singleton metadata row are
        corruption/unavailability, never evidence that cutover did not happen.
        """
        try:
            async with open_shared_ctx(self.db_path) as db:
                async with db.execute("SELECT name FROM sqlite_schema WHERE type='table'") as cursor:
                    tables = {str(row[0]) for row in await cursor.fetchall()}
                if "agent_marketplace_schema_meta" not in tables:
                    if "alembic_version" not in tables:
                        return "unavailable"
                    async with db.execute("SELECT version_num FROM alembic_version") as cursor:
                        revisions = tuple(str(row[0]) for row in await cursor.fetchall())
                    if len(revisions) == 1 and _is_pre_marketplace_revision(revisions[0]):
                        return "pre_0025"
                    return "unavailable"
            await self.initialize()
            async with (
                open_shared_ctx(self.db_path) as db,
                db.execute(
                    "SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta WHERE singleton=1",
                ) as cursor,
            ):
                row = await cursor.fetchone()
        except Exception:
            return "unavailable"
        if row is None:
            return "unavailable"
        if not row[0]:
            return "pre_cutover"
        return "cutover" if await self._completed_backfill_result() is not None else "pre_cutover"

    async def get(self, *, team_id: str, user_id: str, agent_id: str) -> AgentAssignment | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_assignment WHERE team_id=? AND user_id=? AND agent_id=?",
                (team_id, user_id, agent_id),
            ) as cursor:
                row = await cursor.fetchone()
        return _assignment_from_row(row) if row else None

    async def get_many(self, *, team_id: str, user_id: str) -> dict[str, AgentAssignment]:
        """Every assignment row this user has in the team, in one read; unlike `list_for_user` it writes nothing."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM agent_assignment WHERE team_id=? AND user_id=?", (team_id, user_id)) as cursor:
                rows = await cursor.fetchall()
        return {str(row["agent_id"]): _assignment_from_row(row) for row in rows}

    async def list_for_user(self, *, team_id: str, user_id: str) -> dict[str, AgentAssignment]:
        await self.apply_team_defaults_for_user(team_id=team_id, user_id=user_id)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_assignment WHERE team_id=? AND user_id=? ORDER BY updated_at DESC",
                (team_id, user_id),
            ) as cursor:
                rows = await cursor.fetchall()
        return {str(row["agent_id"]): _assignment_from_row(row) for row in rows}

    async def list_default_policies(self, *, team_id: str) -> dict[str, AgentDefaultPolicy]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_default_policy WHERE team_id=? ORDER BY agent_id",
                (team_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return {str(row["agent_id"]): _default_policy_from_row(row) for row in rows}

    async def active_member_ids(self, *, team_id: str) -> tuple[str, ...]:
        """Return the exact active membership cohort used by team defaults."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT u.id
                FROM users AS u
                JOIN team_members AS tm
                  ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
                WHERE u.status='active'
                ORDER BY u.id
                """,
                (team_id,),
            ) as cursor,
        ):
            return tuple(str(row[0]) for row in await cursor.fetchall())

    async def preview_team_default(
        self,
        *,
        team_id: str,
        agent_id: str,
        action: Literal["set", "remove"],
    ) -> AgentDefaultPreview:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_default_policy WHERE team_id=? AND agent_id=?",
                (team_id, agent_id),
            ) as cursor:
                policy_row = await cursor.fetchone()
            active_member_count = await self._active_member_count_conn(db, team_id=team_id)
            async with db.execute(
                """
                SELECT COUNT(*) FROM agent_assignment
                WHERE team_id=? AND agent_id=? AND personal_state<>'removed'
                """,
                (team_id, agent_id),
            ) as cursor:
                assignment_count = int((await cursor.fetchone())[0])
            async with db.execute(
                """
                SELECT COUNT(*) FROM agent_default_opt_out
                WHERE team_id=? AND agent_id=? AND cleared_at IS NULL
                """,
                (team_id, agent_id),
            ) as cursor:
                opted_out_count = int((await cursor.fetchone())[0])
            if action == "set":
                async with db.execute(
                    """
                    SELECT COUNT(*)
                    FROM users AS u
                    JOIN team_members AS tm
                      ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
                    LEFT JOIN agent_assignment AS aa
                      ON aa.team_id=tm.team_id AND aa.user_id=u.id AND aa.agent_id=?
                     AND aa.personal_state<>'removed'
                    LEFT JOIN agent_default_opt_out AS opt
                      ON opt.team_id=tm.team_id AND opt.user_id=u.id AND opt.agent_id=?
                     AND opt.cleared_at IS NULL
                    WHERE u.status='active' AND aa.id IS NULL AND opt.user_id IS NULL
                    """,
                    (team_id, agent_id, agent_id),
                ) as cursor:
                    affected_count = int((await cursor.fetchone())[0])
            else:
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM agent_assignment
                    WHERE team_id=? AND agent_id=? AND source='team_default'
                    """,
                    (team_id, agent_id),
                ) as cursor:
                    affected_count = int((await cursor.fetchone())[0])
        return AgentDefaultPreview(
            agent_id=agent_id,
            action=action,
            active_member_count=active_member_count,
            assignment_count=assignment_count,
            opted_out_count=opted_out_count,
            affected_count=affected_count,
            policy=_default_policy_from_row(policy_row) if policy_row else None,
        )

    async def set_team_default(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        enabled: bool,
        expected_agent_row_version: int | None = None,
    ) -> AgentDefaultPreview:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_team_admin_conn(
                    db,
                    team_id=team_id,
                    user_id=actor_user_id,
                )
                if enabled:
                    await self._require_teamwide_agent_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        expected_row_version=expected_agent_row_version,
                    )
                    await self._require_all_active_members_live_access_locked(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                    )
                async with db.execute(
                    "SELECT * FROM agent_default_policy WHERE team_id=? AND agent_id=?",
                    (team_id, agent_id),
                ) as cursor:
                    current = await cursor.fetchone()
                if current is None and not enabled:
                    await db.commit()
                    return await self.preview_team_default(
                        team_id=team_id,
                        agent_id=agent_id,
                        action="remove",
                    )
                if current is not None and bool(current["enabled"]) is enabled:
                    await db.commit()
                    return await self.preview_team_default(
                        team_id=team_id,
                        agent_id=agent_id,
                        action="set" if enabled else "remove",
                    )
                policy_version = int(current["policy_version"]) + 1 if current else 1
                await db.execute(
                    """
                    INSERT INTO agent_default_policy(
                      team_id,agent_id,agent_source_kind,enabled,policy_version,row_version,
                      changed_by,created_at,updated_at
                    ) VALUES (?,?,?,?,?,1,?,?,?)
                    ON CONFLICT(team_id,agent_id) DO UPDATE SET
                      enabled=excluded.enabled,
                      policy_version=excluded.policy_version,
                      row_version=agent_default_policy.row_version+1,
                      changed_by=excluded.changed_by,
                      updated_at=excluded.updated_at
                    """,
                    (
                        team_id,
                        agent_id,
                        "team",
                        int(enabled),
                        policy_version,
                        actor_user_id,
                        timestamp,
                        timestamp,
                    ),
                )
                if enabled:
                    affected_ids = await self._apply_default_assignments_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        policy_version=policy_version,
                        timestamp=timestamp,
                    )
                else:
                    async with db.execute(
                        """
                        SELECT id FROM agent_assignment
                        WHERE team_id=? AND agent_id=? AND source='team_default'
                        """,
                        (team_id, agent_id),
                    ) as cursor:
                        affected_ids = tuple(str(row[0]) for row in await cursor.fetchall())
                    await db.execute(
                        """
                        UPDATE agent_assignment
                        SET source='former_team_default',row_version=row_version+1,updated_at=?
                        WHERE team_id=? AND agent_id=? AND source='team_default'
                        """,
                        (timestamp, team_id, agent_id),
                    )
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action=(AuditAction.AGENT_TEAM_DEFAULT_SET if enabled else AuditAction.AGENT_TEAM_DEFAULT_REMOVED),
                    agent_id=agent_id,
                    assignment_id=f"default:{agent_id}",
                    payload={
                        "policy_version": policy_version,
                        "affected_count": len(affected_ids),
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.preview_team_default(
            team_id=team_id,
            agent_id=agent_id,
            action="set" if enabled else "remove",
        )

    async def apply_team_defaults_for_user(self, *, team_id: str, user_id: str) -> tuple[str, ...]:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_active_member_conn(db, team_id=team_id, user_id=user_id)
                policies = await self._eligible_default_policies_conn(db, team_id=team_id)
                affected: list[str] = []
                for policy in policies:
                    try:
                        await self._require_live_team_access_locked(
                            team_id=team_id,
                            user_id=user_id,
                            agent_id=str(policy["agent_id"]),
                        )
                    except AgentAssignmentAccessError:
                        # A team default is a convenience, never a grant. A
                        # later audience/resource deny must therefore prevent
                        # repair from creating an apparently active roster row.
                        continue
                    affected.extend(
                        await self._apply_default_assignments_conn(
                            db,
                            team_id=team_id,
                            agent_id=str(policy["agent_id"]),
                            policy_version=int(policy["policy_version"]),
                            timestamp=timestamp,
                            user_id=user_id,
                        ),
                    )
                await db.commit()
            except AgentAssignmentAccessError:
                await db.rollback()
                return ()
            except BaseException:
                await db.rollback()
                raise
        return tuple(affected)

    async def backfill_existing_access(
        self,
        *,
        apply: bool,
        source_build: str | None = None,
        batch_size: int = 100,
    ) -> AgentAssignmentBackfillResult:
        """Backfill the pre-M1 visible roster and open the cutover gate after reconciliation.

        Apply owns a persisted lease, evaluates every membership at one fixed
        ``snapshot_at``, stages the expected set by run, then writes assignments
        in bounded keyset batches. Once cutover completes, apply is permanently
        rejected so later Agents and personal removals cannot be mistaken for
        legacy state.
        """
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        await self.initialize()
        completed = await self._completed_backfill_result()
        if completed is not None:
            if apply:
                raise AgentAssignmentCutoverCompleteError(
                    "assignment cutover is already complete; backfill apply is one-time",
                )
            return completed

        if not apply:
            return await self._dry_run_backfill(batch_size=batch_size)

        timestamp = _now()
        owner_token = uuid4().hex
        lease: _BackfillLease | None = None
        try:
            lease = await self._start_or_resume_backfill(
                source_build=source_build,
                timestamp=timestamp,
                owner_token=owner_token,
            )
            expected, digest = await self._stage_backfill_expected(
                lease=lease,
                batch_size=batch_size,
            )
            await self._backfill_names_for_run(
                run_id=lease.run_id,
                owner_token=owner_token,
                timestamp=lease.snapshot_at,
            )
            while True:
                applied = await self._apply_backfill_batch(
                    lease=lease,
                    batch_size=batch_size,
                )
                if not applied:
                    break
            inserted, conflict, completed_at = await self._complete_backfill_run(
                lease=lease,
            )
        except BaseException:
            if lease is not None:
                await self._mark_backfill_failed(
                    run_id=lease.run_id,
                    owner_token=owner_token,
                )
            raise
        return AgentAssignmentBackfillResult(
            expected=expected,
            inserted=inserted,
            conflict=conflict,
            digest=digest,
            cutover_completed_at=completed_at,
        )

    async def _dry_run_backfill(self, *, batch_size: int) -> AgentAssignmentBackfillResult:
        from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

        snapshot = datetime.now(UTC)
        resolver = TeamSubagentAccessResolver(self.db_path)
        after: tuple[str, str] | None = None
        expected = 0
        hasher = hashlib.sha256()
        while True:
            members = await self._member_batch(after=after, limit=batch_size)
            if not members:
                break
            for team_id, user_id in members:
                decisions = await resolver.resolve_many(
                    team_id=team_id,
                    actor_user_id=user_id,
                    now=snapshot,
                )
                for agent_id in sorted(agent_id for agent_id, decision in decisions.items() if decision.resolution.can_consume):
                    _update_backfill_digest(hasher, (team_id, user_id, agent_id))
                    expected += 1
            after = members[-1]
        return AgentAssignmentBackfillResult(
            expected=expected,
            inserted=0,
            conflict=0,
            digest=hasher.hexdigest(),
            cutover_completed_at=None,
        )

    async def _member_batch(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        created_at_or_before: str | None = None,
    ) -> list[tuple[str, str]]:
        after_team, after_user = after or ("", "")
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT DISTINCT tm.team_id,tm.user_id
                FROM team_members AS tm
                JOIN users AS u ON u.id=tm.user_id
                WHERE tm.status='active' AND u.status='active'
                  AND (
                    ? IS NULL OR (
                      julianday(tm.created_at) IS NOT NULL
                      AND julianday(tm.created_at)<=julianday(?)
                      AND NOT EXISTS (
                        SELECT 1 FROM admin_audit_log AS removal
                        WHERE removal.action=? AND removal.target_kind='user'
                          AND removal.target_id=tm.user_id
                          AND (
                            removal.target_team_id=tm.team_id OR removal.team_id=tm.team_id
                          )
                      )
                    )
                  )
                  AND (tm.team_id>? OR (tm.team_id=? AND tm.user_id>?))
                ORDER BY tm.team_id,tm.user_id
                LIMIT ?
                """,
                (
                    created_at_or_before,
                    created_at_or_before,
                    AuditAction.TEAM_MEMBER_DEACTIVATED,
                    after_team,
                    after_team,
                    after_user,
                    limit,
                ),
            ) as cursor,
        ):
            return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]

    async def _legacy_v1_eligible_agent_ids(
        self,
        *,
        team_id: str,
        cutover_at: str,
    ) -> frozenset[str]:
        """Reconstruct only definitions provably present and unchanged at v1 cutover."""
        marker = _parse_timestamp(cutover_at)
        eligible: set[str] = set()
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT id,created_at,updated_at FROM team_subagent
                WHERE team_id=? ORDER BY id
                """,
                (team_id,),
            ) as cursor,
        ):
            while rows := await cursor.fetchmany(250):
                for row in rows:
                    try:
                        created_at = _parse_timestamp(str(row[1]))
                        updated_at = _parse_timestamp(str(row[2]))
                    except ValueError as exc:
                        raise AgentAssignmentUnavailableError(
                            f"legacy v1 Agent {row[0]} has malformed lifecycle timestamps",
                        ) from exc
                    if created_at > marker:
                        continue
                    if updated_at > marker:
                        raise AgentAssignmentUnavailableError(
                            "legacy v1 repair cannot reconstruct Agent audience/lifecycle "
                            f"changed after cutover: {row[0]}; manual reconciliation required",
                        )
                    eligible.add(str(row[0]))
        return frozenset(eligible)

    async def _legacy_v1_custom_access_was_active(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        access_reason: str,
        cutover_at: str,
    ) -> bool:
        """Return whether current custom access is provably continuous from v1 cutover."""
        marker = _parse_timestamp(cutover_at)
        if access_reason == "user_grant":
            query = """
                SELECT grant_row.id,grant_row.created_at,grant_row.updated_at,
                       grant_row.expires_at
                FROM resource_grants AS grant_row
                WHERE grant_row.team_id=? AND grant_row.resource_kind='team_subagent'
                  AND grant_row.resource_id=? AND grant_row.principal_type='user'
                  AND grant_row.principal_id=?
                  AND grant_row.permission IN ('use','write','admin')
                  AND grant_row.effect='allow' AND grant_row.revoked_at IS NULL
                ORDER BY grant_row.id
                """
            params = (team_id, agent_id, user_id)
        elif access_reason == "group_grant":
            query = """
                SELECT grant_row.id,grant_row.created_at,grant_row.updated_at,
                       grant_row.expires_at,access_group.id AS group_id,
                       access_group.created_at AS group_created_at,
                       access_group.updated_at AS group_updated_at,
                       membership.created_at AS membership_created_at,
                       membership.removed_at AS membership_removed_at
                FROM resource_grants AS grant_row
                JOIN groups AS access_group
                  ON access_group.id=grant_row.principal_id
                 AND access_group.team_id=grant_row.team_id
                 AND access_group.status='active'
                JOIN group_memberships AS membership
                  ON membership.team_id=grant_row.team_id
                 AND membership.group_id=grant_row.principal_id
                 AND membership.principal_type='user'
                 AND membership.principal_id=?
                 AND membership.status='active'
                WHERE grant_row.team_id=? AND grant_row.resource_kind='team_subagent'
                  AND grant_row.resource_id=? AND grant_row.principal_type='group'
                  AND grant_row.permission IN ('use','write','admin')
                  AND grant_row.effect='allow' AND grant_row.revoked_at IS NULL
                ORDER BY grant_row.id
                """
            params = (user_id, team_id, agent_id)
        else:
            return True

        unprovable: str | None = None
        proven_allow = False
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        for row in rows:
            grant_id = str(row["id"])
            try:
                grant_created = _parse_timestamp(str(row["created_at"]))
            except (TypeError, ValueError):
                unprovable = f"grant {grant_id} has malformed created_at"
                continue
            if grant_created > marker:
                continue
            try:
                grant_updated = _parse_timestamp(str(row["updated_at"]))
                expires_at = _parse_timestamp(str(row["expires_at"])) if row["expires_at"] is not None else None
            except (TypeError, ValueError):
                unprovable = f"grant {grant_id} has malformed lifecycle timestamps"
                continue
            if grant_updated > marker:
                unprovable = f"grant {grant_id} changed after cutover"
                continue
            if expires_at is not None and expires_at <= marker:
                continue
            if access_reason == "group_grant":
                group_id = str(row["group_id"])
                try:
                    group_created = _parse_timestamp(str(row["group_created_at"]))
                    group_updated = _parse_timestamp(str(row["group_updated_at"]))
                    membership_created = _parse_timestamp(str(row["membership_created_at"]))
                except (TypeError, ValueError):
                    unprovable = f"group {group_id} has malformed lifecycle timestamps"
                    continue
                if group_created > marker or membership_created > marker:
                    continue
                if group_updated > marker or row["membership_removed_at"] is not None:
                    unprovable = f"group {group_id} changed after cutover"
                    continue
            proven_allow = True

        if not proven_allow:
            if unprovable is not None:
                raise AgentAssignmentUnavailableError(
                    "legacy v1 repair cannot reconstruct custom Agent access: "
                    f"{team_id}/{user_id}/{agent_id}: {unprovable}; "
                    "manual reconciliation required",
                )
            return False
        return True

    async def _legacy_v1_deny_was_active(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        cutover_at: str,
    ) -> bool:
        """Fail closed when a direct or group deny covered the user at cutover."""
        marker = _parse_timestamp(cutover_at)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT deny.id,deny.principal_type,deny.principal_id,deny.created_at,
                       deny.updated_at,deny.expires_at,deny.revoked_at,
                       access_group.status AS group_status,
                       access_group.created_at AS group_created_at,
                       access_group.updated_at AS group_updated_at,
                       membership.status AS membership_status,
                       membership.created_at AS membership_created_at,
                       membership.removed_at AS membership_removed_at
                FROM resource_grants AS deny
                LEFT JOIN groups AS access_group
                  ON deny.principal_type='group' AND access_group.id=deny.principal_id
                 AND access_group.team_id=deny.team_id
                LEFT JOIN group_memberships AS membership
                  ON deny.principal_type='group' AND membership.team_id=deny.team_id
                 AND membership.group_id=deny.principal_id
                 AND membership.principal_type='user' AND membership.principal_id=?
                WHERE deny.team_id=? AND deny.resource_kind='team_subagent'
                  AND deny.resource_id=? AND deny.permission IN ('use','write','admin')
                  AND deny.effect='deny'
                  AND (
                    (deny.principal_type='user' AND deny.principal_id=?)
                    OR deny.principal_type='group'
                  )
                ORDER BY deny.id
                """,
                (user_id, team_id, agent_id, user_id),
            ) as cursor:
                rows = await cursor.fetchall()
        for row in rows:
            deny_id = str(row["id"])
            try:
                created_at = _parse_timestamp(str(row["created_at"]))
            except (TypeError, ValueError) as exc:
                raise AgentAssignmentUnavailableError(
                    f"legacy v1 repair cannot reconstruct deny lifecycle: {deny_id}; manual reconciliation required",
                ) from exc
            if created_at > marker:
                continue
            try:
                updated_at = _parse_timestamp(str(row["updated_at"]))
                expires_at = _parse_timestamp(str(row["expires_at"])) if row["expires_at"] is not None else None
                revoked_at = _parse_timestamp(str(row["revoked_at"])) if row["revoked_at"] is not None else None
            except (TypeError, ValueError) as exc:
                raise AgentAssignmentUnavailableError(
                    f"legacy v1 repair cannot reconstruct deny lifecycle: {deny_id}; manual reconciliation required",
                ) from exc
            if expires_at is not None and expires_at <= marker:
                if updated_at > marker and (revoked_at is None or revoked_at > marker):
                    return True
                continue
            if revoked_at is not None and revoked_at <= marker:
                continue
            if row["principal_type"] == "user":
                return True
            if row["membership_created_at"] is None:
                continue
            try:
                group_created = _parse_timestamp(str(row["group_created_at"]))
                group_updated = _parse_timestamp(str(row["group_updated_at"]))
                membership_created = _parse_timestamp(str(row["membership_created_at"]))
                membership_removed = _parse_timestamp(str(row["membership_removed_at"])) if row["membership_removed_at"] is not None else None
            except (TypeError, ValueError) as exc:
                raise AgentAssignmentUnavailableError(
                    f"legacy v1 repair cannot reconstruct deny group membership: {deny_id}; manual reconciliation required",
                ) from exc
            if group_created > marker:
                continue
            if group_updated > marker or membership_created > marker:
                raise AgentAssignmentUnavailableError(
                    f"legacy v1 repair cannot prove absence of historical group deny: {deny_id}; manual reconciliation required",
                )
            if row["group_status"] != "active":
                continue
            membership_status = str(row["membership_status"])
            if (membership_status == "active") != (membership_removed is None):
                raise AgentAssignmentUnavailableError(
                    f"legacy v1 repair cannot reconstruct deny group membership: {deny_id}; manual reconciliation required",
                )
            membership_active = membership_created <= marker and (membership_removed is None or membership_removed > marker)
            if membership_active:
                return True
        return False

    async def _completed_backfill_result(self) -> AgentAssignmentBackfillResult | None:
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                """
                SELECT assignment_cutover_complete_at,assignment_backfill_expected,
                       assignment_backfill_inserted,assignment_backfill_digest
                FROM agent_marketplace_schema_meta WHERE singleton=1
                """,
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or not row[0]:
                return None
            async with db.execute(
                """
                SELECT conflict_count FROM agent_assignment_backfill_run
                WHERE status='completed' AND snapshot_at IS NOT NULL
                ORDER BY finished_at DESC LIMIT 1
                """,
            ) as cursor:
                run = await cursor.fetchone()
        if run is None:
            return None
        return AgentAssignmentBackfillResult(
            expected=int(row[1] or 0),
            inserted=int(row[2] or 0),
            conflict=int(run[0]),
            digest=str(row[3] or ""),
            cutover_completed_at=str(row[0]),
        )

    async def _legacy_v1_cutover_receipt_exists(self) -> bool:
        """Return whether 0025 completed cutover before fixed snapshots existed."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT 1 FROM agent_assignment_backfill_run
                WHERE status='completed' AND snapshot_at IS NULL
                LIMIT 1
                """,
            ) as cursor,
        ):
            return await cursor.fetchone() is not None

    async def _reconcile_legacy_v1_cutover(
        self,
        *,
        source_build: str | None,
        batch_size: int,
    ) -> AgentAssignmentBackfillResult:
        """Upgrade a completed 0025 receipt to the fixed-snapshot v2 contract.

        The 0025 enumerator followed ``users.team_id`` and could omit secondary
        active memberships. This repair snapshots all active memberships and
        inserts only genuinely missing rows. Existing disabled or removed rows
        satisfy reconciliation and are never reactivated.
        """
        timestamp = _now()
        owner_token = uuid4().hex
        lease: _BackfillLease | None = None
        try:
            try:
                lease = await self._start_or_resume_legacy_v1_repair(
                    source_build=source_build,
                    timestamp=timestamp,
                    owner_token=owner_token,
                )
            except _LegacyV1RepairAlreadyCompleted:
                completed = await self._completed_backfill_result()
                if completed is None:
                    raise AgentAssignmentUnavailableError(
                        "concurrent legacy v1 repair completed without a readable v2 receipt",
                    ) from None
                return completed
            expected, digest = await self._stage_backfill_expected(
                lease=lease,
                batch_size=batch_size,
            )
            await self._backfill_names_for_run(
                run_id=lease.run_id,
                owner_token=owner_token,
                timestamp=lease.snapshot_at,
                legacy_v1_repair=True,
            )
            while await self._apply_backfill_batch(
                lease=lease,
                batch_size=batch_size,
            ):
                pass
            inserted, conflict, completed_at = await self._complete_legacy_v1_repair(
                lease=lease,
            )
        except BaseException:
            if lease is not None:
                await self._mark_backfill_failed(
                    run_id=lease.run_id,
                    owner_token=owner_token,
                )
            raise
        return AgentAssignmentBackfillResult(
            expected=expected,
            inserted=inserted,
            conflict=conflict,
            digest=digest,
            cutover_completed_at=completed_at,
        )

    async def _start_or_resume_legacy_v1_repair(
        self,
        *,
        source_build: str | None,
        timestamp: str,
        owner_token: str,
    ) -> _BackfillLease:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta
                    WHERE singleton=1
                    """,
                ) as cursor:
                    meta = await cursor.fetchone()
                if meta is None or not meta["assignment_cutover_complete_at"]:
                    raise AgentAssignmentUnavailableError(
                        "legacy v1 repair requires a completed 0025 cutover marker",
                    )
                legacy_cutover_at = str(meta["assignment_cutover_complete_at"])
                try:
                    _parse_timestamp(legacy_cutover_at)
                except ValueError as exc:
                    raise AgentAssignmentUnavailableError(
                        "legacy v1 cutover marker is malformed",
                    ) from exc
                async with db.execute(
                    """
                    SELECT 1 FROM agent_assignment_backfill_run
                    WHERE status='completed' AND snapshot_at IS NOT NULL LIMIT 1
                    """,
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise _LegacyV1RepairAlreadyCompleted
                async with db.execute(
                    """
                    SELECT 1 FROM agent_assignment_backfill_run
                    WHERE status='completed' AND snapshot_at IS NULL LIMIT 1
                    """,
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise AgentAssignmentUnavailableError(
                            "legacy v1 repair requires a completed 0025 receipt",
                        )
                async with db.execute(
                    """
                    SELECT * FROM agent_assignment_backfill_run
                    WHERE status IN ('running','failed') AND snapshot_at IS NOT NULL
                    ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, started_at DESC
                    LIMIT 1
                    """,
                ) as cursor:
                    current = await cursor.fetchone()
                if current is not None:
                    current_owner = str(current["owner_token"] or "")
                    lease_expires_at = str(current["lease_expires_at"] or "")
                    if str(current["status"]) == "running" and current_owner != owner_token:
                        if not lease_expires_at:
                            raise AgentAssignmentBackfillLeaseError(
                                "active legacy v1 repair has no recoverable lease expiry",
                            )
                        try:
                            lease_active = _parse_timestamp(lease_expires_at) > _parse_timestamp(
                                timestamp,
                            )
                        except ValueError as exc:
                            raise AgentAssignmentBackfillLeaseError(
                                "active legacy v1 repair lease metadata is malformed",
                            ) from exc
                        if lease_active:
                            raise AgentAssignmentBackfillLeaseError(
                                "another process owns the active legacy v1 repair lease",
                            )
                    run_id = str(current["id"])
                    updated = await db.execute(
                        """
                        UPDATE agent_assignment_backfill_run
                        SET status='running',owner_token=?,lease_expires_at=?,
                            source_build=COALESCE(?,source_build),updated_at=?,finished_at=NULL
                        WHERE id=?
                        """,
                        (
                            owner_token,
                            _lease_deadline(timestamp),
                            source_build,
                            timestamp,
                            run_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AgentAssignmentBackfillLeaseError(
                            "legacy v1 repair lease acquisition lost",
                        )
                    snapshot_at = str(current["snapshot_at"])
                    if _parse_timestamp(snapshot_at) != _parse_timestamp(legacy_cutover_at):
                        raise AgentAssignmentUnavailableError(
                            "legacy v1 repair snapshot does not match the original cutover marker",
                        )
                    snapshot_completed = bool(current["snapshot_completed_at"])
                else:
                    run_id = _id("abf")
                    await db.execute(
                        """
                        INSERT INTO agent_assignment_backfill_run(
                          id,status,expected_count,inserted_count,conflict_count,error_count,
                          source_build,started_at,updated_at,snapshot_at,owner_token,lease_expires_at
                        ) VALUES (?,'running',0,0,0,0,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            source_build,
                            timestamp,
                            timestamp,
                            legacy_cutover_at,
                            owner_token,
                            _lease_deadline(timestamp),
                        ),
                    )
                    snapshot_at = legacy_cutover_at
                    snapshot_completed = False
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _BackfillLease(
            run_id=run_id,
            owner_token=owner_token,
            snapshot_at=snapshot_at,
            snapshot_completed=snapshot_completed,
            legacy_v1_repair=True,
        )

    async def _start_or_resume_backfill(
        self,
        *,
        source_build: str | None,
        timestamp: str,
        owner_token: str,
    ) -> _BackfillLease:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT assignment_cutover_complete_at
                    FROM agent_marketplace_schema_meta WHERE singleton=1
                    """,
                ) as cursor:
                    meta = await cursor.fetchone()
                if meta is None:
                    raise AgentAssignmentUnavailableError("Agent Marketplace schema metadata is missing")
                if meta["assignment_cutover_complete_at"]:
                    raise AgentAssignmentCutoverCompleteError(
                        "assignment cutover completed while backfill was starting",
                    )
                async with db.execute(
                    """
                    SELECT * FROM agent_assignment_backfill_run
                    WHERE status IN ('running','failed') AND snapshot_at IS NOT NULL
                    ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, started_at DESC
                    LIMIT 1
                    """,
                ) as cursor:
                    current = await cursor.fetchone()
                if current is not None:
                    current_owner = str(current["owner_token"] or "")
                    lease_expires_at = str(current["lease_expires_at"] or "")
                    if str(current["status"]) == "running" and current_owner != owner_token:
                        if not lease_expires_at:
                            raise AgentAssignmentBackfillLeaseError(
                                "active assignment backfill has no recoverable lease expiry",
                            )
                        try:
                            lease_active = _parse_timestamp(lease_expires_at) > _parse_timestamp(timestamp)
                        except ValueError as exc:
                            raise AgentAssignmentBackfillLeaseError(
                                "active assignment backfill lease metadata is malformed",
                            ) from exc
                        if lease_active:
                            raise AgentAssignmentBackfillLeaseError(
                                "another process owns the active assignment backfill lease",
                            )
                    run_id = str(current["id"])
                    updated = await db.execute(
                        """
                        UPDATE agent_assignment_backfill_run
                        SET status='running',owner_token=?,lease_expires_at=?,
                            source_build=COALESCE(?,source_build),updated_at=?,finished_at=NULL
                        WHERE id=?
                        """,
                        (
                            owner_token,
                            _lease_deadline(timestamp),
                            source_build,
                            timestamp,
                            run_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AgentAssignmentBackfillLeaseError("assignment backfill lease acquisition lost")
                    snapshot_at = str(current["snapshot_at"])
                    snapshot_completed = bool(current["snapshot_completed_at"])
                else:
                    run_id = _id("abf")
                    await db.execute(
                        """
                        INSERT INTO agent_assignment_backfill_run(
                          id,status,expected_count,inserted_count,conflict_count,error_count,
                          source_build,started_at,updated_at,snapshot_at,owner_token,lease_expires_at
                        ) VALUES (?,'running',0,0,0,0,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            source_build,
                            timestamp,
                            timestamp,
                            timestamp,
                            owner_token,
                            _lease_deadline(timestamp),
                        ),
                    )
                    snapshot_at = timestamp
                    snapshot_completed = False
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _BackfillLease(
            run_id=run_id,
            owner_token=owner_token,
            snapshot_at=snapshot_at,
            snapshot_completed=snapshot_completed,
        )

    async def _stage_backfill_expected(
        self,
        *,
        lease: _BackfillLease,
        batch_size: int,
    ) -> tuple[int, str]:
        from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

        resolver = TeamSubagentAccessResolver(self.db_path)
        snapshot = _parse_timestamp(lease.snapshot_at)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT stage_cursor_team_id,stage_cursor_user_id,snapshot_completed_at "
                "FROM agent_assignment_backfill_run WHERE id=? AND owner_token=?",
                (lease.run_id, lease.owner_token),
            ) as cursor:
                progress = await cursor.fetchone()
        if progress is None:
            raise AgentAssignmentBackfillLeaseError("assignment backfill lease disappeared")
        after = (
            (str(progress["stage_cursor_team_id"]), str(progress["stage_cursor_user_id"])) if progress["stage_cursor_team_id"] is not None else None
        )
        legacy_eligible_by_team: dict[str, frozenset[str]] = {}
        if progress["snapshot_completed_at"] is None:
            while True:
                members = await self._member_batch(
                    after=after,
                    limit=batch_size,
                    created_at_or_before=(lease.snapshot_at if lease.legacy_v1_repair else None),
                )
                if not members:
                    break
                expected_pairs: list[tuple[str, str, str]] = []
                for team_id, user_id in members:
                    decisions = await resolver.resolve_many(
                        team_id=team_id,
                        actor_user_id=user_id,
                        now=snapshot,
                    )
                    if lease.legacy_v1_repair:
                        eligible_agent_ids = legacy_eligible_by_team.get(team_id)
                        if eligible_agent_ids is None:
                            eligible_agent_ids = await self._legacy_v1_eligible_agent_ids(
                                team_id=team_id,
                                cutover_at=lease.snapshot_at,
                            )
                            legacy_eligible_by_team[team_id] = eligible_agent_ids
                    else:
                        eligible_agent_ids = frozenset(decisions)
                    for agent_id, decision in sorted(decisions.items()):
                        if agent_id not in eligible_agent_ids or not decision.resolution.can_consume:
                            continue
                        access_reason = decision.resolution.access_reason
                        if lease.legacy_v1_repair and access_reason in {
                            "user_grant",
                            "group_grant",
                        }:
                            if not await self._legacy_v1_custom_access_was_active(
                                team_id=team_id,
                                user_id=user_id,
                                agent_id=agent_id,
                                access_reason=access_reason,
                                cutover_at=lease.snapshot_at,
                            ):
                                continue
                        if lease.legacy_v1_repair and await self._legacy_v1_deny_was_active(
                            team_id=team_id,
                            user_id=user_id,
                            agent_id=agent_id,
                            cutover_at=lease.snapshot_at,
                        ):
                            continue
                        expected_pairs.append((team_id, user_id, agent_id))
                await self._stage_expected_batch(
                    lease=lease,
                    members=members,
                    expected_pairs=expected_pairs,
                )
                after = members[-1]
        return await self._finalize_expected_snapshot(lease=lease, fetch_size=batch_size)

    async def _stage_expected_batch(
        self,
        *,
        lease: _BackfillLease,
        members: list[tuple[str, str]],
        expected_pairs: list[tuple[str, str, str]],
    ) -> None:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_running_backfill_conn(db, lease=lease)
                if expected_pairs:
                    await db.executemany(
                        """
                        INSERT INTO agent_assignment_backfill_expected(run_id,team_id,user_id,agent_id)
                        VALUES (?,?,?,?) ON CONFLICT DO NOTHING
                        """,
                        ((lease.run_id, *pair) for pair in expected_pairs),
                    )
                last_team_id, last_user_id = members[-1]
                updated = await db.execute(
                    """
                    UPDATE agent_assignment_backfill_run
                    SET stage_cursor_team_id=?,stage_cursor_user_id=?,updated_at=?,lease_expires_at=?
                    WHERE id=? AND status='running' AND owner_token=?
                    """,
                    (
                        last_team_id,
                        last_user_id,
                        timestamp,
                        _lease_deadline(timestamp),
                        lease.run_id,
                        lease.owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentAssignmentBackfillLeaseError("assignment backfill stage lease lost")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _finalize_expected_snapshot(
        self,
        *,
        lease: _BackfillLease,
        fetch_size: int,
    ) -> tuple[int, str]:
        hasher = hashlib.sha256()
        expected = 0
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
            SELECT team_id,user_id,agent_id
            FROM agent_assignment_backfill_expected
            WHERE run_id=? ORDER BY team_id,user_id,agent_id
            """,
                (lease.run_id,),
            ) as cursor,
        ):
            while rows := await cursor.fetchmany(fetch_size):
                for row in rows:
                    _update_backfill_digest(
                        hasher,
                        (str(row[0]), str(row[1]), str(row[2])),
                    )
                    expected += 1
        digest = hasher.hexdigest()
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_running_backfill_conn(db, lease=lease)
                updated = await db.execute(
                    """
                    UPDATE agent_assignment_backfill_run
                    SET expected_count=?,expected_digest=?,snapshot_completed_at=COALESCE(snapshot_completed_at,?),
                        updated_at=?,lease_expires_at=?
                    WHERE id=? AND status='running' AND owner_token=?
                    """,
                    (
                        expected,
                        digest,
                        timestamp,
                        timestamp,
                        _lease_deadline(timestamp),
                        lease.run_id,
                        lease.owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentAssignmentBackfillLeaseError("assignment backfill snapshot lease lost")
                await db.execute(
                    """
                    UPDATE agent_marketplace_schema_meta
                    SET assignment_backfill_expected=?,assignment_backfill_digest=?,updated_at=?
                    WHERE singleton=1 AND assignment_cutover_complete_at IS NULL
                    """,
                    (expected, digest, timestamp),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return expected, digest

    async def _backfill_names_for_run(
        self,
        *,
        run_id: str,
        owner_token: str,
        timestamp: str,
        legacy_v1_repair: bool = False,
    ) -> None:
        lease = _BackfillLease(
            run_id,
            owner_token,
            timestamp,
            True,
            legacy_v1_repair=legacy_v1_repair,
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_running_backfill_conn(db, lease=lease)
                await self._backfill_name_claims_conn(db, timestamp=timestamp)
                renewed_at = _now()
                updated = await db.execute(
                    """
                    UPDATE agent_assignment_backfill_run
                    SET updated_at=?,lease_expires_at=?
                    WHERE id=? AND status='running' AND owner_token=?
                    """,
                    (
                        renewed_at,
                        _lease_deadline(renewed_at),
                        run_id,
                        owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentAssignmentBackfillLeaseError("assignment backfill name lease lost")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _apply_backfill_batch(
        self,
        *,
        lease: _BackfillLease,
        batch_size: int,
    ) -> bool:
        inserted = 0
        conflict = 0
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_running_backfill_conn(db, lease=lease)
                async with db.execute(
                    "SELECT cursor_team_id,cursor_user_id,cursor_agent_id FROM agent_assignment_backfill_run WHERE id=? AND owner_token=?",
                    (lease.run_id, lease.owner_token),
                ) as cursor:
                    progress = await cursor.fetchone()
                if progress is None:
                    raise AgentAssignmentBackfillLeaseError("assignment backfill apply lease disappeared")
                cursor_key = (
                    str(progress["cursor_team_id"] or ""),
                    str(progress["cursor_user_id"] or ""),
                    str(progress["cursor_agent_id"] or ""),
                )
                async with db.execute(
                    """
                    SELECT team_id,user_id,agent_id
                    FROM agent_assignment_backfill_expected
                    WHERE run_id=? AND (
                      team_id>? OR
                      (team_id=? AND user_id>?) OR
                      (team_id=? AND user_id=? AND agent_id>?)
                    )
                    ORDER BY team_id,user_id,agent_id LIMIT ?
                    """,
                    (
                        lease.run_id,
                        cursor_key[0],
                        cursor_key[0],
                        cursor_key[1],
                        cursor_key[0],
                        cursor_key[1],
                        cursor_key[2],
                        batch_size,
                    ),
                ) as cursor:
                    batch = await cursor.fetchall()
                if not batch:
                    await db.rollback()
                    return False
                for row in batch:
                    team_id, user_id, agent_id = str(row[0]), str(row[1]), str(row[2])
                    inserted_row = await db.execute(
                        """
                        INSERT INTO agent_assignment(
                          id,team_id,user_id,agent_id,agent_source_kind,personal_state,access_state,
                          source,row_version,hired_at,current_cycle_started_at,personal_state_changed_at,
                          access_state_changed_at,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'active','granted','migration_existing_access',1,?,?,?,?,?,?)
                        ON CONFLICT(team_id,user_id,agent_id) DO NOTHING
                        """,
                        (
                            _id("asa"),
                            team_id,
                            user_id,
                            agent_id,
                            "team",
                            lease.snapshot_at,
                            lease.snapshot_at,
                            lease.snapshot_at,
                            lease.snapshot_at,
                            lease.snapshot_at,
                            lease.snapshot_at,
                        ),
                    )
                    if inserted_row.rowcount == 1:
                        inserted += 1
                    else:
                        conflict += 1
                last = batch[-1]
                renewed_at = _now()
                updated = await db.execute(
                    """
                    UPDATE agent_assignment_backfill_run SET
                      cursor_team_id=?,cursor_user_id=?,cursor_agent_id=?,
                      inserted_count=inserted_count+?,conflict_count=conflict_count+?,
                      updated_at=?,lease_expires_at=?
                    WHERE id=? AND status='running' AND owner_token=?
                    """,
                    (
                        str(last[0]),
                        str(last[1]),
                        str(last[2]),
                        inserted,
                        conflict,
                        renewed_at,
                        _lease_deadline(renewed_at),
                        lease.run_id,
                        lease.owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentAssignmentBackfillLeaseError("assignment backfill apply lease lost")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return True

    async def _complete_backfill_run(
        self,
        *,
        lease: _BackfillLease,
    ) -> tuple[int, int, str]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_running_backfill_conn(db, lease=lease)
                async with db.execute(
                    "SELECT * FROM agent_assignment_backfill_run WHERE id=? AND owner_token=?",
                    (lease.run_id, lease.owner_token),
                ) as cursor:
                    run = await cursor.fetchone()
                if run is None or run["snapshot_completed_at"] is None or not run["expected_digest"]:
                    raise RuntimeError("assignment backfill snapshot is incomplete")
                expected = int(run["expected_count"])
                inserted = int(run["inserted_count"])
                conflict = int(run["conflict_count"])
                digest = str(run["expected_digest"])
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM agent_assignment_backfill_expected AS expected
                    LEFT JOIN agent_assignment AS actual
                      ON actual.team_id=expected.team_id AND actual.user_id=expected.user_id
                     AND actual.agent_id=expected.agent_id
                     AND actual.personal_state='active' AND actual.access_state='granted'
                    WHERE expected.run_id=? AND actual.id IS NULL
                    """,
                    (lease.run_id,),
                ) as cursor:
                    missing_total = int((await cursor.fetchone())[0])
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM agent_assignment AS actual
                    LEFT JOIN agent_assignment_backfill_expected AS expected
                      ON expected.run_id=? AND expected.team_id=actual.team_id
                     AND expected.user_id=actual.user_id AND expected.agent_id=actual.agent_id
                    WHERE actual.personal_state='active' AND actual.access_state='granted'
                      AND expected.agent_id IS NULL
                    """,
                    (lease.run_id,),
                ) as cursor:
                    unexpected_total = int((await cursor.fetchone())[0])
                if missing_total or unexpected_total:
                    raise RuntimeError(
                        f"assignment backfill reconciliation failed: expected={expected} missing={missing_total} unexpected={unexpected_total}",
                    )
                completed_at = _monotonic_now(
                    str(run["started_at"]),
                    str(run["snapshot_at"]),
                    str(run["updated_at"]),
                    str(run["snapshot_completed_at"]),
                )
                completed_run = await db.execute(
                    """
                    UPDATE agent_assignment_backfill_run
                    SET status='completed',updated_at=?,finished_at=?,owner_token=NULL,lease_expires_at=NULL
                    WHERE id=? AND status='running' AND owner_token=?
                    """,
                    (completed_at, completed_at, lease.run_id, lease.owner_token),
                )
                if completed_run.rowcount != 1:
                    raise AgentAssignmentBackfillLeaseError("assignment backfill completion lease lost")
                completed_meta = await db.execute(
                    """
                    UPDATE agent_marketplace_schema_meta
                    SET assignment_cutover_complete_at=?,assignment_backfill_expected=?,
                        assignment_backfill_inserted=?,assignment_backfill_digest=?,updated_at=?
                    WHERE singleton=1 AND assignment_cutover_complete_at IS NULL
                    """,
                    (completed_at, expected, inserted, digest, completed_at),
                )
                if completed_meta.rowcount != 1:
                    raise AgentAssignmentCutoverCompleteError(
                        "assignment cutover completed while reconciliation was committing",
                    )
                async with db.execute(
                    """
                    SELECT team_id,COUNT(*) FROM agent_assignment_backfill_expected
                    WHERE run_id=? GROUP BY team_id ORDER BY team_id
                    """,
                    (lease.run_id,),
                ) as cursor:
                    team_count = await cursor.fetchone()
                    while team_count is not None:
                        team_id, expected_count = str(team_count[0]), int(team_count[1])
                        await self._write_audit_conn(
                            db,
                            actor_user_id="system-agent-backfill",
                            team_id=team_id,
                            action=AuditAction.AGENT_ASSIGNMENT_BACKFILLED,
                            agent_id="*",
                            assignment_id=lease.run_id,
                            payload={
                                "expected_count": expected_count,
                                "global_expected_count": expected,
                                "global_inserted_count": inserted,
                                "global_conflict_count": conflict,
                                "digest": digest,
                            },
                        )
                        team_count = await cursor.fetchone()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return inserted, conflict, completed_at

    async def _complete_legacy_v1_repair(
        self,
        *,
        lease: _BackfillLease,
    ) -> tuple[int, int, str]:
        """Commit a v2 receipt while preserving all v1 personal preferences."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_running_backfill_conn(db, lease=lease)
                async with db.execute(
                    "SELECT * FROM agent_assignment_backfill_run WHERE id=? AND owner_token=?",
                    (lease.run_id, lease.owner_token),
                ) as cursor:
                    run = await cursor.fetchone()
                if run is None or run["snapshot_completed_at"] is None or not run["expected_digest"]:
                    raise RuntimeError("legacy v1 repair snapshot is incomplete")
                expected = int(run["expected_count"])
                inserted = int(run["inserted_count"])
                conflict = int(run["conflict_count"])
                digest = str(run["expected_digest"])
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM agent_assignment_backfill_expected AS expected
                    LEFT JOIN agent_assignment AS actual
                      ON actual.team_id=expected.team_id AND actual.user_id=expected.user_id
                     AND actual.agent_id=expected.agent_id
                    WHERE expected.run_id=? AND actual.id IS NULL
                    """,
                    (lease.run_id,),
                ) as cursor:
                    missing_total = int((await cursor.fetchone())[0])
                if missing_total:
                    raise RuntimeError(
                        f"legacy v1 repair reconciliation failed: expected={expected} missing={missing_total}",
                    )
                async with db.execute(
                    """
                    SELECT assignment_cutover_complete_at
                    FROM agent_marketplace_schema_meta WHERE singleton=1
                    """,
                ) as cursor:
                    meta = await cursor.fetchone()
                if meta is None or not meta[0]:
                    raise AgentAssignmentUnavailableError(
                        "legacy v1 cutover marker disappeared during repair",
                    )
                completed_at = _monotonic_now(
                    str(run["started_at"]),
                    str(run["snapshot_at"]),
                    str(run["updated_at"]),
                    str(run["snapshot_completed_at"]),
                )
                completed_run = await db.execute(
                    """
                    UPDATE agent_assignment_backfill_run
                    SET status='completed',updated_at=?,finished_at=?,owner_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=? AND status='running' AND owner_token=?
                    """,
                    (completed_at, completed_at, lease.run_id, lease.owner_token),
                )
                if completed_run.rowcount != 1:
                    raise AgentAssignmentBackfillLeaseError(
                        "legacy v1 repair completion lease lost",
                    )
                await db.execute(
                    """
                    UPDATE agent_marketplace_schema_meta
                    SET assignment_backfill_expected=?,assignment_backfill_inserted=?,
                        assignment_backfill_digest=?,updated_at=?
                    WHERE singleton=1 AND assignment_cutover_complete_at IS NOT NULL
                    """,
                    (expected, inserted, digest, completed_at),
                )
                async with db.execute(
                    """
                    SELECT team_id,COUNT(*) FROM agent_assignment_backfill_expected
                    WHERE run_id=? GROUP BY team_id ORDER BY team_id
                    """,
                    (lease.run_id,),
                ) as cursor:
                    team_count = await cursor.fetchone()
                    while team_count is not None:
                        team_id, expected_count = str(team_count[0]), int(team_count[1])
                        await self._write_audit_conn(
                            db,
                            actor_user_id="system-agent-backfill",
                            team_id=team_id,
                            action=AuditAction.AGENT_ASSIGNMENT_BACKFILLED,
                            agent_id="*",
                            assignment_id=lease.run_id,
                            payload={
                                "repair_from_schema_version": 1,
                                "expected_count": expected_count,
                                "global_expected_count": expected,
                                "global_inserted_count": inserted,
                                "global_conflict_count": conflict,
                                "digest": digest,
                            },
                        )
                        team_count = await cursor.fetchone()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return inserted, conflict, str(meta[0])

    async def _require_running_backfill_conn(
        self,
        db: aiosqlite.Connection,
        *,
        lease: _BackfillLease,
    ) -> None:
        async with db.execute(
            """
            SELECT meta.assignment_cutover_complete_at,
                   EXISTS(
                     SELECT 1 FROM agent_assignment_backfill_run AS legacy
                     WHERE legacy.status='completed' AND legacy.snapshot_at IS NULL
                   )
            FROM agent_assignment_backfill_run AS run
            JOIN agent_marketplace_schema_meta AS meta ON meta.singleton=1
            WHERE run.id=? AND run.status='running' AND run.owner_token=?
            """,
            (lease.run_id, lease.owner_token),
        ) as cursor:
            row = await cursor.fetchone()
            normal_owned = row is not None and row[0] is None and not lease.legacy_v1_repair
            repair_owned = row is not None and bool(row[0]) and bool(row[1]) and lease.legacy_v1_repair
            if not (normal_owned or repair_owned):
                raise AgentAssignmentBackfillLeaseError(
                    "assignment backfill is no longer owned by this process",
                )

    async def _mark_backfill_failed(self, *, run_id: str, owner_token: str) -> None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT updated_at,snapshot_at FROM agent_assignment_backfill_run WHERE id=? AND status='running' AND owner_token=?",
                    (run_id, owner_token),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    timestamp = _monotonic_now(
                        str(row["updated_at"]),
                        str(row["snapshot_at"]),
                    )
                    await db.execute(
                        """
                        UPDATE agent_assignment_backfill_run
                        SET status='failed',error_count=error_count+1,updated_at=?,finished_at=?,
                            owner_token=NULL,lease_expires_at=NULL
                        WHERE id=? AND status='running' AND owner_token=?
                        """,
                        (timestamp, timestamp, run_id, owner_token),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def hire(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        agent_source_kind: Literal["team", "system"],
        live_access: bool,
        expected_agent_row_version: int | None = None,
    ) -> AgentAssignmentMutation:
        if not live_access:
            raise AgentAssignmentAccessError("agent is not currently hireable")
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_active_member_conn(db, team_id=team_id, user_id=user_id)
                if agent_source_kind == "team":
                    await self._require_live_team_access_locked(
                        team_id=team_id,
                        user_id=user_id,
                        agent_id=agent_id,
                    )
                    await self._require_enabled_team_agent_conn(db, team_id=team_id, agent_id=agent_id)
                    if expected_agent_row_version is not None:
                        async with db.execute(
                            "SELECT row_version FROM team_subagent WHERE team_id=? AND id=?",
                            (team_id, agent_id),
                        ) as cursor:
                            source_row = await cursor.fetchone()
                        if source_row is None or int(source_row[0]) != expected_agent_row_version:
                            raise AgentAssignmentAccessError("agent changed before hire commit")
                async with db.execute(
                    "SELECT * FROM agent_assignment WHERE team_id=? AND user_id=? AND agent_id=?",
                    (team_id, user_id, agent_id),
                ) as cursor:
                    current = await cursor.fetchone()
                if current and str(current["personal_state"]) != "removed" and str(current["access_state"]) == "granted":
                    await db.rollback()
                    return AgentAssignmentMutation(_assignment_from_row(current))

                if current:
                    await db.execute(
                        """
                        UPDATE agent_assignment
                        SET agent_source_kind=?, personal_state='active', access_state='granted',
                            source='user_hired', source_policy_version=NULL,
                            row_version=row_version+1, current_cycle_started_at=?,
                            personal_state_changed_at=?, access_state_changed_at=?,
                            removed_at=NULL, updated_at=?
                        WHERE id=?
                        """,
                        (agent_source_kind, timestamp, timestamp, timestamp, timestamp, str(current["id"])),
                    )
                    assignment_id = str(current["id"])
                else:
                    assignment_id = _id("asa")
                    await db.execute(
                        """
                        INSERT INTO agent_assignment(
                          id,team_id,user_id,agent_id,agent_source_kind,personal_state,access_state,
                          source,row_version,hired_at,current_cycle_started_at,personal_state_changed_at,
                          access_state_changed_at,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'active','granted','user_hired',1,?,?,?,?,?,?)
                        """,
                        (
                            assignment_id,
                            team_id,
                            user_id,
                            agent_id,
                            agent_source_kind,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                await db.execute(
                    """
                    UPDATE agent_default_opt_out
                    SET cleared_at=?, row_version=row_version+1
                    WHERE team_id=? AND user_id=? AND agent_id=? AND cleared_at IS NULL
                    """,
                    (timestamp, team_id, user_id, agent_id),
                )
                await self._write_audit_conn(
                    db,
                    actor_user_id=user_id,
                    team_id=team_id,
                    action=AuditAction.AGENT_HIRED,
                    agent_id=agent_id,
                    assignment_id=assignment_id,
                    payload={"source_kind": agent_source_kind},
                )
                assignment = await self._get_by_id_conn(db, assignment_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentAssignmentMutation(assignment)

    async def set_personal_state(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        personal_state: Literal["active", "disabled"],
        expected_row_version: int,
        live_access: bool,
        runnable: bool,
        expected_agent_row_version: int | None = None,
    ) -> AgentAssignmentMutation:
        if personal_state == "active" and (not live_access or not runnable):
            raise AgentAssignmentAccessError("agent cannot be enabled in its current state")
        timestamp = _now()
        paused_ids: tuple[str, ...] = ()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                if personal_state == "active":
                    await self._require_live_team_access_locked(
                        team_id=team_id,
                        user_id=user_id,
                        agent_id=agent_id,
                    )
                    if expected_agent_row_version is not None:
                        async with db.execute(
                            "SELECT row_version FROM team_subagent WHERE team_id=? AND id=?",
                            (team_id, agent_id),
                        ) as cursor:
                            source_row = await cursor.fetchone()
                        if source_row is None or int(source_row[0]) != expected_agent_row_version:
                            raise AgentAssignmentAccessError("agent changed before enable commit")
                current = await self._require_assignment_conn(db, team_id=team_id, user_id=user_id, agent_id=agent_id)
                if int(current["row_version"]) != expected_row_version:
                    raise AgentAssignmentVersionConflictError(int(current["row_version"]))
                if str(current["personal_state"]) == "removed":
                    raise AgentAssignmentNotFoundError("removed assignment must be hired again")
                if str(current["personal_state"]) == personal_state:
                    await db.rollback()
                    return AgentAssignmentMutation(_assignment_from_row(current))
                if personal_state == "disabled":
                    paused_ids = await self._pause_active_schedules_conn(
                        db,
                        team_id=team_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        pause_reason=f"agent_personal_disabled:{current['id']}",
                        timestamp=timestamp,
                    )
                await db.execute(
                    """
                    UPDATE agent_assignment
                    SET personal_state=?, personal_state_changed_at=?, updated_at=?, row_version=row_version+1
                    WHERE id=? AND row_version=?
                    """,
                    (personal_state, timestamp, timestamp, str(current["id"]), expected_row_version),
                )
                assignment = await self._get_by_id_conn(db, str(current["id"]))
                await self._write_audit_conn(
                    db,
                    actor_user_id=user_id,
                    team_id=team_id,
                    action=AuditAction.AGENT_PERSONAL_STATUS_CHANGED,
                    agent_id=agent_id,
                    assignment_id=assignment.id,
                    payload={"personal_state": personal_state, "paused_schedule_ids": list(paused_ids)},
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentAssignmentMutation(assignment, paused_schedule_ids=paused_ids)

    async def unhire_impact(self, *, team_id: str, user_id: str, agent_id: str) -> AgentUnhireImpact:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            current = await self._require_assignment_conn(db, team_id=team_id, user_id=user_id, agent_id=agent_id)
            return await self._impact_conn(db, assignment=current)

    async def unhire(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        expected_row_version: int,
        expected_impact_digest: str,
        schedule_action: Literal["pause", "expire"] | None,
    ) -> AgentAssignmentMutation:
        timestamp = _now()
        paused_ids: tuple[str, ...] = ()
        expired_ids: tuple[str, ...] = ()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await self._require_assignment_conn(db, team_id=team_id, user_id=user_id, agent_id=agent_id)
                if int(current["row_version"]) != expected_row_version:
                    raise AgentAssignmentVersionConflictError(int(current["row_version"]))
                impact = await self._impact_conn(db, assignment=current)
                if impact.digest != expected_impact_digest:
                    raise AgentAssignmentScheduleConflictError("schedule_impact_changed", impact)
                if impact.running_count:
                    raise AgentAssignmentScheduleConflictError("schedule_running", impact)
                if impact.active_count and schedule_action is None:
                    raise AgentAssignmentScheduleConflictError("schedule_action_required", impact)
                active_ids = tuple(item.id for item in impact.jobs if item.status == "active")
                if active_ids:
                    if schedule_action == "pause":
                        paused_ids = await self._set_schedule_status_conn(
                            db,
                            job_ids=active_ids,
                            status="paused",
                            pause_reason=f"agent_unhired:{current['id']}",
                            timestamp=timestamp,
                        )
                    else:
                        expired_ids = await self._set_schedule_status_conn(
                            db,
                            job_ids=active_ids,
                            status="expired",
                            pause_reason=f"agent_unhired:{current['id']}",
                            timestamp=timestamp,
                        )
                await db.execute(
                    """
                    UPDATE agent_assignment
                    SET personal_state='removed', removed_at=?, personal_state_changed_at=?,
                        updated_at=?, row_version=row_version+1
                    WHERE id=? AND row_version=?
                    """,
                    (timestamp, timestamp, timestamp, str(current["id"]), expected_row_version),
                )
                async with db.execute(
                    "SELECT policy_version,enabled FROM agent_default_policy WHERE team_id=? AND agent_id=?",
                    (team_id, agent_id),
                ) as cursor:
                    default_row = await cursor.fetchone()
                preserves_default_opt_out = bool(
                    default_row and (bool(default_row["enabled"]) or str(current["source"]) == "former_team_default"),
                )
                if preserves_default_opt_out:
                    await db.execute(
                        """
                        INSERT INTO agent_default_opt_out(
                          team_id,user_id,agent_id,policy_version_at_opt_out,opted_out_at,cleared_at,row_version
                        ) VALUES (?,?,?,?,?,NULL,1)
                        ON CONFLICT(team_id,user_id,agent_id) DO UPDATE SET
                          policy_version_at_opt_out=excluded.policy_version_at_opt_out,
                          opted_out_at=excluded.opted_out_at,
                          cleared_at=NULL,
                          row_version=agent_default_opt_out.row_version+1
                        """,
                        (
                            team_id,
                            user_id,
                            agent_id,
                            int(default_row["policy_version"]),
                            timestamp,
                        ),
                    )
                assignment = await self._get_by_id_conn(db, str(current["id"]))
                await self._write_audit_conn(
                    db,
                    actor_user_id=user_id,
                    team_id=team_id,
                    action=AuditAction.AGENT_UNHIRED,
                    agent_id=agent_id,
                    assignment_id=assignment.id,
                    payload={
                        "paused_schedule_ids": list(paused_ids),
                        "expired_schedule_ids": list(expired_ids),
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentAssignmentMutation(
            assignment,
            paused_schedule_ids=paused_ids,
            expired_schedule_ids=expired_ids,
        )

    async def _get_by_id_conn(self, db: aiosqlite.Connection, assignment_id: str) -> AgentAssignment:
        async with db.execute("SELECT * FROM agent_assignment WHERE id=?", (assignment_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentAssignmentNotFoundError(assignment_id)
        return _assignment_from_row(row)

    async def _backfill_name_claims_conn(self, db: aiosqlite.Connection, *, timestamp: str) -> None:
        from bowhead_core.system_agent_marketplace import (
            system_agent_default_display_name,
            system_reserved_display_names,
        )

        try:
            from bowhead_core.agents.registry import AgentRegistry

            registry = AgentRegistry.bootstrap()
            system_names = list(registry.list_all())
        except Exception:
            system_names = []
        # Suspended teams are recoverable. Reserve their system names too so a
        # later reactivation cannot surface a collision created during cutover.
        async with db.execute("SELECT id FROM teams ORDER BY id") as team_cursor:
            team_row = await team_cursor.fetchone()
            while team_row is not None:
                team_id = str(team_row[0])
                reserved_names = set(system_reserved_display_names())
                for system_name in system_names:
                    reserved_names.add(system_name)
                    reserved_names.add(system_name.replace("-", " ").title())
                    reserved_names.add(system_agent_default_display_name(system_name))
                async with db.execute(
                    """
                    SELECT display_name FROM team_builtin_subagent_override
                    WHERE team_id=? AND display_name IS NOT NULL ORDER BY name
                    """,
                    (team_id,),
                ) as override_cursor:
                    while override := await override_cursor.fetchone():
                        if str(override[0] or "").strip():
                            reserved_names.add(str(override[0]))
                for visible_name in sorted(reserved_names):
                    normalized = " ".join(
                        unicodedata.normalize("NFKC", visible_name).strip().split(),
                    ).casefold()
                    key = hashlib.sha256(normalized.encode()).hexdigest()
                    await db.execute(
                        """
                        INSERT INTO agent_name_claim(
                          team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                        ) VALUES (?,'*',?,'system',?,NULL,?)
                        ON CONFLICT(team_id,locale,normalized_key) DO UPDATE SET
                          holder_kind='system',holder_id=excluded.holder_id,
                          agent_id=NULL,claimed_at=excluded.claimed_at
                        """,
                        (team_id, key, f"system:{key[:20]}", timestamp),
                    )
                team_row = await team_cursor.fetchone()

        await db.execute(
            """
            CREATE TEMP TABLE legacy_agent_name_stage(
              team_id TEXT NOT NULL,normalized_key TEXT NOT NULL,agent_id TEXT NOT NULL,
              PRIMARY KEY(team_id,normalized_key,agent_id)
            ) WITHOUT ROWID
            """,
        )
        async with db.execute(
            "SELECT team_id,id,COALESCE(NULLIF(display_name,''),name) FROM team_subagent ORDER BY team_id,id",
        ) as agent_cursor:
            while rows := await agent_cursor.fetchmany(250):
                await db.executemany(
                    "INSERT INTO legacy_agent_name_stage(team_id,normalized_key,agent_id) VALUES (?,?,?)",
                    (
                        (
                            str(row[0]),
                            hashlib.sha256(
                                " ".join(
                                    unicodedata.normalize("NFKC", str(row[2])).strip().split(),
                                )
                                .casefold()
                                .encode(),
                            ).hexdigest(),
                            str(row[1]),
                        )
                        for row in rows
                    ),
                )
        async with db.execute(
            """
            SELECT team_id,normalized_key,COUNT(*)
            FROM legacy_agent_name_stage
            GROUP BY team_id,normalized_key ORDER BY team_id,normalized_key
            """,
        ) as group_cursor:
            group = await group_cursor.fetchone()
            while group is not None:
                team_id, key, agent_count = str(group[0]), str(group[1]), int(group[2])
                async with db.execute(
                    """
                    SELECT agent_id FROM legacy_agent_name_stage
                    WHERE team_id=? AND normalized_key=? ORDER BY agent_id LIMIT 1
                    """,
                    (team_id, key),
                ) as cursor:
                    first_agent = await cursor.fetchone()
                first_agent_id = str(first_agent[0]) if first_agent else ""
                async with db.execute(
                    """
                    SELECT holder_kind,agent_id FROM agent_name_claim
                    WHERE team_id=? AND locale='*' AND normalized_key=?
                    """,
                    (team_id, key),
                ) as cursor:
                    existing_claim = await cursor.fetchone()
                existing_kind = str(existing_claim["holder_kind"]) if existing_claim else None
                existing_agent_id = str(existing_claim["agent_id"] or "") if existing_claim else ""
                if agent_count == 1 and (
                    existing_claim is None or existing_kind == "legacy_conflict" or (existing_kind == "agent" and existing_agent_id == first_agent_id)
                ):
                    await db.execute(
                        """
                        INSERT INTO agent_name_claim(
                          team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                        ) VALUES (?,'*',?,'agent',?,?,?)
                        ON CONFLICT(team_id,locale,normalized_key) DO UPDATE SET
                          holder_kind='agent',holder_id=excluded.holder_id,
                          agent_id=excluded.agent_id,claimed_at=excluded.claimed_at
                        """,
                        (team_id, key, first_agent_id, first_agent_id, timestamp),
                    )
                    await db.execute(
                        """
                        UPDATE agent_name_conflict_group SET resolved_at=?
                        WHERE team_id=? AND normalized_key=? AND resolved_at IS NULL
                        """,
                        (timestamp, team_id, key),
                    )
                else:
                    group_id = f"legacy:{key[:20]}"
                    if existing_claim is None:
                        await db.execute(
                            """
                            INSERT INTO agent_name_claim(
                              team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                            ) VALUES (?,'*',?,'legacy_conflict',?,NULL,?)
                            """,
                            (team_id, key, group_id, timestamp),
                        )
                    elif existing_kind == "agent":
                        async with db.execute(
                            """
                            SELECT 1 FROM legacy_agent_name_stage
                            WHERE team_id=? AND normalized_key=? AND agent_id=?
                            """,
                            (team_id, key, existing_agent_id),
                        ) as cursor:
                            held_by_legacy_agent = await cursor.fetchone()
                        if held_by_legacy_agent:
                            await db.execute(
                                """
                                UPDATE agent_name_claim
                                SET holder_kind='legacy_conflict',holder_id=?,agent_id=NULL,claimed_at=?
                                WHERE team_id=? AND locale='*' AND normalized_key=?
                                """,
                                (group_id, timestamp, team_id, key),
                            )
                    async with db.execute(
                        """
                        SELECT agent_id FROM legacy_agent_name_stage
                        WHERE team_id=? AND normalized_key=? ORDER BY agent_id
                        """,
                        (team_id, key),
                    ) as agent_ids_cursor:
                        agent_id_row = await agent_ids_cursor.fetchone()
                        while agent_id_row is not None:
                            await db.execute(
                                """
                                INSERT INTO agent_name_conflict_group(
                                  team_id,normalized_key,agent_id,detected_at
                                ) VALUES (?,?,?,?)
                                ON CONFLICT(team_id,normalized_key,agent_id)
                                DO UPDATE SET resolved_at=NULL
                                """,
                                (team_id, key, str(agent_id_row[0]), timestamp),
                            )
                            agent_id_row = await agent_ids_cursor.fetchone()
                group = await group_cursor.fetchone()
        await db.execute("DROP TABLE legacy_agent_name_stage")

    async def _active_member_count_conn(self, db: aiosqlite.Connection, *, team_id: str) -> int:
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM users AS u
            JOIN team_members AS tm
              ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            WHERE u.status='active'
            """,
            (team_id,),
        ) as cursor:
            return int((await cursor.fetchone())[0])

    async def _require_team_admin_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
    ) -> None:
        async with db.execute(
            """
            SELECT 1
            FROM teams AS t
            JOIN users AS u ON u.id=? AND u.status='active'
            LEFT JOIN team_members AS tm
              ON tm.team_id=t.id AND tm.user_id=u.id AND tm.status='active'
            WHERE t.id=? AND t.status='active'
              AND (t.created_by_user_id=u.id OR tm.role='team_admin')
            """,
            (user_id, team_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentAssignmentAccessError("team admin required")

    async def _require_teamwide_agent_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        expected_row_version: int | None = None,
    ) -> None:
        version_clause = "AND sa.row_version=?" if expected_row_version is not None else ""
        params: list[object] = [team_id, agent_id]
        if expected_row_version is not None:
            params.append(expected_row_version)
        async with db.execute(
            f"""
            SELECT 1
            FROM team_subagent AS sa
            JOIN users AS owner
              ON owner.id=COALESCE(sa.owner_user_id,sa.created_by)
             AND owner.status='active'
            JOIN team_members AS member
              ON member.user_id=owner.id AND member.team_id=sa.team_id
             AND member.status='active'
            WHERE sa.team_id=? AND sa.id=? AND sa.status='enabled'
              AND sa.visibility='team_wide'
              AND COALESCE(sa.lifecycle_state,'active')='active'
              {version_clause}
            """,  # noqa: S608 -- the optional version clause is a fixed server literal
            params,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentAssignmentAccessError("team default requires an enabled team-wide agent")

    async def _apply_default_assignments_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        policy_version: int,
        timestamp: str,
        user_id: str | None = None,
    ) -> tuple[str, ...]:
        members = await self._default_candidate_members_conn(db, team_id=team_id, agent_id=agent_id, user_id=user_id)
        affected_ids: list[str] = []
        for member in members:
            assignment_id = str(member["assignment_id"] or _id("asa"))
            await db.execute(
                """
                INSERT INTO agent_assignment(
                  id,team_id,user_id,agent_id,agent_source_kind,personal_state,access_state,
                  source,source_policy_version,row_version,hired_at,current_cycle_started_at,
                  personal_state_changed_at,access_state_changed_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,'active','granted','team_default',?,1,?,?,?,?,?,?)
                ON CONFLICT(team_id,user_id,agent_id) DO UPDATE SET
                  personal_state='active',access_state='granted',source='team_default',
                  source_policy_version=excluded.source_policy_version,
                  row_version=agent_assignment.row_version+1,
                  current_cycle_started_at=excluded.current_cycle_started_at,
                  personal_state_changed_at=excluded.personal_state_changed_at,
                  access_state_changed_at=excluded.access_state_changed_at,
                  removed_at=NULL,updated_at=excluded.updated_at
                """,
                (
                    assignment_id,
                    team_id,
                    str(member["user_id"]),
                    agent_id,
                    "team",
                    policy_version,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            affected_ids.append(assignment_id)
        return tuple(affected_ids)

    async def _require_assignment_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
    ) -> aiosqlite.Row:
        async with db.execute(
            "SELECT * FROM agent_assignment WHERE team_id=? AND user_id=? AND agent_id=?",
            (team_id, user_id, agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or str(row["personal_state"]) == "removed":
            raise AgentAssignmentNotFoundError(agent_id)
        return row

    async def _eligible_default_policies_conn(self, db: aiosqlite.Connection, *, team_id: str) -> list[aiosqlite.Row]:
        async with db.execute(
            """
            SELECT p.agent_id,p.policy_version
            FROM agent_default_policy AS p
            JOIN team_subagent AS sa
              ON sa.team_id=p.team_id AND sa.id=p.agent_id
            WHERE p.team_id=? AND p.enabled=1
              AND sa.status='enabled' AND sa.visibility='team_wide'
              AND COALESCE(sa.lifecycle_state,'active')='active'
            ORDER BY p.agent_id
            """,
            (team_id,),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _default_candidate_members_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        user_id: str | None,
    ) -> list[aiosqlite.Row]:
        """Members a team default would (re)assign: active, not opted out, not already holding a live row."""
        user_clause = "AND u.id=?" if user_id is not None else ""
        params: list[Any] = [team_id, agent_id, agent_id]
        if user_id is not None:
            params.append(user_id)
        async with db.execute(
            f"""
            SELECT u.id AS user_id,aa.id AS assignment_id
            FROM users AS u
            JOIN team_members AS tm
              ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            LEFT JOIN agent_assignment AS aa
              ON aa.team_id=tm.team_id AND aa.user_id=u.id AND aa.agent_id=?
            LEFT JOIN agent_default_opt_out AS opt
              ON opt.team_id=tm.team_id AND opt.user_id=u.id AND opt.agent_id=?
             AND opt.cleared_at IS NULL
            WHERE u.status='active' AND opt.user_id IS NULL
              AND (aa.id IS NULL OR aa.personal_state='removed')
              {user_clause}
            ORDER BY u.id
            """,  # noqa: S608 -- the optional clause is a fixed server literal
            params,
        ) as cursor:
            return list(await cursor.fetchall())

    async def admissible_agent_ids(
        self,
        *,
        team_id: str,
        user_id: str,
        materialize_team_defaults: bool = True,
    ) -> frozenset[str]:
        """Team Agents this user's assignment admits (``ADMISSIBLE_ASSIGNMENT_SQL``).

        Execution callers keep the default: pending team defaults are written first, exactly as
        before. ``materialize_team_defaults=False`` answers the same question without writing — a
        pending default counts when it would be created and its live access passes — so a read-only
        explainer can ask it for any number of users.
        """
        if materialize_team_defaults:
            await self.apply_team_defaults_for_user(team_id=team_id, user_id=user_id)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT agent_id FROM agent_assignment WHERE team_id=? AND user_id=? AND {ADMISSIBLE_ASSIGNMENT_SQL}",  # noqa: S608 -- fixed server literal
                (team_id, user_id),
            ) as cursor:
                admitted = {str(row[0]) for row in await cursor.fetchall()}
            if materialize_team_defaults:
                return frozenset(admitted)
            try:
                await self._require_active_member_conn(db, team_id=team_id, user_id=user_id)
            except AgentAssignmentAccessError:
                return frozenset(admitted)
            pending = [
                str(policy["agent_id"])
                for policy in await self._eligible_default_policies_conn(db, team_id=team_id)
                if str(policy["agent_id"]) not in admitted
                and await self._default_candidate_members_conn(db, team_id=team_id, agent_id=str(policy["agent_id"]), user_id=user_id)
            ]
        if pending:
            from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

            resolver = TeamSubagentAccessResolver(self.db_path)
            for agent_id in pending:
                decision = await resolver.resolve(team_id=team_id, actor_user_id=user_id, subagent_id=agent_id)
                if decision is not None and decision.resolution.can_consume:
                    admitted.add(agent_id)
        return frozenset(admitted)

    async def _require_active_member_conn(self, db: aiosqlite.Connection, *, team_id: str, user_id: str) -> None:
        async with db.execute(
            """
            SELECT 1 FROM users AS u
            JOIN team_members AS tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            WHERE u.id=? AND u.status='active'
            """,
            (team_id, user_id),
        ) as cursor:
            active = await cursor.fetchone()
        if active is None:
            raise AgentAssignmentAccessError("active team membership required")

    async def _require_enabled_team_agent_conn(self, db: aiosqlite.Connection, *, team_id: str, agent_id: str) -> None:
        async with db.execute(
            "SELECT 1 FROM team_subagent WHERE team_id=? AND id=? AND status='enabled'",
            (team_id, agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentAssignmentAccessError("agent is not enabled")

    async def _require_live_team_access_locked(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
    ) -> None:
        """Re-read canonical access while this Store holds the write lock.

        The resolver uses a read connection, but BEGIN IMMEDIATE prevents a
        concurrent audience/status writer from slipping between this decision
        and the assignment mutation.
        """
        from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

        decision = await TeamSubagentAccessResolver(self.db_path).resolve(
            team_id=team_id,
            actor_user_id=user_id,
            subagent_id=agent_id,
        )
        if decision is None or not decision.resolution.can_consume:
            raise AgentAssignmentAccessError("agent access changed before assignment commit")

    async def _require_all_active_members_live_access_locked(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
    ) -> None:
        async with db.execute(
            """
            SELECT u.id
            FROM users AS u
            JOIN team_members AS tm
              ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            WHERE u.status='active'
            ORDER BY u.id
            """,
            (team_id,),
        ) as cursor:
            user_ids = tuple(str(row[0]) for row in await cursor.fetchall())
        for user_id in user_ids:
            await self._require_live_team_access_locked(
                team_id=team_id,
                user_id=user_id,
                agent_id=agent_id,
            )

    async def _impact_conn(self, db: aiosqlite.Connection, *, assignment: aiosqlite.Row) -> AgentUnhireImpact:
        if not await self._has_recipe_ref_conn(db):
            rows: list[aiosqlite.Row] = []
        else:
            async with db.execute(
                """
                SELECT j.id,j.name,j.status,j.updated_at,
                       EXISTS(
                         SELECT 1 FROM scheduled_job_runs AS r
                         WHERE r.job_id=j.id AND r.status IN ('running','awaiting_input')
                       ) AS has_in_flight_run
                FROM scheduled_jobs AS j
                JOIN workspaces AS w ON w.id=j.workspace_id AND w.team_id=?
                WHERE j.user_id=?
                  AND json_valid(j.recipe_ref)
                  AND json_extract(j.recipe_ref,'$.form')='team_subagent'
                  AND (
                    json_extract(j.recipe_ref,'$.id_or_name')=?
                    OR json_extract(j.recipe_ref,'$.id_or_name')=(
                      SELECT name FROM team_subagent WHERE id=?
                    )
                  )
                ORDER BY j.id
                """,
                (
                    str(assignment["team_id"]),
                    str(assignment["user_id"]),
                    str(assignment["agent_id"]),
                    str(assignment["agent_id"]),
                ),
            ) as cursor:
                rows = await cursor.fetchall()
        jobs = tuple(
            AgentScheduleImpactItem(
                id=str(row["id"]),
                name=str(row["name"]),
                status=str(row["status"]),
                updated_at=str(row["updated_at"]),
                has_in_flight_run=bool(row["has_in_flight_run"]),
            )
            for row in rows
        )
        digest_payload = {
            "assignment_id": str(assignment["id"]),
            "assignment_row_version": int(assignment["row_version"]),
            "jobs": [[item.id, item.status, item.updated_at, item.has_in_flight_run] for item in jobs],
        }
        digest = hashlib.sha256(
            json.dumps(digest_payload, separators=(",", ":"), sort_keys=True).encode(),
        ).hexdigest()
        return AgentUnhireImpact(
            assignment_id=str(assignment["id"]),
            assignment_row_version=int(assignment["row_version"]),
            jobs=jobs,
            digest=digest,
        )

    async def _has_recipe_ref_conn(self, db: aiosqlite.Connection) -> bool:
        async with db.execute("PRAGMA table_info(scheduled_jobs)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        return "recipe_ref" in columns

    async def _pause_active_schedules_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        pause_reason: str,
        timestamp: str,
    ) -> tuple[str, ...]:
        if not await self._has_recipe_ref_conn(db):
            return ()
        async with db.execute(
            """
            SELECT j.id FROM scheduled_jobs AS j
            JOIN workspaces AS w ON w.id=j.workspace_id AND w.team_id=?
            WHERE j.user_id=? AND j.status='active' AND json_valid(j.recipe_ref)
              AND json_extract(j.recipe_ref,'$.form')='team_subagent'
              AND (
                json_extract(j.recipe_ref,'$.id_or_name')=?
                OR json_extract(j.recipe_ref,'$.id_or_name')=(
                  SELECT name FROM team_subagent WHERE id=?
                )
              )
            ORDER BY j.id
            """,
            (team_id, user_id, agent_id, agent_id),
        ) as cursor:
            job_ids = tuple(str(row[0]) for row in await cursor.fetchall())
        return await self._set_schedule_status_conn(
            db,
            job_ids=job_ids,
            status="paused",
            pause_reason=pause_reason,
            timestamp=timestamp,
        )

    async def _set_schedule_status_conn(
        self,
        db: aiosqlite.Connection,
        *,
        job_ids: tuple[str, ...],
        status: Literal["paused", "expired"],
        pause_reason: str,
        timestamp: str,
    ) -> tuple[str, ...]:
        if not job_ids:
            return ()
        placeholders = ",".join("?" for _ in job_ids)
        await db.execute(
            f"""
            UPDATE scheduled_jobs SET status=?,pause_reason=?,updated_at=?
            WHERE id IN ({placeholders}) AND status='active'
            """,  # noqa: S608 -- placeholders are generated from a bounded, server-read tuple
            (status, pause_reason, timestamp, *job_ids),
        )
        return job_ids

    async def _write_audit_conn(
        self,
        db: aiosqlite.Connection,
        *,
        actor_user_id: str,
        team_id: str,
        action: str,
        agent_id: str,
        assignment_id: str,
        payload: dict[str, Any],
    ) -> None:
        await UserStore(self.db_path).write_admin_audit_log(
            actor_user_id=actor_user_id,
            team_id=team_id,
            action=action,
            target_kind="agent_assignment",
            target_id=assignment_id,
            target_team_id=team_id,
            scope="user",
            payload={"agent_id": agent_id, **payload},
            conn=db,
        )


__all__ = [
    "AgentAssignment",
    "AgentAssignmentAccessError",
    "AgentAssignmentBackfillResult",
    "AgentAssignmentError",
    "AgentAssignmentMutation",
    "AgentAssignmentNotFoundError",
    "AgentAssignmentScheduleConflictError",
    "AgentAssignmentStore",
    "AgentAssignmentUnavailableError",
    "AgentAssignmentVersionConflictError",
    "AgentDefaultPolicy",
    "AgentDefaultPreview",
    "AgentScheduleImpactItem",
    "AgentUnhireImpact",
]
