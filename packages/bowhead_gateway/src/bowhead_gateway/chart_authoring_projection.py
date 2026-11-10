"""Current-viewer source authorization for native-chart authoring tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from bowhead_core.chart.authoring_tools import ChartProjectionUnavailable
from bowhead_core.source_access_projection import canonical_source_identity, collect_source_identities


async def project_chart_for_authoring(request: Any, session_id: str, section: Any, *, access: Any = None) -> dict[str, Any]:
    """Return the viewer-safe chart, or raise ``ChartProjectionUnavailable`` with the failing check.

    A chart delivered in chat is hidden from the report lens and Canvas, but it
    remains this conversation's chart and is edited in place. Only this target
    section's own typed relations join its authorization closure; every
    existing source-access and projector check still applies.
    """
    from bowhead_gateway.routes.canvas import _build_source_refs_for_session, _closure_from_sections, _viewer_safe_projector

    store = getattr(request.app.state, "canvas_store", None)
    if access is not None:
        try:
            canvas = await access._canvas()
        except ValueError as exc:
            raise ChartProjectionUnavailable("scope_unavailable") from exc
    else:
        canvas = await store.get_canvas_by_session(session_id) if store else None
    if canvas is None or section.canvas_id != canvas.id:
        raise ChartProjectionUnavailable("section_unavailable")
    sections = await store.list_sections(canvas.id, include_hidden=True)
    if not any(item.id == section.id and item.rev == section.rev for item in sections):
        raise ChartProjectionUnavailable("section_changed")
    canonical = section.to_report_section()
    try:
        identities, related_sources, derived_section = await chart_source_requirements(request.app.state.artifact_store, canonical)
    except ValueError as exc:
        raise ChartProjectionUnavailable("provenance_unavailable") from exc
    if not identities:
        raise ChartProjectionUnavailable("source_unrecorded")
    refs = await _build_source_refs_for_session(request, session_id, strict=True)
    related_refs = [ref for ref in refs if canonical_source_identity(ref) in identities]
    if derived_section is not None:
        # The server-owned derivation links this report's data artifact to the
        # original, even when the report has no separate source-ref row for it.
        related_refs.extend(related_sources)
    authorized_sections = [canonical, *([derived_section] if derived_section else [])]
    ref_artifacts = {artifact_id for ref in related_refs for artifact_id in _ref_artifact_ids(ref)}
    if access is not None:
        candidates = {identity for identity in collect_source_identities(authorized_sections, related_refs) if identity.startswith("art_")}
        projector = await access._sources(canvas, authorized_sections, related_refs)
    else:
        candidates = set(_closure_from_sections([section], include_chat_hidden=True))
        projector = await _viewer_safe_projector(
            request,
            canvas,
            [section],
            related_refs,
            additional_sections=[derived_section] if derived_section else None,
            include_chat_hidden=True,
        )
    candidates |= ref_artifacts
    denied = [identity for identity in identities if not projector.access_by_identity.get(identity, False)]
    if denied:
        # A required source with no backing artifact in this chart's own
        # closure is a closure gap; a backed source (or a knowledge source,
        # which is judged by the viewer's knowledge access) that still fails
        # was removed by current authorization or no longer exists.
        knowledge = getattr(projector, "kb_by_identity", None) or {}
        if any(identity not in knowledge and not (_identity_backing(identity, related_refs) & candidates) for identity in denied):
            raise ChartProjectionUnavailable("section_outside_report_closure")
        raise ChartProjectionUnavailable("source_access_denied")
    projected, _ = projector.project([canonical])
    if len(projected) != 1:
        raise ChartProjectionUnavailable("projection_unavailable")
    return projected[0]


def actor_chart_authoring_projection(
    app: Any, *, session_id: str, workspace_id: str, team_id: str, user_id: str
) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """Bind chart authoring to a Turn's admitted actor when no browser request exists.

    Terminal, resume and scheduled Turns have no HTTP credentials. They use the
    same actor-bound report authority as the chat route, which rechecks the
    conversation scope and every chart source on each call.
    """
    from bowhead_gateway.report_read_access import ReportReadAccess

    return ReportReadAccess(SimpleNamespace(app=app), session_id, workspace_id, team_id or "", user_id).project_chart


def _ref_artifact_ids(ref: Any) -> set[str]:
    if not isinstance(ref, dict):
        return set()
    out = {ref["artifact_id"]} if isinstance(ref.get("artifact_id"), str) and ref["artifact_id"] else set()
    for sub in ref.get("sub_sources") or ():
        if isinstance(sub, dict) and isinstance(sub.get("artifact_id"), str) and sub["artifact_id"]:
            out.add(sub["artifact_id"])
    return out


def _identity_backing(identity: str, refs: list[dict[str, Any]]) -> set[str]:
    backing = {identity} if identity.startswith("art_") else set()
    for ref in refs:
        if canonical_source_identity(ref) == identity:
            backing |= _ref_artifact_ids(ref)
    return backing


async def chart_source_requirements(artifact_store: Any, canonical: dict[str, Any]) -> tuple[list[str], list[dict], dict | None]:
    """Resolve the original chart source requirements, including verified derivation."""
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
        try:
            derived = await artifact_store.get_many([artifact_id]) if artifact_store else []
        except Exception:
            raise ValueError("Chart provenance unavailable")
        if (
            len(derived) != 1
            or getattr(derived[0], "id", None) != artifact_id
            or getattr(derived[0], "source", None) != "chart_calculation"
            or getattr(derived[0], "derived_from", None) != original_id
        ):
            raise ValueError("Chart provenance unavailable")
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
    related_sources = [{"id": original_id, "artifact_id": original_id}] if derived_section is not None else []
    return identities, related_sources, derived_section
