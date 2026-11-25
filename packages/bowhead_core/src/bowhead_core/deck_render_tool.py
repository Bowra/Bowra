"""Render a new presentation from server-authorized inputs and register its bytes."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from bowhead_core.artifact_store import VersionTargetRefused
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router import ToolSchema

logger = logging.getLogger(__name__)
MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@dataclass
class DeckRenderBinding:
    """In-memory authority shared only by copies of the original Tool context."""

    target: tuple[str, str, str, str, str]
    template_path: str
    # True when the assignment can only be a new deck (grounded report delivery, or no
    # uploaded deck to inspect): completion then needs a verified file. Otherwise the
    # authority is available, and a file is required only once rendering is attempted.
    required: bool = True
    pending: dict[str, tuple[bytes, dict[str, Any]]] = field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    staged: dict[str, Any] = field(default_factory=dict)
    lock: anyio.Lock = field(default_factory=anyio.Lock, repr=False)


def deck_target(ctx: ToolExecutionContext) -> tuple[str, str, str, str, str]:
    return ctx.user_id, ctx.team_id, ctx.workspace_id, ctx.session_id, ctx.turn_id


def register_deck_render_tool(registry: ToolRegistry, runtime: Any) -> None:
    from bowhead_core.visual_self_check import with_presentation_fix

    async def render(args, ctx):
        from bowhead_skill.loader import load_skill

        from bowhead_core.agents._lead_context import get_current_peer_name
        from bowhead_skill.catalog import builtin_catalog_dir

        binding = ctx.deck_render_binding
        if get_current_peer_name() != "consultant" or type(binding) is not DeckRenderBinding or binding.target != deck_target(ctx):
            return {
                "ok": False,
                "error": "deck_render_not_admitted",
                "message": "This tool needs the original new-presentation assignment. It creates new slides and does not change an existing file.",
            }
        if not ctx.artifact_store or not ctx.user_id:
            return {
                "ok": False,
                "error": "deck_storage_unavailable",
                "message": "Presentation storage is unavailable. No downloadable file can be confirmed.",
            }
        if set(args) - {"sections", "expected_slides", "style", "revises"}:
            return {
                "ok": False,
                "error": "invalid_deck_arguments",
                "message": "Use sections, optional expected_slides, optional style and optional revises only. Template and output paths are server-owned.",
            }
        sections = args.get("sections")
        expected = args.get("expected_slides")
        style = args.get("style", {})
        if (
            not isinstance(sections, list)
            or not sections
            or len(sections) > 100
            or (expected is not None and (type(expected) is not int or not 1 <= expected <= 100))
            or not isinstance(style, dict)
            or set(style) - {"accent", "ink", "background"}
            or any(not isinstance(v, str) or re.fullmatch(r"[0-9A-Fa-f]{6}", v) is None for v in style.values())
        ):
            return {
                "ok": False,
                "error": "invalid_deck_arguments",
                "message": "Provide nonempty sections, an optional slide count from 1 to 100 and six-digit RGB style colors.",
            }
        allowed = {"eyebrow", "h1", "kpi_row", "insight_card", "chart", "recommendations"}
        if (
            any(not isinstance(s, dict) or s.get("kind") not in allowed or not isinstance(s.get("payload"), dict) for s in sections)
            or sum(s.get("kind") == "h1" for s in sections) != 1
            or any(sum(s.get("kind") == kind for s in sections) > 1 for kind in ("eyebrow", "kpi_row", "recommendations"))
        ):
            return {
                "ok": False,
                "error": "invalid_deck_sections",
                "message": "Use one h1 cover title, optional eyebrow, and supported content sections. Do not repeat singleton section types.",
            }
        for section in sections:
            payload = section["payload"]
            if "bullets" in payload and (
                section["kind"] != "insight_card"
                or not isinstance(payload["bullets"], list)
                or not payload["bullets"]
                or any(not isinstance(point, str) or not point.strip() for point in payload["bullets"])
                or any(payload.get(key) for key in ("thesis", "narrative", "body_md"))
            ):
                return {
                    "ok": False,
                    "error": "invalid_deck_bullets",
                    "message": "For a list slide use insight_card with title and a nonempty bullets array of text, one point per item. Do not mix bullets with thesis or narrative.",
                }
        revises = args.get("revises")
        if revises is not None:
            # File versions (docs/sprints/file-versions-2026-11-10/tech-design.md W2): checked before rendering.
            refusal = await _deck_version_refusal(ctx, revises)
            if refusal is not None:
                return refusal
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
        if len(encoded) > 128_000:
            return {"ok": False, "error": "deck_content_too_large", "message": "Reduce the presentation content to the requested slides and retry."}
        digest = hashlib.sha256(encoded).hexdigest()
        async with binding.lock:
            if digest in binding.receipts:
                return dict(binding.receipts[digest])
            if digest not in binding.pending:
                # This native Tool uses the reviewed packaged renderer; personal Skills
                # named pptx do not replace its implementation or receive host paths.
                try:
                    spec = load_skill(builtin_catalog_dir() / "pptx")
                    result = await runtime.run(
                        spec, entry_script="render_deck.py", input_data={"template_path": binding.template_path, "sections": sections, "style": style}
                    )
                    if not result.ok or result.result_json.get("ok") is not True:
                        return {
                            "ok": False,
                            "error": "deck_render_failed",
                            "message": f"The presentation renderer failed its success check (exit code {result.exit_code}). No downloadable file was registered; check the content or share this task with your administrator.",
                        }
                    workdir = Path(result.workdir).resolve() if result.workdir else None
                    paths = [Path(p) if Path(p).is_absolute() else workdir / p for p in result.outputs if workdir is not None]
                    output = next((p for p in paths if p.suffix.lower() == ".pptx" and p.is_file() and p.resolve().is_relative_to(workdir)), None)
                    if output is None:
                        return {
                            "ok": False,
                            "error": "deck_output_missing",
                            "message": "The renderer returned no accessible PPTX file. No download was registered; retry rendering.",
                        }
                    content = output.read_bytes()
                    with zipfile.ZipFile(io.BytesIO(content)) as package:
                        slides = sum(re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name) is not None for name in package.namelist())
                        valid = "[Content_Types].xml" in package.namelist() and "ppt/presentation.xml" in package.namelist()
                    meta = result.result_json
                    # A slide count other than expected_slides is reported in the receipt, not
                    # refused: the deck is kept and the model judges it (spike: docs/sprints/
                    # pptx-upload-edit-dogfood-2026-11-23/spike.md, "render_deck slide count").
                    if not valid or slides < 1 or meta.get("placeholder_leftovers") != 0 or meta.get("raw_inline_markers") != 0:
                        return {
                            "ok": False,
                            "error": "deck_validation_failed",
                            "message": f"Rendered slide count is {slides}. "
                            f"Leftover placeholders: {meta.get('placeholder_leftovers') if type(meta.get('placeholder_leftovers')) is int else 'unknown'}; "
                            f"inline markers: {meta.get('raw_inline_markers') if type(meta.get('raw_inline_markers')) is int else 'unknown'}. Correct the sections and retry.",
                        }
                    facts: dict[str, Any] = {"slide_count": slides}
                    if expected is not None:
                        facts["expected_slides"] = expected
                    slide_map = slides_by_section(meta.get("slide_sections"), slides)
                    if slide_map:
                        facts["slides_by_section"] = slide_map
                    binding.pending[digest] = content, facts
                except Exception:
                    logger.warning("deck_render.failed turn=%s", ctx.turn_id, exc_info=True)
                    return {
                        "ok": False,
                        "error": "deck_render_failed",
                        "message": "Presentation rendering or file validation failed. No download was confirmed; retry or share the task reference with your administrator.",
                    }
            content, meta = binding.pending[digest]
            try:
                staged = binding.staged.get(digest)
                if staged is None:
                    staged = await ctx.artifact_store.stage_export_bytes(
                        content=content,
                        filename="consultant-deck.pptx",
                        mime=MIME,
                        canvas_id=None,
                        session_id=ctx.session_id,
                        created_by=ctx.user_id,
                        producer_kind="consultant_deck",
                        source="consultant_deck",
                        created_by_turn=ctx.turn_id,
                        version_of_target=revises if isinstance(revises, str) and revises and await ctx.artifact_store.versions_supported() else None,
                    )
                    binding.staged[digest] = staged
                art = staged.artifact
                if await ctx.artifact_store.open_read(art) != content:
                    raise ValueError("Staged presentation bytes differ")
                existing = await ctx.artifact_store.get(art.id)
                if existing is None:
                    try:
                        await ctx.artifact_store.commit_staged_artifacts((staged,))
                    except VersionTargetRefused as refused:
                        # Another version was recorded first: drop this staged copy and name the latest version.
                        binding.staged.pop(digest, None)
                        return _version_refusal(refused.reason, refused.latest_id)
                elif existing.created_by != ctx.user_id or existing.created_by_turn != ctx.turn_id or existing.path != art.path:
                    raise ValueError("Presentation registration identity differs")
                # The deck this file was recorded as the next version of, as stored (None without version support).
                version_of = getattr(art if existing is None else existing, "version_of", None)

            except Exception:
                logger.warning("deck_registration.failed turn=%s", ctx.turn_id, exc_info=True)
                return {
                    "ok": False,
                    "error": "deck_registration_failed",
                    "message": "The presentation rendered, but its downloadable file could not be verified. Retry the same render_deck arguments to register the retained bytes without rendering again.",
                }
            receipt = {"ok": True, "artifact_id": art.id, "filename": art.filename, **meta, "message": _receipt_message(meta)}
            if isinstance(version_of, str) and version_of:
                receipt["revises"] = version_of
            binding.receipts[digest] = receipt
            binding.pending.pop(digest, None)
            from bowhead_core.visual_self_check import attach_deck_view

            # Visual self-check: the first receipt of a render carries its view; a repeated call returns the receipt alone.
            return await attach_deck_view(dict(receipt), ctx, art, presentation_fix=args.get("presentation_fix"), deck_facts=_slide_count_fact(meta))

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="render_deck",
                description="Render and register a new editable PPTX from confirmed content or verified analysis. Template and paths are server-owned. On success call subagent_return; do not inspect renderer scripts or use python_sandbox.",
                input_schema=with_presentation_fix(
                    {
                        "type": "object",
                        "required": ["sections"],
                        "additionalProperties": False,
                        "properties": {
                            "sections": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {
                                    "type": "object",
                                    "required": ["kind", "payload"],
                                    "properties": {
                                        "kind": {"type": "string", "enum": ["eyebrow", "h1", "kpi_row", "insight_card", "chart", "recommendations"]},
                                        "payload": {
                                            "type": "object",
                                            "description": "For insight_card lists use title+bullets, one item per requested point. For analytical prose use title+thesis+narrative instead.",
                                            "properties": {"bullets": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}},
                                        },
                                    },
                                },
                            },
                            "expected_slides": {"type": "integer", "minimum": 1, "maximum": 100},
                            "revises": {
                                "type": "string",
                                "description": "Optional: the artifact_id of a deck delivered earlier in this conversation that this render revises. The new PPTX is recorded as its next version (latest version only).",
                            },
                            "style": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {key: {"type": "string", "pattern": "^[0-9A-Fa-f]{6}$"} for key in ["accent", "ink", "background"]},
                            },
                        },
                    }
                ),
            ),
            handler=render,
            allowed_for_roles=frozenset({"analyst"}),
            risk_level="low",
        )
    )


_TITLE_CHARS = 40


def slides_by_section(entries: Any, slide_count: int) -> str | None:
    """State which section produced each slide, from the renderer's own record.

    Runs of the same kind are grouped ("slides 3-6 insight_card x4 ('A', 'B', ...)"). Returns
    None when the record is missing, malformed or disagrees with the counted slides.
    """
    if not isinstance(entries, list) or len(entries) != slide_count:
        return None
    runs: list[tuple[int, int, str, list[str]]] = []
    for number, entry in enumerate(entries, 1):
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if not isinstance(kind, str) or not kind:
            return None
        title = entry.get("title")
        title = title[:_TITLE_CHARS] + ("…" if len(title) > _TITLE_CHARS else "") if isinstance(title, str) else ""
        if runs and runs[-1][2] == kind and kind != "cover":
            start, _, _, titles = runs[-1]
            runs[-1] = (start, number, kind, [*titles, title])
        else:
            runs.append((number, number, kind, [title]))
    parts = []
    for start, end, kind, titles in runs:
        if kind == "cover":
            parts.append(f"slide {start} cover (eyebrow and h1)")
            continue
        span = f"slide {start} {kind}" if start == end else f"slides {start}-{end} {kind} x{end - start + 1}"
        named = ", ".join(f"'{t}'" for t in titles if t)
        parts.append(f"{span} ({named})" if named else span)
    return "; ".join(parts)


def _slide_count_fact(facts: dict[str, Any]) -> str:
    """The slide count against expected_slides, stated only when they differ."""
    slides, expected = facts.get("slide_count"), facts.get("expected_slides")
    if expected is None or expected == slides:
        return ""
    return (
        f"It has {slides} slides; expected_slides was {expected}. "
        "The renderer gives the cover (eyebrow and h1) a slide of its own, then one slide to each kpi_row, insight_card, "
        "chart and recommendations section, in that order. A render_deck call with revises set to this artifact_id "
        "records its next version."
    )


def _receipt_message(facts: dict[str, Any]) -> str:
    count = _slide_count_fact(facts)
    if not count:
        return "The editable PPTX is registered and readable. Call subagent_return with a concise Chat summary now; do not render again."
    return f"The editable PPTX is registered and readable. {count}"


_DECK_VERSION_HINTS = {
    "version_target_not_found": "The deck to revise was not found among your files.",
    "version_target_out_of_scope": "Only a deck produced earlier in this conversation can get a new version; omit revises to register a new deck.",
    "version_target_format_changed": "A new version must stay a PPTX; omit revises to register a different file.",
    "version_target_not_latest": "A newer version of this deck exists; revise the latest version.",
}


def _version_refusal(reason: str, latest_id: str | None) -> dict[str, Any]:
    message = _DECK_VERSION_HINTS.get(reason, "The deck to revise could not be verified; retry the same call.")
    return {
        "ok": False,
        "error": reason if reason in _DECK_VERSION_HINTS else "version_target_unverified",
        "message": message + (f" Latest version: {latest_id}." if latest_id else ""),
        **({"latest_id": latest_id} if latest_id else {}),
    }


async def _deck_version_refusal(ctx: Any, revises: Any) -> dict[str, Any] | None:
    if not isinstance(revises, str) or not revises.strip():
        return {"ok": False, "error": "invalid_revises", "message": "revises must be the artifact_id of the deck being revised."}
    store = getattr(ctx, "artifact_store", None)
    if store is None or not await store.versions_supported():
        return None
    try:
        await store.check_version_target(
            revises.strip(), user_id=getattr(ctx, "user_id", None), session_id=getattr(ctx, "session_id", None), workspace_id=None, mime=MIME
        )
    except VersionTargetRefused as refused:
        return _version_refusal(refused.reason, refused.latest_id)
    return None
