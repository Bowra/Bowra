"""ScheduledJobStore — scheduled jobs CRUD + run history (#84 Step 1).

按 docs/scheduled-task-design.md §数据模型 建表. v1 只做 CRUD + run-now；独立 scheduler
worker + SMTP 通知 defer 到 Step 2（建新 task 追踪）.

schema 见 SCHEMA 常量.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core.clarification_store import (
    ClarificationIdentity,
    ClarificationInvalidation,
    ClarificationInvalidationReason,
    ClarificationTerminalStatus,
    clarification_invalidation_event_id,
    invalidate_pending_in_tx,
    record_clarification_invalidation_metrics,
)
from bowhead_core.scheduled_agent_binding import ScheduledAgentBindingSeed

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    name                TEXT NOT NULL,
    cron                TEXT NOT NULL,
    tz                  TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    query               TEXT NOT NULL,
    session_strategy    TEXT NOT NULL DEFAULT 'fresh',
    anchor_session_id   TEXT,
    notify_channels     TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'active',
    pause_reason        TEXT,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    next_run_at         TEXT,
    last_run_at         TEXT,
    last_turn_id        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    datarc_workspace_id TEXT,            -- 批2/c9: 创建时快照（routine pin）
    public_entry_id     TEXT             -- 创建时公开入口快照（通知深链）
);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_user
    ON scheduled_jobs(user_id, status);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_next_run
    ON scheduled_jobs(next_run_at, status);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_workspace_status
    ON scheduled_jobs(workspace_id, status);

CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL,
    turn_id             TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,
    error               TEXT,
    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_sjr_job
    ON scheduled_job_runs(job_id, started_at DESC);
"""

# At most one in-flight run per job: the DB-level guarantee behind has_active_run, so a cron tick racing
# run-now (or run-now racing run-now) can't both INSERT a 'running' row (the app-level check-then-insert is
# a TOCTOU). The loser's INSERT hits this and start_run raises ActiveRunExistsError.
# Created in initialize() (NOT in SCHEMA) with a heal-then-create, so a DB carrying pre-existing duplicate
# 'running' rows (an unclean shutdown before this version, before recover_running_runs ran) doesn't
# IntegrityError-crash gateway boot.
# The predicate covers 'awaiting_input' too — a run parked on a clarification is still in flight and must
# keep blocking a second run for the same job. `IF NOT EXISTS` is a no-op against an already-created index,
# so an existing deployment only picks the widened predicate up from alembic revision
# 0011_scheduled_recipe_runs (which DROPs and recreates the index under the same name). Keep this string
# and the revision's predicate identical, or migrated and freshly-initialised databases diverge.
_RUNNING_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sjr_one_running ON scheduled_job_runs(job_id) WHERE status IN ('running','awaiting_input')"
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


class ActiveRunExistsError(Exception):
    """start_run lost the race: a 'running' run already exists for this job (DB unique-index
    guard). Callers map this to 409 (run-now) / skip (cron tick)."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id} already has an in-flight run")
        self.job_id = job_id


@dataclass
class ScheduledJob:
    id: str
    user_id: str
    workspace_id: str
    name: str
    cron: str
    query: str
    tz: str = "Asia/Shanghai"
    session_strategy: str = "fresh"
    anchor_session_id: str | None = None
    notify_channels: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    pause_reason: str | None = None
    failure_count: int = 0
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_turn_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # Batch 2/c9 (PRD §3.9 decision 10): snapshot Datarc workspace at creation; routine always queries
    # this one (never drifts with owner preference); owner loses access → pause+notify. None=legacy row.
    datarc_workspace_id: str | None = None
    public_entry_id: str | None = None
    # scheduled-recipe-runs S1: bound frozen workflow reference (JSON: {form, id_or_name, bound_sha?}).
    # Store only reference/fingerprint, **never owner/team**: recalculate authorization scope from job.user_id whenever firing.
    recipe_ref: str | None = None
    run_params: str | None = None


@dataclass
class ScheduledJobRun:
    id: str
    job_id: str
    turn_id: str | None
    started_at: str
    finished_at: str | None
    # running | awaiting_input | success | failed | skipped | expired
    # awaiting_input = reached ambiguity, waiting for owner answer (S3). It **still occupies the in-flight unique index**:
    # a suspended run has not ended; do not start a second turn for the same job.
    status: str
    error: str | None


@dataclass(frozen=True)
class ScheduledAwaitingTransitionResult:
    won: bool
    cause: str | None = None
    invalidations: tuple[ClarificationInvalidation, ...] = ()


@dataclass(frozen=True)
class ScheduledDeleteResult:
    deleted: bool
    cause: str | None = None
    invalidations: tuple[ClarificationInvalidation, ...] = ()


class ScheduledRunSourceError(RuntimeError):
    """A source-aware run start no longer owns an eligible job."""

    def __init__(self, job_id: str, cause: str) -> None:
        super().__init__(f"scheduled job {job_id} cannot start: {cause}")
        self.job_id = job_id
        self.cause = cause


# Rate limit: maximum active jobs per user.
MAX_ACTIVE_JOBS_PER_USER = 10

#: Pause reason written on space archival; unarchive resumes only pauses with this reason.
ARCHIVE_PAUSE_REASON = "workspace_archived"


@dataclass(frozen=True)
class ScheduledArchiveResumeResult:
    """恢复空间时的任务结果：已恢复的，以及因所有者启用数已满而仍暂停的。"""

    resumed: list[str]
    over_limit: list[str]


def _row_to_job(row: aiosqlite.Row) -> ScheduledJob:
    try:
        notify = json.loads(row["notify_channels"] or "{}")
    except (json.JSONDecodeError, TypeError):
        notify = {}
    return ScheduledJob(
        id=row["id"],
        user_id=row["user_id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        cron=row["cron"],
        tz=row["tz"],
        query=row["query"],
        session_strategy=row["session_strategy"],
        anchor_session_id=row["anchor_session_id"],
        notify_channels=notify,
        status=row["status"],
        pause_reason=(row["pause_reason"] if "pause_reason" in row.keys() else None),  # noqa: SIM118
        failure_count=row["failure_count"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        last_turn_id=row["last_turn_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        # sqlite3.Row lacks .get; `x in row` checks **values**, not keys. `in row.keys()`
        # is the only correct form; never "simplify" under SIM118 (06ba6e19 did so, causing runtime AttributeError).
        datarc_workspace_id=(
            row["datarc_workspace_id"] if "datarc_workspace_id" in row.keys() else None  # noqa: SIM118
        ),
        public_entry_id=(
            row["public_entry_id"] if "public_entry_id" in row.keys() else None  # noqa: SIM118
        ),
        recipe_ref=(row["recipe_ref"] if "recipe_ref" in row.keys() else None),  # noqa: SIM118
        run_params=(row["run_params"] if "run_params" in row.keys() else None),  # noqa: SIM118
    )


def _row_to_run(row: aiosqlite.Row) -> ScheduledJobRun:
    return ScheduledJobRun(
        id=row["id"],
        job_id=row["job_id"],
        turn_id=row["turn_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        error=row["error"],
    )


class ScheduledJobStore:
    def __init__(self, db_path: str):
        self._db_path = str(db_path)

    @property
    def db_path(self) -> str:
        return self._db_path

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            # Batch 2/c9: add snapshot column to old DBs (idempotent).
            async with db.execute("PRAGMA table_info(scheduled_jobs)") as cur:
                _cols = {r[1] for r in await cur.fetchall()}
            if "datarc_workspace_id" not in _cols:
                await db.execute("ALTER TABLE scheduled_jobs ADD COLUMN datarc_workspace_id TEXT")
            if "public_entry_id" not in _cols:
                await db.execute("ALTER TABLE scheduled_jobs ADD COLUMN public_entry_id TEXT")
            if "pause_reason" not in _cols:
                await db.execute("ALTER TABLE scheduled_jobs ADD COLUMN pause_reason TEXT")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_workspace_status ON scheduled_jobs(workspace_id, status)",
            )
            # Heal-then-create the partial unique index: only sweeps 'running'→'failed' if the index
            # actually conflicts (first deploy over a DB with pre-existing dup running rows); a clean DB
            # just creates it. Prevents an IntegrityError from bricking boot.
            try:
                await db.execute(_RUNNING_UNIQUE_INDEX)
            except aiosqlite.IntegrityError:
                await db.execute(
                    # Predicate now includes awaiting_input; healing must cover it too, or two awaiting rows for one job
                    # remain, retry raises IntegrityError again → boot crash.
                    "UPDATE scheduled_job_runs SET status='failed', finished_at=?, error='scheduled_run_interrupted' "
                    "WHERE status IN ('running','awaiting_input')",
                    (_now(),),
                )
                await db.execute(_RUNNING_UNIQUE_INDEX)
            await db.commit()

    async def create(
        self,
        *,
        user_id: str,
        workspace_id: str,
        name: str,
        cron: str,
        query: str,
        tz: str = "Asia/Shanghai",
        session_strategy: str = "fresh",
        anchor_session_id: str | None = None,
        notify_channels: dict[str, Any] | None = None,
        datarc_workspace_id: str | None = None,
        public_entry_id: str | None = None,
        recipe_ref: str | None = None,
        agent_binding: ScheduledAgentBindingSeed | None = None,
    ) -> ScheduledJob:
        # Rate limit: active-job count cap.
        active = await self.count_active(user_id=user_id)
        if active >= MAX_ACTIVE_JOBS_PER_USER:
            raise ValueError(
                f"user {user_id} already has {active} active jobs (max {MAX_ACTIVE_JOBS_PER_USER})",
            )
        job = ScheduledJob(
            id=f"sj_{uuid4().hex[:12]}",
            user_id=user_id,
            workspace_id=workspace_id,
            name=name,
            cron=cron,
            query=query,
            tz=tz,
            session_strategy=session_strategy,
            anchor_session_id=anchor_session_id,
            notify_channels=notify_channels or {},
            created_at=_now(),
            updated_at=_now(),
            datarc_workspace_id=datarc_workspace_id,
            public_entry_id=public_entry_id,
            recipe_ref=recipe_ref or None,
        )
        # recipe_ref was added by alembic after 1.0.0 (`SCHEMA` is transitional compatibility code and must not
        # expand per docs/design/数据库迁移.md). Include it in INSERT only when actually binding, so
        # unmigrated DBs (store.initialize() only, e.g. unit tests) can still create ordinary jobs, matching patch()'s
        # dynamic-column construction.
        cols = [
            "id",
            "user_id",
            "workspace_id",
            "name",
            "cron",
            "tz",
            "query",
            "session_strategy",
            "anchor_session_id",
            "notify_channels",
            "status",
            "failure_count",
            "created_at",
            "updated_at",
            "datarc_workspace_id",
            "public_entry_id",
        ]
        vals: list[Any] = [
            job.id,
            job.user_id,
            job.workspace_id,
            job.name,
            job.cron,
            job.tz,
            job.query,
            job.session_strategy,
            job.anchor_session_id,
            json.dumps(job.notify_channels),
            job.status,
            0,
            job.created_at,
            job.updated_at,
            job.datarc_workspace_id,
            job.public_entry_id,
        ]
        if job.recipe_ref:
            cols.append("recipe_ref")
            vals.append(job.recipe_ref)
        placeholders = ", ".join("?" for _ in cols)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    f"INSERT INTO scheduled_jobs({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608 — fixed column set
                    vals,
                )
                if agent_binding is not None:
                    from bowhead_core.scheduled_agent_binding import (
                        apply_scheduled_agent_binding_conn,
                    )

                    await apply_scheduled_agent_binding_conn(
                        db,
                        db_path=self._db_path,
                        job_id=job.id,
                        seed=agent_binding,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return job

    async def get(self, job_id: str) -> ScheduledJob | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ?",
                (job_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def list_for_user(self, user_id: str) -> list[ScheduledJob]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_jobs WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def count_active(self, user_id: str) -> int:
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def patch(
        self,
        job_id: str,
        *,
        name: str | None = None,
        cron: str | None = None,
        status: str | None = None,
        query: str | None = None,
        tz: str | None = None,
        session_strategy: str | None = None,
        anchor_session_id: str | None = None,
        notify_channels: dict[str, Any] | None = None,
        pause_reason: str | None = None,
        recipe_ref: str | None = None,
        agent_binding: ScheduledAgentBindingSeed | None = None,
        clear_agent_binding: bool = False,
        expected_updated_at: str | None = None,
        expected_agent_assignment: tuple[str, str, str, int] | None = None,
    ) -> bool:
        sets: list[str] = []
        vals: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if cron is not None:
            sets.append("cron = ?")
            vals.append(cron)
        if query is not None:
            sets.append("query = ?")
            vals.append(query)
        if tz is not None:
            sets.append("tz = ?")
            vals.append(tz)
        if session_strategy is not None:
            if session_strategy not in ("fresh", "anchor"):
                raise ValueError(f"invalid session_strategy: {session_strategy}")
            sets.append("session_strategy = ?")
            vals.append(session_strategy)
        if anchor_session_id is not None:
            sets.append("anchor_session_id = ?")
            vals.append(anchor_session_id)
        if notify_channels is not None:
            sets.append("notify_channels = ?")
            vals.append(json.dumps(notify_channels))
        if recipe_ref is not None:
            # Empty string = unbind (set column NULL). Validation/flag gates are in workflow_binding.validate_and_bind;
            # all three write paths go through it; this only persists.
            sets.append("recipe_ref = ?")
            vals.append(recipe_ref or None)
        if status is not None:
            if status not in ("active", "paused", "expired"):
                raise ValueError(f"invalid status: {status}")
            sets.append("status = ?")
            vals.append(status)
            # Un-pausing clears the strike count, else the job re-pauses on the very next failure
            # (it was auto-paused at failure_count>=3 and _reset only runs on success).
            if status == "active":
                sets.append("failure_count = ?")
                vals.append(0)
                sets.append("pause_reason = NULL")
        # Pause reason conflicts with status=active clearing semantics (reenabling clears it), so handle it afterward.
        # System pauses (invalid workflow/content change/admission failure) let the UI distinguish "paused by me"
        # from "paused by system" and offer the relevant repair entry.
        if pause_reason is not None and status != "active":
            sets.append("pause_reason = ?")
            vals.append(pause_reason or None)
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(job_id)
        where = "id = ?"
        if expected_updated_at is not None:
            where += " AND updated_at = ?"
            vals.append(expected_updated_at)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if expected_agent_assignment is not None:
                team_id, user_id, agent_id, row_version = expected_agent_assignment
                async with db.execute(
                    """
                    SELECT personal_state,row_version FROM agent_assignment
                    WHERE team_id=? AND user_id=? AND agent_id=?
                    """,
                    (team_id, user_id, agent_id),
                ) as cursor:
                    assignment = await cursor.fetchone()
                if assignment is None or str(assignment["personal_state"]) != "active" or int(assignment["row_version"]) != row_version:
                    await db.execute("ROLLBACK")
                    return False
            cur = await db.execute(
                f"UPDATE scheduled_jobs SET {', '.join(sets)} WHERE {where}",  # noqa: S608 — fixed column set
                vals,
            )
            if cur.rowcount > 0:
                if agent_binding is not None:
                    from bowhead_core.scheduled_agent_binding import (
                        apply_scheduled_agent_binding_conn,
                    )

                    await apply_scheduled_agent_binding_conn(
                        db,
                        db_path=self._db_path,
                        job_id=job_id,
                        seed=agent_binding,
                    )
                elif clear_agent_binding:
                    from bowhead_core.scheduled_agent_binding import (
                        clear_scheduled_agent_binding_conn,
                    )

                    await clear_scheduled_agent_binding_conn(
                        db,
                        job_id=job_id,
                    )
            await db.commit()
            return cur.rowcount > 0

    async def pause_active_for_workspace(
        self,
        workspace_id: str,
        *,
        reason: str = ARCHIVE_PAUSE_REASON,
    ) -> int:
        """Pause active jobs for one archived Space without touching existing pauses.

        Symmetric with :meth:`resume_archived_for_workspace`: the UPDATE only lands
        while the Space is still archived.  Archive and restore each converge jobs
        after their own commit, so a restore that commits (and resumes) before the
        archive side gets here must not be followed by a late pause that leaves an
        active Space with archive-paused jobs.
        """

        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE scheduled_jobs SET status='paused',pause_reason=?,updated_at=? "
                "WHERE workspace_id=? AND status='active' "
                "AND EXISTS (SELECT 1 FROM workspaces WHERE id=? AND status='archived')",
                (reason, _now(), workspace_id, workspace_id),
            )
            await db.commit()
            return cur.rowcount

    async def resume_archived_for_workspace(
        self,
        workspace_id: str,
        *,
        now: datetime | None = None,
    ) -> ScheduledArchiveResumeResult:
        """Resume the jobs that archiving this Space paused, once the Space is active again.

        Only ``status='paused' AND pause_reason='workspace_archived'`` rows qualify:
        archiving pauses active jobs with exactly that reason, while a user's own
        pause, failure auto-pauses and binding pauses carry another (or no) reason
        and stay paused.  Resuming follows the manual un-pause semantics: clear the
        reason and strike count, and compute ``next_run_at`` from ``now`` (missed
        fires are not replayed).  The per-owner active-job cap still applies; a job
        over the cap stays paused with ``scheduled_job_limit_reached``.

        The Space status is re-read inside the same write transaction, so a restore
        racing a re-archive never re-activates jobs of an archived Space.
        """

        from bowhead_core.scheduler_loop import next_run_for_job

        moment = now or datetime.now(tz=UTC)
        stamp = _now()
        resumed: list[str] = []
        over_limit: list[str] = []
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT status FROM workspaces WHERE id=?",
                    (workspace_id,),
                ) as cursor:
                    workspace = await cursor.fetchone()
                if workspace is None or str(workspace["status"]) != "active":
                    await db.rollback()
                    return ScheduledArchiveResumeResult(resumed=[], over_limit=[])
                async with db.execute(
                    "SELECT id,user_id,cron,tz FROM scheduled_jobs "
                    "WHERE workspace_id=? AND status='paused' AND pause_reason=? "
                    "ORDER BY created_at, id",
                    (workspace_id, ARCHIVE_PAUSE_REASON),
                ) as cursor:
                    rows = await cursor.fetchall()
                active_by_user: dict[str, int] = {}
                for row in rows:
                    user_id = str(row["user_id"])
                    if user_id not in active_by_user:
                        async with db.execute(
                            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id=? AND status='active'",
                            (user_id,),
                        ) as cursor:
                            count_row = await cursor.fetchone()
                        active_by_user[user_id] = int(count_row[0]) if count_row else 0
                    if active_by_user[user_id] >= MAX_ACTIVE_JOBS_PER_USER:
                        await db.execute(
                            "UPDATE scheduled_jobs SET pause_reason=?,updated_at=? WHERE id=? AND status='paused' AND pause_reason=?",
                            ("scheduled_job_limit_reached", stamp, row["id"], ARCHIVE_PAUSE_REASON),
                        )
                        over_limit.append(str(row["id"]))
                        continue
                    nxt = next_run_for_job(str(row["cron"]), row["tz"], moment)
                    await db.execute(
                        "UPDATE scheduled_jobs SET status='active',pause_reason=NULL,failure_count=0,"
                        "next_run_at=?,updated_at=? WHERE id=? AND status='paused' AND pause_reason=?",
                        (nxt.isoformat() if nxt is not None else None, stamp, row["id"], ARCHIVE_PAUSE_REASON),
                    )
                    active_by_user[user_id] += 1
                    resumed.append(str(row["id"]))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return ScheduledArchiveResumeResult(resumed=resumed, over_limit=over_limit)

    async def set_next_run_at(self, job_id: str, next_run_at: str | None) -> None:
        """Set (or clear, when None — e.g. @manual) a job's next_run_at. Used after the schedule changes."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE scheduled_jobs SET next_run_at = ?, updated_at = ? WHERE id = ?",
                (next_run_at, _now(), job_id),
            )
            await db.commit()

    async def delete_with_clarification_invalidation(
        self,
        job_id: str,
    ) -> ScheduledDeleteResult:
        """Delete a job without orphaning an active or awaiting clarification."""

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM scheduled_jobs WHERE id=?",
                (job_id,),
            ) as cursor:
                job = await cursor.fetchone()
            if job is None:
                await db.execute("ROLLBACK")
                return ScheduledDeleteResult(False, "not_found")
            async with db.execute(
                "SELECT * FROM scheduled_job_runs WHERE job_id=? AND status IN ('running','awaiting_input') ORDER BY started_at",
                (job_id,),
            ) as cursor:
                active_runs = await cursor.fetchall()
            if any(str(row["status"]) == "running" for row in active_runs):
                await db.execute("ROLLBACK")
                return ScheduledDeleteResult(False, "run_active")
            invalidations: list[ClarificationInvalidation] = []
            now = _now()
            for run in active_runs:
                turn_id = str(run["turn_id"] or "")
                if not turn_id:
                    await db.execute("ROLLBACK")
                    return ScheduledDeleteResult(False, "turn_missing")
                if await self._turn_is_v1_in_tx(db, turn_id):
                    await db.execute("ROLLBACK")
                    return ScheduledDeleteResult(False, "terminal_protocol_v1")
                async with db.execute(
                    "SELECT request_id,turn_id,status FROM clarification_requests WHERE turn_id=? ORDER BY created_at,request_id",
                    (turn_id,),
                ) as cursor:
                    clarification_rows = await cursor.fetchall()
                if not clarification_rows:
                    await db.execute("ROLLBACK")
                    return ScheduledDeleteResult(False, "clarification_missing")
                statuses = {str(row["status"]) for row in clarification_rows}
                if "resolved" in statuses:
                    await db.execute("ROLLBACK")
                    return ScheduledDeleteResult(False, "resolution_in_flight")
                pending = [
                    ClarificationIdentity(
                        request_id=str(row["request_id"]),
                        turn_id=turn_id,
                    )
                    for row in clarification_rows
                    if str(row["status"]) == "pending"
                ]
                invalidations.extend(
                    await invalidate_pending_in_tx(
                        db,
                        pending,
                        target_status="dismissed",
                        reason="scheduled_job_deleted",
                        actor="system",
                        now=now,
                    ),
                )
            await db.execute(
                "DELETE FROM scheduled_job_runs WHERE job_id=?",
                (job_id,),
            )
            cursor = await db.execute(
                "DELETE FROM scheduled_jobs WHERE id=?",
                (job_id,),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return ScheduledDeleteResult(False, "delete_race_lost")
            await db.commit()
        record_clarification_invalidation_metrics(
            invalidations,
            outcome="transitioned",
        )
        return ScheduledDeleteResult(
            True,
            invalidations=tuple(invalidations),
        )

    async def delete(self, job_id: str) -> bool:
        result = await self.delete_with_clarification_invalidation(job_id)
        return result.deleted

    # ─── Run records ───────────────────────────────────────
    async def start_run(
        self,
        job_id: str,
        *,
        source: Literal["scheduler", "run_now"] = "run_now",
        expected_job_updated_at: str | None = None,
    ) -> ScheduledJobRun:
        """Create a run after validating the caller's durable source contract.

        Cron snapshots require the job to remain active.  An owner-triggered
        run-now may intentionally retry an ordinary paused job, but an Agent
        authorization pause must first be explicitly restored.  The optional
        row timestamp fences authorization changes between route preflight and
        this transaction.
        """

        if source not in {"scheduler", "run_now"}:
            raise ValueError("unknown scheduled run source")
        run = ScheduledJobRun(
            id=f"sr_{uuid4().hex[:12]}",
            job_id=job_id,
            turn_id=None,
            started_at=_now(),
            finished_at=None,
            status="running",
            error=None,
        )
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT status,pause_reason,updated_at FROM scheduled_jobs WHERE id=?",
                (job_id,),
            ) as cursor:
                job_row = await cursor.fetchone()
            if job_row is None:
                await db.execute("ROLLBACK")
                raise ScheduledRunSourceError(job_id, "not_found")
            if expected_job_updated_at is not None and str(job_row["updated_at"]) != expected_job_updated_at:
                await db.execute("ROLLBACK")
                raise ScheduledRunSourceError(job_id, "job_changed")
            if source == "scheduler" and str(job_row["status"]) != "active":
                await db.execute("ROLLBACK")
                raise ScheduledRunSourceError(job_id, "not_active")
            pause_reason = str(job_row["pause_reason"] or "")
            if source == "run_now" and str(job_row["status"]) != "active" and pause_reason.startswith("agent_"):
                await db.execute("ROLLBACK")
                raise ScheduledRunSourceError(job_id, "agent_authorization_paused")
            try:
                await db.execute(
                    "INSERT INTO scheduled_job_runs(id, job_id, turn_id, started_at, "
                    "finished_at, status, error) VALUES (?, ?, NULL, ?, NULL, 'running', NULL)",
                    (run.id, job_id, run.started_at),
                )
            except aiosqlite.IntegrityError as exc:
                # idx_sjr_one_running: another run is already in-flight for this job (lost the race).
                await db.execute("ROLLBACK")
                raise ActiveRunExistsError(job_id) from exc
            await db.execute(
                "UPDATE scheduled_jobs SET last_run_at = ?, updated_at = ? WHERE id = ?",
                (run.started_at, run.started_at, job_id),
            )
            await db.commit()
        return run

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        turn_id: str | None = None,
        error: str | None = None,
        expected_status: str | None = None,
    ) -> bool:
        """终结一个 running run。返回是否真的写入。

        ``awaiting_input`` owns a clarification lifecycle and is deliberately
        excluded: callers must use one of the atomic awaiting settlement APIs
        so a terminal run can never leave an actionable card behind.
        """
        if expected_status not in {None, "running"}:
            raise ValueError(
                "awaiting scheduled runs require an atomic settlement API",
            )
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE scheduled_job_runs SET finished_at=?,status=?,turn_id=COALESCE(?,turn_id),error=? WHERE id=? AND status='running'",
                (_now(), status, turn_id, error, run_id),
            )
            if cur.rowcount == 0:
                await db.commit()
                return False
            if turn_id:
                await db.execute(
                    "UPDATE scheduled_jobs SET last_turn_id = ?, updated_at = ? WHERE id = (SELECT job_id FROM scheduled_job_runs WHERE id = ?)",
                    (turn_id, _now(), run_id),
                )
            await db.commit()
            return True

    @staticmethod
    async def _turn_is_v1_in_tx(
        db: aiosqlite.Connection,
        turn_id: str,
    ) -> bool:
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'",
        ) as cursor:
            has_turns = await cursor.fetchone()
        if has_turns is None:
            return False
        async with db.execute(
            "SELECT terminal_protocol_version FROM turns WHERE id=?",
            (turn_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row is not None and int(row[0] or 0) == 1)

    async def mark_run_awaiting_if_pending(
        self,
        run_id: str,
        turn_id: str,
    ) -> ScheduledAwaitingTransitionResult:
        """Publish ``awaiting_input`` only for a durable legacy pending card."""

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT status FROM scheduled_job_runs WHERE id=?",
                (run_id,),
            ) as cursor:
                run = await cursor.fetchone()
            if run is None:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_not_found")
            if str(run["status"]) != "running":
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_not_running")
            if await self._turn_is_v1_in_tx(db, turn_id):
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "terminal_protocol_v1")
            async with db.execute(
                "SELECT status FROM clarification_requests WHERE turn_id=?",
                (turn_id,),
            ) as cursor:
                statuses = [str(row["status"]) for row in await cursor.fetchall()]
            if "resolved" in statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "resolution_won")
            if "pending" not in statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "pending_missing")
            cursor = await db.execute(
                "UPDATE scheduled_job_runs SET status='awaiting_input',turn_id=? WHERE id=? AND status='running'",
                (turn_id, run_id),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_race_lost")
            await db.commit()
        return ScheduledAwaitingTransitionResult(True)

    async def mark_run_awaiting(self, run_id: str, turn_id: str) -> bool:
        """Compatibility helper for fixtures predating the pending-card fence.

        Production callers must use ``mark_run_awaiting_if_pending`` after the
        producer-local Turn persistence drain.  Keeping this low-level CAS
        avoids silently changing old data/fixture construction utilities.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE scheduled_job_runs SET status='awaiting_input',turn_id=? WHERE id=? AND status='running'",
                (turn_id, run_id),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def finish_awaiting_with_clarification_invalidation(
        self,
        run_id: str,
        *,
        run_status: Literal["failed", "skipped"],
        run_error: Literal[
            "scheduled_input_timeout",
            "scheduled_input_superseded",
            "scheduled_anchor_session_deleted",
            "scheduled_turn_failed",
        ],
        clarification_status: ClarificationTerminalStatus,
        clarification_reason: ClarificationInvalidationReason,
    ) -> ScheduledAwaitingTransitionResult:
        """Atomically leave awaiting and invalidate every owned clarification."""

        expected_transition = {
            "scheduled_input_timeout": (
                "failed",
                "expired",
                "scheduled_input_timeout",
            ),
            "scheduled_input_superseded": (
                "skipped",
                "dismissed",
                "scheduled_input_superseded",
            ),
            "scheduled_anchor_session_deleted": (
                "failed",
                "dismissed",
                "scheduled_session_deleted",
            ),
            "scheduled_turn_failed": (
                "failed",
                "dismissed",
                "scheduled_turn_terminated",
            ),
        }.get(run_error)
        if expected_transition != (
            run_status,
            clarification_status,
            clarification_reason,
        ):
            raise ValueError("scheduled awaiting terminal transition is invalid")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM scheduled_job_runs WHERE id=?",
                (run_id,),
            ) as cursor:
                run = await cursor.fetchone()
            if run is None:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_not_found")
            if str(run["status"]) != "awaiting_input":
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_not_awaiting")
            turn_id = str(run["turn_id"] or "")
            if not turn_id:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "turn_missing")
            if await self._turn_is_v1_in_tx(db, turn_id):
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "terminal_protocol_v1")
            async with db.execute(
                "SELECT request_id,turn_id,status FROM clarification_requests WHERE turn_id=? ORDER BY created_at,request_id",
                (turn_id,),
            ) as cursor:
                clarification_rows = await cursor.fetchall()
            if not clarification_rows:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "clarification_missing")
            statuses = {str(row["status"]) for row in clarification_rows}
            if "resolved" in statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "resolution_won")
            unknown_statuses = statuses - {
                "pending",
                "dismissed",
                "expired",
            }
            if unknown_statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(
                    False,
                    "clarification_terminal_conflict",
                )
            now = _now()
            pending = [
                ClarificationIdentity(
                    request_id=str(row["request_id"]),
                    turn_id=turn_id,
                )
                for row in clarification_rows
                if str(row["status"]) == "pending"
            ]
            invalidations = await invalidate_pending_in_tx(
                db,
                pending,
                target_status=clarification_status,
                reason=clarification_reason,
                actor="system",
                now=now,
            )
            # Upgrade repair: old versions may have changed the row before they
            # changed the run.  Preserve that proven status and attach the
            # missing durable intent inside this same winning transaction.
            transitioned_ids = {item.request_id for item in invalidations}
            for row in clarification_rows:
                request_id = str(row["request_id"])
                row_status = str(row["status"])
                if request_id in transitioned_ids or row_status == "pending" or row_status != clarification_status:
                    continue
                event_id = clarification_invalidation_event_id(request_id)
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO clarification_invalidation_outbox(
                      request_id,turn_id,event_id,target_status,reason_code,
                      actor_kind,state,lease_owner,lease_expires_at,attempt_count,
                      created_at,updated_at,delivered_at
                    ) VALUES (?,?,?,?,?,'system','pending',NULL,NULL,0,?,?,NULL)
                    """,
                    (
                        request_id,
                        turn_id,
                        event_id,
                        clarification_status,
                        clarification_reason,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    invalidations.append(
                        ClarificationInvalidation(
                            request_id=request_id,
                            turn_id=turn_id,
                            event_id=event_id,
                            target_status=clarification_status,
                            reason=clarification_reason,
                            actor="system",
                        ),
                    )
            cursor = await db.execute(
                "UPDATE scheduled_job_runs SET finished_at=?,status=?,error=? WHERE id=? AND status='awaiting_input'",
                (now, run_status, run_error, run_id),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_race_lost")
            await db.execute(
                "UPDATE scheduled_jobs SET last_turn_id=?,updated_at=? WHERE id=?",
                (turn_id, now, str(run["job_id"])),
            )
            await db.commit()
        record_clarification_invalidation_metrics(
            invalidations,
            outcome="transitioned",
        )
        return ScheduledAwaitingTransitionResult(
            True,
            invalidations=tuple(invalidations),
        )

    async def finish_awaiting_after_resolution(
        self,
        run_id: str,
        *,
        run_status: Literal["success", "failed"],
        run_error: str | None = None,
    ) -> ScheduledAwaitingTransitionResult:
        """Settle an answered run only after its clarification is durable.

        A resumed Turn can reach completed/failed before reconciliation sees
        it.  Re-reading the clarification and CASing the run in one SQLite
        transaction prevents an impossible terminal run from orphaning a
        still-pending card.
        """

        if run_status not in {"success", "failed"}:
            raise ValueError("scheduled answered run status is invalid")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM scheduled_job_runs WHERE id=?",
                (run_id,),
            ) as cursor:
                run = await cursor.fetchone()
            if run is None:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_not_found")
            if str(run["status"]) != "awaiting_input":
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_not_awaiting")
            turn_id = str(run["turn_id"] or "")
            if not turn_id:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "turn_missing")
            if await self._turn_is_v1_in_tx(db, turn_id):
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "terminal_protocol_v1")
            async with db.execute(
                "SELECT status FROM clarification_requests WHERE turn_id=?",
                (turn_id,),
            ) as cursor:
                statuses = {str(row["status"]) for row in await cursor.fetchall()}
            if not statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "clarification_missing")
            if "pending" in statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "pending_remaining")
            if "resolved" not in statuses:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "resolution_missing")
            now = _now()
            cursor = await db.execute(
                "UPDATE scheduled_job_runs SET finished_at=?,status=?,error=? WHERE id=? AND status='awaiting_input'",
                (now, run_status, run_error, run_id),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return ScheduledAwaitingTransitionResult(False, "run_race_lost")
            await db.execute(
                "UPDATE scheduled_jobs SET last_turn_id=?,updated_at=? WHERE id=?",
                (turn_id, now, str(run["job_id"])),
            )
            await db.commit()
        return ScheduledAwaitingTransitionResult(True)

    async def list_awaiting_runs(self) -> list[ScheduledJobRun]:
        """所有挂起中的 run（reconciliation 扫描用）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_job_runs WHERE status='awaiting_input' ORDER BY started_at ASC",
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def awaiting_turn_ids(self) -> set[str]:
        """挂起中的 run 对应的 turn_id 集合。

        供 clarification 清扫任务排除 —— 系统级 24h TTL 会把 pending 行清成 expired，
        而一个周更任务的挂起要活到下次 fire（可能一周）。这些 turn 的 clarification
        生命周期归 scheduled 侧管。
        """
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT turn_id FROM scheduled_job_runs WHERE status='awaiting_input' AND turn_id IS NOT NULL",
            ) as cur,
        ):
            return {str(row[0]) for row in await cur.fetchall()}

    async def get_run_by_turn(self, turn_id: str) -> ScheduledJobRun | None:
        """按 turn_id 反查 run —— resume 端点用它识别「这个 turn 是定时任务出身」。

        判据选它而不是 session origin / turn_id 前缀：fresh 定时 session 的 origin 是
        'normal'，anchor 模式下 session 本就是用户自己的会话，两者都判不出来；而这一查
        同时把 job 和它的绑定/pin 一起带回来。索引 idx_sjr_turn（alembic 0011）。
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_job_runs WHERE turn_id=? ORDER BY started_at DESC LIMIT 1",
                (turn_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_run(row) if row else None

    async def list_runs(self, job_id: str, limit: int = 10) -> list[ScheduledJobRun]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def has_active_run(self, job_id: str) -> bool:
        """True if the job has an in-flight run. Used to serialize run-now vs the cron worker
        (and run-now vs run-now) so the same job never runs two turns at once.

        ``awaiting_input`` 也算在飞（S3）：挂起的 run 还在等人回答，同 job 再起一个 turn
        会让两条结果撞车。与 ``idx_sjr_one_running`` 的谓词保持一致。
        """
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT 1 FROM scheduled_job_runs WHERE job_id = ? AND status IN ('running','awaiting_input') LIMIT 1",
                (job_id,),
            ) as cur,
        ):
            return await cur.fetchone() is not None

    async def recover_running_runs(self) -> int:
        """Mark every ``running`` run as ``failed`` — call once on boot. The in-process scheduler and
        run-now keep no run across a gateway restart, so any ``running`` row at startup is an orphan
        (would otherwise block run-now forever via ``has_active_run``). Returns rows recovered.

        ⚠️ Assumes a SINGLE gateway instance (the in-process scheduler already does — multiple replicas
        would double-fire cron). With >1 replica this would mark a peer's legitimately in-flight runs
        failed; a distributed scheduler must replace this boot-sweep with lease/heartbeat ownership."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE scheduled_job_runs SET status = 'failed', finished_at = ?, error = 'scheduled_run_interrupted' WHERE status = 'running'",
                (_now(),),
            )
            await db.commit()
            return cur.rowcount
