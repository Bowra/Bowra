"""Add the reader-first Terminal Protocol v1 schema.

This expand-only revision adds protocol metadata, immutable terminal ledgers,
lifecycle/effect retention tables, indexes, and shape guards. It does not
backfill legacy Turn/Session rows and does not enable a v1 writer.

Revision ID: 0008_terminal_protocol_v1
Revises: 0007_config_copilot_drafts
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import context

revision = "0008_terminal_protocol_v1"
down_revision = "0007_config_copilot_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Terminal Protocol state lives only in bowhead.db. The seven KB targets
    # explicitly no-op so every SQLite target advances on the same revision chain.
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    _add_protocol_columns(connection)
    _create_protocol_tables(connection)
    _create_protocol_indexes(connection)
    _create_protocol_triggers(connection)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")


def _add_protocol_columns(connection) -> None:
    turn_columns = (
        "terminal_protocol_version INTEGER",
        "terminal_ingress_surface TEXT CHECK (terminal_ingress_surface IS NULL OR length(terminal_ingress_surface) BETWEEN 1 AND 64)",
        "terminal_admission_cohort TEXT CHECK (terminal_admission_cohort IS NULL OR length(terminal_admission_cohort) BETWEEN 1 AND 256)",
        "protocol_admitted_at TEXT",
        "current_generation INTEGER",
        "generation_transition_epoch INTEGER",
        "last_terminal_generation INTEGER",
        "current_terminal_receipt_id TEXT",
        "event_sequence_next INTEGER",
        "delivery_fenced_at TEXT",
        "delivery_fence_epoch INTEGER",
        "presentation_locale TEXT CHECK (presentation_locale IS NULL OR length(presentation_locale) BETWEEN 1 AND 35)",
        "message_catalog_version INTEGER",
    )
    session_columns = (
        "active_turn_protocol_version INTEGER",
        "active_turn_generation INTEGER",
        "active_turn_claim_epoch INTEGER",
        "recovery_lock_turn_id TEXT",
        "recovery_lock_generation INTEGER",
        "recovery_lock_reason TEXT CHECK (recovery_lock_reason IS NULL OR recovery_lock_reason IN ('commit_outcome_unknown','owner_lost','effect_outcome_unknown','dependency_missing','non_durable'))",
        "steer_buffer_revision INTEGER NOT NULL DEFAULT 0",
        "steer_freeze_turn_id TEXT",
        "steer_freeze_generation INTEGER",
        "steer_freeze_claim_epoch INTEGER",
        "steer_frozen_revision INTEGER",
        "steer_buffer_state TEXT NOT NULL DEFAULT 'collecting' CHECK (steer_buffer_state IN ('collecting','frozen','deferred'))",
    )
    event_columns = (
        "protocol_version INTEGER",
        "generation INTEGER",
        "event_seq INTEGER",
        "actor_scope TEXT",
        "phase_scope TEXT",
        "payload_digest TEXT",
        "terminal_receipt_id TEXT",
        "source_proposal_id TEXT",
        "accept_ordinal INTEGER",
    )
    for definition in turn_columns:
        _add_column_if_missing(connection, "turns", definition)
    for definition in session_columns:
        _add_column_if_missing(connection, "sessions", definition)
    for definition in event_columns:
        _add_column_if_missing(connection, "turn_events", definition)


def _add_column_if_missing(connection, table: str, definition: str) -> None:
    column = definition.split(" ", 1)[0]
    columns = {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _create_protocol_tables(connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS turn_generations (
          turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE NO ACTION,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
          kind TEXT NOT NULL CHECK (kind IN ('execution','control')),
          prior_generation INTEGER,
          decision_id TEXT,
          execution_intent_id TEXT,
          execution_intent_digest TEXT,
          opened_at TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('opened','running','closing','recovery_pending','closed')),
          row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
          closing_reason TEXT,
          closed_at TEXT,
          terminal_candidate_id TEXT,
          terminal_receipt_id TEXT,
          accepted_proposal_next INTEGER NOT NULL DEFAULT 1 CHECK (accepted_proposal_next >= 1),
          closing_accepted_through INTEGER CHECK (closing_accepted_through >= 0),
          owner_instance_id TEXT,
          claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK (claim_epoch >= 0),
          writer_lease_epoch_at_claim INTEGER,
          claimed_at TEXT,
          lease_expires_at TEXT,
          last_heartbeat_at TEXT,
          finalization_owner_instance_id TEXT,
          finalization_epoch INTEGER NOT NULL DEFAULT 0 CHECK (finalization_epoch >= 0),
          finalization_lease_expires_at TEXT,
          finalization_started_at TEXT,
          execution_started_at TEXT,
          execution_effect_class TEXT NOT NULL DEFAULT 'none'
            CHECK (execution_effect_class IN ('none','read_only','side_effect_possible')),
          input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
          output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
          cost_amount REAL NOT NULL DEFAULT 0 CHECK (cost_amount >= 0),
          cost_currency TEXT NOT NULL DEFAULT 'USD',
          usage_digest TEXT,
          recovery_started_at TEXT,
          recovery_reason TEXT,
          recovery_resolution TEXT CHECK (recovery_resolution IN ('receipt_committed','fenced_deleted')),
          PRIMARY KEY (turn_id, generation),
          CHECK (prior_generation IS NULL OR prior_generation >= 1),
          CHECK (kind != 'execution' OR (execution_intent_id IS NOT NULL AND execution_intent_digest IS NOT NULL)),
          CHECK (state NOT IN ('closing','recovery_pending','closed') OR closing_accepted_through IS NOT NULL),
          CHECK (state IN ('closing','recovery_pending','closed') OR closing_accepted_through IS NULL),
          CHECK (state != 'running' OR (owner_instance_id IS NOT NULL AND claim_epoch >= 1 AND lease_expires_at IS NOT NULL)),
          CHECK (state != 'closed' OR terminal_receipt_id IS NOT NULL OR recovery_resolution IS 'fenced_deleted'),
          CHECK ((state = 'closed' AND closed_at IS NOT NULL) OR
                 (state != 'closed' AND closed_at IS NULL AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL)),
          CHECK (state != 'closed' OR terminal_receipt_id IS NULL OR terminal_candidate_id IS NOT NULL)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_terminal_candidates (
          candidate_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          candidate_origin TEXT NOT NULL CHECK (candidate_origin IN ('normal','recovery','control')),
          reserved_receipt_id TEXT NOT NULL UNIQUE,
          reserved_terminal_event_id TEXT NOT NULL UNIQUE,
          prepared_execution_claim_epoch INTEGER,
          finalization_epoch_at_prepare INTEGER NOT NULL CHECK (finalization_epoch_at_prepare >= 1),
          followup_admission_epoch_at_prepare INTEGER,
          primary_status TEXT CHECK (primary_status IN ('completed','failed','cancelled','aborted','clarification_pending','approval_pending')),
          primary_reason_code TEXT,
          primary_message_key TEXT,
          closing_accepted_through INTEGER NOT NULL CHECK (closing_accepted_through >= 0),
          persisted_event_highwater INTEGER NOT NULL CHECK (persisted_event_highwater >= 0),
          terminal_event_counts_by_variant_json TEXT NOT NULL,
          candidate_projection_digest TEXT NOT NULL,
          canonicalization_version INTEGER NOT NULL CHECK (canonicalization_version = 1),
          state TEXT NOT NULL CHECK (state IN ('prepared','committed','fenced_deleted')),
          session_disposition_plan_json TEXT NOT NULL,
          session_disposition_plan_digest TEXT NOT NULL,
          projection_variant_digests_json TEXT NOT NULL,
          projection_extensions_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          prepared_at TEXT NOT NULL,
          committed_at TEXT,
          UNIQUE (turn_id, generation),
          FOREIGN KEY (turn_id, generation) REFERENCES turn_generations(turn_id, generation) ON DELETE NO ACTION
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_terminal_candidate_variants (
          candidate_id TEXT NOT NULL REFERENCES turn_terminal_candidates(candidate_id) ON DELETE NO ACTION,
          variant_key TEXT NOT NULL,
          outcome_variant TEXT NOT NULL CHECK (outcome_variant IN ('primary','recovery_interrupt')),
          session_variant TEXT NOT NULL CHECK (session_variant IN ('followup','deferred','discard','none')),
          status TEXT NOT NULL CHECK (status IN ('completed','failed','cancelled','aborted','clarification_pending','approval_pending')),
          reason_code TEXT,
          message_key TEXT NOT NULL,
          final_answer_mode TEXT NOT NULL CHECK (final_answer_mode IN ('none','precommitted','in_terminal_batch')),
          final_answer_event_id TEXT,
          final_answer_digest TEXT,
          required_manifest_template_digest TEXT NOT NULL,
          session_mutation_template_digest TEXT NOT NULL,
          variant_digest TEXT NOT NULL,
          PRIMARY KEY (candidate_id, variant_key),
          UNIQUE (candidate_id, outcome_variant, session_variant),
          CHECK ((final_answer_mode = 'none' AND final_answer_event_id IS NULL) OR
                 (final_answer_mode != 'none' AND final_answer_event_id IS NOT NULL)),
          CHECK ((final_answer_mode = 'none' AND final_answer_digest IS NULL) OR
                 (final_answer_mode != 'none' AND final_answer_digest IS NOT NULL)),
          CHECK ((status = 'completed' AND final_answer_mode != 'none') OR
                 (status IN ('cancelled','aborted','clarification_pending','approval_pending') AND
                  final_answer_mode = 'none') OR status = 'failed'),
          CHECK (outcome_variant != 'recovery_interrupt' OR
                 (status = 'aborted' AND final_answer_mode = 'none' AND session_variant IN ('deferred','discard','none')))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_terminal_candidate_events (
          candidate_id TEXT NOT NULL,
          variant_key TEXT NOT NULL,
          ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
          event_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          actor_scope TEXT NOT NULL CHECK (actor_scope IN ('root','peer','step','transport')),
          phase_scope TEXT NOT NULL CHECK (phase_scope IN ('in_generation','control_transition','post_terminal_auxiliary')),
          payload_json TEXT NOT NULL,
          payload_digest TEXT NOT NULL,
          PRIMARY KEY (candidate_id, variant_key, ordinal),
          FOREIGN KEY (candidate_id, variant_key)
            REFERENCES turn_terminal_candidate_variants(candidate_id, variant_key) ON DELETE NO ACTION
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_terminal_receipts (
          receipt_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          candidate_id TEXT NOT NULL UNIQUE REFERENCES turn_terminal_candidates(candidate_id) ON DELETE NO ACTION,
          protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
          status TEXT NOT NULL CHECK (status IN ('completed','failed','cancelled','aborted','clarification_pending','approval_pending')),
          reason_code TEXT,
          message_key TEXT NOT NULL,
          final_answer_mode TEXT NOT NULL CHECK (final_answer_mode IN ('none','precommitted','in_terminal_batch')),
          final_answer_event_id TEXT,
          selected_variant_key TEXT NOT NULL,
          selected_outcome_variant TEXT NOT NULL CHECK (selected_outcome_variant IN ('primary','recovery_interrupt')),
          selected_session_variant TEXT NOT NULL CHECK (selected_session_variant IN ('followup','deferred','discard','none')),
          terminal_event_id TEXT NOT NULL UNIQUE,
          first_event_seq INTEGER NOT NULL CHECK (first_event_seq >= 1),
          last_event_seq INTEGER NOT NULL CHECK (last_event_seq >= first_event_seq),
          persisted_event_highwater INTEGER NOT NULL CHECK (persisted_event_highwater >= 0),
          required_event_manifest_json TEXT NOT NULL,
          candidate_projection_digest TEXT NOT NULL,
          selected_projection_digest TEXT NOT NULL,
          selected_manifest_digest TEXT NOT NULL,
          selected_session_mutation_digest TEXT NOT NULL,
          projection_extensions_json TEXT NOT NULL,
          committed_at TEXT NOT NULL,
          UNIQUE (turn_id, generation),
          FOREIGN KEY (turn_id, generation) REFERENCES turn_generations(turn_id, generation) ON DELETE NO ACTION,
          FOREIGN KEY (candidate_id, selected_variant_key)
            REFERENCES turn_terminal_candidate_variants(candidate_id, variant_key) ON DELETE NO ACTION,
          CHECK ((final_answer_mode = 'none' AND final_answer_event_id IS NULL) OR
                 (final_answer_mode != 'none' AND final_answer_event_id IS NOT NULL)),
          CHECK ((status = 'completed' AND final_answer_mode != 'none') OR
                 (status IN ('cancelled','aborted','clarification_pending','approval_pending') AND
                  final_answer_mode = 'none') OR status = 'failed'),
          CHECK (persisted_event_highwater <= last_event_seq)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_terminal_session_mutations (
          receipt_id TEXT PRIMARY KEY REFERENCES turn_terminal_receipts(receipt_id) ON DELETE NO ACTION,
          candidate_id TEXT NOT NULL,
          selected_variant_key TEXT NOT NULL,
          old_active_claim_tuple TEXT NOT NULL,
          disposition TEXT NOT NULL
            CHECK (disposition IN ('followup','deferred','discard','none','control_preserve_unrelated')),
          new_active_claim_tuple TEXT NOT NULL,
          freeze_revision INTEGER NOT NULL CHECK (freeze_revision >= 0),
          buffer_identity_digest TEXT NOT NULL,
          consumed_or_retained_count INTEGER NOT NULL CHECK (consumed_or_retained_count >= 0),
          authz_snapshot_digest_at_commit TEXT NOT NULL,
          writer_admission_epoch_at_commit INTEGER NOT NULL CHECK (writer_admission_epoch_at_commit >= 0),
          mutation_projection_json TEXT NOT NULL,
          mutation_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY (candidate_id, selected_variant_key)
            REFERENCES turn_terminal_candidate_variants(candidate_id, variant_key) ON DELETE NO ACTION
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_event_proposals (
          proposal_id TEXT PRIMARY KEY,
          event_id TEXT NOT NULL UNIQUE,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          accept_ordinal INTEGER NOT NULL CHECK (accept_ordinal >= 1),
          accepted_claim_epoch INTEGER NOT NULL CHECK (accepted_claim_epoch >= 1),
          event_type TEXT NOT NULL,
          actor_scope TEXT NOT NULL CHECK (actor_scope IN ('root','peer','step','transport')),
          phase_scope TEXT NOT NULL CHECK (phase_scope IN ('in_generation','control_transition','post_terminal_auxiliary')),
          payload_json TEXT NOT NULL,
          payload_digest TEXT NOT NULL,
          durability TEXT NOT NULL CHECK (durability IN ('committed','non_durable','unknown')),
          state TEXT NOT NULL CHECK (state IN ('accepted','persisted')),
          accepted_at TEXT NOT NULL,
          persisted_at TEXT,
          persisted_event_seq INTEGER,
          UNIQUE (turn_id, generation, accept_ordinal),
          FOREIGN KEY (turn_id, generation) REFERENCES turn_generations(turn_id, generation) ON DELETE NO ACTION,
          CHECK ((state = 'accepted' AND persisted_at IS NULL AND persisted_event_seq IS NULL) OR
                 (state = 'persisted' AND persisted_at IS NOT NULL AND persisted_event_seq >= 1))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_control_decisions (
          decision_id TEXT PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_id TEXT NOT NULL,
          turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE NO ACTION,
          prior_generation INTEGER NOT NULL CHECK (prior_generation >= 1),
          action TEXT NOT NULL,
          decision_digest TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('pending','decision_recorded','generation_opened','terminal_control_committed')),
          target_generation INTEGER,
          actor_ref TEXT NOT NULL,
          authz_snapshot_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          recorded_at TEXT,
          consumed_at TEXT,
          UNIQUE (source_kind, source_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_recovery_records (
          recovery_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          scope TEXT NOT NULL CHECK (scope IN ('terminal','delivery')),
          scope_ref TEXT,
          kind TEXT NOT NULL CHECK (kind IN ('opened_owner_lost','executing_owner_lost','finalization_unknown','non_durable','delivery_pending','effect_unknown','dependency_missing')),
          phase TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('active','resolved')),
          candidate_id TEXT,
          receipt_id TEXT,
          resolution TEXT CHECK (resolution IN ('receipt_committed','fenced_deleted')),
          discovery_identity TEXT,
          discovered_generation_row_version INTEGER NOT NULL CHECK (discovered_generation_row_version >= 1),
          recovery_owner_instance_id TEXT,
          recovery_claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK (recovery_claim_epoch >= 0),
          recovery_lease_expires_at TEXT,
          recovery_heartbeat_at TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          next_attempt_at TEXT,
          last_code TEXT,
          created_at TEXT NOT NULL,
          resolved_at TEXT,
          FOREIGN KEY (turn_id, generation) REFERENCES turn_generations(turn_id, generation) ON DELETE NO ACTION,
          CHECK ((scope = 'terminal' AND scope_ref IS NULL) OR
                 (scope = 'delivery' AND scope_ref IS NOT NULL AND length(scope_ref) > 0)),
          CHECK ((state = 'active' AND resolved_at IS NULL) OR (state = 'resolved' AND resolved_at IS NOT NULL))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_effect_evidence_links (
          evidence_link_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          audit_object_ref TEXT,
          evidence_digest TEXT NOT NULL,
          object_version_digest TEXT NOT NULL,
          retention_state TEXT NOT NULL CHECK (retention_state IN ('active','purge_pending','purged')),
          purge_request_id TEXT,
          purge_owner TEXT,
          purge_lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK (purge_lease_epoch >= 0),
          purge_lease_expires_at TEXT,
          delete_idempotency_key TEXT,
          delete_outcome_digest TEXT,
          purge_after TEXT NOT NULL,
          purged_at TEXT,
          created_at TEXT NOT NULL,
          CHECK (retention_state != 'purged' OR (audit_object_ref IS NULL AND purged_at IS NOT NULL))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_effect_attempts (
          attempt_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          claim_epoch INTEGER NOT NULL CHECK (claim_epoch >= 1),
          effect_class TEXT NOT NULL CHECK (effect_class IN ('none','read_only','side_effect_possible')),
          provider TEXT NOT NULL,
          action_ref TEXT NOT NULL,
          idempotency_or_fence_token TEXT,
          non_retriable INTEGER NOT NULL CHECK (non_retriable IN (0,1)),
          state TEXT NOT NULL CHECK (state IN ('intent','dispatched','settled','unknown')),
          row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
          started_at TEXT NOT NULL,
          settled_at TEXT,
          result_digest TEXT,
          settlement_id TEXT UNIQUE,
          retention_state TEXT NOT NULL DEFAULT 'active' CHECK (retention_state IN ('active','aggregate_pending')),
          FOREIGN KEY (turn_id, generation) REFERENCES turn_generations(turn_id, generation) ON DELETE NO ACTION,
          FOREIGN KEY (settlement_id) REFERENCES turn_effect_settlements(settlement_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
          UNIQUE (turn_id, generation, action_ref),
          CHECK (idempotency_or_fence_token IS NOT NULL OR non_retriable = 1),
          CHECK ((state = 'settled' AND settlement_id IS NOT NULL AND settled_at IS NOT NULL) OR
                 (state != 'settled' AND settlement_id IS NULL AND settled_at IS NULL))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_effect_settlements (
          settlement_id TEXT PRIMARY KEY,
          attempt_id TEXT NOT NULL UNIQUE,
          source TEXT NOT NULL CHECK (source IN ('provider','manual')),
          outcome TEXT NOT NULL CHECK (outcome IN ('confirmed_applied','confirmed_not_applied','compensated')),
          settlement_code TEXT NOT NULL,
          actor_audit_ref TEXT NOT NULL,
          authz_snapshot_digest TEXT NOT NULL,
          provider_external_ref_digest TEXT,
          provider_external_ref_key_version TEXT,
          evidence_link_id TEXT NOT NULL UNIQUE,
          evidence_digest TEXT NOT NULL,
          evidence_recorded_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY (attempt_id) REFERENCES turn_effect_attempts(attempt_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
          FOREIGN KEY (evidence_link_id) REFERENCES turn_effect_evidence_links(evidence_link_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_effect_retention_tombstones (
          tombstone_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          effect_bundle_digest TEXT NOT NULL,
          audit_key_version TEXT NOT NULL,
          attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
          provider_token_digests_digest TEXT NOT NULL,
          settlement_outcomes_digest TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('aggregate_pending','aggregated')),
          eligible_at TEXT NOT NULL,
          provider_dedup_horizon_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
          created_at TEXT NOT NULL,
          aggregated_at TEXT,
          UNIQUE (team_id, turn_id, generation, effect_bundle_digest),
          UNIQUE (tombstone_id, team_id),
          CHECK ((state = 'aggregate_pending' AND aggregated_at IS NULL) OR
                 (state = 'aggregated' AND aggregated_at IS NOT NULL))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_effect_retention_member_keys (
          member_key_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          attempt_identity_digest TEXT NOT NULL,
          provider_token_digest TEXT,
          audit_key_version TEXT NOT NULL,
          tombstone_id TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          FOREIGN KEY (tombstone_id, team_id)
            REFERENCES turn_effect_retention_tombstones(tombstone_id, team_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_terminal_deletion_ledgers (
          deletion_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL,
          generation INTEGER NOT NULL CHECK (generation >= 1),
          prior_generation_state TEXT NOT NULL
            CHECK (prior_generation_state IN ('opened','running','closing','recovery_pending','closed')),
          prior_row_version INTEGER NOT NULL CHECK (prior_row_version >= 1),
          prior_execution_claim_epoch INTEGER NOT NULL CHECK (prior_execution_claim_epoch >= 0),
          prior_finalization_epoch INTEGER NOT NULL CHECK (prior_finalization_epoch >= 0),
          candidate_id TEXT,
          receipt_id TEXT,
          old_session_active_tuple TEXT NOT NULL,
          old_session_recovery_tuple TEXT NOT NULL,
          new_delivery_fence_epoch INTEGER NOT NULL CHECK (new_delivery_fence_epoch >= 1),
          unsettled_effect_count INTEGER NOT NULL CHECK (unsettled_effect_count >= 0),
          effect_identity_digest TEXT NOT NULL,
          effect_bundle_digest TEXT NOT NULL,
          effect_audit_key_version TEXT NOT NULL,
          effect_key_required_until TEXT NOT NULL,
          resolution TEXT NOT NULL CHECK (resolution = 'fenced_deleted'),
          actor_audit_ref TEXT NOT NULL,
          authz_snapshot_digest TEXT NOT NULL,
          policy_code TEXT NOT NULL,
          created_at TEXT NOT NULL,
          deletion_digest TEXT NOT NULL,
          UNIQUE (turn_id, generation),
          FOREIGN KEY (turn_id, generation) REFERENCES turn_generations(turn_id, generation) ON DELETE NO ACTION
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS session_lifecycle_operations (
          operation_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          session_audit_ref TEXT,
          account_audit_ref TEXT,
          parent_operation_id TEXT,
          request_id TEXT NOT NULL,
          action TEXT NOT NULL CHECK (action IN ('soft_delete','hard_delete','revive','account_hard_delete')),
          state TEXT NOT NULL CHECK (state IN ('prepared','fenced','materializing','processing_targets','protocol_settled','content_detached','pending_delivery','committed','blocked','cancelled','conflict')),
          expected_deleted_state TEXT,
          new_deleted_state TEXT,
          expected_session_visibility_epoch INTEGER,
          new_session_visibility_epoch INTEGER,
          target_session_count INTEGER CHECK (target_session_count >= 0),
          target_sessions_digest TEXT,
          materialization_cursor_team_id TEXT,
          materialization_cursor_session_id TEXT,
          next_child_ordinal INTEGER NOT NULL DEFAULT 1 CHECK (next_child_ordinal >= 1),
          prior_detached_session_count INTEGER NOT NULL DEFAULT 0 CHECK (prior_detached_session_count >= 0),
          prior_detached_operations_digest TEXT,
          account_fence_epoch INTEGER,
          account_fence_committed_at TEXT,
          protocol_delete_identities_digest TEXT,
          result_code TEXT,
          row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
          actor_audit_ref TEXT NOT NULL,
          operation_owner_instance_id TEXT,
          operation_claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK (operation_claim_epoch >= 0),
          operation_lease_expires_at TEXT,
          operation_heartbeat_at TEXT,
          next_attempt_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          committed_at TEXT,
          FOREIGN KEY (parent_operation_id) REFERENCES session_lifecycle_operations(operation_id) ON DELETE NO ACTION,
          CHECK ((action = 'account_hard_delete' AND account_audit_ref IS NOT NULL) OR
                 (action != 'account_hard_delete' AND session_audit_ref IS NOT NULL)),
          CHECK (action = 'account_hard_delete' OR state NOT IN ('materializing','processing_targets')),
          CHECK (account_fence_committed_at IS NULL OR action = 'account_hard_delete')
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS session_lifecycle_targets (
          target_id TEXT PRIMARY KEY,
          parent_operation_id TEXT NOT NULL REFERENCES session_lifecycle_operations(operation_id) ON DELETE NO ACTION,
          child_ordinal INTEGER NOT NULL CHECK (child_ordinal >= 1),
          team_id TEXT NOT NULL,
          session_audit_ref TEXT NOT NULL,
          required_action TEXT NOT NULL CHECK (required_action = 'hard_delete'),
          dependency_operation_id TEXT REFERENCES session_lifecycle_operations(operation_id) ON DELETE NO ACTION,
          bound_operation_id TEXT REFERENCES session_lifecycle_operations(operation_id) ON DELETE NO ACTION,
          state TEXT NOT NULL CHECK (state IN ('materialized','waiting_dependency','operation_bound','committed','conflict')),
          discovery_source_kind TEXT NOT NULL CHECK (discovery_source_kind IN ('session_mapping','lifecycle_operation')),
          source_operation_id TEXT REFERENCES session_lifecycle_operations(operation_id) ON DELETE NO ACTION,
          expected_session_audit_digest TEXT NOT NULL,
          session_audit_key_version TEXT NOT NULL,
          row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          committed_at TEXT,
          UNIQUE (parent_operation_id, child_ordinal),
          UNIQUE (parent_operation_id, team_id, session_audit_ref),
          CHECK ((discovery_source_kind = 'session_mapping' AND source_operation_id IS NULL) OR
                 (discovery_source_kind = 'lifecycle_operation' AND source_operation_id IS NOT NULL))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS terminal_auth_scope_epochs (
          scope_kind TEXT NOT NULL CHECK (scope_kind IN ('team_membership','resource_grant','session_visibility','user_account')),
          team_id TEXT NOT NULL,
          scope_ref TEXT NOT NULL,
          epoch INTEGER NOT NULL CHECK (epoch >= 1),
          row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
          updated_at TEXT NOT NULL,
          PRIMARY KEY (scope_kind, team_id, scope_ref),
          CHECK ((scope_kind = 'user_account' AND team_id = '__global__') OR
                 (scope_kind != 'user_account' AND team_id != '__global__'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS terminal_protocol_deployment_attestations (
          attestation_id TEXT PRIMARY KEY,
          deployment_id TEXT NOT NULL,
          expected_instance_count INTEGER NOT NULL CHECK (expected_instance_count >= 1),
          reader_capable_instance_count INTEGER NOT NULL CHECK (reader_capable_instance_count >= 0),
          old_writer_instance_count INTEGER NOT NULL CHECK (old_writer_instance_count >= 0),
          certifier_actor_ref TEXT NOT NULL,
          certified_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          evidence_digest TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_delivery_intents (
          intent_id TEXT PRIMARY KEY,
          receipt_id TEXT NOT NULL REFERENCES turn_terminal_receipts(receipt_id) ON DELETE NO ACTION,
          surface TEXT NOT NULL,
          binding_ref TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('pending','leased','sending','delivered','unknown','blocked','cancelled')),
          lease_owner TEXT,
          lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
          lease_expires_at TEXT,
          expected_delivery_fence_epoch INTEGER NOT NULL CHECK (expected_delivery_fence_epoch >= 0),
          expected_auth_fence_vector_digest TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          idempotency_key TEXT NOT NULL,
          created_at TEXT NOT NULL,
          delivered_at TEXT,
          UNIQUE (receipt_id, surface, binding_ref),
          CHECK (status != 'delivered' OR delivered_at IS NOT NULL)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS terminal_protocol_writer_lease (
          lease_name TEXT PRIMARY KEY CHECK (lease_name = 'v1_writer'),
          owner_instance_id TEXT,
          epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
          admission_state TEXT NOT NULL CHECK (admission_state IN ('open','draining','closed')),
          admission_epoch INTEGER NOT NULL DEFAULT 0 CHECK (admission_epoch >= 0),
          rollout_config_generation TEXT NOT NULL,
          deployment_attestation_id TEXT REFERENCES terminal_protocol_deployment_attestations(attestation_id) ON DELETE NO ACTION,
          lease_expires_at TEXT,
          heartbeat_at TEXT
        )
        """,
    )
    _execute_all(connection, statements)


def _create_protocol_indexes(connection) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_turn_generations_discovery ON turn_generations(state, lease_expires_at, opened_at) WHERE protocol_version = 1 AND state IN ('opened','running') AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_turn_generations_finalization ON turn_generations(state, finalization_lease_expires_at) WHERE protocol_version = 1 AND state = 'closing' AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_terminal_candidates_turn ON turn_terminal_candidates(turn_id, generation, state)",
        "CREATE INDEX IF NOT EXISTS idx_terminal_candidate_events_identity ON turn_terminal_candidate_events(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_terminal_receipts_turn ON turn_terminal_receipts(turn_id, generation)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_events_v1_sequence ON turn_events(turn_id, event_seq) WHERE protocol_version = 1",
        "CREATE INDEX IF NOT EXISTS idx_turn_events_v1_generation ON turn_events(turn_id, generation, event_seq) WHERE protocol_version = 1",
        "CREATE INDEX IF NOT EXISTS idx_turn_events_v1_receipt ON turn_events(terminal_receipt_id, event_seq) WHERE terminal_receipt_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_discovery_identity ON turn_recovery_records(scope, discovery_identity) WHERE discovery_identity IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_active_terminal ON turn_recovery_records(turn_id, generation) WHERE scope = 'terminal' AND state = 'active'",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_active_delivery ON turn_recovery_records(scope_ref) WHERE scope = 'delivery' AND state = 'active'",
        "CREATE INDEX IF NOT EXISTS idx_recovery_due ON turn_recovery_records(scope, state, next_attempt_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_effect_provider_token ON turn_effect_attempts(provider, idempotency_or_fence_token) WHERE idempotency_or_fence_token IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_object_identity ON turn_effect_evidence_links(team_id, audit_object_ref, object_version_digest) WHERE audit_object_ref IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_effect_member_attempt ON turn_effect_retention_member_keys(team_id, attempt_identity_digest, audit_key_version)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_effect_member_provider_token ON turn_effect_retention_member_keys(team_id, provider_token_digest, audit_key_version) WHERE provider_token_digest IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_active_session ON session_lifecycle_operations(team_id, session_audit_ref) WHERE session_audit_ref IS NOT NULL AND state IN ('prepared','fenced','materializing','processing_targets','protocol_settled','content_detached','pending_delivery','blocked')",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_active_account ON session_lifecycle_operations(account_audit_ref) WHERE action = 'account_hard_delete' AND state NOT IN ('committed','cancelled','conflict')",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_request_session ON session_lifecycle_operations(team_id, session_audit_ref, actor_audit_ref, request_id, action) WHERE session_audit_ref IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_request_account ON session_lifecycle_operations(team_id, account_audit_ref, actor_audit_ref, request_id, action) WHERE action = 'account_hard_delete'",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_target_bound_operation ON session_lifecycle_targets(bound_operation_id) WHERE bound_operation_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_lifecycle_targets_parent ON session_lifecycle_targets(parent_operation_id, child_ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_delivery_due ON turn_delivery_intents(status, lease_expires_at, created_at)",
    )
    _execute_all(connection, statements)


def _create_protocol_triggers(connection) -> None:
    statements = (
        """
        CREATE TRIGGER IF NOT EXISTS turns_terminal_protocol_immutable
        BEFORE UPDATE OF terminal_protocol_version, terminal_ingress_surface,
          terminal_admission_cohort, protocol_admitted_at, presentation_locale,
          message_catalog_version ON turns
        WHEN OLD.terminal_protocol_version = 1 AND (
          NEW.terminal_protocol_version IS NOT OLD.terminal_protocol_version OR
          NEW.terminal_ingress_surface IS NOT OLD.terminal_ingress_surface OR
          NEW.terminal_admission_cohort IS NOT OLD.terminal_admission_cohort OR
          NEW.protocol_admitted_at IS NOT OLD.protocol_admitted_at OR
          NEW.presentation_locale IS NOT OLD.presentation_locale OR
          NEW.message_catalog_version IS NOT OLD.message_catalog_version
        )
        BEGIN
          SELECT RAISE(ABORT, 'terminal protocol admission snapshot is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turns_terminal_protocol_monotonic_update
        BEFORE UPDATE OF current_generation, generation_transition_epoch, last_terminal_generation,
          event_sequence_next, delivery_fence_epoch ON turns
        WHEN OLD.terminal_protocol_version = 1 AND (
          NEW.current_generation < OLD.current_generation OR
          NEW.event_sequence_next < OLD.event_sequence_next OR
          NEW.delivery_fence_epoch < OLD.delivery_fence_epoch OR
          (OLD.last_terminal_generation IS NOT NULL AND
           (NEW.last_terminal_generation IS NULL OR NEW.last_terminal_generation < OLD.last_terminal_generation)) OR
          (NEW.current_generation = OLD.current_generation AND
           NEW.generation_transition_epoch != OLD.generation_transition_epoch) OR
          (NEW.current_generation = OLD.current_generation + 1 AND
           NEW.generation_transition_epoch != OLD.generation_transition_epoch + 1) OR
          NEW.current_generation > OLD.current_generation + 1
        )
        BEGIN
          SELECT RAISE(ABORT, 'terminal protocol turn fences must be monotonic');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turns_terminal_receipt_reference_update
        BEFORE UPDATE OF current_generation, last_terminal_generation, current_terminal_receipt_id ON turns
        WHEN NEW.terminal_protocol_version = 1 AND NOT EXISTS (
          SELECT 1 FROM turn_generations g
          LEFT JOIN turn_terminal_receipts r ON r.receipt_id = NEW.current_terminal_receipt_id
          WHERE g.turn_id = NEW.id AND g.generation = NEW.current_generation AND g.protocol_version = 1
            AND (
              (g.state != 'closed' AND NEW.current_terminal_receipt_id IS NULL) OR
              (g.state = 'closed' AND g.recovery_resolution = 'fenced_deleted'
               AND g.terminal_receipt_id IS NULL AND NEW.current_terminal_receipt_id IS NULL) OR
              (g.state = 'closed' AND g.terminal_receipt_id = NEW.current_terminal_receipt_id
               AND r.turn_id = NEW.id AND r.generation = NEW.current_generation AND r.protocol_version = 1
               AND NEW.last_terminal_generation = NEW.current_generation)
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'terminal receipt pointer does not match current generation');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turns_terminal_protocol_no_legacy_upgrade
        BEFORE UPDATE OF terminal_protocol_version ON turns
        WHEN OLD.terminal_protocol_version IS NULL AND NEW.terminal_protocol_version IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, 'legacy turn cannot be upgraded in place');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turns_terminal_protocol_shape_insert
        BEFORE INSERT ON turns
        WHEN NEW.terminal_protocol_version = 1 AND (
          NEW.terminal_ingress_surface IS NULL OR length(NEW.terminal_ingress_surface) = 0 OR
          length(NEW.terminal_ingress_surface) > 64 OR
          NEW.terminal_admission_cohort IS NULL OR length(NEW.terminal_admission_cohort) = 0 OR
          length(NEW.terminal_admission_cohort) > 256 OR
          NEW.protocol_admitted_at IS NULL OR length(NEW.protocol_admitted_at) = 0 OR
          NEW.current_generation IS NULL OR NEW.current_generation < 1 OR
          NEW.generation_transition_epoch IS NULL OR NEW.generation_transition_epoch < 1 OR
          NEW.event_sequence_next IS NULL OR NEW.event_sequence_next < 1 OR
          NEW.delivery_fence_epoch IS NULL OR NEW.delivery_fence_epoch < 0 OR NEW.presentation_locale IS NULL OR
          length(NEW.presentation_locale) = 0 OR length(NEW.presentation_locale) > 35 OR
          NEW.message_catalog_version IS NULL OR NEW.message_catalog_version < 1 OR
          (NEW.last_terminal_generation IS NOT NULL AND NEW.last_terminal_generation > NEW.current_generation)
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed terminal protocol turn');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turns_terminal_protocol_shape_update
        BEFORE UPDATE ON turns
        WHEN NEW.terminal_protocol_version = 1 AND (
          NEW.terminal_ingress_surface IS NULL OR length(NEW.terminal_ingress_surface) = 0 OR
          length(NEW.terminal_ingress_surface) > 64 OR
          NEW.terminal_admission_cohort IS NULL OR length(NEW.terminal_admission_cohort) = 0 OR
          length(NEW.terminal_admission_cohort) > 256 OR
          NEW.protocol_admitted_at IS NULL OR length(NEW.protocol_admitted_at) = 0 OR
          NEW.current_generation IS NULL OR NEW.current_generation < 1 OR
          NEW.generation_transition_epoch IS NULL OR NEW.generation_transition_epoch < 1 OR
          NEW.event_sequence_next IS NULL OR NEW.event_sequence_next < 1 OR
          NEW.delivery_fence_epoch IS NULL OR NEW.delivery_fence_epoch < 0 OR NEW.presentation_locale IS NULL OR
          length(NEW.presentation_locale) = 0 OR length(NEW.presentation_locale) > 35 OR
          NEW.message_catalog_version IS NULL OR NEW.message_catalog_version < 1 OR
          (NEW.last_terminal_generation IS NOT NULL AND NEW.last_terminal_generation > NEW.current_generation)
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed terminal protocol turn');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_active_shape_insert
        BEFORE INSERT ON sessions
        WHEN (
          (NEW.active_turn_id IS NULL AND (NEW.active_turn_protocol_version IS NOT NULL OR
           NEW.active_turn_generation IS NOT NULL OR NEW.active_turn_claim_epoch IS NOT NULL)) OR
          (NEW.active_turn_id IS NOT NULL AND NEW.active_turn_protocol_version IS NULL AND
           (NEW.active_turn_generation IS NOT NULL OR NEW.active_turn_claim_epoch IS NOT NULL)) OR
          (NEW.active_turn_id IS NOT NULL AND NEW.active_turn_protocol_version = 1 AND
           (NEW.active_turn_generation IS NULL OR NEW.active_turn_generation < 1 OR
            NEW.active_turn_claim_epoch IS NULL OR NEW.active_turn_claim_epoch < 0)) OR
          (NEW.active_turn_id IS NOT NULL AND NEW.active_turn_protocol_version IS NOT NULL AND
           NEW.active_turn_protocol_version != 1)
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed session active turn shape');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_active_shape_update
        BEFORE UPDATE OF active_turn_id, active_turn_protocol_version,
          active_turn_generation, active_turn_claim_epoch ON sessions
        WHEN (
          (NEW.active_turn_id IS NULL AND (NEW.active_turn_protocol_version IS NOT NULL OR
           NEW.active_turn_generation IS NOT NULL OR NEW.active_turn_claim_epoch IS NOT NULL)) OR
          (NEW.active_turn_id IS NOT NULL AND NEW.active_turn_protocol_version IS NULL AND
           (NEW.active_turn_generation IS NOT NULL OR NEW.active_turn_claim_epoch IS NOT NULL)) OR
          (NEW.active_turn_id IS NOT NULL AND NEW.active_turn_protocol_version = 1 AND
           (NEW.active_turn_generation IS NULL OR NEW.active_turn_generation < 1 OR
            NEW.active_turn_claim_epoch IS NULL OR NEW.active_turn_claim_epoch < 0)) OR
          (NEW.active_turn_id IS NOT NULL AND NEW.active_turn_protocol_version IS NOT NULL AND
           NEW.active_turn_protocol_version != 1)
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed session active turn shape');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_recovery_shape_insert
        BEFORE INSERT ON sessions
        WHEN (
          (NEW.recovery_lock_turn_id IS NULL AND
           (NEW.recovery_lock_generation IS NOT NULL OR NEW.recovery_lock_reason IS NOT NULL)) OR
          (NEW.recovery_lock_turn_id IS NOT NULL AND
           (NEW.recovery_lock_generation IS NULL OR NEW.recovery_lock_generation < 1 OR NEW.recovery_lock_reason IS NULL))
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed session recovery lock');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_recovery_shape_update
        BEFORE UPDATE OF recovery_lock_turn_id, recovery_lock_generation, recovery_lock_reason ON sessions
        WHEN (
          (NEW.recovery_lock_turn_id IS NULL AND
           (NEW.recovery_lock_generation IS NOT NULL OR NEW.recovery_lock_reason IS NOT NULL)) OR
          (NEW.recovery_lock_turn_id IS NOT NULL AND
           (NEW.recovery_lock_generation IS NULL OR NEW.recovery_lock_generation < 1 OR NEW.recovery_lock_reason IS NULL))
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed session recovery lock');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_active_reference_insert
        BEFORE INSERT ON sessions
        WHEN NEW.active_turn_id IS NOT NULL AND (
          (EXISTS (SELECT 1 FROM turns t WHERE t.id = NEW.active_turn_id AND t.terminal_protocol_version = 1) AND
           NOT EXISTS (
             SELECT 1 FROM turns t
             JOIN turn_generations g ON g.turn_id = t.id AND g.generation = t.current_generation
             WHERE t.id = NEW.active_turn_id AND t.session_id = NEW.id AND t.terminal_protocol_version = 1
               AND NEW.active_turn_protocol_version = 1
               AND NEW.active_turn_generation = t.current_generation
               AND NEW.active_turn_claim_epoch = g.claim_epoch
           )) OR
          (NEW.active_turn_protocol_version = 1 AND NOT EXISTS (
             SELECT 1 FROM turns t
             JOIN turn_generations g ON g.turn_id = t.id AND g.generation = t.current_generation
             WHERE t.id = NEW.active_turn_id AND t.session_id = NEW.id AND t.terminal_protocol_version = 1
               AND NEW.active_turn_generation = t.current_generation
               AND NEW.active_turn_claim_epoch = g.claim_epoch
           ))
        )
        BEGIN
          SELECT RAISE(ABORT, 'session active turn protocol reference mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_active_epoch_monotonic
        BEFORE UPDATE OF active_turn_id, active_turn_protocol_version,
          active_turn_generation, active_turn_claim_epoch ON sessions
        WHEN OLD.active_turn_protocol_version = 1 AND NEW.active_turn_protocol_version = 1
          AND OLD.active_turn_id = NEW.active_turn_id
          AND OLD.active_turn_generation = NEW.active_turn_generation
          AND NEW.active_turn_claim_epoch < OLD.active_turn_claim_epoch
        BEGIN
          SELECT RAISE(ABORT, 'session active claim epoch must be monotonic');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_active_reference_update
        BEFORE UPDATE OF active_turn_id, active_turn_protocol_version,
          active_turn_generation, active_turn_claim_epoch ON sessions
        WHEN NEW.active_turn_id IS NOT NULL AND (
          (EXISTS (SELECT 1 FROM turns t WHERE t.id = NEW.active_turn_id AND t.terminal_protocol_version = 1) AND
           NOT EXISTS (
             SELECT 1 FROM turns t
             JOIN turn_generations g ON g.turn_id = t.id AND g.generation = t.current_generation
             WHERE t.id = NEW.active_turn_id AND t.session_id = NEW.id AND t.terminal_protocol_version = 1
               AND NEW.active_turn_protocol_version = 1
               AND NEW.active_turn_generation = t.current_generation
               AND NEW.active_turn_claim_epoch = g.claim_epoch
           )) OR
          (NEW.active_turn_protocol_version = 1 AND NOT EXISTS (
             SELECT 1 FROM turns t
             JOIN turn_generations g ON g.turn_id = t.id AND g.generation = t.current_generation
             WHERE t.id = NEW.active_turn_id AND t.session_id = NEW.id AND t.terminal_protocol_version = 1
               AND NEW.active_turn_generation = t.current_generation
               AND NEW.active_turn_claim_epoch = g.claim_epoch
           ))
        )
        BEGIN
          SELECT RAISE(ABORT, 'session active turn protocol reference mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_recovery_reference_insert
        BEFORE INSERT ON sessions
        WHEN NEW.recovery_lock_turn_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM turns t
          JOIN turn_recovery_records rr
            ON rr.turn_id = t.id AND rr.generation = NEW.recovery_lock_generation
           AND rr.scope = 'terminal' AND rr.state = 'active'
          WHERE t.id = NEW.recovery_lock_turn_id AND t.session_id = NEW.id
            AND t.terminal_protocol_version = 1 AND t.current_generation = NEW.recovery_lock_generation
        )
        BEGIN
          SELECT RAISE(ABORT, 'session recovery lock protocol reference mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_recovery_reference_update
        BEFORE UPDATE OF recovery_lock_turn_id, recovery_lock_generation, recovery_lock_reason ON sessions
        WHEN NEW.recovery_lock_turn_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM turns t
          JOIN turn_recovery_records rr
            ON rr.turn_id = t.id AND rr.generation = NEW.recovery_lock_generation
           AND rr.scope = 'terminal' AND rr.state = 'active'
          WHERE t.id = NEW.recovery_lock_turn_id AND t.session_id = NEW.id
            AND t.terminal_protocol_version = 1 AND t.current_generation = NEW.recovery_lock_generation
        )
        BEGIN
          SELECT RAISE(ABORT, 'session recovery lock protocol reference mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_steer_freeze_shape_insert
        BEFORE INSERT ON sessions
        WHEN (
          (NEW.steer_buffer_state = 'frozen' AND
           (NEW.steer_freeze_turn_id IS NULL OR NEW.steer_freeze_generation IS NULL OR
            NEW.steer_freeze_generation < 1 OR NEW.steer_freeze_claim_epoch IS NULL OR
            NEW.steer_freeze_claim_epoch < 0 OR NEW.steer_frozen_revision IS NULL OR
            NEW.steer_frozen_revision < 0 OR NEW.steer_frozen_revision > NEW.steer_buffer_revision)) OR
          (NEW.steer_buffer_state != 'frozen' AND
           (NEW.steer_freeze_turn_id IS NOT NULL OR NEW.steer_freeze_generation IS NOT NULL OR
            NEW.steer_freeze_claim_epoch IS NOT NULL OR NEW.steer_frozen_revision IS NOT NULL))
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed session steer freeze');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_steer_freeze_shape_update
        BEFORE UPDATE OF steer_buffer_revision, steer_freeze_turn_id, steer_freeze_generation,
          steer_freeze_claim_epoch, steer_frozen_revision, steer_buffer_state ON sessions
        WHEN NEW.steer_buffer_revision < OLD.steer_buffer_revision OR (
          (NEW.steer_buffer_state = 'frozen' AND
           (NEW.steer_freeze_turn_id IS NULL OR NEW.steer_freeze_generation IS NULL OR
            NEW.steer_freeze_generation < 1 OR NEW.steer_freeze_claim_epoch IS NULL OR
            NEW.steer_freeze_claim_epoch < 0 OR NEW.steer_frozen_revision IS NULL OR
            NEW.steer_frozen_revision < 0 OR NEW.steer_frozen_revision > NEW.steer_buffer_revision)) OR
          (NEW.steer_buffer_state != 'frozen' AND
           (NEW.steer_freeze_turn_id IS NOT NULL OR NEW.steer_freeze_generation IS NOT NULL OR
            NEW.steer_freeze_claim_epoch IS NOT NULL OR NEW.steer_frozen_revision IS NOT NULL))
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed session steer freeze');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_steer_freeze_reference_insert
        BEFORE INSERT ON sessions
        WHEN NEW.steer_buffer_state = 'frozen' AND NOT EXISTS (
          SELECT 1 FROM turns t
          JOIN turn_generations g ON g.turn_id = t.id AND g.generation = NEW.steer_freeze_generation
          WHERE t.id = NEW.steer_freeze_turn_id AND t.session_id = NEW.id
            AND t.terminal_protocol_version = 1 AND g.claim_epoch = NEW.steer_freeze_claim_epoch
        )
        BEGIN
          SELECT RAISE(ABORT, 'session steer freeze protocol reference mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sessions_terminal_steer_freeze_reference_update
        BEFORE UPDATE OF steer_freeze_turn_id, steer_freeze_generation,
          steer_freeze_claim_epoch, steer_buffer_state ON sessions
        WHEN NEW.steer_buffer_state = 'frozen' AND NOT EXISTS (
          SELECT 1 FROM turns t
          JOIN turn_generations g ON g.turn_id = t.id AND g.generation = NEW.steer_freeze_generation
          WHERE t.id = NEW.steer_freeze_turn_id AND t.session_id = NEW.id
            AND t.terminal_protocol_version = 1 AND g.claim_epoch = NEW.steer_freeze_claim_epoch
        )
        BEGIN
          SELECT RAISE(ABORT, 'session steer freeze protocol reference mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turn_events_terminal_protocol_shape_insert
        BEFORE INSERT ON turn_events
        WHEN NEW.protocol_version = 1 AND (
          NEW.generation IS NULL OR NEW.generation < 1 OR NEW.event_seq IS NULL OR NEW.event_seq < 1 OR
          NEW.actor_scope IS NULL OR NEW.actor_scope NOT IN ('root','peer','step','transport') OR
          NEW.phase_scope IS NULL OR NEW.phase_scope NOT IN ('in_generation','control_transition','post_terminal_auxiliary') OR
          NEW.payload_digest IS NULL OR length(NEW.payload_digest) = 0 OR
          (NEW.phase_scope = 'in_generation' AND NEW.terminal_receipt_id IS NULL AND
           (NEW.source_proposal_id IS NULL OR NEW.accept_ordinal IS NULL OR NEW.accept_ordinal < 1 OR
            NOT EXISTS (
              SELECT 1 FROM turn_event_proposals p
              WHERE p.proposal_id = NEW.source_proposal_id AND p.event_id = NEW.id
                AND p.turn_id = NEW.turn_id AND p.generation = NEW.generation
                AND p.accept_ordinal = NEW.accept_ordinal
            )))
        )
        BEGIN
          SELECT RAISE(ABORT, 'malformed terminal protocol event');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turn_events_terminal_protocol_shape_update
        BEFORE UPDATE ON turn_events
        WHEN OLD.protocol_version = 1 OR NEW.protocol_version = 1
        BEGIN
          SELECT RAISE(ABORT, 'terminal protocol events are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_candidates_immutable
        BEFORE UPDATE ON turn_terminal_candidates
        WHEN NEW.candidate_id IS NOT OLD.candidate_id
          OR NEW.turn_id IS NOT OLD.turn_id
          OR NEW.generation IS NOT OLD.generation
          OR NEW.candidate_origin IS NOT OLD.candidate_origin
          OR NEW.reserved_receipt_id IS NOT OLD.reserved_receipt_id
          OR NEW.reserved_terminal_event_id IS NOT OLD.reserved_terminal_event_id
          OR NEW.prepared_execution_claim_epoch IS NOT OLD.prepared_execution_claim_epoch
          OR NEW.finalization_epoch_at_prepare IS NOT OLD.finalization_epoch_at_prepare
          OR NEW.followup_admission_epoch_at_prepare IS NOT OLD.followup_admission_epoch_at_prepare
          OR NEW.primary_status IS NOT OLD.primary_status
          OR NEW.primary_reason_code IS NOT OLD.primary_reason_code
          OR NEW.primary_message_key IS NOT OLD.primary_message_key
          OR NEW.closing_accepted_through IS NOT OLD.closing_accepted_through
          OR NEW.persisted_event_highwater IS NOT OLD.persisted_event_highwater
          OR NEW.terminal_event_counts_by_variant_json IS NOT OLD.terminal_event_counts_by_variant_json
          OR NEW.candidate_projection_digest IS NOT OLD.candidate_projection_digest
          OR NEW.canonicalization_version IS NOT OLD.canonicalization_version
          OR NEW.session_disposition_plan_json IS NOT OLD.session_disposition_plan_json
          OR NEW.session_disposition_plan_digest IS NOT OLD.session_disposition_plan_digest
          OR NEW.projection_variant_digests_json IS NOT OLD.projection_variant_digests_json
          OR NEW.projection_extensions_json IS NOT OLD.projection_extensions_json
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.prepared_at IS NOT OLD.prepared_at
        BEGIN
          SELECT RAISE(ABORT, 'terminal candidate projection is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_candidate_state_transition
        BEFORE UPDATE OF state, committed_at ON turn_terminal_candidates
        WHEN NOT (
          (OLD.state = 'prepared' AND NEW.state IN ('prepared','committed','fenced_deleted')) OR
          (OLD.state = 'committed' AND NEW.state IN ('committed','fenced_deleted')) OR
          (OLD.state = 'fenced_deleted' AND NEW.state = 'fenced_deleted')
        ) OR
        (NEW.state = 'committed' AND NEW.committed_at IS NULL) OR
        (NEW.state = 'prepared' AND NEW.committed_at IS NOT NULL) OR
        (OLD.state = 'prepared' AND NEW.state = 'fenced_deleted' AND NEW.committed_at IS NOT NULL) OR
        (OLD.committed_at IS NOT NULL AND NEW.committed_at IS NOT OLD.committed_at)
        BEGIN
          SELECT RAISE(ABORT, 'invalid terminal candidate state transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_candidate_variants_immutable
        BEFORE UPDATE ON turn_terminal_candidate_variants
        BEGIN
          SELECT RAISE(ABORT, 'terminal candidate variants are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_candidate_events_immutable
        BEFORE UPDATE ON turn_terminal_candidate_events
        BEGIN
          SELECT RAISE(ABORT, 'terminal candidate events are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turn_generation_identity_immutable
        BEFORE UPDATE OF protocol_version, kind, prior_generation, decision_id,
          execution_intent_id, execution_intent_digest, opened_at ON turn_generations
        WHEN NEW.protocol_version IS NOT OLD.protocol_version OR NEW.kind IS NOT OLD.kind
          OR NEW.prior_generation IS NOT OLD.prior_generation OR NEW.decision_id IS NOT OLD.decision_id
          OR NEW.execution_intent_id IS NOT OLD.execution_intent_id
          OR NEW.execution_intent_digest IS NOT OLD.execution_intent_digest
          OR NEW.opened_at IS NOT OLD.opened_at
        BEGIN
          SELECT RAISE(ABORT, 'terminal generation identity is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS turn_event_proposal_projection_immutable
        BEFORE UPDATE ON turn_event_proposals
        WHEN NEW.proposal_id IS NOT OLD.proposal_id OR NEW.event_id IS NOT OLD.event_id
          OR NEW.turn_id IS NOT OLD.turn_id OR NEW.generation IS NOT OLD.generation
          OR NEW.accept_ordinal IS NOT OLD.accept_ordinal
          OR NEW.accepted_claim_epoch IS NOT OLD.accepted_claim_epoch
          OR NEW.event_type IS NOT OLD.event_type OR NEW.actor_scope IS NOT OLD.actor_scope
          OR NEW.phase_scope IS NOT OLD.phase_scope OR NEW.payload_json IS NOT OLD.payload_json
          OR NEW.payload_digest IS NOT OLD.payload_digest OR NEW.durability IS NOT OLD.durability
          OR NEW.accepted_at IS NOT OLD.accepted_at
        BEGIN
          SELECT RAISE(ABORT, 'terminal event proposal projection is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_receipts_immutable
        BEFORE UPDATE ON turn_terminal_receipts
        BEGIN
          SELECT RAISE(ABORT, 'terminal receipts are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_session_mutations_immutable
        BEFORE UPDATE ON turn_terminal_session_mutations
        BEGIN
          SELECT RAISE(ABORT, 'terminal session mutation ledgers are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_deletion_ledgers_immutable
        BEFORE UPDATE ON turn_terminal_deletion_ledgers
        BEGIN
          SELECT RAISE(ABORT, 'terminal deletion ledgers are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS effect_settlements_immutable
        BEFORE UPDATE ON turn_effect_settlements
        BEGIN
          SELECT RAISE(ABORT, 'effect settlements are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS terminal_auth_epoch_monotonic
        BEFORE UPDATE ON terminal_auth_scope_epochs
        WHEN NEW.epoch <= OLD.epoch OR NEW.row_version <= OLD.row_version
        BEGIN
          SELECT RAISE(ABORT, 'terminal auth epoch must increase');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS account_fence_terminal_guard
        BEFORE UPDATE OF state ON session_lifecycle_operations
        WHEN OLD.account_fence_committed_at IS NOT NULL AND NEW.state IN ('cancelled','conflict')
        BEGIN
          SELECT RAISE(ABORT, 'fenced account lifecycle cannot cancel or conflict');
        END
        """,
    )
    _execute_all(connection, statements)


def _execute_all(connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        connection.exec_driver_sql(statement)
