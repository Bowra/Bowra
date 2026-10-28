"""The request record migration adds its tables and retires the unread usage source columns."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0192_llm_request_records")
RECORD_TABLES = {"llm_request_records", "llm_request_chunks", "llm_request_changes"}
DIGEST = "a" * 64


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def _insert_minimal(db: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """Fill NOT NULL columns without defaults so trigger tests need no full fixture."""
    row = dict(values)
    for _cid, name, kind, notnull, default, _pk in db.execute(f"PRAGMA table_info({table})"):
        if name in row or not notnull or default is not None:
            continue
        row[name] = 0 if "INT" in (kind or "").upper() else f"{name}_x"
    columns = ",".join(row)
    db.execute(f"INSERT INTO {table}({columns}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))


def _record(db: sqlite3.Connection, record_id: str, turn_id: str, session_id: str) -> None:
    db.execute(
        "INSERT INTO llm_request_records(id,turn_id,session_id,team_id,user_id,workspace_id,reason,provider,model,mode,"
        "request_digest,chunk_hashes_json,created_at,expires_at) VALUES(?,?,?,'team_1','u_1','ws_1','turn','p','m','invoke',?,'[]','t','t')",
        (record_id, turn_id, session_id, DIGEST),
    )
    db.execute(
        "INSERT INTO llm_request_chunks(turn_id,chunk_hash,ciphertext,byte_size,omitted,created_at) VALUES(?,?,?,1,0,'t')",
        (turn_id, DIGEST, "cipher"),
    )
    db.execute(
        "INSERT INTO llm_request_changes(id,record_id,kind,detail_ciphertext,created_at) VALUES(?,?,'tool_result_capped','cipher','t')",
        ("lrc_" + record_id, record_id),
    )


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_request_tables_and_retires_source_columns(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            assert {"source_ciphertext", "source_expires_at"} <= set(_columns(db, "llm_usage_events"))
            db.execute(
                "INSERT INTO llm_usage_events(id,team_id,user_id,workspace_id,turn_id,reason,agent_type,provider,model,streamed,"
                "created_at,receipt_json,receipt_state,source_ciphertext,source_expires_at) "
                "VALUES('lu_1','team_1','u_1','ws_1','t_1','turn','lead','p','m',0,'t','{}','pending','cipher','t')"
            )
    before = schema(path)

    _upgrade(path, MIGRATION.revision, target)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
    if target != "core":
        assert schema(path) == before
        return
    with sqlite3.connect(path) as db:
        usage_columns = _columns(db, "llm_usage_events")
        assert "source_ciphertext" not in usage_columns and "source_expires_at" not in usage_columns
        # The existing receipt survives, and the recreated trigger still keeps it immutable.
        assert db.execute("SELECT receipt_state FROM llm_usage_events WHERE id='lu_1'").fetchone() == ("pending",)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE llm_usage_events SET model='other' WHERE id='lu_1'")
        db.execute("UPDATE llm_usage_events SET receipt_state='completed',tokens_input=1,tokens_output=1,tokens_cached=0 WHERE id='lu_1'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE llm_usage_events SET tokens_input=2 WHERE id='lu_1'")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert tables >= RECORD_TABLES

    after = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after


def test_record_rows_follow_turn_deletion_and_session_soft_deletion(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for session_id in ("s_keep", "s_drop"):
            _insert_minimal(db, "sessions", {"id": session_id, "deleted_at": None})
        for seq, (turn_id, session_id) in enumerate((("t_deleted", "s_keep"), ("t_kept", "s_keep"), ("t_in_deleted_session", "s_drop"))):
            _insert_minimal(db, "turns", {"id": turn_id, "session_id": session_id, "seq": seq})
            _record(db, "lrq_" + turn_id, turn_id, session_id)

        db.execute("DELETE FROM turns WHERE id='t_deleted'")
        db.execute("UPDATE sessions SET deleted_at='now' WHERE id='s_drop'")

        assert [row[0] for row in db.execute("SELECT turn_id FROM llm_request_records ORDER BY turn_id")] == ["t_kept"]
        assert [row[0] for row in db.execute("SELECT turn_id FROM llm_request_chunks ORDER BY turn_id")] == ["t_kept"]
        assert [row[0] for row in db.execute("SELECT record_id FROM llm_request_changes")] == ["lrq_t_kept"]


def test_record_tables_reject_malformed_rows(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO llm_request_chunks(turn_id,chunk_hash,ciphertext,byte_size,omitted,created_at) VALUES('t',?,NULL,1,0,'t')",
                (DIGEST,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO llm_request_changes(id,record_id,kind,detail_ciphertext,created_at) VALUES('lrc_1','lrq_1','unknown','c','t')")
