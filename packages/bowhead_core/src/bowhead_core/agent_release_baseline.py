"""Recoverable, explicit import of legacy mutable Agents into release history.

The importer is intentionally absent from application startup.  ``preview`` is
read-only, ``stage`` freezes and compiles a shadow inventory, and ``apply``
requires both the staged digest and an operator assertion that legacy writers
are stopped.

``apply`` DOES activate the revisioned runtime gate as its final step: once every
legacy Agent reconciles it sets ``team_subagent_schema_meta.release_semantics_activated_at``
and raises the minimum reader/writer version to ``AGENT_RELEASE_COMPATIBILITY_VERSION``
(see ``_finish_apply_conn``).  Two consequences an operator has to plan for:

* the cutover is one-way for the running fleet -- binaries older than that
  compatibility floor can no longer start or write;
* from that moment every Agent must be revisioned, so the dispatch compatibility
  path stops accepting legacy rows.

This paragraph used to claim the opposite ("It does not activate the revisioned
runtime reader/writer gate"), which sent one root-cause analysis down the wrong
branch entirely.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_materializer import AgentBehaviorMaterializer, default_agent_behavior_materializer
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.agents.exceptions import ManifestSecurityError
from bowhead_core.agents.manifest_security import lint_user_append
from bowhead_core.team_subagent_store import (
    AGENT_RELEASE_COMPATIBILITY_VERSION,
    TeamSubagentNameConflictError,
    effective_agent_name_key,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

BASELINE_COMPILER_VERSION = "agent-legacy-baseline-v1"
BASELINE_SEMANTIC_SCANNER_VERSION = "none"
BASELINE_ACTOR = "system:m2-baseline-import"
_LEASE_DURATION = timedelta(minutes=5)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class AgentReleaseBaselineError(RuntimeError):
    """The requested baseline operation cannot safely continue."""


class AgentReleaseBaselineDriftError(AgentReleaseBaselineError):
    """A frozen legacy source changed before staging or apply completed."""


@dataclass(frozen=True, slots=True)
class AgentReleaseBaselineSummary:
    run_id: str | None
    state: str
    expected: int
    staged: int
    ready: int
    degraded: int
    blocked: int
    imported: int
    digest: str


@dataclass(frozen=True, slots=True)
class AgentReleaseBaselineIssue:
    team_id: str
    agent_id: str
    state: str
    reason_codes: tuple[str, ...]
    imported: bool


logger = logging.getLogger(__name__)

# The baseline's scope, spelled once.  "Which rows does this import own" was
# previously written out in four separate queries (inventory, staging insert,
# final reconciliation, drift check); quarantining an Agent then made them
# disagree and every run aborted with a drift error.
_BASELINE_SCOPE_SQL = "definition_mode='legacy_mutable' AND lifecycle_state<>'quarantined'"


# A blocked Agent is either unsafe to grandfather, or evidence that our own
# backfill left its row wrong.  Automatic cutover must treat those differently:
# quarantining a customer's working Agent because *we* never wrote its name claim
# would repeat the class of bug that produced `name_claim_missing` in the first place.
_DATA_INTEGRITY_BLOCKED_REASONS = frozenset(
    {
        "legacy_release_state_inconsistent",
        "name_claim_invalid",
        "name_claim_missing",
        "name_claim_owner_mismatch",
    },
)
_SAFETY_BLOCKED_REASONS = frozenset({"prompt_injection_pattern", "prompt_safety_check_failed"})


def _is_safety_blocked(reason_codes: tuple[str, ...]) -> bool:
    """Whether this Agent is blocked because its definition is unsafe.

    Listing-safety findings arrive prefixed (`listing_<code>`), so they are matched
    by prefix rather than enumerated.
    """
    return any(code in _SAFETY_BLOCKED_REASONS or code.startswith("listing_") for code in reason_codes)


def _summary_from_run(run: aiosqlite.Row) -> AgentReleaseBaselineSummary:
    return AgentReleaseBaselineSummary(
        run_id=str(run["id"]),
        state=str(run["state"]),
        expected=int(run["expected_count"]),
        staged=int(run["staged_count"]),
        ready=int(run["ready_count"]),
        degraded=int(run["degraded_count"]),
        blocked=int(run["blocked_count"]),
        imported=int(run["imported_count"]),
        digest=str(run["expected_digest"] or ""),
    )


@dataclass(frozen=True, slots=True)
class _CompiledLegacyAgent:
    team_id: str
    agent_id: str
    source_row_version: int
    source_updated_at: str
    source_hash: str
    definition_json: str
    listing_json: str
    definition_hash: str
    safety_content_hash: str
    dependency_fingerprint: str
    capability_fingerprint: str
    audience_snapshot_json: str
    audience_hash: str
    safety_report_id: str
    imported_revision_id: str
    safety_status: str
    stage_state: str
    reason_codes: tuple[str, ...]
    deterministic_json: str
    semantic_json: str
    safe_summary_json: str
    risk_json: str


class AgentReleaseBaselineStore:
    """Three-step legacy baseline workflow: preview, stage, explicit apply."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Clock | None = None,
        behavior_materializer: AgentBehaviorMaterializer | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._clock = clock or SystemClock()
        self._behavior_materializer = behavior_materializer or default_agent_behavior_materializer()

    async def ensure_release_baseline(
        self,
        *,
        source_build: str | None = None,
        batch_size: int = 100,
    ) -> AgentReleaseBaselineSummary:
        """Idempotently complete the release cutover, or explain why it was skipped.

        Same shape as the M1 ``ensure_marketplace_cutover``: safe to call on every
        boot, no-op once the marker is set, and it never leaves the deployment half
        migrated.  Startup is the natural writer-stop window -- compose stops the
        previous app writer before this process starts -- which is what makes the
        offline three-step flow safe to drive automatically.

        Blocked Agents are split by *why* they are blocked:

        * **safety** (prompt injection pattern, blocked listing, failed prompt
          check) -- quarantined, then the rest of the inventory migrates.  Grand-
          fathering an unsafe definition into an immutable release is not something
          an unattended boot should do.
        * **data integrity** (missing/invalid/mismatched name claim, inconsistent
          legacy release pointers) -- the cutover is skipped entirely and reported.
          Those indicate *our* backfill left the row wrong. The affected legacy
          Agents remain unavailable for runtime use until the data is repaired;
          startup and management continue so the repair can be performed.

        Returns the completed summary, or a summary with ``state='skipped'`` and the
        offending reason codes.  It does not raise for a skip: an un-migrated
        deployment is a supported, working state.
        """
        await self._prepare()
        if await self._release_semantics_active():
            completed = await self._completed_run_summary()
            if completed is not None:
                return completed
            return await self._summary_skipped(("release_semantics_active_without_run",))

        snapshot_at = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            compiled = [await self._compile_conn(db, row=row, snapshot_at=snapshot_at) for row in await self._legacy_rows_conn(db)]

        blocked = [item for item in compiled if item.stage_state == "blocked"]
        data_blocked = [item for item in blocked if not _is_safety_blocked(item.reason_codes)]
        if data_blocked:
            reasons = sorted({code for item in data_blocked for code in item.reason_codes if code in _DATA_INTEGRITY_BLOCKED_REASONS})
            logger.warning(
                "agent_release_baseline.skipped agents=%d reasons=%s",
                len(data_blocked),
                ",".join(reasons),
            )
            return await self._summary_skipped(tuple(reasons))

        # A previous boot may have staged a run and then skipped.  Leaving it open
        # makes the next `stage()` raise "another baseline run is already open" --
        # under start-app.sh's `set -eu` that is a container that will not boot.
        await self._abandon_open_run()

        for item in blocked:
            await self._quarantine_unsafe(item)
        if blocked:
            logger.warning(
                "agent_release_baseline.quarantined agents=%d",
                len(blocked),
            )

        staged = await self.stage(source_build=source_build, batch_size=batch_size)
        if staged.blocked:
            return await self._summary_skipped(("blocked_after_quarantine",))
        return await self.apply(
            run_id=str(staged.run_id),
            expected_digest=staged.digest,
            writer_stopped=True,
            batch_size=batch_size,
        )

    async def _abandon_open_run(self) -> None:
        """Clear any run left open by an earlier boot so staging can start clean.

        Only ``staging``/``staged`` runs are abandoned: they hold no live pointers,
        so discarding them loses nothing.  An ``importing`` run is left alone --
        that one has already written revisions and must be resumed, not discarded.
        """
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,state FROM agent_release_baseline_run WHERE state IN ('staging','staged')",
            ) as cursor:
                rows = await cursor.fetchall()
        for row in rows:
            logger.warning("agent_release_baseline.abandoning_stale_run run=%s state=%s", row["id"], row["state"])
            with contextlib.suppress(AgentReleaseBaselineError):
                await self.abandon(run_id=str(row["id"]))

    async def _release_semantics_active(self) -> bool:
        from bowhead_core.team_subagent_store import release_semantics_active_conn

        async with open_shared_ctx(self.db_path) as db:
            return await release_semantics_active_conn(db)

    async def _completed_run_summary(self) -> AgentReleaseBaselineSummary | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_release_baseline_run WHERE state='completed' ORDER BY finished_at DESC LIMIT 1",
            ) as cursor:
                run = await cursor.fetchone()
        return _summary_from_run(run) if run is not None else None

    async def _summary_skipped(self, reason_codes: tuple[str, ...]) -> AgentReleaseBaselineSummary:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await self._legacy_rows_conn(db)
        return AgentReleaseBaselineSummary(
            run_id=None,
            state="skipped",
            expected=len(rows),
            staged=0,
            ready=0,
            degraded=0,
            blocked=len(rows),
            imported=0,
            digest=",".join(reason_codes),
        )

    async def _quarantine_unsafe(self, compiled: _CompiledLegacyAgent) -> None:
        timestamp = self._now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE team_subagent
                    SET status='disabled',lifecycle_state='quarantined',
                        quarantine_started_at=?,quarantine_reason=?,
                        quarantined_owner_user_id=COALESCE(owner_user_id,created_by),
                        row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND id=? AND lifecycle_state<>'quarantined'
                    """,
                    (
                        timestamp,
                        "agent_release_baseline_unsafe_definition",
                        timestamp,
                        compiled.team_id,
                        compiled.agent_id,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def preview(self) -> AgentReleaseBaselineSummary:
        """Compile all legacy Agents without changing the database."""
        await self._prepare()
        snapshot_at = self._now()
        compiled: list[_CompiledLegacyAgent] = []
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await self._legacy_rows_conn(db)
            for row in rows:
                compiled.append(await self._compile_conn(db, row=row, snapshot_at=snapshot_at))
        return self._summary_from_compiled(compiled, run_id=None, state="preview")

    async def stage(
        self,
        *,
        run_id: str | None = None,
        source_build: str | None = None,
        batch_size: int = 100,
    ) -> AgentReleaseBaselineSummary:
        """Freeze and durably compile a shadow inventory without live pointers."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        await self._prepare()
        effective_run_id = run_id or f"abr_{uuid4().hex}"
        lease_owner = f"baseline-stage:{uuid4().hex}"
        staged = await self._create_or_claim_stage(
            run_id=effective_run_id,
            lease_owner=lease_owner,
            source_build=source_build,
        )
        if staged is not None:
            return staged
        try:
            while True:
                async with open_shared_ctx(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("BEGIN IMMEDIATE")
                    run = await self._require_leased_run_conn(
                        db,
                        run_id=effective_run_id,
                        lease_owner=lease_owner,
                        allowed_states=("staging",),
                    )
                    async with db.execute(
                        """
                        SELECT expected.team_id,expected.agent_id,
                               expected.source_row_version,expected.source_updated_at,
                               agent.*
                        FROM agent_release_baseline_expected expected
                        JOIN team_subagent agent
                          ON agent.team_id=expected.team_id AND agent.id=expected.agent_id
                        WHERE expected.run_id=? AND expected.stage_state='pending'
                        ORDER BY expected.team_id,expected.agent_id
                        LIMIT ?
                        """,
                        (effective_run_id, batch_size),
                    ) as cursor:
                        rows = await cursor.fetchall()
                    if not rows:
                        summary = await self._finish_staging_conn(
                            db,
                            run_id=effective_run_id,
                            lease_owner=lease_owner,
                        )
                        await db.commit()
                        return summary
                    for row in rows:
                        self._assert_frozen_source(row)
                        compiled = await self._compile_conn(
                            db,
                            row=row,
                            snapshot_at=str(run["snapshot_at"]),
                        )
                        await self._persist_stage_result_conn(
                            db,
                            run_id=effective_run_id,
                            compiled=compiled,
                        )
                    await self._renew_lease_conn(db, run_id=effective_run_id, lease_owner=lease_owner)
                    await self._refresh_run_counts_conn(db, run_id=effective_run_id)
                    await db.commit()
        except BaseException:
            await self._release_lease(run_id=effective_run_id, lease_owner=lease_owner, increment_error=True)
            raise

    async def list_issues(
        self,
        *,
        run_id: str,
        limit: int = 100,
    ) -> tuple[AgentReleaseBaselineIssue, ...]:
        """Return safe identifiers and reason codes; never return Agent content."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self._prepare()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            run = await self._run_conn(db, run_id=run_id)
            self._assert_compiler_version(run)
            async with db.execute(
                """
                SELECT team_id,agent_id,stage_state,reason_codes_json,imported_at
                FROM agent_release_baseline_expected
                WHERE run_id=? AND stage_state IN ('pending','degraded','blocked')
                ORDER BY CASE stage_state WHEN 'blocked' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                         team_id,agent_id
                LIMIT ?
                """,
                (run_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        issues: list[AgentReleaseBaselineIssue] = []
        for row in rows:
            try:
                parsed = json.loads(str(row["reason_codes_json"]))
            except json.JSONDecodeError as exc:
                raise AgentReleaseBaselineError("baseline issue reason codes are invalid") from exc
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise AgentReleaseBaselineError("baseline issue reason codes are invalid")
            issues.append(
                AgentReleaseBaselineIssue(
                    team_id=str(row["team_id"]),
                    agent_id=str(row["agent_id"]),
                    state=str(row["stage_state"]),
                    reason_codes=tuple(parsed),
                    imported=row["imported_at"] is not None,
                ),
            )
        return tuple(issues)

    async def apply(
        self,
        *,
        run_id: str,
        expected_digest: str,
        writer_stopped: bool,
        batch_size: int = 100,
    ) -> AgentReleaseBaselineSummary:
        """Create immutable baseline revisions/releases and attach live pointers."""
        if not writer_stopped:
            raise AgentReleaseBaselineError("apply requires explicit writer-stopped confirmation")
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        await self._prepare()
        lease_owner = f"baseline-apply:{uuid4().hex}"
        completed = await self._claim_apply(
            run_id=run_id,
            expected_digest=expected_digest,
            lease_owner=lease_owner,
        )
        if completed is not None:
            return completed
        try:
            while True:
                async with open_shared_ctx(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("BEGIN IMMEDIATE")
                    run = await self._require_leased_run_conn(
                        db,
                        run_id=run_id,
                        lease_owner=lease_owner,
                        allowed_states=("importing",),
                    )
                    await self._assert_expected_source_set_conn(db, run_id=run_id)
                    async with db.execute(
                        """
                        SELECT expected.team_id,expected.agent_id,
                               expected.source_row_version,expected.source_updated_at,
                               expected.source_hash AS planned_source_hash,
                               expected.definition_hash AS planned_definition_hash,
                               expected.dependency_fingerprint AS planned_dependency_fingerprint,
                               expected.capability_fingerprint AS planned_capability_fingerprint,
                               expected.audience_hash AS planned_audience_hash,
                               expected.safety_report_id AS planned_safety_report_id,
                               expected.reason_codes_json AS planned_reason_codes_json,
                               expected.stage_state AS planned_stage_state,
                               agent.*
                        FROM agent_release_baseline_expected expected
                        JOIN team_subagent agent
                          ON agent.team_id=expected.team_id AND agent.id=expected.agent_id
                        WHERE expected.run_id=? AND expected.imported_at IS NULL
                        ORDER BY expected.team_id,expected.agent_id
                        LIMIT ?
                        """,
                        (run_id, batch_size),
                    ) as cursor:
                        rows = await cursor.fetchall()
                    if not rows:
                        summary = await self._finish_apply_conn(
                            db,
                            run_id=run_id,
                            lease_owner=lease_owner,
                        )
                        await db.commit()
                        return summary
                    applied_at = self._now()
                    for row in rows:
                        self._assert_frozen_source(row)
                        if str(row["definition_mode"]) != "legacy_mutable":
                            raise AgentReleaseBaselineDriftError("legacy Agent definition mode changed")
                        compiled = await self._compile_conn(
                            db,
                            row=row,
                            snapshot_at=str(run["snapshot_at"]),
                        )
                        self._assert_staged_plan(row, compiled=compiled)
                        await self._import_one_conn(
                            db,
                            run_id=run_id,
                            compiled=compiled,
                            applied_at=applied_at,
                        )
                    await self._renew_lease_conn(db, run_id=run_id, lease_owner=lease_owner)
                    await self._refresh_run_counts_conn(db, run_id=run_id)
                    await db.commit()
        except BaseException:
            await self._release_lease(run_id=run_id, lease_owner=lease_owner, increment_error=True)
            raise

    async def abandon(self, *, run_id: str) -> AgentReleaseBaselineSummary:
        """Close an unapplied staged run so a fresh snapshot can be created."""
        await self._prepare()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            run = await self._run_conn(db, run_id=run_id)
            if str(run["state"]) not in {"staging", "staged", "importing"}:
                raise AgentReleaseBaselineError("only an unapplied baseline run can be abandoned")
            if int(run["imported_count"]) != 0:
                raise AgentReleaseBaselineError("a partially imported run cannot be abandoned")
            self._assert_lease_available(run)
            timestamp = self._now()
            await db.execute(
                """
                UPDATE agent_release_baseline_run
                SET state='abandoned',lease_owner=NULL,lease_expires_at=NULL,
                    finished_at=?,updated_at=?
                WHERE id=?
                """,
                (timestamp, timestamp, run_id),
            )
            summary = await self._summary_for_run_conn(db, run_id=run_id)
            await db.commit()
            return summary

    async def _prepare(self) -> None:
        if not Path(self.db_path).is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")

    async def _legacy_rows_conn(self, db: aiosqlite.Connection) -> list[aiosqlite.Row]:
        # Quarantined Agents are deliberately out of the inventory: they are already
        # disabled, so leaving them on the legacy row costs nothing, and keeping a
        # single unsafe Agent in the run would otherwise block the whole cutover.
        async with db.execute(
            """
            SELECT * FROM team_subagent
            WHERE definition_mode='legacy_mutable' AND lifecycle_state<>'quarantined'
            ORDER BY team_id,id
            """,
        ) as cursor:
            return list(await cursor.fetchall())

    async def _create_or_claim_stage(
        self,
        *,
        run_id: str,
        lease_owner: str,
        source_build: str | None,
    ) -> AgentReleaseBaselineSummary | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT * FROM agent_release_baseline_run WHERE id=?", (run_id,)) as cursor:
                run = await cursor.fetchone()
            timestamp = self._now()
            lease_expires_at = self._lease_expiry()
            if run is None:
                await self._assert_cutover_not_active_conn(db)
                try:
                    await db.execute(
                        """
                        INSERT INTO agent_release_baseline_run(
                          id,state,compiler_version,source_build,snapshot_at,
                          lease_owner,lease_expires_at,started_at,updated_at
                        ) VALUES (?,'staging',?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            BASELINE_COMPILER_VERSION,
                            source_build,
                            timestamp,
                            lease_owner,
                            lease_expires_at,
                            timestamp,
                            timestamp,
                        ),
                    )
                except aiosqlite.IntegrityError as exc:
                    raise AgentReleaseBaselineError("another baseline run is already open") from exc
                await db.execute(
                    """
                    INSERT INTO agent_release_baseline_expected(
                      run_id,team_id,agent_id,source_row_version,source_updated_at,stage_state
                    )
                    SELECT ?,team_id,id,row_version,updated_at,'pending'
                    FROM team_subagent
                    WHERE definition_mode='legacy_mutable' AND lifecycle_state<>'quarantined'
                    ORDER BY team_id,id
                    """,
                    (run_id,),
                )
                await db.execute(
                    """
                    UPDATE agent_release_baseline_run
                    SET expected_count=(
                      SELECT COUNT(*) FROM agent_release_baseline_expected WHERE run_id=?
                    )
                    WHERE id=?
                    """,
                    (run_id, run_id),
                )
            else:
                self._assert_compiler_version(run)
                if str(run["state"]) == "staged":
                    summary = await self._summary_for_run_conn(db, run_id=run_id)
                    await db.commit()
                    return summary
                if str(run["state"]) != "staging":
                    raise AgentReleaseBaselineError("baseline run cannot resume staging")
                self._assert_lease_available(run)
                await db.execute(
                    """
                    UPDATE agent_release_baseline_run
                    SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?
                    """,
                    (lease_owner, lease_expires_at, timestamp, run_id),
                )
            await db.commit()
            return None

    async def _claim_apply(
        self,
        *,
        run_id: str,
        expected_digest: str,
        lease_owner: str,
    ) -> AgentReleaseBaselineSummary | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            run = await self._run_conn(db, run_id=run_id)
            self._assert_compiler_version(run)
            if str(run["expected_digest"] or "") != expected_digest:
                raise AgentReleaseBaselineError("staged digest does not match operator confirmation")
            if str(run["state"]) == "completed":
                await self._assert_cutover_active_conn(db)
                summary = await self._summary_for_run_conn(db, run_id=run_id)
                await db.commit()
                return summary
            if str(run["state"]) not in {"staged", "importing"}:
                raise AgentReleaseBaselineError("baseline run is not ready to apply")
            if int(run["blocked_count"]) != 0:
                raise AgentReleaseBaselineError("blocked legacy Agents must be fixed before apply")
            if int(run["staged_count"]) != int(run["expected_count"]):
                raise AgentReleaseBaselineError("baseline inventory is not fully staged")
            self._assert_lease_available(run)
            timestamp = self._now()
            await db.execute(
                """
                UPDATE agent_release_baseline_run
                SET state='importing',lease_owner=?,lease_expires_at=?,updated_at=?
                WHERE id=?
                """,
                (lease_owner, self._lease_expiry(), timestamp, run_id),
            )
            await db.commit()
            return None

    async def _compile_conn(
        self,
        db: aiosqlite.Connection,
        *,
        row: aiosqlite.Row,
        snapshot_at: str,
    ) -> _CompiledLegacyAgent:
        team_id = str(row["team_id"])
        agent_id = str(row["id"])
        definition = {
            "max_iterations": int(row["max_iterations"]),
            "model": row["model"],
            "output_schema_json": row["output_schema_json"],
            "skills_json": str(row["skills_json"]),
            "system_prompt": str(row["system_prompt"]),
            "tool_refs_json": str(row["tool_refs_json"]),
        }
        listing = {
            "description": str(row["description"]),
            "display_name": str(row["display_name"]),
            "name": str(row["name"]),
        }
        definition_json = _canonical_json(definition)
        listing_json = _canonical_json(listing)
        definition_hash = _hash_text(definition_json)
        safety_content_hash = _hash_json({"definition": definition, "listing": listing})

        reason_codes: set[str] = {"grandfathered_unverified_dependencies"}
        blocked = False
        if str(row["definition_mode"]) == "legacy_mutable" and (
            row["live_revision_id"] is not None
            or row["live_release_id"] is not None
            or int(row["release_sequence"]) != 0
            or row["last_release_event_id"] is not None
        ):
            blocked = True
            reason_codes.add("legacy_release_state_inconsistent")
        for field_name in ("tool_refs_json", "skills_json"):
            try:
                parsed = json.loads(str(row[field_name]))
                if not isinstance(parsed, list) or not all(isinstance(item, str) and item for item in parsed):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                reason_codes.add(f"{field_name.removesuffix('_json')}_unparseable")
        output_schema = row["output_schema_json"]
        if output_schema is not None:
            try:
                if not isinstance(json.loads(str(output_schema)), dict):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                reason_codes.add("output_schema_unparseable")

        listing_safety = scan_agent_listing(**listing)
        if listing_safety.blocked:
            blocked = True
            reason_codes.update(f"listing_{code}" for code in listing_safety.reason_codes)
        try:
            lint_user_append(str(row["system_prompt"]))
        except ManifestSecurityError:
            blocked = True
            reason_codes.add("prompt_injection_pattern")
        except Exception:
            blocked = True
            reason_codes.add("prompt_safety_check_failed")

        claimed_name = str(row["display_name"] or row["name"])
        try:
            normalized_key = effective_agent_name_key(claimed_name, allow_legacy_controls=True)
        except TeamSubagentNameConflictError:
            blocked = True
            reason_codes.add("name_claim_invalid")
        else:
            async with db.execute(
                """
                SELECT holder_kind,holder_id,agent_id FROM agent_name_claim
                WHERE team_id=? AND locale='*' AND normalized_key=?
                """,
                (team_id, normalized_key),
            ) as cursor:
                claim = await cursor.fetchone()
            if claim is None:
                blocked = True
                reason_codes.add("name_claim_missing")
            elif str(claim[0]) == "agent" and str(claim[2] or "") == agent_id:
                pass
            elif str(claim[0]) == "legacy_conflict":
                async with db.execute(
                    """
                    SELECT 1 FROM agent_name_conflict_group
                    WHERE team_id=? AND normalized_key=? AND agent_id=? AND resolved_at IS NULL
                    """,
                    (team_id, normalized_key, agent_id),
                ) as cursor:
                    conflict_member = await cursor.fetchone()
                if conflict_member is None:
                    blocked = True
                    reason_codes.add("name_claim_owner_mismatch")
                else:
                    reason_codes.add("grandfathered_name_conflict")
            elif str(claim[0]) == "system":
                reason_codes.add("grandfathered_name_conflict")
            else:
                blocked = True
                reason_codes.add("name_claim_owner_mismatch")

        audience = await self._audience_snapshot_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            visibility=str(row["visibility"]),
            audience_mode=str(row["audience_mode"]),
            snapshot_at=snapshot_at,
        )
        audience_snapshot_json = _canonical_json(audience)
        audience_hash = _hash_text(audience_snapshot_json)
        dependency_fingerprint = _hash_json(
            {
                "skills_json": str(row["skills_json"]),
                "tool_refs_json": str(row["tool_refs_json"]),
            },
        )
        capability_fingerprint = _hash_json(
            {
                "dependency_fingerprint": dependency_fingerprint,
                "max_iterations": int(row["max_iterations"]),
                "model": row["model"],
                "output_schema_json": row["output_schema_json"],
            },
        )
        source_hash = _hash_json(
            {
                "agent_id": agent_id,
                "archived_at": row["archived_at"],
                "archived_by": row["archived_by"],
                "audience": audience,
                "created_at": str(row["created_at"]),
                "created_by": row["created_by"],
                "definition": definition,
                "lifecycle_state": str(row["lifecycle_state"]),
                "listing": listing,
                "owner_user_id": row["owner_user_id"],
                "origin_mode": str(row["origin_mode"]),
                "quarantine_expires_at": row["quarantine_expires_at"],
                "quarantine_reason": row["quarantine_reason"],
                "quarantine_started_at": row["quarantine_started_at"],
                "quarantined_owner_user_id": row["quarantined_owner_user_id"],
                "source_draft_id": row["source_draft_id"],
                "status": str(row["status"]),
                "team_id": team_id,
                "visibility": str(row["visibility"]),
            },
        )
        sorted_reasons = tuple(sorted(reason_codes))
        safety_status = "blocked" if blocked else "warning"
        stage_state = "blocked" if blocked else "degraded"
        deterministic_json = _canonical_json(
            {
                "rule_codes": list(sorted_reasons),
                "status": safety_status,
            },
        )
        semantic_json = _canonical_json({"scanner_version": "none", "status": "not_run"})
        safe_summary_json = _canonical_json(
            {
                "blocked": blocked,
                "finding_count": len(sorted_reasons),
                "reason_codes": list(sorted_reasons),
            },
        )
        risk_json = _canonical_json(
            {
                "classification": "blocked" if blocked else "unknown",
                "compiler_version": BASELINE_COMPILER_VERSION,
                "reason_codes": list(sorted_reasons),
            },
        )
        # The imported revision's id is derived from the same source hash `apply`
        # uses, so the safety report can be scoped to that revision at stage time.
        # It previously carried subject_kind='legacy_row'/subject_ref=agent_id,
        # which no admission join can satisfy -- meaning a completed baseline
        # import left every Agent revisioned and permanently undispatchable.
        imported_revision_id = _stable_id("arv_", team_id, agent_id, source_hash)
        report_id = _stable_id(
            "asr_",
            team_id,
            agent_id,
            agent_id,
            safety_content_hash,
            dependency_fingerprint,
            BASELINE_COMPILER_VERSION,
        )
        return _CompiledLegacyAgent(
            team_id=team_id,
            agent_id=agent_id,
            source_row_version=int(row["row_version"]),
            source_updated_at=str(row["updated_at"]),
            source_hash=source_hash,
            definition_json=definition_json,
            listing_json=listing_json,
            definition_hash=definition_hash,
            safety_content_hash=safety_content_hash,
            dependency_fingerprint=dependency_fingerprint,
            capability_fingerprint=capability_fingerprint,
            audience_snapshot_json=audience_snapshot_json,
            audience_hash=audience_hash,
            safety_report_id=report_id,
            imported_revision_id=imported_revision_id,
            safety_status=safety_status,
            stage_state=stage_state,
            reason_codes=sorted_reasons,
            deterministic_json=deterministic_json,
            semantic_json=semantic_json,
            safe_summary_json=safe_summary_json,
            risk_json=risk_json,
        )

    async def _audience_snapshot_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        visibility: str,
        audience_mode: str,
        snapshot_at: str,
    ) -> dict[str, Any]:
        async with db.execute(
            """
            SELECT COALESCE(MAX(audience_semantics_activated_at IS NOT NULL),0)
            FROM team_subagent_schema_meta WHERE singleton=1
            """,
        ) as cursor:
            semantics_row = await cursor.fetchone()
        semantics_active = bool(semantics_row and semantics_row[0])
        async with db.execute(
            """
            SELECT id,principal_type,principal_id,permission,effect,
                   created_by,created_at,updated_at,expires_at
            FROM resource_grants
            WHERE team_id=? AND resource_kind='team_subagent' AND resource_id=?
              AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)
            ORDER BY principal_type,principal_id,permission,effect,id
            """,
            (team_id, agent_id, snapshot_at),
        ) as cursor:
            grant_rows = await cursor.fetchall()
        grants = [
            {
                "created_at": item[6],
                "created_by": item[5],
                "effect": item[4],
                "expires_at": item[8],
                "id": item[0],
                "permission": item[3],
                "principal_id": item[2],
                "principal_type": item[1],
                "updated_at": item[7],
            }
            for item in grant_rows
        ]
        if semantics_active:
            effective_mode = audience_mode
        elif visibility == "team_wide":
            effective_mode = "teamwide"
        elif any(item[1] in {"user", "group"} and item[3] in {"use", "write", "admin"} and item[4] == "allow" for item in grant_rows):
            effective_mode = "custom"
        else:
            effective_mode = "private"
        return {
            "effective_mode": effective_mode,
            "grants": grants,
            "semantics": "activated" if semantics_active else "legacy_projection",
            "snapshot_at": snapshot_at,
        }

    async def _persist_stage_result_conn(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        compiled: _CompiledLegacyAgent,
    ) -> None:
        timestamp = self._now()
        await db.execute(
            """
            INSERT OR IGNORE INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,
              status,deterministic_json,semantic_json,restricted_evidence_ref,
              safe_summary_json,created_by,created_at
            ) VALUES (?,?,?,'revision',?,?,?,?,?,?,?,?,NULL,?,?,?)
            """,
            (
                compiled.safety_report_id,
                compiled.team_id,
                compiled.agent_id,
                compiled.imported_revision_id,
                compiled.safety_content_hash,
                compiled.dependency_fingerprint,
                BASELINE_COMPILER_VERSION,
                BASELINE_SEMANTIC_SCANNER_VERSION,
                compiled.safety_status,
                compiled.deterministic_json,
                compiled.semantic_json,
                compiled.safe_summary_json,
                BASELINE_ACTOR,
                timestamp,
            ),
        )
        async with db.execute(
            """
            SELECT id,status,deterministic_json,semantic_json,safe_summary_json
            FROM agent_safety_report
            WHERE team_id=? AND agent_id=? AND subject_kind='revision'
              AND subject_ref=? AND content_hash=? AND dependency_fingerprint=?
              AND compiler_version=? AND semantic_scanner_version=?
            """,
            (
                compiled.team_id,
                compiled.agent_id,
                compiled.imported_revision_id,
                compiled.safety_content_hash,
                compiled.dependency_fingerprint,
                BASELINE_COMPILER_VERSION,
                BASELINE_SEMANTIC_SCANNER_VERSION,
            ),
        ) as cursor:
            report = await cursor.fetchone()
        expected_report = (
            compiled.safety_report_id,
            compiled.safety_status,
            compiled.deterministic_json,
            compiled.semantic_json,
            compiled.safe_summary_json,
        )
        if report is None or tuple(str(value) for value in report) != expected_report:
            raise AgentReleaseBaselineError("existing safety report does not match compiled baseline")
        updated = await db.execute(
            """
            UPDATE agent_release_baseline_expected
            SET stage_state=?,source_hash=?,definition_hash=?,dependency_fingerprint=?,
                capability_fingerprint=?,audience_hash=?,safety_report_id=?,reason_codes_json=?
            WHERE run_id=? AND team_id=? AND agent_id=? AND stage_state='pending'
            """,
            (
                compiled.stage_state,
                compiled.source_hash,
                compiled.definition_hash,
                compiled.dependency_fingerprint,
                compiled.capability_fingerprint,
                compiled.audience_hash,
                compiled.safety_report_id,
                _canonical_json(list(compiled.reason_codes)),
                run_id,
                compiled.team_id,
                compiled.agent_id,
            ),
        )
        if updated.rowcount != 1:
            raise AgentReleaseBaselineError("baseline stage row changed concurrently")

    async def _import_one_conn(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        compiled: _CompiledLegacyAgent,
        applied_at: str,
    ) -> None:
        revision_id = compiled.imported_revision_id
        release_id = _stable_id("arl_", compiled.team_id, compiled.agent_id, compiled.source_hash)
        await db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (?,?,?,1,'imported_baseline',?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                compiled.team_id,
                compiled.agent_id,
                compiled.definition_json,
                compiled.listing_json,
                compiled.definition_hash,
                compiled.dependency_fingerprint,
                compiled.capability_fingerprint,
                BASELINE_COMPILER_VERSION,
                compiled.risk_json,
                BASELINE_ACTOR,
                applied_at,
            ),
        )
        definition = json.loads(compiled.definition_json)
        behavior = self._behavior_materializer.compile_legacy_team_revision(
            revision_id=revision_id,
            definition=definition,
            source_definition_hash=compiled.definition_hash,
            dependency_fingerprint=compiled.dependency_fingerprint,
            capability_fingerprint=compiled.capability_fingerprint,
            risk_json=compiled.risk_json,
        )
        await self._behavior_materializer.persist_team_revision_conn(
            db,
            revision_id=revision_id,
            team_id=compiled.team_id,
            subagent_id=compiled.agent_id,
            descriptor=behavior,
            created_at=applied_at,
        )
        await db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              source_candidate_id,audience_snapshot_json,audience_hash,
              capability_fingerprint,dependency_fingerprint,risk_json,safety_report_id,
              eval_evidence_json,release_note,activated_by,activated_at
            ) VALUES (?,?,?,1,?,NULL,NULL,?,?,?,?,?,?,NULL,?,?,?)
            """,
            (
                release_id,
                compiled.team_id,
                compiled.agent_id,
                revision_id,
                compiled.audience_snapshot_json,
                compiled.audience_hash,
                compiled.capability_fingerprint,
                compiled.dependency_fingerprint,
                compiled.risk_json,
                compiled.safety_report_id,
                "Imported legacy baseline",
                BASELINE_ACTOR,
                applied_at,
            ),
        )
        updated = await db.execute(
            """
            UPDATE team_subagent
            SET live_revision_id=?,live_release_id=?,release_sequence=1,
                definition_mode='revisioned',row_version=row_version+1
            WHERE team_id=? AND id=? AND definition_mode='legacy_mutable'
              AND row_version=? AND updated_at=?
              AND live_revision_id IS NULL AND live_release_id IS NULL AND release_sequence=0
            """,
            (
                revision_id,
                release_id,
                compiled.team_id,
                compiled.agent_id,
                compiled.source_row_version,
                compiled.source_updated_at,
            ),
        )
        if updated.rowcount != 1:
            raise AgentReleaseBaselineDriftError("legacy Agent changed during baseline apply")
        staged = await db.execute(
            """
            UPDATE agent_release_baseline_expected
            SET imported_revision_id=?,imported_release_id=?,imported_at=?
            WHERE run_id=? AND team_id=? AND agent_id=? AND imported_at IS NULL
            """,
            (
                revision_id,
                release_id,
                applied_at,
                run_id,
                compiled.team_id,
                compiled.agent_id,
            ),
        )
        if staged.rowcount != 1:
            raise AgentReleaseBaselineError("baseline import ledger changed concurrently")

    def _assert_frozen_source(self, row: aiosqlite.Row) -> None:
        if int(row["row_version"]) != int(row["source_row_version"]) or str(row["updated_at"]) != str(row["source_updated_at"]):
            raise AgentReleaseBaselineDriftError("legacy Agent changed after baseline inventory froze")

    def _assert_staged_plan(self, row: aiosqlite.Row, *, compiled: _CompiledLegacyAgent) -> None:
        actual = (
            compiled.source_hash,
            compiled.definition_hash,
            compiled.dependency_fingerprint,
            compiled.capability_fingerprint,
            compiled.audience_hash,
            compiled.safety_report_id,
            _canonical_json(list(compiled.reason_codes)),
            compiled.stage_state,
        )
        expected = tuple(
            str(row[key])
            for key in (
                "planned_source_hash",
                "planned_definition_hash",
                "planned_dependency_fingerprint",
                "planned_capability_fingerprint",
                "planned_audience_hash",
                "planned_safety_report_id",
                "planned_reason_codes_json",
                "planned_stage_state",
            )
        )
        if actual != expected:
            raise AgentReleaseBaselineDriftError("legacy Agent dependencies or audience changed after staging")

    async def _finish_staging_conn(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        lease_owner: str,
    ) -> AgentReleaseBaselineSummary:
        await self._refresh_run_counts_conn(db, run_id=run_id)
        run = await self._run_conn(db, run_id=run_id)
        if int(run["staged_count"]) != int(run["expected_count"]):
            raise AgentReleaseBaselineDriftError("frozen legacy Agent set changed during staging")
        digest = await self._staged_digest_conn(db, run_id=run_id)
        timestamp = self._now()
        updated = await db.execute(
            """
            UPDATE agent_release_baseline_run
            SET state='staged',expected_digest=?,staged_at=?,updated_at=?,
                lease_owner=NULL,lease_expires_at=NULL
            WHERE id=? AND state='staging' AND lease_owner=?
            """,
            (digest, timestamp, timestamp, run_id, lease_owner),
        )
        if updated.rowcount != 1:
            raise AgentReleaseBaselineError("baseline staging lease changed")
        return await self._summary_for_run_conn(db, run_id=run_id)

    async def _finish_apply_conn(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        lease_owner: str,
    ) -> AgentReleaseBaselineSummary:
        await self._refresh_run_counts_conn(db, run_id=run_id)
        run = await self._run_conn(db, run_id=run_id)
        if int(run["imported_count"]) != int(run["expected_count"]):
            raise AgentReleaseBaselineError("baseline import reconciliation failed")
        await self._assert_expected_source_set_conn(db, run_id=run_id)
        async with db.execute(
            """
            SELECT expected.source_hash AS planned_source_hash,
                   expected.definition_hash AS planned_definition_hash,
                   expected.dependency_fingerprint AS planned_dependency_fingerprint,
                   expected.capability_fingerprint AS planned_capability_fingerprint,
                   expected.audience_hash AS planned_audience_hash,
                   expected.safety_report_id AS planned_safety_report_id,
                   expected.reason_codes_json AS planned_reason_codes_json,
                   expected.stage_state AS planned_stage_state,
                   agent.*
            FROM agent_release_baseline_expected expected
            JOIN team_subagent agent
              ON agent.team_id=expected.team_id AND agent.id=expected.agent_id
            WHERE expected.run_id=?
            ORDER BY expected.team_id,expected.agent_id
            """,
            (run_id,),
        ) as cursor:
            parity_rows = await cursor.fetchall()
        for row in parity_rows:
            compiled = await self._compile_conn(
                db,
                row=row,
                snapshot_at=str(run["snapshot_at"]),
            )
            self._assert_staged_plan(row, compiled=compiled)
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM agent_release_baseline_expected expected
            JOIN team_subagent agent
              ON agent.team_id=expected.team_id AND agent.id=expected.agent_id
             AND agent.definition_mode='revisioned'
             AND agent.live_revision_id=expected.imported_revision_id
             AND agent.live_release_id=expected.imported_release_id
             AND agent.release_sequence=1
            JOIN team_subagent_revision revision
              ON revision.team_id=expected.team_id AND revision.subagent_id=expected.agent_id
             AND revision.id=expected.imported_revision_id
             AND revision.sequence=1 AND revision.revision_kind='imported_baseline'
            JOIN team_subagent_release release
              ON release.team_id=expected.team_id AND release.subagent_id=expected.agent_id
             AND release.id=expected.imported_release_id
             AND release.revision_id=expected.imported_revision_id
             AND release.release_sequence=1 AND release.safety_report_id=expected.safety_report_id
            WHERE expected.run_id=? AND expected.imported_at IS NOT NULL
            """,
            (run_id,),
        ) as cursor:
            reconciled_row = await cursor.fetchone()
        if reconciled_row is None or int(reconciled_row[0]) != int(run["expected_count"]):
            raise AgentReleaseBaselineError("baseline live pointer reconciliation failed")
        async with db.execute(
            "SELECT COUNT(*) FROM team_subagent WHERE definition_mode='legacy_mutable' AND lifecycle_state<>'quarantined'",
        ) as cursor:
            remaining_legacy = int((await cursor.fetchone())[0])
        if remaining_legacy:
            raise AgentReleaseBaselineError("legacy Agent reconciliation is incomplete")
        for table_name in (
            "agent_safety_report",
            "team_subagent_revision",
            "team_subagent_release",
            "agent_release_baseline_expected",
        ):
            async with db.execute(
                f"PRAGMA foreign_key_check({table_name})",  # noqa: S608 -- fixed internal allowlist
            ) as cursor:
                if await cursor.fetchone() is not None:
                    raise AgentReleaseBaselineError("foreign key check failed after baseline import")
        timestamp = self._now()
        activated = await db.execute(
            """
            UPDATE team_subagent_schema_meta
            SET release_semantics_activated_at=COALESCE(release_semantics_activated_at,?),
                minimum_reader_version=?,minimum_writer_version=?,updated_at=?
            WHERE singleton=1
              AND minimum_reader_version<=?
              AND minimum_writer_version<=?
            """,
            (
                timestamp,
                AGENT_RELEASE_COMPATIBILITY_VERSION,
                AGENT_RELEASE_COMPATIBILITY_VERSION,
                timestamp,
                AGENT_RELEASE_COMPATIBILITY_VERSION,
                AGENT_RELEASE_COMPATIBILITY_VERSION,
            ),
        )
        if activated.rowcount != 1:
            raise AgentReleaseBaselineError("Agent release compatibility floor changed unexpectedly")
        await self._assert_cutover_active_conn(db)
        updated = await db.execute(
            """
            UPDATE agent_release_baseline_run
            SET state='completed',finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL
            WHERE id=? AND state='importing' AND lease_owner=?
            """,
            (timestamp, timestamp, run_id, lease_owner),
        )
        if updated.rowcount != 1:
            raise AgentReleaseBaselineError("baseline apply lease changed")
        return await self._summary_for_run_conn(db, run_id=run_id)

    async def _assert_cutover_not_active_conn(self, db: aiosqlite.Connection) -> None:
        async with db.execute(
            """
            SELECT release_semantics_activated_at
            FROM team_subagent_schema_meta WHERE singleton=1
            """,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentReleaseBaselineError("Agent release schema metadata is missing")
        if row[0] is not None:
            raise AgentReleaseBaselineError("Agent release cutover is already active")

    async def _assert_cutover_active_conn(self, db: aiosqlite.Connection) -> None:
        async with db.execute(
            """
            SELECT release_semantics_activated_at,minimum_reader_version,minimum_writer_version
            FROM team_subagent_schema_meta WHERE singleton=1
            """,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] is None or str(row[1]) < AGENT_RELEASE_COMPATIBILITY_VERSION or str(row[2]) < AGENT_RELEASE_COMPATIBILITY_VERSION:
            raise AgentReleaseBaselineError("Agent release cutover metadata is inconsistent")

    async def _refresh_run_counts_conn(self, db: aiosqlite.Connection, *, run_id: str) -> None:
        await db.execute(
            """
            UPDATE agent_release_baseline_run
            SET staged_count=(SELECT COUNT(*) FROM agent_release_baseline_expected WHERE run_id=? AND stage_state!='pending'),
                ready_count=(SELECT COUNT(*) FROM agent_release_baseline_expected WHERE run_id=? AND stage_state='ready'),
                degraded_count=(SELECT COUNT(*) FROM agent_release_baseline_expected WHERE run_id=? AND stage_state='degraded'),
                blocked_count=(SELECT COUNT(*) FROM agent_release_baseline_expected WHERE run_id=? AND stage_state='blocked'),
                imported_count=(SELECT COUNT(*) FROM agent_release_baseline_expected WHERE run_id=? AND imported_at IS NOT NULL),
                updated_at=?
            WHERE id=?
            """,
            (run_id, run_id, run_id, run_id, run_id, self._now(), run_id),
        )

    async def _assert_expected_source_set_conn(self, db: aiosqlite.Connection, *, run_id: str) -> None:
        async with db.execute(
            """
            SELECT
              (
                SELECT COUNT(*)
                FROM agent_release_baseline_expected expected
                LEFT JOIN team_subagent agent
                  ON agent.team_id=expected.team_id AND agent.id=expected.agent_id
                 AND agent.definition_mode='legacy_mutable' AND agent.lifecycle_state<>'quarantined'
                WHERE expected.run_id=? AND expected.imported_at IS NULL AND agent.id IS NULL
              ) AS missing_count,
              (
                SELECT COUNT(*)
                FROM team_subagent agent
                LEFT JOIN agent_release_baseline_expected expected
                  ON expected.run_id=? AND expected.team_id=agent.team_id
                 AND expected.agent_id=agent.id AND expected.imported_at IS NULL
                WHERE agent.definition_mode='legacy_mutable' AND agent.lifecycle_state<>'quarantined'
                  AND expected.agent_id IS NULL
              ) AS extra_count
            """,
            (run_id, run_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or int(row[0]) or int(row[1]):
            raise AgentReleaseBaselineDriftError("legacy Agent set changed after baseline inventory froze")

    async def _staged_digest_conn(self, db: aiosqlite.Connection, *, run_id: str) -> str:
        async with db.execute(
            """
            SELECT team_id,agent_id,source_hash,definition_hash,dependency_fingerprint,
                   capability_fingerprint,audience_hash,safety_report_id,stage_state,reason_codes_json
            FROM agent_release_baseline_expected WHERE run_id=?
            ORDER BY team_id,agent_id
            """,
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return _hash_json([list(row) for row in rows])

    async def _renew_lease_conn(self, db: aiosqlite.Connection, *, run_id: str, lease_owner: str) -> None:
        updated = await db.execute(
            """
            UPDATE agent_release_baseline_run SET lease_expires_at=?,updated_at=?
            WHERE id=? AND lease_owner=?
            """,
            (self._lease_expiry(), self._now(), run_id, lease_owner),
        )
        if updated.rowcount != 1:
            raise AgentReleaseBaselineError("baseline lease changed")

    async def _release_lease(self, *, run_id: str, lease_owner: str, increment_error: bool) -> None:
        try:
            async with open_shared_ctx(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """
                    UPDATE agent_release_baseline_run
                    SET lease_owner=NULL,lease_expires_at=NULL,
                        error_count=error_count+?,updated_at=?
                    WHERE id=? AND lease_owner=? AND state IN ('staging','importing')
                    """,
                    (1 if increment_error else 0, self._now(), run_id, lease_owner),
                )
                await db.commit()
        except Exception:
            return

    async def _require_leased_run_conn(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        lease_owner: str,
        allowed_states: Sequence[str],
    ) -> aiosqlite.Row:
        run = await self._run_conn(db, run_id=run_id)
        if str(run["state"]) not in allowed_states or str(run["lease_owner"] or "") != lease_owner:
            raise AgentReleaseBaselineError("baseline run lease is not owned by this operation")
        return run

    async def _run_conn(self, db: aiosqlite.Connection, *, run_id: str) -> aiosqlite.Row:
        async with db.execute("SELECT * FROM agent_release_baseline_run WHERE id=?", (run_id,)) as cursor:
            run = await cursor.fetchone()
        if run is None:
            raise AgentReleaseBaselineError("baseline run does not exist")
        return run

    async def _summary_for_run_conn(self, db: aiosqlite.Connection, *, run_id: str) -> AgentReleaseBaselineSummary:
        run = await self._run_conn(db, run_id=run_id)
        return AgentReleaseBaselineSummary(
            run_id=run_id,
            state=str(run["state"]),
            expected=int(run["expected_count"]),
            staged=int(run["staged_count"]),
            ready=int(run["ready_count"]),
            degraded=int(run["degraded_count"]),
            blocked=int(run["blocked_count"]),
            imported=int(run["imported_count"]),
            digest=str(run["expected_digest"] or ""),
        )

    def _summary_from_compiled(
        self,
        compiled: list[_CompiledLegacyAgent],
        *,
        run_id: str | None,
        state: str,
    ) -> AgentReleaseBaselineSummary:
        digest = _hash_json(
            [
                [
                    item.team_id,
                    item.agent_id,
                    item.source_hash,
                    item.definition_hash,
                    item.dependency_fingerprint,
                    item.capability_fingerprint,
                    item.audience_hash,
                    item.safety_report_id,
                    item.stage_state,
                    _canonical_json(list(item.reason_codes)),
                ]
                for item in compiled
            ],
        )
        return AgentReleaseBaselineSummary(
            run_id=run_id,
            state=state,
            expected=len(compiled),
            staged=len(compiled),
            ready=sum(item.stage_state == "ready" for item in compiled),
            degraded=sum(item.stage_state == "degraded" for item in compiled),
            blocked=sum(item.stage_state == "blocked" for item in compiled),
            imported=0,
            digest=digest,
        )

    def _assert_lease_available(self, run: aiosqlite.Row) -> None:
        owner = run["lease_owner"]
        expires_at = run["lease_expires_at"]
        if owner is None:
            return
        if expires_at is None or _parse_timestamp(str(expires_at)) > self._clock_now():
            raise AgentReleaseBaselineError("baseline run is leased by another operation")

    def _assert_compiler_version(self, run: aiosqlite.Row) -> None:
        if str(run["compiler_version"]) != BASELINE_COMPILER_VERSION:
            raise AgentReleaseBaselineError("baseline run compiler version is incompatible")

    def _now(self) -> str:
        return self._clock_now().isoformat(timespec="seconds").replace("+00:00", "Z")

    def _lease_expiry(self) -> str:
        return (self._clock_now() + _LEASE_DURATION).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _clock_now(self) -> datetime:
        value = self._clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("baseline clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _hash_json(value: Any) -> str:
    return _hash_text(_canonical_json(value))


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x00".join(parts).encode()
    return f"{prefix}{hashlib.sha256(payload).hexdigest()[:32]}"


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


__all__ = [
    "AgentReleaseBaselineDriftError",
    "AgentReleaseBaselineError",
    "AgentReleaseBaselineIssue",
    "AgentReleaseBaselineStore",
    "AgentReleaseBaselineSummary",
    "BASELINE_COMPILER_VERSION",
]
