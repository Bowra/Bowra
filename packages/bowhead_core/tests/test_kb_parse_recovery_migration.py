"""0114_kb_parse_recovery 最小升级测试：仅 files 目标加两列、存量行不回填、Store 初始化后的 1.0.0 库也能升到 head。"""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from bowhead_core.kb.files_store import KbFilesStore
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0114_kb_parse_recovery")
_TARGETS = ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"]


@pytest.mark.parametrize("target", _TARGETS)
async def test_parse_recovery_columns_only_on_files_target_and_existing_rows_keep_data(tmp_path, target) -> None:
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "files":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,"
                "workspace_id,team_id,uploaded_by,uploaded_at,parse_status) "
                "VALUES ('kbf_1','art_1','a.pptx','application/pdf',1,'personal','u1','','team_1','u1',"
                "'2026-08-08T00:00:00+00:00','pending')"
            )
            db.commit()
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(kb_files)")}
    if target != "files":
        assert columns == set()
        return
    assert {"parse_attempts", "parse_heartbeat_at"} <= columns

    # Store startup DDL no-ops on migrated DB; reads see existing stuck behavior as retryable.
    store = KbFilesStore(path)
    await store.initialize()
    dto = await store.get("kbf_1")
    assert (dto.parse_status, dto.parse_stuck) == ("pending", True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT parse_attempts, parse_heartbeat_at FROM kb_files").fetchone() == (0, None)


async def test_store_initialized_database_then_migration_converges(tmp_path) -> None:
    path = tmp_path / "kb_files.db"
    await KbFilesStore(path).initialize()
    config = _config(path, "files")
    command.upgrade(config, "head")

    with sqlite3.connect(path) as db:
        names = [row[1] for row in db.execute("PRAGMA table_info(kb_files)")]
    assert names.count("parse_attempts") == 1
    assert names.count("parse_heartbeat_at") == 1
