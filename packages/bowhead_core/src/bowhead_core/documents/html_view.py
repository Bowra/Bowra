"""Sandbox document worker: capture one model-written HTML or SVG file as the user's preview shows it.

Visual self-check, slice B (docs/sprints/visual-self-check-2026-11-16/tech-design.md §6).
Runs only inside the network-less, read-only sandbox container of the document worker
(``authoring_worker`` operation ``html_view``); the application never opens the file in a
browser. The application sends the product preview's own transform as a browser script
(``bowheadHtmlPreview``, built from ``lib/interactive-html.ts``), so the page here is the
same document in the same sandboxed frame as ``SafeHtmlFrame``.

Writes ``snapshot.png`` (1280 px wide, up to ``MAX_HEIGHT``), ``text.txt`` (the page's
visible text) and, for HTML, ``layout.json`` (the page's blocks, the rows a screen cut must not cross,
elements a model can name and measured layout findings; docs/sprints/visual-geometry-2026-11-27/tech-design.md
§3) to the output directory and returns content-free facts.
"""

from __future__ import annotations

import base64
import hashlib
import json
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


MAX_LAYOUT_BYTES = 65_536
# Measured in the content frame after the viewport has grown to the page, so coordinates match the capture.
# Findings are measurements only, for elements that show text or a picture (decoration may overflow).
_LAYOUT = r"""() => {
  const W = document.documentElement.clientWidth;
  const H = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
  const cut = (text, n) => (text || "").replace(/\s+/g, " ").trim().slice(0, n);
  const shown = (el) => {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const inSvg = (el) => !!(el.parentElement && el.parentElement.closest("svg"));
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height) };
  };
  const selector = (el) => {
    let sel = el.tagName.toLowerCase();
    if (el.id) sel += "#" + el.id;
    else if (el.classList && el.classList.length) sel += "." + [...el.classList].slice(0, 2).join(".");
    return sel.slice(0, 40);
  };
  const label = (el) => cut(el.innerText || el.textContent, 40);
  const content = (el) => !!((el.innerText || el.textContent || "").trim() || el.matches("svg, img, canvas, video, picture") || el.querySelector("svg, img, canvas, video, picture"));
  let root = document.body;
  for (let i = 0; i < 8 && root; i++) {
    const kids = [...root.children].filter(shown);
    const rootH = root.getBoundingClientRect().height || 1;
    const big = kids.filter((k) => k.getBoundingClientRect().height >= 0.9 * rootH);
    if (kids.length === 1) root = kids[0];
    else if (big.length === 1 && kids.length <= 3) root = big[0];
    else break;
  }
  const blockEls = root ? [...root.children].filter(shown).filter((el) => el.getBoundingClientRect().height >= 40).slice(0, 64) : [];
  const blocks = blockEls.map((el) => { const b = box(el); return { y: Math.max(0, b.y), h: Math.max(1, b.h), label: label(el) }; });
  const raw = [];
  const guard = (el) => { const b = box(el); if (b.h >= 4 && b.h <= 800) raw.push([b.y, b.y + b.h]); };
  for (const el of document.querySelectorAll("svg, img, canvas, video, picture, figure, tr, p, h1, h2, h3, h4, h5, h6, li, button, input, textarea, select, blockquote, pre, [role=img]")) {
    if (shown(el) && !inSvg(el)) guard(el);
  }
  for (const el of document.querySelectorAll("div, span, td, th, a, strong, em, b, label, small")) {
    if (shown(el) && [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) guard(el);
  }
  for (const b of blocks) if (b.h <= 800) raw.push([b.y, b.y + b.h]);
  raw.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const guards = [];
  for (const [a, b] of raw) {
    const last = guards[guards.length - 1];
    if (last && a < last[1]) last[1] = Math.max(last[1], b);
    else guards.push([a, b]);
  }
  const elements = [];
  for (const el of document.querySelectorAll("svg, canvas, img, picture, video, table, h1, h2, h3")) {
    if (elements.length >= 120) break;
    if (!shown(el) || inSvg(el)) continue;
    const b = box(el);
    if (b.w < 24 || b.h < 16) continue;
    const tag = el.tagName.toLowerCase();
    const kind = { svg: "chart", canvas: "chart", img: "picture", picture: "picture", video: "picture", table: "table" }[tag] || "heading";
    elements.push({ kind, ...b, label: kind === "chart" || kind === "picture" ? "" : label(el) });
  }
  const findings = [];
  const push = (f) => { if (findings.length < 40) findings.push(f); };
  for (const el of document.querySelectorAll("body *")) {
    if (!shown(el) || el.closest("svg")) continue;
    const s = getComputedStyle(el);
    const cx = ["hidden", "clip"].includes(s.overflowX), cy = ["hidden", "clip"].includes(s.overflowY);
    if (!((cy && el.scrollHeight > el.clientHeight + 2) || (cx && el.scrollWidth > el.clientWidth + 2))) continue;
    const er = el.getBoundingClientRect();
    for (const d of el.querySelectorAll("*")) {
      if (!shown(d) || inSvg(d)) continue;
      if (!(d.matches("svg, img, canvas, video") || [...d.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()))) continue;
      const r = d.getBoundingClientRect();
      if ((cy && (r.bottom > er.bottom + 2 || r.top < er.top - 2)) || (cx && (r.right > er.right + 2 || r.left < er.left - 2))) {
        push({ kind: "clipped", selector: selector(el), text: label(el), box: box(el), other_text: label(d), other_box: box(d) });
        break;
      }
    }
  }
  for (const el of document.querySelectorAll("body *")) {
    if (!shown(el) || inSvg(el) || !content(el)) continue;
    const r = el.getBoundingClientRect();
    const p = el.parentElement ? el.parentElement.getBoundingClientRect() : null;
    if (r.right > W + 1 && (!p || p.right <= W + 1)) push({ kind: "beyond_page_width", selector: selector(el), text: label(el), box: box(el), right: Math.round(r.right + scrollX) });
  }
  for (const t of document.querySelectorAll("svg text")) {
    const svg = t.ownerSVGElement;
    if (!svg || !shown(svg) || !(t.textContent || "").trim()) continue;
    const tr = t.getBoundingClientRect(), sr = svg.getBoundingClientRect();
    const outX = Math.max(0, sr.left - tr.left) + Math.max(0, tr.right - sr.right);
    const outY = Math.max(0, sr.top - tr.top) + Math.max(0, tr.bottom - sr.bottom);
    if (tr.width && (outX >= 0.3 * tr.width || outY >= 0.3 * tr.height)) {
      push({ kind: "chart_text_outside_chart", selector: selector(svg), text: cut(t.textContent, 40), box: box(t), other_box: box(svg) });
    }
  }
  for (const blockEl of blockEls) {
    const br = blockEl.getBoundingClientRect();
    for (const el of blockEl.querySelectorAll("*")) {
      if (!shown(el) || inSvg(el) || !content(el)) continue;
      const r = el.getBoundingClientRect();
      const edge = r.top < br.top - 2 ? "top" : r.bottom > br.bottom + 2 ? "bottom" : "";
      if (edge && r.height < 3000) {
        push({ kind: "outside_block", selector: selector(el), text: label(el), box: box(el), other_text: label(blockEl), other_box: box(blockEl), edge });
        break;
      }
    }
  }
  const lines = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && lines.length < 5000) {
    const n = walker.currentNode;
    if (!n.textContent.trim()) continue;
    const p = n.parentElement;
    if (!p || !shown(p)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) if (r.width > 2 && r.height > 2) lines.push({ el: p, x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height, text: cut(n.textContent, 40) });
  }
  lines.sort((a, b) => a.y - b.y);
  const pairs = new Set();
  const ids = new Map();
  const id = (el) => { if (!ids.has(el)) ids.set(el, ids.size); return ids.get(el); };
  for (let i = 0; i < lines.length; i++) {
    for (let j = i + 1; j < lines.length && lines[j].y < lines[i].y + lines[i].h; j++) {
      const a = lines[i], c = lines[j];
      if (a.el === c.el || a.el.contains(c.el) || c.el.contains(a.el)) continue;
      const ox = Math.min(a.x + a.w, c.x + c.w) - Math.max(a.x, c.x);
      const oy = Math.min(a.y + a.h, c.y + c.h) - Math.max(a.y, c.y);
      if (ox <= 0 || oy <= 0 || ox * oy < 0.25 * Math.min(a.w * a.h, c.w * c.h)) continue;
      const key = id(a.el) + "|" + id(c.el);
      if (pairs.has(key)) continue;
      pairs.add(key);
      push({ kind: "text_overlap", selector: "", text: a.text, box: { x: Math.round(a.x), y: Math.round(a.y), w: Math.round(a.w), h: Math.round(a.h) }, other_text: c.text });
    }
  }
  return { version: 1, width: W, height: H, blocks, guards: guards.slice(0, 1000), elements, findings };
}"""


def _write_layout(frame: Any, output: Path) -> dict[str, Any]:
    """Measure the page and write ``layout.json``; ``{}`` when the measurement fails or does not fit.

    The capture never depends on it: an older application ignores the file, a newer one falls back to pixel cuts.
    """
    try:
        layout = frame.evaluate(_LAYOUT)
    except Exception:  # noqa: BLE001 - a page script can break the measurement; the capture stands
        return {}
    if not isinstance(layout, dict):
        return {}
    data = json.dumps(layout, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Trim the longest lists first until it fits; guards, then findings and elements keep their first entries.
    for key in ("guards", "findings", "elements"):
        while len(data) > MAX_LAYOUT_BYTES and layout.get(key):
            layout[key] = layout[key][: len(layout[key]) // 2]
            data = json.dumps(layout, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_LAYOUT_BYTES:
        return {}
    (output / "layout.json").write_bytes(data)
    return {"layout_sha256": hashlib.sha256(data).hexdigest()}


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
                    facts.update(_write_layout(frame, output))
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
