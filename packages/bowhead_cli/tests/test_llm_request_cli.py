"""`bowhead llm-request` rebuilds recorded sends exactly, and every decrypting read is audited first."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from bowhead_cli.main import app
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.llm_request_record import OMIT_CHUNK_BYTES, write_request_records
from bowhead_router.invocation import canonical_transport_bytes
from bowhead_router.request_record import RequestChange, RequestRecordEntry
from bowhead_router.usage import UsageContext
from cryptography.fernet import Fernet
from typer.testing import CliRunner

KEY = Fernet.generate_key().decode()
START = datetime(2026, 10, 21, 8, tzinfo=UTC)
FIRST = {"model": "m", "messages": [{"role": "user", "content": "季度营收"}]}
SECOND = {"model": "m", "messages": [*FIRST["messages"], {"role": "assistant", "content": "ok"}], "tools": [{"name": "bi_query"}]}


def _entry(body: dict, *, at: int, subagent_id: str = "sub_1", changes=(), usage_event_id: str | None = None) -> RequestRecordEntry:
    canonical = canonical_transport_bytes(body)
    return RequestRecordEntry(
        usage=UsageContext(team_id="team_1", user_id="u_1", workspace_id="ws_1", turn_id="t_1", reason="turn"),
        provider="p",
        model="m",
        family="openai",
        mode="stream",
        canonical_body=canonical,
        request_digest=hashlib.sha256(canonical).hexdigest(),
        usage_event_id=usage_event_id,
        subagent_id=subagent_id,
        created_at=START + timedelta(seconds=at),
        changes=tuple(changes),
    )


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", KEY)
    path = tmp_path / TARGET_FILES["core"]
    upgrade(f"sqlite:///{path}", "core")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table, values in (("sessions", {"id": "s_1", "deleted_at": None}), ("turns", {"id": "t_1", "session_id": "s_1", "seq": 0})):
            row = dict(values)
            for _cid, name, kind, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
                if name not in row and notnull and default is None:
                    row[name] = 0 if "INT" in (kind or "").upper() else f"{name}_x"
            conn.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    return path


def _seed(path: Path, entries: list[RequestRecordEntry]) -> list[str]:
    async def write() -> None:
        async with aiosqlite.connect(path) as conn:
            await write_request_records(conn, ReferenceCipher(KEY), entries)
            await conn.commit()

    asyncio.run(write())
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT id FROM llm_request_records ORDER BY created_at")]


def _show(path: Path, ref: str):
    return CliRunner().invoke(app, ["llm-request", "show", ref, "--operator", "alice: user report #12", "--db", f"sqlite:///{path}"])


def _audit_rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT action, target_id, payload_json FROM admin_audit_log").fetchall()


def test_show_rebuilds_the_exact_body_with_the_runs_changes_and_audits_it(db: Path) -> None:
    capped = RequestChange(kind="tool_result_capped", detail={"tool_call_id": "c1"})
    dropped = RequestChange(kind="history_turns_dropped", detail={"turns": 1})
    other_run = RequestChange(kind="chat_direct_demoted", detail={"tool_call_ids": ["s9"]})
    ids = _seed(
        db,
        [
            _entry(FIRST, at=0, changes=[capped]),
            _entry(FIRST, at=1, subagent_id="sub_other", changes=[other_run]),
            _entry(SECOND, at=2, changes=[dropped], usage_event_id="lue_2"),
        ],
    )

    result = _show(db, "lue_2")

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert shown["verified"] is True and shown["omitted_chunks"] == []
    assert canonical_transport_bytes(shown["body"]) == canonical_transport_bytes(SECOND)
    assert shown["record"]["id"] == ids[2]
    # The first send's change still applies to the later send of the same run; another run's does not.
    assert [(c["kind"], c["detail"]) for c in shown["changes"]] == [
        ("tool_result_capped", {"tool_call_id": "c1"}),
        ("history_turns_dropped", {"turns": 1}),
    ]
    [(action, target, payload)] = _audit_rows(db)
    assert (action, target) == ("llm_request_record_viewed", ids[2])
    assert json.loads(payload)["operator"] == "alice: user report #12"


def test_a_tampered_chunk_fails_verification(db: Path) -> None:
    [record_id] = _seed(db, [_entry(FIRST, at=0)])
    forged = canonical_transport_bytes({"role": "user", "content": "forged"})
    with sqlite3.connect(db) as conn:
        chunk_hash = conn.execute(
            "SELECT chunk_hash FROM llm_request_chunks WHERE byte_size=?", (len(canonical_transport_bytes(FIRST["messages"][0])),)
        ).fetchone()[0]
        from bowhead_core.chat_reference_storage import ReferenceScope
        from bowhead_core.llm_request_record import CHUNK_PURPOSE

        scope = ReferenceScope(team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id="s_1", policy_epoch=0)
        sealed = ReferenceCipher(KEY).seal(scope=scope, purpose=CHUNK_PURPOSE, identity=f"t_1:{chunk_hash}", content=forged)
        conn.execute("UPDATE llm_request_chunks SET ciphertext=? WHERE chunk_hash=?", (sealed, chunk_hash))

    result = _show(db, record_id)

    assert result.exit_code == 2
    assert json.loads(result.stdout)["verified"] is False


def test_an_omitted_chunk_is_reported_instead_of_verified(db: Path) -> None:
    [record_id] = _seed(db, [_entry({"model": "m", "messages": [{"role": "user", "content": "x" * (OMIT_CHUNK_BYTES + 1)}]}, at=0)])

    result = _show(db, record_id)

    assert result.exit_code == 0
    shown = json.loads(result.stdout)
    assert shown["verified"] is None and len(shown["omitted_chunks"]) == 1
    assert shown["body"]["messages"][0]["_omitted_chunk"] == shown["omitted_chunks"][0]


def test_unknown_reference_exits_without_auditing(db: Path) -> None:
    result = _show(db, "lrq_missing")
    assert result.exit_code == 1
    assert _audit_rows(db) == []


def test_nothing_is_decrypted_when_the_audit_write_fails(db: Path, monkeypatch) -> None:
    [record_id] = _seed(db, [_entry(FIRST, at=0)])
    opened: list[str] = []
    monkeypatch.setattr(ReferenceCipher, "open", lambda self, **kw: opened.append(kw["identity"]))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER audit_down BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(ABORT, 'audit down'); END")

    result = _show(db, record_id)

    assert result.exit_code != 0
    assert opened == []


def test_list_shows_a_turns_sends_without_decrypting(db: Path, monkeypatch) -> None:
    _seed(db, [_entry(FIRST, at=0, changes=[RequestChange(kind="tool_result_capped", detail={})]), _entry(SECOND, at=1)])
    monkeypatch.setattr(ReferenceCipher, "open", lambda self, **kw: pytest.fail("list must not decrypt"))

    result = CliRunner().invoke(app, ["llm-request", "list", "--turn", "t_1", "--db", f"sqlite:///{db}"])

    assert result.exit_code == 0, result.output
    listed = json.loads(result.stdout)
    assert [row["change_count"] for row in listed] == [1, 0]
    assert _audit_rows(db) == []
