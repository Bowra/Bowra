from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _upgrade(path: Path, revision: str, target: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    command.upgrade(config, revision)


@pytest.mark.parametrize(
    "target",
    ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"],
)
def test_non_core_targets_are_explicit_noop(tmp_path, target) -> None:
    path = tmp_path / f"{target}.db"
    _upgrade(path, "0074_teams_personal_connection", target)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    _upgrade(path, "0075_agent_version_operations", target)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    assert after == before


def test_core_upgrade_adds_immutable_operational_ledgers(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, "0075_agent_version_operations", "core")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "agent_change_proposal",
            "agent_change_proposal_decision",
            "agent_dependency_update_policy",
            "agent_version_automation_grant",
            "agent_version_webhook_subscription",
            "agent_version_webhook_event",
            "agent_version_webhook_delivery",
        } <= tables
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
