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
    async def render(args, ctx):
        from bowhead_skill.loader import load_skill

        from bowhead_core.agents._lead_context import get_current_peer_name
        from bowhead_skill.catalog import builtin_catalog_dir

        binding = ctx.deck_render_binding
        if get_current_peer_name() != "consultant" or type(binding) is not DeckRenderBinding or binding.target != deck_target(ctx):
            return {
                "ok": False,
                "error": "deck_render_not_admitted",
                "message": "This tool needs the original new-presentation assignment. Read-only inspection and template updates use their existing workflows.",
            }
        if not ctx.artifact_store or not ctx.user_id:
            return {
                "ok": False,
                "error": "deck_storage_unavailable",
                "message": "Presentation storage is unavailable. No downloadable file can be confirmed.",
            }
        if set(args) - {"sections", "expected_slides", "style"}:
            return {
                "ok": False,
                "error": "invalid_deck_arguments",
                "message": "Use sections, optional expected_slides and optional style only. Template and output paths are server-owned.",
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
                    if (
                        not valid
                        or slides < 1
                        or (expected is not None and expected != slides)
                        or meta.get("placeholder_leftovers") != 0
                        or meta.get("raw_inline_markers") != 0
                    ):
                        return {
                            "ok": False,
                            "error": "deck_validation_failed",
                            "message": f"Rendered slide count is {slides}; expected {expected or 'a nonempty deck'}. "
                            f"Leftover placeholders: {meta.get('placeholder_leftovers') if type(meta.get('placeholder_leftovers')) is int else 'unknown'}; "
                            f"inline markers: {meta.get('raw_inline_markers') if type(meta.get('raw_inline_markers')) is int else 'unknown'}. Correct the sections and retry.",
                        }
                    binding.pending[digest] = content, {"slide_count": slides}
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
                    )
                    binding.staged[digest] = staged
                art = staged.artifact
                if await ctx.artifact_store.open_read(art) != content:
                    raise ValueError("Staged presentation bytes differ")
                existing = await ctx.artifact_store.get(art.id)
                if existing is None:
                    await ctx.artifact_store.commit_staged_artifacts((staged,))
                elif existing.created_by != ctx.user_id or existing.created_by_turn != ctx.turn_id or existing.path != art.path:
                    raise ValueError("Presentation registration identity differs")

            except Exception:
                logger.warning("deck_registration.failed turn=%s", ctx.turn_id, exc_info=True)
                return {
                    "ok": False,
                    "error": "deck_registration_failed",
                    "message": "The presentation rendered, but its downloadable file could not be verified. Retry the same render_deck arguments to register the retained bytes without rendering again.",
                }
            receipt = {
                "ok": True,
                "artifact_id": art.id,
                "filename": art.filename,
                **meta,
                "message": "The editable PPTX is registered and readable. Call subagent_return with a concise Chat summary now; do not render again.",
            }
            binding.receipts[digest] = receipt
            binding.pending.pop(digest, None)
            return dict(receipt)

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="render_deck",
                description="Render and register a new editable PPTX from confirmed content or verified analysis. Template and paths are server-owned. On success call subagent_return; do not inspect renderer scripts or use python_sandbox.",
                input_schema={
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
                        "style": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {key: {"type": "string", "pattern": "^[0-9A-Fa-f]{6}$"} for key in ["accent", "ink", "background"]},
                        },
                    },
                },
            ),
            handler=render,
            allowed_for_roles=frozenset({"analyst"}),
            risk_level="low",
        )
    )
