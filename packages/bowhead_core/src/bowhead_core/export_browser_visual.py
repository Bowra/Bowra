"""Playwright subprocess adapter for the frontend visual export route."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bowhead_core.chart_style_export import (
    CHART_EXPORT_SURFACE_CONTRACT_VERSION,
    ChartStyleExportError,
    parse_chart_style_export_theme,
)
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_ir import ExportIRBlock
from bowhead_core.export_materializer import BrowserRenderedVisual
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import PrintThemeError, parse_print_theme_snapshot

MAX_BROWSER_INPUT_BYTES = 4 * 1024 * 1024
MAX_BROWSER_OUTPUT_BYTES = 50 * 1024 * 1024
_SUPPORTED_KINDS = frozenset({"chart", "spec_block"})
_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "NODE_OPTIONS",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "TMPDIR",
    }
)


class BrowserVisualBackendError(CodedError):
    """The browser renderer is unavailable or returned an untrusted result."""


@dataclass(frozen=True, slots=True)
class PlaywrightVisualRenderer:
    attempt_dir: Path
    frontend_dir: Path
    frontend_base_url: str
    locale: str
    timezone: str
    theme: dict[str, Any]
    node_binary: Path
    script_path: Path
    print_theme: dict[str, Any]
    timeout_seconds: float = 60.0
    extra_environment: dict[str, str] = field(default_factory=dict)
    name: str = "canvas-browser-visual-v1"
    version: str = EXPORT_VISUAL_RENDERER_VERSION

    def __post_init__(self) -> None:
        _validate_configuration(self)

    async def render(
        self,
        block: ExportIRBlock,
        *,
        asset_paths: dict[str, Path],
    ) -> BrowserRenderedVisual:
        if block.source.section_kind not in _SUPPORTED_KINDS or block.visual_input_sha256 is None:
            raise BrowserVisualBackendError("visual_browser_kind_invalid")
        _validate_pinned_chart_data(
            block=block,
            asset_paths=asset_paths,
        )
        working_dir = self.attempt_dir / "browser"
        working_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if working_dir.is_symlink() or not working_dir.is_dir():
            raise BrowserVisualBackendError("visual_browser_staging_invalid")
        digest = hashlib.sha256(block.block_key.encode("utf-8")).hexdigest()[:24]
        input_path = working_dir / f"{digest}.json"
        output_path = working_dir / f"{digest}.png"
        if input_path.exists() or input_path.is_symlink() or output_path.exists() or output_path.is_symlink():
            raise BrowserVisualBackendError("visual_browser_staging_conflict")
        request = {
            "frontendBaseUrl": self.frontend_base_url,
            "locale": self.locale,
            "timezone": self.timezone,
            "timeoutMs": int(self.timeout_seconds * 1000),
            "payload": {
                "blockKey": block.block_key,
                "sectionKind": block.source.section_kind,
                "payload": block.content.get("payload"),
                "locale": self.locale,
                "theme": self.theme,
                "printTheme": self.print_theme,
                "visualInputSha256": block.visual_input_sha256,
                "rendererVersion": self.version,
                **(
                    {
                        "exportSurfaceContractVersion": (CHART_EXPORT_SURFACE_CONTRACT_VERSION),
                        **({"frozenPresentation": block.content.get("frozen_chart_presentation")} if block.source.section_kind == "chart" else {}),
                    }
                    if self.theme.get("schemaVersion") == 2
                    else {}
                ),
            },
        }
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if not 0 < len(encoded) <= MAX_BROWSER_INPUT_BYTES:
            raise BrowserVisualBackendError("visual_browser_input_size_invalid")
        try:
            with input_path.open("xb") as stream:
                stream.write(encoded)
            environment = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}
            environment.update(self.extra_environment)
            try:
                completed = await _run_browser_process(
                    command=(
                        str(self.node_binary),
                        str(self.script_path),
                        str(input_path),
                        str(output_path),
                    ),
                    cwd=self.frontend_dir,
                    env=environment,
                    timeout_seconds=self.timeout_seconds + 5,
                )
            except TimeoutError as exc:
                raise BrowserVisualBackendError("visual_browser_timeout") from exc
            if completed.returncode != 0:
                raise BrowserVisualBackendError(
                    "visual_browser_failed",
                    stderr=_safe_stderr(completed.stderr),
                )
            receipt = _parse_receipt(completed.stdout)
            _validate_receipt(
                receipt=receipt,
                block=block,
                renderer_version=self.version,
                renderer_contract_version=int(self.theme["schemaVersion"]),
            )
            if output_path.is_symlink() or not output_path.is_file() or not output_path.resolve().is_relative_to(working_dir.resolve()):
                raise BrowserVisualBackendError("visual_browser_output_invalid")
            content = output_path.read_bytes()
            if not 0 < len(content) <= MAX_BROWSER_OUTPUT_BYTES:
                raise BrowserVisualBackendError("visual_browser_output_size_invalid")
            warnings = receipt.get("warnings")
            return BrowserRenderedVisual(
                content=content,
                mime="image/png",
                visual_input_sha256=block.visual_input_sha256,
                warnings=tuple(item for item in warnings if isinstance(item, str)) if isinstance(warnings, list) else (),
            )
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)


def _validate_configuration(
    renderer: PlaywrightVisualRenderer,
) -> None:
    try:
        parsed_theme = parse_chart_style_export_theme(renderer.theme)
        parsed_print = parse_print_theme_snapshot(renderer.print_theme)
    except (ChartStyleExportError, PrintThemeError) as exc:
        raise BrowserVisualBackendError("visual_browser_config_invalid") from exc
    if parsed_theme.get("schemaVersion") == 2 and parsed_theme.get("structuralThemeChecksum") != parsed_print["checksum"]:
        raise BrowserVisualBackendError("visual_browser_config_invalid")
    if (
        not renderer.attempt_dir.is_absolute()
        or not renderer.frontend_dir.is_absolute()
        or not renderer.frontend_dir.is_dir()
        or not renderer.node_binary.is_absolute()
        or not renderer.node_binary.is_file()
        or not renderer.script_path.is_absolute()
        or not renderer.script_path.is_file()
        or not renderer.script_path.is_relative_to(renderer.frontend_dir)
        or renderer.locale not in {"zh-CN", "en-US", "de-DE", "fr-FR"}
        or not renderer.timezone
        or not 1 <= renderer.timeout_seconds <= 120
        or renderer.version != EXPORT_VISUAL_RENDERER_VERSION
        or any(key not in _ENV_ALLOWLIST for key in renderer.extra_environment)
    ):
        raise BrowserVisualBackendError("visual_browser_config_invalid")
    parsed = urlsplit(renderer.frontend_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserVisualBackendError("visual_browser_config_invalid")


def chart_style_theme_from_snapshot(snapshot: Any) -> dict[str, Any]:
    request = snapshot.payload.get("request")
    theme = request.get("theme") if isinstance(request, dict) else None
    try:
        return parse_chart_style_export_theme(theme)
    except ChartStyleExportError as exc:
        raise BrowserVisualBackendError("visual_browser_config_invalid") from exc


def print_theme_from_snapshot(snapshot: Any) -> dict[str, Any]:
    request = snapshot.payload.get("request")
    print_theme = request.get("print_theme") if isinstance(request, dict) else None
    try:
        return parse_print_theme_snapshot(print_theme)
    except PrintThemeError as exc:
        raise BrowserVisualBackendError("visual_browser_config_invalid") from exc


def _validate_pinned_chart_data(
    *,
    block: ExportIRBlock,
    asset_paths: dict[str, Path],
) -> None:
    if block.source.section_kind != "chart" or not asset_paths:
        return
    payload = block.content.get("payload")
    spec = payload.get("spec") if isinstance(payload, dict) else None
    rows = spec.get("rows") if isinstance(spec, dict) else None
    if not isinstance(rows, list):
        # Fetching a live artifact preview here would violate the snapshot.
        # A pinned internal resolver is required before this shape is enabled.
        raise BrowserVisualBackendError("visual_pinned_data_resolver_required")


def _parse_receipt(value: bytes) -> dict[str, Any]:
    if len(value) > 16_384:
        raise BrowserVisualBackendError("visual_browser_receipt_invalid")
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserVisualBackendError("visual_browser_receipt_invalid") from exc
    if not isinstance(parsed, dict):
        raise BrowserVisualBackendError("visual_browser_receipt_invalid")
    return parsed


def _validate_receipt(
    *,
    receipt: dict[str, Any],
    block: ExportIRBlock,
    renderer_version: str,
    renderer_contract_version: int,
) -> None:
    if (
        receipt.get("mime") != "image/png"
        or receipt.get("rendererVersion") != renderer_version
        or receipt.get("visualInputSha256") != block.visual_input_sha256
        or receipt.get("width") != 1600
        or receipt.get("height") != 900
        or (
            renderer_contract_version == 2
            and (
                receipt.get("exportSurfaceContractVersion") != CHART_EXPORT_SURFACE_CONTRACT_VERSION
                or (
                    block.source.section_kind == "chart"
                    and receipt.get("presentationChecksum") != (block.content.get("frozen_chart_presentation") or {}).get("presentationChecksum")
                )
            )
        )
    ):
        raise BrowserVisualBackendError("visual_browser_receipt_mismatch")


def _safe_stderr(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")[:500]


async def _run_browser_process(
    *,
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    if os.name != "posix":
        raise BrowserVisualBackendError(
            "visual_browser_process_isolation_unavailable",
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
        await _finish_browser_process(process)
        raise BrowserVisualBackendError("visual_browser_pipes_unavailable")
    stdout_task = asyncio.create_task(
        _read_browser_stream(
            process.stdout,
            limit=16_384,
            reject_overflow=True,
        )
    )
    stderr_task = asyncio.create_task(
        _read_browser_stream(
            process.stderr,
            limit=16_384,
            reject_overflow=False,
        )
    )
    wait_task = asyncio.create_task(process.wait())
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr, returncode = await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
            )
    except BaseException:
        await _finish_browser_process(process)
        raise
    finally:
        for task in (stdout_task, stderr_task, wait_task):
            task.cancel()
        await asyncio.gather(
            stdout_task,
            stderr_task,
            wait_task,
            return_exceptions=True,
        )
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


async def _read_browser_stream(
    stream: asyncio.StreamReader,
    *,
    limit: int,
    reject_overflow: bool,
) -> bytes:
    captured = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return bytes(captured)
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if reject_overflow and len(chunk) > remaining:
            raise BrowserVisualBackendError(
                "visual_browser_receipt_invalid",
            )


async def _finish_browser_process(
    process: asyncio.subprocess.Process,
) -> None:
    termination = asyncio.create_task(_terminate_browser_process_group(process))
    try:
        await asyncio.shield(termination)
    except asyncio.CancelledError:
        await termination


async def _terminate_browser_process_group(
    process: asyncio.subprocess.Process,
) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=3.0,
            )
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=3.0,
            )


__all__ = [
    "BrowserVisualBackendError",
    "PlaywrightVisualRenderer",
    "chart_style_theme_from_snapshot",
    "print_theme_from_snapshot",
]
