"""Upgrade prior deployments without rewriting verification history."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from gateway_legacy_database import TARGET_FILES, upgrade

from bowhead_core import migrations


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_probe_diagnostics_upgrade_is_additive_and_repeatable(tmp_path: Path, target: str) -> None:
    db_path = tmp_path / TARGET_FILES[target]
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["bowhead_target"] = target
    command.upgrade(config, importlib.import_module("bowhead_core.migrations.versions.0217_probe_diagnostics").down_revision)
    if target == "core":
        with sqlite3.connect(db_path) as db:
            db.execute("""INSERT INTO team_llm_verification_items
                (id, batch_id, team_id, ordinal, provider_id, model_id, capability,
                 config_signature, testability_contract_version, probe_execution_policy_version,
                 request_count, state, safe_code)
                VALUES ('legacy', 'batch', 'team', 0, 'provider', 'model', 'chat',
                        'signature', 'v1', 'v1', 1, 'failed', 'empty_completion')""")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bowhead.db'}"
    assert upgrade(db_url, target=target) == (target,)
    assert upgrade(db_url, target=target) == (target,)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (SOURCE_REVISION,)
        if target == "core":
            assert db.execute("SELECT id, safe_code, diagnostics_ciphertext FROM team_llm_verification_items").fetchall() == [
                ("legacy", "empty_completion", None)
            ]
        else:
            assert db.execute("SELECT name FROM sqlite_master WHERE name = 'team_llm_verification_items'").fetchone() is None
