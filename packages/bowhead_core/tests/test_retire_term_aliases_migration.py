"""Retiring term aliases must erase recoverable copies and preserve other memory."""

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0187_retire_term_aliases")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_retires_alias_data_and_preserves_other_memory(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO user_glossary(scope,user_id,workspace_id,alias_lower,alias,real_name,kind,source,created_at) "
                "VALUES('user','u1','ws_1','sales shorthand','sales shorthand','sales metric','measure','admin','2026-10-19')"
            )
            db.execute(
                "INSERT INTO memory_reset_snapshots(id,user_id,snapshot_json,created_at,counts_json) VALUES(?,?,?,?,?)",
                (
                    "snap_retired",
                    "u1",
                    json.dumps(
                        {
                            "glossary": [{"alias": "sales shorthand"}],
                            "memory_entries": [
                                {"kind": "glossary", "key": "sales shorthand", "value": "sales metric"},
                                {"kind": "preference", "key": "style", "value": "concise"},
                            ],
                            "notes": {"content": "Keep this note"},
                        }
                    ),
                    "2026-10-19",
                    '{"entries":1,"personal_glossary":1}',
                ),
            )
    if target == "core":
        from bowhead_core.memory_pack_snapshots import _TABLE_SCHEMA as pack_schema
        from bowhead_memory.candidates import SCHEMA as candidate_schema

        with sqlite3.connect(path) as db:
            db.executescript(candidate_schema)
            db.executescript(pack_schema)
            for kind in ("glossary", "preference"):
                db.execute(
                    "INSERT INTO memory_entries(id,subject_type,subject_id,kind,key,value,source,created_at,updated_at) "
                    "VALUES(?, 'user','u1',?,'label',?,'manual','2026-10-19','2026-10-19')",
                    (f"mem_{kind}", kind, json.dumps("value")),
                )
                db.execute(
                    "INSERT INTO memory_candidates(id,user_id,kind,key,value_json,status,created_at,updated_at) "
                    "VALUES(?,'u1',?,'label',?,'pending','2026-10-19','2026-10-19')",
                    (f"cand_{kind}", kind, json.dumps("value")),
                )
            for turn_id, content in (
                ("t_alias", "- User business glossary:\n    · alias = meaning"),
                ("t_other", "Keep this note"),
                ("t_alias_localized", "Private localized term content"),
            ):
                db.execute(
                    "INSERT INTO memory_pack_snapshots(turn_id,user_id,content,char_count,pack_version,created_at,sections_breakdown_json) VALUES(?,'u1',?,?, 'v1','2026-10-19',?)",
                    (turn_id, content, len(content), json.dumps({"glossary": 1 if turn_id.startswith("t_alias") else 0})),
                )
    before_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        if target == "core":
            assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='user_glossary'").fetchone() is None
            payload, counts = db.execute("SELECT snapshot_json,counts_json FROM memory_reset_snapshots WHERE id='snap_retired'").fetchone()
            assert json.loads(payload) == {
                "memory_entries": [{"kind": "preference", "key": "style", "value": "concise"}],
                "notes": {"content": "Keep this note"},
            }
            assert json.loads(counts) == {"entries": 1}
            assert db.execute("SELECT id FROM memory_entries").fetchall() == [("mem_preference",)]
            assert db.execute("SELECT id FROM memory_candidates").fetchall() == [("cand_preference",)]
            assert db.execute("SELECT turn_id,content FROM memory_pack_snapshots").fetchall() == [("t_other", "Keep this note")]

        else:
            assert schema(path) == before_schema
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    installed = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed


def test_invalid_reset_snapshot_rolls_back_the_entire_retirement(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO memory_reset_snapshots(id,user_id,snapshot_json,created_at) VALUES('snap_invalid','u1','invalid','2026-10-19')")
    with pytest.raises(RuntimeError, match="snap_invalid contains invalid JSON"):
        _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='user_glossary'").fetchone() == (1,)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision
        assert db.execute("SELECT snapshot_json FROM memory_reset_snapshots WHERE id='snap_invalid'").fetchone() == ("invalid",)
