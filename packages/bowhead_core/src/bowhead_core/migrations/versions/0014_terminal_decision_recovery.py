"""Add durable retry/effect state for Terminal Protocol decisions.

Revision ID: 0014_terminal_decision_recovery
Revises: 0013_terminal_execution_envelopes
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import context

revision = "0014_terminal_decision_recovery"
down_revision = "0013_terminal_execution_envelopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    clarification_columns = {
        str(row[1])
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(clarification_requests)"
        ).fetchall()
    }
    if "answers_json" not in clarification_columns:
        connection.exec_driver_sql(
            "ALTER TABLE clarification_requests ADD COLUMN answers_json TEXT"
        )
    generation_columns = {
        str(row[1])
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(turn_generations)"
        ).fetchall()
    }
    if "claim_authz_snapshot_digest" not in generation_columns:
        connection.exec_driver_sql(
            "ALTER TABLE turn_generations "
            "ADD COLUMN claim_authz_snapshot_digest TEXT"
        )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_generation_claim_auth_once
        BEFORE UPDATE OF claim_authz_snapshot_digest ON turn_generations
        WHEN NEW.claim_authz_snapshot_digest IS NOT OLD.claim_authz_snapshot_digest
          AND NOT (
            OLD.claim_authz_snapshot_digest IS NULL
            AND NEW.claim_authz_snapshot_digest IS NOT NULL
            AND OLD.state='opened' AND OLD.owner_instance_id IS NULL
            AND OLD.claim_epoch=0
          )
        BEGIN
          SELECT RAISE(ABORT, 'terminal generation claim authorization is immutable');
        END
        """
    )
    columns = {
        str(row[1])
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(turn_control_decisions)"
        ).fetchall()
    }
    additions = (
        ("recovery_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("recovery_next_attempt_at", "TEXT"),
        ("recovery_last_code", "TEXT"),
        ("control_outcome_code", "TEXT"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE turn_control_decisions ADD COLUMN {name} {definition}"
            )
    # Pre-0011 system revocations did not carry a denial-reason audit field.
    # Preserve their immutable digest and bind their equivalent finite outcome.
    connection.exec_driver_sql(
        """
        UPDATE turn_control_decisions
        SET control_outcome_code='access_revoked'
        WHERE source_kind='system_control' AND action='revoke'
          AND control_outcome_code IS NULL
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_decision_control_outcome_shape_insert
        BEFORE INSERT ON turn_control_decisions
        WHEN (
          NEW.source_kind='system_control' AND NEW.action='revoke'
          AND (
            NEW.control_outcome_code IS NULL
            OR NEW.control_outcome_code NOT IN (
              'access_revoked','dependency_missing','entitlement_denied'
            )
          )
        ) OR (
          NOT (NEW.source_kind='system_control' AND NEW.action='revoke')
          AND NEW.control_outcome_code IS NOT NULL
        )
        BEGIN
          SELECT RAISE(ABORT, 'terminal decision control outcome is invalid');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_decision_control_outcome_immutable
        BEFORE UPDATE OF control_outcome_code ON turn_control_decisions
        WHEN NEW.control_outcome_code IS NOT OLD.control_outcome_code
        BEGIN
          SELECT RAISE(ABORT, 'terminal decision control outcome is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_terminal_decision_recovery_due
        ON turn_control_decisions(state,recovery_next_attempt_at,recorded_at,decision_id)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS turn_decision_effects (
          effect_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL,
          effect_kind TEXT NOT NULL,
          target_ref TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_digest TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending','claimed','applied','skipped')),
          claim_owner TEXT,
          claim_epoch INTEGER NOT NULL DEFAULT 0,
          claim_expires_at TEXT,
          next_attempt_at TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_code TEXT,
          resolution_code TEXT,
          created_at TEXT NOT NULL,
          applied_at TEXT,
          UNIQUE(decision_id,effect_kind,target_ref),
          FOREIGN KEY(decision_id) REFERENCES turn_control_decisions(decision_id),
          CHECK (
            (state='applied' AND applied_at IS NOT NULL AND resolution_code IS NULL)
            OR (state='skipped' AND applied_at IS NOT NULL AND resolution_code IS NOT NULL)
            OR (state IN ('pending','claimed') AND applied_at IS NULL AND resolution_code IS NULL)
          )
        )
        """
    )
    connection.exec_driver_sql(
        "DROP INDEX IF EXISTS idx_terminal_decision_effect_pending"
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_terminal_decision_effect_pending
        ON turn_decision_effects(state,next_attempt_at,claim_expires_at,created_at,effect_id)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_decision_effect_identity_immutable
        BEFORE UPDATE OF effect_id,decision_id,effect_kind,target_ref,payload_json,payload_digest,created_at
        ON turn_decision_effects
        WHEN NEW.effect_id IS NOT OLD.effect_id OR NEW.decision_id IS NOT OLD.decision_id
          OR NEW.effect_kind IS NOT OLD.effect_kind OR NEW.target_ref IS NOT OLD.target_ref
          OR NEW.payload_json IS NOT OLD.payload_json OR NEW.payload_digest IS NOT OLD.payload_digest
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
          SELECT RAISE(ABORT, 'terminal decision effect identity is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_approval_execution_context_immutable
        BEFORE UPDATE OF turn_id,session_id,workspace_id,member_uid,
          datarc_workspace_id,user_id,tool_name,arguments_json,request_key,
          arguments_digest,reason,risk_level,created_at ON approvals
        WHEN EXISTS (
          SELECT 1 FROM turn_control_decisions d
          WHERE d.source_kind='approval' AND d.source_id=OLD.id
        ) AND (
          NEW.turn_id IS NOT OLD.turn_id
          OR NEW.session_id IS NOT OLD.session_id
          OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.member_uid IS NOT OLD.member_uid
          OR NEW.datarc_workspace_id IS NOT OLD.datarc_workspace_id
          OR NEW.user_id IS NOT OLD.user_id
          OR NEW.tool_name IS NOT OLD.tool_name
          OR NEW.arguments_json IS NOT OLD.arguments_json
          OR NEW.arguments_digest IS NOT OLD.arguments_digest
          OR NEW.request_key IS NOT OLD.request_key
          OR NEW.reason IS NOT OLD.reason
          OR NEW.risk_level IS NOT OLD.risk_level
          OR NEW.created_at IS NOT OLD.created_at
        )
        BEGIN
          SELECT RAISE(ABORT, 'terminal approval execution context is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_clarification_execution_context_immutable
        BEFORE UPDATE OF turn_id,user_id,workspace_id,token,rationale,
          candidates_json,status,chosen_value,chosen_at,created_at,kind,
          multi_select,chosen_values_json,answers_json,user_directive,
          edited_subs_json,session_id,resume_context_json,tool
        ON clarification_requests
        WHEN EXISTS (
          SELECT 1 FROM turn_control_decisions d
          WHERE d.source_kind='clarification'
            AND d.source_id=OLD.request_id
        ) AND (
          NEW.turn_id IS NOT OLD.turn_id
          OR NEW.user_id IS NOT OLD.user_id
          OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.token IS NOT OLD.token
          OR NEW.rationale IS NOT OLD.rationale
          OR NEW.candidates_json IS NOT OLD.candidates_json
          OR NEW.status IS NOT OLD.status
          OR NEW.chosen_value IS NOT OLD.chosen_value
          OR NEW.chosen_at IS NOT OLD.chosen_at
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.kind IS NOT OLD.kind
          OR NEW.multi_select IS NOT OLD.multi_select
          OR NEW.chosen_values_json IS NOT OLD.chosen_values_json
          OR NEW.answers_json IS NOT OLD.answers_json
          OR NEW.user_directive IS NOT OLD.user_directive
          OR NEW.edited_subs_json IS NOT OLD.edited_subs_json
          OR NEW.session_id IS NOT OLD.session_id
          OR NEW.resume_context_json IS NOT OLD.resume_context_json
          OR NEW.tool IS NOT OLD.tool
        )
        BEGIN
          SELECT RAISE(ABORT, 'terminal clarification execution context is immutable');
        END
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
