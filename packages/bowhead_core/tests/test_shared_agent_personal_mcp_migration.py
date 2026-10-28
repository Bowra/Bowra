"""The sharing migration removes only the obsolete audience guards."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_artifact_workspace_cutover_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0166_shared_agent_personal_mcp")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_upgrade_removes_sharing_guards_and_preserves_owner_guard(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
        if target == "core":
            db.execute(
                "INSERT INTO personal_mcp_binding(agent_id,revision_id,owner_user_id,tool_ids_json,version) VALUES (?,?,?,?,?)",
                ("osa_existing", "arv_existing", "usr_existing", '["pmt_existing"]', 2),
            )
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        after = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    if target == "core":
        assert set(MIGRATION.SHARING_TRIGGERS) <= before
        assert not set(MIGRATION.SHARING_TRIGGERS) & after
        assert "personal_mcp_owner_immutable" in after
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT tool_ids_json,version FROM personal_mcp_binding WHERE agent_id='osa_existing'").fetchone() == (
                '["pmt_existing"]',
                2,
            )
    else:
        assert before == after
