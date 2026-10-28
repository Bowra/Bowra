"""知识库体验整治 FR1：解析收敛（启动恢复上限、整体时限、卡住重试的独占领取）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from bowhead_core.kb import worker as worker_module
from bowhead_core.kb.embedder import EMBED_DIM
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.parse_recovery import (
    MAX_PARSE_RUNS,
    STUCK_AFTER_S,
    heartbeat_iso,
    recover_unfinished_parses,
)
from bowhead_core.kb.worker import KbWorker

pytestmark = pytest.mark.unit


@pytest.fixture
async def store(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    s = KbFilesStore(tmp_path / "kb_files.db")
    await s.initialize()
    return s


async def _make(store: KbFilesStore, tmp_path, name: str = "a.md", **overrides) -> str:
    path = tmp_path / name
    path.write_text("# 标题\n\n第一段内容。\n\n## 小节\n\n第二段内容。\n", encoding="utf-8")
    fields = {
        "artifact_id": str(path),
        "filename": name,
        "mime_type": "text/markdown",
        "size_bytes": path.stat().st_size,
        "scope": "personal",
        "scope_id": "user_a",
        "workspace_id": "ws_1",
        "team_id": "team_1",
        "uploaded_by": "user_a",
    }
    fields.update(overrides)
    return await store.create(**fields)


async def _force(store: KbFilesStore, file_id: str, **cols) -> None:
    assignments = ", ".join(f"{k} = ?" for k in cols)
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(f"UPDATE kb_files SET {assignments} WHERE file_id = ?", (*cols.values(), file_id))
        await db.commit()


def _ago(seconds: int) -> str:
    return heartbeat_iso(datetime.now(tz=UTC) - timedelta(seconds=seconds))


def _worker(store: KbFilesStore) -> KbWorker:
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.1] * EMBED_DIM] * len(texts))
    added: dict[str, int] = {}

    async def _add(_file, chunks) -> None:
        added["n"] = len(chunks)

    chroma = MagicMock()
    chroma.hard_delete_file = AsyncMock(return_value=0)
    chroma.add_chunks = AsyncMock(side_effect=_add)
    chroma.count_chunks = AsyncMock(side_effect=lambda _file: added.get("n", 0))
    return KbWorker(files_store=store, store=chroma, embedder=embedder)


class _RecordingWorker:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def parse_and_index_many(self, file_ids: list[str]) -> None:
        self.batches.append(list(file_ids))


# ─── Startup recovery ──────────────────────────────────────────


async def test_startup_recovery_requeues_orphans_and_caps_repeat_interruptions(store, tmp_path) -> None:
    boot_at = heartbeat_iso()
    stale = _ago(60)
    orphan = await _make(store, tmp_path, "orphan.md")
    await _force(store, orphan, parse_status="embedding", parse_attempts=1, parse_heartbeat_at=stale)
    never_started = await _make(store, tmp_path, "pending.md")
    await _force(store, never_started, parse_heartbeat_at=None)  # Premigration pending row.
    exhausted = await _make(store, tmp_path, "exhausted.md")
    await _force(store, exhausted, parse_status="parsing", parse_attempts=MAX_PARSE_RUNS, parse_heartbeat_at=stale)
    awaiting = await _make(store, tmp_path, "awaiting.md", initial_parse_status="awaiting_upload")
    await _force(store, awaiting, parse_heartbeat_at=stale)
    deleted = await _make(store, tmp_path, "deleted.md")
    await _force(store, deleted, parse_status="parsing", parse_heartbeat_at=stale, soft_deleted_at=stale)
    # Rows updated after this process startup (new uploads already running) are not orphaned.
    live = await _make(store, tmp_path, "live.md")
    await _force(store, live, parse_status="parsing", parse_attempts=1, parse_heartbeat_at=_ago(-5))

    recording = _RecordingWorker()

    async def resolve(team_id: str):
        return recording if team_id == "team_1" else None

    result = await recover_unfinished_parses(store, resolve, started_before=boot_at)
    await asyncio.gather(*result.tasks)

    assert (result.requeued, result.interrupted, result.unavailable) == (2, 1, 0)
    assert recording.batches == [[orphan, never_started]] or recording.batches == [[never_started, orphan]]
    assert (await store.get(orphan)).parse_status == "queued"
    capped = await store.get(exhausted)
    assert capped.parse_status == "failed"
    assert capped.parse_error_code == "interrupted"
    assert (await store.get(awaiting)).parse_status == "awaiting_upload"
    assert (await store.get(deleted)).parse_status == "parsing"
    assert (await store.get(live)).parse_status == "parsing"


async def test_recovered_run_counts_toward_the_cap(store, tmp_path) -> None:
    """恢复后的那次运行本身计数；再被打断一次就收敛为失败，而不是无限重排。"""
    fid = await _make(store, tmp_path)
    worker = _worker(store)
    assert await store.claim_parse_start(fid)  # Process dies after first claim.
    await _force(store, fid, parse_heartbeat_at=_ago(60))

    first = await recover_unfinished_parses(store, AsyncMock(return_value=_RecordingWorker()), started_before=heartbeat_iso())
    assert first.requeued == 1
    assert await store.claim_parse_start(fid)  # Recovered run interrupted again.
    await _force(store, fid, parse_heartbeat_at=_ago(60))

    second = await recover_unfinished_parses(store, AsyncMock(return_value=worker), started_before=heartbeat_iso())
    assert (second.requeued, second.interrupted) == (0, 1)
    assert (await store.get(fid)).parse_error_code == "interrupted"


async def test_startup_recovery_without_team_runtime_fails_as_service_unavailable(store, tmp_path) -> None:
    fid = await _make(store, tmp_path)
    await _force(store, fid, parse_status="queued", parse_heartbeat_at=_ago(60))

    result = await recover_unfinished_parses(store, AsyncMock(return_value=None), started_before=heartbeat_iso())

    assert result.unavailable == 1
    dto = await store.get(fid)
    assert (dto.parse_status, dto.parse_error_code) == ("failed", "service_unavailable")


# ─── Overall deadline ──────────────────────────────────────────


async def test_parse_exceeding_overall_timeout_is_marked_failed(store, tmp_path, monkeypatch) -> None:
    fid = await _make(store, tmp_path)
    worker = _worker(store)
    monkeypatch.setattr(worker_module, "PARSE_TIMEOUT_S", 0.05)

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(worker, "_load_with_retry", _hang)
    await worker.parse_and_index(fid)

    dto = await store.get(fid)
    assert dto.parse_status == "failed"
    assert dto.parse_error_code == "timeout"
    assert dto.parse_stuck is False


# ─── Retry: failed/stuck retriable, repeated clicks start once ───


async def test_reparse_claim_is_exclusive_for_stuck_file_and_refuses_live_one(store, tmp_path) -> None:
    stuck = await _make(store, tmp_path, "stuck.md")
    await _force(store, stuck, parse_status="chunking", parse_attempts=2, parse_heartbeat_at=_ago(STUCK_AFTER_S + 60))
    assert (await store.get(stuck)).parse_stuck is True

    results = await asyncio.gather(*(store.requeue_for_reparse(stuck) for _ in range(2)))
    assert sorted(results) == [False, True]
    dto = await store.get(stuck)
    assert (dto.parse_status, dto.parse_stuck) == ("queued", False)

    running = await _make(store, tmp_path, "running.md")
    assert await store.claim_parse_start(running)
    assert await store.requeue_for_reparse(running) is False
    assert (await store.get(running)).parse_status == "parsing"


async def test_failed_file_reparse_runs_exactly_once_on_double_enqueue(store, tmp_path) -> None:
    fid = await _make(store, tmp_path)
    await _force(store, fid, parse_status="failed", parse_error="timeout: exceeded")
    assert await store.requeue_for_reparse(fid) is True
    assert await store.requeue_for_reparse(fid) is False

    worker = _worker(store)
    loads = 0
    real_load = worker._load_with_retry

    async def _counting_load(*args, **kwargs):
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.05)
        return await real_load(*args, **kwargs)

    worker._load_with_retry = _counting_load  # type: ignore[method-assign]
    await asyncio.gather(worker.parse_and_index(fid), worker.parse_and_index(fid))

    dto = await store.get(fid)
    assert loads == 1
    assert (dto.parse_status, dto.parse_error_code) == ("done", None)
