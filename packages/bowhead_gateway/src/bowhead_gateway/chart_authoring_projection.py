"""Current-viewer source authorization for native-chart authoring tools."""

from __future__ import annotations

from typing import Any

from bowhead_core.source_access_projection import canonical_source_identity, collect_source_identities


async def project_chart_for_authoring(request: Any, session_id: str, section: Any) -> dict[str, Any] | None:
    from bowhead_gateway.routes.canvas import _build_source_refs_for_session, _viewer_safe_projector

    store = getattr(request.app.state, "canvas_store", None)
    canvas = await store.get_canvas_by_session(session_id) if store else None
    if canvas is None or section.canvas_id != canvas.id:
        return None
    sections = await store.list_sections(canvas.id, include_hidden=True)
    if not any(item.id == section.id and item.rev == section.rev for item in sections):
        return None
    canonical = section.to_report_section()
    # add_chart records the active Datarc workspace on every new chart, even
    # when the actual input is a Python artifact. This context is not an
    # additional data source in that specific shape. Keep it in the viewer
    # projection, but do not make it a second authorization prerequisite.
    # Unknown or richer provenance stays fail-closed.
    required_section = canonical
    spec = canonical.get("spec")
    context = canonical.get("data_source")
    source = spec.get("source_meta") if isinstance(spec, dict) else None
    artifact_id = spec.get("data_artifact_id") if isinstance(spec, dict) else None
    original_id = source.get("artifact_id") if isinstance(source, dict) else None
    derived_section = None
    if (
        isinstance(source, dict)
        and source.get("tool") == "python_sandbox"
        and isinstance(original_id, str)
        and original_id.startswith("art_")
        and canonical_source_identity(source) == original_id
        and isinstance(artifact_id, str)
        and artifact_id.startswith("art_")
        and artifact_id != original_id
    ):
        artifact_store = getattr(request.app.state, "artifact_store", None)
        try:
            derived = await artifact_store.get_many([artifact_id]) if artifact_store else []
        except Exception:
            return None
        if (
            len(derived) != 1
            or getattr(derived[0], "id", None) != artifact_id
            or getattr(derived[0], "source", None) != "chart_calculation"
            or getattr(derived[0], "derived_from", None) != original_id
        ):
            return None
        # The current Canvas closure and the projector independently authorize
        # the stored calculation and its original source; do not trust a model
        # claim that two unrelated artifacts form a derivation.
        derived_section = {"kind": "chart", "spec": {"source_meta": {"id": artifact_id, "artifact_id": artifact_id}}}
    if (
        isinstance(context, dict)
        and context.get("tool") == "datarc"
        and set(context) <= {"tool", "context_id", "context_name"}
        and isinstance(source, dict)
        and source.get("tool") == "python_sandbox"
        and isinstance(artifact_id, str)
        and artifact_id.startswith("art_")
        and (source.get("artifact_id") == artifact_id and canonical_source_identity(source) == artifact_id or derived_section is not None)
    ):
        required_section = {key: value for key, value in canonical.items() if key != "data_source"}
    identities = collect_source_identities([required_section])
    if derived_section is not None:
        identities = list(dict.fromkeys([*identities, artifact_id]))
    if not identities:
        return None
    refs = await _build_source_refs_for_session(request, session_id, strict=True)
    related_refs = [ref for ref in refs if canonical_source_identity(ref) in identities]
    if derived_section is not None:
        # The server-owned derivation links this report's data artifact to the
        # original, even when the report has no separate source-ref row for it.
        related_refs.append({"id": original_id, "artifact_id": original_id})
    projector = await _viewer_safe_projector(
        request, canvas, [section], related_refs, additional_sections=[derived_section] if derived_section else None
    )
    if any(not projector.access_by_identity.get(identity, False) for identity in identities):
        return None
    projected, _ = projector.project([canonical])
    return projected[0] if len(projected) == 1 else None
