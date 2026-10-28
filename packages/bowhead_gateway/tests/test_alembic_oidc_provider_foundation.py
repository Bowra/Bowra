from __future__ import annotations

import sqlite3

from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

OIDC_TABLES = {
    "identity_provider_realms",
    "oidc_callback_routes",
    "oidc_client_registration_revisions",
    "oidc_client_registrations",
    "oidc_client_secret_versions",
    "oidc_jwks_snapshots",
    "oidc_metadata_snapshots",
    "oidc_public_entry_bindings",
    "team_identity_provider_binding_revisions",
    "team_identity_provider_bindings",
}

OIDC_TRIGGERS = {
    "oidc_callback_route_immutable",
    "oidc_callback_route_no_delete",
    "oidc_callback_route_no_reactivate",
}


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
            ("team_legacy", "Legacy Team", "active", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()


def test_oidc_provider_foundation_upgrades_1_0_core_schema(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    assert upgrade(db_url, target="core") == ("core",)
    assert upgrade(db_url, target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'",
            ).fetchall()
        }
        assert tables >= OIDC_TABLES
        assert triggers >= OIDC_TRIGGERS
        assert connection.execute(
            "SELECT name,status FROM teams WHERE id='team_legacy'",
        ).fetchone() == ("Legacy Team", "active")
