"""Add the provider-neutral OIDC configuration foundation.

Revision ID: 0004_oidc_provider_foundation
Revises: 0003_space_administration
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import context

revision = "0004_oidc_provider_foundation"
down_revision = "0003_space_administration"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_provider_realms (
    id                       TEXT PRIMARY KEY,
    provider_type            TEXT NOT NULL CHECK(provider_type = 'oidc'),
    legacy_key               TEXT UNIQUE,
    display_name             TEXT NOT NULL,
    issuer_exact             TEXT NOT NULL UNIQUE,
    status                   TEXT NOT NULL
                             CHECK(status IN ('draft','trusted','security_disabled','retired')),
    allowed_algs_json        TEXT NOT NULL DEFAULT '["RS256"]',
    network_policy_id        TEXT NOT NULL,
    tenant_constraints_json  TEXT NOT NULL DEFAULT '{}',
    subject_type_policy      TEXT NOT NULL DEFAULT 'unknown',
    config_version           INTEGER NOT NULL DEFAULT 1,
    security_epoch           INTEGER NOT NULL DEFAULT 0,
    created_by               TEXT NOT NULL,
    updated_by               TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_metadata_snapshots (
    id                              TEXT PRIMARY KEY,
    provider_id                     TEXT NOT NULL,
    issuer_exact                    TEXT NOT NULL,
    authorization_endpoint          TEXT NOT NULL,
    token_endpoint                  TEXT NOT NULL,
    jwks_uri                        TEXT NOT NULL,
    userinfo_endpoint               TEXT,
    response_modes_json             TEXT NOT NULL,
    code_challenge_methods_json     TEXT NOT NULL,
    token_auth_methods_json         TEXT NOT NULL,
    id_token_algs_json              TEXT NOT NULL,
    capabilities_json               TEXT NOT NULL DEFAULT '{}',
    document_digest                 TEXT NOT NULL,
    protocol_config_digest          TEXT NOT NULL,
    fetched_at                      TEXT NOT NULL,
    fresh_until                     TEXT NOT NULL,
    hard_expires_at                 TEXT NOT NULL,
    status                          TEXT NOT NULL
                                    CHECK(status IN ('current','superseded','invalid')),
    UNIQUE(id, provider_id),
    FOREIGN KEY(provider_id) REFERENCES identity_provider_realms(id)
);
CREATE INDEX IF NOT EXISTS idx_oidc_metadata_provider
    ON oidc_metadata_snapshots(provider_id, status, fresh_until);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oidc_metadata_current
    ON oidc_metadata_snapshots(provider_id)
    WHERE status = 'current';

CREATE TABLE IF NOT EXISTS oidc_jwks_snapshots (
    id                    TEXT PRIMARY KEY,
    provider_id           TEXT NOT NULL,
    metadata_snapshot_id  TEXT NOT NULL,
    jwks_json             TEXT NOT NULL,
    document_digest       TEXT NOT NULL,
    fetched_at            TEXT NOT NULL,
    fresh_until           TEXT NOT NULL,
    hard_expires_at       TEXT NOT NULL,
    status                TEXT NOT NULL
                          CHECK(status IN ('current','superseded','invalid')),
    UNIQUE(id, provider_id, metadata_snapshot_id),
    FOREIGN KEY(provider_id) REFERENCES identity_provider_realms(id),
    FOREIGN KEY(metadata_snapshot_id, provider_id)
        REFERENCES oidc_metadata_snapshots(id, provider_id)
);
CREATE INDEX IF NOT EXISTS idx_oidc_jwks_provider
    ON oidc_jwks_snapshots(provider_id, status, fresh_until);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oidc_jwks_current
    ON oidc_jwks_snapshots(provider_id)
    WHERE status = 'current';

CREATE TABLE IF NOT EXISTS oidc_client_registrations (
    id                  TEXT PRIMARY KEY,
    provider_id         TEXT NOT NULL,
    logical_name        TEXT NOT NULL,
    active_revision_id  TEXT,
    status              TEXT NOT NULL
                        CHECK(status IN
                              ('draft','testing','ready','credential_expired','disabled')),
    created_by          TEXT NOT NULL,
    updated_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(id, provider_id),
    UNIQUE(provider_id, logical_name),
    FOREIGN KEY(provider_id) REFERENCES identity_provider_realms(id),
    FOREIGN KEY(active_revision_id, id, provider_id)
        REFERENCES oidc_client_registration_revisions(id, client_registration_id, provider_id)
);

CREATE TABLE IF NOT EXISTS oidc_client_secret_versions (
    id                      TEXT PRIMARY KEY,
    client_registration_id  TEXT NOT NULL,
    version                 INTEGER NOT NULL,
    secret_ref              TEXT NOT NULL,
    status                  TEXT NOT NULL
                            CHECK(status IN
                                  ('staged','active','retiring','compromised','retired')),
    not_before              TEXT NOT NULL,
    not_after               TEXT,
    created_by              TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    retired_at              TEXT,
    UNIQUE(id, client_registration_id),
    UNIQUE(client_registration_id, version),
    FOREIGN KEY(client_registration_id) REFERENCES oidc_client_registrations(id)
);

CREATE TABLE IF NOT EXISTS oidc_client_registration_revisions (
    id                       TEXT PRIMARY KEY,
    client_registration_id   TEXT NOT NULL,
    provider_id              TEXT NOT NULL,
    revision                 INTEGER NOT NULL,
    client_id                TEXT NOT NULL,
    client_auth_method       TEXT NOT NULL
                             CHECK(client_auth_method IN
                                   ('client_secret_basic','client_secret_post')),
    secret_version_id        TEXT NOT NULL,
    scopes_json              TEXT NOT NULL,
    redirect_uris_json       TEXT NOT NULL,
    config_digest            TEXT NOT NULL,
    status                   TEXT NOT NULL
                             CHECK(status IN
                                   ('staged','active','retiring','compromised','retired')),
    created_by               TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    UNIQUE(id, provider_id),
    UNIQUE(id, client_registration_id, provider_id),
    UNIQUE(id, client_registration_id, provider_id, secret_version_id),
    UNIQUE(client_registration_id, revision),
    UNIQUE(provider_id, client_id, revision),
    FOREIGN KEY(client_registration_id, provider_id)
        REFERENCES oidc_client_registrations(id, provider_id),
    FOREIGN KEY(secret_version_id, client_registration_id)
        REFERENCES oidc_client_secret_versions(id, client_registration_id)
);

CREATE TABLE IF NOT EXISTS team_identity_provider_bindings (
    id                       TEXT PRIMARY KEY,
    public_alias             TEXT NOT NULL UNIQUE,
    team_id                  TEXT NOT NULL,
    provider_id              TEXT NOT NULL,
    client_registration_id   TEXT NOT NULL,
    active_revision_id       TEXT,
    status                   TEXT NOT NULL
                             CHECK(status IN ('draft','testing','active','disabled')),
    activated_at             TEXT,
    created_by               TEXT NOT NULL,
    updated_by               TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE(id, provider_id),
    UNIQUE(id, team_id, provider_id),
    UNIQUE(id, provider_id, client_registration_id),
    FOREIGN KEY(team_id) REFERENCES teams(id),
    FOREIGN KEY(provider_id) REFERENCES identity_provider_realms(id),
    FOREIGN KEY(client_registration_id, provider_id)
        REFERENCES oidc_client_registrations(id, provider_id),
    FOREIGN KEY(active_revision_id, id, provider_id)
        REFERENCES team_identity_provider_binding_revisions(id, team_binding_id, provider_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oidc_team_provider_live
    ON team_identity_provider_bindings(team_id, provider_id)
    WHERE status != 'disabled';

CREATE TABLE IF NOT EXISTS team_identity_provider_binding_revisions (
    id                       TEXT PRIMARY KEY,
    team_binding_id          TEXT NOT NULL,
    provider_id              TEXT NOT NULL,
    revision                 INTEGER NOT NULL,
    display_label            TEXT NOT NULL,
    provisioning_policy      TEXT NOT NULL
                             CHECK(provisioning_policy IN
                                   ('existing_only','invite_only','jit')),
    allowed_domains_json     TEXT NOT NULL DEFAULT '[]',
    email_assurance_policy   TEXT NOT NULL
                             CHECK(email_assurance_policy IN
                                   ('not_required','verified_email_required')),
    default_member_role      TEXT NOT NULL DEFAULT 'member'
                             CHECK(default_member_role = 'member'),
    config_digest            TEXT NOT NULL,
    created_by               TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    UNIQUE(id, team_binding_id, provider_id),
    UNIQUE(team_binding_id, revision),
    FOREIGN KEY(team_binding_id, provider_id)
        REFERENCES team_identity_provider_bindings(id, provider_id)
);

CREATE TABLE IF NOT EXISTS oidc_callback_routes (
    id                TEXT PRIMARY KEY,
    public_route_id   TEXT NOT NULL UNIQUE,
    provider_id       TEXT NOT NULL,
    public_entry_id   TEXT NOT NULL,
    redirect_uri      TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL
                      CHECK(status IN ('active','tombstoned')),
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    tombstoned_at     TEXT,
    tombstone_reason  TEXT,
    UNIQUE(id, provider_id),
    UNIQUE(id, provider_id, public_entry_id),
    FOREIGN KEY(provider_id) REFERENCES identity_provider_realms(id),
    FOREIGN KEY(public_entry_id) REFERENCES public_entries(id)
);

CREATE TRIGGER IF NOT EXISTS oidc_callback_route_immutable
BEFORE UPDATE OF public_route_id, provider_id, public_entry_id, redirect_uri
ON oidc_callback_routes
BEGIN
    SELECT RAISE(ABORT, 'oidc_callback_route_immutable');
END;

CREATE TRIGGER IF NOT EXISTS oidc_callback_route_no_reactivate
BEFORE UPDATE OF status ON oidc_callback_routes
WHEN OLD.status = 'tombstoned' AND NEW.status != 'tombstoned'
BEGIN
    SELECT RAISE(ABORT, 'oidc_callback_route_tombstoned');
END;

CREATE TRIGGER IF NOT EXISTS oidc_callback_route_no_delete
BEFORE DELETE ON oidc_callback_routes
BEGIN
    SELECT RAISE(ABORT, 'oidc_callback_route_retained');
END;

CREATE TABLE IF NOT EXISTS oidc_public_entry_bindings (
    id                       TEXT PRIMARY KEY,
    team_binding_id          TEXT NOT NULL,
    provider_id              TEXT NOT NULL,
    client_registration_id   TEXT NOT NULL,
    public_entry_id          TEXT NOT NULL,
    redirect_uri             TEXT NOT NULL,
    mix_up_defense           TEXT NOT NULL
                             CHECK(mix_up_defense IN
                                   ('response_iss','distinct_redirect_uri')),
    callback_route_id        TEXT,
    registration_status      TEXT NOT NULL
                             CHECK(registration_status IN
                                   ('unregistered','attested','verified','stale','unreachable')),
    attested_binding_revision_id TEXT,
    attested_client_revision_id  TEXT,
    attested_metadata_snapshot_id TEXT,
    entry_updated_at         TEXT NOT NULL,
    attested_by              TEXT,
    attested_at              TEXT,
    verified_at              TEXT,
    last_test_result         TEXT,
    last_tested_at           TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    CHECK((mix_up_defense = 'response_iss' AND callback_route_id IS NULL)
       OR (mix_up_defense = 'distinct_redirect_uri' AND callback_route_id IS NOT NULL)),
    CHECK(registration_status != 'verified'
       OR (attested_binding_revision_id IS NOT NULL
           AND attested_client_revision_id IS NOT NULL
           AND attested_metadata_snapshot_id IS NOT NULL)),
    UNIQUE(team_binding_id, public_entry_id),
    UNIQUE(id, provider_id),
    UNIQUE(id, team_binding_id, client_registration_id, provider_id, public_entry_id),
    FOREIGN KEY(team_binding_id, provider_id, client_registration_id)
        REFERENCES team_identity_provider_bindings(id, provider_id, client_registration_id),
    FOREIGN KEY(attested_binding_revision_id, team_binding_id, provider_id)
        REFERENCES team_identity_provider_binding_revisions(id, team_binding_id, provider_id),
    FOREIGN KEY(attested_client_revision_id, client_registration_id, provider_id)
        REFERENCES oidc_client_registration_revisions(
            id, client_registration_id, provider_id
        ),
    FOREIGN KEY(attested_metadata_snapshot_id, provider_id)
        REFERENCES oidc_metadata_snapshots(id, provider_id),
    FOREIGN KEY(public_entry_id) REFERENCES public_entries(id),
    FOREIGN KEY(callback_route_id, provider_id, public_entry_id)
        REFERENCES oidc_callback_routes(id, provider_id, public_entry_id)
);
"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    raw_connection = connection.connection.driver_connection
    raw_connection.executescript(SCHEMA)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
