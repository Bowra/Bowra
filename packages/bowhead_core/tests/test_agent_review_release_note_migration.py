"""Migration 0191 adds the submission's release note without rewriting candidate control rows."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0191_agent_review_release_note")
pytestmark = pytest.mark.integration


def _columns(path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute("PRAGMA table_info(agent_eval_candidate_control)")]


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_the_review_note_only_to_core(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    installed = schema(path)
    if target == "core":
        assert _columns(path)[-1] == "review_release_note"
        assert {row[1] for row in set(installed) - set(previous)} == {"agent_eval_candidate_control"}
    else:
        assert installed == previous
    assert snapshot(path) == before
    # Idempotent: a second upgrade changes nothing.
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed


def test_the_note_is_bounded(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='agent_eval_candidate_control'").fetchone()[0]
    assert "review_release_note TEXT CHECK(review_release_note IS NULL OR length(review_release_note) BETWEEN 1 AND 2000)" in sql
