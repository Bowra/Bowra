"""Add the team LLM verification V2 control-plane schema.

Revision ID: 0005_team_llm_verification
Revises: 0004_oidc_provider_foundation
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import context

revision = "0005_team_llm_verification"
down_revision = "0004_oidc_provider_foundation"
branch_labels = None
depends_on = None

_IMMUTABLE_TABLES = (
    "team_llm_capability_evidence",
    "team_llm_risk_acceptances",
    "team_llm_risk_acceptance_retirements",
    "team_llm_v2_enrollments",
    "team_llm_v2_migration_baselines",
    "team_llm_v2_migration_baseline_invalidations",
    "team_llm_apply_snapshots",
)


def _create_immutable_trigger(table: str) -> None:
    context.get_bind().exec_driver_sql(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is immutable');
        END
        """
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return

    connection = context.get_bind()
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_provider_identities (
            team_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            runtime_key TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK(state IN ('reserved', 'active', 'deleted')),
            created_by TEXT,
            creation_source TEXT NOT NULL
                CHECK(creation_source IN ('user', 'legacy_migration')),
            created_at TEXT NOT NULL,
            activated_at TEXT,
            deleted_at TEXT,
            reservation_expires_at TEXT,
            PRIMARY KEY (team_id, provider_id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_team_llm_provider_active_key
        ON team_llm_provider_identities(team_id, runtime_key)
        WHERE state IN ('reserved', 'active')
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_llm_provider_identity_state
        ON team_llm_provider_identities(team_id, state, reservation_expires_at)
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_verification_plans (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            actor_user_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            items_json TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            provider_request_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_batch_id TEXT,
            consumed_at TEXT
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_llm_plans_expiry
        ON team_llm_verification_plans(team_id, expires_at)
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_verification_batches (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            actor_user_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            base_config_version INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(
                state IN (
                    'queued', 'running', 'cancelling',
                    'completed', 'cancelled', 'interrupted'
                )
            ),
            probe_context_ciphertext TEXT,
            cancel_requested_at TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            UNIQUE (team_id, actor_user_id, idempotency_key)
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_team_llm_batches_team_state
        ON team_llm_verification_batches(team_id, state, created_at DESC)
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_verification_items (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            capability TEXT NOT NULL
                CHECK(capability IN ('chat', 'tool', 'vision', 'embed', 'rerank')),
            config_signature TEXT NOT NULL,
            testability_contract_version TEXT NOT NULL,
            probe_contract_version TEXT,
            probe_execution_policy_version TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(
                state IN (
                    'queued', 'verifying', 'verified', 'failed', 'not_testable',
                    'errored', 'cancelled', 'interrupted'
                )
            ),
            runner_id TEXT,
            lease_until TEXT,
            cancel_requested_at TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            safe_code TEXT,
            safe_http_status INTEGER,
            safe_summary_code TEXT,
            duration_ms INTEGER,
            request_started_at TEXT,
            completed_at TEXT,
            UNIQUE (batch_id, ordinal)
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_items_team_slots
        ON team_llm_verification_items(team_id, state, lease_until)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_items_batch_revision
        ON team_llm_verification_items(batch_id, revision, ordinal)
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_capability_evidence (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            provider_display_name_snapshot TEXT NOT NULL,
            model_id TEXT NOT NULL,
            capability TEXT NOT NULL
                CHECK(capability IN ('chat', 'tool', 'vision', 'embed', 'rerank')),
            config_signature TEXT NOT NULL,
            signature_version TEXT NOT NULL,
            testability_contract_version TEXT NOT NULL,
            probe_contract_version TEXT,
            probe_execution_policy_version TEXT NOT NULL,
            outcome TEXT NOT NULL
                CHECK(outcome IN ('verified', 'failed', 'not_testable')),
            safe_code TEXT NOT NULL,
            safe_http_status INTEGER,
            safe_summary_code TEXT,
            vector_dimension INTEGER,
            batch_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            initiated_by TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            fresh_until TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            UNIQUE (item_id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_evidence_lookup
        ON team_llm_capability_evidence(
            team_id, provider_id, model_id, capability,
            config_signature, observed_at DESC
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_evidence_provider_history
        ON team_llm_capability_evidence(
            team_id, provider_id, observed_at DESC
        )
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_risk_acceptances (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            capability TEXT NOT NULL
                CHECK(capability IN ('chat', 'tool', 'vision', 'embed', 'rerank')),
            role_key TEXT NOT NULL,
            config_signature TEXT NOT NULL,
            testability_contract_version TEXT NOT NULL,
            risk_code TEXT NOT NULL,
            accepted_by TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            applied_config_version INTEGER NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_risk_acceptance_retirements (
            risk_acceptance_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            retired_at TEXT NOT NULL,
            retired_by_config_version INTEGER,
            reason TEXT NOT NULL CHECK(
                reason IN (
                    'scope_removed', 'signature_changed',
                    'verifier_available', 'team_deleted'
                )
            )
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_risk_acceptance_scope
        ON team_llm_risk_acceptances(
            team_id, provider_id, model_id, capability,
            role_key, config_signature
        )
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_v2_enrollments (
            team_id TEXT PRIMARY KEY,
            enrolled_config_version INTEGER NOT NULL,
            enrolled_semantic_hash TEXT NOT NULL,
            assignments_json TEXT NOT NULL,
            rollout_generation TEXT NOT NULL,
            enrollment_source TEXT NOT NULL
                CHECK(enrollment_source IN ('first_v2_read', 'first_v2_write')),
            enrolled_at TEXT NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_v2_migration_baselines (
            team_id TEXT PRIMARY KEY,
            enrollment_config_version INTEGER NOT NULL,
            enrollment_semantic_hash TEXT NOT NULL,
            baseline_config_version INTEGER NOT NULL,
            baseline_semantic_hash TEXT NOT NULL,
            assignments_json TEXT NOT NULL,
            accepted_by TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            applied_config_version INTEGER NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_v2_migration_baseline_invalidations (
            team_id TEXT PRIMARY KEY,
            baseline_config_version INTEGER NOT NULL,
            invalidated_at TEXT NOT NULL,
            invalidated_by_config_version INTEGER NOT NULL,
            reason TEXT NOT NULL CHECK(reason = 'critical_config_changed'),
            changed_role_keys_json TEXT NOT NULL
        )
        """
    )

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_llm_apply_snapshots (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            previous_config_version INTEGER NOT NULL,
            actor_user_id TEXT NOT NULL,
            draft_semantic_hash TEXT NOT NULL,
            readiness_contract_version TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            UNIQUE (team_id, config_version)
        )
        """
    )

    for table in _IMMUTABLE_TABLES:
        _create_immutable_trigger(table)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
