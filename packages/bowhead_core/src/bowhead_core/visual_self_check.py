"""Visual self-check: per-run state, renders of saved deliverables, and the render message.

Design: docs/sprints/visual-self-check-2026-11-16/tech-design.md §3–§5 and §7.1.

Every run (one Lead or helper execution) registers a ``VisualRun``. When its primary model
reads images, each deliverable a Tool call saves (PPTX, PNG/JPEG/WebP, PDF) is rendered; the Tool result carries a content-free
``visual_view`` summary and the images wait in ``VisualRun.pending`` for the next model
request, where they travel as one ``bowhead_visual_view_v1`` message that is never
persisted. A render a ``view_file`` call asked for brings back the pages of the same file
version shown in earlier requests (``earlier_views``, 2026-11-23, page by page since
2026-11-24), so a model looking screen by screen has the whole file in one request; any other
request carries no earlier images. A save the model declares a presentation-only fix (``presentation_fix``) is
compared with the version of that file the model last saw, and content differences are
returned as ``content_changes`` (decision D12, 2026-11-17: a file built in steps is not
compared, so filling it is never reported as a change).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

import anyio
from bowhead_router.runtime_notes import VISUAL_VIEW_NOTE_NAME
from bowhead_router.types import ImageRef, LLMRequest, Message

from bowhead_core.product_identity import product_name

_LOG = logging.getLogger(__name__)

VIEW_FILE_TOOL = "view_file"
MAX_IMAGES_PER_REQUEST = 4
# A request carrying a view_file render holds up to this many images, earlier pages of the same file version included.
MAX_IMAGES_WITH_EARLIER_PAGES = 8
# Renders whose images reached a model, kept so a later view_file of the same file version can bring their pages back.
_MAX_SEEN_RENDERS = 8
RENDER_WAIT_SECONDS = 30
_MAX_RUNS = 512
_IMAGE_MIMES = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "image/svg+xml": "svg",
}
_IMAGE_SUFFIXES = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
    ".svg": "svg",
}
PRESENTATION_FIX_FIELD = "presentation_fix"
PRESENTATION_FIX_SCHEMA = {
    "type": "boolean",
    "description": (
        "Set true only when this call fixes the presentation of files you already saw rendered (position, size, font, "
        f"colour, wrapping, chart type) without changing their content. {product_name()} then compares each saved file with the "
        "version you last saw and reports any text, number, row or chart value that changed. Omit it for calls that "
        "create or change content."
    ),
}
CONTENT_CHANGES_NOTE = (
    "You declared this save a presentation-only fix, but its content differs from the version you last saw. Undo these "
    "changes with another presentation_fix save unless the user asked for them; otherwise say so in your answer."
)
_MAX_KEPT_PDFS = 3
_MAX_KEPT_PDF_BYTES = 8 * 1024 * 1024
_MAX_KEPT_CAPTURES = 3
_MAX_KEPT_CAPTURE_BYTES = 8 * 1024 * 1024
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0", ""})
_CHECK_INSTRUCTIONS = (
    "Check it as the user will see it: overflow or clipping, overlapping elements, empty or missing content, boxes instead "
    "of characters, unreadable text, leftover placeholder text, charts that cannot be read. Fix only presentation: "
    "position, size, font, colour, wrapping, chart type. Do not add, change or remove text, numbers, names, sources or "
    "data. When data is missing or looks wrong, keep it and say so in the file or in your answer. Set presentation_fix "
    f"to true on a call that only fixes presentation, so {product_name()} can confirm that no content changed."
)
# Renderer outcomes of the layout facts (pptx_result_view._render_pdf) as visual_view reasons.
_RENDER_REASONS = {
    "time_limit": "time_limit",
    "renderer_unavailable": "renderer_unavailable",
    "file_not_rendered": "render_failed",
    "size_limit": "too_large",
    "file_size_limit": "too_large",
}


@dataclass
class PendingRender:
    """A shown render waiting for the next model request. ``view`` holds the images."""

    call_id: str
    file: str
    view: dict[str, Any]
    # (file identity, content digest) of the version shown; None when unknown.
    version: tuple[str, str] | None = None
    # A render a view_file call asked for; only these bring back earlier views of the same version.
    requested: bool = False
    # The images went out with the last request built (not the not-sent note).
    sent: bool = False


@dataclass
class EarlierView:
    """The images of one earlier render that come back with a requested view, in the render's order."""

    render: PendingRender
    images: list[dict[str, Any]]


@dataclass
class VisualRun:
    """Visual self-check state of one agent run. ``enabled``: the primary model reads images."""

    enabled: bool
    pending: list[PendingRender] = field(default_factory=list)
    # Canonical file identity by lineage root and by file name, so versions and same-name saves agree.
    identities: dict[str, str] = field(default_factory=dict)
    slide_hashes: dict[str, dict[str, Any]] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    # Per file: the content snapshot of the version the model last saw (D12).
    baselines: dict[str, dict[str, Any]] = field(default_factory=dict)
    # File identity of each artifact saved in this run, so a fix that reads it can find its baseline.
    artifact_identities: dict[str, str] = field(default_factory=dict)
    # Executor PDFs of presentations rendered in this run, by artifact ID, so view_file need not render again.
    pdfs: dict[str, bytes] = field(default_factory=dict)
    # Sandbox captures of HTML and SVG content in this run, by kind and content digest: unchanged
    # content is not captured again (view_file, or the same bytes saved under another name).
    captures: dict[tuple[str, str], Any] = field(default_factory=dict)
    # Renders whose images reached a model, oldest first (`settle_renders`); one file version each.
    seen: list[PendingRender] = field(default_factory=list)

    def keep_pdf(self, artifact_id: str, pdf: bytes | None) -> None:
        if pdf is None or len(pdf) > _MAX_KEPT_PDF_BYTES:
            return
        self.pdfs.pop(artifact_id, None)
        while len(self.pdfs) >= _MAX_KEPT_PDFS:
            self.pdfs.pop(next(iter(self.pdfs)))
        self.pdfs[artifact_id] = pdf

    def keep_capture(self, key: tuple[str, str], capture: Any) -> None:
        if len(capture.png) > _MAX_KEPT_CAPTURE_BYTES:
            return
        self.captures.pop(key, None)
        while len(self.captures) >= _MAX_KEPT_CAPTURES:
            self.captures.pop(next(iter(self.captures)))
        self.captures[key] = capture


_RUNS: dict[tuple[str, str], VisualRun] = {}


def agent_key(profile_name: str, subagent_id: str) -> str:
    """The key a Tool call finds through ``ToolStepRef.parent_agent_id`` (None for Lead)."""
    return "lead" if profile_name == "lead" else subagent_id


def begin_run(turn_id: str | None, key: str, *, accepts_images: bool) -> VisualRun | None:
    """Register a run; None without a Turn."""
    if not turn_id:
        return None
    run = VisualRun(enabled=accepts_images)
    _RUNS.pop((turn_id, key), None)
    while len(_RUNS) >= _MAX_RUNS:
        _RUNS.pop(next(iter(_RUNS)))
    _RUNS[(turn_id, key)] = run
    return run


def end_run(turn_id: str | None, key: str, run: VisualRun | None) -> None:
    """Drop the run's state, including renders that never reached a model (FR7: never persisted)."""
    if run is not None and turn_id and _RUNS.get((turn_id, key)) is run:
        del _RUNS[(turn_id, key)]


def run_for(ctx: Any) -> tuple[VisualRun, str] | None:
    """The run and Tool call ID of the call ``ctx`` is serving."""
    step = getattr(ctx, "tool_step", None)
    turn = getattr(ctx, "turn_id", None)
    if step is None or not turn:
        return None
    run = _RUNS.get((turn, step.parent_agent_id or "lead"))
    return (run, str(step.call_id)) if run is not None else None


def with_presentation_fix(input_schema: dict[str, Any]) -> dict[str, Any]:
    """A producer Tool's schema with the optional ``presentation_fix`` field."""
    properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
    if not isinstance(properties, dict) or PRESENTATION_FIX_FIELD in properties:
        return input_schema
    return {**input_schema, "properties": {**properties, PRESENTATION_FIX_FIELD: PRESENTATION_FIX_SCHEMA}}


def normalize_presentation_fix(value: Any) -> tuple[bool, str | None]:
    """(declared, reason the value was ignored). Omitted, null and false mean not declared."""
    if value is None or isinstance(value, bool):
        return bool(value), None
    if isinstance(value, int) and value in (0, 1):
        return bool(value), None
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE, None
    return False, "presentation_fix must be true or false; this save was not checked as a presentation fix"


def deliverable_kind(mime: str | None, name: str | None) -> str | None:
    """``pptx``, ``image``, ``pdf`` (slice A), ``html`` or ``svg`` (slice B) for a renderable deliverable; None otherwise."""
    from bowhead_core.pptx_result_view import is_pptx

    if is_pptx(mime, name):
        return "pptx"
    base = str(mime or "").split(";", 1)[0].strip().lower()
    if base in _IMAGE_MIMES:
        return _IMAGE_MIMES[base]
    lowered = str(name or "").lower()
    return next((kind for suffix, kind in _IMAGE_SUFFIXES.items() if lowered.endswith(suffix)), None)


def _identity(run: VisualRun, artifact: Any) -> str:
    keys = (f"lineage:{artifact.lineage_root}", f"name:{artifact.filename}")
    canonical = next((run.identities[key] for key in keys if key in run.identities), keys[0])
    for key in keys:
        run.identities[key] = canonical
    run.artifact_identities[str(artifact.id)] = canonical
    return canonical


def _summary(kind: str, view: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"status": view["status"], "kind": kind}
    for key in (
        "reason",
        "pages",
        "shown",
        "not_shown",
        "substituted_fonts",
        "runtime_issues",
        "scroll_width",
        "scroll_height",
        "external_resources_removed",
    ):
        if view.get(key) is not None:
            summary[key] = view[key]
    if view["status"] == "shown":
        summary["next_message"] = True
    return summary


async def _worker(control: dict[str, Any], data: bytes, extra: dict[str, bytes] | None = None) -> dict[str, Any] | None:
    from bowhead_core.untrusted_parse import run_worker_json

    result, outcome = await run_worker_json("visual_view", control, data=data, extra_files=extra)
    if outcome != "ok" or not isinstance(result, dict):
        _LOG.info("visual_view.worker_failed op=%s outcome=%s", control.get("op"), outcome)
        return None
    return result


async def _read(ctx: Any, artifact: Any) -> bytes | None:
    from bowhead_core.pptx_result_view import MAX_VIEW_BYTES

    if not 0 < artifact.size <= MAX_VIEW_BYTES:
        return None
    data = await ctx.artifact_store.backend.read_bounded(artifact.path, limit=MAX_VIEW_BYTES)
    return data if len(data) == artifact.size else None


async def _render(
    run: VisualRun, ctx: Any, artifact: Any, identity: str, kind: str, rendered: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(view with images, content snapshot or None)."""
    data = rendered.get("data") or await _read(ctx, artifact)
    if data is None:
        return {"status": "not_completed", "reason": "too_large"}, None
    rendered["sha256"] = hashlib.sha256(data).hexdigest()
    if kind == "pptx":
        pdf = rendered.get("pdf")
        # One worker process: hashes, snapshot, changed slides and their view.
        control = {"op": "pptx_render", "previous_hashes": run.slide_hashes.get(identity), "render": pdf is not None}
        inspected = await _worker(control, data, {"pdf": pdf} if pdf is not None else None)
        if inspected is None:
            return {"status": "not_completed", "reason": "render_failed"}, None
        snapshot = inspected["snapshot"]
        if inspected["changed_pages"] == []:
            return {"status": "skipped", "reason": "unchanged"}, snapshot
        if pdf is None:
            return {"status": "not_completed", "reason": _RENDER_REASONS.get(rendered.get("outcome") or "", "renderer_unavailable")}, snapshot
        view = inspected.get("view")
        if not isinstance(view, dict):
            return {"status": "not_completed", "reason": "render_failed"}, snapshot
        if view.get("status") == "shown":
            run.slide_hashes[identity] = inspected["hashes"]
            run.keep_pdf(artifact.id, pdf)
        return view, snapshot
    digest = rendered["sha256"]
    if run.digests.get(identity) == digest:
        return {"status": "skipped", "reason": "unchanged"}, None
    if kind in {"html", "svg"}:
        view, snapshot = await _render_page(run, kind, data, None, "sheet")
        if view.get("status") == "shown":
            run.digests[identity] = digest
        return view, snapshot
    view = await _worker({"op": "pdf_view" if kind == "pdf" else "image_view", "snapshot": kind == "pdf"}, data)
    if view is None:
        return {"status": "not_completed", "reason": "render_failed"}, None
    if view.get("status") == "shown":
        run.digests[identity] = digest
    return view, view.pop("snapshot", None)


def _fix_baseline(run: VisualRun, identity: str, inputs: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """The version a declared fix changes: the saved file's own, else the one seen file the call read.

    Models often fix a file by reading it and saving under a new name (final2.pptx from
    final.pptx; 116 acceptance 2026-11-18), so the file read is the version the fix starts from.
    """
    if identity in run.baselines:
        return run.baselines[identity], None
    read = {run.artifact_identities[artifact_id] for artifact_id in inputs if artifact_id in run.artifact_identities}
    seen = [run.baselines[read_identity] for read_identity in sorted(read) if read_identity in run.baselines]
    if len(seen) == 1:
        return seen[0], None
    if seen:
        return None, "the call read several files you saw; save the fix under the name of the file it changes"
    return None, "no rendered view of this file, or of a file this call read, was shown earlier in this run"


def _check_presentation_fix(
    output: dict[str, Any],
    run: VisualRun,
    identity: str,
    kind: str,
    snapshot: dict[str, Any] | None,
    view: dict[str, Any],
    inputs: list[str],
) -> dict[str, Any] | None:
    """Compare a declared presentation fix with the version the model last saw.

    Returns the baseline when content changed, so the caller keeps it for this file.
    """
    if kind == "image":
        output["presentation_check"] = {"status": "not_checked", "reason": "images have no text to compare"}
        return None
    baseline, missing = _fix_baseline(run, identity, inputs)
    if baseline is None:
        output["presentation_check"] = {"status": "not_checked", "reason": missing}
        return None
    if snapshot is None:
        reason = view.get("reason")
        if reason == "unchanged":
            output["presentation_check"] = {"status": "no_content_change"}
        else:
            output["presentation_check"] = {"status": "not_checked", "reason": "the saved content could not be read"}
        return None
    from bowhead_core.visual_view import compare_content

    changes = compare_content(baseline["snapshot"], snapshot)
    if changes is None:
        output["presentation_check"] = {"status": "no_content_change"}
        return None
    output["presentation_check"] = {"status": "content_changed"}
    output["content_changes"] = {"against": baseline["against"], **changes, "note": CONTENT_CHANGES_NOTE}
    _LOG.info("visual_view.content_changes kind=%s %s", kind, " ".join(f"{name}={len(items)}" for name, items in changes.items()))
    return baseline


async def _render_page(run: VisualRun, kind: str, data: bytes, pages: list[int] | None, detail: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Slice B: an HTML or SVG file through the product preview's transform in the sandbox Chromium (§6).

    Content captured earlier in the run is not captured again. An SVG's content snapshot is its
    ``<text>`` elements, read from the file by the worker.
    """
    from bowhead_core import html_snapshot
    from bowhead_core.visual_view import html_content_snapshot

    key = (kind, hashlib.sha256(data).hexdigest())
    snapshot = run.captures.get(key)
    if snapshot is None:
        try:
            snapshot = await html_snapshot.capture(kind, data)
        except html_snapshot.HtmlSnapshotUnavailable as exc:
            return {"status": "not_completed", "reason": exc.reason}, None
        run.keep_capture(key, snapshot)
    else:
        _LOG.info("visual_view.capture_reused kind=%s", kind)
    view = await _worker(
        {"op": "screens_view", "kind": kind, "pages": pages, "detail": detail, "text_present": bool(snapshot.text.strip())},
        snapshot.png,
        {"svg": data} if kind == "svg" else None,
    )
    if not isinstance(view, dict):
        return {"status": "not_completed", "reason": "render_failed"}, None
    content = html_content_snapshot(snapshot.text) if kind == "html" else view.pop("snapshot", None)
    if kind == "html" and view.get("status") == "shown":
        # Facts go only with the picture: alone they misled the model in the spike (F4).
        view.update(scroll_width=snapshot.scroll_width, scroll_height=snapshot.scroll_height)
        if snapshot.issues:
            view["runtime_issues"] = list(snapshot.issues)
        if snapshot.external_resources:
            view["external_resources_removed"] = True
    return view, content


async def attach_visual_view(
    output: dict[str, Any],
    ctx: Any,
    artifact: Any,
    *,
    kind: str,
    rendered: dict[str, Any] | None = None,
    presentation_fix: Any = None,
    inputs: list[str] | None = None,
) -> None:
    """Write ``visual_view`` (and, for a declared presentation fix, ``presentation_check``) into one output entry.

    ``inputs`` are the artifact IDs the call read; a fix saved under a new name is compared
    with the one of them the model saw.

    ``rendered`` may carry the file bytes, the executor PDF and its outcome that the PPTX
    layout facts already obtained. A render that cannot be built never fails the call.
    """
    found = run_for(ctx)
    if found is None:
        return
    run, call_id = found
    declared, ignored = normalize_presentation_fix(presentation_fix)
    if not run.enabled:
        output["visual_view"] = {"status": "not_available", "reason": "model_without_image_input"}
        if declared or ignored:
            output["presentation_check"] = {
                "status": "not_checked",
                "reason": ignored or "this model does not receive rendered views, so there is no version to compare with",
            }
        return
    started = time.perf_counter()
    identity = _identity(run, artifact)
    snapshot = None
    render_inputs = dict(rendered or {})
    try:
        with anyio.fail_after(RENDER_WAIT_SECONDS):
            view, snapshot = await _render(run, ctx, artifact, identity, kind, render_inputs)
    except TimeoutError:
        view = {"status": "not_completed", "reason": "time_limit"}
    except Exception as exc:
        _LOG.warning("visual_view.render_failed kind=%s error_type=%s", kind, type(exc).__name__)
        view = {"status": "not_completed", "reason": "render_failed"}
    output["visual_view"] = _summary(kind, view)
    images = view.get("images") or []
    if view["status"] == "shown":
        digest = render_inputs.get("sha256")
        run.pending.append(PendingRender(call_id=call_id, file=artifact.filename, view=view, version=(identity, digest) if digest else None))
    kept = None
    if ignored:
        output["presentation_check"] = {"status": "not_checked", "reason": ignored}
    elif declared:
        kept = _check_presentation_fix(output, run, identity, kind, snapshot, view, list(inputs or []))
    # The baseline is the version the model last saw. A declared fix that changed content keeps
    # the earlier one for this file, so a later fix that restores it compares as unchanged.
    seen = view["status"] == "shown" or view.get("reason") == "unchanged"
    if kept is not None:
        run.baselines[identity] = kept
    elif snapshot is not None and seen:
        run.baselines[identity] = {"snapshot": snapshot, "against": f"the version saved by call {call_id} that you saw before this fix"}
    _LOG.info(
        "visual_view.render kind=%s status=%s reason=%s pages=%s images=%d image_bytes=%d declared_fix=%s ms=%.0f",
        kind,
        view["status"],
        view.get("reason") or "-",
        view.get("pages") or "-",
        len(images),
        sum(len(image.get("base64") or "") * 3 // 4 for image in images),
        declared,
        (time.perf_counter() - started) * 1000,
    )


# ---------------------------------------------------------------- carrier


def take_renders(run: VisualRun) -> list[PendingRender]:
    """Renders for the next request, in save order, up to ``MAX_IMAGES_PER_REQUEST`` images."""
    taken: list[PendingRender] = []
    images = 0
    for render in run.pending:
        count = len(render.view.get("images") or [])
        if taken and images + count > MAX_IMAGES_PER_REQUEST:
            break
        taken.append(render)
        images += count
    return taken


def earlier_views(run: VisualRun, renders: list[PendingRender]) -> list[EarlierView]:
    """Pages of the same file versions shown in earlier requests, for the renders a view_file call asked for.

    A render lives for one request, so a model looking through a file screen by screen never had the whole
    file in front of it and went back and forth (an observed execution: seven views of one unchanged HTML file;
    docs/sprints/chat-dogfood-findings-2026-11-23/spike.md §3). Whole earlier views within four images left
    them out after any view of 3–4 pages, and models viewed those pages again: 7 of 30 replays, none with every
    earlier page in the request (spike §5). So earlier views come back page by page: each page shown at full
    size once, newest first, then the newest sheet of those pages unless the request already has one, up to
    ``MAX_IMAGES_WITH_EARLIER_PAGES`` images together with the new renders. Without the sheet, models with every
    screen at full size still asked for it (1 of 40 replays). Earlier views come only with a
    requested render, so a request that follows any other step carries no extra images.
    """
    versions = {render.version for render in renders if render.requested and render.version is not None}
    if not versions:
        return []
    room = MAX_IMAGES_WITH_EARLIER_PAGES - sum(len(render.view.get("images") or []) for render in renders)
    full = {(render.version, image["page"]) for render in renders for image in render.view.get("images") or [] if image.get("page")}
    in_sheets = {(render.version, page) for render in renders for image in render.view.get("images") or [] for page in image.get("pages") or []}
    earlier = [render for render in reversed(run.seen) if render.version in versions]
    picked: dict[int, set[int]] = {}
    for render in earlier:
        for index, image in enumerate(render.view.get("images") or []):
            key = (render.version, image.get("page"))
            if room > 0 and image.get("page") and key not in full:
                full.add(key)
                picked.setdefault(id(render), set()).add(index)
                room -= 1
    for render in earlier:
        for index, image in enumerate(render.view.get("images") or []):
            pages = {(render.version, page) for page in image.get("pages") or []}
            if room > 0 and pages and not pages <= in_sheets:
                in_sheets |= pages
                picked.setdefault(id(render), set()).add(index)
                room -= 1
    return [
        EarlierView(render, [image for index, image in enumerate(render.view.get("images") or []) if index in picked[id(render)]])
        for render in earlier
        if id(render) in picked
    ]


def _earlier_shown(images: list[dict[str, Any]]) -> str:
    """What earlier images show, worded like the render lines: ``screens 2-4, each at 1280x800 px``."""
    from bowhead_core.visual_view import SHEET_TILE_WIDTH, page_ranges

    parts: list[str] = []
    pages = [image for image in images if image.get("page")]
    if pages:
        noun = str(pages[0].get("label") or "page").split(" ", 1)[0].lower()
        numbers = [image["page"] for image in pages]
        width, height = pages[0].get("px") or (0, 0)
        size = f"{width}x{height} px" if width and height else "full size"
        parts.append(f"{noun}s {page_ranges(numbers)}, each at {size}" if len(numbers) > 1 else f"{noun} {numbers[0]}, at {size}")
    parts.extend(f"{image.get('label')} in 1 sheet, each {SHEET_TILE_WIDTH} px wide" for image in images if image.get("pages"))
    return ", then ".join(parts)


def render_message(renders: list[PendingRender], earlier: list[EarlierView] | None = None) -> Message:
    """The ``bowhead_visual_view_v1`` message; the runtime-note prefix is added at projection.

    ``earlier`` are pages shown in earlier requests that come back with a requested view (`earlier_views`).
    """
    lines = ["Rendered view of files saved by your recent Tool calls."]
    images: list[ImageRef] = []
    for render in renders:
        view = render.view
        first = len(images) + 1
        images.extend(ImageRef(base64=image["base64"], mime=image["mime"]) for image in view.get("images") or [])
        numbers = f"image {first}" if len(images) == first else f"images {first}-{len(images)}"
        lines.append(f"- {render.file} (Tool call {render.call_id}): {view.get('shown')} ({numbers}).")
        if view.get("not_shown"):
            lines.append(f"  Not shown: {view['not_shown']}; {VIEW_FILE_TOOL} with pages can show them.")
        page_px = view.get("page_px") or [0, 0]
        if page_px[0] and page_px[0] < 800:
            lines.append(
                "  Text smaller than about 10 px at this size cannot be judged; do not report it as unreadable. "
                f"{VIEW_FILE_TOOL} with detail full shows a page at 1280 px."
            )
        if view.get("external_resources_removed"):
            # Facts only: an instruction here made a model merge files the user asked to keep apart (116, 2026-11-19).
            lines.append(
                f"  The user's preview in {product_name()} removes external scripts, stylesheets, fonts and images, including linked files "
                f"saved beside the page, so this view shows the page without them. Opened outside {product_name()} together with those "
                "files, the page loads them."
            )
        if view.get("runtime_issues"):
            names = {"runtime": "script errors", "blocked": "blocked requests"}
            lines.append(f"  The page reported {' and '.join(names.get(item, item) for item in view['runtime_issues'])}.")
        fonts = view.get("substituted_fonts")
        if isinstance(fonts, dict) and fonts.get("requested"):
            lines.append(
                f"  Fonts the file asks for that the renderer does not have: {', '.join(fonts['requested'])} "
                f"(rendered with {', '.join(fonts.get('rendered_with') or []) or 'other fonts'}). Wrapping and widths may "
                "differ in the user's application; do not change the layout only for that."
            )
    for item in earlier or []:
        first = len(images) + 1
        images.extend(ImageRef(base64=image["base64"], mime=image["mime"]) for image in item.images)
        numbers = f"image {first}" if len(images) == first else f"images {first}-{len(images)}"
        lines.append(
            f"- {item.render.file} (Tool call {item.render.call_id}): {_earlier_shown(item.images)} ({numbers}), shown again "
            "from an earlier request; the file has not changed since."
        )
    lines.append(_CHECK_INSTRUCTIONS)
    return Message(role="user", name=VISUAL_VIEW_NOTE_NAME, content="\n".join(lines), images=images)


def not_sent_message(renders: list[PendingRender]) -> Message:
    files = ", ".join(f"{render.file} (Tool call {render.call_id})" for render in renders)
    return Message(
        role="user",
        name=VISUAL_VIEW_NOTE_NAME,
        content=(
            f"The rendered view of {files} was not sent: this request has no room for images. You have not seen it; do not "
            "describe the file as visually checked."
        ),
    )


def _with_message(request: LLMRequest, message: Message) -> LLMRequest:
    """Place the message right after the last Tool result, before the per-request tail."""
    messages = list(request.messages)
    index = len(messages)
    for position in range(len(messages) - 1, -1, -1):
        if messages[position].role != "user":
            index = position + 1
            break
    messages.insert(index, message)
    return replace(request, messages=messages, context_budget=None, output_budget=None)


async def attach_pending_renders(
    run: VisualRun | None,
    request: LLMRequest,
    *,
    router: Any,
    agent_type: str,
    profile: str,
    user_pref: Any,
    target: tuple[str, str] | None,
    stream: bool,
) -> tuple[LLMRequest, list[PendingRender]]:
    """Add the waiting renders to one request; the caller settles them after a model response.

    The router's own accounting decides whether the images fit (each image counts 4,096
    tokens); when they do not, the request carries a one-line note instead (skipped:
    context_budget) and the renders are settled with it.
    """
    if run is None or not run.pending:
        return request, []
    renders = take_renders(run)
    for render in renders:
        render.sent = False
    earlier = earlier_views(run, renders)
    # Earlier views are dropped before the new renders are: without room for both, send only the new ones.
    for extra in [earlier, []] if earlier else [[]]:
        candidate = _with_message(request, render_message(renders, extra))
        fits = False
        try:
            measured = await router.request_context_budget(
                agent_type,
                candidate,
                profile,
                user_pref,
                **({"target": target} if target is not None else {}),
                stream=stream,
                reject_over_capacity=False,
            )
            fits = bool(measured.budget.fits)
        except Exception as exc:
            _LOG.info("visual_view.budget_unavailable error_type=%s", type(exc).__name__)
            break
        if fits:
            for render in renders:
                render.sent = True
            if extra:
                _LOG.info("visual_view.earlier_views renders=%d images=%d", len(extra), sum(len(item.images) for item in extra))
            return candidate, renders
    _LOG.info("visual_view.render status=skipped reason=context_budget files=%d", len(renders))
    return _with_message(request, not_sent_message(renders)), renders


def settle_renders(run: VisualRun | None, renders: list[PendingRender]) -> None:
    """After a model response: the renders reached a model and leave the queue.

    Those whose images went out are remembered for `earlier_views`; a newer version of a file replaces the
    views of its older versions.
    """
    if run is None or not renders:
        return
    sent = {id(render) for render in renders}
    run.pending = [render for render in run.pending if id(render) not in sent]
    for render in renders:
        if not render.sent or render.version is None:
            continue
        identity = render.version[0]
        run.seen = [seen for seen in run.seen if seen.version is not None and (seen.version[0] != identity or seen.version == render.version)]
        run.seen.append(render)
    del run.seen[:-_MAX_SEEN_RENDERS]


# ------------------------------------------------------- delivery review (§7.2)


def visual_disclosure_calls(invocations: Any) -> dict[str, dict[str, Any]]:
    """Save calls the delivery review must account for (FR11, FR19), by call ID.

    Derived from Tool results only, so live, persisted and replayed runs agree. For each
    file name, the latest save in the run counts: it is listed when it still carries
    ``content_changes`` (the file differs from the version the model saw), or when the file
    is delivered (declared by its producer or presented) and its
    visual check did not complete. Undoing a change with a new save removes the item.
    """
    from bowhead_core.artifact_presentation import declared_deliverable_names
    from bowhead_core.tool_outcome import invocation_result

    latest: dict[str, tuple[str, dict[str, Any], bool]] = {}
    presented: set[str] = set()
    for invocation in invocations:
        call_id = getattr(invocation, "call_id", None)
        if not call_id:
            continue
        result = invocation_result(invocation) if hasattr(invocation, "result") or hasattr(invocation, "result_full") else None
        arguments = getattr(invocation, "arguments", None)
        if getattr(invocation, "tool_name", None) == "present_artifacts" and isinstance(arguments, dict):
            if getattr(invocation, "error", None) is None and isinstance(result, dict) and result.get("ok"):
                presented.update(
                    item["artifact_id"]
                    for item in arguments.get("artifacts") or []
                    if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
                )
            continue
        outputs = result.get("output_artifacts") if isinstance(result, dict) else None
        if not call_id or not isinstance(outputs, list):
            continue
        try:
            declared = declared_deliverable_names(invocation.tool_name, arguments) or set()
        except Exception:  # noqa: BLE001 - an invalid declaration declares nothing
            declared = set()
        for entry in outputs:
            if isinstance(entry, dict) and isinstance(entry.get("visual_view"), dict) and isinstance(entry.get("name"), str):
                latest[entry["name"]] = (call_id, entry, entry["name"] in declared)
    calls: dict[str, dict[str, Any]] = {}
    for name, (call_id, entry, declared) in latest.items():
        if entry.get("content_changes"):
            reason = "content_changes"
        elif entry["visual_view"].get("status") == "not_completed" and (declared or entry.get("artifact_id") in presented):
            reason = "visual_check_not_completed"
        else:
            continue
        calls.setdefault(call_id, {"call_id": call_id, "files": []})["files"].append({"file": name, "reason": reason})
    return calls


VISUAL_REVIEW_GUIDANCE = (
    "review_context.visual_disclosures lists saves you declared presentation fixes whose file still differs from the "
    "version you saw before the fix (reason content_changes), or whose delivered file could not be visually checked (reason "
    "visual_check_not_completed). Either undo a content change with a new save of the earlier content, which removes the "
    "item, or give the listed call one issue with resolution disclosed, empty evidence_call_ids and an explanation written "
    "for the user: what changed in the file, or that the visual check of the file was not completed. Disclosed "
    "explanations are added to your answer. "
)


def visual_issue_problem(call_id: str, item: dict[str, Any], issue: dict[str, Any]) -> str | None:
    """None when the issue resolves the listed call; otherwise the correction to return."""
    resolution = issue.get("resolution")
    files = ", ".join(entry["file"] for entry in item["files"])
    explanation = issue.get("explanation")
    if resolution == "disclosed" and isinstance(explanation, str) and explanation.strip():
        return None
    if resolution == "disclosed":
        return f"{call_id}: a disclosed issue needs an explanation for the user about {files}"
    if resolution == "undone" and any(entry["reason"] == "content_changes" for entry in item["files"]):
        return (
            f"{call_id}: the latest saved version of {files} still differs from the version you saw; undo the change with a new "
            "save of the earlier content, or resolve this issue as disclosed and state the change"
        )
    return f"{call_id}: resolve this visual disclosure for {files} as disclosed with an explanation for the user"


# ------------------------------------------------------------ look on request (§16)

_VIEW_FILE_DESCRIPTION = (
    "Look at a PPTX, PNG, JPEG, WebP, PDF, HTML or SVG file as the user will see it: a file saved in this Turn, a delivered "
    "file or an attached input such as a template. The rendered view comes with your next request; this result says what "
    "was rendered. detail sheet (default) shows up to 12 pages per sheet at 400 px each; detail full shows up to 4 pages at "
    "1280 px, for checking body text and numbers. An HTML page is shown as screens of its preview, 1280 px wide and up to "
    "800 px tall, each cut where the page is blank; grey fills a screen below its cut. Saved files are "
    "rendered automatically after each save; use this for pages that were not shown, fine detail, or files you did not save."
)
_MAX_REQUESTED_PAGES = 24


def _refusal(error: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message}


async def view_file(arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Render one readable file for the next request (FR20). Never fails the run."""
    found = run_for(ctx)
    if found is None or not found[0].enabled:
        return _refusal("view_file_unavailable", "Viewing files is not available for this model.")
    run, call_id = found
    artifact_id = arguments.get("artifact_id")
    detail = arguments.get("detail") or "sheet"
    pages = arguments.get("pages")
    if not isinstance(artifact_id, str) or not artifact_id:
        return _refusal("invalid_arguments", "artifact_id must be the ID of a file from a Tool result or the Turn's attachments.")
    if detail not in {"sheet", "full"}:
        return _refusal("invalid_arguments", "detail must be sheet or full.")
    if pages is not None and (
        not isinstance(pages, list) or not pages or len(pages) > _MAX_REQUESTED_PAGES or any(type(page) is not int or page < 1 for page in pages)
    ):
        return _refusal("invalid_arguments", f"pages must list 1 to {_MAX_REQUESTED_PAGES} page numbers, starting at 1.")
    from bowhead_core.artifact_presentation import PresentationStorageUnavailable, PresentationUnavailable, available_artifact

    try:
        artifact = await available_artifact(ctx, artifact_id)
    except PresentationStorageUnavailable:
        return _refusal("file_storage_unavailable", "The file could not be read from storage now. Continue without viewing it.")
    except PresentationUnavailable:
        return _refusal("file_unavailable", "No readable file has this artifact_id in this Space. Copy the ID from a Tool result.")
    kind = deliverable_kind(artifact.mime, artifact.filename)
    if kind is None:
        return _refusal("file_kind_not_viewable", f"{VIEW_FILE_TOOL} shows PPTX, PNG, JPEG, WebP, PDF, HTML and SVG files.")
    started = time.perf_counter()
    zero_based = sorted({page - 1 for page in pages}) if pages else None
    digest = None
    try:
        with anyio.fail_after(RENDER_WAIT_SECONDS):
            view, digest = await _render_on_request(run, ctx, artifact, kind, zero_based, detail, call_id)
    except TimeoutError:
        view = {"status": "not_completed", "reason": "time_limit"}
    except Exception as exc:
        _LOG.warning("visual_view.view_file_failed kind=%s error_type=%s", kind, type(exc).__name__)
        view = {"status": "not_completed", "reason": "render_failed"}
    if view["status"] == "shown":
        version = (_identity(run, artifact), digest) if digest else None
        run.pending.append(PendingRender(call_id=call_id, file=artifact.filename, view=view, version=version, requested=True))
    images = view.get("images") or []
    _LOG.info(
        "visual_view.render source=request kind=%s status=%s reason=%s pages=%s images=%d ms=%.0f",
        kind,
        view["status"],
        view.get("reason") or "-",
        view.get("pages") or "-",
        len(images),
        (time.perf_counter() - started) * 1000,
    )
    return {"ok": True, "artifact_id": artifact.id, "file": artifact.filename, "visual_view": _summary(kind, view)}


async def _render_on_request(
    run: VisualRun, ctx: Any, artifact: Any, kind: str, pages: list[int] | None, detail: str, call_id: str
) -> tuple[dict[str, Any], str | None]:
    """(view, content digest of the file viewed)."""
    data = await _read(ctx, artifact)
    if data is None:
        return {"status": "not_completed", "reason": "too_large"}, None
    digest = hashlib.sha256(data).hexdigest()
    control: dict[str, Any] = {"pages": pages, "detail": detail}
    saved_now = getattr(artifact, "created_by_turn", None) == getattr(ctx, "turn_id", None)
    snapshot = None
    if kind == "pptx":
        pdf = run.pdfs.get(artifact.id)
        if pdf is None:
            # Only a presentation not rendered in this run goes back to the executor.
            from bowhead_core.pptx_result_view import render_presentation_pdf

            pdf, outcome = await render_presentation_pdf(data)
            if pdf is None:
                return {"status": "not_completed", "reason": _RENDER_REASONS.get(outcome, "renderer_unavailable")}, digest
            run.keep_pdf(artifact.id, pdf)
        rendered = await _worker({**control, "op": "pptx_render", "render": True}, data, {"pdf": pdf})
        view = rendered.get("view") if rendered is not None else None
        snapshot = rendered.get("snapshot") if rendered is not None else None
    elif kind == "pdf":
        view = await _worker({**control, "op": "pdf_view", "snapshot": saved_now}, data)
        snapshot = view.pop("snapshot", None) if isinstance(view, dict) else None
    elif kind in {"html", "svg"}:
        view, snapshot = await _render_page(run, kind, data, pages, detail)
        if view.get("status") != "shown":
            return view, digest
    else:
        view = await _worker({"op": "image_view"}, data)
    if not isinstance(view, dict):
        return {"status": "not_completed", "reason": "render_failed"}, digest
    # Looking at a file saved in this Turn makes it the version the model last saw (D12).
    if saved_now and view.get("status") == "shown" and snapshot is not None:
        run.baselines[_identity(run, artifact)] = {"snapshot": snapshot, "against": f"the version you viewed with call {call_id}"}
    return view, digest


def view_file_spec() -> Any:
    """The run-local Tool; the runner adds it only when the run's primary model reads images."""
    from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
    from bowhead_core.tool_registry import ToolSchema, ToolSpec
    from bowhead_core.types import ToolAnnotations

    return ToolSpec(
        schema=ToolSchema(
            name=VIEW_FILE_TOOL,
            description=_VIEW_FILE_DESCRIPTION,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["artifact_id"],
                "properties": {
                    "artifact_id": {"type": "string", "description": "The file's artifact_id from a Tool result or the Turn's attachments."},
                    "pages": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "maxItems": _MAX_REQUESTED_PAGES,
                        "description": "Slide or page numbers, starting at 1. Omit for all pages.",
                    },
                    "detail": {"type": "string", "enum": ["sheet", "full"], "description": "sheet (default) or full (1280 px per page, at most 4)."},
                },
            },
        ),
        handler=view_file,
        allowed_for_roles=frozenset({"lead", "analyst", "composer", "narrative"}),
        annotations=ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False),
        effect_spec=TOOL_EFFECT_READ_ONLY,
    )
