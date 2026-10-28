"""Append-only team retention policy and admission version fences.

Revision ID: 0068_reference_retention_policy
Revises: 0067_reference_retention
"""

from __future__ import annotations

import re

from alembic import context

revision = "0068_reference_retention_policy"
down_revision = "0067_reference_retention"
branch_labels = None
depends_on = None

TABLES = ("chat_reference_plans", "chat_reference_queued_inputs")
DDL = (
    """
    CREATE TABLE team_reference_retention_policies (
      team_id TEXT NOT NULL REFERENCES teams(id),
      version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=1),
      days INTEGER NOT NULL CHECK(typeof(days)='integer' AND days BETWEEN 1 AND 180),
      updated_by TEXT NOT NULL REFERENCES users(id),
      updated_at TEXT NOT NULL,
      PRIMARY KEY(team_id,version)
    )
    """,
    """
    CREATE TRIGGER trg_reference_retention_policy_insert BEFORE INSERT ON team_reference_retention_policies
    WHEN NEW.version<>COALESCE((SELECT MAX(version) FROM team_reference_retention_policies WHERE team_id=NEW.team_id),0)+1
      OR NOT EXISTS (SELECT 1 FROM teams t JOIN users u ON u.id=NEW.updated_by
        WHERE t.id=NEW.team_id AND t.status='active' AND u.status='active'
          AND (t.created_by_user_id=u.id OR EXISTS (SELECT 1 FROM team_members m
            WHERE m.team_id=t.id AND m.user_id=u.id AND m.status='active' AND m.role='team_admin')))
    BEGIN SELECT RAISE(ABORT,'reference retention policy writer conflict'); END
    """,
    *(
        f"""
    CREATE TRIGGER trg_reference_retention_policy_no_{op.lower()} BEFORE {op} ON team_reference_retention_policies
    BEGIN SELECT RAISE(ABORT,'reference retention policy is append only'); END
    """
        for op in ("UPDATE", "DELETE")
    ),
    """
    CREATE VIEW chat_reference_plan_retention AS
    SELECT p.turn_id,
      p.retention_state='available' AND s.deleted_at IS NULL
        AND NOT EXISTS(SELECT 1 FROM chat_reference_content_snapshots c WHERE c.turn_id=p.turn_id
          AND (c.retention_state='purged' OR julianday(c.expires_at) IS NULL))
        AND NOT EXISTS(SELECT 1 FROM chat_reference_evidence e WHERE e.turn_id=p.turn_id
          AND (e.retention_state='purged' OR julianday(e.expires_at) IS NULL)) AS available,
      min(julianday(p.expires_at), julianday(p.created_at) + COALESCE(
        (SELECT MIN(r.days) FROM team_reference_retention_policies r
          WHERE r.team_id=w.team_id AND r.version>=p.retention_policy_version),180),
        COALESCE((SELECT MIN(julianday(c.expires_at)) FROM chat_reference_content_snapshots c WHERE c.turn_id=p.turn_id),julianday(p.expires_at)),
        COALESCE((SELECT MIN(julianday(e.expires_at)) FROM chat_reference_evidence e WHERE e.turn_id=p.turn_id),julianday(p.expires_at))) AS deadline
    FROM chat_reference_plans p JOIN turns t ON t.id=p.turn_id
      JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
    """,
    """
    CREATE VIEW chat_reference_queue_retention AS
    SELECT q.team_id,q.user_id,q.client_submission_id,
      q.retention_state='available' AND s.deleted_at IS NULL AND COALESCE(p.available,1) AS available,
      min(julianday(q.expires_at), julianday(q.created_at) + COALESCE(
        (SELECT MIN(r.days) FROM team_reference_retention_policies r
          WHERE r.team_id=q.team_id AND r.version>=q.retention_policy_version),180),
        CASE WHEN p.turn_id IS NULL THEN julianday(q.expires_at) ELSE p.deadline END) AS deadline
    FROM chat_reference_queued_inputs q JOIN sessions s ON s.id=q.session_id
      LEFT JOIN chat_reference_plan_retention p ON p.turn_id=q.accepted_parent_turn_id
    """,
    """
    CREATE TRIGGER trg_reference_policy_completion BEFORE UPDATE OF status ON turns
    WHEN NEW.status='completed' AND OLD.status IS NOT NEW.status AND EXISTS (
      SELECT 1 FROM chat_reference_plan_retention p WHERE p.turn_id=NEW.id
        AND (NOT p.available OR COALESCE(p.deadline<=julianday('now'),1)))
    BEGIN SELECT RAISE(ABORT,'reference material unavailable for completion'); END
    """,
    *(
        f"""
    CREATE TRIGGER trg_{table}_policy_lifecycle BEFORE INSERT ON {table}
    WHEN NOT EXISTS (SELECT 1 FROM chat_reference_plan_retention p WHERE p.turn_id=NEW.turn_id
      AND p.available AND p.deadline>julianday('now'))
    BEGIN SELECT RAISE(ABORT,'reference source unavailable'); END
    """
        for table in ("chat_reference_content_snapshots", "chat_reference_resolutions", "chat_reference_evidence")
    ),
    """
    CREATE TRIGGER trg_reference_queue_policy_lifecycle BEFORE INSERT ON chat_reference_queued_inputs
    WHEN EXISTS (SELECT 1 FROM chat_reference_plan_retention p WHERE p.turn_id=NEW.accepted_parent_turn_id
      AND (NOT p.available OR COALESCE(p.deadline<=julianday('now'),1)))
    BEGIN SELECT RAISE(ABORT,'reference source unavailable'); END
    """,
)


def _version_triggers(table: str) -> tuple[str, str]:
    team = (
        "(SELECT w.team_id FROM turns t JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id WHERE t.id=NEW.turn_id)"
        if table.endswith("plans")
        else "NEW.team_id"
    )
    return (
        f"""
        CREATE TRIGGER trg_{table}_policy_insert BEFORE INSERT ON {table}
        WHEN NEW.retention_policy_version<>COALESCE((SELECT MAX(version) FROM team_reference_retention_policies WHERE team_id={team}),0)
        BEGIN SELECT RAISE(ABORT,'reference retention policy version conflict'); END
        """,
        f"""
        CREATE TRIGGER trg_{table}_policy_immutable BEFORE UPDATE OF retention_policy_version ON {table}
        WHEN NEW.retention_policy_version IS NOT OLD.retention_policy_version
        BEGIN SELECT RAISE(ABORT,'reference retention policy version is immutable'); END
        """,
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    statements = (*DDL, *(sql for table in TABLES for sql in _version_triggers(table)))
    names = [re.search(r"CREATE (?:TABLE|VIEW|TRIGGER) (\w+)", sql).group(1) for sql in statements]
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")", tuple(names)).fetchone():
        raise RuntimeError("reference retention policy schema drift")
    for table in TABLES:
        if any(row[1] == "retention_policy_version" for row in db.exec_driver_sql(f"PRAGMA table_info({table})")):
            raise RuntimeError("reference retention policy schema drift")
    for table in TABLES:
        db.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN retention_policy_version INTEGER NOT NULL DEFAULT 0 "
            "CHECK(typeof(retention_policy_version)='integer' AND retention_policy_version>=0)"
        )
    for sql in statements:
        db.exec_driver_sql(sql)


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
