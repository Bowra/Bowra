"""Queryable reverse dependency projection for Agent resources.

The edge table is an expand-only read model derived from authored Agent
definitions.  A separate subject marker records zero-dependency subjects and
lets readers distinguish "unused" from an incomplete backfill.

Revision ID: 0039_resource_dependency_projection
Revises: 0038_agent_eval_execution
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0039_resource_dependency_projection"
down_revision = "0038_agent_eval_execution"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "resource_dependency_subject_projection",
        """
        CREATE TABLE resource_dependency_subject_projection (
          team_id             TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subject_kind        TEXT NOT NULL CHECK(subject_kind IN (
                                'agent_working_copy',
                                'agent_revision',
                                'system_agent_revision'
                              )),
          subject_ref         TEXT NOT NULL,
          agent_id            TEXT NOT NULL,
          subject_fingerprint TEXT NOT NULL CHECK(subject_fingerprint LIKE 'sha256:%'),
          status              TEXT NOT NULL CHECK(status IN ('complete','partial')),
          reason_code         TEXT,
          compiler_version    TEXT NOT NULL,
          projected_at        TEXT NOT NULL,
          PRIMARY KEY(team_id, subject_kind, subject_ref),
          CHECK(
            (status='complete' AND reason_code IS NULL)
            OR (status='partial' AND reason_code IS NOT NULL AND length(reason_code) > 0)
          )
        ) WITHOUT ROWID
        """,
    ),
    (
        "table",
        "resource_dependency_edge",
        """
        CREATE TABLE resource_dependency_edge (
          edge_key             TEXT PRIMARY KEY CHECK(edge_key LIKE 'sha256:%'),
          team_id              TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subject_kind         TEXT NOT NULL CHECK(subject_kind IN (
                                 'agent_working_copy',
                                 'agent_revision',
                                 'system_agent_revision'
                               )),
          subject_ref          TEXT NOT NULL,
          agent_id             TEXT NOT NULL,
          resource_kind        TEXT NOT NULL CHECK(resource_kind IN (
                                 'skill', 'tool_operation', 'connection'
                               )),
          resource_ref         TEXT NOT NULL,
          resource_parent_ref  TEXT,
          resource_revision    TEXT,
          dependency_role      TEXT NOT NULL CHECK(dependency_role IN ('required','optional')),
          evidence_level       TEXT NOT NULL CHECK(evidence_level IN ('exact','stable_only')),
          subject_fingerprint  TEXT NOT NULL CHECK(subject_fingerprint LIKE 'sha256:%'),
          created_at           TEXT NOT NULL
        )
        """,
    ),
    (
        "index",
        "idx_resource_dependency_reverse",
        """
        CREATE INDEX idx_resource_dependency_reverse
        ON resource_dependency_edge(
          team_id, resource_kind, resource_ref, subject_kind, subject_ref
        )
        """,
    ),
    (
        "index",
        "idx_resource_dependency_parent",
        """
        CREATE INDEX idx_resource_dependency_parent
        ON resource_dependency_edge(
          team_id, resource_kind, resource_parent_ref, subject_kind, subject_ref
        )
        """,
    ),
    (
        "index",
        "idx_resource_dependency_subject",
        """
        CREATE INDEX idx_resource_dependency_subject
        ON resource_dependency_edge(team_id, subject_kind, subject_ref)
        """,
    ),
    (
        "trigger",
        "trg_resource_dependency_projection_scope",
        """
        CREATE TRIGGER trg_resource_dependency_projection_scope
        BEFORE INSERT ON resource_dependency_subject_projection
        WHEN NOT (
          (
            NEW.subject_kind='agent_working_copy'
            AND EXISTS (
              SELECT 1 FROM team_subagent_working_copy subject
              WHERE subject.id=NEW.subject_ref
                AND subject.team_id=NEW.team_id
                AND subject.subagent_id=NEW.agent_id
            )
          ) OR (
            NEW.subject_kind='agent_revision'
            AND EXISTS (
              SELECT 1 FROM team_subagent_revision subject
              WHERE subject.id=NEW.subject_ref
                AND subject.team_id=NEW.team_id
                AND subject.subagent_id=NEW.agent_id
            )
          ) OR (
            NEW.subject_kind='system_agent_revision'
            AND EXISTS (
              SELECT 1 FROM system_agent_effective_revision subject
              WHERE subject.id=NEW.subject_ref
                AND subject.team_id=NEW.team_id
                AND subject.agent_id=NEW.agent_id
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'resource dependency subject scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_resource_dependency_projection_identity",
        """
        CREATE TRIGGER trg_resource_dependency_projection_identity
        BEFORE UPDATE ON resource_dependency_subject_projection
        WHEN NEW.team_id IS NOT OLD.team_id
          OR NEW.subject_kind IS NOT OLD.subject_kind
          OR NEW.subject_ref IS NOT OLD.subject_ref
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.subject_fingerprint IS NOT OLD.subject_fingerprint
          OR NEW.compiler_version IS NOT OLD.compiler_version
          OR NEW.projected_at IS NOT OLD.projected_at
        BEGIN
          SELECT RAISE(ABORT, 'resource dependency projection identity is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_resource_dependency_projection_delete",
        """
        CREATE TRIGGER trg_resource_dependency_projection_delete
        BEFORE DELETE ON resource_dependency_subject_projection
        WHEN OLD.subject_kind!='agent_working_copy'
        BEGIN
          SELECT RAISE(ABORT, 'immutable resource dependency projection cannot be deleted');
        END
        """,
    ),
    (
        "trigger",
        "trg_resource_dependency_edge_scope",
        """
        CREATE TRIGGER trg_resource_dependency_edge_scope
        BEFORE INSERT ON resource_dependency_edge
        WHEN NOT EXISTS (
          SELECT 1
          FROM resource_dependency_subject_projection projection
          WHERE projection.team_id=NEW.team_id
            AND projection.subject_kind=NEW.subject_kind
            AND projection.subject_ref=NEW.subject_ref
            AND projection.agent_id=NEW.agent_id
            AND projection.subject_fingerprint=NEW.subject_fingerprint
        )
        BEGIN
          SELECT RAISE(ABORT, 'resource dependency edge projection mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_resource_dependency_edge_update",
        """
        CREATE TRIGGER trg_resource_dependency_edge_update
        BEFORE UPDATE ON resource_dependency_edge
        BEGIN
          SELECT RAISE(ABORT, 'resource dependency edge is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_resource_dependency_edge_delete",
        """
        CREATE TRIGGER trg_resource_dependency_edge_delete
        BEFORE DELETE ON resource_dependency_edge
        WHEN OLD.subject_kind!='agent_working_copy'
        BEGIN
          SELECT RAISE(ABORT, 'immutable resource dependency edge cannot be deleted');
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
        raise RuntimeError(
            f"0039 found incompatible {object_type} semantics: {name}",
        )


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
