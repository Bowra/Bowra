"""Queue contention must not terminate the original durable export runner."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest
from bowhead_core.export_runner import DurableLocalExportRunner, ExportProcessorRegistry
from bowhead_core.export_store import ExportCompletion
from test_export_store import _create_job
from test_export_store import env as env

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
async def source_section(env):
    await env.canvases.append_section(canvas_id=env.canvas.id, kind="h1", payload={"text": "Queue contention fixture"})


def _sqlite_contention(tmp_path, kind):
    """Obtain real SQLite result codes without waiting on the export database."""
    path = tmp_path / f"{kind}.db"
    if kind == "locked":
        with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as owner:
            owner.executescript("CREATE TABLE probe(value INTEGER); INSERT INTO probe VALUES(1),(2)")
            with closing(owner.execute("SELECT value FROM probe")) as cursor:
                cursor.fetchone()
                with pytest.raises(sqlite3.OperationalError) as failed:
                    owner.execute("DROP TABLE probe")
                return failed.value
    shared = kind == "locked-shared"
    address = f"file:{path}?cache=shared" if shared else str(path)
    with closing(sqlite3.connect(address, timeout=0, isolation_level=None, uri=shared)) as owner:
        if kind == "busy-snapshot":
            owner.execute("PRAGMA journal_mode=WAL")
        owner.execute("CREATE TABLE probe(value INTEGER)")
        with closing(sqlite3.connect(address, timeout=0, isolation_level=None, uri=shared)) as contender:
            if kind == "busy-snapshot":
                owner.execute("BEGIN")
                owner.execute("SELECT * FROM probe").fetchall()
                contender.execute("INSERT INTO probe VALUES(1)")
                with pytest.raises(sqlite3.OperationalError) as failed:
                    owner.execute("INSERT INTO probe VALUES(2)")
            else:
                owner.execute("BEGIN IMMEDIATE")
                with pytest.raises(sqlite3.OperationalError) as failed:
                    contender.execute("BEGIN IMMEDIATE")
            return failed.value


def _runner(env, monkeypatch):
    processed = []
    completed = asyncio.Event()
    registry = ExportProcessorRegistry()

    async def process(job, context):
        processed.append((job.id, job.attempt))
        for phase in ("normalizing", "materializing", "planning", "rendering", "validating", "persisting"):
            await context.advance(phase)
        return ExportCompletion(
            artifact_id="art_contention_output",
            manifest_artifact_id="art_contention_manifest",
            filename="recovered.pptx",
            size=42,
            quality_status="clean",
        )

    registry.register(fmt="pptx", profile="full_report", handler=process)
    runner = DurableLocalExportRunner(
        store=env.exports,
        processor=registry,
        worker_id="contention-runner",
        poll_interval_seconds=60,
        heartbeat_interval_seconds=30,
        lease_seconds=120,
    )
    execute = runner._execute

    async def observe(job):
        await execute(job)
        completed.set()

    monkeypatch.setattr(runner, "_execute", observe)
    return runner, processed, completed


async def _event_before_loop_exit(event, loop):
    waiter = asyncio.create_task(event.wait())
    try:
        finished, _ = await asyncio.wait((waiter, loop), timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if loop in finished:
            await loop
            raise AssertionError("The runner exited before reaching the expected queue boundary")
        assert waiter in finished
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("stage", ["fail_unclaimable_queued_jobs", "claim_next"])
@pytest.mark.parametrize("kind", ["busy", "locked", "busy-snapshot", "locked-shared"])
async def test_runner_recovers_transient_queue_contention(env, monkeypatch, tmp_path, stage, kind):
    created = await _create_job(env, key="queue-contention")
    runner, processed, completed = _runner(env, monkeypatch)
    error = _sqlite_contention(tmp_path, kind)
    assert error.sqlite_errorcode & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    original = getattr(env.exports, stage)
    calls = 0

    async def contend_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return await original(**kwargs)

    monkeypatch.setattr(env.exports, stage, contend_once)
    waiting = asyncio.Event()
    wait_for_work = runner._wait_for_work

    async def observe_wait():
        waiting.set()
        await wait_for_work()

    monkeypatch.setattr(runner, "_wait_for_work", observe_wait)
    await runner.start()
    loop = runner._loop_task
    try:
        await _event_before_loop_exit(waiting, loop)
        queued = await env.exports.get_job(created.job.id)
        assert queued.status == "queued" and queued.attempt == 0
        assert calls == 1 and processed == [] and not loop.done()
        assert runner.is_available_for("pptx", "full_report")
        await runner.enqueue(created.job.id)
        await _event_before_loop_exit(completed, loop)
        done = await env.exports.get_job(created.job.id)
        assert done.status == "done" and done.attempt == 1
        assert runner._loop_task is loop and not loop.done()
        assert processed == [(created.job.id, 1)]
        events = await env.exports.list_events(created.job.id)
        assert [event.event_type for event in events].count("claimed") == 1
        assert not any(event.event_type == "reclaimed" for event in events)
    finally:
        await runner.stop(grace_seconds=0)


@pytest.mark.parametrize("stage", ["fail_unclaimable_queued_jobs", "claim_next"])
@pytest.mark.parametrize("action", ["stop", "cancel"])
async def test_stop_and_cancellation_leave_queue_contention_wait(env, monkeypatch, tmp_path, stage, action):
    created = await _create_job(env, key="stop-contention")
    runner, processed, _ = _runner(env, monkeypatch)
    error = _sqlite_contention(tmp_path, "busy")
    calls = 0

    async def always_busy(**kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(env.exports, stage, always_busy)
    waiting = asyncio.Event()
    wait_for_work = runner._wait_for_work

    async def observe_wait():
        waiting.set()
        await wait_for_work()

    monkeypatch.setattr(runner, "_wait_for_work", observe_wait)
    await runner.start()
    loop = runner._loop_task
    try:
        await _event_before_loop_exit(waiting, loop)
        if action == "cancel":
            loop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop
        await asyncio.wait_for(runner.stop(grace_seconds=0), timeout=2)
        assert loop.done() and not runner.is_available_for("pptx", "full_report")
        queued = await env.exports.get_job(created.job.id)
        assert queued.status == "queued" and queued.attempt == 0
        assert calls == 1 and processed == []
    finally:
        await runner.stop(grace_seconds=0)


@pytest.mark.parametrize("stage", ["fail_unclaimable_queued_jobs", "claim_next"])
@pytest.mark.parametrize("kind", ["schema", "uncoded-lock-message", "non-sqlite"])
async def test_non_contention_errors_terminate_runner_instead_of_reporting_healthy(env, monkeypatch, stage, kind):
    runner, processed, _ = _runner(env, monkeypatch)
    if kind == "schema":
        error = sqlite3.OperationalError("No such synthetic table")
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    elif kind == "uncoded-lock-message":
        error = sqlite3.OperationalError("database is locked")
    else:
        error = RuntimeError("Synthetic queue failure")

    async def fail(**kwargs):
        raise error

    monkeypatch.setattr(env.exports, stage, fail)
    await runner.start()
    loop = runner._loop_task
    try:
        with pytest.raises(type(error)) as failed:
            await asyncio.wait_for(asyncio.shield(loop), timeout=2)
        assert failed.value is error
        assert not runner.is_available_for("pptx", "full_report") and processed == []
    finally:
        await runner.stop(grace_seconds=0)


async def test_claim_result_contention_does_not_replay_a_durable_owned_attempt(env, monkeypatch, tmp_path):
    unknown = await _create_job(env, key="durable-claim-result-unknown")
    runner, processed, completed = _runner(env, monkeypatch)
    error = _sqlite_contention(tmp_path, "busy")
    claim = env.exports.claim_next
    lost_result = asyncio.Event()

    async def lose_first_result(**kwargs):
        job = await claim(**kwargs)
        if not lost_result.is_set():
            assert job.id == unknown.job.id
            lost_result.set()
            raise error
        return job

    monkeypatch.setattr(env.exports, "claim_next", lose_first_result)
    waiting = asyncio.Event()
    wait_for_work = runner._wait_for_work

    async def observe_wait():
        waiting.set()
        await wait_for_work()

    monkeypatch.setattr(runner, "_wait_for_work", observe_wait)
    await runner.start()
    loop = runner._loop_task
    try:
        await _event_before_loop_exit(waiting, loop)
        owned = await env.exports.get_job(unknown.job.id)
        assert owned.status == "running" and owned.attempt == 1 and owned.worker_id == runner.worker_id
        normal = await _create_job(env, key="next-normal-job")
        await runner.enqueue(normal.job.id)
        await _event_before_loop_exit(completed, loop)
        assert processed == [(normal.job.id, 1)]
        retained = await env.exports.get_job(unknown.job.id)
        assert retained.status == "running" and retained.attempt == 1
        assert [event.event_type for event in await env.exports.list_events(unknown.job.id)] == ["queued", "claimed"]
    finally:
        await runner.stop(grace_seconds=0)


async def test_processing_contention_is_not_replayed_as_a_queue_retry(env, monkeypatch, tmp_path, caplog):
    created = await _create_job(env, key="processing-contention")
    runner, processed, completed = _runner(env, monkeypatch)
    error = _sqlite_contention(tmp_path, "busy")

    async def process_once(job, context):
        processed.append((job.id, job.attempt))
        await context.advance("normalizing")
        raise error

    registry = ExportProcessorRegistry()
    registry.register(fmt="pptx", profile="full_report", handler=process_once)
    runner.processor = registry
    await runner.start()
    loop = runner._loop_task
    try:
        await _event_before_loop_exit(completed, loop)
        failed = await env.exports.get_job(created.job.id)
        assert failed.status == "failed" and failed.attempt == 1
        assert failed.error_code == "renderer_failed"
        assert processed == [(created.job.id, 1)]
        assert not any("canvas_export.queue_contention" in record.message for record in caplog.records)
        assert [event.event_type for event in await env.exports.list_events(created.job.id)] == ["queued", "claimed", "phase_changed", "failed"]
    finally:
        await runner.stop(grace_seconds=0)
