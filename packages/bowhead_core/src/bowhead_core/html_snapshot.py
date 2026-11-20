"""Visual self-check, slice B: capture a model-written HTML or SVG file in the sandbox.

Design: docs/sprints/visual-self-check-2026-11-16/tech-design.md §6. The application never
opens the file in a browser. It sends the file and the product preview's own transform
(``html-preview-transform.js``, built from ``lib/html-preview-sandbox-entry.ts`` with the
frontend) to the sandbox executor's document worker (operation ``html_view``,
``documents/html_view.py``). The worker runs Chromium in the network-less, read-only,
resource-bounded worker container and shows the transformed document in the same sandboxed
frame as ``SafeHtmlFrame``.

Each process runs at most as many captures at once as the executor has document run slots
(``document_slots``, sized to the host; two for an executor that does not say), a further one
waits up to 5 s, and the worker ends a capture after 25 s.
Failures are reported as a ``visual_view`` reason and never fail the Tool call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

TRANSFORM_NAME = "html-preview-transform.js"
# Container start, Chromium launch and the worker's own 25 s limit.
WAIT_SECONDS = 40
READINESS_WAIT_SECONDS = 10
QUEUE_WAIT_SECONDS = 5
DEFAULT_CONCURRENT = 2
MAX_CONCURRENT = 16
_semaphores: dict[tuple[int, int], asyncio.Semaphore] = {}
_transform_cache: dict[Path, tuple[float, bytes]] = {}


class HtmlSnapshotUnavailable(Exception):
    """The capture did not complete; ``reason`` is a ``visual_view`` reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class HtmlSnapshot:
    png: bytes
    text: str
    scroll_width: int
    scroll_height: int
    issues: tuple[str, ...]
    external_resources: bool


def transform_path() -> Path:
    """The built preview transform: in the app image under the export runtime, locally under the frontend."""
    root = os.environ.get("BOWHEAD_EXPORT_FRONTEND_DIR") or str(Path(__file__).resolve().parents[3] / "bowhead_frontend")
    return Path(root) / "dist" / TRANSFORM_NAME


def _transform() -> bytes | None:
    from bowhead_core.documents.authoring_protocol import MAX_HTML_VIEW_TRANSFORM

    path = transform_path()
    try:
        stat = path.stat()
        if path.is_symlink() or not path.is_file() or not 0 < stat.st_size <= MAX_HTML_VIEW_TRANSFORM:
            return None
        cached = _transform_cache.get(path)
        if cached is not None and cached[0] == stat.st_mtime:
            return cached[1]
        data = path.read_bytes()
    except OSError:
        return None
    _transform_cache[path] = (stat.st_mtime, data)
    return data


def _semaphore(slots: int) -> asyncio.Semaphore:
    # A changed slot count (an executor restarted on another host size) starts a new limit;
    # captures holding the old one finish under it, and the executor still enforces its own.
    key = (id(asyncio.get_running_loop()), slots)
    if key not in _semaphores:
        _semaphores[key] = asyncio.Semaphore(slots)
    return _semaphores[key]


def concurrent_captures(readiness: dict) -> int:
    slots = readiness.get("document_slots")
    return slots if type(slots) is int and 1 <= slots <= MAX_CONCURRENT else DEFAULT_CONCURRENT


_REASONS = {
    "document_html_time_limit": "time_limit",
    "document_html_resource_limited": "too_large",
    "document_html_render_failed": "render_failed",
    "document_authoring_output_invalid": "render_failed",
}


async def capture(kind: str, content: bytes) -> HtmlSnapshot:
    """Capture ``content`` (an HTML or SVG file) as one PNG at 1280 px wide, up to 9600 px tall."""
    from bowhead_core.documents.authoring_protocol import MAX_HTML_VIEW_SOURCE

    if kind not in {"html", "svg"}:
        raise HtmlSnapshotUnavailable("render_failed")
    if not 0 < len(content) <= MAX_HTML_VIEW_SOURCE:
        raise HtmlSnapshotUnavailable("too_large")
    root, url, token = (
        os.environ.get(name, "") for name in ("BOWHEAD_SKILL_WORKDIR_ROOT", "BOWHEAD_SANDBOX_EXECUTOR_URL", "BOWHEAD_SANDBOX_EXECUTOR_TOKEN")
    )
    transform = _transform()
    if not root or not url or not token or transform is None:
        raise HtmlSnapshotUnavailable("renderer_unavailable")
    from bowhead_core.documents.authoring_executor import AuthoringExecutor

    executor = AuthoringExecutor(root=Path(root), url=url, token=token)
    try:
        async with asyncio.timeout(READINESS_WAIT_SECONDS):
            readiness = await executor.capabilities()
    except Exception as exc:
        _LOG.info("html_snapshot.failed kind=%s error_type=%s code=%s", kind, type(exc).__name__, getattr(exc, "code", ""))
        raise HtmlSnapshotUnavailable("renderer_unavailable") from exc
    semaphore = _semaphore(concurrent_captures(readiness))
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=QUEUE_WAIT_SECONDS)
    except TimeoutError as exc:
        raise HtmlSnapshotUnavailable("renderer_busy") from exc
    started = time.perf_counter()
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            view = await executor.view_html(kind, content, transform, readiness=readiness)
    except TimeoutError as exc:
        raise HtmlSnapshotUnavailable("time_limit") from exc
    except Exception as exc:
        code = getattr(exc, "code", "")
        _LOG.info("html_snapshot.failed kind=%s error_type=%s code=%s", kind, type(exc).__name__, code)
        raise HtmlSnapshotUnavailable(_REASONS.get(code, "renderer_unavailable")) from exc
    finally:
        semaphore.release()
    _LOG.info("html_snapshot.captured kind=%s ms=%.0f bytes=%d", kind, (time.perf_counter() - started) * 1000, len(view.png))
    return HtmlSnapshot(
        png=view.png,
        text=view.text,
        scroll_width=view.scroll_width,
        scroll_height=view.scroll_height,
        issues=view.issues,
        external_resources=view.external_resources,
    )
