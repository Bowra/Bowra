from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

from alembic import command
from alembic.config import Config
from bowhead_core.migrations.baseline_schema import initial_schema

from bowhead_core import migrations

MIGRATION = import_module("bowhead_core.migrations.versions.0022_powerbi_service_principal")


def _config(path: Path, target: str = "core") -> Config:
    # Build Alembic config locally: bowhead_core/tests is outside sys.path when running Gateway tests independently.
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_powerbi_service_principal_upgrade_preserves_legacy_registry_and_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute(
            """
            INSERT INTO pbi_connection(id, team_id, name, workspace_id, dataset_id, created_at, updated_at)
            VALUES ('pbi_legacy', 'team_legacy', 'Legacy Power BI', 'workspace_legacy', 'dataset_legacy',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        connection.commit()

    config = _config(db_path)
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert connection.execute("SELECT workspace_id,dataset_id FROM pbi_connection_legacy_0022 WHERE id='pbi_legacy'").fetchone() == (
            "workspace_legacy",
            "dataset_legacy",
        )
        assert {
            "id",
            "team_id",
            "name",
            "auth_ref",
            "secret_hint",
            "status",
            "created_at",
            "updated_at",
        } == {str(row[1]) for row in connection.execute("PRAGMA table_info(pbi_connection)")}
        assert {"idx_pbi_connection_team"} <= {str(row[1]) for row in connection.execute("PRAGMA index_list(pbi_connection)")}
        assert {"idx_pbi_dax_template_connection"} <= {str(row[1]) for row in connection.execute("PRAGMA index_list(pbi_dax_template)")}


def test_powerbi_service_principal_upgrade_recovers_after_partial_rename(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute("ALTER TABLE pbi_connection RENAME TO pbi_connection_legacy_0022")
        connection.execute("DROP INDEX IF EXISTS idx_pbi_connection_team")
        connection.executescript(
            """
            CREATE TABLE pbi_connection (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                name TEXT NOT NULL,
                auth_ref TEXT NOT NULL,
                secret_hint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_pbi_connection_team ON pbi_connection(team_id, status);
            """
        )
        connection.commit()

    command.upgrade(_config(db_path), MIGRATION.revision)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pbi_connection_legacy_0022'").fetchone() == (
            "pbi_connection_legacy_0022",
        )
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pbi_dax_template'").fetchone() == ("pbi_dax_template",)
