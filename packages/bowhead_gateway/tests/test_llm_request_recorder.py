"""The request recorder buffers on the call path and drops, never retries, on failure."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_gateway.llm_request_recorder import LLMRequestRecorder
from bowhead_router.invocation import canonical_transport_bytes
from bowhead_router.request_record import RequestRecordEntry
from bowhead_router.usage import UsageContext
from cryptography.fernet import Fernet

BODY = canonical_transport_bytes({"model": "m", "messages": [{"role": "user", "content": "hi"}]})


def _entry(turn_id: str = "t_1", created_at: datetime | None = None) -> RequestRecordEntry:
    return RequestRecordEntry(
        usage=UsageContext(team_id="team_1", user_id="u_1", workspace_id="ws_1", turn_id=turn_id, reason="turn"),
        provider="p",
        model="m",
        family="openai",
        mode="invoke",
        canonical_body=BODY,
        request_digest=hashlib.sha256(BODY).hexdigest(),
        usage_event_id=None,
        subagent_id=None,
        created_at=created_at or datetime.now(UTC),
    )


def _cipher() -> ReferenceCipher:
    return ReferenceCipher(Fernet.generate_key().decode())


async def test_a_full_buffer_drops_the_oldest_and_counts_it(monkeypatch):
    counted: list[float] = []
    monkeypatch.setattr("bowhead_core.metrics.inc", lambda name, value=1.0, **_: counted.append(value))
    recorder = LLMRequestRecorder("unused.db", _cipher(), max_buffer=100)
    for _ in range(101):
        recorder.record(_entry())
    assert recorder.stats == {"buffered": 100, "written": 0, "dropped": 1}
    assert counted == [1.0]


async def test_a_failed_write_drops_the_batch_instead_of_retrying(tmp_path):
    recorder = LLMRequestRecorder(str(tmp_path / "missing" / "core.db"), _cipher())
    recorder.record(_entry())
    assert await recorder.flush_once() == 0
    assert recorder.stats == {"buffered": 0, "written": 0, "dropped": 1}


def _core_db(tmp_path):
    from bowhead_cli.migrate import upgrade

    path = tmp_path / TARGET_FILES["core"]
    upgrade(f"sqlite:///{path}", "core")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for table, values in (("sessions", {"id": "s_1", "deleted_at": None}), ("turns", {"id": "t_1", "session_id": "s_1", "seq": 0})):
            row = dict(values)
            for _cid, name, kind, notnull, default, _pk in db.execute(f"PRAGMA table_info({table})"):
                if name not in row and notnull and default is None:
                    row[name] = 0 if "INT" in (kind or "").upper() else f"{name}_x"
            db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    return path


async def test_flush_writes_records_for_existing_turns(tmp_path):
    path = _core_db(tmp_path)
    recorder = LLMRequestRecorder(str(path), _cipher())
    recorder.record(_entry())
    recorder.record(_entry("t_deleted"))
    assert await recorder.flush_once() == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT turn_id FROM llm_request_records").fetchall() == [("t_1",)]


@pytest.mark.parametrize("interval", [0.0, 1.0])
def test_interval_is_clamped(interval):
    assert LLMRequestRecorder("unused.db", _cipher(), interval_s=interval)._interval_s == 1.0


async def test_purge_drains_expired_records_in_batches_and_keeps_live_ones(tmp_path, monkeypatch):
    monkeypatch.setattr("bowhead_gateway.llm_request_recorder._PURGE_BATCH", 2)
    path = _core_db(tmp_path)
    recorder = LLMRequestRecorder(str(path), _cipher(), max_buffer=100)
    old = datetime.now(UTC) - timedelta(days=15)
    for _ in range(5):
        recorder.record(_entry(created_at=old))
    recorder.record(_entry())
    assert await recorder.flush_once() == 6

    assert await recorder.purge_expired() == 5
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM llm_request_records").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM llm_request_chunks").fetchone()[0] > 0


async def test_the_flush_loop_purges_at_most_once_per_interval(tmp_path, monkeypatch):
    recorder = LLMRequestRecorder(str(tmp_path / "core.db"), _cipher())
    runs: list[int] = []

    async def purge(now=None):
        runs.append(1)
        return 0

    monkeypatch.setattr(recorder, "purge_expired", purge)
    await recorder._before_flush()
    await recorder._before_flush()
    assert runs == [1]
