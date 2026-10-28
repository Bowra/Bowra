"""Durable exact Capability implementation execution identity.

M3-8 records the exact reviewed implementation selected for one composition
node and attempt.  The row is subordinate to an already-admitted Agent
execution; it carries identities and fingerprints only, never prompts,
credentials, Tool arguments, or business data.

Revision ID: 0036_agent_capability_execution
Revises: 0035_scheduled_agent_binding
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0036_agent_capability_execution"
down_revision = "0035_scheduled_agent_binding"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "turn_capability_execution",
        """
        CREATE TABLE turn_capability_execution (
          id                         TEXT PRIMARY KEY CHECK(id LIKE 'capx_%'),
          team_id                    TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          turn_id                    TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
          agent_execution_id         TEXT NOT NULL
                                     REFERENCES turn_agent_execution(id) ON DELETE CASCADE,
          composition_node_id        TEXT NOT NULL CHECK(length(composition_node_id) > 0),
          attempt_generation         INTEGER NOT NULL CHECK(attempt_generation >= 1),
          capability_id              TEXT NOT NULL CHECK(capability_id LIKE 'cap_%'),
          capability_version         INTEGER NOT NULL CHECK(capability_version >= 1),
          implementation_id          TEXT NOT NULL CHECK(implementation_id LIKE 'capi_%'),
          implementation_version     INTEGER NOT NULL CHECK(implementation_version >= 1),
          adapter_id                  TEXT CHECK(adapter_id IS NULL OR length(adapter_id) > 0),
          prompt_module_fingerprint  TEXT NOT NULL CHECK(prompt_module_fingerprint LIKE 'sha256:%'),
          dependency_fingerprint     TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          input_artifact_refs_json   TEXT NOT NULL
                                     CHECK(json_valid(input_artifact_refs_json))
                                     CHECK(json_type(input_artifact_refs_json)='array'),
          output_contract            TEXT NOT NULL CHECK(length(output_contract) > 0),
          status                     TEXT NOT NULL CHECK(status IN (
                                       'selected','running','completed','failed','blocked','cancelled'
                                     )),
          created_at                 TEXT NOT NULL,
          started_at                 TEXT,
          ended_at                   TEXT,
          failure_code               TEXT,
          UNIQUE(agent_execution_id, composition_node_id, attempt_generation),
          CHECK(status != 'selected' OR (
            started_at IS NULL AND ended_at IS NULL AND failure_code IS NULL
          )),
          CHECK(status != 'running' OR (
            started_at IS NOT NULL AND ended_at IS NULL AND failure_code IS NULL
          )),
          CHECK(status IN ('selected','running') OR ended_at IS NOT NULL),
          CHECK(failure_code IS NULL OR status IN ('failed','blocked'))
        )
        """,
    ),
    (
        "index",
        "idx_turn_capability_execution_turn",
        """
        CREATE INDEX idx_turn_capability_execution_turn
        ON turn_capability_execution(turn_id, created_at, id)
        """,
    ),
    (
        "index",
        "idx_turn_capability_execution_implementation",
        """
        CREATE INDEX idx_turn_capability_execution_implementation
        ON turn_capability_execution(
          team_id, capability_id, capability_version,
          implementation_id, implementation_version, created_at
        )
        """,
    ),
    (
        "trigger",
        "trg_turn_capability_execution_scope",
        """
        CREATE TRIGGER trg_turn_capability_execution_scope
        BEFORE INSERT ON turn_capability_execution
        WHEN NOT EXISTS (
          SELECT 1
          FROM turn_agent_execution execution
          WHERE execution.id=NEW.agent_execution_id
            AND execution.team_id=NEW.team_id
            AND execution.turn_id=NEW.turn_id
            AND execution.status IN ('admitted','running')
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Capability execution scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_capability_execution_identity_immutable",
        """
        CREATE TRIGGER trg_turn_capability_execution_identity_immutable
        BEFORE UPDATE ON turn_capability_execution
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.turn_id IS NOT OLD.turn_id
          OR NEW.agent_execution_id IS NOT OLD.agent_execution_id
          OR NEW.composition_node_id IS NOT OLD.composition_node_id
          OR NEW.attempt_generation IS NOT OLD.attempt_generation
          OR NEW.capability_id IS NOT OLD.capability_id
          OR NEW.capability_version IS NOT OLD.capability_version
          OR NEW.implementation_id IS NOT OLD.implementation_id
          OR NEW.implementation_version IS NOT OLD.implementation_version
          OR NEW.adapter_id IS NOT OLD.adapter_id
          OR NEW.prompt_module_fingerprint IS NOT OLD.prompt_module_fingerprint
          OR NEW.dependency_fingerprint IS NOT OLD.dependency_fingerprint
          OR NEW.input_artifact_refs_json IS NOT OLD.input_artifact_refs_json
          OR NEW.output_contract IS NOT OLD.output_contract
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
          SELECT RAISE(ABORT, 'turn_capability_execution identity is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_capability_execution_parent_state",
        """
        CREATE TRIGGER trg_turn_capability_execution_parent_state
        BEFORE UPDATE ON turn_capability_execution
        WHEN NEW.status='running'
          AND NOT EXISTS (
            SELECT 1
            FROM turn_agent_execution execution
            WHERE execution.id=NEW.agent_execution_id
              AND execution.status='running'
          )
        BEGIN
          SELECT RAISE(ABORT, 'Capability execution parent is not running');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_capability_execution_status_transition",
        """
        CREATE TRIGGER trg_turn_capability_execution_status_transition
        BEFORE UPDATE ON turn_capability_execution
        WHEN NOT (
          (
            NEW.status=OLD.status
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS OLD.ended_at
            AND NEW.failure_code IS OLD.failure_code
          )
          OR (
            OLD.status='selected' AND NEW.status='running'
            AND NEW.started_at IS NOT NULL
            AND NEW.ended_at IS NULL
            AND NEW.failure_code IS NULL
          )
          OR (
            OLD.status='selected' AND NEW.status IN ('failed','blocked','cancelled')
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NOT NULL
          )
          OR (
            OLD.status='running' AND NEW.status IN ('completed','failed','blocked','cancelled')
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NOT NULL
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid turn Capability execution transition');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_execution_capability_fence",
        """
        CREATE TRIGGER trg_turn_agent_execution_capability_fence
        BEFORE UPDATE ON turn_agent_execution
        WHEN NEW.status IN ('completed','failed','blocked','cancelled')
          AND EXISTS (
            SELECT 1
            FROM turn_capability_execution capability_execution
            WHERE capability_execution.agent_execution_id=NEW.id
              AND capability_execution.status IN ('selected','running')
          )
        BEGIN
          SELECT RAISE(ABORT, 'Agent execution has active Capability executions');
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
        raise RuntimeError(f"0036 found incompatible {object_type} semantics: {name}")


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
