"""Forward migration preserves historical citations/cells and never invents bindings."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0121_kb_maintenance")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_rows_and_is_repeatable(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    with sqlite3.connect(path) as db:
        if target == "files":
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,uploaded_by,uploaded_at,parse_status,chunks_count,version) VALUES ('kbf_old','art_old','old.pdf','application/pdf',12,'personal','u_old','u_old','2026-09-01','done',3,7)"
            )
        elif target == "core":
            db.execute(
                "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES ('s_old','ws_old','u_old','2026-09-01','2026-09-01')"
            )
            db.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,started_at,final_answer) VALUES ('t_old','s_old',1,'q','2026-09-01','Historical [cite:1]')"
            )
            db.execute("INSERT INTO turn_citations(turn_id,seq,artifact_id,kind) VALUES ('t_old',1,'kb:kbf_old:chunk_old','kb_chunk')")
        elif target == "matrix":
            db.execute(
                "INSERT INTO kb_matrix_cells(matrix_id,file_id,column_id,workspace_id,team_id,status,variants_json,edited_by_user,verified,prompt_version,seq,created_at,updated_at) VALUES ('mx_old','kbf_old','c_old','ws_old','team_old','done','[{\"value\":123}]',1,1,2,1,'2026-09-01','2026-09-01')"
            )
        saved_columns = {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")] for table in ("kb_files", "turn_citations", "kb_matrix_cells")
        }
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    for table, rows in before.items():
        if table not in {"kb_files", "turn_citations", "kb_matrix_cells"}:
            assert after[table] == rows
    with sqlite3.connect(path) as db:
        for table, columns in saved_columns.items():
            if columns:
                assert db.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall() == before[table]
    current_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == current_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "files":
            columns = {row[1] for row in db.execute("PRAGMA table_info(kb_files)")}
            assert {"source_observation_json"} <= columns
            assert db.execute("SELECT count(*) FROM kb_rebuild_jobs").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM kb_file_generations").fetchone()[0] == 0
        if target == "core":
            assert db.execute("SELECT count(*) FROM turn_kb_source_checks").fetchone()[0] == 0


@pytest.mark.parametrize("target,table", [("files", "kb_files")])
def test_failed_ddl_rolls_back_all_new_columns(tmp_path, monkeypatch, target, table):
    from sqlalchemy.engine import Connection

    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement.startswith(f"ALTER TABLE {table}"):
            raise RuntimeError("injected DDL fault")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="injected DDL fault"):
        _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision
