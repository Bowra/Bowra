"""Durable Agent Eval jobs and scratch-Turn provenance.

M3-10 adds lease-fenced case jobs and immutable links from every infrastructure
attempt to its isolated chat Session/Turn.  Outputs remain in the M3-9 result
table; fixtures, prompts, tool arguments, and business data are never copied
into this control-plane schema.

Revision ID: 0038_agent_eval_execution
Revises: 0037_agent_eval_store
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0038_agent_eval_execution"
down_revision = "0037_agent_eval_store"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "agent_eval_run_execution",
        """
        CREATE TABLE agent_eval_run_execution (
          run_id                TEXT PRIMARY KEY REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          workspace_id          TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
          requested_attempts    INTEGER NOT NULL CHECK(requested_attempts BETWEEN 1 AND 5),
          max_concurrency       INTEGER NOT NULL CHECK(max_concurrency BETWEEN 1 AND 8),
          max_duration_ms       INTEGER NOT NULL CHECK(max_duration_ms BETWEEN 1000 AND 3600000),
          max_cost_microusd     INTEGER NOT NULL CHECK(max_cost_microusd BETWEEN 0 AND 1000000000),
          cancel_requested_at   TEXT,
          created_at            TEXT NOT NULL,
          CHECK(team_id != '')
        )
        """,
    ),
    (
        "table",
        "agent_eval_case_job",
        """
        CREATE TABLE agent_eval_case_job (
          id                    TEXT PRIMARY KEY CHECK(id LIKE 'aej_%'),
          run_id                TEXT NOT NULL REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
          case_revision_id      TEXT NOT NULL
                                REFERENCES agent_eval_case_revision(id) ON DELETE RESTRICT,
          attempt               INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 5),
          status                TEXT NOT NULL CHECK(status IN (
                                  'queued','leased','running','completed','failed','cancelled'
                                )),
          lease_generation      INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
          lease_owner           TEXT,
          lease_token_hash      TEXT CHECK(
                                  lease_token_hash IS NULL OR lease_token_hash LIKE 'sha256:%'
                                ),
          lease_expires_at      TEXT,
          created_at            TEXT NOT NULL,
          started_at            TEXT,
          ended_at              TEXT,
          failure_code          TEXT,
          UNIQUE(run_id, case_revision_id, attempt),
          CHECK(
            (status='queued' AND lease_generation=0 AND lease_owner IS NULL
              AND lease_token_hash IS NULL AND lease_expires_at IS NULL
              AND started_at IS NULL AND ended_at IS NULL AND failure_code IS NULL)
            OR (status IN ('leased','running') AND lease_generation >= 1
              AND lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL
              AND lease_expires_at IS NOT NULL AND ended_at IS NULL
              AND failure_code IS NULL)
            OR (status IN ('completed','failed','cancelled') AND lease_generation >= 1
              AND lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL
              AND lease_expires_at IS NOT NULL AND ended_at IS NOT NULL)
          ),
          CHECK(started_at IS NULL OR lease_generation >= 1),
          CHECK(failure_code IS NULL OR status IN ('completed','failed','cancelled'))
        )
        """,
    ),
    (
        "table",
        "agent_eval_scratch_turn",
        """
        CREATE TABLE agent_eval_scratch_turn (
          job_id                TEXT NOT NULL REFERENCES agent_eval_case_job(id) ON DELETE RESTRICT,
          lease_generation      INTEGER NOT NULL CHECK(lease_generation >= 1),
          run_id                TEXT NOT NULL REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
          case_revision_id      TEXT NOT NULL
                                REFERENCES agent_eval_case_revision(id) ON DELETE RESTRICT,
          attempt               INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 5),
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          revision_ref          TEXT NOT NULL CHECK(revision_ref LIKE 'arv_%'),
          session_id            TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
          turn_id               TEXT NOT NULL REFERENCES turns(id) ON DELETE RESTRICT,
          created_at            TEXT NOT NULL,
          PRIMARY KEY(job_id, lease_generation),
          UNIQUE(session_id),
          UNIQUE(turn_id)
        )
        """,
    ),
    (
        "index",
        "idx_agent_eval_case_job_claim",
        """
        CREATE INDEX idx_agent_eval_case_job_claim
        ON agent_eval_case_job(run_id, status, lease_expires_at, created_at, id)
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_evidence_monotonic_run",
        """
        CREATE TRIGGER trg_agent_eval_evidence_monotonic_run
        BEFORE UPDATE ON agent_eval_evidence_state
        WHEN OLD.current_run_id IS NOT NULL
          AND NEW.current_run_id IS NOT NULL
          AND NEW.current_run_id IS NOT OLD.current_run_id
          AND (
            SELECT rowid FROM agent_eval_run WHERE id=OLD.current_run_id
          ) > (
            SELECT rowid FROM agent_eval_run WHERE id=NEW.current_run_id
          )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval evidence cannot regress to an older run');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_execution_scope",
        """
        CREATE TRIGGER trg_agent_eval_run_execution_scope
        BEFORE INSERT ON agent_eval_run_execution
        WHEN NOT EXISTS (
          SELECT 1
          FROM agent_eval_run run
          JOIN workspaces workspace_row
            ON workspace_row.id=NEW.workspace_id
           AND workspace_row.team_id=run.team_id
           AND workspace_row.status='active'
          JOIN users actor ON actor.id=run.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=run.team_id AND member.user_id=actor.id
           AND member.status='active'
          WHERE run.id=NEW.run_id AND run.team_id=NEW.team_id AND run.status='queued'
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval run execution scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_execution_update",
        """
        CREATE TRIGGER trg_agent_eval_run_execution_update
        BEFORE UPDATE ON agent_eval_run_execution
        WHEN NEW.run_id IS NOT OLD.run_id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.requested_attempts IS NOT OLD.requested_attempts
          OR NEW.max_concurrency IS NOT OLD.max_concurrency
          OR NEW.max_duration_ms IS NOT OLD.max_duration_ms
          OR NEW.max_cost_microusd IS NOT OLD.max_cost_microusd
          OR NEW.created_at IS NOT OLD.created_at
          OR OLD.cancel_requested_at IS NOT NULL
          OR NEW.cancel_requested_at IS NULL
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval run execution update');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_execution_delete",
        """
        CREATE TRIGGER trg_agent_eval_run_execution_delete
        BEFORE DELETE ON agent_eval_run_execution
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_run_execution is immutable');
        END
        """,
    ),
    (
        "index",
        "idx_agent_eval_scratch_run",
        """
        CREATE INDEX idx_agent_eval_scratch_run
        ON agent_eval_scratch_turn(run_id, case_revision_id, attempt, lease_generation)
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_job_scope",
        """
        CREATE TRIGGER trg_agent_eval_case_job_scope
        BEFORE INSERT ON agent_eval_case_job
        WHEN NOT EXISTS (
          SELECT 1
          FROM agent_eval_run run
          JOIN agent_eval_case_revision case_revision
            ON case_revision.id=NEW.case_revision_id
           AND case_revision.suite_revision_id=run.suite_revision_id
          WHERE run.id=NEW.run_id AND run.status='queued'
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval case job scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_job_identity",
        """
        CREATE TRIGGER trg_agent_eval_case_job_identity
        BEFORE UPDATE ON agent_eval_case_job
        WHEN NEW.id IS NOT OLD.id
          OR NEW.run_id IS NOT OLD.run_id
          OR NEW.case_revision_id IS NOT OLD.case_revision_id
          OR NEW.attempt IS NOT OLD.attempt
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.lease_generation < OLD.lease_generation
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval case job identity is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_job_transition",
        """
        CREATE TRIGGER trg_agent_eval_case_job_transition
        BEFORE UPDATE ON agent_eval_case_job
        WHEN NOT (
          (
            OLD.status='queued' AND NEW.status='leased'
            AND NEW.lease_generation=1 AND NEW.started_at IS NULL
          ) OR (
            OLD.status IN ('leased','running') AND NEW.status='leased'
            AND OLD.lease_expires_at <= NEW.lease_expires_at
            AND NEW.lease_generation=OLD.lease_generation+1
            AND NEW.started_at IS OLD.started_at
          ) OR (
            OLD.status='leased' AND NEW.status='running'
            AND NEW.lease_generation=OLD.lease_generation
            AND NEW.lease_owner=OLD.lease_owner
            AND NEW.lease_token_hash=OLD.lease_token_hash
            AND NEW.lease_expires_at=OLD.lease_expires_at
            AND NEW.started_at IS NOT NULL
          ) OR (
            OLD.status IN ('leased','running') AND NEW.status=OLD.status
            AND NEW.lease_generation=OLD.lease_generation
            AND NEW.lease_owner=OLD.lease_owner
            AND NEW.lease_token_hash=OLD.lease_token_hash
            AND NEW.lease_expires_at > OLD.lease_expires_at
            AND NEW.started_at IS OLD.started_at
          ) OR (
            OLD.status='running' AND NEW.status IN ('completed','failed','cancelled')
            AND NEW.lease_generation=OLD.lease_generation
            AND NEW.lease_owner=OLD.lease_owner
            AND NEW.lease_token_hash=OLD.lease_token_hash
            AND NEW.lease_expires_at=OLD.lease_expires_at
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NOT NULL
          ) OR (
            OLD.status='leased' AND NEW.status='cancelled'
            AND NEW.lease_generation=OLD.lease_generation
            AND NEW.lease_owner=OLD.lease_owner
            AND NEW.lease_token_hash=OLD.lease_token_hash
            AND NEW.lease_expires_at=OLD.lease_expires_at
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NOT NULL
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval case job transition');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_job_terminal_result",
        """
        CREATE TRIGGER trg_agent_eval_case_job_terminal_result
        BEFORE UPDATE ON agent_eval_case_job
        WHEN NEW.status IN ('completed','failed','cancelled') AND NOT EXISTS (
          SELECT 1 FROM agent_eval_case_result result
          WHERE result.run_id=NEW.run_id
            AND result.case_revision_id=NEW.case_revision_id
            AND result.attempt=NEW.attempt
            AND (
              (NEW.status='completed' AND result.status IN ('passed','failed','judge_unknown'))
              OR (NEW.status='failed' AND result.status='infra_failed')
              OR (NEW.status='cancelled' AND result.status='cancelled')
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval case job result mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_job_delete",
        """
        CREATE TRIGGER trg_agent_eval_case_job_delete
        BEFORE DELETE ON agent_eval_case_job
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_case_job is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_scratch_scope",
        """
        CREATE TRIGGER trg_agent_eval_scratch_scope
        BEFORE INSERT ON agent_eval_scratch_turn
        WHEN NOT EXISTS (
          SELECT 1
          FROM agent_eval_case_job job
          JOIN agent_eval_run run ON run.id=job.run_id
          JOIN sessions session_row ON session_row.id=NEW.session_id
          JOIN turns turn_row ON turn_row.id=NEW.turn_id
          JOIN workspaces workspace_row
            ON workspace_row.id=session_row.workspace_id
           AND workspace_row.team_id=run.team_id
          WHERE job.id=NEW.job_id
            AND job.run_id=NEW.run_id
            AND job.case_revision_id=NEW.case_revision_id
            AND job.attempt=NEW.attempt
            AND job.lease_generation=NEW.lease_generation
            AND job.status='leased'
            AND run.team_id=NEW.team_id
            AND run.revision_ref=NEW.revision_ref
            AND run.status='running'
            AND session_row.origin='agent_eval'
            AND session_row.user_id=run.created_by
            AND turn_row.session_id=session_row.id
            AND turn_row.status='running'
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval scratch Turn scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_scratch_update",
        """
        CREATE TRIGGER trg_agent_eval_scratch_update
        BEFORE UPDATE ON agent_eval_scratch_turn
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_scratch_turn is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_scratch_delete",
        """
        CREATE TRIGGER trg_agent_eval_scratch_delete
        BEFORE DELETE ON agent_eval_scratch_turn
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_scratch_turn is immutable');
        END
        """,
    ),
)


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _object_sql(connection: Any, *, object_type: str, name: str) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _ensure_object(
    connection: Any,
    *,
    object_type: str,
    name: str,
    ddl: str,
) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(f"0038 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(
            connection,
            object_type=object_type,
            name=name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
