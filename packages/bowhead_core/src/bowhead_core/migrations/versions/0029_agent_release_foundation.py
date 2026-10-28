"""Agent M2 immutable revision and release persistence foundation.

This revision is expand-only.  Existing Agent rows remain ``legacy_mutable`` and
all live pointers stay NULL; the M2 baseline import and writer cutover are
separate, recoverable operations.

Revision ID: 0029_agent_release_foundation
Revises: 0028_agent_listing_safety
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0029_agent_release_foundation"
down_revision = "0028_agent_listing_safety"
branch_labels = None
depends_on = None


_AGGREGATE_COLUMNS: tuple[tuple[str, str, str, int, str | None], ...] = (
    ("live_revision_id", "TEXT", "TEXT", 0, None),
    ("live_release_id", "TEXT", "TEXT", 0, None),
    (
        "release_sequence",
        "INTEGER NOT NULL DEFAULT 0 CHECK(release_sequence >= 0)",
        "INTEGER",
        1,
        "0",
    ),
    ("archived_at", "TEXT", "TEXT", 0, None),
    ("archived_by", "TEXT", "TEXT", 0, None),
    ("last_release_event_id", "TEXT", "TEXT", 0, None),
    (
        "definition_mode",
        "TEXT NOT NULL DEFAULT 'legacy_mutable' "
        "CHECK(definition_mode IN ('legacy_mutable','revisioned'))",
        "TEXT",
        1,
        "'legacy_mutable'",
    ),
)

_TABLE_DDL: tuple[tuple[str, str, tuple[tuple[str, str, int, str | None, int], ...]], ...] = (
    (
        "team_subagent_revision",
        """
        CREATE TABLE team_subagent_revision (
          id                       TEXT PRIMARY KEY CHECK(id LIKE 'arv_%'),
          team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subagent_id              TEXT NOT NULL,
          sequence                 INTEGER NOT NULL CHECK(sequence >= 1),
          revision_kind            TEXT NOT NULL
            CHECK(revision_kind IN ('authored','imported_baseline','rollback')),
          definition_json          TEXT NOT NULL CHECK(length(definition_json) > 0),
          listing_json             TEXT NOT NULL CHECK(length(listing_json) > 0),
          definition_hash          TEXT NOT NULL CHECK(definition_hash LIKE 'sha256:%'),
          dependency_fingerprint   TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          capability_fingerprint   TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          risk_compiler_version    TEXT NOT NULL,
          risk_json                TEXT NOT NULL CHECK(length(risk_json) > 0),
          created_by               TEXT NOT NULL,
          created_at               TEXT NOT NULL,
          UNIQUE(subagent_id, sequence),
          UNIQUE(team_id, subagent_id, id),
          FOREIGN KEY(team_id, subagent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT
        )
        """,
        (
            ("id", "TEXT", 0, None, 1),
            ("team_id", "TEXT", 1, None, 0),
            ("subagent_id", "TEXT", 1, None, 0),
            ("sequence", "INTEGER", 1, None, 0),
            ("revision_kind", "TEXT", 1, None, 0),
            ("definition_json", "TEXT", 1, None, 0),
            ("listing_json", "TEXT", 1, None, 0),
            ("definition_hash", "TEXT", 1, None, 0),
            ("dependency_fingerprint", "TEXT", 1, None, 0),
            ("capability_fingerprint", "TEXT", 1, None, 0),
            ("risk_compiler_version", "TEXT", 1, None, 0),
            ("risk_json", "TEXT", 1, None, 0),
            ("created_by", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        ),
    ),
    (
        "agent_safety_report",
        """
        CREATE TABLE agent_safety_report (
          id                       TEXT PRIMARY KEY CHECK(id LIKE 'asr_%'),
          team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_id                 TEXT NOT NULL,
          subject_kind             TEXT NOT NULL
            CHECK(subject_kind IN ('working_copy','revision','legacy_row')),
          subject_ref              TEXT NOT NULL,
          content_hash             TEXT NOT NULL CHECK(content_hash LIKE 'sha256:%'),
          dependency_fingerprint   TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          compiler_version         TEXT NOT NULL,
          semantic_scanner_version TEXT NOT NULL,
          status                   TEXT NOT NULL
            CHECK(status IN ('pass','warning','review_required','blocked','unknown')),
          deterministic_json       TEXT NOT NULL CHECK(length(deterministic_json) > 0),
          semantic_json            TEXT NOT NULL CHECK(length(semantic_json) > 0),
          restricted_evidence_ref  TEXT,
          safe_summary_json        TEXT NOT NULL CHECK(length(safe_summary_json) > 0),
          created_by               TEXT NOT NULL,
          created_at               TEXT NOT NULL,
          UNIQUE(
            team_id, agent_id, subject_kind, subject_ref, content_hash,
            dependency_fingerprint, compiler_version, semantic_scanner_version
          ),
          UNIQUE(team_id, agent_id, id),
          FOREIGN KEY(team_id, agent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT
        )
        """,
        (
            ("id", "TEXT", 0, None, 1),
            ("team_id", "TEXT", 1, None, 0),
            ("agent_id", "TEXT", 1, None, 0),
            ("subject_kind", "TEXT", 1, None, 0),
            ("subject_ref", "TEXT", 1, None, 0),
            ("content_hash", "TEXT", 1, None, 0),
            ("dependency_fingerprint", "TEXT", 1, None, 0),
            ("compiler_version", "TEXT", 1, None, 0),
            ("semantic_scanner_version", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("deterministic_json", "TEXT", 1, None, 0),
            ("semantic_json", "TEXT", 1, None, 0),
            ("restricted_evidence_ref", "TEXT", 0, None, 0),
            ("safe_summary_json", "TEXT", 1, None, 0),
            ("created_by", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        ),
    ),
    (
        "team_subagent_release",
        """
        CREATE TABLE team_subagent_release (
          id                       TEXT PRIMARY KEY CHECK(id LIKE 'arl_%'),
          team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subagent_id              TEXT NOT NULL,
          release_sequence         INTEGER NOT NULL CHECK(release_sequence >= 1),
          revision_id              TEXT NOT NULL,
          predecessor_release_id   TEXT,
          source_candidate_id      TEXT,
          audience_snapshot_json   TEXT NOT NULL CHECK(length(audience_snapshot_json) > 0),
          audience_hash            TEXT NOT NULL CHECK(audience_hash LIKE 'sha256:%'),
          capability_fingerprint   TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          dependency_fingerprint   TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          risk_json                TEXT NOT NULL CHECK(length(risk_json) > 0),
          safety_report_id         TEXT NOT NULL,
          eval_evidence_json       TEXT,
          release_note             TEXT,
          activated_by             TEXT NOT NULL,
          activated_at             TEXT NOT NULL,
          UNIQUE(subagent_id, release_sequence),
          UNIQUE(team_id, subagent_id, id),
          FOREIGN KEY(team_id, subagent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, revision_id)
            REFERENCES team_subagent_revision(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, predecessor_release_id)
            REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, source_candidate_id)
            REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, safety_report_id)
            REFERENCES agent_safety_report(team_id, agent_id, id) ON DELETE RESTRICT
        )
        """,
        (
            ("id", "TEXT", 0, None, 1),
            ("team_id", "TEXT", 1, None, 0),
            ("subagent_id", "TEXT", 1, None, 0),
            ("release_sequence", "INTEGER", 1, None, 0),
            ("revision_id", "TEXT", 1, None, 0),
            ("predecessor_release_id", "TEXT", 0, None, 0),
            ("source_candidate_id", "TEXT", 0, None, 0),
            ("audience_snapshot_json", "TEXT", 1, None, 0),
            ("audience_hash", "TEXT", 1, None, 0),
            ("capability_fingerprint", "TEXT", 1, None, 0),
            ("dependency_fingerprint", "TEXT", 1, None, 0),
            ("risk_json", "TEXT", 1, None, 0),
            ("safety_report_id", "TEXT", 1, None, 0),
            ("eval_evidence_json", "TEXT", 0, None, 0),
            ("release_note", "TEXT", 0, None, 0),
            ("activated_by", "TEXT", 1, None, 0),
            ("activated_at", "TEXT", 1, None, 0),
        ),
    ),
    (
        "team_subagent_working_copy",
        """
        CREATE TABLE team_subagent_working_copy (
          id                TEXT PRIMARY KEY CHECK(id LIKE 'awc_%'),
          team_id           TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subagent_id       TEXT NOT NULL UNIQUE,
          base_release_id   TEXT,
          definition_json   TEXT NOT NULL CHECK(length(definition_json) > 0),
          listing_json      TEXT NOT NULL CHECK(length(listing_json) > 0),
          audience_json     TEXT NOT NULL CHECK(length(audience_json) > 0),
          validation_json   TEXT NOT NULL CHECK(length(validation_json) > 0),
          row_version       INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          updated_by        TEXT NOT NULL,
          created_at        TEXT NOT NULL,
          updated_at        TEXT NOT NULL,
          FOREIGN KEY(team_id, subagent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, base_release_id)
            REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT
        )
        """,
        (
            ("id", "TEXT", 0, None, 1),
            ("team_id", "TEXT", 1, None, 0),
            ("subagent_id", "TEXT", 1, None, 0),
            ("base_release_id", "TEXT", 0, None, 0),
            ("definition_json", "TEXT", 1, None, 0),
            ("listing_json", "TEXT", 1, None, 0),
            ("audience_json", "TEXT", 1, None, 0),
            ("validation_json", "TEXT", 1, None, 0),
            ("row_version", "INTEGER", 1, "1", 0),
            ("updated_by", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
        ),
    ),
    (
        "team_subagent_release_candidate",
        """
        CREATE TABLE team_subagent_release_candidate (
          id                       TEXT PRIMARY KEY CHECK(id LIKE 'arc_%'),
          team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subagent_id              TEXT NOT NULL,
          revision_id              TEXT NOT NULL,
          base_live_release_id     TEXT,
          target_audience_json     TEXT NOT NULL CHECK(length(target_audience_json) > 0),
          target_audience_hash     TEXT NOT NULL CHECK(target_audience_hash LIKE 'sha256:%'),
          capability_fingerprint   TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          dependency_fingerprint   TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          risk_compiler_version    TEXT NOT NULL,
          risk_json                TEXT NOT NULL CHECK(length(risk_json) > 0),
          safety_report_id         TEXT NOT NULL,
          eval_evidence_json       TEXT,
          release_note             TEXT,
          candidate_hash           TEXT NOT NULL CHECK(candidate_hash LIKE 'sha256:%'),
          state                    TEXT NOT NULL
            CHECK(state IN ('pending','approved','rejected','withdrawn','activated','stale')),
          row_version              INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          submitted_by             TEXT NOT NULL,
          submitted_at             TEXT NOT NULL,
          decided_by               TEXT,
          decision_reason_code     TEXT,
          self_review              INTEGER CHECK(self_review IN (0,1)),
          decided_at               TEXT,
          activated_release_id     TEXT,
          updated_at               TEXT NOT NULL,
          UNIQUE(team_id, subagent_id, id),
          CHECK(state != 'activated' OR activated_release_id IS NOT NULL),
          CHECK(
            (decided_at IS NULL AND decided_by IS NULL AND self_review IS NULL)
            OR (decided_at IS NOT NULL AND decided_by IS NOT NULL AND self_review IS NOT NULL)
          ),
          FOREIGN KEY(team_id, subagent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, revision_id)
            REFERENCES team_subagent_revision(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, base_live_release_id)
            REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, safety_report_id)
            REFERENCES agent_safety_report(team_id, agent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, activated_release_id)
            REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT
        )
        """,
        (
            ("id", "TEXT", 0, None, 1),
            ("team_id", "TEXT", 1, None, 0),
            ("subagent_id", "TEXT", 1, None, 0),
            ("revision_id", "TEXT", 1, None, 0),
            ("base_live_release_id", "TEXT", 0, None, 0),
            ("target_audience_json", "TEXT", 1, None, 0),
            ("target_audience_hash", "TEXT", 1, None, 0),
            ("capability_fingerprint", "TEXT", 1, None, 0),
            ("dependency_fingerprint", "TEXT", 1, None, 0),
            ("risk_compiler_version", "TEXT", 1, None, 0),
            ("risk_json", "TEXT", 1, None, 0),
            ("safety_report_id", "TEXT", 1, None, 0),
            ("eval_evidence_json", "TEXT", 0, None, 0),
            ("release_note", "TEXT", 0, None, 0),
            ("candidate_hash", "TEXT", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("row_version", "INTEGER", 1, "1", 0),
            ("submitted_by", "TEXT", 1, None, 0),
            ("submitted_at", "TEXT", 1, None, 0),
            ("decided_by", "TEXT", 0, None, 0),
            ("decision_reason_code", "TEXT", 0, None, 0),
            ("self_review", "INTEGER", 0, None, 0),
            ("decided_at", "TEXT", 0, None, 0),
            ("activated_release_id", "TEXT", 0, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
        ),
    ),
    (
        "agent_command_receipt",
        """
        CREATE TABLE agent_command_receipt (
          team_id          TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          actor_user_id    TEXT NOT NULL,
          command_scope    TEXT NOT NULL,
          idempotency_hash TEXT NOT NULL CHECK(idempotency_hash LIKE 'sha256:%'),
          request_hash     TEXT NOT NULL CHECK(request_hash LIKE 'sha256:%'),
          result_json      TEXT NOT NULL CHECK(length(result_json) > 0),
          created_at       TEXT NOT NULL,
          expires_at       TEXT NOT NULL,
          PRIMARY KEY(team_id, actor_user_id, command_scope, idempotency_hash)
        ) WITHOUT ROWID
        """,
        (
            ("team_id", "TEXT", 1, None, 1),
            ("actor_user_id", "TEXT", 1, None, 2),
            ("command_scope", "TEXT", 1, None, 3),
            ("idempotency_hash", "TEXT", 1, None, 4),
            ("request_hash", "TEXT", 1, None, 0),
            ("result_json", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("expires_at", "TEXT", 1, None, 0),
        ),
    ),
)

_INDEX_DDL: tuple[tuple[str, str], ...] = (
    (
        "idx_agent_revision_history",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_revision_history
    ON team_subagent_revision(team_id, subagent_id, sequence DESC)
    """,
    ),
    (
        "idx_agent_safety_report_subject",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_safety_report_subject
    ON agent_safety_report(team_id, agent_id, subject_kind, created_at DESC)
    """,
    ),
    (
        "idx_agent_release_history",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_release_history
    ON team_subagent_release(team_id, subagent_id, release_sequence DESC)
    """,
    ),
    (
        "idx_agent_working_copy_updated",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_working_copy_updated
    ON team_subagent_working_copy(team_id, updated_at DESC)
    """,
    ),
    (
        "uq_agent_one_pending_candidate",
        """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_one_pending_candidate
    ON team_subagent_release_candidate(subagent_id)
    WHERE state='pending'
    """,
    ),
    (
        "idx_agent_candidate_inbox",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_candidate_inbox
    ON team_subagent_release_candidate(team_id, state, submitted_at DESC)
    """,
    ),
    (
        "idx_agent_candidate_history",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_candidate_history
    ON team_subagent_release_candidate(team_id, subagent_id, submitted_at DESC)
    """,
    ),
    (
        "idx_agent_command_receipt_expiry",
        """
    CREATE INDEX IF NOT EXISTS idx_agent_command_receipt_expiry
    ON agent_command_receipt(expires_at)
    """,
    ),
)

_IMMUTABLE_TABLES = (
    "team_subagent_revision",
    "team_subagent_release",
    "agent_safety_report",
)


def _table_info(connection: Any, table_name: str) -> list[Any]:
    return list(connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall())


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _require_schema_sql(
    connection: Any,
    *,
    object_type: str,
    object_name: str,
    expected_sql: str,
) -> None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, object_name),
    ).fetchone()
    if row is None or row[0] is None or _canonical_sql(str(row[0])) != _canonical_sql(expected_sql):
        raise RuntimeError(f"0029 found incompatible {object_type} semantics: {object_name}")


def _ensure_aggregate_column(
    connection: Any,
    *,
    name: str,
    definition: str,
    expected_type: str,
    expected_not_null: int,
    expected_default: str | None,
) -> None:
    columns = {str(row[1]): row for row in _table_info(connection, "team_subagent")}
    if name not in columns:
        connection.exec_driver_sql(f"ALTER TABLE team_subagent ADD COLUMN {name} {definition}")
        columns = {str(row[1]): row for row in _table_info(connection, "team_subagent")}
    column = columns.get(name)
    if column is None or (
        str(column[2]).upper(),
        int(column[3]),
        column[4],
        int(column[5]),
    ) != (expected_type, expected_not_null, expected_default, 0):
        raise RuntimeError(f"0029 found incompatible column semantics: team_subagent.{name}")
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='team_subagent'",
    ).fetchone()
    expected_fragment = _canonical_sql(f"{name} {definition}")
    if row is None or row[0] is None or expected_fragment not in _canonical_sql(str(row[0])):
        raise RuntimeError(f"0029 found incompatible column constraint: team_subagent.{name}")


def _ensure_table(
    connection: Any,
    *,
    table_name: str,
    ddl: str,
    expected_columns: tuple[tuple[str, str, int, str | None, int], ...],
) -> None:
    if not _table_info(connection, table_name):
        connection.exec_driver_sql(ddl)
    actual_columns = tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in _table_info(connection, table_name)
    )
    if actual_columns != expected_columns:
        raise RuntimeError(f"0029 found incompatible table semantics: {table_name}")
    _require_schema_sql(
        connection,
        object_type="table",
        object_name=table_name,
        expected_sql=ddl,
    )
    if table_name == "agent_command_receipt":
        rows = connection.exec_driver_sql("PRAGMA table_list('agent_command_receipt')").fetchall()
        if len(rows) != 1 or int(rows[0][4]) != 1:
            raise RuntimeError("0029 requires agent_command_receipt WITHOUT ROWID")


def _ensure_immutable_trigger(connection: Any, table_name: str, operation: str) -> None:
    trigger_name = f"trg_{table_name}_immutable_{operation.lower()}"
    ddl = f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_name}
        BEFORE {operation} ON {table_name}
        BEGIN
          SELECT RAISE(ABORT, '{table_name} is immutable');
        END
        """
    connection.exec_driver_sql(ddl)
    _require_schema_sql(
        connection,
        object_type="trigger",
        object_name=trigger_name,
        expected_sql=ddl,
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for name, definition, expected_type, expected_not_null, expected_default in _AGGREGATE_COLUMNS:
        _ensure_aggregate_column(
            connection,
            name=name,
            definition=definition,
            expected_type=expected_type,
            expected_not_null=expected_not_null,
            expected_default=expected_default,
        )
    for table_name, ddl, expected_columns in _TABLE_DDL:
        _ensure_table(
            connection,
            table_name=table_name,
            ddl=ddl,
            expected_columns=expected_columns,
        )
    for index_name, statement in _INDEX_DDL:
        connection.exec_driver_sql(statement)
        _require_schema_sql(
            connection,
            object_type="index",
            object_name=index_name,
            expected_sql=statement,
        )
    for table_name in _IMMUTABLE_TABLES:
        _ensure_immutable_trigger(connection, table_name, "UPDATE")
        _ensure_immutable_trigger(connection, table_name, "DELETE")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
