"""Request bodies split into Turn-scoped chunks and rebuild to the exact recorded digest."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.llm_request_record import (
    CHANGE_PURPOSE,
    CHUNK_PURPOSE,
    OMIT_CHUNK_BYTES,
    purge_expired_request_records,
    rebuild_body,
    split_body,
    write_request_records,
)
from bowhead_router.invocation import canonical_transport_bytes
from bowhead_router.request_record import RequestChange, RequestRecordEntry
from bowhead_router.usage import UsageContext
from cryptography.fernet import Fernet
from test_chat_reference_storage import _upgrade

OPENAI = {
    "model": "deepseek-v4-pro",
    "temperature": 0.2,
    "messages": [
        {"role": "system", "content": "Rules"},
        {"role": "user", "content": "季度营收"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bi_query", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "rows"},
    ],
    "tools": [{"type": "function", "function": {"name": "bi_query", "parameters": {"type": "object"}}}],
    "stream": True,
    "stream_options": {"include_usage": True},
}
ANTHROPIC = {
    "model": "claude",
    "max_tokens": 4000,
    "system": [{"type": "text", "text": "Rules", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "QUJD"}}, {"type": "text", "text": "看图"}]}],
    "tools": [{"name": "add_chart", "input_schema": {"type": "object"}}],
}
RESPONSES = {"model": "gpt", "input": [{"role": "user", "content": "hi"}, {"type": "function_call_output", "call_id": "c1", "output": "ok"}]}
EMPTY_HISTORY = {"model": "m", "messages": []}


def _rebuild(canonical: bytes) -> bytes:
    return rebuild_body((chunk.field, json.loads(chunk.data)) for chunk in split_body(canonical))


@pytest.mark.parametrize("body", [OPENAI, ANTHROPIC, RESPONSES, EMPTY_HISTORY], ids=["openai", "anthropic", "responses", "empty"])
def test_split_and_rebuild_reproduce_the_canonical_body(body):
    canonical = canonical_transport_bytes(body)
    assert _rebuild(canonical) == canonical
    assert hashlib.sha256(_rebuild(canonical)).hexdigest() == hashlib.sha256(canonical).hexdigest()


def test_messages_become_one_chunk_each_in_order():
    fields = [chunk.field for chunk in split_body(canonical_transport_bytes(OPENAI))]
    assert fields == ["_header", "tools", "messages", "messages", "messages", "messages"]


def _usage(turn_id: str = "t_1") -> UsageContext:
    return UsageContext(team_id="team_1", user_id="u_1", workspace_id="ws_1", turn_id=turn_id, reason="turn")


def _entry(body: dict, *, turn_id: str = "t_1", changes=(), created_at: datetime = datetime(2026, 10, 21, tzinfo=UTC)) -> RequestRecordEntry:
    canonical = canonical_transport_bytes(body)
    return RequestRecordEntry(
        usage=_usage(turn_id),
        provider="p",
        model="m",
        family="openai",
        mode="stream",
        canonical_body=canonical,
        request_digest=hashlib.sha256(canonical).hexdigest(),
        usage_event_id=None,
        subagent_id="sub_1",
        created_at=created_at,
        changes=tuple(changes),
    )


def _insert_minimal(db: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    row = dict(values)
    for _cid, name, kind, notnull, default, _pk in db.execute(f"PRAGMA table_info({table})"):
        if name in row or not notnull or default is not None:
            continue
        row[name] = 0 if "INT" in (kind or "").upper() else f"{name}_x"
    db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))


@pytest.fixture
def core_db(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, "head", "core")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        _insert_minimal(db, "sessions", {"id": "s_1", "deleted_at": None})
        _insert_minimal(db, "turns", {"id": "t_1", "session_id": "s_1", "seq": 0})
        _insert_minimal(db, "turns", {"id": "t_2", "session_id": "s_1", "seq": 1})
    return path


async def _write(path, cipher, entries) -> int:
    async with aiosqlite.connect(path) as db:
        written = await write_request_records(db, cipher, entries)
        await db.commit()
    return written


async def test_records_store_encrypted_chunks_deduplicated_within_the_turn(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    first = dict(OPENAI, messages=OPENAI["messages"][:2])
    second = OPENAI

    assert await _write(core_db, cipher, [_entry(first), _entry(second)]) == 2

    with sqlite3.connect(core_db) as db:
        db.row_factory = sqlite3.Row
        records = db.execute("SELECT * FROM llm_request_records ORDER BY rowid").fetchall()
        chunks = {row["chunk_hash"]: row for row in db.execute("SELECT * FROM llm_request_chunks")}
    # The second send repeats the first two messages; each distinct chunk is stored once per Turn.
    second_refs = json.loads(records[1]["chunk_hashes_json"])
    first_refs = json.loads(records[0]["chunk_hashes_json"])
    assert {ref["h"] for ref in first_refs} <= set(chunks)
    assert len(chunks) == len({ref["h"] for ref in first_refs} | {ref["h"] for ref in second_refs})
    assert records[1]["session_id"] == "s_1" and records[1]["subagent_id"] == "sub_1"
    assert records[1]["expires_at"] == "2026-11-04T00:00:00+00:00"

    scope = ReferenceScope(team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id="s_1", policy_epoch=0)
    decoded = [
        (ref["f"], json.loads(cipher.open(scope=scope, purpose=CHUNK_PURPOSE, identity=f"t_1:{ref['h']}", ciphertext=chunks[ref["h"]]["ciphertext"])))
        for ref in second_refs
    ]
    assert hashlib.sha256(rebuild_body(decoded)).hexdigest() == records[1]["request_digest"]


async def test_large_chunks_keep_only_hash_and_size(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    big = {"model": "m", "messages": [{"role": "user", "content": "x" * (OMIT_CHUNK_BYTES + 1)}]}
    await _write(core_db, cipher, [_entry(big)])
    with sqlite3.connect(core_db) as db:
        rows = db.execute("SELECT omitted, ciphertext IS NULL, byte_size FROM llm_request_chunks ORDER BY byte_size DESC").fetchall()
    assert rows[0][0] == 1 and rows[0][1] == 1 and rows[0][2] > OMIT_CHUNK_BYTES
    assert rows[1][0] == 0


async def test_change_records_are_encrypted_and_linked(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    change = RequestChange(kind="tool_result_capped", detail={"tool_call_id": "c1", "full_bytes": 900})
    await _write(core_db, cipher, [_entry(OPENAI, changes=[change])])
    with sqlite3.connect(core_db) as db:
        record_id = db.execute("SELECT id FROM llm_request_records").fetchone()[0]
        change_id, linked, kind, ciphertext = db.execute("SELECT id, record_id, kind, detail_ciphertext FROM llm_request_changes").fetchone()
    assert (linked, kind) == (record_id, "tool_result_capped")
    scope = ReferenceScope(team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id="s_1", policy_epoch=0)
    assert json.loads(cipher.open(scope=scope, purpose=CHANGE_PURPOSE, identity=change_id, ciphertext=ciphertext)) == {
        "full_bytes": 900,
        "tool_call_id": "c1",
    }


async def test_entries_of_deleted_turns_are_skipped(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    assert await _write(core_db, cipher, [_entry(OPENAI, turn_id="t_gone")]) == 0
    with sqlite3.connect(core_db) as db:
        assert db.execute("SELECT count(*) FROM llm_request_records").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM llm_request_chunks").fetchone() == (0,)


def _counts(path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("llm_request_records", "llm_request_chunks", "llm_request_changes")
        }


async def test_purge_deletes_expired_records_their_changes_and_turn_chunks(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    old = datetime(2026, 10, 1, tzinfo=UTC)
    change = RequestChange(kind="tool_result_capped", detail={"tool_call_id": "c1"})
    await _write(core_db, cipher, [_entry(OPENAI, turn_id="t_1", created_at=old, changes=[change]), _entry(ANTHROPIC, turn_id="t_2")])
    kept = _counts(core_db)

    async with aiosqlite.connect(core_db) as db:
        assert await purge_expired_request_records(db, datetime(2026, 10, 21, tzinfo=UTC), limit=100) == 1
        await db.commit()

    with sqlite3.connect(core_db) as db:
        assert db.execute("SELECT DISTINCT turn_id FROM llm_request_records").fetchall() == [("t_2",)]
        assert db.execute("SELECT DISTINCT turn_id FROM llm_request_chunks").fetchall() == [("t_2",)]
    assert _counts(core_db)["llm_request_changes"] == 0
    assert _counts(core_db)["llm_request_chunks"] < kept["llm_request_chunks"]


async def test_purge_keeps_chunks_a_live_record_of_the_same_turn_still_uses(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    first = dict(OPENAI, messages=OPENAI["messages"][:2])
    await _write(
        core_db, cipher, [_entry(first, created_at=datetime(2026, 10, 1, tzinfo=UTC)), _entry(OPENAI, created_at=datetime(2026, 10, 15, tzinfo=UTC))]
    )
    chunks_before = _counts(core_db)["llm_request_chunks"]

    async with aiosqlite.connect(core_db) as db:
        assert await purge_expired_request_records(db, datetime(2026, 10, 21, tzinfo=UTC), limit=100) == 1
        await db.commit()

    assert _counts(core_db)["llm_request_chunks"] == chunks_before
    with sqlite3.connect(core_db) as db:
        db.row_factory = sqlite3.Row
        record = db.execute("SELECT * FROM llm_request_records").fetchone()
        stored = {row["chunk_hash"]: row["ciphertext"] for row in db.execute("SELECT * FROM llm_request_chunks")}
    scope = ReferenceScope(team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id="s_1", policy_epoch=0)
    decoded = [
        (ref["f"], json.loads(cipher.open(scope=scope, purpose=CHUNK_PURPOSE, identity=f"t_1:{ref['h']}", ciphertext=stored[ref["h"]])))
        for ref in json.loads(record["chunk_hashes_json"])
    ]
    assert hashlib.sha256(rebuild_body(decoded)).hexdigest() == record["request_digest"]


async def test_purge_honours_the_batch_limit_oldest_first(core_db):
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await _write(core_db, cipher, [_entry(dict(OPENAI, temperature=i / 10), created_at=start + timedelta(days=i)) for i in range(3)])

    async with aiosqlite.connect(core_db) as db:
        assert await purge_expired_request_records(db, datetime(2026, 10, 21, tzinfo=UTC), limit=2) == 2
        await db.commit()

    with sqlite3.connect(core_db) as db:
        assert db.execute("SELECT created_at FROM llm_request_records").fetchall() == [((start + timedelta(days=2)).isoformat(),)]
