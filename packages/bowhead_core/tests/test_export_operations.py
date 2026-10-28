"""Canvas Export V2 retention operational gates."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bowhead_core._db import open_shared_ctx
from bowhead_core.export_retention import run_export_retention
from bowhead_core.export_snapshot import ExportSnapshotStore
from bowhead_core.export_store import ExportStore

from bowhead_core import canvas_export as legacy_export

_NOW = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[3]


async def test_export_retention_preserves_active_jobs_and_deletes_orphans(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "retention.db"
    old_legacy_db_path = legacy_export._db_path
    await legacy_export.init_export_jobs(str(db_path))
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("ALTER TABLE canvas_export_jobs ADD COLUMN renderer_contract_version INTEGER NOT NULL DEFAULT 1")
        await db.commit()
    snapshots = ExportSnapshotStore(db_path)
    await snapshots.initialize()
    store = ExportStore(db_path, snapshots)
    await store.initialize()
    expired = (_NOW - timedelta(days=1)).isoformat(timespec="seconds")
    future = (_NOW + timedelta(days=1)).isoformat(timespec="seconds")
    try:
        async with open_shared_ctx(str(db_path)) as db:
            for snapshot_id, expires_at in (
                ("exs_terminal", expired),
                ("exs_active", expired),
                ("exs_orphan", expired),
            ):
                await _insert_snapshot(
                    db,
                    snapshot_id=snapshot_id,
                    expires_at=expires_at,
                )
            await _insert_job(
                db,
                job_id="exp_terminal",
                status="done",
                snapshot_id="exs_terminal",
                expires_at=expired,
            )
            await _insert_job(
                db,
                job_id="exp_active",
                status="queued",
                snapshot_id="exs_active",
                expires_at=future,
            )
            await db.execute(
                "INSERT INTO canvas_export_job_events(job_id,attempt,sequence,event_type,created_at) VALUES (?,?,?,?,?)",
                (
                    "exp_terminal",
                    1,
                    1,
                    "completed",
                    expired,
                ),
            )
            await db.commit()

        staging_root = tmp_path / "staging"
        stale = staging_root / "exp_stale" / "attempt-1"
        active = staging_root / "exp_active" / "attempt-1"
        unrelated = staging_root / "user-folder"
        for directory in (stale, active, unrelated):
            directory.mkdir(parents=True)
            (directory / "output.bin").write_bytes(b"1234")
        old_timestamp = (_NOW - timedelta(hours=48)).timestamp()
        for root in (
            stale,
            stale.parent,
            active,
            active.parent,
            unrelated,
        ):
            os.utime(root, (old_timestamp, old_timestamp))
        for file_path in staging_root.rglob("output.bin"):
            os.utime(file_path, (old_timestamp, old_timestamp))

        result = await run_export_retention(
            store=store,
            staging_root=staging_root,
            staging_retention_hours=24,
            now=_NOW,
        )

        assert result.jobs_deleted == 1
        assert result.snapshots_deleted == 2
        assert result.staging_job_dirs_deleted == 1
        assert result.staging_bytes_deleted == 4
        assert not stale.parent.exists()
        assert active.parent.exists()
        assert unrelated.exists()
        async with open_shared_ctx(str(db_path)) as db:
            async with db.execute("SELECT id FROM canvas_export_jobs ORDER BY id") as cursor:
                jobs = await cursor.fetchall()
            async with db.execute("SELECT id FROM canvas_export_snapshots ORDER BY id") as cursor:
                remaining_snapshots = await cursor.fetchall()
            async with db.execute("SELECT COUNT(*) FROM canvas_export_job_events WHERE job_id='exp_terminal'") as cursor:
                event_count = await cursor.fetchone()
        assert jobs == [("exp_active",)]
        assert remaining_snapshots == [("exs_active",)]
        assert event_count is not None
        assert int(event_count[0]) == 0
    finally:
        legacy_export._jobs.clear()
        legacy_export._db_path = old_legacy_db_path


async def _insert_snapshot(
    db: object,
    *,
    snapshot_id: str,
    expires_at: str,
) -> None:
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO canvas_export_snapshots("
        "id,schema_version,canvas_id,session_id,team_id,workspace_id,"
        "canvas_owner,requested_by,format,profile,title,locale,timezone,"
        "projection_token,surface_contract_version,surface_token,"
        "parity_contract,payload_json,payload_sha256,size_bytes,"
        "section_count,created_at,expires_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id,
            1,
            "cv_retention",
            "s_retention",
            "team_retention",
            "ws_retention",
            "u_owner",
            "u_owner",
            "pdf",
            "full_report",
            "Retention",
            "zh-CN",
            "Asia/Singapore",
            "sha256:projection",
            1,
            "sha256:surface",
            "full_content_v1",
            "{}",
            "sha256:payload",
            2,
            0,
            "2026-07-01T00:00:00+00:00",
            expires_at,
        ),
    )


async def _insert_job(
    db: object,
    *,
    job_id: str,
    status: str,
    snapshot_id: str,
    expires_at: str,
) -> None:
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO canvas_export_jobs("
        "id,canvas_id,fmt,status,created_at,updated_at,contract_version,"
        "profile,parity_contract,surface_token,phase,quality_status,"
        "warning_count,snapshot_id,requested_by,idempotency_key,"
        "request_sha256,attempt,runner_kind,expires_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            "cv_retention",
            "pdf",
            status,
            "2026-07-01T00:00:00+00:00",
            "2026-07-01T00:00:00+00:00",
            2,
            "full_report",
            "full_content_v1",
            "sha256:surface",
            "terminal" if status == "done" else "queued",
            "clean" if status == "done" else "pending",
            0,
            snapshot_id,
            "u_owner",
            f"idem-{job_id}",
            "sha256:request",
            1 if status == "done" else 0,
            "local",
            expires_at,
        ),
    )
