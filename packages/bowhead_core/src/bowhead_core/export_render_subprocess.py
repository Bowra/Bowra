"""Supervised fresh-interpreter execution for Canvas Export V2 renderers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from bowhead_core.export_ir import ExportIR
from bowhead_core.export_materializer import MaterializedVisual
from bowhead_core.export_snapshot import ExportSnapshot

ExportFormat = Literal["pdf", "pptx", "xlsx"]


class ProgressCallback(Protocol):
    async def __call__(
        self,
        phase: str,
        *,
        progress: dict[str, object] | None = None,
    ) -> None: ...


_CHILD_ENV_ALLOW = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "FONTCONFIG_FILE",
    "FONTCONFIG_PATH",
    "SAL_FONTPATH",
)
_MAX_EVENT_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 16 * 1024
_MAX_CONTROL_BYTES = 64 * 1024 * 1024
_MAX_RESULT_BYTES = 16 * 1024 * 1024


class ExportRenderSubprocessError(Exception):
    """The isolated renderer failed before returning a verified result."""


@dataclass(frozen=True, slots=True)
class ExportRenderLimits:
    cpu_seconds: int
    max_memory_mb: int
    max_file_size_mb: int
    wall_timeout_seconds: float
    terminate_grace_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class ExportRenderOutcome:
    output_path: Path
    manifest: dict[str, Any]
    quality_status: Literal["clean", "warnings", "rejected"]
    warning_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stderr: str
    progress_events: int


def export_render_limits(
    fmt: ExportFormat,
    profile: str,
) -> ExportRenderLimits:
    """Return the design-budget cap, with bounded deployment overrides."""
    if fmt == "pdf":
        defaults = (600, 1024, 512, 600.0)
    elif fmt == "pptx" and profile == "full_report":
        defaults = (900, 1024, 512, 900.0)
    elif fmt == "pptx":
        defaults = (600, 768, 512, 600.0)
    else:
        defaults = (300, 768, 512, 300.0)
    return ExportRenderLimits(
        cpu_seconds=_bounded_int(
            "BOWHEAD_EXPORT_RENDER_CPU_SECONDS",
            defaults[0],
            minimum=1,
            maximum=1800,
        ),
        max_memory_mb=_bounded_int(
            "BOWHEAD_EXPORT_RENDER_MEMORY_MB",
            defaults[1],
            minimum=256,
            maximum=4096,
        ),
        max_file_size_mb=_bounded_int(
            "BOWHEAD_EXPORT_RENDER_FILE_SIZE_MB",
            defaults[2],
            minimum=160,
            maximum=2048,
        ),
        wall_timeout_seconds=float(
            _bounded_int(
                "BOWHEAD_EXPORT_RENDER_WALL_SECONDS",
                int(defaults[3]),
                minimum=1,
                maximum=1800,
            )
        ),
        terminate_grace_seconds=float(
            _bounded_int(
                "BOWHEAD_EXPORT_RENDER_TERM_GRACE_SECONDS",
                2,
                minimum=1,
                maximum=30,
            )
        ),
    )


async def run_export_format_worker(
    *,
    fmt: ExportFormat,
    job_id: str,
    snapshot: ExportSnapshot,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    attempt_dir: Path,
    include_appendix: bool,
    target: str,
    profile: str,
    soffice_path: Path | None = None,
    pdftoppm_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    limits: ExportRenderLimits | None = None,
) -> ExportRenderOutcome:
    """Render, reopen, and validate one format outside the gateway process."""
    selected_limits = limits or export_render_limits(fmt, profile)
    control_path = attempt_dir / ".render-control.json"
    result_path = attempt_dir / ".render-result.json"
    control: dict[str, Any] = {
        "schema_version": 1,
        "format": fmt,
        "job_id": job_id,
        "snapshot": asdict(snapshot),
        "export_ir": export_ir.to_dict(),
        "materialized_visuals": [asdict(visual) for visual in materialized_visuals],
        "include_appendix": include_appendix,
        "target": target,
        "profile": profile,
        "soffice_path": (str(soffice_path) if soffice_path is not None else None),
        "pdftoppm_path": (str(pdftoppm_path) if pdftoppm_path is not None else None),
    }
    await asyncio.to_thread(
        _write_control,
        attempt_dir,
        control_path,
        control,
    )
    env = _child_env(attempt_dir)
    command = (
        sys.executable,
        "-m",
        "bowhead_core.export_render_worker",
        str(control_path),
        str(result_path),
        str(selected_limits.cpu_seconds),
        str(selected_limits.max_memory_mb),
        str(selected_limits.max_file_size_mb),
    )
    try:
        process_result = await _run_supervised_process(
            command=command,
            cwd=attempt_dir,
            env=env,
            wall_timeout_seconds=selected_limits.wall_timeout_seconds,
            terminate_grace_seconds=(selected_limits.terminate_grace_seconds),
            on_progress=on_progress,
        )
    except TimeoutError as exc:
        raise ExportRenderSubprocessError(
            "renderer_timeout",
        ) from exc
    if process_result.returncode != 0 or not result_path.is_file():
        raise ExportRenderSubprocessError(f"renderer_subprocess_failed:{process_result.returncode}:{process_result.stderr[:500]}")
    if process_result.progress_events != 1:
        raise ExportRenderSubprocessError(
            "renderer_progress_protocol_invalid",
        )
    raw_result = await asyncio.to_thread(
        _read_json_object,
        result_path,
    )
    return await asyncio.to_thread(
        _validate_result,
        raw_result,
        fmt,
        job_id,
        snapshot,
        attempt_dir,
    )


async def _run_supervised_process(
    *,
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    wall_timeout_seconds: float,
    terminate_grace_seconds: float,
    on_progress: ProgressCallback | None,
) -> _ProcessResult:
    """Run one command in a new session and own its whole process group."""
    if os.name != "posix":
        raise ExportRenderSubprocessError(
            "renderer_process_isolation_unavailable",
        )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        await _finish_termination(
            process,
            grace_seconds=terminate_grace_seconds,
        )
        raise ExportRenderSubprocessError(
            "renderer_process_pipes_unavailable",
        )
    wait_task = asyncio.create_task(process.wait())
    progress_task = asyncio.create_task(
        _read_progress_events(
            process.stdout,
            on_progress=on_progress,
        )
    )
    stderr_task = asyncio.create_task(_read_bounded_stderr(process.stderr))
    try:
        async with asyncio.timeout(wall_timeout_seconds):
            done, _pending = await asyncio.wait(
                (wait_task, progress_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if progress_task in done:
                await progress_task
            await wait_task
            progress_events = await progress_task
            stderr = await stderr_task
    except BaseException:
        await _finish_termination(
            process,
            grace_seconds=terminate_grace_seconds,
        )
        raise
    finally:
        for task in (wait_task, progress_task, stderr_task):
            task.cancel()
        await asyncio.gather(
            wait_task,
            progress_task,
            stderr_task,
            return_exceptions=True,
        )
    return _ProcessResult(
        returncode=cast(int, process.returncode),
        stderr=stderr,
        progress_events=progress_events,
    )


async def _read_progress_events(
    stream: asyncio.StreamReader,
    *,
    on_progress: ProgressCallback | None,
) -> int:
    count = 0
    while True:
        line = await stream.readline()
        if not line:
            return count
        if len(line) > _MAX_EVENT_BYTES:
            raise ExportRenderSubprocessError(
                "renderer_progress_event_too_large",
            )
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportRenderSubprocessError(
                "renderer_progress_event_invalid",
            ) from exc
        if not isinstance(event, dict) or event.get("phase") != "validating" or not isinstance(event.get("progress"), dict) or count != 0:
            raise ExportRenderSubprocessError(
                "renderer_progress_event_invalid",
            )
        progress = {
            str(key): cast(object, value)
            for key, value in event["progress"].items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
        if len(progress) != len(event["progress"]):
            raise ExportRenderSubprocessError(
                "renderer_progress_event_invalid",
            )
        count += 1
        if on_progress is not None:
            await on_progress(
                "validating",
                progress=progress,
            )


async def _read_bounded_stderr(
    stream: asyncio.StreamReader,
) -> str:
    captured = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        remaining = _MAX_STDERR_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return captured.decode("utf-8", "replace")


async def _finish_termination(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    termination = asyncio.create_task(
        _terminate_process_group(
            process,
            grace_seconds=grace_seconds,
        )
    )
    try:
        await asyncio.shield(termination)
    except asyncio.CancelledError:
        await termination


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    pgid = process.pid
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=grace_seconds,
            )
    # The session leader may have exited while LibreOffice/Poppler descendants
    # remain. Always sweep the original process group after TERM.
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=max(1.0, grace_seconds),
            )


def _child_env(attempt_dir: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _CHILD_ENV_ALLOW if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(attempt_dir / ".worker-home")
    env["TMPDIR"] = str(attempt_dir / ".worker-tmp")
    env["XDG_CACHE_HOME"] = str(attempt_dir / ".worker-cache")
    env["XDG_CONFIG_HOME"] = str(attempt_dir / ".worker-config")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _write_control(
    attempt_dir: Path,
    control_path: Path,
    control: dict[str, Any],
) -> None:
    for name in (
        ".worker-home",
        ".worker-tmp",
        ".worker-cache",
        ".worker-config",
    ):
        (attempt_dir / name).mkdir(mode=0o700)
    content = json.dumps(
        control,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 0 < len(content) <= _MAX_CONTROL_BYTES:
        raise ExportRenderSubprocessError(
            "renderer_control_size_invalid",
        )
    with control_path.open("xb") as handle:
        handle.write(content)
    control_path.chmod(0o600)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > _MAX_RESULT_BYTES:
        raise ExportRenderSubprocessError(
            "renderer_result_size_invalid",
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ExportRenderSubprocessError(
            "renderer_result_invalid",
        )
    return cast(dict[str, Any], raw)


def _validate_result(
    raw: dict[str, Any],
    fmt: ExportFormat,
    job_id: str,
    snapshot: ExportSnapshot,
    attempt_dir: Path,
) -> ExportRenderOutcome:
    if raw.get("schema_version") != 1 or raw.get("format") != fmt:
        raise ExportRenderSubprocessError(
            "renderer_result_invalid",
        )
    output = raw.get("output")
    manifest = raw.get("manifest")
    if not isinstance(output, dict) or not isinstance(manifest, dict):
        raise ExportRenderSubprocessError(
            "renderer_result_invalid",
        )
    relative_value = output.get("relative_path")
    expected_extension = f".{fmt}"
    if not isinstance(relative_value, str):
        raise ExportRenderSubprocessError(
            "renderer_result_invalid",
        )
    relative_path = Path(relative_value)
    if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.suffix.lower() != expected_extension:
        raise ExportRenderSubprocessError(
            "renderer_result_path_invalid",
        )
    output_path = attempt_dir / relative_path
    root = attempt_dir.resolve()
    if output_path.is_symlink() or not output_path.is_file() or not output_path.resolve().is_relative_to(root):
        raise ExportRenderSubprocessError(
            "renderer_result_path_invalid",
        )
    size_bytes = output.get("size_bytes")
    digest = output.get("content_sha256")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
        or output_path.stat().st_size != size_bytes
        or not isinstance(digest, str)
        or digest != _sha256_file(output_path)
    ):
        raise ExportRenderSubprocessError(
            "renderer_result_content_mismatch",
        )
    if (
        manifest.get("job_id") != job_id
        or manifest.get("snapshot_id") != snapshot.id
        or manifest.get("input_sha256") != snapshot.payload_sha256
        or manifest.get("format") != snapshot.fmt
        or manifest.get("profile") != snapshot.profile
    ):
        raise ExportRenderSubprocessError(
            "renderer_result_manifest_mismatch",
        )
    validation = manifest.get("validation")
    if not isinstance(validation, dict):
        raise ExportRenderSubprocessError(
            "renderer_result_manifest_invalid",
        )
    quality = validation.get("result")
    if quality not in {"clean", "warnings", "rejected"}:
        raise ExportRenderSubprocessError(
            "renderer_result_manifest_invalid",
        )
    return ExportRenderOutcome(
        output_path=output_path,
        manifest=cast(dict[str, Any], manifest),
        quality_status=cast(
            Literal["clean", "warnings", "rejected"],
            quality,
        ),
        warning_count=_warning_count(manifest),
        size_bytes=size_bytes,
    )


def _warning_count(manifest: dict[str, Any]) -> int:
    sections = manifest.get("sections")
    if not isinstance(sections, list):
        raise ExportRenderSubprocessError(
            "renderer_result_manifest_invalid",
        )
    warnings: set[str] = set()
    for raw_section in sections:
        if not isinstance(raw_section, dict):
            raise ExportRenderSubprocessError(
                "renderer_result_manifest_invalid",
            )
        raw_warnings = raw_section.get("warnings")
        if not isinstance(raw_warnings, list):
            raise ExportRenderSubprocessError(
                "renderer_result_manifest_invalid",
            )
        if not all(isinstance(warning, str) for warning in raw_warnings):
            raise ExportRenderSubprocessError(
                "renderer_result_manifest_invalid",
            )
        warnings.update(cast(list[str], raw_warnings))
        reason = raw_section.get("reason_code")
        if reason is not None:
            if not isinstance(reason, str):
                raise ExportRenderSubprocessError(
                    "renderer_result_manifest_invalid",
                )
            warnings.add(reason)
    return len(warnings)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _bounded_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if not minimum <= value <= maximum:
        return default
    return value


__all__ = [
    "ExportRenderLimits",
    "ExportRenderOutcome",
    "ExportRenderSubprocessError",
    "export_render_limits",
    "run_export_format_worker",
]
