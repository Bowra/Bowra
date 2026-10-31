"""Routing-envelope migration preserves old bytes and exact source restrictions."""

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.documents.routing import DocumentRoutingSnapshot
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCheckpoint
from bowhead_core.ordinary_lead_continuation import (
    OrdinaryContinuationUnavailable,
    canonical_json,
    content_hash,
    read_ordinary_card_in_tx,
    reopen_ordinary_checkpoint,
)
from bowhead_core.ordinary_lead_runtime import _validate_document_routing_source
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_ordinary_lead_continuation import prepare
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0155_document_routing")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_routing_envelope_upgrade_is_idempotent_and_preserves_all_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before and schema(path) == old_schema
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("contract", ["ordinary-lead-loop.v1", "ordinary-lead-loop.v2", "ordinary-lead-loop.v3", "ordinary-lead-loop.v4"])
async def test_original_encrypted_card_roundtrip_preserves_version_and_bytes(ctx, tmp_path, monkeypatch, contract):
    routing = (
        DocumentRoutingSnapshot(
            source_hash=content_hash(chat_turn_user_source_projection(ctx.source)),
            mode="template",
            evidence=ctx.source.user_text,
        )
        if contract.endswith(".v4")
        else None
    )
    _, card, _, checkpoint = await prepare(ctx, monkeypatch, contract=contract, document_routing=routing)
    encoded = checkpoint.encode()
    if routing is None:
        assert "document_routing" not in json.loads(encoded)
        assert "collaboration" not in json.loads(encoded)
        assert "knowledge_read" not in json.loads(encoded)
        assert encoded == canonical_json(checkpoint.model_dump(exclude={"document_routing", "collaboration", "knowledge_read"})).encode()
    path = tmp_path / "old-routing.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        record = await read_ordinary_card_in_tx(db, card_id=card.request_id)
    assert record.card.resume_context == card.resume_context
    restored = reopen_ordinary_checkpoint(record)
    assert restored.encode() == encoded and restored.document_routing == routing
    if routing is None:
        with pytest.raises(OrdinaryContinuationUnavailable):
            OrdinaryLeadCheckpoint.decode(canonical_json({**json.loads(encoded), "document_routing": None}).encode())
    else:
        with pytest.raises(OrdinaryContinuationUnavailable):
            _validate_document_routing_source(routing.model_copy(update={"source_hash": "f" * 64}), ctx.source)
        with pytest.raises(OrdinaryContinuationUnavailable):
            restored.model_copy(update={"document_routing": None}).encode()


@pytest.mark.parametrize("field", ["source_hash", "evidence", "inspection_quote", "question_quote", "ordinary_prerequisite"])
async def test_historical_routing_provenance_rejects_changed_source(ctx, field):
    snapshot = DocumentRoutingSnapshot(
        source_hash=content_hash(chat_turn_user_source_projection(ctx.source)),
        mode="template",
        evidence=ctx.source.user_text,
    )
    _validate_document_routing_source(snapshot, ctx.source)
    change = (
        {"mode": "ordinary", "inspection_quote": ctx.source.user_text}
        if field == "ordinary_prerequisite"
        else {field: "f" * 64 if field == "source_hash" else "Quotation not in the original request"}
    )
    with pytest.raises(OrdinaryContinuationUnavailable):
        _validate_document_routing_source(snapshot.model_copy(update=change), ctx.source)
