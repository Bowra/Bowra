"""The optional encrypted JSON policy has no DDL or ciphertext rewrite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config
from cryptography.fernet import Fernet

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def test_runtime_parameter_revision_preserves_all_target_schemas_and_core_ciphertext(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    policy = {
        "providers": {
            "service": {
                "models": [
                    {
                        "id": "model",
                        "runtime_parameters": {"defaults": {"outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}}}},
                    }
                ]
            }
        }
    }
    encrypted = encrypt_llm_config(policy)
    core_url = f"sqlite+aiosqlite:///{tmp_path / 'bowhead.db'}"
    upgrade(core_url)
    for target, filename in TARGET_FILES.items():
        path = tmp_path / filename
        with sqlite3.connect(path) as connection:
            before = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            if target == "core":
                connection.execute(
                    "INSERT INTO teams(id,name,status,created_at,llm_config_encrypted) VALUES (?,?,?,?,?)",
                    ("team_runtime", "Runtime", "active", "2026-10-10T00:00:00+00:00", encrypted),
                )
                connection.commit()
        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        config.attributes["bowhead_target"] = target
        command.stamp(config, "0166_shared_agent_personal_mcp")
        command.upgrade(config, "0167_model_runtime_parameters")
        command.stamp(config, migrations.chain_head())
        upgrade(core_url, target=target)
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
            if target == "core":
                stored = connection.execute("SELECT llm_config_encrypted FROM teams WHERE id='team_runtime'").fetchone()[0]
                assert stored == encrypted
                assert decrypt_llm_config(stored) == policy
