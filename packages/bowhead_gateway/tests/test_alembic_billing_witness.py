"""Add the settlement witness without weakening the immutable receipt journal."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from bowhead_core import migrations

MIGRATION = importlib.import_module("bowhead_core.migrations.versions.0218_billing_witness")


def test_billing_witness_upgrade_preserves_original_settlement_contract(tmp_path: Path) -> None:
    path = tmp_path / "bowhead.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as connection:
        original_columns = connection.execute("PRAGMA table_info(turn_effect_settlements)").fetchall()
        original_guards = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='turn_effect_settlements' ORDER BY name"
        ).fetchall()
        connection.execute("CREATE TABLE billing_migration_probe(identity TEXT PRIMARY KEY, payload BLOB)")
        connection.execute("INSERT INTO billing_migration_probe VALUES(?,?)", ("retained", b"\x00\xff unchanged"))
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as connection:
        columns = connection.execute("PRAGMA table_info(turn_effect_settlements)").fetchall()
        assert columns[:-1] == original_columns
        assert columns[-1][1:5] == ("billing_witness_ciphertext", "TEXT", 0, None)
        assert (
            connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='turn_effect_settlements' ORDER BY name"
            ).fetchall()
            == original_guards
        )
        assert connection.execute("SELECT identity,payload FROM billing_migration_probe").fetchall() == [("retained", b"\x00\xff unchanged")]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
