"""Add owner-bound conversational Subagent draft persistence.

Revision ID: 0006_subagent_conversational_drafts
Revises: 0005_team_llm_verification
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import context

revision = "0006_subagent_conversational_drafts"
down_revision = "0005_team_llm_verification"
branch_labels = None
depends_on = None

_TEAM_SUBAGENT_EXPAND_COLUMNS = (
    ("owner_user_id", "TEXT"),
    ("row_version", "INTEGER NOT NULL DEFAULT 1"),
    ("audience_mode", "TEXT NOT NULL DEFAULT 'private'"),
    ("lifecycle_state", "TEXT NOT NULL DEFAULT 'active'"),
    ("quarantine_started_at", "TEXT"),
    ("quarantine_expires_at", "TEXT"),
    ("quarantine_reason", "TEXT"),
    ("quarantined_owner_user_id", "TEXT"),
    (
        "origin_mode",
        "TEXT NOT NULL DEFAULT 'manual' "
        "CHECK(origin_mode IN ('manual', 'intent_draft', 'turn_draft'))",
    ),
    ("source_draft_id", "TEXT"),
)


def _column_names(table: str) -> set[str]:
    rows = context.get_bind().exec_driver_sql(
        f"PRAGMA table_info({table})",
    )
    return {str(row[1]) for row in rows}


def _expand_team_subagent() -> None:
    connection = context.get_bind()
    existing = _column_names("team_subagent")
    for name, definition in _TEAM_SUBAGENT_EXPAND_COLUMNS:
        if name not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE team_subagent ADD COLUMN {name} {definition}",
            )

    # Adopt the pre-Alembic Release-A compatibility tables for clean 1.0.0
    # fixtures as well as already-running deployments.
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_subagent_schema_meta (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          schema_version INTEGER NOT NULL,
          minimum_reader_version TEXT NOT NULL,
          minimum_writer_version TEXT NOT NULL,
          owner_semantics_activated_at TEXT,
          lifecycle_semantics_activated_at TEXT,
          audience_semantics_activated_at TEXT,
          updated_at TEXT NOT NULL
        )
        """,
    )
    connection.exec_driver_sql(
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
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO team_subagent_schema_meta (
          singleton, schema_version, minimum_reader_version,
          minimum_writer_version, updated_at
        ) VALUES (
          1, 3, '0001', '0001', '1970-01-01T00:00:00+00:00'
        )
        """,
    )
    connection.exec_driver_sql(
        """
        UPDATE team_subagent_schema_meta
        SET schema_version = 3
        WHERE singleton = 1 AND schema_version < 3
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_team_subagent_source_draft
        ON team_subagent(source_draft_id)
        WHERE source_draft_id IS NOT NULL
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS trg_team_subagent_attribution_immutable
        BEFORE UPDATE OF origin_mode, source_draft_id ON team_subagent
        WHEN NEW.origin_mode IS NOT OLD.origin_mode
          OR NEW.source_draft_id IS NOT OLD.source_draft_id
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent attribution is immutable');
        END
        """,
    )


def _create_draft_tables() -> None:
    connection = context.get_bind()
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_subagent_authoring_context (
          team_id TEXT NOT NULL,
          owner_user_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          state TEXT NOT NULL
            CHECK(state IN ('collecting_purpose', 'clarifying')),
          purpose_text TEXT,
          purpose_prompt_count INTEGER NOT NULL DEFAULT 0
            CHECK(purpose_prompt_count BETWEEN 0 AND 2),
          insufficient_reprompt_count INTEGER NOT NULL DEFAULT 0
            CHECK(insufficient_reprompt_count BETWEEN 0 AND 1),
          business_clarification_count INTEGER NOT NULL DEFAULT 0
            CHECK(business_clarification_count BETWEEN 0 AND 1),
          draft_id TEXT,
          row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (team_id, owner_user_id, session_id),
          CHECK(length(purpose_text) <= 2000),
          CHECK(
            (state = 'clarifying' AND draft_id IS NOT NULL)
            OR state = 'collecting_purpose'
          )
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_subagent_authoring_context_expiry
        ON team_subagent_authoring_context(expires_at)
        """,
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_subagent_draft_request (
          team_id TEXT NOT NULL,
          owner_user_id TEXT NOT NULL,
          idempotency_key_hash TEXT NOT NULL,
          request_fingerprint TEXT NOT NULL,
          state TEXT NOT NULL
            CHECK(state IN ('assessing', 'completed', 'failed')),
          result_draft_id TEXT,
          outcome_code TEXT,
          lease_expires_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (team_id, owner_user_id, idempotency_key_hash),
          CHECK(
            (state = 'completed' AND outcome_code IS NOT NULL)
            OR state != 'completed'
          )
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_subagent_draft_request_expiry
        ON team_subagent_draft_request(expires_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_subagent_draft_usage (
          team_id TEXT NOT NULL,
          owner_user_id TEXT NOT NULL,
          request_key_hash TEXT NOT NULL,
          state TEXT NOT NULL
            CHECK(state IN (
              'reserved', 'settlement_pending', 'settled', 'released'
            )),
          reserved_credits INTEGER NOT NULL CHECK(reserved_credits >= 0),
          actual_credits INTEGER CHECK(actual_credits >= 0),
          input_tokens INTEGER CHECK(input_tokens >= 0),
          output_tokens INTEGER CHECK(output_tokens >= 0),
          attempts INTEGER CHECK(attempts BETWEEN 1 AND 2),
          draft_id TEXT,
          reservation_ledger_id TEXT NOT NULL,
          adjustment_ledger_id TEXT,
          usage_event_id TEXT,
          lease_expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(team_id, owner_user_id, request_key_hash),
          CHECK(
            (state = 'reserved' AND actual_credits IS NULL)
            OR (
              state != 'reserved'
              AND actual_credits IS NOT NULL
            )
          )
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_subagent_draft_usage_lease
        ON team_subagent_draft_usage(state, lease_expires_at)
        """,
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_subagent_draft (
          id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          owner_user_id TEXT NOT NULL,
          source_mode TEXT NOT NULL
            CHECK(source_mode IN ('intent', 'turn')),
          entry_mode TEXT NOT NULL DEFAULT 'conversational'
            CHECK(entry_mode = 'conversational'),
          source_turn_id TEXT,
          status TEXT NOT NULL
            CHECK(status IN (
              'generating', 'needs_clarification', 'ready', 'blocked',
              'expired', 'consumed', 'discarded'
            )),
          validation_status TEXT NOT NULL
            CHECK(validation_status IN ('valid', 'warning', 'invalid', 'unknown')),
          locale TEXT NOT NULL,
          purpose_text TEXT,
          goal_summary TEXT,
          definition_json TEXT,
          recommendations_json TEXT,
          assumptions_json TEXT NOT NULL DEFAULT '[]',
          warnings_json TEXT NOT NULL DEFAULT '[]',
          clarification_json TEXT,
          validation_issues_json TEXT NOT NULL DEFAULT '[]',
          catalog_fingerprint TEXT,
          definition_fingerprint TEXT,
          idempotency_key_hash TEXT NOT NULL,
          mutation_id TEXT NOT NULL,
          generation_attempt INTEGER NOT NULL DEFAULT 1
            CHECK(generation_attempt BETWEEN 1 AND 3),
          business_clarification_count INTEGER NOT NULL DEFAULT 0
            CHECK(business_clarification_count BETWEEN 0 AND 1),
          row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          outcome_reason_code TEXT,
          consumed_definition_id TEXT,
          consumed_at TEXT,
          discarded_at TEXT,
          expired_at TEXT,
          expires_at TEXT NOT NULL,
          content_scrub_at TEXT NOT NULL,
          purge_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          CHECK(id LIKE 'sad_%'),
          CHECK(length(purpose_text) <= 2000),
          CHECK(length(goal_summary) <= 500),
          CHECK(length(definition_json) <= 65536),
          CHECK(length(recommendations_json) <= 131072),
          CHECK(length(assumptions_json) <= 16384),
          CHECK(length(warnings_json) <= 16384),
          CHECK(length(clarification_json) <= 16384),
          CHECK(length(validation_issues_json) <= 32768),
          CHECK(
            (source_mode = 'turn' AND source_turn_id IS NOT NULL)
            OR (source_mode = 'intent' AND source_turn_id IS NULL)
          ),
          CHECK(
            (
              status = 'consumed'
              AND consumed_definition_id IS NOT NULL
              AND consumed_at IS NOT NULL
              AND purpose_text IS NULL
              AND goal_summary IS NULL
              AND definition_json IS NULL
              AND recommendations_json IS NULL
              AND clarification_json IS NULL
            )
            OR (
              status != 'consumed'
              AND consumed_definition_id IS NULL
              AND consumed_at IS NULL
            )
          ),
          CHECK(
            (
              status = 'discarded'
              AND discarded_at IS NOT NULL
              AND purpose_text IS NULL
              AND goal_summary IS NULL
              AND definition_json IS NULL
              AND recommendations_json IS NULL
              AND clarification_json IS NULL
            )
            OR (status != 'discarded' AND discarded_at IS NULL)
          ),
          CHECK(
            (status = 'expired' AND expired_at IS NOT NULL)
            OR (status != 'expired' AND expired_at IS NULL)
          ),
          CHECK(
            status != 'needs_clarification'
            OR clarification_json IS NOT NULL
          ),
          CHECK(
            status != 'ready'
            OR (
              definition_json IS NOT NULL
              AND definition_fingerprint IS NOT NULL
              AND validation_status IN ('valid', 'warning')
            )
          ),
          CHECK(
            expires_at < content_scrub_at
            AND content_scrub_at < purge_at
          )
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_team_subagent_draft_idempotency
        ON team_subagent_draft(
          team_id, owner_user_id, idempotency_key_hash
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_subagent_draft_owner_status_updated
        ON team_subagent_draft(
          team_id, owner_user_id, status, updated_at DESC
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_subagent_draft_expiry
        ON team_subagent_draft(status, expires_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_subagent_draft_source_turn
        ON team_subagent_draft(team_id, owner_user_id, source_turn_id)
        WHERE source_turn_id IS NOT NULL
        """,
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    _expand_team_subagent()
    _create_draft_tables()


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
