"""Envelope versioning preserves all existing database bytes and schemas."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_ordinary_lead_continuation import prepare
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0154_ordinary_material_identity")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
@pytest.mark.parametrize(
    "migration",
    [
        MIGRATION,
        import_module("bowhead_core.migrations.versions.0163_ordinary_uncertain_dialogue"),
        import_module("bowhead_core.migrations.versions.0164_ordinary_attachment_reads"),
        import_module("bowhead_core.migrations.versions.0168_ordinary_session_context"),
        import_module("bowhead_core.migrations.versions.0170_ordinary_parallel_helpers"),
    ],
    ids=["material-identity", "uncertain-dialogue", "attachment-reads", "session-context", "parallel-helpers"],
)
def test_identity_envelope_upgrade_preserves_all_targets(tmp_path, target, migration):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, migration.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, migration.revision, target)
    after = snapshot(path)
    assert after == before
    assert schema(path) == old_schema
    _upgrade(path, migration.revision, target)
    assert snapshot(path) == after
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migration.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("contract", ["ordinary-lead-loop.v1", "ordinary-lead-loop.v2"])
async def test_upgrade_preserves_original_encrypted_card_and_reader(ctx, tmp_path, monkeypatch, contract):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint

    _, card, _, checkpoint = await prepare(ctx, monkeypatch, contract=contract)
    path = tmp_path / "old-material.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        restored = await read_ordinary_card_in_tx(db, card_id=card.request_id)
    assert restored.card.resume_context == card.resume_context
    assert reopen_ordinary_checkpoint(restored).encode() == checkpoint.encode()
