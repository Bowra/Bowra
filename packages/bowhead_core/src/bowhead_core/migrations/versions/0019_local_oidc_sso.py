"""Add the local-deployment OIDC Authorization Code login data model.

This deliberately does not alter the historical 0004 provider-foundation
tables.  Those tables describe a different, feature-gated architecture and
remain readable only for migration compatibility.

Revision ID: 0019_local_oidc_sso
Revises: 0018_capability_evidence_actual_model
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import context

revision = "0019_local_oidc_sso"
down_revision = "0018_capability_evidence_actual_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()

    # Email is a login identifier.  Check before changing it so an unexpected
    # legacy collision fails the migration rather than silently merging users.
    collisions = connection.exec_driver_sql(
        "SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*) > 1",
    ).fetchall()
    if collisions:
        raise RuntimeError("users.email lowercase migration found duplicate values")
    connection.exec_driver_sql("UPDATE users SET email=lower(email)")

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS local_oidc_configs (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('draft','testing','active','disabled','security_disabled','retired')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
            issuer TEXT NOT NULL,
            metadata_url TEXT,
            authorization_endpoint TEXT NOT NULL,
            token_endpoint TEXT NOT NULL,
            userinfo_endpoint TEXT,
            jwks_uri TEXT NOT NULL,
            client_id TEXT NOT NULL,
            client_secret_encrypted TEXT NOT NULL,
            scopes_json TEXT NOT NULL DEFAULT '["openid","profile","email"]',
            allowed_algs_json TEXT NOT NULL DEFAULT '["RS256"]',
            email_claim TEXT NOT NULL DEFAULT 'email',
            require_verified_email INTEGER NOT NULL DEFAULT 1 CHECK(require_verified_email IN (0,1)),
            join_policy TEXT NOT NULL DEFAULT 'existing_only' CHECK(join_policy IN ('existing_only','invite_only')),
            metadata_fetched_at TEXT,
            metadata_expires_at TEXT,
            last_tested_at TEXT,
            last_test_result TEXT,
            security_epoch INTEGER NOT NULL DEFAULT 1 CHECK(security_epoch > 0),
            created_by TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS local_oidc_config_revisions (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL REFERENCES local_oidc_configs(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(config_id, revision)
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS oidc_login_transactions (
            id TEXT PRIMARY KEY,
            state_hash TEXT NOT NULL UNIQUE,
            config_id TEXT NOT NULL REFERENCES local_oidc_configs(id),
            config_revision INTEGER NOT NULL,
            team_id TEXT NOT NULL,
            public_entry_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            browser_binding_hash TEXT NOT NULL,
            nonce_hash TEXT NOT NULL,
            nonce_encrypted TEXT NOT NULL,
            pkce_verifier_encrypted TEXT NOT NULL,
            return_to TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_oidc_login_transactions_expiry ON oidc_login_transactions(expires_at)",
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS oidc_identities (
            provider_id TEXT NOT NULL REFERENCES local_oidc_configs(id),
            subject TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id),
            claims_digest TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
            linked_at TEXT NOT NULL,
            last_used_at TEXT,
            PRIMARY KEY(provider_id, subject)
        )
        """,
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_oidc_identities_user ON oidc_identities(user_id, last_used_at DESC)",
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS oidc_auth_sessions (
            auth_session_generation TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id),
            config_revision INTEGER NOT NULL,
            security_epoch INTEGER NOT NULL,
            require_verified_email INTEGER NOT NULL CHECK(require_verified_email IN (0,1)),
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            revoke_reason TEXT
        )
        """,
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_oidc_auth_sessions_identity ON oidc_auth_sessions(provider_id, subject, revoked_at)",
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS oidc_audit_events (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            provider_id TEXT,
            actor_id TEXT,
            event_type TEXT NOT NULL,
            result_code TEXT NOT NULL,
            request_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_oidc_audit_events_team_at ON oidc_audit_events(team_id, created_at DESC)",
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
