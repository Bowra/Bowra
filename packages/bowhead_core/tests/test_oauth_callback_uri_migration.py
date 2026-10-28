import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

from bowhead_core import migrations

REVISION = import_module("bowhead_core.migrations.versions.0175_oauth_callback_uri")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_callback_uri_upgrade_preserves_legacy_state_and_other_schemas(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        if target == "core":
            db.execute(
                "INSERT INTO oauth_state(state,provider,next_path,public_entry_id,expires_at,created_at) VALUES('old','feishu','/next/','pe_old','2099-01-01','2026-10-14')"
            )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
        if target == "core":
            assert db.execute("SELECT state,provider,next_path,public_entry_id,redirect_uri FROM oauth_state").fetchone() == (
                "old",
                "feishu",
                "/next/",
                "pe_old",
                None,
            )
        else:
            assert db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall() == before
