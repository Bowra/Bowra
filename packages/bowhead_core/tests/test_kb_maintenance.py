"""Durable claims, bounded physical I/O, and display facts on real files/SQLite."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from bowhead_core.kb.eligibility import KbUseDecision
from bowhead_core.kb.generations import now_iso
from bowhead_core.kb.maintenance import KbMaintenanceStore, cached_source_state
from bowhead_core.kb.source_integrity import MAX_SOURCE_IO, bounded_source_io
from test_kb_generations import source as source

pytestmark = pytest.mark.integration


def manifest(file):
    return {
        key: getattr(file, key)
        for key in ("file_id", "version", "active_generation_id", "team_id", "workspace_id", "scope", "scope_id", "artifact_id")
    }


async def test_job_claim_survives_repeated_interruptions_without_automatic_retry(source):
    jobs = KbMaintenanceStore(source.files._db_path)
    file = await source.files.get(source.fid)
    jid = await jobs.create_job(team_id=file.team_id, workspace_id=file.workspace_id, user_id=file.uploaded_by, items=[manifest(file)])
    for attempt in range(2):
        current = await source.files.get(file.file_id)
        assert await jobs.claim_file(jid, 0, current)
        claimed = await jobs.get_job(jid)
        assert claimed["items"][0]["claimed_version"] == file.version + attempt + 1
        await jobs.pause_interrupted()
        current = await source.files.get(file.file_id)
        assert current.parse_status == "failed" and current.active_generation_id == file.active_generation_id
        assert (await jobs.get_job(jid))["state"] == "paused"
        assert await jobs.resume(jid)
    current = await source.files.get(file.file_id)
    assert await jobs.claim_file(jid, 0, current)
    assert not await jobs.claim_file(jid, 0, current)  # Same version cannot be claimed twice.
    await source.worker.parse_and_index(file.file_id, authorize=AsyncMock(return_value=True))
    done = await source.files.get(file.file_id)
    assert done.active_generation_id != file.active_generation_id
    await jobs.pause_interrupted()
    assert (await source.files.get(file.file_id)).parse_status == "done"


async def test_claim_does_not_follow_file_into_another_space(source):
    jobs = KbMaintenanceStore(source.files._db_path)
    file = await source.files.get(source.fid)
    jid = await jobs.create_job(team_id=file.team_id, workspace_id=file.workspace_id, user_id=file.uploaded_by, items=[manifest(file)])
    import aiosqlite

    async with aiosqlite.connect(source.files._db_path) as db:
        await db.execute("UPDATE kb_files SET workspace_id='ws_other',scope_id='ws_other' WHERE file_id=?", (file.file_id,))
        await db.commit()
    assert not await jobs.claim_file(jid, 0, file)
    assert (await source.files.get(file.file_id)).parse_status == "done"


async def test_observation_cache_is_scoped_to_exact_source_namespace_and_time(source):
    jobs = KbMaintenanceStore(source.files._db_path)
    file = await source.files.get(source.fid)
    await jobs.observe(file, KbUseDecision("eligible", "verified_binding", file.active_generation_id, now_iso()), index_version="model1")
    observed = await source.files.get(file.file_id)
    assert cached_source_state(observed, index_version="model1")["state"] == "eligible"
    for changed in ({"version": file.version + 1}, {"scope_id": "other"}, {"artifact_id": "other"}, {"team_id": "other"}):
        assert cached_source_state(observed.model_copy(update=changed), index_version="model1")["state"] == "unchecked"
    assert cached_source_state(observed, index_version="model2")["state"] == "unchecked"
    raw = json.loads(observed.source_observation_json)
    raw["checked_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    assert (
        cached_source_state(observed.model_copy(update={"source_observation_json": json.dumps(raw)}), index_version="model1")["state"] == "unchecked"
    )
    await jobs.observe(
        file.model_copy(update={"version": file.version + 1}), KbUseDecision("paused", "source_missing", None, now_iso()), index_version="model1"
    )
    assert cached_source_state(await source.files.get(file.file_id), index_version="model1")["state"] == "eligible"
    assert (await jobs.scan_page(limit=1))[0].file_id == file.file_id
    await jobs.checkpoint(file.file_id)
    assert await KbMaintenanceStore(source.files._db_path).scan_page() == []


async def test_timed_out_source_work_keeps_capacity_until_real_io_finishes():
    release = asyncio.Event()
    entered = asyncio.Queue()

    async def blocked():
        entered.put_nowait(True)
        await release.wait()
        return "finished"

    requests = []
    try:
        for _ in range(MAX_SOURCE_IO):
            task = asyncio.create_task(bounded_source_io(blocked))
            await entered.get()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            requests.append(task)
        with pytest.raises(TimeoutError):
            await bounded_source_io(AsyncMock(return_value="should not start"))
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert await bounded_source_io(AsyncMock(return_value="recovered")) == "recovered"


async def test_rebuild_rechecks_access_after_parsing_before_embedding(source, monkeypatch):
    old = await source.files.get(source.fid)
    load = source.worker._load_with_retry
    permitted = True

    async def revoke_after_load(*args, **kwargs):
        nonlocal permitted
        result = await load(*args, **kwargs)
        permitted = False
        return result

    async def authorize(_):
        return permitted

    monkeypatch.setattr(source.worker, "_load_with_retry", revoke_after_load)
    await source.worker.parse_and_index(source.fid, allow_terminal=True, authorize=authorize)
    current = await source.files.get(source.fid)
    assert current.parse_status == "failed"
    assert current.active_generation_id == old.active_generation_id
    assert source.embedder.embed_batch.await_count == 1


async def test_chart_only_report_gets_durable_warning_before_live_event(tmp_path):
    from types import SimpleNamespace

    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.kb.turn_guard import PAUSED_NOTICE, annotate_turn_report

    canvas = CanvasStore(tmp_path / "canvas.db")
    await canvas.initialize()
    report = await canvas.ensure_canvas(session_id="s_chart", workspace_id="ws_test", user_id="u_test")
    await canvas.append_section(canvas_id=report.id, kind="chart", payload={"option": {}}, by_turn_id="t_chart")
    emitted = []

    async def emit(event):
        persisted = await canvas.list_sections_appended_by_turn("t_chart", kinds=["narrative"])
        assert len(persisted) == 1 and PAUSED_NOTICE in persisted[0].payload["markdown"]
        emitted.append(event)

    ctx = SimpleNamespace(canvas_store=canvas, turn_id="t_chart", parent_bus=SimpleNamespace(emit=emit))
    await annotate_turn_report(ctx, PAUSED_NOTICE, paused=True)
    assert len(emitted) == 1
    await annotate_turn_report(ctx, PAUSED_NOTICE, paused=True)
    saved = await CanvasStore(tmp_path / "canvas.db").list_sections_appended_by_turn("t_chart", kinds=["narrative"])
    assert len(saved) == 1 and PAUSED_NOTICE in saved[0].payload["unverified_numbers"]
    assert len(emitted) == 1


async def test_stopping_job_keeps_team_slot_until_current_file_finishes(source):
    jobs = KbMaintenanceStore(source.files._db_path)
    file = await source.files.get(source.fid)
    args = {"team_id": file.team_id, "workspace_id": file.workspace_id, "user_id": file.uploaded_by, "items": [manifest(file)]}
    jid = await jobs.create_job(**args)
    assert await jobs.claim_file(jid, 0, file)
    assert await jobs.state(jid, "cancelling")
    with pytest.raises(ValueError, match="kb_rebuild_busy"):
        await jobs.create_job(**args)
    await jobs.pause_interrupted()
    assert (await jobs.get_job(jid))["state"] == "cancelled"
    assert (await source.files.get(file.file_id)).parse_status == "failed"
    assert await jobs.create_job(**args) != jid


async def test_warning_event_failure_is_not_reported_as_success(tmp_path):
    from types import SimpleNamespace

    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.kb.turn_guard import PAUSED_NOTICE, annotate_turn_report

    canvas = CanvasStore(tmp_path / "canvas.db")
    await canvas.initialize()
    report = await canvas.ensure_canvas(session_id="s_chart", workspace_id="ws_test", user_id="u_test")
    await canvas.append_section(canvas_id=report.id, kind="chart", payload={"option": {}}, by_turn_id="t_chart")
    ctx = SimpleNamespace(canvas_store=canvas, turn_id="t_chart", parent_bus=SimpleNamespace(emit=AsyncMock(side_effect=RuntimeError("bus offline"))))
    with pytest.raises(RuntimeError, match="bus offline"):
        await annotate_turn_report(ctx, PAUSED_NOTICE, paused=True)
    saved = await canvas.list_sections_appended_by_turn("t_chart", kinds=["narrative"])
    assert len(saved) == 1 and PAUSED_NOTICE in saved[0].payload["markdown"]
