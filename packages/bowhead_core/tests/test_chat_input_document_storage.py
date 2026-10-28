"""W0 source expansion: real SQLite transactions and legacy digest compatibility."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.chat_references import InputDocument
from bowhead_core.chat_turn_source import ChatTurnUserSource, chat_turn_user_source_projection, snapshot_chat_turn_user_source
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx
from bowhead_core.terminal_protocol import canonical_json_v1
from test_chat_reference_storage import assert_rows_survive_upgrade

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0058_chat_input_document")
NOW = "2026-09-08T00:00:00+00:00"


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _source(*, with_document: bool = True) -> ChatTurnUserSource:
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000001",
                    "resource": {"kind": "skill", "source_kind": "team", "id": "sk_dupont", "version": "sha256:" + "a" * 64},
                    "label": "杜邦分析",
                    "parameters": {"period": "2026Q3", "optional": None},
                },
                {"type": "text", "text": " 分析本季度盈利能力"},
            ],
        }
    )
    return ChatTurnUserSource(
        turn_id="t_1",
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document if with_document else None,
    )


def _seed_turn(path: Path, source: ChatTurnUserSource) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES(?,?,?,?,?)",
            (source.session_id, source.workspace_id, source.user_id, NOW, NOW),
        )
        db.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES(?,?,?,?,?)",
            (source.turn_id, source.session_id, 1, source.user_text, NOW),
        )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    _seed_turn(path, _source())
    return path


async def _read(path: Path, user_id: str = "u_1", workspace_id: str = "ws_1") -> ChatTurnUserSource | None:
    return await SessionStore(str(path)).get_chat_turn_user_source(turn_id="t_1", user_id=user_id, workspace_id=workspace_id)


async def _insert(path: Path, source: ChatTurnUserSource) -> bool:
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        result = await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=NOW)
        await db.commit()
    return result


@pytest.mark.asyncio
async def test_upgrade_preserves_legacy_source_bytes_and_reader(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    source = _source(with_document=False)
    _seed_turn(path, source)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,parts_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("t_1", "s_1", "u_1", "ws_1", source.user_text, "[]", "[]", None, NOW),
        )
        before = db.execute("SELECT * FROM chat_turn_user_sources").fetchone()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT * FROM chat_turn_user_sources").fetchone()
        assert_rows_survive_upgrade({"chat_turn_user_sources": [before]}, {"chat_turn_user_sources": [after]})
        assert db.execute("SELECT input_document_json,document_revision_target_json FROM chat_turn_user_sources").fetchone() == (None, None)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
    await SessionStore(str(path)).initialize()
    assert await _read(path) == source
    expected = {
        "attachment_ids": [],
        "attachment_refs": [],
        "parts": None,
        "session_id": "s_1",
        "turn_id": "t_1",
        "user_id": "u_1",
        "user_text": source.user_text,
        "workspace_id": "ws_1",
    }
    assert canonical_json_v1(chat_turn_user_source_projection(source)) == canonical_json_v1(expected)


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_non_core_targets_remain_noop(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'trg_chat_input_document_%'").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)


@pytest.mark.asyncio
async def test_exact_source_roundtrip_and_retry_do_not_lose_reference_identity(db_path: Path) -> None:
    source = _source()
    assert await _insert(db_path, source)
    assert not await _insert(db_path, source)
    assert await _read(db_path) == source
    assert await _read(db_path, user_id="u_other") is None
    assert await _read(db_path, workspace_id="ws_other") is None
    changed = snapshot_chat_turn_user_source(source)
    changed.input_document.nodes[0].parameters["period"] = "2026Q4"
    with pytest.raises(ValueError, match="source_conflict"):
        await _insert(db_path, changed)
    assert await _read(db_path) == source


@pytest.mark.asyncio
async def test_source_write_obeys_caller_transaction_and_scope(db_path: Path) -> None:
    source = _source()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=NOW)
        assert await _read(db_path) is None
        await db.rollback()
    assert await _read(db_path) is None
    with pytest.raises(ValueError, match="scope_conflict"):
        await _insert(db_path, replace(source, user_id="u_other"))
    assert await _read(db_path) is None


@pytest.mark.parametrize("mutation", ["clear", "parameters", "text", "owner", "replace"])
@pytest.mark.asyncio
async def test_database_rejects_reference_source_overwrite(db_path: Path, mutation: str) -> None:
    source = _source()
    await _insert(db_path, source)
    statements = {
        "clear": "UPDATE chat_turn_user_sources SET input_document_json=NULL WHERE turn_id='t_1'",
        "parameters": "UPDATE chat_turn_user_sources SET input_document_json=json_set(input_document_json,'$.nodes[0].parameters.period','2026Q4') WHERE turn_id='t_1'",
        "text": "UPDATE chat_turn_user_sources SET user_text='changed' WHERE turn_id='t_1'",
        "owner": "UPDATE chat_turn_user_sources SET user_id='u_other' WHERE turn_id='t_1'",
        "replace": "INSERT OR REPLACE INTO chat_turn_user_sources SELECT * FROM chat_turn_user_sources WHERE turn_id='t_1'",
    }
    with sqlite3.connect(db_path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(statements[mutation])
    assert await _read(db_path) == source


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "null",
        '{"schema_version":2,"nodes":[]}',
        '{"schema_version":1,"nodes":{}}',
        '{"schema_version":true,"nodes":[]}',
        '{"schema_version":1,"nodes":[],"grant":true}',
        "not json",
    ],
)
def test_database_rejects_malformed_document_envelope(db_path: Path, raw: str) -> None:
    with sqlite3.connect(db_path) as db, pytest.raises(sqlite3.IntegrityError, match="invalid"):
        db.execute(
            "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,created_at,input_document_json) VALUES(?,?,?,?,?,?,?,?,?)",
            ("t_1", "s_1", "u_1", "ws_1", _source().user_text, "[]", "[]", NOW, raw),
        )


def test_column_drift_rejects_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE chat_turn_user_sources ADD COLUMN input_document_json INTEGER")
    with pytest.raises(RuntimeError, match="column drift"):
        command.upgrade(config, "head")


def test_trigger_drift_rejects_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER trg_chat_input_document_immutable BEFORE UPDATE ON chat_turn_user_sources BEGIN SELECT 1; END")
    with pytest.raises(RuntimeError, match="trigger drift"):
        command.upgrade(config, "head")


@pytest.mark.asyncio
async def test_reader_rejects_unknown_node_instead_of_falling_back_to_text(db_path: Path) -> None:
    # SQL validates the envelope; the codec must validate nested content too.
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,created_at,input_document_json) VALUES(?,?,?,?,?,?,?,?,?)",
            ("t_1", "s_1", "u_1", "ws_1", "fallback", "[]", "[]", NOW, json.dumps({"schema_version": 1, "nodes": [{"type": "future_node"}]})),
        )
    with pytest.raises(ValueError):
        await _read(db_path)


@pytest.mark.unit
def test_source_snapshot_detaches_parameters_and_preserves_full_document_identity() -> None:
    source = _source()
    snapshot = snapshot_chat_turn_user_source(source)
    source.input_document.nodes[0].parameters["period"] = "2026Q4"
    assert snapshot.input_document.nodes[0].parameters["period"] == "2026Q3"
    assert chat_turn_user_source_projection(snapshot)["input_document"] == snapshot.input_document.model_dump(mode="json", exclude_none=True)
    with pytest.raises(ValueError, match="display projection"):
        replace(snapshot, user_text="different label")
    with pytest.raises(ValueError, match="one source message"):
        replace(snapshot, parts=({"text": "first"}, {"text": "second"}))


@pytest.mark.parametrize(("key", "value"), [(5, "not a string key"), ("value", b"not JSON"), ("value", (1, 2)), ("value", {"not JSON"})])
@pytest.mark.asyncio
async def test_mutated_parameter_types_are_rejected_before_source_write(db_path: Path, key: object, value: object) -> None:
    source = _source()
    source.input_document.nodes[0].parameters[key] = value
    with pytest.raises(ValueError):
        await _insert(db_path, source)
    assert await _read(db_path) is None


@pytest.mark.asyncio
async def test_system_agent_document_at_byte_limit_can_be_saved_and_restored(tmp_path: Path) -> None:
    from bowhead_core.chat_references import MAX_DOCUMENT_BYTES, decode_input_document

    raw = {
        "schema_version": 1,
        "nodes": [
            {
                "type": "reference",
                "node_id": "00000000-0000-4000-8000-000000000001",
                "label": "助手",
                "resource": {"kind": "agent", "source_kind": "system", "id": "system:compute", "version": "arv_1"},
                "parameters": {},
            },
            {"type": "text", "text": ""},
        ],
    }
    size = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode())
    raw["nodes"][1]["text"] = "x" * (MAX_DOCUMENT_BYTES - size)
    wire = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    assert len(wire.encode()) == MAX_DOCUMENT_BYTES
    document = decode_input_document(wire)
    source = replace(_source(with_document=False), user_text=document.display_text(), input_document=document)
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    _seed_turn(path, source)
    assert await _insert(path, source)
    assert await _read(path) == source


@pytest.mark.parametrize("isolation_level", [None, ""])
@pytest.mark.asyncio
async def test_reference_source_requires_existing_caller_transaction(db_path: Path, isolation_level: str | None) -> None:
    async with aiosqlite.connect(db_path, isolation_level=isolation_level) as db:
        assert not db.in_transaction
        with pytest.raises(ValueError, match="transaction_required"):
            await _insert_chat_turn_user_source_in_tx(db, source=_source(), created_at=NOW)
        assert not db.in_transaction
    assert await _read(db_path) is None
