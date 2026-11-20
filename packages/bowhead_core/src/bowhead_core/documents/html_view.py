"""Sandbox document worker: capture one model-written HTML or SVG file as the user's preview shows it.

Visual self-check, slice B (docs/sprints/visual-self-check-2026-11-16/tech-design.md §6).
Runs only inside the network-less, read-only sandbox container of the document worker
(``authoring_worker`` operation ``html_view``); the application never opens the file in a
browser. The application sends the product preview's own transform as a browser script
(``bowheadHtmlPreview``, built from ``lib/interactive-html.ts``), so the page here is the
same document in the same sandboxed frame as ``SafeHtmlFrame``.

Writes ``snapshot.png`` (1280 px wide, up to ``MAX_HEIGHT``) and ``text.txt`` (the page's
visible text) to the output directory and returns content-free facts.
"""

from __future__ import annotations

import base64
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bowhead_core.documents.package_io import OfficePackageError

WIDTH = 1280
HEIGHT = 800
MAX_HEIGHT = 9600
SETTLE_MS = 1200
TIMEOUT_MS = 20_000
# The whole capture, launch to screenshot. Playwright's evaluate has no timeout, so a page that
# loops after load would otherwise hold a document worker slot until the container limit.
TOTAL_SECONDS = 25
MAX_TEXT_CHARS = 400_000
MAX_SCROLL = 1_000_000
TRANSFORM_VERSION = "html-preview-transform-v1"
# The image installs Debian's headless Chromium; tests point this at a local browser.
CHROMIUM = os.environ.get("BOWHEAD_HTML_VIEW_CHROMIUM", "/usr/bin/chromium-headless-shell")
# One browser process: the worker container has one CPU, and starting the zygote, GPU and
# renderer processes took about 1.3 s of a 6.3 s capture (116, runsc, 2026-11-20). Each capture
# is a fresh container, so a crash still ends only that capture. Text, page size, script-error
# and external-resource facts, WebGL and the time limit were unchanged; glyph edges differ by a pixel.
CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-extensions",
    "--no-first-run",
    "--single-process",
    "--no-zygote",
]
_WRAPPER = (
    '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;overflow:hidden;'
    "background:#ffffff}iframe{display:block;border:0;width:100%;height:100%}</style></head><body></body></html>"
)
# Mirrors SafeHtmlFrame: the transformed document in a sandboxed srcdoc frame, issues relayed by channel.
_MOUNT = """async (raw) => {
  const preview = globalThis.bowheadHtmlPreview;
  if (!preview || preview.version !== "__VERSION__") return { ok: false, error: "transform_unavailable" };
  const built = await preview.build(raw);
  if (!built.ok) return built;
  const frame = document.createElement("iframe");
  globalThis.__bowheadIssues = [];
  addEventListener("message", (event) => {
    if (event.source !== frame.contentWindow || !event.data || event.data.channel !== built.channel) return;
    const issue = { "bowhead-html-runtime": "runtime", "bowhead-html-blocked": "blocked" }[event.data.type];
    if (issue && !globalThis.__bowheadIssues.includes(issue)) globalThis.__bowheadIssues.push(issue);
  });
  frame.title = "HTML content";
  frame.setAttribute("sandbox", preview.sandbox);
  frame.setAttribute("allow", preview.permissions);
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.srcdoc = built.document;
  document.body.appendChild(frame);
  return { ok: true, externalResources: built.externalResources === true };
}""".replace("__VERSION__", TRANSFORM_VERSION)
_MEASURE = """() => {
  const html = document.documentElement;
  const body = document.body;
  return {
    scrollWidth: Math.max(html.scrollWidth, body ? body.scrollWidth : 0),
    scrollHeight: Math.max(html.scrollHeight, body ? body.scrollHeight : 0),
    text: body ? body.innerText : "",
  };
}"""


def _bounded(value: Any) -> int:
    return min(max(int(value), 0), MAX_SCROLL) if isinstance(value, int | float) and value == value else 0


def _content_frame(page: Any) -> Any:
    deadline_ms = TIMEOUT_MS
    while deadline_ms > 0:
        outer = page.main_frame.child_frames[0] if page.main_frame.child_frames else None
        inner = outer.child_frames[0] if outer is not None and outer.child_frames else None
        if inner is not None:
            return inner
        page.wait_for_timeout(50)
        deadline_ms -= 50
    raise OfficePackageError("document_html_render_failed")


def _settle(page: Any) -> None:
    page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.wait_for_timeout(300)


class _Deadline:
    """Ends the worker process after ``TOTAL_SECONDS``; the container, and Chromium with it, then exits."""

    def __init__(self, on_deadline: Callable[[], object] | None) -> None:
        self._lock = threading.Lock()
        self._finished = False
        self._on_deadline = on_deadline
        self._timer = threading.Timer(TOTAL_SECONDS, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        with self._lock:  # Held through exit: the capture can never finish after this point.
            if self._finished:
                return
            try:
                if self._on_deadline is not None:
                    self._on_deadline()
            finally:
                os._exit(0)

    def __enter__(self) -> _Deadline:
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self._finished = True
        self._timer.cancel()


def render_html_view(kind: str, content: bytes, transform: bytes, output: Path, *, on_deadline: Callable[[], object] | None = None) -> dict[str, Any]:
    """Capture ``content``; raise ``OfficePackageError`` with a ``document_html_*`` code on failure.

    ``on_deadline`` runs on a timer thread when the capture exceeds ``TOTAL_SECONDS``, just before
    the process exits; the worker writes its time-limit result there.
    """
    if kind not in {"html", "svg"}:
        raise OfficePackageError("document_authoring_control_invalid")
    with _Deadline(on_deadline):
        return _capture(kind, content, transform, output)


def _capture(kind: str, content: bytes, transform: bytes, output: Path) -> dict[str, Any]:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    text = content.decode("utf-8", errors="replace")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=CHROMIUM,
                headless=True,
                # The container is the sandbox: no network, no secrets, read-only, bounded.
                chromium_sandbox=False,
                args=CHROMIUM_ARGS,
            )
            try:
                context = browser.new_context(
                    viewport={"width": WIDTH, "height": HEIGHT},
                    device_scale_factor=1,
                    color_scheme="light",
                    reduced_motion="reduce",
                    service_workers="block",
                )
                context.set_default_timeout(TIMEOUT_MS)
                page = context.new_page()
                page.set_content(_WRAPPER)
                facts: dict[str, Any] = {"issues": [], "external_resources": False}
                if kind == "html":
                    page.add_script_tag(content=transform.decode("utf-8"))
                    mounted = page.evaluate(_MOUNT, text)
                    if not isinstance(mounted, dict) or not mounted.get("ok"):
                        error = mounted.get("error") if isinstance(mounted, dict) else None
                        raise OfficePackageError("document_html_resource_limited" if error == "resource_limited" else "document_html_render_failed")
                    facts["external_resources"] = mounted.get("externalResources") is True
                    frame = _content_frame(page)
                    frame.wait_for_load_state("load")
                    page.wait_for_timeout(SETTLE_MS)
                    measured = frame.evaluate(_MEASURE)
                    measured = measured if isinstance(measured, dict) else {}
                    scroll_width, scroll_height = _bounded(measured.get("scrollWidth")), _bounded(measured.get("scrollHeight"))
                    visible = measured.get("text") if isinstance(measured.get("text"), str) else ""
                    height = min(max(scroll_height, HEIGHT), MAX_HEIGHT)
                    if height > HEIGHT:
                        page.set_viewport_size({"width": WIDTH, "height": height})
                        _settle(page)
                    page.screenshot(path=str(output / "snapshot.png"), type="png", animations="disabled")
                    issues = page.evaluate("() => globalThis.__bowheadIssues || []")
                    facts["issues"] = [issue for issue in issues if issue in {"runtime", "blocked"}] if isinstance(issues, list) else []
                else:
                    source = "data:image/svg+xml;base64," + base64.b64encode(content).decode("ascii")
                    page.evaluate(
                        """(source) => new Promise((resolve, reject) => {
                          const image = document.createElement("img");
                          image.style.cssText = "display:block;max-width:__WIDTH__px;height:auto";
                          image.onload = () => resolve(true);
                          image.onerror = () => reject(new Error("svg"));
                          image.src = source;
                          document.body.appendChild(image);
                        })""".replace("__WIDTH__", str(WIDTH)),
                        source,
                    )
                    box = page.locator("img").bounding_box()
                    if not box or box["width"] < 1 or box["height"] < 1:
                        raise OfficePackageError("document_html_render_failed")
                    width, height = min(int(box["width"] + 0.999), WIDTH), min(int(box["height"] + 0.999), MAX_HEIGHT)
                    if height > HEIGHT:
                        page.set_viewport_size({"width": WIDTH, "height": height})
                        _settle(page)
                    # A clip, not an element capture: an element capture ignores the viewport and is unbounded.
                    page.screenshot(path=str(output / "snapshot.png"), type="png", clip={"x": 0, "y": 0, "width": width, "height": height})
                    scroll_width, scroll_height, visible = _bounded(box["width"]), _bounded(box["height"]), ""
            finally:
                browser.close()
    except PlaywrightTimeout as error:
        raise OfficePackageError("document_html_time_limit") from error
    except PlaywrightError as error:
        raise OfficePackageError("document_html_render_failed") from error
    (output / "text.txt").write_text(visible[:MAX_TEXT_CHARS], encoding="utf-8")
    return {"scroll_width": scroll_width, "scroll_height": scroll_height, **facts}
