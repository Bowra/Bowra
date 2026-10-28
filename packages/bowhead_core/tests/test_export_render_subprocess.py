"""Supervised renderer process isolation, timeout, and cancellation contracts."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from bowhead_core.export_ir import ExportIR, ExportIRDocument
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.print_theme import current_print_theme_snapshot

from bowhead_core import export_render_subprocess


async def test_supervised_renderer_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    task = asyncio.create_task(
        export_render_subprocess._run_supervised_process(  # noqa: SLF001
            command=(
                sys.executable,
                "-c",
                "import time; time.sleep(0.2)",
            ),
            cwd=tmp_path,
            env=os.environ.copy(),
            wall_timeout_seconds=2.0,
            terminate_grace_seconds=0.1,
            on_progress=None,
        )
    )
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.01)

    result = await task

    assert result.returncode == 0
    assert ticks >= 5


async def test_supervised_renderer_timeout_kills_process_group(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "timeout.pid"
    with pytest.raises(TimeoutError):
        await export_render_subprocess._run_supervised_process(  # noqa: SLF001
            command=_sleeping_process_command(pid_path),
            cwd=tmp_path,
            env=os.environ.copy(),
            wall_timeout_seconds=0.1,
            terminate_grace_seconds=0.1,
            on_progress=None,
        )

    assert pid_path.is_file()
    assert not _pid_exists(int(pid_path.read_text()))


async def test_supervised_renderer_cancellation_kills_process_group(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "cancel.pid"
    task = asyncio.create_task(
        export_render_subprocess._run_supervised_process(  # noqa: SLF001
            command=_sleeping_process_command(pid_path),
            cwd=tmp_path,
            env=os.environ.copy(),
            wall_timeout_seconds=60.0,
            terminate_grace_seconds=0.1,
            on_progress=None,
        )
    )
    for _ in range(100):
        if pid_path.is_file():
            break
        await asyncio.sleep(0.01)
    assert pid_path.is_file()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _pid_exists(int(pid_path.read_text()))


def test_renderer_child_environment_drops_unapproved_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_DATABASE_URL", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    environment = export_render_subprocess._child_env(  # noqa: SLF001
        tmp_path,
    )

    assert "BOWHEAD_DATABASE_URL" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["HOME"] == str(tmp_path / ".worker-home")


async def test_pdf_render_and_validation_run_in_fresh_interpreter(
    tmp_path: Path,
) -> None:
    snapshot = _empty_pdf_snapshot()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()

    outcome = await export_render_subprocess.run_export_format_worker(
        fmt="pdf",
        job_id="exp_subprocess_smoke",
        snapshot=snapshot,
        export_ir=_empty_export_ir(snapshot),
        materialized_visuals=(),
        attempt_dir=attempt_dir,
        include_appendix=True,
        target="pdf_full",
        profile="full_report",
        limits=export_render_subprocess.ExportRenderLimits(
            cpu_seconds=120,
            max_memory_mb=512,
            max_file_size_mb=160,
            wall_timeout_seconds=120,
        ),
    )

    assert outcome.output_path.is_file()
    assert outcome.output_path.read_bytes().startswith(b"%PDF")
    assert outcome.manifest["job_id"] == "exp_subprocess_smoke"
    assert outcome.quality_status in {"clean", "warnings"}


def _sleeping_process_command(
    pid_path: Path,
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        ("import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"),
        str(pid_path),
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _empty_pdf_snapshot() -> ExportSnapshot:
    return ExportSnapshot(
        id="exs_subprocess_smoke",
        schema_version=1,
        canvas_id="cv_subprocess_smoke",
        session_id="s_subprocess_smoke",
        team_id="team_subprocess_smoke",
        workspace_id="ws_subprocess_smoke",
        canvas_owner="u_owner",
        requested_by="u_owner",
        fmt="pdf",
        profile="full_report",
        title="Subprocess smoke",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'1' * 64}",
        surface_contract_version=1,
        surface_token=f"sha256:{'2' * 64}",
        parity_contract="full_content_v1",
        payload={
            "request": {
                "format": "pdf",
                "profile": "full_report",
                "options": {"include_appendix": True},
                "client_surface_receipt_sha256": (f"sha256:{'4' * 64}"),
                "print_theme": current_print_theme_snapshot(),
            },
            "surface_projection": {
                "contract_version": 1,
                "token": f"sha256:{'2' * 64}",
                "entries": [],
                "exclusions": [],
            },
            "sections": [],
        },
        payload_sha256=f"sha256:{'3' * 64}",
        size_bytes=100,
        section_count=0,
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-24T00:00:00+00:00",
    )


def _empty_export_ir(snapshot: ExportSnapshot) -> ExportIR:
    return ExportIR(
        schema_version=1,
        snapshot_id=snapshot.id,
        input_sha256=snapshot.payload_sha256,
        surface_token=snapshot.surface_token,
        document=ExportIRDocument(
            title=snapshot.title,
            locale=snapshot.locale,
            timezone=snapshot.timezone,
            profile=snapshot.profile,
        ),
        blocks=(),
        visual_assets=(),
        sources=(),
        normalization_warnings=(),
    )
