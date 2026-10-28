"""Shared export-copy trust projection for already-authorized catalog items.

This is a read projection, not an artifact or regeneration authorization grant.
Callers must select their authorized page before reading copy-contract evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore
    from bowhead_core.canvas_store import CanvasStore


def is_export_output(*, source: str, filename: str) -> bool:
    return source in {"canvas_export", "canvas_export_clone"} and Path(filename).suffix.lower() in {".pdf", ".pptx", ".xlsx", ".docx"}


async def project_export_trust(
    artifacts: list[Artifact],
    *,
    artifact_store: ArtifactStore,
    canvas_store: CanvasStore | None,
    viewer_id: str,
) -> dict[str, str]:
    candidates = [artifact for artifact in artifacts if is_export_output(source=artifact.source, filename=artifact.filename)]
    if not candidates:
        return {}
    from bowhead_core.export_copy_contract import classify_export_artifact_versions

    versions = await classify_export_artifact_versions(candidates, artifact_store=artifact_store)
    projected: dict[str, str] = {}
    canvas_cache: dict[str, Any] = {}
    sections_cache: dict[str, list[Any] | None] = {}
    for artifact in candidates:
        if versions.get(artifact.id) == 3:
            projected[artifact.id] = "export_copy_current"
            continue
        if canvas_store is None or not artifact.canvas_id:
            projected[artifact.id] = "export_copy_legacy_source_missing"
            continue
        if artifact.canvas_id not in canvas_cache:
            try:
                canvas_cache[artifact.canvas_id] = await canvas_store.get_canvas(artifact.canvas_id)
            except Exception:  # noqa: BLE001
                canvas_cache[artifact.canvas_id] = None
        canvas = canvas_cache[artifact.canvas_id]
        if canvas is None:
            projected[artifact.id] = "export_copy_legacy_source_missing"
            continue
        if canvas.user_id != viewer_id:
            projected[artifact.id] = "export_copy_legacy_no_permission"
            continue
        if canvas.id not in sections_cache:
            try:
                list_projection = getattr(canvas_store, "list_export_projection", None)
                sections_cache[canvas.id] = (
                    await list_projection(canvas.id) if callable(list_projection) else await canvas_store.list_sections(canvas.id)
                )
            except Exception:  # noqa: BLE001
                sections_cache[canvas.id] = None
        sections = sections_cache[canvas.id]
        if sections is None:
            projected[artifact.id] = "export_copy_legacy_source_not_exportable"
            continue
        if any(getattr(section, "quarantined", False) for section in sections):
            projected[artifact.id] = "export_copy_legacy_source_quarantined"
        elif canvas.status == "archived" or not sections:
            projected[artifact.id] = "export_copy_legacy_source_not_exportable"
        else:
            projected[artifact.id] = "export_copy_legacy_regenerable"
    return projected
