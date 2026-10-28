"""Migration 0192 adds the resume checkpoint tables in core only, idempotently."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0193_subagent_resume_checkpoints")
pytestmark = pytest.mark.integration

NEW_OBJECTS = {
    "subagent_resume_checkpoints",
    "subagent_resume_requests",
    "idx_subagent_resume_session",
    "idx_subagent_resume_lineage",
    "idx_subagent_resume_expiry",
    "trg_subagent_resume_session_delete",
}


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_the_tables_only_to_core(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    installed = schema(path)
    if target == "core":
        assert {row[1] for row in set(installed) - set(previous) if not row[1].startswith("sqlite_autoindex")} == NEW_OBJECTS
    else:
        assert installed == previous
    # Existing rows are untouched; the new tables start empty.
    after = snapshot(path)
    assert {name: rows for name, rows in after.items() if not name.startswith("subagent_resume_")} == before
    assert all(rows == [] for name, rows in after.items() if name.startswith("subagent_resume_"))
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed


def test_a_superseded_or_purged_row_cannot_keep_its_transcript(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        row = (
            "rsc_0123456789abcdef0123",
            *("team", "u_1", "ws", "s_1", "t_1", "sub_1", "compute", None, None, "sha256:x", "task", "partial", "step_limit", None, None),
        )
        insert = (
            "INSERT INTO subagent_resume_checkpoints(id,team_id,user_id,workspace_id,session_id,turn_id,run_subagent_id,profile,"
            "agent_id,agent_revision_id,tool_names_hash,task,completion,stop_reason,partial_done,partial_remaining,state,"
            "claimed_by_turn_id,ciphertext,payload_bytes,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'a','b')"
        )
        for state, claimed_by, ciphertext in (("superseded", None, "x"), ("live", None, None), ("claimed", None, "x"), ("bogus", None, "x")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(insert, (*row, state, claimed_by, ciphertext))
        db.execute(insert, (*row, "claimed", "t_2", "x"))
