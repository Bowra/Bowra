"""V2 export snapshot/pins/job atomicity and idempotency contracts."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chart_style_export import build_chart_style_export_theme_v2
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    actual_surface_projection,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.chart_style_v2 import (
    built_in_style_projection,
    chart_style_policy_etag_v2,
)
from bowhead_core.export_assets import list_snapshot_asset_pins
from bowhead_core.export_copy_contract import CURRENT_MANIFEST_PRODUCER, output_producer_for
from bowhead_core.export_runner import (
    DurableLocalExportRunner,
    ExportExecutionContext,
    ExportProcessingError,
    ExportProcessorRegistry,
)
from bowhead_core.export_snapshot import (
    ExportSnapshotCandidate,
    ExportSnapshotRequest,
    ExportSnapshotStore,
    SnapshotError,
)
from bowhead_core.export_store import (
    ExportCompletion,
    ExportJobV2,
    ExportStore,
    ExportStoreError,
    compute_export_request_sha256,
)
from bowhead_core.print_theme import PRINT_PAPER, current_print_theme_snapshot
from bowhead_core.session_store import SessionStore

from bowhead_core import canvas_export as legacy_export

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bowhead_core.canvas_store import Canvas

_PPTX_V1_TARGET = (("pptx", "full_report", 1),)


@dataclass(frozen=True)
class ExportEnvironment:
    path: Path
    sessions: SessionStore
    canvases: CanvasStore
    snapshots: ExportSnapshotStore
    artifacts: ArtifactStore
    exports: ExportStore
    canvas: Canvas


def _request() -> ExportSnapshotRequest:
    return ExportSnapshotRequest(
        requested_by="u_owner",
        team_id="team_1",
        fmt="pptx",
        profile="full_report",
        parity_contract="full_content_v1",
        locale="zh-CN",
        timezone="Asia/Singapore",
        client_surface_receipt_sha256="sha256:receipt",
        theme={"kind": "default"},
        options={"include_appendix": True},
    )


def _request_hash(*, include_appendix: bool = True) -> str:
    return compute_export_request_sha256(
        {
            "format": "pptx",
            "profile": "full_report",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "options": {"include_appendix": include_appendix},
        }
    )


def _v2_theme() -> dict:
    print_theme = current_print_theme_snapshot()
    style = built_in_style_projection("bowhead-editorial")
    snapshot = style["resolvedPresetSnapshot"]
    return build_chart_style_export_theme_v2(
        {
            "schemaVersion": 2,
            "registryVersion": 0,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
            "style": style,
            "selection": {
                "source": "product",
                "savedStyleId": None,
                "unavailableReason": None,
            },
            "accessibility": {
                "contrast": "standard",
                "colorVision": "standard",
                "source": {
                    "contrast": "product",
                    "colorVision": "product",
                },
            },
            "modeColors": {
                "light": actual_surface_projection(
                    snapshot,
                    mode="light",
                    surface=PRINT_PAPER,
                    color_scheme_checksum=print_theme["checksum"],
                ),
                "dark": actual_surface_projection(
                    snapshot,
                    mode="dark",
                    surface=DARK_STANDARD_SURFACE,
                    color_scheme_checksum=print_theme["checksum"],
                ),
            },
            "teamPolicy": {"etag": chart_style_policy_etag_v2(None)},
        },
        print_theme=print_theme,
    )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ExportEnvironment]:
    path = tmp_path / "export_store.db"
    sessions = SessionStore(path)
    canvases = CanvasStore(path)
    snapshots = ExportSnapshotStore(path)
    artifacts = ArtifactStore(
        db_path=str(path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await sessions.initialize()
    await canvases.initialize()
    await snapshots.initialize()
    await artifacts.initialize()
    await sessions.ensure_session(
        session_id="s_export",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    canvas = await canvases.ensure_canvas(
        session_id="s_export",
        workspace_id="ws_1",
        user_id="u_owner",
        title="Atomic export",
    )
    old_db_path = legacy_export._db_path
    await legacy_export.init_export_jobs(str(path))
    async with open_shared_ctx(str(path)) as db:
        await db.execute("ALTER TABLE canvas_export_jobs ADD COLUMN renderer_contract_version INTEGER NOT NULL DEFAULT 1")
        await db.commit()
    exports = ExportStore(path, snapshots)
    await exports.initialize()
    try:
        yield ExportEnvironment(
            path=path,
            sessions=sessions,
            canvases=canvases,
            snapshots=snapshots,
            artifacts=artifacts,
            exports=exports,
            canvas=canvas,
        )
    finally:
        legacy_export._jobs.clear()
        legacy_export._db_path = old_db_path


async def _candidate(env: ExportEnvironment) -> ExportSnapshotCandidate:
    return await env.snapshots.build_candidate(
        canvas_id=env.canvas.id,
        request=_request(),
    )


async def _create_job(
    env: ExportEnvironment,
    *,
    key: str,
    candidate: ExportSnapshotCandidate | None = None,
):
    selected = candidate or await _candidate(env)
    return await env.exports.create_export_job(
        candidate=selected,
        expected_projection_token=selected.content_projection.projection_token,
        expected_surface_token=selected.surface_projection.surface_token,
        idempotency_key=key,
        request_sha256=_request_hash(),
    )


async def _count_rows(env: ExportEnvironment, table: str) -> int:
    allowed = {
        "canvas_export_jobs",
        "canvas_export_snapshots",
        "canvas_export_snapshot_assets",
    }
    if table not in allowed:
        raise ValueError("unsupported test table")
    async with open_shared_ctx(str(env.path)) as db, db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _wait_for_status(
    store: ExportStore,
    job_id: str,
    expected: str,
) -> None:
    async with asyncio.timeout(2):
        while True:
            job = await store.get_job(job_id)
            if job is not None and job.status == expected:
                return
            await asyncio.sleep(0.01)


async def _claim_to_persisting(
    env: ExportEnvironment,
    *,
    job_id: str,
    worker_id: str,
) -> ExportJobV2:
    claimed = await env.exports.claim_next(
        worker_id=worker_id,
        supported_targets=_PPTX_V1_TARGET,
    )
    assert claimed is not None and claimed.id == job_id
    for phase in (
        "normalizing",
        "materializing",
        "planning",
        "rendering",
        "validating",
        "persisting",
    ):
        assert await env.exports.advance_phase(
            job_id=claimed.id,
            worker_id=worker_id,
            attempt=claimed.attempt,
            phase=phase,
        )
    return claimed


async def test_create_atomically_persists_snapshot_pins_and_v2_job(env: ExportEnvironment) -> None:
    image = await env.artifacts.put_bytes(
        content=b"image",
        filename="image.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_export",
    )
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="image",
        payload={"artifact_id": image.id},
    )
    candidate = await _candidate(env)

    result = await env.exports.create_export_job(
        candidate=candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
        idempotency_key="atomic-create",
        request_sha256=_request_hash(),
    )

    assert result.created is True
    assert result.job.contract_version == 2
    assert result.job.status == "queued"
    assert result.job.phase == "queued"
    assert result.job.snapshot_id == candidate.id
    assert result.job.expires_at != candidate.expires_at
    assert await env.snapshots.get_snapshot(candidate.id) is not None
    pins = await list_snapshot_asset_pins(str(env.path), candidate.id)
    assert len(pins) == 1 and pins[0].artifact_id == image.id


async def test_concurrent_same_key_same_hash_returns_one_job_and_one_snapshot(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Idempotent"},
    )
    first_candidate, second_candidate = await asyncio.gather(
        _candidate(env),
        _candidate(env),
    )
    request_hash = _request_hash()

    first, second = await asyncio.gather(
        env.exports.create_export_job(
            candidate=first_candidate,
            expected_projection_token=first_candidate.content_projection.projection_token,
            expected_surface_token=first_candidate.surface_projection.surface_token,
            idempotency_key="same-key",
            request_sha256=request_hash,
        ),
        env.exports.create_export_job(
            candidate=second_candidate,
            expected_projection_token=second_candidate.content_projection.projection_token,
            expected_surface_token=second_candidate.surface_projection.surface_token,
            idempotency_key="same-key",
            request_sha256=request_hash,
        ),
    )

    assert first.job.id == second.job.id
    assert sorted([first.created, second.created]) == [False, True]
    assert await _count_rows(env, "canvas_export_jobs") == 1
    assert await _count_rows(env, "canvas_export_snapshots") == 1
    persisted_ids = {
        snapshot_id for snapshot_id in (first_candidate.id, second_candidate.id) if await env.snapshots.get_snapshot(snapshot_id) is not None
    }
    assert len(persisted_ids) == 1


async def test_same_key_different_hash_rejects_without_second_snapshot(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Conflict"},
    )
    first_candidate = await _candidate(env)
    first = await env.exports.create_export_job(
        candidate=first_candidate,
        expected_projection_token=first_candidate.content_projection.projection_token,
        expected_surface_token=first_candidate.surface_projection.surface_token,
        idempotency_key="conflicting-key",
        request_sha256=_request_hash(),
    )
    second_candidate = await _candidate(env)

    with pytest.raises(ExportStoreError) as error:
        await env.exports.create_export_job(
            candidate=second_candidate,
            expected_projection_token=second_candidate.content_projection.projection_token,
            expected_surface_token=second_candidate.surface_projection.surface_token,
            idempotency_key="conflicting-key",
            request_sha256=_request_hash(include_appendix=False),
        )
    assert error.value.code == "idempotency_conflict"
    assert await env.snapshots.get_snapshot(second_candidate.id) is None
    assert await _count_rows(env, "canvas_export_jobs") == 1
    assert (await env.exports.get_job(first.job.id)) == first.job


async def test_job_insert_failure_rolls_back_snapshot_and_pins(env: ExportEnvironment) -> None:
    image = await env.artifacts.put_bytes(
        content=b"image",
        filename="image.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_export",
    )
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="image",
        payload={"artifact_id": image.id},
    )
    candidate = await _candidate(env)
    async with open_shared_ctx(str(env.path)) as db:
        await db.executescript(
            """
            CREATE TRIGGER fail_v2_job_insert
            BEFORE INSERT ON canvas_export_jobs
            WHEN NEW.idempotency_key = 'fault-injected'
            BEGIN
                SELECT RAISE(ABORT, 'injected V2 job failure');
            END;
            """
        )
        await db.commit()

    with pytest.raises(ExportStoreError) as error:
        await env.exports.create_export_job(
            candidate=candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
            idempotency_key="fault-injected",
            request_sha256=_request_hash(),
        )
    assert error.value.code == "export_job_insert_failed"
    assert await env.snapshots.get_snapshot(candidate.id) is None
    assert await list_snapshot_asset_pins(str(env.path), candidate.id) == ()
    assert await _count_rows(env, "canvas_export_jobs") == 0


async def test_projection_conflict_cannot_leave_job_or_snapshot(env: ExportEnvironment) -> None:
    section = await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Before"},
    )
    candidate = await _candidate(env)
    await env.canvases.patch_section(
        section_id=section.id,
        patch={"text": "After"},
        expected_rev=section.rev,
    )

    with pytest.raises(SnapshotError) as error:
        await env.exports.create_export_job(
            candidate=candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
            idempotency_key="projection-conflict",
            request_sha256=_request_hash(),
        )
    assert error.value.code == "projection_conflict"
    assert await _count_rows(env, "canvas_export_jobs") == 0
    assert await env.snapshots.get_snapshot(candidate.id) is None


def test_request_hash_is_canonical_and_excludes_idempotency_key() -> None:
    left = compute_export_request_sha256(
        {
            "profile": "full_report",
            "format": "pptx",
            "idempotency_key": "first",
        }
    )
    right = compute_export_request_sha256(
        {
            "idempotency_key": "second",
            "format": "pptx",
            "profile": "full_report",
        }
    )
    assert left == right


async def test_validate_and_insert_writer_lock_slo(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Lock SLO"},
    )
    candidates = [await _candidate(env) for _ in range(20)]
    durations: list[float] = []
    for index, candidate in enumerate(candidates):
        result = await env.exports.create_export_job(
            candidate=candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
            idempotency_key=f"perf-{index}",
            request_sha256=_request_hash(),
        )
        durations.append(result.writer_lock_duration_ms)
        canceled = await env.exports.cancel_job(
            job_id=result.job.id,
            canvas_id=env.canvas.id,
            requested_by="u_owner",
        )
        assert canceled is not None

    ordered = sorted(durations)
    p95 = ordered[math.ceil(len(ordered) * 0.95) - 1]
    assert p95 <= 100
    assert max(ordered) <= 250


async def test_list_jobs_is_scoped_and_newest_first(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "List jobs"},
    )
    created_ids: list[str] = []
    for index in range(3):
        candidate = await env.snapshots.build_candidate(
            canvas_id=env.canvas.id,
            request=_request(),
            created_at=f"2026-07-16T00:00:0{index}+00:00",
        )
        result = await env.exports.create_export_job(
            candidate=candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
            idempotency_key=f"list-{index}",
            request_sha256=_request_hash(),
        )
        created_ids.append(result.job.id)

    jobs = await env.exports.list_jobs(
        canvas_id=env.canvas.id,
        requested_by="u_owner",
        limit=2,
    )

    assert [job.id for job in jobs] == list(reversed(created_ids))[:2]
    assert (
        await env.exports.list_jobs(
            canvas_id=env.canvas.id,
            requested_by="u_other",
        )
        == ()
    )


async def test_cancel_job_is_terminal_and_fences_late_results(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Cancel me"},
    )
    candidate = await _candidate(env)
    created = await env.exports.create_export_job(
        candidate=candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
        idempotency_key="cancel-job",
        request_sha256=_request_hash(),
    )

    canceled = await env.exports.cancel_job(
        job_id=created.job.id,
        canvas_id=env.canvas.id,
        requested_by="u_owner",
    )

    assert canceled is not None
    assert canceled.status == "canceled"
    assert canceled.phase == "terminal"
    assert canceled.attempt == created.job.attempt + 1
    assert canceled.finished_at is not None
    with pytest.raises(ExportStoreError) as conflict:
        await env.exports.cancel_job(
            job_id=created.job.id,
            canvas_id=env.canvas.id,
            requested_by="u_owner",
        )
    assert conflict.value.code == "job_not_cancelable"
    assert (
        await env.exports.cancel_job(
            job_id=created.job.id,
            canvas_id=env.canvas.id,
            requested_by="u_other",
        )
        is None
    )


async def test_claim_phase_completion_and_event_ledger_are_monotonic(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Lifecycle"},
    )
    created = await _create_job(env, key="lifecycle")
    claimed = await env.exports.claim_next(
        worker_id="worker_a",
        supported_targets=_PPTX_V1_TARGET,
        now="2026-07-16T01:00:00+00:00",
    )
    assert claimed is not None
    assert claimed.id == created.job.id
    assert claimed.status == "running"
    assert claimed.phase == "resolving_assets"
    assert claimed.attempt == 1

    for phase in (
        "normalizing",
        "materializing",
        "planning",
        "rendering",
        "validating",
        "persisting",
    ):
        assert await env.exports.advance_phase(
            job_id=claimed.id,
            worker_id="worker_a",
            attempt=claimed.attempt,
            phase=phase,
            progress={"completed": 1, "total": 1, "unit": "sections"},
        )
    with pytest.raises(ExportStoreError) as backward:
        await env.exports.advance_phase(
            job_id=claimed.id,
            worker_id="worker_a",
            attempt=claimed.attempt,
            phase="rendering",
        )
    assert backward.value.code == "invalid_job_transition"

    completed = await env.exports.complete_job(
        job_id=claimed.id,
        worker_id="worker_a",
        attempt=claimed.attempt,
        completion=ExportCompletion(
            artifact_id="art_output123",
            manifest_artifact_id="art_manifest123",
            filename="report.pdf",
            size=1024,
            quality_status="clean",
        ),
    )
    assert completed is True
    done = await env.exports.get_job(claimed.id)
    assert done is not None
    assert (done.status, done.phase, done.quality_status) == ("done", "terminal", "clean")
    assert done.artifact_id == "art_output123"
    assert (
        await env.exports.complete_job(
            job_id=claimed.id,
            worker_id="worker_a",
            attempt=claimed.attempt,
            completion=ExportCompletion(
                artifact_id="art_late123",
                manifest_artifact_id="art_late_manifest",
                filename="late.pdf",
                size=1,
                quality_status="clean",
            ),
        )
        is False
    )

    events = await env.exports.list_events(claimed.id)
    assert [event.event_type for event in events] == [
        "queued",
        "claimed",
        "phase_changed",
        "phase_changed",
        "phase_changed",
        "phase_changed",
        "phase_changed",
        "phase_changed",
        "completed",
    ]
    assert [(event.attempt, event.sequence) for event in events[:2]] == [(0, 1), (1, 1)]


async def test_output_manifest_and_job_metadata_publish_atomically(
    env: ExportEnvironment,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Atomic publication"},
    )
    created = await _create_job(env, key="atomic-publication")
    claimed = await _claim_to_persisting(
        env,
        job_id=created.job.id,
        worker_id="worker_publish",
    )
    manifest = await env.artifacts.stage_export_bytes(
        content=b'{"quality":"clean"}',
        filename="report.manifest.json",
        mime="application/json",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=CURRENT_MANIFEST_PRODUCER,
    )
    output = await env.artifacts.stage_export_bytes(
        content=b"PK\x03\x04test-pptx",
        filename="report.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=output_producer_for(engine="durable", fmt=claimed.fmt),
        manifest_artifact_id=manifest.artifact.id,
    )
    completion = ExportCompletion(
        artifact_id=output.artifact.id,
        manifest_artifact_id=manifest.artifact.id,
        filename=output.artifact.filename,
        size=output.artifact.size,
        quality_status="clean",
        committed=True,
    )

    assert await env.exports.complete_job_with_staged_artifacts(
        job_id=claimed.id,
        worker_id="worker_publish",
        attempt=claimed.attempt,
        completion=completion,
        artifact_store=env.artifacts,
        output_artifact=output,
        manifest_artifact=manifest,
    )

    done = await env.exports.get_job(claimed.id)
    assert done is not None
    assert (
        done.status,
        done.artifact_id,
        done.manifest_artifact_id,
    ) == (
        "done",
        output.artifact.id,
        manifest.artifact.id,
    )
    stored_output = await env.artifacts.get(output.artifact.id)
    stored_manifest = await env.artifacts.get(manifest.artifact.id)
    assert stored_output is not None
    assert stored_manifest is not None
    assert stored_output.manifest_artifact_id == stored_manifest.id
    async with (
        open_shared_ctx(str(env.path)) as db,
        db.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE artifact_id IN (?,?) AND detached_at IS NULL",
            (
                output.artifact.id,
                manifest.artifact.id,
            ),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    assert row is not None and int(row[0]) == 2


async def test_boot_cleanup_removes_only_old_unpublished_export_blobs(
    env: ExportEnvironment,
) -> None:
    orphan = await env.artifacts.stage_export_bytes(
        content=b"orphan",
        filename="orphan.manifest.json",
        mime="application/json",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=CURRENT_MANIFEST_PRODUCER,
    )
    published = await env.artifacts.stage_export_bytes(
        content=b"published",
        filename="published.manifest.json",
        mime="application/json",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=CURRENT_MANIFEST_PRODUCER,
    )
    await env.artifacts.commit_staged_artifacts((published,))
    old = time.time() - 2 * 60 * 60
    os.utime(orphan.artifact.path, (old, old))
    os.utime(published.artifact.path, (old, old))

    restarted = ArtifactStore(
        db_path=str(env.path),
        backend=env.artifacts.backend,
    )
    await restarted.initialize()

    assert not Path(orphan.artifact.path).exists()
    assert Path(published.artifact.path).exists()
    assert await restarted.get(published.artifact.id) is not None


async def test_cancellation_after_publish_commit_keeps_done_artifacts(
    env: ExportEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Commit wins cancellation"},
    )
    created = await _create_job(env, key="atomic-publication-cancel")
    claimed = await _claim_to_persisting(
        env,
        job_id=created.job.id,
        worker_id="worker_publish_cancel",
    )
    manifest = await env.artifacts.stage_export_bytes(
        content=b'{"quality":"clean"}',
        filename="report.manifest.json",
        mime="application/json",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=CURRENT_MANIFEST_PRODUCER,
    )
    output = await env.artifacts.stage_export_bytes(
        content=b"PK\x03\x04test-pptx",
        filename="report.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=output_producer_for(engine="durable", fmt=claimed.fmt),
        manifest_artifact_id=manifest.artifact.id,
    )
    commit_landed = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = aiosqlite.Connection.commit

    async def delayed_commit(connection: aiosqlite.Connection) -> None:
        await original_commit(connection)
        commit_landed.set()
        await release_commit.wait()

    monkeypatch.setattr(aiosqlite.Connection, "commit", delayed_commit)
    publication = asyncio.create_task(
        env.exports.complete_job_with_staged_artifacts(
            job_id=claimed.id,
            worker_id="worker_publish_cancel",
            attempt=claimed.attempt,
            completion=ExportCompletion(
                artifact_id=output.artifact.id,
                manifest_artifact_id=manifest.artifact.id,
                filename=output.artifact.filename,
                size=output.artifact.size,
                quality_status="clean",
                committed=True,
            ),
            artifact_store=env.artifacts,
            output_artifact=output,
            manifest_artifact=manifest,
        )
    )
    await commit_landed.wait()
    publication.cancel()
    release_commit.set()

    assert await publication
    done = await env.exports.get_job(claimed.id)
    assert done is not None and done.status == "done"
    assert await env.artifacts.get(output.artifact.id) is not None
    assert await env.artifacts.get(manifest.artifact.id) is not None


async def test_atomic_publication_failure_rolls_back_metadata_and_blobs(
    env: ExportEnvironment,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Publication failure"},
    )
    created = await _create_job(env, key="atomic-publication-failure")
    claimed = await _claim_to_persisting(
        env,
        job_id=created.job.id,
        worker_id="worker_publish_failure",
    )
    manifest = await env.artifacts.stage_export_bytes(
        content=b'{"quality":"clean"}',
        filename="report.manifest.json",
        mime="application/json",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=CURRENT_MANIFEST_PRODUCER,
    )
    output = await env.artifacts.stage_export_bytes(
        content=b"PK\x03\x04test-pptx",
        filename="report.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        canvas_id=env.canvas.id,
        created_by="u_owner",
        session_id="s_export",
        producer_kind=output_producer_for(engine="durable", fmt=claimed.fmt),
        manifest_artifact_id=manifest.artifact.id,
    )
    async with open_shared_ctx(str(env.path)) as db:
        await db.executescript(
            "CREATE TRIGGER fail_atomic_export_output "
            "BEFORE INSERT ON artifacts "
            "WHEN NEW.producer_kind='canvas_export_b1a_v1_pptx_v2' "
            "BEGIN SELECT RAISE(ABORT, 'forced publication failure'); END;"
        )
        await db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await env.exports.complete_job_with_staged_artifacts(
            job_id=claimed.id,
            worker_id="worker_publish_failure",
            attempt=claimed.attempt,
            completion=ExportCompletion(
                artifact_id=output.artifact.id,
                manifest_artifact_id=manifest.artifact.id,
                filename=output.artifact.filename,
                size=output.artifact.size,
                quality_status="clean",
                committed=True,
            ),
            artifact_store=env.artifacts,
            output_artifact=output,
            manifest_artifact=manifest,
        )

    running = await env.exports.get_job(claimed.id)
    assert running is not None
    assert (running.status, running.phase) == (
        "running",
        "persisting",
    )
    assert await env.artifacts.get(output.artifact.id) is None
    assert await env.artifacts.get(manifest.artifact.id) is None
    assert not Path(output.artifact.path).exists()
    assert not Path(manifest.artifact.path).exists()


async def test_unclaimable_queued_jobs_fail_without_consuming_capacity(
    env: ExportEnvironment,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Queued before rollback"},
    )
    created = await _create_job(env, key="unsupported-after-rollback")

    assert (
        await env.exports.fail_unclaimable_queued_jobs(
            supported_pairs=(("pdf", "full_report"),),
            now="2026-07-17T01:00:00+00:00",
        )
        == 1
    )

    failed = await env.exports.get_job(created.job.id)
    assert failed is not None
    assert (
        failed.status,
        failed.phase,
        failed.error_code,
    ) == (
        "failed",
        "terminal",
        "export_runner_unavailable",
    )
    events = await env.exports.list_events(created.job.id)
    assert events[-1].reason_code == "export_runner_unavailable"


async def test_renderer_contract_is_filtered_before_claim_without_consuming_attempt(
    env: ExportEnvironment,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Renderer V2"},
    )
    candidate = await env.snapshots.build_candidate(
        canvas_id=env.canvas.id,
        request=replace(
            _request(),
            theme=_v2_theme(),
        ),
    )
    with pytest.raises(SnapshotError, match="snapshot_renderer_contract_invalid"):
        await env.snapshots.build_candidate(
            canvas_id=env.canvas.id,
            request=replace(
                _request(),
                theme={
                    "kind": "chart-style",
                    "schemaVersion": 1,
                    "rendererContractVersion": "2",
                },
            ),
        )
    created = await _create_job(env, key="renderer-v2", candidate=candidate)

    assert created.job.renderer_contract_version == 2
    assert (
        await env.exports.claim_next(
            worker_id="worker_v1",
            supported_targets=(("pptx", "full_report", 1),),
        )
        is None
    )
    queued = await env.exports.get_job(created.job.id)
    assert queued is not None and (queued.status, queued.attempt) == ("queued", 0)
    assert (
        await env.exports.fail_unclaimable_queued_jobs(
            supported_pairs=(("pptx", "full_report"),),
        )
        == 0
    )

    claimed = await env.exports.claim_next(
        worker_id="worker_v2",
        supported_targets=(("pptx", "full_report", 2),),
    )
    assert claimed is not None
    assert (claimed.id, claimed.renderer_contract_version, claimed.attempt) == (created.job.id, 2, 1)


async def test_expired_lease_reclaims_once_then_fails_worker_lost(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Recovery"},
    )
    created = await _create_job(env, key="recover")
    first = await env.exports.claim_next(
        worker_id="worker_old",
        supported_targets=_PPTX_V1_TARGET,
        lease_seconds=10,
        now="2026-07-16T02:00:00+00:00",
    )
    assert first is not None and first.id == created.job.id and first.attempt == 1
    reclaimed = await env.exports.claim_next(
        worker_id="worker_new",
        supported_targets=_PPTX_V1_TARGET,
        lease_seconds=10,
        now="2026-07-16T02:00:11+00:00",
    )
    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.attempt == 2
    assert reclaimed.worker_id == "worker_new"
    assert (
        await env.exports.heartbeat(
            job_id=first.id,
            worker_id="worker_old",
            attempt=first.attempt,
            now="2026-07-16T02:00:12+00:00",
        )
        is False
    )

    assert (
        await env.exports.claim_next(
            worker_id="worker_third",
            supported_targets=_PPTX_V1_TARGET,
            lease_seconds=10,
            now="2026-07-16T02:00:22+00:00",
        )
        is None
    )
    failed = await env.exports.get_job(first.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "worker_lost"
    assert [event.event_type for event in await env.exports.list_events(first.id)] == [
        "queued",
        "claimed",
        "reclaimed",
        "failed",
    ]


async def test_cancel_fences_heartbeat_phase_and_late_completion(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Cancel running"},
    )
    created = await _create_job(env, key="cancel-running")
    claimed = await env.exports.claim_next(
        worker_id="worker_cancel",
        supported_targets=_PPTX_V1_TARGET,
    )
    assert claimed is not None and claimed.id == created.job.id
    canceled = await env.exports.cancel_job(
        job_id=claimed.id,
        canvas_id=env.canvas.id,
        requested_by="u_owner",
    )
    assert canceled is not None and canceled.attempt == claimed.attempt + 1
    assert (
        await env.exports.heartbeat(
            job_id=claimed.id,
            worker_id="worker_cancel",
            attempt=claimed.attempt,
        )
        is False
    )
    assert (
        await env.exports.advance_phase(
            job_id=claimed.id,
            worker_id="worker_cancel",
            attempt=claimed.attempt,
            phase="normalizing",
        )
        is False
    )
    assert (
        await env.exports.complete_job(
            job_id=claimed.id,
            worker_id="worker_cancel",
            attempt=claimed.attempt,
            completion=ExportCompletion(
                artifact_id="art_stale_output",
                manifest_artifact_id="art_stale_manifest",
                filename="stale.pdf",
                size=1,
                quality_status="clean",
            ),
        )
        is False
    )


async def test_admission_rejects_fourth_active_job_without_snapshot(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Capacity"},
    )
    for index in range(3):
        await _create_job(env, key=f"capacity-{index}")
    rejected_candidate = await _candidate(env)
    with pytest.raises(ExportStoreError) as capacity:
        await _create_job(
            env,
            key="capacity-rejected",
            candidate=rejected_candidate,
        )
    assert capacity.value.code == "export_capacity_exceeded"
    assert capacity.value.params == {"scope": "canvas", "limit": 3}
    assert await env.snapshots.get_snapshot(rejected_candidate.id) is None
    assert await _count_rows(env, "canvas_export_jobs") == 3


async def test_admission_enforces_per_user_running_limit_across_canvases(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "First running"},
    )
    await _create_job(env, key="user-limit-1")
    assert (
        await env.exports.claim_next(
            worker_id="user_limit_worker_1",
            supported_targets=_PPTX_V1_TARGET,
        )
        is not None
    )

    async def new_canvas_candidate(index: int) -> ExportSnapshotCandidate:
        session_id = f"s_export_{index}"
        await env.sessions.ensure_session(
            session_id=session_id,
            workspace_id="ws_1",
            user_id="u_owner",
        )
        canvas = await env.canvases.ensure_canvas(
            session_id=session_id,
            workspace_id="ws_1",
            user_id="u_owner",
            title=f"Capacity {index}",
        )
        await env.canvases.append_section(
            canvas_id=canvas.id,
            kind="h1",
            payload={"text": f"Running {index}"},
        )
        return await env.snapshots.build_candidate(
            canvas_id=canvas.id,
            request=_request(),
        )

    second_candidate = await new_canvas_candidate(2)
    await _create_job(env, key="user-limit-2", candidate=second_candidate)
    assert (
        await env.exports.claim_next(
            worker_id="user_limit_worker_2",
            supported_targets=_PPTX_V1_TARGET,
        )
        is not None
    )
    rejected_candidate = await new_canvas_candidate(3)
    with pytest.raises(ExportStoreError) as capacity:
        await _create_job(
            env,
            key="user-limit-rejected",
            candidate=rejected_candidate,
        )
    assert capacity.value.code == "export_capacity_exceeded"
    assert capacity.value.params == {"scope": "user", "limit": 2}
    assert await env.snapshots.get_snapshot(rejected_candidate.id) is None


async def test_admission_bounds_queued_jobs_across_user_canvases(
    env: ExportEnvironment,
) -> None:
    async def candidate_for(index: int) -> ExportSnapshotCandidate:
        session_id = f"s_user_queue_{index}"
        await env.sessions.ensure_session(
            session_id=session_id,
            workspace_id="ws_1",
            user_id="u_owner",
        )
        canvas = await env.canvases.ensure_canvas(
            session_id=session_id,
            workspace_id="ws_1",
            user_id="u_owner",
            title=f"Queued capacity {index}",
        )
        await env.canvases.append_section(
            canvas_id=canvas.id,
            kind="h1",
            payload={"text": f"Queued {index}"},
        )
        return await env.snapshots.build_candidate(
            canvas_id=canvas.id,
            request=_request(),
        )

    for index in range(6):
        await _create_job(
            env,
            key=f"user-active-{index}",
            candidate=await candidate_for(index),
        )
    rejected_candidate = await candidate_for(6)
    with pytest.raises(ExportStoreError) as capacity:
        await _create_job(
            env,
            key="user-active-rejected",
            candidate=rejected_candidate,
        )

    assert capacity.value.code == "export_capacity_exceeded"
    assert capacity.value.params == {"scope": "user_active", "limit": 6}
    assert await env.snapshots.get_snapshot(rejected_candidate.id) is None


async def test_durable_runner_supervises_processor_to_clean_completion(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Runner success"},
    )
    created = await _create_job(env, key="runner-success")
    registry = ExportProcessorRegistry()

    async def process(
        job: ExportJobV2,
        context: ExportExecutionContext,
    ) -> ExportCompletion:
        assert job.id == created.job.id
        for phase in (
            "normalizing",
            "materializing",
            "planning",
            "rendering",
            "validating",
            "persisting",
        ):
            await context.advance(phase)
        return ExportCompletion(
            artifact_id="art_runner_output",
            manifest_artifact_id="art_runner_manifest",
            filename="runner.pdf",
            size=42,
            quality_status="clean",
        )

    registry.register(fmt="pptx", profile="full_report", handler=process)
    runner = DurableLocalExportRunner(
        store=env.exports,
        processor=registry,
        worker_id="runner_success",
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.05,
        lease_seconds=1,
    )
    await runner.start()
    assert runner.is_available_for("pptx", "full_report")
    await runner.enqueue(created.job.id)
    await _wait_for_status(env.exports, created.job.id, "done")
    await runner.stop(grace_seconds=0.1)

    done = await env.exports.get_job(created.job.id)
    assert done is not None
    assert done.artifact_id == "art_runner_output"
    assert done.manifest_artifact_id == "art_runner_manifest"
    assert done.worker_id is None


async def test_durable_runner_failure_and_cancel_remain_terminal(env: ExportEnvironment) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="h1",
        payload={"text": "Runner terminal states"},
    )
    failed_job = await _create_job(env, key="runner-failure")
    registry = ExportProcessorRegistry()

    async def fail_processor(
        job: ExportJobV2,
        context: ExportExecutionContext,
    ) -> ExportCompletion:
        del job, context
        raise ExportProcessingError(
            "validation_failed",
            "导出内容校验未通过",
            quality_status="rejected",
        )

    registry.register(fmt="pptx", profile="full_report", handler=fail_processor)
    runner = DurableLocalExportRunner(
        store=env.exports,
        processor=registry,
        worker_id="runner_failure",
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.05,
        lease_seconds=1,
    )
    await runner.start()
    await runner.enqueue(failed_job.job.id)
    await _wait_for_status(env.exports, failed_job.job.id, "failed")
    failed = await env.exports.get_job(failed_job.job.id)
    assert failed is not None
    assert failed.error_code == "validation_failed"
    assert failed.quality_status == "rejected"
    await runner.stop(grace_seconds=0.1)

    cancel_job = await _create_job(env, key="runner-cancel")
    started = asyncio.Event()

    async def blocking_processor(
        job: ExportJobV2,
        context: ExportExecutionContext,
    ) -> ExportCompletion:
        del job, context
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    cancel_registry = ExportProcessorRegistry()
    cancel_registry.register(
        fmt="pptx",
        profile="full_report",
        handler=blocking_processor,
    )
    cancel_runner = DurableLocalExportRunner(
        store=env.exports,
        processor=cancel_registry,
        worker_id="runner_cancel",
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.05,
        lease_seconds=1,
    )
    await cancel_runner.start()
    await cancel_runner.enqueue(cancel_job.job.id)
    await asyncio.wait_for(started.wait(), timeout=2)
    canceled = await env.exports.cancel_job(
        job_id=cancel_job.job.id,
        canvas_id=env.canvas.id,
        requested_by="u_owner",
    )
    assert canceled is not None
    await cancel_runner.cancel(cancel_job.job.id)
    await _wait_for_status(env.exports, cancel_job.job.id, "canceled")
    await cancel_runner.stop(grace_seconds=0.1)
    terminal = await env.exports.get_job(cancel_job.job.id)
    assert terminal is not None
    assert terminal.status == "canceled"
    assert terminal.error_code is None
