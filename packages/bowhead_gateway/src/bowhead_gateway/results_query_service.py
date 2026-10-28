"""Authorized owner/report Results query orchestration.

Authorization stays in the route.  This service consumes only an already-owned
session and emits the strict v1 DTO; it never grants access to artifact bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ResultsArtifactEntry, ResultsArtifactSnapshot
from bowhead_core.results_read_model import (
    RESULTS_SORT_VERSION_V1,
    ProjectedResult,
    artifact_search_rank,
    compare_sort_tuples,
    latest_occurred_at,
    normalize_results_query,
    project_artifact_result,
    project_code_result,
    project_report_result,
    relation_alias_matches,
    results_projection_digest,
    results_query_filter_hash,
    results_scope_fingerprint,
    sort_projected_results,
)
from bowhead_core.results_report_relations import (
    ReportRelations,
    project_report_relations,
)

from bowhead_gateway.artifact_origin import origin_for
from bowhead_gateway.export_artifact_trust import project_export_trust
from bowhead_gateway.results_contracts import (
    ArtifactResultRouteV1,
    CodeResultRouteV1,
    ReportResultRouteV1,
    ResultArtifactMetadataV1,
    ResultDisplayContextV1,
    ResultItemDTOV1,
    ResultsEffectiveCapabilityV1,
    ResultsFiltersV1,
    ResultsGroupCountsV1,
    ResultsNewResultsV1,
    ResultsProjectionStatusV1,
    ResultsQueryRequestV1,
    ResultsQueryResponseV1,
)
from bowhead_gateway.results_cursor import (
    ResultsCursorBinding,
    ResultsCursorCodec,
    ResultsCursorTokenError,
)

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactStore
    from bowhead_core.canvas_store import CanvasStore, Section
    from bowhead_core.session_store import Session

    from bowhead_core import SessionStore


ResultsQueryErrorCode = Literal[
    "results_query_invalid",
    "results_snapshot_stale",
    "results_anchor_not_in_view",
    "results_filter_unavailable",
    "results_read_model_unavailable",
]


class ResultsQueryError(RuntimeError):
    def __init__(self, code: ResultsQueryErrorCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _ResultsProjection:
    report: ProjectedResult | None
    code: ProjectedResult | None
    report_exists: bool
    code_exists: bool
    report_relations: ReportRelations
    report_relations_ready: bool
    projection_digest: str


@dataclass(frozen=True, slots=True)
class _ResultsScope:
    route_kind: Literal["owner", "report_only"]
    session_id: str
    scope_id: str
    principal_id: str
    team_id: str
    artifact_owner_id: str
    authorized_artifact_ids: tuple[str, ...] | None

    @property
    def report_only(self) -> bool:
        return self.route_kind == "report_only"


def _matches_filters(item: ProjectedResult, filters: ResultsFiltersV1) -> bool:
    if filters.primary != "all" and item.primary_group != filters.primary:
        return False
    return filters.secondary == "all" or item.secondary_group == filters.secondary


def _relation_search_ids(
    relations: ReportRelations,
    query: str,
) -> tuple[str, ...]:
    return tuple(relation.artifact_id for relation in relations.relations if relation_alias_matches(relation.roles, query))


def _report_title(canvas: Any, sections: list[Any]) -> str:
    for section in sections:
        if section.kind != "h1" or section.superseded:
            continue
        text = section.payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()[:500]
    title = str(canvas.title or "").strip()
    return title[:500] or "报告"


async def _read_code_event_state(
    *,
    db_path: str,
    session_id: str,
) -> tuple[bool, str | None, int]:
    """Read only code-event existence/watermark, never code or stdout."""

    code_predicate = """
    (
      e.event_type IN ('pycell.in', 'pycell.out')
      OR (
        e.event_type IN ('tool_call', 'tool_result', 'step_started', 'step_progress', 'step_done')
        AND json_valid(e.payload_json)
        AND (
          lower(COALESCE(json_extract(e.payload_json, '$.tool_name'), ''))
            IN ('python_sandbox', 'python_exec')
          OR lower(COALESCE(json_extract(e.payload_json, '$.kind'), ''))
            IN ('python', 'code', 'sql')
          OR lower(COALESCE(json_extract(e.payload_json, '$.language'), ''))
            IN ('python', 'sql', 'shell')
        )
      )
    )
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT COUNT(*) AS n, MAX(e.occurred_at) AS watermark
            FROM turn_events e
            JOIN turns t ON t.id=e.turn_id
            WHERE t.session_id=? AND {code_predicate}
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        async with db.execute(
            f"""
            SELECT t.status, e.event_type
            FROM turn_events e
            JOIN turns t ON t.id=e.turn_id
            WHERE t.session_id=? AND {code_predicate}
            ORDER BY t.seq DESC, e.seq DESC
            LIMIT 1
            """,
            (session_id,),
        ) as cursor:
            latest = await cursor.fetchone()
    running = bool(latest and latest["status"] == "running" and latest["event_type"] in {"pycell.in", "tool_call", "step_started", "step_progress"})
    return (
        bool(row and int(row["n"] or 0)),
        (str(row["watermark"]) if row and row["watermark"] else None),
        int(running),
    )


async def _owner_projection(
    *,
    session: Session,
    session_store: SessionStore,
    canvas_store: CanvasStore,
    query: str,
    writable: bool,
) -> _ResultsProjection:
    canvas = await canvas_store.get_canvas_by_session(session.id)
    sections: list[Section] = []
    if canvas is not None:
        state = await canvas_store.read_canvas_projection_state(canvas.id)
        if state is None:
            raise ResultsQueryError("results_read_model_unavailable")
        canvas, sections = state
        if canvas.session_id != session.id or canvas.user_id != session.user_id:
            raise ResultsQueryError("results_read_model_unavailable")
    code_event_exists, code_event_at, code_running = await _read_code_event_state(
        db_path=session_store.db_path,
        session_id=session.id,
    )
    visible_sections = [section for section in sections if not section.hidden_for_chat_mode]
    try:
        report_relations = project_report_relations(visible_sections)
        report_relations_ready = True
    except Exception:
        report_relations = project_report_relations([])
        report_relations_ready = False
    report_sections = [section for section in visible_sections if section.kind not in {"sql_block", "code_block"}]
    code_sections = [section for section in visible_sections if section.kind in {"sql_block", "code_block"}]
    report_exists = canvas is not None and bool(report_sections)
    code_exists = bool(code_sections) or code_event_exists
    report = None
    if report_exists and canvas is not None:
        report = project_report_result(
            canvas_id=canvas.id,
            title=_report_title(canvas, report_sections),
            occurred_at=canvas.updated_at,
            editable=writable,
            query=query,
            export_available=writable,
        )
    code = None
    if code_exists:
        code = project_code_result(
            session_id=session.id,
            occurred_at=latest_occurred_at(
                [code_event_at, *(section.updated_at for section in code_sections)],
            ),
            running=bool(code_running),
            query=query,
            writable=writable,
        )
    digest = results_projection_digest(
        [
            {
                "canvas": ([canvas.id, canvas.title, canvas.updated_at] if canvas is not None else None),
                "sections": [[section.id, section.kind, section.rev, section.updated_at, section.superseded] for section in visible_sections],
                "code_event": [code_event_exists, code_event_at, code_running],
                "report_relations": report_relations.projection_revision,
                "statuses": [
                    "ready" if report_relations_ready else "unavailable",
                    "unavailable",
                    "degraded",
                ],
            }
        ],
    )
    return _ResultsProjection(
        report=report,
        code=code,
        report_exists=report_exists,
        code_exists=code_exists,
        report_relations=report_relations,
        report_relations_ready=report_relations_ready,
        projection_digest=digest,
    )


def _report_only_projection(
    *,
    canvas: Any,
    sections: list[Any],
    relations: ReportRelations,
    query: str,
) -> _ResultsProjection:
    visible_sections = [section for section in sections if not section.hidden_for_chat_mode]
    report_sections = [section for section in visible_sections if section.kind not in {"sql_block", "code_block"}]
    report = project_report_result(
        canvas_id=canvas.id,
        title=_report_title(canvas, report_sections),
        occurred_at=canvas.updated_at,
        editable=False,
        query=query,
        history_available=False,
        export_available=False,
    )
    digest = results_projection_digest(
        [
            {
                "canvas": [canvas.id, canvas.title, canvas.updated_at],
                "report_relations": relations.projection_revision,
                "statuses": ["ready", "unavailable", "degraded"],
            },
        ],
    )
    return _ResultsProjection(
        report=report,
        code=None,
        report_exists=True,
        code_exists=False,
        report_relations=relations,
        report_relations_ready=True,
        projection_digest=digest,
    )


def _to_dto(item: ProjectedResult) -> ResultItemDTOV1:
    if item.route.type == "artifact":
        if item.route.artifact_id is None or item.route.default_mode is None:
            raise ResultsQueryError("results_read_model_unavailable")
        route = ArtifactResultRouteV1(
            type="artifact",
            artifact_id=item.route.artifact_id,
            default_mode=item.route.default_mode,
        )
    elif item.route.type == "report":
        route = ReportResultRouteV1(type="report")
    else:
        route = CodeResultRouteV1(type="code")
    artifact_metadata = None
    if item.artifact_metadata is not None:
        metadata = item.artifact_metadata
        artifact_metadata = ResultArtifactMetadataV1(
            mime=metadata.mime,
            size=metadata.size,
            source=metadata.source,
            kind=metadata.kind,
            origin=origin_for(
                metadata.kind,
                metadata.source,
                metadata.producer_kind,
            ),
            trust=metadata.trust,
            created_at=metadata.created_at,
            producer_kind=metadata.producer_kind,
        )
    return ResultItemDTOV1(
        key=item.key,
        object_type=item.object_type,
        title=item.title,
        primary_group=item.primary_group,
        secondary_group=item.secondary_group,
        viewer_profile=item.viewer_profile,
        status=item.status,
        route=route,
        capabilities=list(item.capabilities),
        relation_roles=list(item.relation_roles),
        execution=None,
        display_context=ResultDisplayContextV1(
            source_role=item.source_role,
            output_role=item.output_role,
            execution_label=None,
            occurred_at=item.occurred_at,
            duplicate_ordinal=None,
            duplicate_count=None,
        ),
        position=item.position,
        version=None,
        artifact_metadata=artifact_metadata,
    )


async def _page_dtos(
    selected: list[ProjectedResult],
    *,
    artifact_store: ArtifactStore,
    canvas_store: CanvasStore,
    viewer_id: str,
) -> list[ResultItemDTOV1]:
    items = [_to_dto(item) for item in selected]
    exports = {
        item.route.artifact_id: item
        for item in items
        if isinstance(item.route, ArtifactResultRouteV1)
        and item.artifact_metadata is not None
        and item.artifact_metadata.source in {"canvas_export", "canvas_export_clone"}
    }
    if not exports:
        return items
    # Only the final authorized page is enriched, never candidate/anchor probes.
    # Titles are display-only (truncated to 500 chars), so classify using the
    # complete stored filename, including for long filenames and sidecars.
    artifacts = await artifact_store.get_many(list(exports))
    if {artifact.id for artifact in artifacts} != exports.keys():
        raise ResultsQueryError("results_snapshot_stale")
    if any(artifact.source != exports[artifact.id].artifact_metadata.source for artifact in artifacts):
        raise ResultsQueryError("results_snapshot_stale")
    trust_by_id = await project_export_trust(
        artifacts,
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        viewer_id=viewer_id,
    )
    for index, item in enumerate(items):
        if isinstance(item.route, ArtifactResultRouteV1) and item.artifact_metadata is not None:
            trust = trust_by_id.get(item.route.artifact_id)
            if trust is not None:
                items[index] = item.model_copy(update={"artifact_metadata": item.artifact_metadata.model_copy(update={"trust": trust})})
    return items


async def _position_synthetics(
    *,
    artifact_store: ArtifactStore,
    session_id: str,
    user_id: str,
    snapshot: ResultsArtifactSnapshot,
    query: str,
    filters: ResultsFiltersV1,
    synthetics: list[ProjectedResult],
    authorized_artifact_ids: tuple[str, ...] | None,
    direct_report_artifact_ids: tuple[str, ...],
    relation_search_artifact_ids: tuple[str, ...],
) -> list[ProjectedResult]:
    positioned: list[ProjectedResult] = []
    ordered = sort_projected_results(synthetics)
    for index, item in enumerate(ordered):
        probe = await artifact_store.query_results_for_session(
            session_id=session_id,
            user_id=user_id,
            snapshot=snapshot,
            normalized_query=query,
            primary=filters.primary,
            secondary=filters.secondary,
            authorized_artifact_ids=authorized_artifact_ids,
            direct_report_artifact_ids=direct_report_artifact_ids,
            relation_search_artifact_ids=relation_search_artifact_ids,
            report_direct_only=filters.lens == "current_report",
            cursor_sort_tuple=item.sort_tuple,
            direction="previous",
            limit=1,
        )
        artifact_before = probe.entries[-1].position if probe.entries else 0
        positioned.append(replace(item, position=artifact_before + index + 1))
    return positioned


def _position_artifacts(
    entries: tuple[ResultsArtifactEntry, ...],
    synthetics: list[ProjectedResult],
    query: str,
    *,
    writable: bool,
    report_only: bool,
    report_relations: ReportRelations,
) -> list[ProjectedResult]:
    out: list[ProjectedResult] = []
    for entry in entries:
        projected = project_artifact_result(
            entry.artifact,
            query,
            writable=writable,
            report_only=report_only,
            relation_roles=report_relations.roles_for(entry.artifact.id),
        )
        synthetic_before = sum(compare_sort_tuples(item.sort_tuple, projected.sort_tuple) < 0 for item in synthetics)
        out.append(replace(projected, position=entry.position + synthetic_before))
    return out


def _synthetics_after_boundary(
    items: list[ProjectedResult],
    boundary: tuple[int | str | None, ...] | None,
    direction: Literal["next", "previous"],
) -> list[ProjectedResult]:
    if boundary is None:
        return items
    return [
        item
        for item in items
        if (compare_sort_tuples(item.sort_tuple, boundary) > 0 if direction == "next" else compare_sort_tuples(item.sort_tuple, boundary) < 0)
    ]


def _artifact_within_snapshot(artifact: Any, snapshot: ResultsArtifactSnapshot) -> bool:
    if snapshot.high_water_created_at is None:
        return False
    if artifact.created_at < snapshot.high_water_created_at:
        return True
    return artifact.created_at == snapshot.high_water_created_at and artifact.id <= snapshot.high_water_artifact_id


async def _resolve_authorized_key(
    *,
    key: str,
    artifact_store: ArtifactStore,
    session_id: str,
    user_id: str,
    snapshot: ResultsArtifactSnapshot,
    query: str,
    filters: ResultsFiltersV1,
    synthetics: list[ProjectedResult],
    writable: bool,
    report_only: bool,
    report_relations: ReportRelations,
    authorized_artifact_ids: tuple[str, ...] | None,
    relation_search_artifact_ids: tuple[str, ...],
) -> ProjectedResult | None:
    synthetic = next((item for item in synthetics if item.key == key), None)
    if synthetic is not None:
        return synthetic
    if not key.startswith("artifact:"):
        return None
    exact = await artifact_store.get_result_artifact_for_scope(
        artifact_id=key.removeprefix("artifact:"),
        session_id=session_id,
        user_id=user_id,
        authorized_artifact_ids=authorized_artifact_ids,
    )
    if exact is None or not _artifact_within_snapshot(exact, snapshot):
        return None
    candidate = project_artifact_result(
        exact,
        query,
        writable=writable,
        report_only=report_only,
        relation_roles=report_relations.roles_for(exact.id),
    )
    if artifact_search_rank(
        exact,
        query,
        relation_roles=report_relations.roles_for(exact.id),
    ) is None or not _matches_filters(candidate, filters):
        return None
    if filters.lens == "current_report" and exact.id not in report_relations.direct_ids:
        return None
    before_probe = await artifact_store.query_results_for_session(
        session_id=session_id,
        user_id=user_id,
        snapshot=snapshot,
        normalized_query=query,
        primary=filters.primary,
        secondary=filters.secondary,
        authorized_artifact_ids=authorized_artifact_ids,
        direct_report_artifact_ids=report_relations.direct_ids,
        relation_search_artifact_ids=relation_search_artifact_ids,
        report_direct_only=filters.lens == "current_report",
        cursor_sort_tuple=candidate.sort_tuple,
        direction="previous",
        limit=1,
    )
    artifact_position = before_probe.entries[-1].position + 1 if before_probe.entries else 1
    entry = ResultsArtifactEntry(
        artifact=exact,
        position=artifact_position,
        sort_tuple=candidate.sort_tuple,
    )
    return _position_artifacts(
        (entry,),
        synthetics,
        query,
        writable=writable,
        report_only=report_only,
        report_relations=report_relations,
    )[0]


async def _query_results(
    *,
    scope: _ResultsScope,
    projection: _ResultsProjection,
    query: str,
    request_body: ResultsQueryRequestV1,
    scale_nav: bool,
    artifact_store: ArtifactStore,
    canvas_store: CanvasStore,
    cursor_codec: ResultsCursorCodec,
    writable: bool,
) -> ResultsQueryResponseV1:
    filters = request_body.filters
    relation_search_artifact_ids = _relation_search_ids(
        projection.report_relations,
        query,
    )
    if filters.lens == "current_report" and not projection.report_relations_ready:
        raise ResultsQueryError("results_filter_unavailable")
    if filters.technical_only:
        # Typed execution relations arrive after C3. Returning an empty set
        # would falsely claim that the filter was complete.
        raise ResultsQueryError("results_filter_unavailable")
    if request_body.surface == "changes" and request_body.snapshot_token is None:
        raise ResultsQueryError("results_query_invalid")
    if request_body.surface == "locate" and request_body.anchor_key is None:
        raise ResultsQueryError("results_query_invalid")
    if request_body.surface in {"locate", "changes"} and request_body.cursor is not None:
        raise ResultsQueryError("results_query_invalid")
    quick_page_size = 8 if query else 10
    if request_body.surface == "quick" and request_body.page_size not in {None, quick_page_size}:
        raise ResultsQueryError("results_query_invalid")
    if request_body.surface == "quick" and request_body.cursor is not None:
        raise ResultsQueryError("results_query_invalid")
    page_size = request_body.page_size or (quick_page_size if request_body.surface == "quick" else 50)
    filter_map = filters.model_dump(mode="json")
    binding = ResultsCursorBinding(
        scope_fingerprint=results_scope_fingerprint(
            route_kind=scope.route_kind,
            team_id=scope.team_id,
            user_id=scope.principal_id,
            scope_id=scope.scope_id,
        ),
        query_filter_hash=results_query_filter_hash(
            query=query,
            filters=filter_map,
        ),
        sort_version=RESULTS_SORT_VERSION_V1,
    )
    if request_body.snapshot_token is None:
        if request_body.cursor is not None:
            raise ResultsQueryError("results_query_invalid")
        high_water = await artifact_store.results_high_water(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            authorized_artifact_ids=scope.authorized_artifact_ids,
        )
        artifact_snapshot = ResultsArtifactSnapshot(
            high_water_created_at=high_water.high_water_created_at,
            high_water_artifact_id=high_water.high_water_artifact_id,
            membership_cutoff_at=datetime.now(tz=UTC).isoformat(
                timespec="microseconds",
            ),
        )
        membership_digest = await artifact_store.results_snapshot_membership_digest(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            authorized_artifact_ids=scope.authorized_artifact_ids,
        )
        snapshot_projection_digest = results_projection_digest(
            [projection.projection_digest, membership_digest],
        )
        snapshot_token, snapshot = cursor_codec.issue_snapshot(
            high_water_created_at=artifact_snapshot.high_water_created_at,
            high_water_artifact_id=artifact_snapshot.high_water_artifact_id,
            membership_cutoff_at=artifact_snapshot.membership_cutoff_at,
            binding=binding,
            projection_digest=snapshot_projection_digest,
        )
        artifact_snapshot = ResultsArtifactSnapshot(
            high_water_created_at=snapshot.high_water_created_at,
            high_water_artifact_id=snapshot.high_water_artifact_id,
            membership_cutoff_at=snapshot.membership_cutoff_at,
        )
    else:
        snapshot_token = request_body.snapshot_token
        try:
            snapshot = cursor_codec.read_snapshot(snapshot_token, expected=binding)
        except ResultsCursorTokenError as exc:
            raise ResultsQueryError("results_snapshot_stale") from exc
        artifact_snapshot = ResultsArtifactSnapshot(
            high_water_created_at=snapshot.high_water_created_at,
            high_water_artifact_id=snapshot.high_water_artifact_id,
            membership_cutoff_at=snapshot.membership_cutoff_at,
        )
        membership_digest = await artifact_store.results_snapshot_membership_digest(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            authorized_artifact_ids=scope.authorized_artifact_ids,
        )
        snapshot_projection_digest = results_projection_digest(
            [projection.projection_digest, membership_digest],
        )
    projection_changed = snapshot.projection_digest != snapshot_projection_digest
    if request_body.surface == "changes":
        new_artifact_count = await artifact_store.count_new_results_for_session(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            normalized_query=query,
            primary=filters.primary,
            secondary=filters.secondary,
            authorized_artifact_ids=scope.authorized_artifact_ids,
            direct_report_artifact_ids=projection.report_relations.direct_ids,
            relation_search_artifact_ids=relation_search_artifact_ids,
            report_direct_only=filters.lens == "current_report",
        )
        return ResultsQueryResponseV1(
            capability=ResultsEffectiveCapabilityV1(scale_nav=scale_nav),
            snapshot_token=snapshot_token,
            items=[],
            authorized_total=0,
            matched_total=0,
            group_counts=ResultsGroupCountsV1(
                deliverables=0,
                evidence=0,
                data=0,
                code=0,
                input=0,
            ),
            loaded_count=0,
            page_start=0,
            next_cursor=None,
            previous_cursor=None,
            has_more=False,
            projection_status=ResultsProjectionStatusV1(
                report_relations=("ready" if projection.report_relations_ready else "unavailable"),
                execution_relations="unavailable",
                export_relations="degraded",
            ),
            projection_revision=snapshot_projection_digest,
            new_results=ResultsNewResultsV1(
                count=new_artifact_count,
                projection_changed=projection_changed,
            ),
            resolved_current_key=None,
        )
    if projection_changed:
        raise ResultsQueryError("results_snapshot_stale")

    direction: Literal["next", "previous"] = "next"
    boundary: tuple[int | str | None, ...] | None = None
    if request_body.cursor is not None:
        try:
            page_cursor = cursor_codec.read_page_cursor(
                request_body.cursor,
                snapshot_token=snapshot_token,
                expected=binding,
            )
        except ResultsCursorTokenError as exc:
            raise ResultsQueryError("results_snapshot_stale") from exc
        direction = page_cursor.direction
        boundary = page_cursor.sort_tuple

    all_synthetics = [
        item for item in (projection.report, projection.code) if filters.lens == "all" and item is not None and _matches_filters(item, filters)
    ]
    positioned_synthetics = await _position_synthetics(
        artifact_store=artifact_store,
        session_id=scope.session_id,
        user_id=scope.artifact_owner_id,
        snapshot=artifact_snapshot,
        query=query,
        filters=filters,
        synthetics=all_synthetics,
        authorized_artifact_ids=scope.authorized_artifact_ids,
        direct_report_artifact_ids=projection.report_relations.direct_ids,
        relation_search_artifact_ids=relation_search_artifact_ids,
    )

    if request_body.surface == "locate":
        anchor_key = request_body.anchor_key or ""
        anchor_item = next(
            (item for item in positioned_synthetics if item.key == anchor_key),
            None,
        )
        anchor_entry: ResultsArtifactEntry | None = None
        if anchor_item is None and anchor_key.startswith("artifact:"):
            artifact_id = anchor_key.removeprefix("artifact:")
            exact = await artifact_store.get_result_artifact_for_scope(
                artifact_id=artifact_id,
                session_id=scope.session_id,
                user_id=scope.artifact_owner_id,
                authorized_artifact_ids=scope.authorized_artifact_ids,
            )
            if exact is not None and _artifact_within_snapshot(
                exact,
                artifact_snapshot,
            ):
                candidate = project_artifact_result(
                    exact,
                    query,
                    writable=writable,
                    report_only=scope.report_only,
                    relation_roles=projection.report_relations.roles_for(
                        exact.id,
                    ),
                )
                direct_match = filters.lens == "all" or exact.id in projection.report_relations.direct_ids
                if (
                    artifact_search_rank(
                        exact,
                        query,
                        relation_roles=projection.report_relations.roles_for(
                            exact.id,
                        ),
                    )
                    is not None
                    and _matches_filters(candidate, filters)
                    and direct_match
                ):
                    before_probe = await artifact_store.query_results_for_session(
                        session_id=scope.session_id,
                        user_id=scope.artifact_owner_id,
                        snapshot=artifact_snapshot,
                        normalized_query=query,
                        primary=filters.primary,
                        secondary=filters.secondary,
                        authorized_artifact_ids=scope.authorized_artifact_ids,
                        direct_report_artifact_ids=projection.report_relations.direct_ids,
                        relation_search_artifact_ids=relation_search_artifact_ids,
                        report_direct_only=filters.lens == "current_report",
                        cursor_sort_tuple=candidate.sort_tuple,
                        direction="previous",
                        limit=1,
                    )
                    artifact_position = before_probe.entries[-1].position + 1 if before_probe.entries else 1
                    anchor_entry = ResultsArtifactEntry(
                        artifact=exact,
                        position=artifact_position,
                        sort_tuple=candidate.sort_tuple,
                    )
                    anchor_item = _position_artifacts(
                        (anchor_entry,),
                        positioned_synthetics,
                        query,
                        writable=writable,
                        report_only=scope.report_only,
                        report_relations=projection.report_relations,
                    )[0]
        if anchor_item is None:
            raise ResultsQueryError("results_anchor_not_in_view")
        before_page = await artifact_store.query_results_for_session(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            normalized_query=query,
            primary=filters.primary,
            secondary=filters.secondary,
            authorized_artifact_ids=scope.authorized_artifact_ids,
            direct_report_artifact_ids=projection.report_relations.direct_ids,
            relation_search_artifact_ids=relation_search_artifact_ids,
            report_direct_only=filters.lens == "current_report",
            cursor_sort_tuple=anchor_item.sort_tuple,
            direction="previous",
            limit=min(64, page_size + 3),
        )
        after_page = await artifact_store.query_results_for_session(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            normalized_query=query,
            primary=filters.primary,
            secondary=filters.secondary,
            authorized_artifact_ids=scope.authorized_artifact_ids,
            direct_report_artifact_ids=projection.report_relations.direct_ids,
            relation_search_artifact_ids=relation_search_artifact_ids,
            report_direct_only=filters.lens == "current_report",
            cursor_sort_tuple=anchor_item.sort_tuple,
            direction="next",
            limit=min(64, page_size + 3),
        )
        combined_entries = {entry.artifact.id: entry for entry in (*before_page.entries, *after_page.entries)}
        if anchor_entry is not None:
            combined_entries[anchor_entry.artifact.id] = anchor_entry
        artifact_items = _position_artifacts(
            tuple(combined_entries.values()),
            positioned_synthetics,
            query,
            writable=writable,
            report_only=scope.report_only,
            report_relations=projection.report_relations,
        )
        candidates = sort_projected_results(
            [*artifact_items, *positioned_synthetics],
        )
        anchor_index = next(
            (index for index, item in enumerate(candidates) if item.key == anchor_item.key),
            -1,
        )
        if anchor_index < 0:
            raise ResultsQueryError("results_anchor_not_in_view")
        start = max(0, anchor_index - page_size // 2)
        start = min(start, max(0, len(candidates) - page_size))
        selected = candidates[start : start + page_size]
        artifact_page = after_page
    else:
        artifact_page = await artifact_store.query_results_for_session(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            normalized_query=query,
            primary=filters.primary,
            secondary=filters.secondary,
            authorized_artifact_ids=scope.authorized_artifact_ids,
            direct_report_artifact_ids=projection.report_relations.direct_ids,
            relation_search_artifact_ids=relation_search_artifact_ids,
            report_direct_only=filters.lens == "current_report",
            cursor_sort_tuple=boundary,
            direction=direction,
            limit=min(64, page_size + 3),
        )
        artifact_items = _position_artifacts(
            artifact_page.entries,
            positioned_synthetics,
            query,
            writable=writable,
            report_only=scope.report_only,
            report_relations=projection.report_relations,
        )
        candidate_synthetics = _synthetics_after_boundary(
            positioned_synthetics,
            boundary,
            direction,
        )
        candidates = sort_projected_results([*artifact_items, *candidate_synthetics])
        if direction == "previous":
            selected = candidates[-page_size:]
        else:
            selected = candidates[:page_size]

    if request_body.surface == "quick" and query:
        # Search relevance and direct-report rank stay authoritative.  Current
        # and recent keys are a tie-breaker only, and every requested key is
        # re-resolved inside the same authorization snapshot.
        quick_candidates = list(candidates)
        requested_keys = [
            key
            for key in [
                request_body.current_key,
                *request_body.recent_keys,
            ]
            if key
        ]
        for key in requested_keys:
            item = await _resolve_authorized_key(
                key=key,
                artifact_store=artifact_store,
                session_id=scope.session_id,
                user_id=scope.artifact_owner_id,
                snapshot=artifact_snapshot,
                query=query,
                filters=filters,
                synthetics=positioned_synthetics,
                writable=writable,
                report_only=scope.report_only,
                report_relations=projection.report_relations,
                authorized_artifact_ids=scope.authorized_artifact_ids,
                relation_search_artifact_ids=relation_search_artifact_ids,
            )
            if item is not None:
                quick_candidates.append(item)
        requested_priority = {key: index for index, key in enumerate(requested_keys)}
        relevance_ordered = sort_projected_results(
            list(
                {item.key: item for item in quick_candidates}.values(),
            ),
        )
        selected = sorted(
            relevance_ordered,
            key=lambda item: (
                item.sort_tuple[0],
                item.sort_tuple[3],
                requested_priority.get(
                    item.key,
                    len(requested_priority),
                ),
            ),
        )[:page_size]

    if request_body.surface == "quick" and not query:
        prioritized: list[ProjectedResult] = []
        report_item = next(
            (item for item in positioned_synthetics if item.object_type == "report"),
            None,
        )
        if report_item is not None:
            prioritized.append(report_item)
        requested_keys = [key for key in [request_body.current_key, *request_body.recent_keys] if key]
        for key in requested_keys:
            item = await _resolve_authorized_key(
                key=key,
                artifact_store=artifact_store,
                session_id=scope.session_id,
                user_id=scope.artifact_owner_id,
                snapshot=artifact_snapshot,
                query=query,
                filters=filters,
                synthetics=positioned_synthetics,
                writable=writable,
                report_only=scope.report_only,
                report_relations=projection.report_relations,
                authorized_artifact_ids=scope.authorized_artifact_ids,
                relation_search_artifact_ids=relation_search_artifact_ids,
            )
            if item is not None:
                prioritized.append(item)

        direct_page = await artifact_store.query_results_for_session(
            session_id=scope.session_id,
            user_id=scope.artifact_owner_id,
            snapshot=artifact_snapshot,
            normalized_query=query,
            primary=filters.primary,
            secondary=filters.secondary,
            authorized_artifact_ids=scope.authorized_artifact_ids,
            direct_report_artifact_ids=projection.report_relations.direct_ids,
            relation_search_artifact_ids=relation_search_artifact_ids,
            report_direct_only=True,
            limit=min(64, page_size + 3),
        )
        direct_added = 0
        prioritized_keys = {item.key for item in prioritized}
        for entry in direct_page.entries:
            item = await _resolve_authorized_key(
                key=f"artifact:{entry.artifact.id}",
                artifact_store=artifact_store,
                session_id=scope.session_id,
                user_id=scope.artifact_owner_id,
                snapshot=artifact_snapshot,
                query=query,
                filters=filters,
                synthetics=positioned_synthetics,
                writable=writable,
                report_only=scope.report_only,
                report_relations=projection.report_relations,
                authorized_artifact_ids=scope.authorized_artifact_ids,
                relation_search_artifact_ids=relation_search_artifact_ids,
            )
            if item is None or item.key in prioritized_keys:
                continue
            prioritized.append(item)
            prioritized_keys.add(item.key)
            direct_added += 1
            if direct_added == 3:
                break
        prioritized.extend(item for item in candidates if item.primary_group == "deliverables")
        selected = list(
            {item.key: item for item in prioritized}.values(),
        )[:page_size]

    synthetic_authorized = int(projection.report_exists) + int(projection.code_exists)
    matched_total = artifact_page.matched_total + len(positioned_synthetics)
    authorized_total = artifact_page.authorized_total + synthetic_authorized
    facet_report = projection.report if filters.lens == "all" else None
    facet_code = projection.code if filters.lens == "all" else None
    group_counts = ResultsGroupCountsV1(
        deliverables=artifact_page.group_counts.deliverables + int(facet_report is not None),
        evidence=artifact_page.group_counts.evidence + int(facet_code is not None),
        data=artifact_page.group_counts.data,
        code=artifact_page.group_counts.code + int(facet_code is not None),
        input=artifact_page.group_counts.input,
    )
    fixed_quick = request_body.surface == "quick"
    page_start = 0 if fixed_quick else (selected[0].position - 1 if selected else 0)
    has_next = bool(not fixed_quick and selected and selected[-1].position < matched_total)
    has_previous = bool(not fixed_quick and selected and selected[0].position > 1)
    next_cursor = (
        cursor_codec.issue_page_cursor(
            snapshot_token=snapshot_token,
            expected=binding,
            direction="next",
            sort_tuple=selected[-1].sort_tuple,
        )
        if has_next
        else None
    )
    previous_cursor = (
        cursor_codec.issue_page_cursor(
            snapshot_token=snapshot_token,
            expected=binding,
            direction="previous",
            sort_tuple=selected[0].sort_tuple,
        )
        if has_previous
        else None
    )
    selected_keys = {item.key for item in selected}
    resolved_current = request_body.current_key if request_body.current_key in selected_keys else None
    return ResultsQueryResponseV1(
        capability=ResultsEffectiveCapabilityV1(scale_nav=scale_nav),
        snapshot_token=snapshot_token,
        items=await _page_dtos(selected, artifact_store=artifact_store, canvas_store=canvas_store, viewer_id=scope.principal_id),
        authorized_total=authorized_total,
        matched_total=matched_total,
        group_counts=group_counts,
        loaded_count=len(selected),
        page_start=page_start,
        next_cursor=next_cursor,
        previous_cursor=previous_cursor,
        has_more=(matched_total > len(selected) if fixed_quick else has_next),
        projection_status=ResultsProjectionStatusV1(
            report_relations=("ready" if projection.report_relations_ready else "unavailable"),
            execution_relations="unavailable",
            export_relations="degraded",
        ),
        projection_revision=snapshot_projection_digest,
        new_results=ResultsNewResultsV1(
            count=0,
            projection_changed=False,
        ),
        resolved_current_key=resolved_current,
    )


async def query_owner_results(
    *,
    session: Session,
    team_id: str,
    request_body: ResultsQueryRequestV1,
    scale_nav: bool,
    session_store: SessionStore,
    canvas_store: CanvasStore,
    artifact_store: ArtifactStore,
    cursor_codec: ResultsCursorCodec,
    writable: bool = True,
) -> ResultsQueryResponseV1:
    query = normalize_results_query(request_body.query)
    if len(query) > 120:
        raise ResultsQueryError("results_query_invalid")
    projection = await _owner_projection(
        session=session,
        session_store=session_store,
        canvas_store=canvas_store,
        query=query,
        writable=writable,
    )
    return await _query_results(
        scope=_ResultsScope(
            route_kind="owner",
            session_id=session.id,
            scope_id=session.id,
            principal_id=session.user_id,
            team_id=team_id,
            artifact_owner_id=session.user_id,
            authorized_artifact_ids=None,
        ),
        projection=projection,
        query=query,
        request_body=request_body,
        scale_nav=scale_nav,
        artifact_store=artifact_store,
        cursor_codec=cursor_codec,
        writable=writable,
        canvas_store=canvas_store,
    )


async def query_report_results(
    *,
    canvas: Any,
    sections: list[Any],
    relations: ReportRelations,
    viewer_id: str,
    team_id: str,
    request_body: ResultsQueryRequestV1,
    scale_nav: bool,
    artifact_store: ArtifactStore,
    canvas_store: CanvasStore,
    cursor_codec: ResultsCursorCodec,
) -> ResultsQueryResponseV1:
    query = normalize_results_query(request_body.query)
    if len(query) > 120:
        raise ResultsQueryError("results_query_invalid")
    projection = _report_only_projection(
        canvas=canvas,
        sections=sections,
        relations=relations,
        query=query,
    )
    return await _query_results(
        scope=_ResultsScope(
            route_kind="report_only",
            session_id=canvas.session_id,
            scope_id=canvas.id,
            principal_id=viewer_id,
            team_id=team_id,
            artifact_owner_id=canvas.user_id,
            authorized_artifact_ids=relations.closure_ids,
        ),
        projection=projection,
        query=query,
        request_body=request_body,
        scale_nav=scale_nav,
        artifact_store=artifact_store,
        cursor_codec=cursor_codec,
        writable=False,
        canvas_store=canvas_store,
    )


__all__ = [
    "ResultsQueryError",
    "query_owner_results",
    "query_report_results",
]
