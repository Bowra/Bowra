"""0115_session_attachments_artifact_index 最小升级测试：仅 core 建索引、存量行不动、来源会话查询走索引、重复执行幂等。"""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0115_session_attachments_artifact_index")
_TARGETS = ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"]
_ORIGIN_SQL = "SELECT session_id FROM session_attachments WHERE artifact_id IN (?, ?) ORDER BY pinned_at ASC"


def _indexes(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='session_attachments'")}


@pytest.mark.parametrize("target", _TARGETS)
def test_index_only_on_core_and_existing_rows_are_kept(tmp_path, target) -> None:
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.executemany(
                "INSERT INTO session_attachments(session_id, artifact_id, pinned_at) VALUES (?,?,?)",
                [("s_1", "art_1", "2026-09-01T00:00:00+00:00"), ("s_2", "art_1", "2026-08-01T00:00:00+00:00")],
            )
            db.commit()
            assert "idx_session_attachments_artifact" not in _indexes(db)
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)

    with sqlite3.connect(path) as db:
        if target != "core":
            assert _indexes(db) == set()
            return
        assert "idx_session_attachments_artifact" in _indexes(db)
        plan = " ".join(str(row[-1]) for row in db.execute(f"EXPLAIN QUERY PLAN {_ORIGIN_SQL}", ("art_1", "art_2")))
        assert "idx_session_attachments_artifact" in plan, plan
        assert [row[0] for row in db.execute(_ORIGIN_SQL, ("art_1", "art_2"))] == ["s_2", "s_1"]
