"""M3-7 normalized, pinned Agent binding for scheduled jobs.

The binding is a confirmation record, not an authorization grant.  Creation
pins the current immutable release; every fire rechecks the job owner,
assignment, Audience/access, Agent lifecycle, exact revision, Safety evidence,
and current live identity plus capability/dependency fingerprints before any
LLM work starts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

ScheduledAgentBindingState = Literal["ready", "needs_reconfirm", "paused"]


class ScheduledAgentBindingError(RuntimeError):
    """Finite user-actionable failure for schedule binding admission."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ScheduledAgentBindingSeed:
    assignment_id: str | None
    agent_id: str
    agent_source_kind: Literal["team", "system"]
    revision_ref: str
    release_id: str | None
    capability_fingerprint: str
    dependency_fingerprint: str
    confirmed_by: str
    confirmed_at: str


@dataclass(frozen=True, slots=True)
class ScheduledAgentBinding:
    job_id: str
    assignment_id: str | None
    agent_id: str
    agent_source_kind: Literal["team", "system"]
    revision_ref: str
    release_id: str | None
    capability_fingerprint: str
    dependency_fingerprint: str
    follow_mode: Literal["pinned"]
    state: ScheduledAgentBindingState
    confirmed_by: str
    confirmed_at: str
    row_version: int


class ScheduledAgentBindingStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def release_semantics_active(self) -> bool:
        """Whether exact release pinning is mandatory on this deployment.

        A job naming a team Agent cannot be pinned before the release baseline
        import has run: there is no immutable revision to pin.  Treating that as
        ``scheduled_recipe_lost`` pauses the job, and paused schedules never
        auto-resume, so an un-migrated deployment silently and irreversibly stops
        every team-Agent schedule it has.
        """
        from bowhead_core.team_subagent_store import release_semantics_active_conn

        async with open_shared_ctx(self.db_path) as db:
            return await release_semantics_active_conn(db)

    async def prepare_current_team_binding(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        confirmed_by: str,
    ) -> ScheduledAgentBindingSeed:
        """Read the current exact Team Agent release for an atomic job write."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await _current_team_binding_row(
                db,
                db_path=self.db_path,
                team_id=team_id,
                user_id=user_id,
                agent_id=agent_id,
            )
        if row is None:
            raise ScheduledAgentBindingError("scheduled_recipe_lost")
        return _team_seed(row, confirmed_by=confirmed_by)

    async def read(self, job_id: str) -> ScheduledAgentBinding | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_job_agent_binding WHERE job_id=?",
                (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return _binding_from_row(row) if row is not None else None

    async def read_many(
        self,
        job_ids: list[str],
    ) -> dict[str, ScheduledAgentBinding]:
        """Read list-page bindings in one connection and one bounded query."""

        unique_ids = tuple(dict.fromkeys(job_ids))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM scheduled_job_agent_binding WHERE job_id IN ({placeholders})",  # noqa: S608 -- placeholders only
                unique_ids,
            ) as cursor:
                rows = await cursor.fetchall()
        return {binding.job_id: binding for row in rows if (binding := _binding_from_row(row)) is not None}

    async def bind_legacy_team_job(
        self,
        *,
        job_id: str,
        team_id: str,
        user_id: str,
        agent_id: str,
    ) -> ScheduledAgentBinding:
        """Lazy, exact-ID-only migration for a legacy team_subagent recipe.

        There is deliberately no name lookup here.  A legacy value which is
        not the stable Team Agent ID pauses for explicit user reconfirmation.
        """

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM scheduled_job_agent_binding WHERE job_id=?",
                    (job_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    await db.commit()
                    return _binding_from_row(existing)
                row = await _current_team_binding_row(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    user_id=user_id,
                    agent_id=agent_id,
                )
                if row is None:
                    raise ScheduledAgentBindingError("scheduled_recipe_lost")
                seed = _team_seed(row, confirmed_by=user_id)
                await apply_scheduled_agent_binding_conn(
                    db,
                    db_path=self.db_path,
                    job_id=job_id,
                    seed=seed,
                )
                async with db.execute(
                    "SELECT * FROM scheduled_job_agent_binding WHERE job_id=?",
                    (job_id,),
                ) as cursor:
                    inserted = await cursor.fetchone()
                if inserted is None:
                    raise ScheduledAgentBindingError(
                        "scheduled_agent_binding_unavailable",
                    )
                await db.commit()
                return _binding_from_row(inserted)
            except BaseException:
                await db.rollback()
                raise

    async def validate_for_fire(
        self,
        *,
        job_id: str,
        team_id: str,
        user_id: str,
    ) -> ScheduledAgentBinding:
        """Recheck a normalized pin and atomically pause on durable drift."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM scheduled_job_agent_binding WHERE job_id=?",
                    (job_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ScheduledAgentBindingError(
                        "scheduled_agent_binding_unavailable",
                    )
                binding = _binding_from_row(row)
                if binding.state == "needs_reconfirm":
                    raise ScheduledAgentBindingError("scheduled_recipe_changed")
                if binding.state != "ready":
                    raise ScheduledAgentBindingError("scheduled_recipe_lost")

                if binding.agent_source_kind == "team":
                    reason = await _validate_team_binding_for_fire(
                        db,
                        binding=binding,
                        team_id=team_id,
                        user_id=user_id,
                        db_path=self.db_path,
                    )
                else:
                    reason = await _validate_system_binding_for_fire(
                        db,
                        binding=binding,
                        team_id=team_id,
                        user_id=user_id,
                    )
                if reason is not None:
                    state: ScheduledAgentBindingState = "needs_reconfirm" if reason == "scheduled_recipe_changed" else "paused"
                    await _set_binding_and_job_paused(
                        db,
                        binding=binding,
                        state=state,
                        reason=reason,
                    )
                    await db.commit()
                    raise ScheduledAgentBindingError(reason)
                await db.commit()
                return binding
            except ScheduledAgentBindingError:
                if db.in_transaction:
                    await db.rollback()
                raise
            except BaseException:
                await db.rollback()
                raise


async def apply_scheduled_agent_binding_conn(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    job_id: str,
    seed: ScheduledAgentBindingSeed,
) -> None:
    """Create or explicitly reconfirm a schedule pin in the caller transaction."""

    db.row_factory = aiosqlite.Row
    if seed.agent_source_kind == "team":
        async with db.execute(
            """
            SELECT workspace.team_id,job.user_id
            FROM scheduled_jobs job
            JOIN workspaces workspace
              ON workspace.id=job.workspace_id AND workspace.status='active'
            WHERE job.id=? AND job.user_id=?
            """,
            (job_id, seed.confirmed_by),
        ) as cursor:
            scope = await cursor.fetchone()
        if scope is None or not await _team_agent_can_consume(
            db,
            db_path=db_path,
            team_id=str(scope[0]),
            user_id=str(scope[1]),
            agent_id=seed.agent_id,
        ):
            raise ScheduledAgentBindingError("scheduled_recipe_lost")

    await db.execute(
        """
        INSERT INTO scheduled_job_agent_binding(
          job_id,assignment_id,agent_id,agent_source_kind,revision_ref,release_id,
          capability_fingerprint,dependency_fingerprint,follow_mode,state,
          confirmed_by,confirmed_at,row_version
        ) VALUES (?,?,?,?,?,?,?,?,'pinned','ready',?,?,1)
        ON CONFLICT(job_id) DO UPDATE SET
          assignment_id=excluded.assignment_id,
          agent_id=excluded.agent_id,
          agent_source_kind=excluded.agent_source_kind,
          revision_ref=excluded.revision_ref,
          release_id=excluded.release_id,
          capability_fingerprint=excluded.capability_fingerprint,
          dependency_fingerprint=excluded.dependency_fingerprint,
          follow_mode='pinned',state='ready',
          confirmed_by=excluded.confirmed_by,
          confirmed_at=excluded.confirmed_at,
          row_version=scheduled_job_agent_binding.row_version+1
        """,
        (
            job_id,
            seed.assignment_id,
            seed.agent_id,
            seed.agent_source_kind,
            seed.revision_ref,
            seed.release_id,
            seed.capability_fingerprint,
            seed.dependency_fingerprint,
            seed.confirmed_by,
            seed.confirmed_at,
        ),
    )


async def clear_scheduled_agent_binding_conn(
    db: aiosqlite.Connection,
    *,
    job_id: str,
) -> None:
    await db.execute(
        "DELETE FROM scheduled_job_agent_binding WHERE job_id=?",
        (job_id,),
    )


async def _current_team_binding_row(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    team_id: str,
    user_id: str,
    agent_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT assignment.id AS assignment_id,agent.id AS agent_id,
               release.id AS release_id,release.revision_id,
               release.capability_fingerprint,release.dependency_fingerprint
        FROM team_subagent agent
        JOIN agent_assignment assignment
          ON assignment.team_id=agent.team_id
         AND assignment.user_id=? AND assignment.agent_id=agent.id
         AND assignment.agent_source_kind='team'
         AND assignment.personal_state='active'
         AND assignment.access_state='granted'
        JOIN team_subagent_release release
          ON release.team_id=agent.team_id AND release.subagent_id=agent.id
         AND release.id=agent.live_release_id
         AND release.revision_id=agent.live_revision_id
        JOIN team_subagent_revision agent_revision
          ON agent_revision.team_id=release.team_id
         AND agent_revision.subagent_id=release.subagent_id
         AND agent_revision.id=release.revision_id
         AND agent_revision.capability_fingerprint=release.capability_fingerprint
         AND agent_revision.dependency_fingerprint=release.dependency_fingerprint
        JOIN agent_safety_report safety
          ON safety.team_id=release.team_id
         AND safety.agent_id=release.subagent_id
         AND safety.id=release.safety_report_id
         AND safety.subject_kind='revision'
         AND safety.subject_ref=agent_revision.id
         AND safety.dependency_fingerprint=agent_revision.dependency_fingerprint
         AND safety.status NOT IN ('blocked','unknown')
        WHERE agent.team_id=? AND agent.id=?
          AND agent.status='enabled' AND agent.lifecycle_state='active'
          AND agent.definition_mode='revisioned'
        """,
        (user_id, team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or not await _team_agent_can_consume(
        db,
        db_path=db_path,
        team_id=team_id,
        user_id=user_id,
        agent_id=agent_id,
    ):
        return None
    return row


async def _team_agent_can_consume(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    team_id: str,
    user_id: str,
    agent_id: str,
) -> bool:
    decisions = await TeamSubagentAccessResolver(
        db_path,
    ).resolve_many_in_connection(
        db,
        team_id=team_id,
        actor_user_id=user_id,
        subagent_id=agent_id,
        now=datetime.now(UTC),
    )
    decision = decisions.get(agent_id)
    return decision is not None and decision.resolution.can_consume


async def _validate_team_binding_for_fire(
    db: aiosqlite.Connection,
    *,
    binding: ScheduledAgentBinding,
    team_id: str,
    user_id: str,
    db_path: str,
) -> str | None:
    async with db.execute(
        """
        SELECT agent.live_revision_id,agent.live_release_id,
               current_release.capability_fingerprint AS current_capability_fingerprint,
               current_release.dependency_fingerprint AS current_dependency_fingerprint
        FROM scheduled_jobs job
        JOIN workspaces workspace
          ON workspace.id=job.workspace_id AND workspace.team_id=?
         AND workspace.status='active'
        JOIN users owner ON owner.id=job.user_id AND owner.id=? AND owner.status='active'
        JOIN team_members member
          ON member.team_id=workspace.team_id AND member.user_id=owner.id
         AND member.status='active'
        JOIN agent_assignment assignment
          ON assignment.id=? AND assignment.team_id=workspace.team_id
         AND assignment.user_id=owner.id AND assignment.agent_id=?
         AND assignment.agent_source_kind='team'
         AND assignment.personal_state='active' AND assignment.access_state='granted'
        JOIN team_subagent agent
          ON agent.team_id=workspace.team_id AND agent.id=assignment.agent_id
         AND agent.status='enabled' AND agent.lifecycle_state='active'
         AND agent.definition_mode='revisioned'
        JOIN team_subagent_release current_release
          ON current_release.team_id=agent.team_id
         AND current_release.subagent_id=agent.id
         AND current_release.id=agent.live_release_id
         AND current_release.revision_id=agent.live_revision_id
        WHERE job.id=?
        """,
        (
            team_id,
            user_id,
            binding.assignment_id,
            binding.agent_id,
            binding.job_id,
        ),
    ) as cursor:
        current = await cursor.fetchone()
    if current is None:
        return "scheduled_recipe_lost"

    access = await TeamSubagentAccessResolver(db_path).resolve_many_in_connection(
        db,
        team_id=team_id,
        actor_user_id=user_id,
        subagent_id=binding.agent_id,
        now=datetime.now(UTC),
    )
    decision = access.get(binding.agent_id)
    if decision is None or not decision.resolution.can_consume:
        return "scheduled_recipe_lost"

    async with db.execute(
        """
        SELECT revision.definition_json,revision.definition_hash,
               release.capability_fingerprint,release.dependency_fingerprint,
               safety.status AS safety_status
        FROM team_subagent_release release
        JOIN team_subagent_revision revision
          ON revision.team_id=release.team_id
         AND revision.subagent_id=release.subagent_id
         AND revision.id=release.revision_id
        JOIN agent_safety_report safety
          ON safety.team_id=release.team_id AND safety.agent_id=release.subagent_id
         AND safety.id=release.safety_report_id
         AND safety.subject_kind='revision' AND safety.subject_ref=revision.id
         AND safety.dependency_fingerprint=revision.dependency_fingerprint
        WHERE release.team_id=? AND release.subagent_id=?
          AND release.id=? AND release.revision_id=?
          AND release.capability_fingerprint=?
          AND release.dependency_fingerprint=?
          AND release.capability_fingerprint=revision.capability_fingerprint
          AND release.dependency_fingerprint=revision.dependency_fingerprint
        """,
        (
            team_id,
            binding.agent_id,
            binding.release_id,
            binding.revision_ref,
            binding.capability_fingerprint,
            binding.dependency_fingerprint,
        ),
    ) as cursor:
        exact = await cursor.fetchone()
    if exact is None or str(exact["safety_status"]) in {"blocked", "unknown"}:
        return "scheduled_recipe_lost"
    if _hash_text(str(exact["definition_json"])) != str(exact["definition_hash"]):
        return "scheduled_recipe_rejected"

    if (
        str(current["live_revision_id"]) != binding.revision_ref
        or str(current["live_release_id"]) != str(binding.release_id or "")
        or str(current["current_capability_fingerprint"]) != binding.capability_fingerprint
        or str(current["current_dependency_fingerprint"]) != binding.dependency_fingerprint
    ):
        return "scheduled_recipe_changed"
    return None


async def _validate_system_binding_for_fire(
    db: aiosqlite.Connection,
    *,
    binding: ScheduledAgentBinding,
    team_id: str,
    user_id: str,
) -> str | None:
    async with db.execute(
        """
        SELECT revision.definition_json,revision.definition_hash
        FROM scheduled_jobs job
        JOIN workspaces workspace
          ON workspace.id=job.workspace_id AND workspace.team_id=?
         AND workspace.status='active'
        JOIN users owner ON owner.id=job.user_id AND owner.id=? AND owner.status='active'
        JOIN team_members member
          ON member.team_id=workspace.team_id AND member.user_id=owner.id
         AND member.status='active'
        JOIN system_agent_effective_revision revision
          ON revision.team_id=workspace.team_id
         AND revision.agent_id=? AND revision.id=?
        WHERE job.id=?
          AND (? IS NULL OR EXISTS (
            SELECT 1 FROM agent_assignment assignment
            WHERE assignment.id=? AND assignment.team_id=workspace.team_id
              AND assignment.user_id=owner.id AND assignment.agent_id=?
              AND assignment.agent_source_kind='system'
              AND assignment.personal_state='active'
              AND assignment.access_state='granted'
          ))
        """,
        (
            team_id,
            user_id,
            binding.agent_id,
            binding.revision_ref,
            binding.job_id,
            binding.assignment_id,
            binding.assignment_id,
            binding.agent_id,
        ),
    ) as cursor:
        exact = await cursor.fetchone()
    if exact is None:
        return "scheduled_recipe_lost"
    if _hash_text(str(exact["definition_json"])) != str(exact["definition_hash"]):
        return "scheduled_recipe_rejected"
    return None


async def _set_binding_and_job_paused(
    db: aiosqlite.Connection,
    *,
    binding: ScheduledAgentBinding,
    state: ScheduledAgentBindingState,
    reason: str,
) -> None:
    timestamp = _now()
    await db.execute(
        """
        UPDATE scheduled_job_agent_binding
        SET state=?,row_version=row_version+1
        WHERE job_id=? AND row_version=?
        """,
        (state, binding.job_id, binding.row_version),
    )
    await db.execute(
        """
        UPDATE scheduled_jobs
        SET status='paused',pause_reason=?,updated_at=?
        WHERE id=? AND status='active'
        """,
        (reason, timestamp, binding.job_id),
    )


def _team_seed(
    row: aiosqlite.Row,
    *,
    confirmed_by: str,
) -> ScheduledAgentBindingSeed:
    return ScheduledAgentBindingSeed(
        assignment_id=str(row["assignment_id"]),
        agent_id=str(row["agent_id"]),
        agent_source_kind="team",
        revision_ref=str(row["revision_id"]),
        release_id=str(row["release_id"]),
        capability_fingerprint=str(row["capability_fingerprint"]),
        dependency_fingerprint=str(row["dependency_fingerprint"]),
        confirmed_by=confirmed_by,
        confirmed_at=_now(),
    )


def _binding_from_row(row: aiosqlite.Row) -> ScheduledAgentBinding:
    return ScheduledAgentBinding(
        job_id=str(row["job_id"]),
        assignment_id=(str(row["assignment_id"]) if row["assignment_id"] is not None else None),
        agent_id=str(row["agent_id"]),
        agent_source_kind=str(row["agent_source_kind"]),  # type: ignore[arg-type]
        revision_ref=str(row["revision_ref"]),
        release_id=(str(row["release_id"]) if row["release_id"] else None),
        capability_fingerprint=str(row["capability_fingerprint"]),
        dependency_fingerprint=str(row["dependency_fingerprint"]),
        follow_mode=str(row["follow_mode"]),  # type: ignore[arg-type]
        state=str(row["state"]),  # type: ignore[arg-type]
        confirmed_by=str(row["confirmed_by"]),
        confirmed_at=str(row["confirmed_at"]),
        row_version=int(row["row_version"]),
    )


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ScheduledAgentBinding",
    "ScheduledAgentBindingError",
    "ScheduledAgentBindingSeed",
    "ScheduledAgentBindingStore",
    "apply_scheduled_agent_binding_conn",
    "clear_scheduled_agent_binding_conn",
]
