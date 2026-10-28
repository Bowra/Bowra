"""Add Composer batch-A submission receipts and abandonment fences.

Only keyed semantic fingerprints and public response receipts are persisted;
prompt bodies, attachment metadata and credentials are deliberately excluded.

Revision ID: 0010_composer_submission_protocol
Revises: 0009_builtin_subagent_override
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import context

revision = "0010_composer_submission_protocol"
down_revision = "0009_builtin_subagent_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    turn_columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(turns)").fetchall()
    }
    if "execution_generation" not in turn_columns:
        connection.exec_driver_sql(
            "ALTER TABLE turns ADD COLUMN execution_generation "
            "INTEGER NOT NULL DEFAULT 1 CHECK(execution_generation>=1)",
        )
    if "generation_base_tokens_input" not in turn_columns:
        connection.exec_driver_sql(
            "ALTER TABLE turns ADD COLUMN generation_base_tokens_input "
            "INTEGER NOT NULL DEFAULT 0 CHECK(generation_base_tokens_input>=0)",
        )
    if "generation_base_tokens_output" not in turn_columns:
        connection.exec_driver_sql(
            "ALTER TABLE turns ADD COLUMN generation_base_tokens_output "
            "INTEGER NOT NULL DEFAULT 0 CHECK(generation_base_tokens_output>=0)",
        )
    if "generation_base_cost_usd" not in turn_columns:
        connection.exec_driver_sql(
            "ALTER TABLE turns ADD COLUMN generation_base_cost_usd "
            "REAL NOT NULL DEFAULT 0 CHECK(generation_base_cost_usd>=0)",
        )
    plan_columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(plans)").fetchall()
    }
    if plan_columns and "execution_turn_id" not in plan_columns:
        connection.exec_driver_sql("ALTER TABLE plans ADD COLUMN execution_turn_id TEXT")
    if plan_columns:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_execution_turn "
            "ON plans(execution_turn_id) WHERE execution_turn_id IS NOT NULL"
        )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_submission_receipts (
          team_id                      TEXT NOT NULL,
          user_id                      TEXT NOT NULL,
          client_submission_id         TEXT NOT NULL,
          workspace_id                 TEXT NOT NULL,
          requested_session_id         TEXT,
          actual_session_id            TEXT,
          turn_id                      TEXT,
          submission_kind              TEXT NOT NULL,
          submission_contract_version  TEXT NOT NULL,
          model_control_batch          TEXT NOT NULL,
          request_fingerprint          TEXT NOT NULL,
          fingerprint_key_id           TEXT NOT NULL,
          auth_session_generation_hash TEXT NOT NULL,
          outcome                      TEXT NOT NULL,
          response_json                TEXT NOT NULL,
          created_at                   TEXT NOT NULL,
          expires_at                   TEXT NOT NULL,
          PRIMARY KEY (team_id, user_id, client_submission_id),
          CHECK (submission_kind IN ('start','replay','steer')),
          CHECK (submission_contract_version IN
            ('chat-submit.v1','chat-model.v2','chat-model-effort.v3')),
          CHECK (model_control_batch IN ('a','b','c')),
          CHECK ((submission_contract_version='chat-submit.v1') = (model_control_batch='a')),
          CHECK ((submission_contract_version='chat-model.v2') = (model_control_batch='b')),
          CHECK ((submission_contract_version='chat-model-effort.v3') = (model_control_batch='c')),
          CHECK (outcome IN ('started','buffered','cancelled','completed','rejected')),
          CHECK (length(request_fingerprint)=64),
          CHECK (length(fingerprint_key_id)=64),
          CHECK (length(auth_session_generation_hash)=64)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_submission_receipts_expiry
        ON chat_submission_receipts(expires_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_submission_inflight (
          team_id                      TEXT NOT NULL,
          user_id                      TEXT NOT NULL,
          client_submission_id         TEXT NOT NULL,
          workspace_id                 TEXT NOT NULL,
          requested_session_id         TEXT,
          submission_kind              TEXT NOT NULL,
          submission_contract_version  TEXT NOT NULL,
          model_control_batch          TEXT NOT NULL,
          request_fingerprint          TEXT NOT NULL,
          fingerprint_key_id           TEXT NOT NULL,
          auth_session_generation_hash TEXT NOT NULL,
          lease_owner                  TEXT NOT NULL,
          lease_version                INTEGER NOT NULL,
          lease_expires_at             TEXT NOT NULL,
          created_at                   TEXT NOT NULL,
          PRIMARY KEY (team_id, user_id, client_submission_id),
          CHECK (submission_kind IN ('start','replay','steer')),
          CHECK (submission_contract_version IN
            ('chat-submit.v1','chat-model.v2','chat-model-effort.v3')),
          CHECK (model_control_batch IN ('a','b','c')),
          CHECK ((submission_contract_version='chat-submit.v1') = (model_control_batch='a')),
          CHECK ((submission_contract_version='chat-model.v2') = (model_control_batch='b')),
          CHECK ((submission_contract_version='chat-model-effort.v3') = (model_control_batch='c')),
          CHECK (length(request_fingerprint)=64),
          CHECK (length(fingerprint_key_id)=64),
          CHECK (length(auth_session_generation_hash)=64),
          CHECK (lease_version>=1)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_submission_inflight_expiry
        ON chat_submission_inflight(lease_expires_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_auth_session_generations (
          user_id                       TEXT NOT NULL,
          auth_session_generation_hash  TEXT NOT NULL,
          state                         TEXT NOT NULL,
          created_at                    TEXT NOT NULL,
          invalidated_at                TEXT,
          PRIMARY KEY (user_id, auth_session_generation_hash),
          CHECK (length(auth_session_generation_hash)=64),
          CHECK (state IN ('active','invalidated'))
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_auth_generation_state
        ON chat_auth_session_generations(user_id, state)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_auth_user_generation_fences (
          user_id                       TEXT PRIMARY KEY,
          invalidated_before_epoch      REAL NOT NULL,
          updated_at                    TEXT NOT NULL,
          CHECK (invalidated_before_epoch>=0)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_submission_fingerprint_epochs (
          fingerprint_key_id            TEXT PRIMARY KEY,
          first_seen_at                  TEXT NOT NULL,
          last_seen_at                   TEXT NOT NULL,
          CHECK (length(fingerprint_key_id)=64)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE security_key_epochs (
          purpose                       TEXT NOT NULL,
          key_id                        TEXT NOT NULL,
          activated_at                  TEXT NOT NULL,
          retired_at                    TEXT,
          accept_until                  TEXT,
          PRIMARY KEY (purpose, key_id),
          CHECK (purpose='chat_submission_fingerprint'),
          CHECK (length(key_id)=64),
          CHECK ((retired_at IS NULL)=(accept_until IS NULL)),
          CHECK (retired_at IS NULL OR retired_at>=activated_at),
          CHECK (accept_until IS NULL OR accept_until>=retired_at)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX idx_security_key_epochs_one_current
        ON security_key_epochs(purpose) WHERE retired_at IS NULL
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER trg_security_key_epochs_no_delete
        BEFORE DELETE ON security_key_epochs
        BEGIN
          SELECT RAISE(ABORT, 'security key epoch history is immutable');
        END
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER trg_security_key_epochs_immutable
        BEFORE UPDATE ON security_key_epochs
        WHEN NEW.purpose IS NOT OLD.purpose
          OR NEW.key_id IS NOT OLD.key_id
          OR NEW.activated_at IS NOT OLD.activated_at
          OR (OLD.retired_at IS NOT NULL AND NEW.retired_at IS NOT OLD.retired_at)
          OR (OLD.retired_at IS NOT NULL AND NEW.accept_until>OLD.accept_until)
          OR (OLD.retired_at IS NULL AND
              (NEW.retired_at IS NULL OR NEW.accept_until IS NULL))
        BEGIN
          SELECT RAISE(ABORT, 'security key epoch mutation is not allowed');
        END
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_submission_abandon_tombstones (
          team_id                      TEXT NOT NULL,
          user_id                      TEXT NOT NULL,
          client_submission_id         TEXT NOT NULL,
          workspace_id                 TEXT NOT NULL,
          requested_session_id         TEXT,
          created_at                   TEXT NOT NULL,
          expires_at                   TEXT NOT NULL,
          PRIMARY KEY (team_id, user_id, client_submission_id)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_submission_abandon_expiry
        ON chat_submission_abandon_tombstones(expires_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_turn_user_sources (
          turn_id                      TEXT PRIMARY KEY,
          session_id                   TEXT NOT NULL,
          user_id                      TEXT NOT NULL,
          workspace_id                 TEXT NOT NULL,
          user_text                    TEXT NOT NULL,
          attachment_ids_json          TEXT NOT NULL,
          attachment_refs_json         TEXT NOT NULL,
          parts_json                   TEXT,
          created_at                   TEXT NOT NULL,
          FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE,
          CHECK (json_valid(attachment_ids_json)),
          CHECK (json_type(attachment_ids_json)='array'),
          CHECK (json_valid(attachment_refs_json)),
          CHECK (json_type(attachment_refs_json)='array'),
          CHECK (parts_json IS NULL OR json_valid(parts_json)),
          CHECK (parts_json IS NULL OR json_type(parts_json)='array')
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_turn_user_sources_scope
        ON chat_turn_user_sources(user_id, workspace_id, session_id)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_attachment_bundle_members (
          bundle_artifact_id            TEXT NOT NULL,
          child_artifact_id             TEXT NOT NULL,
          created_at                    TEXT NOT NULL,
          PRIMARY KEY (bundle_artifact_id, child_artifact_id),
          FOREIGN KEY(bundle_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
          FOREIGN KEY(child_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_attachment_bundle_child
        ON chat_attachment_bundle_members(child_artifact_id, bundle_artifact_id)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE chat_turn_spawn_dispatch (
          turn_id                      TEXT PRIMARY KEY,
          session_id                   TEXT NOT NULL,
          source_submission_id         TEXT,
          parent_turn_id               TEXT,
          source_submission_ids_json   TEXT NOT NULL DEFAULT '[]',
          dispatch_kind                TEXT NOT NULL,
          state                        TEXT NOT NULL,
          lease_kind                   TEXT NOT NULL DEFAULT 'turn',
          lease_execution_generation   INTEGER NOT NULL DEFAULT 1,
          owner_id                     TEXT,
          cancel_requested_at          TEXT,
          lease_expires_at             TEXT NOT NULL,
          created_at                   TEXT NOT NULL,
          updated_at                   TEXT NOT NULL,
          FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE,
          CHECK (dispatch_kind IN ('start','follow_up')),
          CHECK (state IN ('claimed','spawned','drain_pending','terminal','failed')),
          CHECK (lease_kind IN ('turn','resume')),
          CHECK (lease_execution_generation>=1),
          CHECK (json_valid(source_submission_ids_json)),
          CHECK (json_type(source_submission_ids_json)='array')
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_turn_spawn_dispatch_state
        ON chat_turn_spawn_dispatch(state, updated_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_chat_turn_spawn_dispatch_parent
        ON chat_turn_spawn_dispatch(parent_turn_id, created_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE turn_cancel_receipts (
          team_id                       TEXT NOT NULL,
          user_id                       TEXT NOT NULL,
          cancel_request_id             TEXT NOT NULL,
          workspace_id                  TEXT NOT NULL,
          turn_id                       TEXT NOT NULL,
          expected_execution_generation INTEGER NOT NULL,
          outcome                       TEXT NOT NULL,
          turn_erased                   INTEGER NOT NULL DEFAULT 0,
          created_at                    TEXT NOT NULL,
          expires_at                    TEXT NOT NULL,
          PRIMARY KEY (team_id, user_id, cancel_request_id),
          CHECK (expected_execution_generation>=1),
          CHECK (turn_erased IN (0,1)),
          CHECK (outcome IN ('cancelled','terminal','generation_conflict'))
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_turn_cancel_receipts_turn
        ON turn_cancel_receipts(turn_id, created_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_turn_cancel_receipts_expiry
        ON turn_cancel_receipts(expires_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER trg_turn_delete_cancel_receipt_tombstone
        BEFORE DELETE ON turns
        BEGIN
          UPDATE turn_cancel_receipts SET turn_erased=1 WHERE turn_id=OLD.id;
        END
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE composer_telemetry_dedupe (
          user_id                      TEXT NOT NULL,
          event_id                     TEXT NOT NULL,
          occurred_at                  TEXT NOT NULL,
          received_at                  TEXT NOT NULL,
          PRIMARY KEY (user_id, event_id)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_composer_telemetry_dedupe_received
        ON composer_telemetry_dedupe(received_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE clarification_resolution_outbox (
          request_id                   TEXT PRIMARY KEY,
          turn_id                      TEXT NOT NULL,
          state                        TEXT NOT NULL,
          channel                      TEXT NOT NULL,
          work_json                    TEXT NOT NULL,
          payload_json                 TEXT,
          lease_owner                  TEXT,
          lease_expires_at             TEXT,
          attempt_count                INTEGER NOT NULL DEFAULT 0,
          created_at                   TEXT NOT NULL,
          updated_at                   TEXT NOT NULL,
          CHECK (state IN ('pending','completed')),
          CHECK (channel IN ('web','feishu','teams','legacy')),
          CHECK (json_valid(work_json)),
          CHECK (json_type(work_json)='object'),
          CHECK (payload_json IS NULL OR json_valid(payload_json)),
          CHECK (payload_json IS NULL OR json_type(payload_json)='object'),
          CHECK (attempt_count>=0),
          CHECK ((state='pending' AND payload_json IS NULL AND lease_owner IS NOT NULL
                  AND lease_expires_at IS NOT NULL)
              OR (state='completed' AND payload_json IS NOT NULL
                  AND lease_owner IS NULL AND lease_expires_at IS NULL))
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX idx_clarification_resolution_outbox_pending
        ON clarification_resolution_outbox(state, lease_expires_at)
        """,
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
