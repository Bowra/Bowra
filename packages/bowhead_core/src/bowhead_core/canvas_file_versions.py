"""Canvas HTML sections follow a file's newest version (file-versions design R4, B2).

When a producer Tool writes a new version of an HTML file, the same Turn updates every
``html_artifact`` section in the owner's canvases that embeds an older version of it.
``patch_section`` bumps the section's ``rev`` (snapshot validation and export staleness see
the change) and ``canvas.section_patched`` reaches live clients; every reader then agrees
because the stored section itself names the new version. Best-effort: a failure leaves the
section on its version, and the next version follows it again.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})


def _is_html(mime: str | None) -> bool:
    return str(mime or "").split(";", 1)[0].strip().lower() in _HTML_MIMES


async def follow_new_versions_on_canvas(result: Any, ctx: Any) -> list[str]:
    """Point the owner's HTML sections at the new versions in ``result.output_artifacts``; return patched section IDs."""
    outputs = result.get("output_artifacts") if isinstance(result, dict) else None
    artifact_store = getattr(ctx, "artifact_store", None)
    canvas_store = getattr(ctx, "canvas_store", None)
    user_id = getattr(ctx, "user_id", None)
    if not isinstance(outputs, list) or artifact_store is None or canvas_store is None or not user_id:
        return []
    if not hasattr(canvas_store, "html_artifact_sections_for"):
        return []
    patched: list[str] = []
    patched_canvases: set[str] = set()
    for output in outputs:
        artifact_id = output.get("artifact_id") if isinstance(output, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        artifact = await artifact_store.get(artifact_id, check_created_by=user_id)
        if artifact is None or (artifact.version_no or 1) < 2 or not _is_html(artifact.mime):
            continue
        older = [member for member in await artifact_store.lineage_member_ids(artifact.id, user_id=user_id) if member != artifact.id]
        for section in await canvas_store.html_artifact_sections_for(user_id=user_id, artifact_ids=older):
            patch = await _follow_patch(artifact_store, section, artifact)
            # A system revision, not this Turn's report writing: Turn-scoped readers (the report final
            # gate, the revision finisher, KB warnings) must not count it as the Turn editing the report.
            updated = await canvas_store.patch_section(section_id=section.id, patch=patch, by_turn_id=None, canvas_id=section.canvas_id)
            if updated is None:
                continue
            patched.append(section.id)
            patched_canvases.add(section.canvas_id)
            await _emit_patched(ctx, section.canvas_id, section.id, patch, updated.rev)
    if patched:
        logger.info("canvas.html_section_followed_version turn=%s sections=%s", getattr(ctx, "turn_id", None), ",".join(patched))
        await _note_followed_on_this_canvas(ctx, canvas_store, patched_canvases)
    return patched


async def _note_followed_on_this_canvas(ctx: Any, canvas_store: Any, canvas_ids: set[str]) -> None:
    """Tell the done branch this session's Canvas now shows the new version (revision-target design §9).

    A pointer to the Canvas is then true, so it is not retracted. Unknown session or a failed
    lookup marks it too: the mark only keeps a pointer.
    """
    bus = getattr(ctx, "parent_bus", None)
    if bus is None:
        return
    session_id = getattr(ctx, "session_id", None)
    here = True
    if session_id:
        try:
            canvas = await canvas_store.get_canvas_by_session(session_id)
            here = canvas is not None and canvas.id in canvas_ids
        except Exception:
            logger.warning("canvas.html_section_follow_session_lookup_failed turn=%s", getattr(ctx, "turn_id", None), exc_info=True)
    if here:
        bus._canvas_followed_file_version = True


async def _follow_patch(artifact_store: Any, section: Any, artifact: Any) -> dict[str, Any]:
    from bowhead_core.html_preview_policy import html_preview_limit_bytes

    payload = section.payload or {}
    embedded = await artifact_store.get(str(payload.get("artifact_id") or ""), include_deleted=True)
    embedded_no = int(payload.get("version_no") or getattr(embedded, "version_no", None) or 1)
    maximum = html_preview_limit_bytes()
    if artifact.size > maximum:
        # Checks at resolution: the section keeps its version and says why.
        return {"version_note": {"reason": "too_large", "version_no": artifact.version_no, "maximum_bytes": maximum}}
    return {
        "artifact_id": artifact.id,
        "version_no": artifact.version_no,
        # `data`, `sources` and `unverified` describe the version the section was created with.
        "provenance_version_no": int(payload.get("provenance_version_no") or embedded_no),
        "version_note": None,
    }


async def _emit_patched(ctx: Any, canvas_id: str, section_id: str, patch: dict[str, Any], rev: int) -> None:
    bus = getattr(ctx, "parent_bus", None)
    if bus is None:
        return
    from bowhead_core.events import OrchestratorEvent

    try:
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_patched",  # type: ignore[arg-type]
                turn_id=getattr(ctx, "turn_id", None),
                payload={"canvas_id": canvas_id, "section_id": section_id, "kind": "html_artifact", "patch": patch, "rev": rev, "ok": True},
            )
        )
    except Exception:
        logger.warning("canvas.html_section_follow_emit_failed section=%s", section_id, exc_info=True)


__all__ = ["follow_new_versions_on_canvas"]
