"""Strict revision intent and historical source preservation before admission."""

import sqlite3
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.chat_turn_source import chat_turn_user_source_projection, snapshot_chat_turn_user_source
from bowhead_core.document_revision_target import DocumentRevisionTarget
from bowhead_core.session_store import SessionStore
from pydantic import ValidationError
from test_chat_input_document_storage import NOW, _seed_turn, _source
from test_chat_reference_storage import _upgrade, assert_rows_survive_upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0147_document_revision_source")
TARGET = {"kind": "document_revision", "version": 1, "session_id": "s_1", "job_id": "djob_original", "draft_digest": "a" * 64}


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 1.0},
        {"version": "1"},
        {"version": 2},
        {"kind": "agent_draft"},
        {"job_id": "other"},
        {"draft_digest": "A" * 64},
        {"permission": "owner"},
    ],
)
def test_revision_pointer_rejects_ambiguous_or_injected_authority(change):
    with pytest.raises(ValidationError):
        DocumentRevisionTarget.model_validate({**TARGET, **change})


def test_revision_source_keeps_target_and_revalidates_copied_models():
    legacy = _source(with_document=False)
    target = DocumentRevisionTarget.model_validate(TARGET)
    source = replace(legacy, document_revision_target=target)
    assert snapshot_chat_turn_user_source(source) == source
    assert chat_turn_user_source_projection(source) == {**chat_turn_user_source_projection(legacy), "document_revision_target": TARGET}
    assert "document_revision_target" not in chat_turn_user_source_projection(legacy)
    with pytest.raises(ValueError, match="scope or mode"):
        replace(source, session_id="s_other")
    with pytest.raises(ValidationError):
        replace(source, document_revision_target=target.model_copy(update={"version": True}))
    with pytest.raises(ValueError, match="scope or mode"):
        replace(_source(), document_revision_target=target)


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_revision_source_upgrade_preserves_history_and_all_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        source = _source(with_document=False)
        _seed_turn(path, source)
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,created_at) VALUES(?,?,?,?,?,'[]','[]',?)",
                (source.turn_id, source.session_id, source.user_id, source.workspace_id, source.user_text, NOW),
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert_rows_survive_upgrade(before, snapshot(path))
    after = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        if target == "core":
            assert db.execute("SELECT document_revision_target_json FROM chat_turn_user_sources").fetchone() == (None,)
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(
                    "UPDATE chat_turn_user_sources SET document_revision_target_json=?",
                    (DocumentRevisionTarget.model_validate(TARGET).model_dump_json(),),
                )


@pytest.fixture
def revision_db(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, "head", "core")
    _seed_turn(path, _source(with_document=False))
    return path


def _insert_fixture(path, value):
    # Migration/reader fixture, not proof of authorized document admission.
    with sqlite3.connect(path) as db:
        source = _source(with_document=False)
        db.execute(
            "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,created_at,document_revision_target_json) VALUES(?,?,?,?,?,'[]','[]',?,?)",
            (source.turn_id, source.session_id, source.user_id, source.workspace_id, source.user_text, NOW, value),
        )


async def test_revision_reader_preserves_exact_source_and_ownership(revision_db):
    target = DocumentRevisionTarget.model_validate(TARGET)
    _insert_fixture(revision_db, target.model_dump_json())
    store = SessionStore(revision_db)
    expected = replace(_source(with_document=False), document_revision_target=target)
    assert await store.get_chat_turn_user_source(turn_id="t_1", user_id="u_1", workspace_id="ws_1") == expected
    assert await store.get_chat_turn_user_source(turn_id="t_1", user_id="other", workspace_id="ws_1") is None
    assert await store.get_chat_turn_user_source(turn_id="t_1", user_id="u_1", workspace_id="other") is None
    with sqlite3.connect(revision_db) as db:
        for statement in (
            "UPDATE chat_turn_user_sources SET document_revision_target_json=NULL",
            "UPDATE chat_turn_user_sources SET user_text='Changed'",
            "INSERT OR REPLACE INTO chat_turn_user_sources SELECT * FROM chat_turn_user_sources",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)


@pytest.mark.parametrize("value", ['{"version":1}', '{"version":true}', "[]", "invalid"])
def test_revision_sql_rejects_malformed_envelopes(revision_db, value):
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        _insert_fixture(revision_db, value)


def test_partial_revision_source_install_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:1], "invalid sql"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute)
            ),
        )
        with pytest.raises(sqlite3.OperationalError):
            MIGRATION.upgrade()
    assert schema(path) == before
