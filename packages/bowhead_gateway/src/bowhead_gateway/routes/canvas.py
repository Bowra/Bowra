"""Canvas HTTP API (TD §12.4).

GET    /api/v1/canvas/by-session/{session_id}       → 读 canvas + 全部 sections（供首屏/刷新回放）
GET    /api/v1/canvas/{canvas_id}                   → 读 canvas + sections
GET    /api/v1/canvas/{canvas_id}/revisions         → 读 revision history
PATCH  /api/v1/canvas/{canvas_id}/sections/{sid}    → 手动 patch payload（前端直改）
DELETE /api/v1/canvas/{canvas_id}/sections/{sid}    → 删除 section
POST   /api/v1/canvas/{canvas_id}/reorder           → 重排 { "order": [sid, ...] }
POST   /api/v1/canvas/{canvas_id}/title             → 设置 title
POST   /api/v1/canvas/{canvas_id}/export            → 触发 PDF/PPTX（stub）

写操作仅限 canvas owner（user_id 匹配 dev_default_user）。
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Literal, cast

from bowhead_core.iana_time_zone import load_iana_time_zone
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

from bowhead_core.canvas_export_projection import (
    build_canvas_content_projection,
    build_canvas_surface_projection,
)
from bowhead_core.canvas_rank import is_seq_authoritative
from bowhead_core.canvas_store import (
    normalize_report_list_sort,
    report_list_cursor_values,
    report_list_cursor_values_valid,
)
from bowhead_core.chart.expression import ChartExpressionError
from bowhead_core.chart_style_export import (
    ChartStyleExportError,
    build_chart_style_export_theme_v2,
    parse_chart_style_export_freshness_v2,
    parse_chart_style_export_theme,
    require_current_chart_style,
    require_current_chart_style_v2,
)
from bowhead_core.chart_style_materializer import actual_surface_projection
from bowhead_core.export_copy_contract import TRUST_COPY_CONTRACT
from bowhead_core.export_visual_identity import (
    compute_screen_visual_input_sha256_v2,
    compute_visual_input_sha256,
)
from bowhead_core.presentation_locale import resolve_presentation_locale
from bowhead_core.print_theme import PRINT_PAPER
from bowhead_core.sources_resolver import is_protected_sources_section
from bowhead_core.space_governance import SpaceOperation

from bowhead_core import CanvasRevConflictError, CanvasStore
from bowhead_gateway._entitlement_gate import require_not_readonly  # §7.5 read-only gate
from bowhead_gateway.access_context import (
    AccessContextError,
    build_access_context_for_user_from_request,
    has_workspace_access_source,
    is_space_authorization_denial,
    require_space_lifecycle_without_authorization,
    space_denial_code,
)
from bowhead_gateway.artifact_representations import ArtifactRepresentationDTO, Representation, build_representation, representation_error
from bowhead_gateway.artifact_streaming import artifact_streaming_response
from bowhead_gateway.bounded_fanout import run_bounded
from bowhead_gateway.chart_style_resolution import (
    resolve_chart_style_for_user,
)
from bowhead_gateway.dtos import (
    CanvasDetailDTO,
    CanvasExportV2CapabilityDTO,
    CanvasMetaDTO,
    CanvasOrderResultDTO,
    CanvasRestoreResultDTO,
    CanvasRevisionDTO,
    CanvasRevisionsDTO,
    CanvasSectionPatchResultDTO,
    CanvasStatusMetaDTO,
    CanvasStatusResultDTO,
    CanvasSurfaceEntryDTO,
    CanvasSurfaceExclusionDTO,
    CanvasSurfaceProjectionDTO,
    CanvasTitleResultDTO,
    DeletedDTO,
    ExportJobDTO,
    ExportJobsV2ListDTO,
    ExportJobV2DTO,
    FilePreviewDTO,
)
from bowhead_gateway.kb_citation_access import kb_file_access_for_request
from bowhead_gateway.media_preview import INLINE_MEDIA_MIMES, VIDEO_PREVIEW_MIMES
from bowhead_gateway.request_access_cache import request_with_cached_access
from bowhead_gateway.results_capabilities import resolve_results_capabilities
from bowhead_gateway.results_contracts import (
    ResultsQueryRequestV1,
    ResultsQueryResponseV1,
)
from bowhead_gateway.results_cursor import ResultsCursorCodec, ResultsCursorUnavailable
from bowhead_gateway.results_query_http import (
    coded_results_error,
    record_results_query_metrics,
)
from bowhead_gateway.results_query_service import (
    ResultsQueryError,
    query_report_results,
)
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.source_access_projection import (
    SourcePatchError,
    ViewerSafeCanvasProjector,
    ViewerSourceProjectionError,
    citation_presentation,
)

router = APIRouter()


def _store(request: Request) -> CanvasStore:
    s: CanvasStore | None = getattr(request.app.state, "canvas_store", None)
    if s is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="canvas store not configured",
        )
    return s


def _artifact_store(request: Request) -> Any:
    s = getattr(request.app.state, "artifact_store", None)
    if s is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="artifact store not configured",
        )
    return s


def _export_store(request: Request) -> Any:
    export_store = getattr(request.app.state, "export_store", None)
    if export_store is None:
        raise _export_v2_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "export_runner_unavailable",
            "导出服务暂不可用，请稍后重试",
            retryable=True,
        )
    return export_store


def _user_id(request: Request) -> str:
    """P4.5.17: JWT > X-Bowhead-User-Id header > dev_default_user."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


async def _load_owned(
    request: Request,
    store: CanvasStore,
    canvas_id: str,
    *,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> Any:
    canvas = await store.get_canvas(canvas_id)
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    if canvas.user_id != _user_id(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your canvas")
    await _require_canvas_lifecycle(
        request,
        workspace_id=canvas.workspace_id or "",
        user_id=canvas.user_id,
        operation=operation,
    )
    return canvas


async def _require_section_in_canvas(store: CanvasStore, canvas_id: str, sid: str) -> None:
    """A0-4 (report-edit-audit): `/sections/{sid}` 族路由此前只验 canvas 所有权、不验
    sid∈canvas —— 拥有任意一个 canvas 即可 PATCH/DELETE/split/merge 他人 canvas 的
    section (IDOR)。归属不符按 404 处理（不泄漏跨 canvas 存在性）。"""
    owner_canvas = await store.get_section_canvas_id(sid)
    if owner_canvas != canvas_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "section not found")


_UNSET_USER: Any = object()


async def _require_canvas_lifecycle(
    request: Request,
    *,
    workspace_id: str,
    user_id: str,
    operation: SpaceOperation,
    enforce_space_membership: bool = True,
    user: Any = _UNSET_USER,
) -> None:
    """Resolve the canonical persisted Space and apply report lifecycle rules.

    ``user`` lets batch callers pass the already loaded user row (``None`` is a valid
    "no such user" value, so the default sentinel means "load it here").

    ``enforce_space_membership=False`` 表示**这个调用方本来就不是靠空间成员资格进来的**
    （今天只有「被分享人」这一档：``_can_read_canvas`` 的 grant 通路、``scope=shared``
    的报告库分区）。那一档归**批4**（tech-design §4.4「分享按空间级」+ PRD §5.2/§5.6），
    批2 不顺手把它一起收紧——那是另一条拍板，还欠「先量存量、再通知分享者」。
    """

    if not workspace_id or not has_workspace_access_source(request):
        return
    user_store = request.app.state.user_store
    canonical_workspace_id = workspace_id
    try:
        if user is _UNSET_USER or user is None:
            user = await user_store.get_user(user_id)
        resolve_workspace = getattr(
            user_store,
            "resolve_workspace_id_for_team",
            None,
        )
        if user is not None and user.team_id and callable(resolve_workspace):
            canonical_workspace_id = await resolve_workspace(user.team_id, workspace_id) or workspace_id
        if enforce_space_membership:
            # Batch 2 (§4.2): check space membership even for active spaces. Canvas has **no** team-admin
            # bypass (_load_owned strictly requires owner; _can_read_canvas recognizes owner ∨ grant),
            # so retain allow_team_admin_override's default False; do not invent a privilege.
            await build_access_context_for_user_from_request(
                request,
                user_id=user_id,
                workspace_id=canonical_workspace_id,
                operation=operation,
            )
            return
        await require_space_lifecycle_without_authorization(
            user_store=user_store,
            workspace_id=canonical_workspace_id,
            operation=operation,
        )
    except AccessContextError as exc:
        raise HTTPException(
            exc.status_code,
            {"error": {"code": exc.code}},
        ) from exc

    if operation is not SpaceOperation.READ_HISTORY:
        return
    workspace = await user_store.get_workspace(canonical_workspace_id)
    if workspace is not None and str(workspace.get("status") or "") == "archived":
        await build_access_context_for_user_from_request(
            request,
            user_id=user_id,
            workspace_id=canonical_workspace_id,
            operation=operation,
        )


_REPORT_UNAVAILABLE = {"code": "report_unavailable"}  # FE placeholder-page contract (PRD §5.3).


class ReportArtifactItemDTO(BaseModel):
    id: str
    filename: str
    mime: str
    size: int
    source: str
    created_at: str
    preview_available: bool
    kind: str = "tool_data"
    origin: str = "upload"
    trust: str = "trusted"
    created_by_turn: str | None = None
    producer_kind: str | None = None
    derived_from: str | None = None
    manifest_artifact_id: str | None = None


class ReportArtifactListDTO(BaseModel):
    items: list[ReportArtifactItemDTO]


_PREVIEWABLE_MIME_PREFIXES = (
    "text/",
    "image/",
    "application/json",
)
_PREVIEWABLE_MIMES = {
    "application/x-ndjson",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
} | VIDEO_PREVIEW_MIMES
# file-management-v2 P0 convergence: source/kind→origin has one implementation in artifact_origin.py.
# Remove this file's manual copy; it lacked image_generate/web_fetch/consultant_deck and incorrectly
# defaulted to upload, labeling AI-generated images uploaded in canvas source-panel.
from bowhead_gateway.artifact_origin import origin_for as _origin_for_artifact


def _can_preview_mime(mime: str) -> bool:
    if any(mime.startswith(p) for p in _PREVIEWABLE_MIME_PREFIXES):
        return True
    return mime in _PREVIEWABLE_MIMES


async def _can_read_canvas(request: Request, canvas: Any) -> bool:
    """content-permissions S5 读通路（tech-design §4.3）：owner 短路 ∨ report read grant。

    读写物理分离：本 helper 只给 READ 路径（by-session / artifact 中介）用；全部写路由
    继续走 ``_load_owned``（owner 硬卡），grant 通路结构上接触不到写。grant 叠加挂
    ``content_sharing_enabled()``；任何缺失/异常 → False（owner 之外 fail-closed）。
    team 包含：canvas 的 workspace（ws_*/datarc 双标识）必须属于 caller 的 team。
    """
    viewer_id = _user_id(request)
    is_owner = canvas.user_id == viewer_id
    allowed = is_owner
    if not allowed:
        try:
            from bowhead_core.share_service import (
                content_sharing_enabled,
                grants_for_viewer,
            )

            if not content_sharing_enabled():
                return False
            access = getattr(request.app.state, "access_control_store", None)
            user_store = getattr(request.app.state, "user_store", None)
            if access is None or user_store is None:
                return False
            viewer = await user_store.get_user(viewer_id)
            if viewer is None or not viewer.team_id:
                return False
            if not await user_store.workspace_in_team(
                viewer.team_id,
                canvas.workspace_id or "",
            ):
                return False
            granted = await grants_for_viewer(
                access,
                team_id=viewer.team_id,
                user_id=viewer_id,
                kind="report",
            )
            allowed = canvas.session_id in granted
        except Exception:
            return False
    if not allowed:
        return False
    try:
        await _require_canvas_lifecycle(
            request,
            workspace_id=canvas.workspace_id or "",
            user_id=viewer_id,
            operation=SpaceOperation.READ_HISTORY,
            # Share recipients gain access without space membership -> batch 4 (§4.4).
            enforce_space_membership=is_owner,
        )
    except HTTPException as exc:
        if is_space_authorization_denial(space_denial_code(exc)):
            # D3: report reads deny with 403 report_unavailable (FE placeholder-page contract;
            # see _REPORT_UNAVAILABLE), without exposing internal ws_access_denied:* codes to FE.
            # Lifecycle errors (409 workspace_archived) continue propagating unchanged.
            return False
        raise
    return True


def _resolve_reload_locale(request: Request) -> str:
    """Choose the page/export visual locale without transforming stored text."""
    accept_language = (request.headers.get("accept-language") or "").lower()
    if accept_language.startswith("de"):
        return "de-DE"
    if accept_language.startswith("fr"):
        return "fr-FR"
    return "zh-CN" if accept_language.startswith("zh") else "en-US"


def _canvas_detail(
    canvas: Any,
    sections: list[Any],
    locale: str = "en-US",
    quarantined_count: int | None = None,
    projection_sections: list[Any] | None = None,
    export_v2_capabilities: list[CanvasExportV2CapabilityDTO] | None = None,
) -> CanvasDetailDTO:
    """Section DTO 恒为 **DB 原文**（citation-audit M3-3 / §4.5）。

    原有的 `raw` / `locale` 两参服务于 i18n inline-numbers v1 的 egress render
    （`raw=False` 时把 `[num:V:U]` 渲成 locale 数字）。该子系统随本批退役，且那条
    render 路**从未被用户面走过**——FE 恒传 `raw=true`（否则 citation-plugin 匹配
    不到标记，数字失去溯源）。`raw` 与正文转换已删除；`locale` 仅保留为 Canvas
    Export V2 visual identity 的输入，不改变落库或响应正文。

    quarantined_count: R2 显示态用. 传入则用权威全量计数(与 GET /status 一致);
    缺省退回数 sections 里 quarantined(注: list_sections 默认滤掉 hidden_for_chat_mode,
    既 quarantine 又 hidden 的 section 会漏数 → 故 detail 端点应传入 count_quarantined)。
    """
    from bowhead_core.canvas_store import _effective_status

    rendered = []
    quar = 0
    for s in sections:
        if getattr(s, "quarantined", False):
            quar += 1
        rendered.append(s.to_report_section())
    if quarantined_count is not None:
        quar = quarantined_count
    projection_source = projection_sections if projection_sections is not None else sections
    content_projection = build_canvas_content_projection(
        title=canvas.title,
        sections=projection_source,
    )
    surface_projection = build_canvas_surface_projection(
        content_projection=content_projection,
        sections=projection_source,
    )
    return CanvasDetailDTO(
        canvas=CanvasMetaDTO(
            id=canvas.id,
            sessionId=canvas.session_id,
            workspaceId=canvas.workspace_id,
            title=canvas.title,
            status=canvas.status,
            effectiveStatus=_effective_status(canvas.status, quar),  # R2: report-header chip.
            quarantinedCount=quar,
            createdAt=canvas.created_at,
            updatedAt=canvas.updated_at,
        ),
        sections=rendered,
        projectionToken=content_projection.projection_token,
        surfaceProjection=CanvasSurfaceProjectionDTO(
            contractVersion=surface_projection.contract_version,
            contentProjectionToken=surface_projection.content_projection_token,
            surfaceToken=surface_projection.surface_token,
            entries=[
                CanvasSurfaceEntryDTO(
                    sectionId=entry.section_id,
                    sectionRev=entry.section_rev,
                    order=entry.order,
                    placement=entry.placement,
                    parentSectionId=entry.parent_section_id,
                    effectiveKind=entry.effective_kind,
                    parityRequirement=entry.parity_requirement,
                    # Export identity depends on the authenticated user's
                    # effective style and is computed during prepare/create.
                    visualInputSha256=None,
                )
                for entry in surface_projection.entries
            ],
            exclusions=[
                CanvasSurfaceExclusionDTO(
                    sectionId=exclusion.section_id,
                    reason=exclusion.reason,
                )
                for exclusion in surface_projection.exclusions
            ],
        ),
        surfaceToken=surface_projection.surface_token,
        exportV2Capabilities=export_v2_capabilities or [],
        # A3: flag off (dark ship) / unmigrated old data -> false; FE retains forced-rank fallback.
        seqAuthoritative=is_seq_authoritative(),
    )


async def _build_source_refs_for_session(
    request: Request,
    session_id: str,
    *,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """W11-5 / B-1: 从 session 的 turn 事件聚合 SourceRef metadata."""
    try:
        from bowhead_core.source_ref_aggregator import (
            aggregate_source_refs_for_session,
        )

        ses_store = getattr(request.app.state, "session_store", None)
        log_store = getattr(request.app.state, "turn_log_store", None)
        if ses_store is None or log_store is None:
            if strict:
                raise RuntimeError("source relation stores unavailable")
            return []
        if strict:
            from bowhead_core.source_ref_aggregator import (
                aggregate_source_refs_from_events,
            )

            turns = await ses_store.list_turns(session_id=session_id)
            out: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for turn in turns:
                events = await log_store.list_events(turn.id)
                for source_ref in aggregate_source_refs_from_events(
                    turn.id,
                    events,
                ):
                    source_id = source_ref.get("id")
                    if not source_id or source_id in seen_ids:
                        continue
                    seen_ids.add(source_id)
                    out.append(source_ref)
            return out
        return await aggregate_source_refs_for_session(
            session_id,
            session_store=ses_store,
            turn_log_store=log_store,
        )
    except Exception:
        if strict:
            raise
        # Aggregation failure does not block canvas load; frontend popover falls back.
        return []


async def _viewer_safe_projector(
    request: Request,
    canvas: Any,
    projection_sections: list[Any],
    source_refs: list[dict[str, Any]],
    additional_sections: list[dict[str, Any]] | None = None,
) -> ViewerSafeCanvasProjector:
    canonical_sections = [section.to_report_section() for section in projection_sections]
    canonical_sections.extend(additional_sections or [])
    closure = _closure_with_source_refs(_closure_from_sections(projection_sections), source_refs)
    try:
        return await ViewerSafeCanvasProjector.build(
            root_secret=getattr(request.app.state, "artifact_fetch_secret", None),
            workspace_id=canvas.workspace_id or "",
            session_id=canvas.session_id or "",
            sections=canonical_sections,
            source_refs=source_refs,
            report_artifact_closure=closure,
            artifact_store=getattr(request.app.state, "artifact_store", None),
            kb_file_access=kb_file_access_for_request(request),
        )
    except ViewerSourceProjectionError as exc:
        logger.error("canvas_source_projection_context_unavailable")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "source_projection_unavailable"}},
        ) from exc


async def _project_canvas_detail(
    request: Request,
    canvas: Any,
    projection_sections: list[Any],
    detail: CanvasDetailDTO,
    source_refs: list[dict[str, Any]],
) -> CanvasDetailDTO:
    projector = await _viewer_safe_projector(request, canvas, projection_sections, source_refs)
    detail.sections, detail.sourceRefs = projector.project(detail.sections, source_refs)
    # review F4: CLAUDE.md and tech-design require a default-off FEATURE_ gate + rollout.
    # Gate at the wire boundary, not inside the pure helper; direct helper tests stay unaffected.
    # Rollback by unsetting the variable: sidecar stays None; FE keeps all hidden, matching prerelease behavior.
    if _source_attribution_v2_enabled():
        try:
            detail.turnCitations = await _turn_citations_sidecar(request, canvas, projection_sections, source_refs)
        except Exception:
            # review F5: failure **retains None**, not {}. None means not fetched this time,
            # letting FE reconciliation keep previous good data; {} means truly no citations and clears
            # rendered citation marks. Fail softly without blocking canvas load, but log it (tech-design §0.4).
            logger.warning("canvas_turn_citations_sidecar_failed canvas=%s", getattr(canvas, "id", ""))
    return detail


def _source_attribution_v2_enabled() -> bool:
    from bowhead_gateway.source_access_projection import source_attribution_v2_enabled

    return source_attribution_v2_enabled()


# source-accessibility §1.6 consumer half c1: sidecar row cap per turn.
# 116 observed at most 166 per turn (existing distribution: consumer-discovery §1.1). 512 is a defensive
# soft limit, not product semantics ([[feedback_design_no_hard_caps]]); truncate excess and log.
_TURN_CITATIONS_CAP = 512


async def _turn_citations_sidecar(
    request: Request,
    canvas: Any,
    projection_sections: list[Any],
    source_refs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """turn_id → viewer-safe 引用行（consumer-discovery §2.1）。

    正文里的 `[cite:N]` 由前端 citation-plugin 按所在段落的 turn_id 查本表解析成角标；
    报告库 / 分享视图没有 turn events，这份数据只能随 detail DTO 走。

    判据 = 批 1 ``attribute_refs``（与事件投影同一套归属判定，tech-design §0.3）；
    态映射 = ``citation_presentation``（单点，与事件投影同形）。非 OWNED 的行只有
    ``{seq, kind, access_state}``，label / anchor 一律不下发。
    """
    from bowhead_core.source_attribution import AttributionStores, ReportScope, attribute_refs

    session_store = getattr(request.app.state, "session_store", None)
    session_id = getattr(canvas, "session_id", None) or ""
    if session_store is None or not session_id:
        return {}

    # Turn set comes from paragraphs FE actually renders (not hidden; superseded still renders in folded history).
    turn_ids: list[str] = []
    for section in projection_sections:
        turn_id = getattr(section, "turn_id", None)
        if turn_id and not getattr(section, "hidden_for_chat_mode", False) and turn_id not in turn_ids:
            turn_ids.append(turn_id)
    if not turn_ids:
        return {}

    rows_by_turn: dict[str, list[tuple[int, str, str | None, str]]] = {}
    refs: list[tuple[str, str]] = []
    # review F7: one batched IN query; detail is a hot read endpoint, so one connection per turn is N+1.
    batch = getattr(session_store, "list_turn_citations_for_turns", None)
    if callable(batch):
        all_rows = await batch(turn_ids=turn_ids)
    else:  # Old store / test-double fallback: per-turn, equivalent behavior.
        all_rows = {t: await session_store.list_turn_citations(turn_id=t) for t in turn_ids}
    for turn_id in turn_ids:
        rows = all_rows.get(turn_id) or []
        if not rows:
            continue
        if len(rows) > _TURN_CITATIONS_CAP:
            logger.info(
                "canvas_turn_citations_truncated turn=%s total=%d cap=%d",
                turn_id,
                len(rows),
                _TURN_CITATIONS_CAP,
            )
            rows = rows[:_TURN_CITATIONS_CAP]
        rows_by_turn[turn_id] = rows
        refs.extend((kind, artifact_id) for _seq, artifact_id, _label, kind in rows)
    if not rows_by_turn:
        return {}

    # rendered uses the same closure as byte delivery: linked_accessible must match the actual set admitted by
    # /by-session/{sid}/artifacts/{aid}.
    rendered = frozenset(_closure_with_source_refs(_closure_from_sections(projection_sections), source_refs))
    # Same branch as events: artifacts produced by this turn (created_by_turn == citing turn,
    # server-stamped ownership) have byte access. Event batch 2 already includes them
    # via _event_projector's owned_event_ids. Detail must also include them, or a citation is accessible
    # live but labeled on canvas load. Bucket by turn and include when present.
    owned_by_turn: dict[str, set[str]] = {}
    _art_refs = list(dict.fromkeys(aid for _k, aid in refs if isinstance(aid, str) and aid.startswith("art_")))
    _art_store = getattr(request.app.state, "artifact_store", None)
    _get_many = getattr(_art_store, "get_many", None)
    if _art_refs and callable(_get_many):
        with contextlib.suppress(Exception):  # Fail-closed: failed lookup never expands access.
            for _art in await _get_many(_art_refs):
                _owner_turn = getattr(_art, "created_by_turn", None)
                _aid = getattr(_art, "id", None)
                if _aid and _owner_turn in rows_by_turn:
                    owned_by_turn.setdefault(_owner_turn, set()).add(_aid)
    # Since batch 2, share the event collector (same source_refs branch shape; see its docstring).
    from bowhead_gateway.source_access_projection import source_ref_artifact_ids as _srai

    source_ref_ids = set(_srai(source_refs))
    session_turns = frozenset(turn.id for turn in await session_store.list_turns(session_id=session_id) if getattr(turn, "id", None))
    scope = ReportScope(
        workspace_id=getattr(canvas, "workspace_id", None) or "",
        session_id=session_id,
        canvas_id=getattr(canvas, "id", None),
        turn_id=None,
        session_turn_ids=session_turns,
        source_ref_artifact_ids=frozenset(source_ref_ids),
        rendered_artifact_ids=rendered,
    )
    stores = AttributionStores(
        artifact_store=getattr(request.app.state, "artifact_store", None),
        canvas_store=getattr(request.app.state, "canvas_store", None),
        kb_rows_store=getattr(request.app.state, "kb_extracted_rows_store", None),
        kb_file_access=kb_file_access_for_request(request),
    )
    verdicts = await attribute_refs(refs, scope=scope, stores=stores)

    provenance = {}
    read_provenance = getattr(session_store, "list_kb_citation_provenance", None)
    if callable(read_provenance):
        provenance = await read_provenance(turn_ids)
        if not isinstance(provenance, dict):
            provenance = {}

    out: dict[str, list[dict[str, Any]]] = {}
    for turn_id, rows in rows_by_turn.items():
        rendered_for_turn: frozenset[str] | Any = rendered
        if owned_by_turn.get(turn_id):
            rendered_for_turn = frozenset(rendered | owned_by_turn[turn_id])
        out[turn_id] = [
            citation_presentation(
                seq=seq,
                kind=kind,
                label=label,
                verdict=verdicts.get((kind, artifact_id)),
                rendered_artifact_ids=rendered_for_turn,
            )
            for seq, artifact_id, label, kind in rows
        ]
        for row in out[turn_id]:
            if row.get("kb_file_id"):
                row.update(provenance.get(turn_id, {}).get(row["seq"], {}))
    return out


def _revision_projection_section(
    payload: dict[str, Any] | None,
    *,
    revision_id: str,
    side: str,
    current_kind: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return a synthetic section plus an optional delete-envelope copy."""
    if payload is None:
        return None, None
    envelope = copy.deepcopy(payload)
    if payload.get("restore_schema") == 1 and isinstance(payload.get("payload"), dict):
        inner = payload["payload"]
        kind = str(payload.get("kind") or current_kind or _infer_revision_section_kind(inner))
        section = {**copy.deepcopy(inner), "id": f"revision:{revision_id}:{side}", "kind": kind}
        return section, envelope
    kind = current_kind or _infer_revision_section_kind(payload)
    return {**copy.deepcopy(payload), "id": f"revision:{revision_id}:{side}", "kind": kind}, None


def _infer_revision_section_kind(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("entries"), list):
        return "sources"
    spec = payload.get("spec")
    if (isinstance(spec, dict) and "source_meta" in spec) or "data_source" in payload:
        return "chart"
    return "narrative"


def _project_revision_payload(
    projector: ViewerSafeCanvasProjector,
    section: dict[str, Any] | None,
    envelope: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if section is None:
        return None
    projected, _ = projector.project([section])
    safe_payload = {key: value for key, value in projected[0].items() if key not in {"id", "kind"}}
    if envelope is None:
        return safe_payload
    envelope["payload"] = safe_payload
    return envelope


@router.get("/by-session/{session_id}", response_model=CanvasDetailDTO)
async def get_canvas_by_session(
    session_id: str,
    request: Request,
    expect_report: bool = False,
    store: CanvasStore = Depends(_store),
) -> CanvasDetailDTO:
    """Section 恒返 DB 原文（citation-audit M3-3：i18n render 半边随 §4.5 退役，
    原 `raw` 参数取消——FE 本来就恒传 raw=true；FastAPI 忽略未知 query 参数，
    在途旧前端不受影响）。"""
    canvas = await store.get_canvas_by_session(session_id)
    if canvas is None:
        if expect_report:
            raise HTTPException(status.HTTP_403_FORBIDDEN, _REPORT_UNAVAILABLE)
        return CanvasDetailDTO(canvas=None, sections=[])
    # content-permissions S5: owner ∨ report read grant; make report-refactor §4.1's placeholder sharing real
    # so share links /?session={sid} actually open for recipients. Denial follows placeholder-page contract.
    if not await _can_read_canvas(request, canvas):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _REPORT_UNAVAILABLE)
    projection_state = await store.read_canvas_projection_state(canvas.id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    canvas, projection_sections = projection_state
    sections = [section for section in projection_sections if not section.hidden_for_chat_mode]
    quar = sum(1 for section in projection_sections if section.quarantined)
    detail = _canvas_detail(
        canvas,
        sections,
        _resolve_reload_locale(request),
        quarantined_count=quar,
        projection_sections=projection_sections,
        export_v2_capabilities=_available_export_v2_capabilities(request),
    )
    # Canonical source metadata never crosses the browser boundary.  Projection
    # happens after visual/content hashes are computed from canonical payloads.
    source_refs = await _build_source_refs_for_session(request, session_id)
    return await _project_canvas_detail(request, canvas, projection_sections, detail, source_refs)


def _add_artifact_id(ids: list[str], seen: set[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in seen:
        seen.add(value)
        ids.append(value)


def _closure_from_sections(sections: list[Any]) -> list[str]:
    """Report artifact closure from visible persisted section payloads.

    This intentionally follows known artifact-id field names, including nested
    objects and code_block input/output arrays, instead of broad string matching.
    """
    from bowhead_core.results_report_relations import project_report_relations

    return list(project_report_relations(sections).closure_ids)


def _closure_with_source_refs(section_ids: list[str], refs: list[dict[str, Any]]) -> list[str]:
    ids = list(section_ids)
    seen = set(section_ids)
    for ref in refs:
        if isinstance(ref, dict):
            _add_artifact_id(ids, seen, ref.get("artifact_id"))
            # Feishu 07.06 #11: aggregated batch refs no longer collapse into a top-level artifact_id.
            # Closure must cover each batch call's child table (shared-report/export reads depend on it;
            # omissions -> child-table 403).
            for sub in ref.get("sub_sources") or []:
                if isinstance(sub, dict):
                    _add_artifact_id(ids, seen, sub.get("artifact_id"))
    return ids


async def _report_artifact_closure(
    request: Request,
    session_id: str,
    store: CanvasStore,
) -> tuple[Any, list[str]]:
    canvas, _, relations = await _report_results_relations(
        request,
        session_id,
        store,
        strict_source_refs=False,
    )
    return canvas, list(relations.closure_ids)


async def _report_results_relations(
    request: Request,
    session_id: str,
    store: CanvasStore,
    *,
    strict_source_refs: bool,
) -> tuple[Any, list[Any], Any]:
    from bowhead_core.results_report_relations import project_report_relations

    canvas = await store.get_canvas_by_session(session_id)
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    if not await _can_read_canvas(request, canvas):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _REPORT_UNAVAILABLE)
    projection_state = await store.read_canvas_projection_state(canvas.id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    canvas, sections = projection_state
    refs = await _build_source_refs_for_session(
        request,
        session_id,
        strict=strict_source_refs,
    )
    relations = project_report_relations(sections, source_refs=refs)
    return canvas, sections, relations


async def _get_report_artifact_in_closure(
    request: Request,
    session_id: str,
    artifact_id: str,
    store: CanvasStore,
) -> Any:
    _, closure = await _report_artifact_closure(request, session_id, store)
    if artifact_id not in set(closure):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    artifact_store = _artifact_store(request)
    art = await artifact_store.get(artifact_id)
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    return art


def _report_artifact_item(art: Any, *, trust_override: str | None = None) -> ReportArtifactItemDTO:
    kind = getattr(art, "kind", None) or "tool_data"
    return ReportArtifactItemDTO(
        id=art.id,
        filename=art.filename,
        mime=art.mime,
        size=art.size,
        source=art.source,
        created_at=art.created_at,
        preview_available=_can_preview_mime(art.mime),
        kind=kind,
        origin=_origin_for_artifact(
            kind,
            art.source,
            producer_kind=getattr(art, "producer_kind", None),
        ),
        trust=trust_override or getattr(art, "trust", None) or "trusted",
        created_by_turn=getattr(art, "created_by_turn", None),
        producer_kind=getattr(art, "producer_kind", None),
        # Reference IDs are intentionally not projected on the report-scoped
        # read path: a version parent or validation manifest may sit outside
        # the persisted report closure.  The optional fields stay shape-
        # compatible without creating a closure-membership side channel.
        derived_from=None,
        manifest_artifact_id=None,
    )


@router.post(
    "/by-session/{session_id}/results/query",
    response_model=ResultsQueryResponseV1,
)
async def report_results_query(
    session_id: str,
    body: ResultsQueryRequestV1,
    request: Request,
    response: Response,
    store: CanvasStore = Depends(_store),
    user: Any = Depends(_current_user),
) -> ResultsQueryResponseV1:
    """Complete Results read model constrained to the readable report closure."""

    started_at = time.perf_counter()
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    capabilities = resolve_results_capabilities()
    if not capabilities.complete_list:
        record_results_query_metrics(
            started_at=started_at,
            scope="report_only",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        )
    try:
        canvas, sections, relations = await _report_results_relations(
            request,
            session_id,
            store,
            strict_source_refs=True,
        )
    except HTTPException as exc:
        if exc.status_code in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        }:
            record_results_query_metrics(
                started_at=started_at,
                scope="report_only",
                surface=body.surface,
                error_class="results_scope_unavailable",
            )
            raise coded_results_error(
                status.HTTP_404_NOT_FOUND,
                "results_scope_unavailable",
            ) from exc
        logger.warning(
            "results.report_scope_http_unavailable status_class=%sxx",
            exc.status_code // 100,
        )
        record_results_query_metrics(
            started_at=started_at,
            scope="report_only",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        ) from exc
    except Exception as exc:
        logger.exception(
            "results.report_scope_unavailable error_type=%s",
            type(exc).__name__,
        )
        record_results_query_metrics(
            started_at=started_at,
            scope="report_only",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        ) from exc

    artifact_store = getattr(request.app.state, "artifact_store", None)
    if artifact_store is None:
        record_results_query_metrics(
            started_at=started_at,
            scope="report_only",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        )
    try:
        result = await query_report_results(
            canvas=canvas,
            sections=sections,
            relations=relations,
            viewer_id=user.id,
            team_id=user.team_id,
            request_body=body,
            scale_nav=capabilities.scale_nav,
            artifact_store=artifact_store,
            canvas_store=store,
            cursor_codec=ResultsCursorCodec.from_environment(),
        )
        record_results_query_metrics(
            started_at=started_at,
            scope="report_only",
            surface=body.surface,
            error_class="none",
            result=result,
        )
        return result
    except ResultsCursorUnavailable as exc:
        cause = exc
        error_code = "results_read_model_unavailable"
        error_status = status.HTTP_503_SERVICE_UNAVAILABLE
    except ResultsQueryError as exc:
        cause = exc
        error_code = exc.code
        error_status = {
            "results_query_invalid": status.HTTP_400_BAD_REQUEST,
            "results_snapshot_stale": status.HTTP_409_CONFLICT,
            "results_anchor_not_in_view": status.HTTP_409_CONFLICT,
            "results_filter_unavailable": status.HTTP_409_CONFLICT,
            "results_read_model_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_artifact_contract_unsupported": status.HTTP_409_CONFLICT,
        }[exc.code]
    except Exception as exc:
        cause = exc
        logger.exception(
            "results.report_query_unavailable error_type=%s",
            type(exc).__name__,
        )
        error_code = "results_read_model_unavailable"
        error_status = status.HTTP_503_SERVICE_UNAVAILABLE
    record_results_query_metrics(
        started_at=started_at,
        scope="report_only",
        surface=body.surface,
        error_class=error_code,
    )
    raise coded_results_error(error_status, error_code) from cause


@router.get("/by-session/{session_id}/artifacts", response_model=ReportArtifactListDTO)
async def list_report_artifacts(
    session_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
    q: str | None = Query(default=None, description="filename substring filter"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ReportArtifactListDTO:
    """Report-scoped artifact list for shared/read-only report views.

    Unlike /sessions/{id}/artifacts this never exposes the whole session. It only
    lists live artifacts in the persisted report closure.
    """
    _, closure = await _report_artifact_closure(request, session_id, store)
    artifact_store = _artifact_store(request)
    from bowhead_core.export_copy_contract import CURRENT_OUTPUT_PRODUCER_VALUES
    from bowhead_core.results_artifact_policy import is_results_internal_artifact

    query = (q or "").strip().lower()
    raw_items: list[Any] = []
    for aid in closure:
        art = await artifact_store.get(aid)
        if art is None:
            continue
        raw_items.append(art)

    artifacts_by_id = {art.id: art for art in raw_items}
    manifest_artifact_ids: set[str] = set()
    for art in raw_items:
        manifest_id = getattr(art, "manifest_artifact_id", None)
        manifest = artifacts_by_id.get(manifest_id) if isinstance(manifest_id, str) else None
        if (
            manifest is not None
            and getattr(art, "producer_kind", None) in CURRENT_OUTPUT_PRODUCER_VALUES
            and getattr(art, "created_by", None) == getattr(manifest, "created_by", None)
        ):
            manifest_artifact_ids.add(manifest_id)
    raw_items = [
        art
        for art in raw_items
        if not is_results_internal_artifact(
            art,
            manifest_artifact_ids=manifest_artifact_ids,
        )
        and (not query or query in (art.filename or "").lower())
    ]
    from bowhead_gateway.routes.files import _export_trust_projection

    trust_by_id = await _export_trust_projection(request, artifact_store, raw_items)
    items = [_report_artifact_item(art, trust_override=trust_by_id.get(art.id)) for art in raw_items]
    return ReportArtifactListDTO(items=items[offset : offset + limit])


@router.get("/by-session/{session_id}/artifacts/{artifact_id}")
async def get_report_artifact(
    session_id: str,
    artifact_id: str,
    request: Request,
    disposition: str | None = None,
    store: CanvasStore = Depends(_store),
):  # type: ignore[no-untyped-def]
    """content-permissions S5 — report 渲染闭包内 artifact 的中介只读端点。

    设计（tech-design §4.3）：① ``_can_read_canvas``（owner ∨ grant）② ``aid ∈ closure``
    否则 404（不泄露存在性）③ **全代理流式,绝不 302/presigned**——presigned URL 可被
    转发/缓存,等价把 bypass 链接交给 grantee。owner 也走本路径（report 视图单代码路径）。
    closure 两段式：先廉价 sections 扫描,miss 才跑 source_refs 聚合。
    保留 files.py 的 inline 白名单 + nosniff 防线。
    """
    art = await _get_report_artifact_in_closure(request, session_id, artifact_id, store)
    artifact_store = _artifact_store(request)
    # Fully proxy (no 302): closure+grant authorizes, intermediary replaces owner check (files.py direct path unchanged).
    _inline = disposition == "inline" and (art.mime or "").lower() in INLINE_MEDIA_MIMES
    return await artifact_streaming_response(
        request,
        artifact_store,
        art,
        inline=_inline,
    )


@router.get("/by-session/{session_id}/artifacts/{artifact_id}/representations/{representation}", response_model=ArtifactRepresentationDTO)
async def get_report_artifact_representation(
    session_id: str,
    artifact_id: str,
    representation: Representation,
    request: Request,
    response: Response,
    store: CanvasStore = Depends(_store),
) -> ArtifactRepresentationDTO:
    _user_id(request)  # Authenticate before reading report/closure/object existence.
    try:
        art = await _get_report_artifact_in_closure(request, session_id, artifact_id, store)
    except HTTPException as exc:
        if exc.status_code in {403, 404, 410}:
            raise representation_error("scope_unavailable") from exc
        raise
    response.headers["Cache-Control"] = "no-store"
    return await build_representation(_artifact_store(request), art, representation)


@router.get("/by-session/{session_id}/artifacts/{artifact_id}/preview", response_model=FilePreviewDTO)
async def get_report_artifact_preview(
    session_id: str,
    artifact_id: str,
    request: Request,
    mode: str = Query(default="auto", description="auto | text | rows | image"),
    max_rows: int = Query(default=100, ge=1, le=10_000),
    store: CanvasStore = Depends(_store),
) -> FilePreviewDTO:
    """Report-scoped artifact preview.

    Uses the same owner-or-grant + report closure checks as the download proxy,
    then reuses the file preview DTO builder without applying the owner-only
    /files/{id}/preview check. This lets shared-report viewers inspect sources
    referenced by the report without granting broad file-library access.
    """
    art = await _get_report_artifact_in_closure(request, session_id, artifact_id, store)
    artifact_store = _artifact_store(request)
    from bowhead_gateway.routes.files import build_file_preview

    return await build_file_preview(artifact_store, art, mode=mode, max_rows=max_rows)


@router.get("/{canvas_id}", response_model=CanvasDetailDTO)
async def get_canvas(
    canvas_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasDetailDTO:
    """Section 恒返 DB 原文（同 by-session；`raw` 参数随 M3-3 取消）。

    WP6 注: 当前 FE 全走 by-session, 此端点无前端调用方 — 保留作 API 完整性
    (canvas_id 直取是合理 REST 面, 外部集成/MCP 可用), 勿因"孤儿"误删.
    """
    await _load_owned(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    projection_state = await store.read_canvas_projection_state(canvas_id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    canvas, projection_sections = projection_state
    sections = [section for section in projection_sections if not section.hidden_for_chat_mode]
    quar = sum(1 for section in projection_sections if section.quarantined)
    detail = _canvas_detail(
        canvas,
        sections,
        _resolve_reload_locale(request),
        quarantined_count=quar,
        projection_sections=projection_sections,
        export_v2_capabilities=_available_export_v2_capabilities(request),
    )
    source_refs = await _build_source_refs_for_session(request, canvas.session_id)
    return await _project_canvas_detail(request, canvas, projection_sections, detail, source_refs)


@router.get("/{canvas_id}/revisions", response_model=CanvasRevisionsDTO)
async def list_revisions(
    canvas_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasRevisionsDTO:
    canvas = await _load_owned(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    from bowhead_core.canvas_store import _parse_restore_envelope

    revs = await store.list_revisions(canvas_id=canvas_id)
    projection_state = await store.read_canvas_projection_state(canvas_id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    _, projection_sections = projection_state
    current_kind_by_id = {section.id: section.kind for section in projection_sections}
    revision_sections: list[dict[str, Any]] = []
    revision_projection_inputs: dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    for revision in revs:
        for side, payload in (("prev", revision.prev_payload), ("new", revision.new_payload)):
            section, envelope = _revision_projection_section(
                payload,
                revision_id=revision.id or "malformed",
                side=side,
                current_kind=current_kind_by_id.get(revision.section_id or ""),
            )
            revision_projection_inputs[(revision.id or "", side)] = (section, envelope)
            if section is not None:
                revision_sections.append(section)
    source_refs = await _build_source_refs_for_session(request, canvas.session_id)
    projector = await _viewer_safe_projector(
        request,
        canvas,
        projection_sections,
        source_refs,
        additional_sections=revision_sections,
    )
    # Defensive (dogfood confirmed): out-of-band writes created NULL revision IDs; DTO id:str validation
    # returns 500, breaking the whole drawer for one malformed row. Skip and warn to keep history available.
    malformed = [r for r in revs if not r.id]
    if malformed:
        logger.warning(
            "canvas_revisions_malformed_rows_skipped canvas_id=%s count=%d",
            canvas_id,
            len(malformed),
        )
    return CanvasRevisionsDTO(
        items=[
            CanvasRevisionDTO(
                id=r.id,
                canvasId=canvas_id,
                sectionId=r.section_id,
                op=r.op,
                prevPayload=_project_revision_payload(
                    projector,
                    *revision_projection_inputs[(r.id or "", "prev")],
                ),
                newPayload=_project_revision_payload(
                    projector,
                    *revision_projection_inputs[(r.id or "", "new")],
                ),
                byTurnId=r.by_turn_id,
                createdAt=r.created_at,
                restorable=(r.op == "delete" and _parse_restore_envelope(r.prev_payload) is not None),
                turnOrdinal=r.turn_ordinal,
            )
            for r in revs
            if r.id
        ],
    )


@router.post(
    "/{canvas_id}/revisions/{revision_id}/restore",
    response_model=CanvasRestoreResultDTO,
    dependencies=[Depends(require_not_readonly)],  # §7.5 WRITE
)
async def restore_deleted_revision(
    canvas_id: str,
    revision_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> Any:
    """方案A (report-edit-audit dogfood P1): 按 delete revision 恢复已删 section,
    级联删除的宿主+子图整组回来。404 = revision 不存在/不属于该 canvas/非 delete
    (统一口径不泄漏); 409 = 全员已在场; 422 = 旧格式记录无恢复 envelope。"""
    from bowhead_core.canvas_store import CanvasRestoreUnsupportedError

    await _load_owned(request, store, canvas_id)
    try:
        restored = await store.restore_deleted_sections(
            canvas_id=canvas_id,
            revision_id=revision_id,
        )
    except CanvasRestoreUnsupportedError:
        return JSONResponse(
            status_code=422,
            content={"error": "restore_unsupported_legacy"},
        )
    except ChartExpressionError as exc:
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_chart_expression", "detail": str(exc)}})
    if restored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "revision not found")
    if not restored:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "already_restored"},
        )
    return CanvasRestoreResultDTO(restoredSectionIds=restored)


class PatchBody(BaseModel):
    patch: dict[str, Any] = Field(..., description="shallow-merge into payload")

    @field_validator("patch")
    @classmethod
    def _bound_patch_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        """patch 体积上限（impl review P0-1 的纵深第二层）。

        本路把 body 直接喂给 `bake_payload_strings` → `strip_legacy`（同步、在 event
        loop 上跑）。正则的二次方回溯已单独修掉，但**无界 body 本身**就是放大器：
        同文件 `selection_text` 早有 `max_length=12000` 的 bound 惯例，patch 路是漏网。
        再来一条别的慢路径，这层 bound 就是兜底。

        16MB 是「真实报告段落 + 图表 payload 的宽松上界」——实测最大 prod section
        payload 三个数量级以下；卡这里的只可能是攻击或 bug。
        """
        if len(json.dumps(v, ensure_ascii=False)) > 16 * 1024 * 1024:
            raise ValueError("patch too large (>16MB)")
        return v

    expected_rev: int | None = Field(
        None,
        description="optional optimistic concurrency guard; 409 returns current_rev on mismatch",
    )


class FlattenDesignBody(BaseModel):
    expected_rev: int | None = Field(
        None,
        description="optional optimistic concurrency guard; 409 returns current_rev on mismatch",
    )


@router.patch(
    "/{canvas_id}/sections/{sid}",
    response_model=CanvasSectionPatchResultDTO,
    dependencies=[Depends(require_not_readonly)],  # §7.5 WRITE
)
async def patch_section(
    canvas_id: str,
    sid: str,
    body: PatchBody,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasSectionPatchResultDTO:
    canvas = await _load_owned(request, store, canvas_id)
    await _require_section_in_canvas(store, canvas_id, sid)  # A0-4 IDOR
    projection_state = await store.read_canvas_projection_state(canvas_id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    _, projection_sections = projection_state
    current = next((section for section in projection_sections if section.id == sid), None)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "section not found")
    if body.expected_rev is not None and body.expected_rev != current.rev:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"current_rev": current.rev},
        )
    source_refs = await _build_source_refs_for_session(request, canvas.session_id)
    projector = await _viewer_safe_projector(request, canvas, projection_sections, source_refs)
    # Prime the request-scoped alias reverse map from the exact canonical rev
    # before accepting any aliases returned by the browser.
    projected_current, _ = projector.project([current.to_report_section()])
    try:
        protected_patch = projector.merge_patch(
            current.payload,
            body.patch,
            section_kind=current.kind,
            section_id=current.id,
            allow_label_edit=current.kind == "sources",
        )
    except SourcePatchError as exc:
        conflict = exc.code in {"source_alias_stale", "source_shape_conflict"}
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT if conflict else status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": exc.code,
                    "refresh_required": conflict,
                },
            },
        )
    # citation-audit M1-6① (permanent ingress boundary): revision rollback writes prevPayload unchanged,
    # users paste, AI rewrites; bake retired gen2/[ref:]/[chart:] markers from **any** write path
    # into plain text at the single write entry (prose-key allowlist; leave structural id/sql/code fields intact),
    # or silently revive markers after M2's no-markers-in-storage migration (tech-design §3.3/§3.4).
    from bowhead_core.citation_marks import bake_changed_payload_strings

    baked_patch = bake_changed_payload_strings(
        protected_patch,
        body.patch,
        projected_current[0],
        normalize_unchanged=projector.normalize_unchanged_prose,
    )
    try:
        updated = await store.patch_section(
            section_id=sid,
            patch=baked_patch,
            # Always CAS against the canonical revision used for provenance
            # rehydration, even when the legacy client omitted expected_rev.
            expected_rev=current.rev,
        )
    except CanvasRevConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"current_rev": e.current_rev},
        )
    except ChartExpressionError as exc:
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_chart_expression", "detail": str(exc)}})
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "section not found")
    projected, _ = projector.project([updated.to_report_section()])
    return CanvasSectionPatchResultDTO(section=projected[0], rev=updated.rev)


@router.post(
    "/{canvas_id}/sections/{sid}/flatten",
    response_model=None,
    dependencies=[Depends(require_not_readonly)],  # §7.5 WRITE
)
async def flatten_design_section_endpoint(
    canvas_id: str,
    sid: str,
    body: FlattenDesignBody,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> Any:
    from bowhead_core.design_flags import is_design_v2a_enabled

    if not is_design_v2a_enabled():
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "design_v2a_disabled",
                "detail": "editable design is disabled",
            },
        )
    canvas = await _load_owned(request, store, canvas_id)
    await _require_section_in_canvas(store, canvas_id, sid)  # A0-4 IDOR
    artifact_store = _artifact_store(request)
    try:
        from bowhead_core.design_renderer import (
            DesignFlattenError,
            flatten_design_section,
        )

        result = await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=store,
            canvas_id=canvas_id,
            section_id=sid,
            user_id=_user_id(request),
            session_id=canvas.session_id,
            expected_rev=body.expected_rev,
            created_by_turn=None,
        )
    except DesignFlattenError as exc:
        content: dict[str, Any] = {
            "error": exc.code,
            "detail": exc.message,
        }
        if exc.current_rev is not None:
            content["current_rev"] = exc.current_rev
        return JSONResponse(status_code=exc.status_code, content=content)
    projection_state = await store.read_canvas_projection_state(canvas_id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    _, projection_sections = projection_state
    source_refs = await _build_source_refs_for_session(request, canvas.session_id)
    projector = await _viewer_safe_projector(request, canvas, projection_sections, source_refs)
    projected, _ = projector.project([result.section])
    return {
        "artifact_id": result.artifact_id,
        "url": f"/api/v1/files/{result.artifact_id}",
        "section": projected[0],
        "rev": result.rev,
        "warnings": result.warnings,
    }


class RewriteBody(BaseModel):
    selection_text: str = Field(..., max_length=12000, description="user-selected text to rewrite")
    preset: str | None = Field(None, description="one of: concise/professional/grammar/different_angle")
    custom_prompt: str | None = Field(None, max_length=1000, description="custom rewrite instruction (overrides preset)")
    source_refs: list[dict[str, Any]] | None = Field(None, description="available SourceRef metadata for reference")


async def _resolve_rewrite_router(request: Request) -> Any:
    """A0-1 (report-edit-audit P0-3): rewrite 走**团队作用域** router，与 chat /send 同一条
    解析路径（team_router_registry + get_team_llm_config_with_version + legacy→v2 迁移）。

    绝不回退 `app.state.router`：它是进程级装配占位，默认 RefusalAdapter（批 2-a §3.2：
    选型已与 BOWHEAD_ENV 解耦，显式 BOWHEAD_ASSEMBLY_ROUTER=mock 才给 echo）——
    没有 `narrative` 绑定（此前 prod 上改写因此 100% 挂 `KeyError: No binding ...`），
    即使哪天补上绑定也只会**静默返回 mock 文本**。

    binding 解析放在返回 StreamingResponse **之前**：配置类错误必须以 HTTP 状态码出去
    （409/503 + code），而不是 200 + SSE error。流内只剩真正的运行期错误。
    """
    from bowhead_gateway.routes.auth import resolve_user_or_stub

    _, team_id = await resolve_user_or_stub(request)
    llm_router: Any = None
    if team_id:
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        resolved = await resolve_team_model_runtime(
            request.app.state,
            team_id,
            entrypoint="tool_continuation",
        )
        if resolved.available:
            llm_router = resolved.router
    if llm_router is None:
        # Same code as chat /send's team_model_unconfigured gate. credential-readiness D5:
        # code-only envelope; config/i18n/errors/ is the sole copy source (error handler adds
        # message_localized via Accept-Language). Previous inline Chinese bypassed the catalog and was not bilingual.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "team_model_unconfigured"}},
        )
    try:
        llm_router.resolve("narrative")
    except KeyError as exc:
        logger.warning("rewrite.binding_missing team=%s err=%s", team_id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "rewrite_binding_missing"}},  # Localized from config/i18n/errors.
        ) from exc
    return llm_router


@router.post("/{canvas_id}/sections/{sid}/rewrite", dependencies=[Depends(require_not_readonly)])  # §7.5 WRITE
async def rewrite_section(
    canvas_id: str,
    sid: str,
    body: RewriteBody,
    request: Request,
    store: CanvasStore = Depends(_store),
):
    """P3.4 AI rewrite — 走团队 LLM Router 的 narrative 绑定流式透传 SSE.

    response: text/event-stream (Tiptap canvas-store 4 reducers / chunk handler 已挂).
    """
    from bowhead_core.canvas_rewrite import (
        stream_rewrite,
        validate_rewrite_request,
    )
    from fastapi.responses import StreamingResponse

    await _load_owned(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.EXECUTE,
    )
    if not (body.selection_text or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "selection_text required")
    try:
        validate_rewrite_request(body.preset, body.custom_prompt)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    llm_router = await _resolve_rewrite_router(request)

    return StreamingResponse(
        stream_rewrite(
            router=llm_router,
            selection_text=body.selection_text,
            preset=body.preset,
            custom_prompt=body.custom_prompt,
            source_refs=body.source_refs,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_REPORT_LIST_DEFAULT_LIMIT = 50
_REPORT_LIST_MAX_LIMIT = 200


_REPORT_LIST_AUTH_CONCURRENCY = 8


def _report_list_filter_key(*, scope: str, workspace_id: str | None, q: str | None, include_empty: bool, sort: str) -> str:
    """Short digest of the filters a cursor was issued under; a cursor never crosses filters."""
    raw = json.dumps([scope, workspace_id or "", (q or "").strip(), bool(include_empty), sort], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _encode_report_list_cursor(sort: str, filter_key: str, values: list[Any]) -> str:
    payload = json.dumps({"s": sort, "f": filter_key, "v": values}, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_report_list_cursor(cursor: str, sort: str, filter_key: str) -> list[Any]:
    """Return the sort-key values the cursor points after.

    A cursor is only valid for the sort and filters it was issued with; anything else
    (including malformed or out-of-range values) is a 400 rather than a silently
    restarted list or a database error.
    """
    try:
        if len(cursor) > 4096:
            raise ValueError("cursor too long")
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        values = payload["v"]
        valid = payload["s"] == sort and payload["f"] == filter_key and report_list_cursor_values_valid(values, sort)
    except (ValueError, TypeError, KeyError, OverflowError):
        valid = False
    if not valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"error": {"code": "invalid_report_cursor"}})
    return list(values)


@router.get("")
async def list_reports(
    request: Request,
    workspace_id: str | None = None,
    q: str | None = None,
    sort: str = "updated_desc",
    limit: int = _REPORT_LIST_DEFAULT_LIMIT,
    scope: str = "mine",
    include_empty: bool = True,
    cursor: str | None = None,
    store: CanvasStore = Depends(_store),
) -> dict:
    """P4.2: list 用户跨 session 所有 canvas (reports library).

    content-permissions S5: ``scope=shared`` → "分享给我的"分区（report grants 反查
    session_id 集 → 同一套 enrich）;条目带 ``shared_by``（owner）。默认 mine 行为不变。
    ``include_empty=false`` 在 SQL 的 ORDER BY / LIMIT 前过滤零可见 section；默认
    True 保持既有 API 调用方兼容，报告库默认态显式传 False。

    Pagination: on the first page (no ``cursor``) ``total`` is the number of reports
    matching the same filters and the same Space visibility as ``items`` (not the page
    size); on cursor pages ``total`` is ``null`` and clients keep the first-page value.
    ``next_cursor`` is an opaque keyset cursor bound to this sort and these filters, or
    ``null`` on the last page (``has_more`` mirrors it). Clients that omit ``cursor``
    receive the first page exactly as before.

    ``empty_total`` (first page with ``include_empty=false`` only, otherwise ``null``)
    counts the reports hidden because they have no visible section, over the same
    filters and readable Spaces, so the library can label its "show empty" toggle
    without a second request.
    """
    user_id = _user_id(request)
    sort = normalize_report_list_sort(sort)
    page_size = max(1, min(_REPORT_LIST_MAX_LIMIT, limit))
    filter_key = _report_list_filter_key(scope=scope, workspace_id=workspace_id, q=q, include_empty=include_empty, sort=sort)
    after = _decode_report_list_cursor(cursor, sort, filter_key) if cursor else None
    # scope=shared entries are shared in, inherently visible without space membership -> batch 4 (§4.4).
    enforce_space_membership = scope != "shared"
    if workspace_id:
        await _require_canvas_lifecycle(
            request,
            workspace_id=workspace_id,
            user_id=user_id,
            operation=SpaceOperation.READ_HISTORY,
            enforce_space_membership=enforce_space_membership,
        )
    session_ids: list[str] | None = None
    exclude_user_id: str | None = None
    if scope == "shared":
        session_ids = sorted(await _shared_report_session_ids(request, user_id))
        exclude_user_id = user_id
    filters: dict[str, Any] = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "q": q,
        "session_ids": session_ids,
        "min_visible_sections": 0 if include_empty else 1,
        "exclude_user_id": exclude_user_id,
    }
    # Space visibility is decided once per workspace_id with the same predicate as
    # report history reads; both the total and every page use the resulting allowlist,
    # so neither can include rows from a Space the caller cannot read. A requested
    # workspace_id was authorized above, and rows are restricted to exactly that id.
    if workspace_id:
        readable = {workspace_id}
    else:
        candidates = await store.list_workspace_ids_for_user(
            user_id=user_id,
            session_ids=session_ids,
            exclude_user_id=exclude_user_id,
        )
        readable = await _readable_report_workspaces(
            request,
            user_id,
            candidates,
            enforce_space_membership=enforce_space_membership,
        )
    total: int | None = None
    empty_total: int | None = None
    if after is None:
        counts = await store.count_by_workspace_for_user(**filters)
        total = sum(n for ws, n in counts.items() if ws in readable)
        if not include_empty:
            all_counts = await store.count_by_workspace_for_user(**{**filters, "min_visible_sections": 0})
            empty_total = max(0, sum(n for ws, n in all_counts.items() if ws in readable) - total)
    rows = await store.list_for_user(
        **filters,
        sort=sort,
        limit=page_size + 1,
        workspace_ids=sorted(readable),
        after=after,
    )
    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        next_cursor = _encode_report_list_cursor(sort, filter_key, report_list_cursor_values(rows[-1], sort))
    items = rows
    if scope == "shared":
        # 0708 ⑤: display sharer name, not raw usr_* ID. Best effort; failed resolution falls back to ID.
        owner_names: dict[str, str] = {}
        try:
            us = getattr(request.app.state, "user_store", None)
            if us is not None:
                owner_ids = [str(it.get("user_id")) for it in items if it.get("user_id")]
                owner_names = await us.get_users_by_ids(owner_ids)
        except Exception:
            owner_names = {}
        for it in items:
            oid = it.get("user_id")
            it["shared_by"] = owner_names.get(oid, oid)
        await _annotate_workspace_names(request, user_id, items)
        return {
            "items": items,
            "total": total,
            "empty_total": empty_total,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }
    await _annotate_workspace_names(request, user_id, items)
    await _annotate_owned_audience(request, user_id, items)
    return {
        "items": items,
        "total": total,
        "empty_total": empty_total,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


async def _readable_report_workspaces(
    request: Request,
    user_id: str,
    workspace_ids: Iterable[str],
    *,
    enforce_space_membership: bool = True,
) -> set[str]:
    """Return the subset of ``workspace_ids`` whose report history the caller may read.

    The user row is loaded once and Space lookups share one request-level cache;
    per-Space checks run concurrently with bounded parallelism and apply exactly
    ``_require_canvas_lifecycle``. A 403/404 hides the Space; any other error,
    including other HTTP statuses, is re-raised as itself (same rule as the session list).
    """

    unique = sorted(set(workspace_ids))
    if not unique:
        return set()
    user: Any = _UNSET_USER
    if has_workspace_access_source(request):
        user = await request.app.state.user_store.get_user(user_id)
        if len(unique) > 1:
            request = request_with_cached_access(request, team_id=getattr(user, "team_id", None))
    readable: set[str] = set()

    async def _check(workspace_id: str) -> None:
        try:
            await _require_canvas_lifecycle(
                request,
                workspace_id=workspace_id,
                user_id=user_id,
                operation=SpaceOperation.READ_HISTORY,
                enforce_space_membership=enforce_space_membership,
                user=user,
            )
        except HTTPException as exc:
            # A denial hides the Space; anything else (503 authz_lookup_failed, 401
            # user_not_active) fails the request with its status: a silently short
            # list and total would be worse than a visible error.
            if exc.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}:
                return
            raise
        readable.add(workspace_id)

    await run_bounded(unique, _check, limit=_REPORT_LIST_AUTH_CONCURRENCY)
    return readable


async def _filter_report_history_items(
    request: Request,
    user_id: str,
    items: list[dict[str, Any]],
    *,
    enforce_space_membership: bool = True,
) -> list[dict[str, Any]]:
    """Filter mixed-Space lists without trusting a single current-Space header."""

    readable = await _readable_report_workspaces(
        request,
        user_id,
        (str(item.get("workspace_id") or "") for item in items),
        enforce_space_membership=enforce_space_membership,
    )
    return [item for item in items if str(item.get("workspace_id") or "") in readable]


async def _annotate_workspace_names(
    request: Request,
    user_id: str,
    items: list[dict[str, Any]],
) -> None:
    """Attach workspace display names to report rows.

    Best-effort: report listing should not fail if auth/workspace stores are absent in
    local tests or during partial deployments. Match both canonical ws_* ids and legacy
    datarc workspace ids because older canvases may carry either value.
    """
    if not items:
        return
    try:
        user_store = getattr(request.app.state, "user_store", None)
        if user_store is None:
            return
        rows = await user_store.list_workspaces_for_user(user_id)
        names: dict[str, str] = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            workspace_id = row.get("workspace_id")
            datarc_workspace_id = row.get("datarc_workspace_id")
            if workspace_id:
                names[str(workspace_id)] = name
            if datarc_workspace_id:
                names[str(datarc_workspace_id)] = name
        for item in items:
            workspace_id = item.get("workspace_id")
            if workspace_id and (name := names.get(str(workspace_id))):
                item["workspace_name"] = name
    except Exception:
        return


async def _annotate_owned_audience(request: Request, user_id: str, items: list) -> None:
    """content-permissions S7: 给"我创建的"报告标 audience(private/custom/team_wide)
    供前端徽标。一次 list_resource_grants(kind=report) 批量取本 team 全部 report grant,
    按 session_id 分组 derive_audience。best-effort,失败/未配置 → 不标(前端隐徽标)。"""
    if not items:
        return
    try:
        from bowhead_core.share_service import derive_audience

        access = getattr(request.app.state, "access_control_store", None)
        user_store = getattr(request.app.state, "user_store", None)
        if access is None or user_store is None:
            return
        viewer = await user_store.get_user(user_id)
        if viewer is None or not viewer.team_id:
            return
        grants = await access.list_resource_grants(viewer.team_id, resource_kind="report")
        by_sid: dict[str, list] = {}
        for g in grants:
            if g.effect == "allow":
                by_sid.setdefault(g.resource_id, []).append(g)
        for it in items:
            sid = it.get("session_id", "")
            sid_grants = by_sid.get(sid, [])
            it["audience"] = derive_audience(sid_grants, team_id=viewer.team_id)
            it["share_count"] = sum(1 for g in sid_grants if g.principal_type in ("user", "group"))
    except Exception:
        return


async def _shared_report_session_ids(request: Request, user_id: str) -> frozenset[str]:
    """report grants 反查（grants-by-principal,tech-design §4.3）。失败/未配置 → ∅。"""
    try:
        from bowhead_core.share_service import grants_for_viewer

        access = getattr(request.app.state, "access_control_store", None)
        user_store = getattr(request.app.state, "user_store", None)
        if access is None or user_store is None:
            return frozenset()
        viewer = await user_store.get_user(user_id)
        if viewer is None or not viewer.team_id:
            return frozenset()
        return await grants_for_viewer(
            access,
            team_id=viewer.team_id,
            user_id=user_id,
            kind="report",
        )
    except Exception:
        return frozenset()


@router.post("/{canvas_id}/clear", response_model=DeletedDTO, dependencies=[Depends(require_not_readonly)])  # §7.5 WRITE
async def clear_canvas(
    canvas_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> DeletedDTO:
    """P4.1: 清空 canvas sections (保留 canvas 壳)."""
    await _load_owned(request, store, canvas_id)
    n = await store.clear_sections(canvas_id=canvas_id)
    return DeletedDTO(deleted=f"{canvas_id}:{n}_sections")


async def _cascade_deleted_report_access(
    request: Request,
    *,
    owner_user_id: str,
    session_id: str,
    committed_team_id: str | None = None,
) -> None:
    """Idempotent grant/request cleanup, including lifecycle ack-loss retries."""

    access = getattr(request.app.state, "access_control_store", None)
    user_store = getattr(request.app.state, "user_store", None)
    if access is None:
        return
    team_id = committed_team_id
    if team_id is None:
        if user_store is None:
            return
        owner = await user_store.get_user(owner_user_id)
        team_id = owner.team_id if owner is not None else None
    if not team_id:
        return
    await access.revoke_grants_for_resource(
        team_id,
        "report",
        session_id,
    )
    from bowhead_gateway.routes.access_requests import (
        cascade_requests_for_resource,
    )

    await cascade_requests_for_resource(
        request.app,
        team_id=team_id,
        kind="report",
        resource_id=session_id,
        decided_by=owner_user_id,
        gone=True,
    )


@router.delete("/{canvas_id}", response_model=DeletedDTO, dependencies=[Depends(require_not_readonly)])  # §7.5 WRITE
async def delete_canvas(
    canvas_id: str,
    request: Request,
    delete_session: bool = Query(default=False, description="feedback-0713 R1: 同时软删来源会话（默认关）"),
    store: CanvasStore = Depends(_store),
) -> DeletedDTO:
    """P4.1: 整 canvas + sections + revisions 删.

    feedback-0713 R1: ``delete_session=1`` 时联动**软删**来源会话（1:1 派生,
    canvases.session_id）——只收敛会话列表面, turns/trace 保留可审计; 定时任务
    scheduled_job 与 run 历史是独立表, 口径不受影响。只软删 canvas 自己指向的
    那个会话（不接受调用方传 session id）, 且 session.user_id 必须与 canvas
    owner 一致（防孤儿/错配数据下的越权抹除）。
    """
    lifecycle = getattr(request.app.state, "session_lifecycle_coordinator", None)
    actor_id = _user_id(request)
    lifecycle_request_id = f"canvas-delete:{canvas_id}"
    if delete_session and lifecycle is not None:
        try:
            committed = await lifecycle.canvas_delete_replay(
                canvas_id=canvas_id,
                actor_audit_ref=actor_id,
            )
        except Exception as exc:
            from bowhead_core.session_lifecycle import SessionLifecycleError

            from bowhead_gateway.session_lifecycle import (
                session_lifecycle_http_error,
            )

            if isinstance(exc, SessionLifecycleError):
                raise session_lifecycle_http_error(exc) from exc
            raise
        if committed is not None:
            await store.notify_canvas_deleted(canvas_id)
            try:
                await _cascade_deleted_report_access(
                    request,
                    owner_user_id=committed.actor_audit_ref,
                    session_id=committed.session_id,
                    committed_team_id=committed.team_id,
                )
            except Exception:  # retry remains repairable through the ledger
                logger.warning(
                    "delete_canvas.replay_cleanup_failed canvas=%s operation=%s",
                    canvas_id,
                    committed.operation_id,
                    exc_info=True,
                )
            return DeletedDTO(deleted=canvas_id)

    canvas = await _load_owned(request, store, canvas_id)
    canvas_deleted_by_lifecycle = False
    if delete_session and canvas.session_id:
        if lifecycle is not None:
            try:
                result = await lifecycle.soft_delete(
                    session_id=canvas.session_id,
                    actor_audit_ref=canvas.user_id,
                    request_id=lifecycle_request_id,
                    expected_user_id=canvas.user_id,
                    linked_canvas_id=canvas_id,
                )
            except Exception as exc:
                from bowhead_core.session_lifecycle import SessionLifecycleError

                from bowhead_gateway.session_lifecycle import (
                    session_lifecycle_http_error,
                )

                if isinstance(exc, SessionLifecycleError):
                    raise session_lifecycle_http_error(exc) from exc
                raise
            canvas_deleted_by_lifecycle = result.protocol_version == 1
            if canvas_deleted_by_lifecycle:
                await store.notify_canvas_deleted(canvas_id)
        else:
            ses_store = getattr(request.app.state, "session_store", None)
            if (
                getattr(
                    request.app.state,
                    "legacy_session_lifecycle_allowed",
                    None,
                )
                is not True
                or ses_store is None
            ):
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": {
                            "code": "terminal_protocol_writer_not_ready",
                        }
                    },
                )
            ses = await ses_store.get_session(canvas.session_id)
            if ses is not None and ses.user_id == canvas.user_id:
                from bowhead_core.session_store import SessionProtocolManagedError

                try:
                    await ses_store.soft_delete_session(canvas.session_id)
                except SessionProtocolManagedError as exc:
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": {
                                "code": "terminal_protocol_writer_not_ready",
                            }
                        },
                    ) from exc
            elif ses is not None:
                logger.warning(
                    "delete_canvas.session_owner_mismatch canvas=%s session=%s — skip soft delete",
                    canvas_id,
                    canvas.session_id,
                )
    if not canvas_deleted_by_lifecycle:
        ok = await store.delete_canvas(canvas_id=canvas_id)
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    # content-permissions S5 cascade (PRD §4.7): report deletion revokes its report grants,
    # clearing Shared-with-me. Best effort; requires owner team resolution via user_store.
    with contextlib.suppress(Exception):
        await _cascade_deleted_report_access(
            request,
            owner_user_id=canvas.user_id,
            session_id=canvas.session_id,
        )
    return DeletedDTO(deleted=canvas_id)


class SectionDeletedDTO(BaseModel):
    """A2: 删除结果 —— `deletedSectionIds` 含宿主 + 级联删掉的内联视觉块。"""

    deleted: str
    deletedSectionIds: list[str]  # noqa: N815 — FE camelCase contract, matching other canvas DTOs.


@router.delete(
    "/{canvas_id}/sections/{sid}",
    response_model=SectionDeletedDTO,
    dependencies=[Depends(require_not_readonly)],
)  # §7.5 WRITE
async def delete_section(
    canvas_id: str,
    sid: str,
    request: Request,
    expected_rev: int | None = Query(None, description="并发防护: 与当前 rev 不符返 409"),
    cascade: bool = Query(False, description="A2: 连带删除宿主独占的内联 chart/image"),
    store: CanvasStore = Depends(_store),
) -> Any:
    # A0-4 (report-edit-audit): store validates sid∈canvas_id. Previously only canvas ownership was checked,
    # letting any canvas owner delete others' sections (IDOR). Ownership mismatch -> 404.
    await _load_owned(request, store, canvas_id)
    # canvas-elements source consolidation: source section is the whole document's traceability carrier ([N] is canvas-scoped).
    # Deletion silences citations in **every turn**; rebuilding from [1] next turn then makes historical [N] refer to the wrong source.
    # Share is_protected_sources_section with agents (two deletion implementations).
    _target = next((s for s in await store.list_sections(canvas_id, include_hidden=True) if s.id == sid), None)
    if _target is not None and is_protected_sources_section(_target.kind):
        raise HTTPException(status.HTTP_409_CONFLICT, "数据来源块不可删除")
    try:
        deleted = await store.delete_section(
            section_id=sid,
            canvas_id=canvas_id,
            expected_rev=expected_rev,
            cascade=cascade,
        )
    except CanvasRevConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"current_rev": e.current_rev},
        )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "section not found")
    # A2: return actual cascade-deletion list (FE uses real server counts, no local estimates).
    return JSONResponse(content={"deleted": sid, "deletedSectionIds": deleted})


# ──────────────────────────────────────────────────────
# B-7: Section split (W1-3 frontend client wired).
# ──────────────────────────────────────────────────────


class SplitBody(BaseModel):
    offset: int = Field(..., ge=0, description="markdown 字符 offset, 切两段")


class SplitResultDTO(BaseModel):
    newSectionId: str
    updated: dict[str, Any]
    new: dict[str, Any]


@router.post(
    "/{canvas_id}/sections/{sid}/split",
    response_model=SplitResultDTO,
    dependencies=[Depends(require_not_readonly)],  # §7.5 WRITE
)
async def split_section(
    canvas_id: str,
    sid: str,
    body: SplitBody,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> SplitResultDTO:
    """
    把 text-based section (narrative/deck/callout) 在 markdown char offset 处分两段.
    400 if section 不是 text-based / 404 if 不存在.
    """
    from bowhead_core.canvas_store import CanvasSplitDegenerateError

    canvas = await _load_owned(request, store, canvas_id)
    await _require_section_in_canvas(store, canvas_id, sid)  # A0-4 IDOR
    try:
        pair = await store.split_section(section_id=sid, offset=body.offset)
    except CanvasSplitDegenerateError as e:
        # affordance r3 §5.2: reject degenerate boundary-offset splits that would create empty shells.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"offset at markdown boundary (offset={e.offset}, length={e.length}); both halves must be non-empty",
        ) from e
    if pair is None:
        # 404: section absent OR kind is not text-based.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "section not found or kind not splittable (must be narrative/deck/callout)",
        )
    updated, new_sec = pair
    projection_state = await store.read_canvas_projection_state(canvas_id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    _, projection_sections = projection_state
    source_refs = await _build_source_refs_for_session(request, canvas.session_id)
    projector = await _viewer_safe_projector(request, canvas, projection_sections, source_refs)
    projected, _ = projector.project([updated.to_report_section(), new_sec.to_report_section()])
    return SplitResultDTO(
        newSectionId=new_sec.id,
        updated=projected[0],
        new=projected[1],
    )


class MergeResultDTO(BaseModel):
    survivorSectionId: str
    deletedSectionId: str
    updated: dict[str, Any]


@router.post(
    "/{canvas_id}/sections/{sid}/merge",
    response_model=MergeResultDTO,
    dependencies=[Depends(require_not_readonly)],  # §7.5 WRITE
)
async def merge_section_with_prev(
    canvas_id: str,
    sid: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> MergeResultDTO:
    """W12-6: 把 sid 与紧前一个同 kind text-based section 单事务合并.

    - sid 必须是 narrative / deck / callout
    - 必须有前一个 section 且同 kind
    - prev.markdown ← prev.markdown + "\\n\\n" + sid.markdown
    - prev.source_refs ← dedupe union
    - DELETE sid + UPDATE prev + 2 revision (patch + delete) 单事务

    400 if section 不存在 / 无 prev / kind 不匹配.
    """
    canvas = await _load_owned(request, store, canvas_id)
    await _require_section_in_canvas(store, canvas_id, sid)  # A0-4 IDOR
    survivor = await store.merge_with_prev(section_id=sid)
    if survivor is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "merge failed: section not found, has no prev, or prev kind mismatch (must be same text-based kind)",
        )
    projection_state = await store.read_canvas_projection_state(canvas_id)
    if projection_state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    _, projection_sections = projection_state
    source_refs = await _build_source_refs_for_session(request, canvas.session_id)
    projector = await _viewer_safe_projector(request, canvas, projection_sections, source_refs)
    projected, _ = projector.project([survivor.to_report_section()])
    return MergeResultDTO(
        survivorSectionId=survivor.id,
        deletedSectionId=sid,
        updated=projected[0],
    )


class ReorderBody(BaseModel):
    order: list[str] = Field(..., description="section ids in new order")


@router.post("/{canvas_id}/reorder", response_model=CanvasOrderResultDTO, dependencies=[Depends(require_not_readonly)])  # §7.5 WRITE
async def reorder(
    canvas_id: str,
    body: ReorderBody,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasOrderResultDTO:
    await _load_owned(request, store, canvas_id)
    # A3 review: return **actual persisted order**, not request order. Reorder completes and resequences
    # (unlisted IDs append in original relative order); returning request order implies storage contains only those sections.
    final_order = await store.reorder_sections(canvas_id=canvas_id, ordered_ids=body.order)
    return CanvasOrderResultDTO(ok=True, order=final_order)


class TitleBody(BaseModel):
    title: str


@router.post("/{canvas_id}/title", response_model=CanvasTitleResultDTO, dependencies=[Depends(require_not_readonly)])  # §7.5 WRITE
async def set_title(
    canvas_id: str,
    body: TitleBody,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasTitleResultDTO:
    await _load_owned(request, store, canvas_id)
    await store.set_title(canvas_id, body.title)
    return CanvasTitleResultDTO(ok=True, title=body.title)


class StatusBody(BaseModel):
    status: str  # R2: active | verified | stale | archived


@router.get("/{canvas_id}/status", response_model=CanvasStatusMetaDTO)
async def get_canvas_status(
    canvas_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasStatusMetaDTO:
    """R2: 报告头 chip 用的轻量状态读(不拉 sections)."""
    from bowhead_core.canvas_store import _effective_status

    canvas = await _load_owned(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    quar = await store.count_quarantined(canvas_id)
    return CanvasStatusMetaDTO(
        status=canvas.status,
        effectiveStatus=_effective_status(canvas.status, quar),
        quarantinedCount=quar,
    )


@router.post("/{canvas_id}/status", response_model=CanvasStatusResultDTO, dependencies=[Depends(require_not_readonly)])  # §7.5 WRITE
async def set_canvas_status(
    canvas_id: str,
    body: StatusBody,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> CanvasStatusResultDTO:
    """R2: owner 改报告生命周期状态(archive/restore/mark-verified/mark-stale)."""
    from bowhead_core.canvas_store import CANVAS_STATUSES

    await _load_owned(request, store, canvas_id)
    if body.status not in CANVAS_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"invalid status {body.status!r}; must be one of {list(CANVAS_STATUSES)}",
        )
    ok = await store.set_status(canvas_id, body.status)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "canvas not found")
    return CanvasStatusResultDTO(ok=True, status=body.status)


class ExportBody(BaseModel):
    fmt: str = Field("pdf", description="pdf | pptx | xlsx | docx")
    template_artifact_id: str | None = Field(
        default=None,
        description=(
            "(仅 pptx clone) 用户自家 PPT 模板 artifact_id (art_xxx). "
            "只有 mode=clone 且 FEATURE_CANVAS_PPTX_CLONE=1 时消费; "
            "default 忽略，auto 携带时拒绝。"
        ),
    )
    # W0-A: auto/default consistently use legacy canvas-pptx; only explicit clone may clone a template.
    mode: str = Field(
        "auto",
        pattern="^(auto|clone|default)$",
        description="pptx 渲染模式; auto/default=标准 renderer; clone=显式模板克隆（feature flag 默认关闭）",
    )


class ExportCreateV2Body(BaseModel):
    """Validated structurally here; semantic failures use the stable V2 envelope."""

    model_config = ConfigDict(extra="allow")

    format: Any = None
    profile: Any = None
    # ``None`` is intentional: a Pydantic default must not masquerade as an
    # explicit user choice and truncate the locale fallback chain.
    locale: Any = None
    timezone: Any = "Asia/Singapore"
    theme: Any = Field(default_factory=lambda: {"kind": "default"})
    consistency: Any = None
    expected_projection_token: Any = None
    expected_surface_token: Any = None
    client_surface_receipt_sha256: Any = None
    client_visual_receipts: Any = None
    options: Any = Field(default_factory=dict)


_EXPORT_PARITY_CONTRACTS = {
    ("pdf", "full_report"): "full_content_v1",
    ("pptx", "full_report"): "full_content_v1",
    ("pptx", "executive_summary"): "summary_policy_v1",
    ("xlsx", "data_workbook"): "data_parity_v1",
}
_EXPORT_V2_BODY_KEYS = frozenset(
    {
        "format",
        "profile",
        "locale",
        "timezone",
        "theme",
        "consistency",
        "expected_projection_token",
        "expected_surface_token",
        "client_surface_receipt_sha256",
        "client_visual_receipts",
        "options",
    }
)


def _job_dto(job: Any, *, copy_contract_version: int | None = None) -> ExportJobDTO:
    return ExportJobDTO(
        jobId=job.id,
        canvasId=job.canvas_id,
        contractVersion=copy_contract_version if copy_contract_version is not None else job.contract_version,
        fmt=job.fmt,
        status=job.status,
        url=job.url,
        artifactId=job.artifact_id,
        filename=job.filename,
        size=job.size,
        error=job.error,
        createdAt=job.created_at,
        updatedAt=job.updated_at,
    )


def _job_v2_dto(job: Any, *, copy_contract_version: int | None = None) -> ExportJobV2DTO:
    progress: dict[str, Any] | None = None
    if job.progress_json:
        try:
            parsed = json.loads(job.progress_json)
            if isinstance(parsed, dict):
                progress = parsed
        except (TypeError, ValueError):
            logger.warning("canvas.export.v2.invalid_progress_json job_id=%s", job.id)
    download_url: str | None = None
    if job.status == "done" and job.quality_status in {"clean", "warnings"}:
        if job.artifact_id:
            download_url = f"/api/v1/files/{job.artifact_id}"
        elif job.url:
            download_url = job.url
    return ExportJobV2DTO(
        job_id=job.id,
        canvas_id=job.canvas_id,
        contract_version=copy_contract_version if copy_contract_version is not None else job.contract_version,
        format=job.fmt,
        profile=job.profile,
        parity_contract=job.parity_contract,
        surface_token=job.surface_token,
        status=job.status,
        phase=job.phase,
        quality_status=job.quality_status,
        progress=progress,
        warning_count=job.warning_count,
        snapshot_id=job.snapshot_id,
        artifact_id=job.artifact_id,
        manifest_artifact_id=job.manifest_artifact_id,
        download_url=download_url,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def _classify_copy_versions(request: Request, jobs: list[Any]) -> dict[str, int]:
    from bowhead_core.export_copy_contract import classify_export_copy_versions

    baseline = {str(job.id): int(job.contract_version) if job.contract_version in {1, 2} else 1 for job in jobs}
    if not any(job.status == "done" and isinstance(job.artifact_id, str) and isinstance(job.manifest_artifact_id, str) for job in jobs):
        return baseline
    return await classify_export_copy_versions(
        jobs,
        artifact_store=_artifact_store(request),
    )


def _export_v2_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
    context: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "context": context or {},
        },
        headers=headers,
    )


async def _load_owned_v2(
    request: Request,
    store: CanvasStore,
    canvas_id: str,
    *,
    operation: SpaceOperation = SpaceOperation.EXECUTE,
) -> Any:
    canvas = await store.get_canvas(canvas_id)
    if canvas is None:
        raise _export_v2_error(
            status.HTTP_404_NOT_FOUND,
            "canvas_not_found",
            "报告不存在",
            retryable=False,
        )
    if canvas.user_id != _user_id(request):
        raise _export_v2_error(
            status.HTTP_403_FORBIDDEN,
            "canvas_not_owned",
            "只有报告所有者可以导出",
            retryable=False,
        )
    await _require_canvas_lifecycle(
        request,
        workspace_id=canvas.workspace_id or "",
        user_id=canvas.user_id,
        operation=operation,
    )
    return canvas


def _validate_export_v2_body(
    body: ExportCreateV2Body,
) -> tuple[
    str,
    str,
    str,
    object,
    str,
    dict[str, Any],
    dict[str, Any],
    tuple[dict[str, Any], ...],
    int,
]:
    if body.model_extra or not _EXPORT_V2_BODY_KEYS.issuperset(body.model_fields_set):
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "导出请求包含不支持的字段",
            retryable=False,
        )
    fmt = body.format.lower() if isinstance(body.format, str) else ""
    profile = body.profile.lower() if isinstance(body.profile, str) else ""
    parity_contract = _EXPORT_PARITY_CONTRACTS.get((fmt, profile))
    if parity_contract is None:
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_format_profile",
            "该文件格式与导出范围组合暂不支持",
            retryable=False,
        )
    if not isinstance(body.timezone, str):
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_locale",
            "语言或时区无效",
            retryable=False,
        )
    # Directory names ("America") would raise IsADirectoryError from ZoneInfo: validate against the zone list.
    if load_iana_time_zone(body.timezone) is None:
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_locale",
            "语言或时区无效",
            retryable=False,
        )
    if body.consistency != "require_projection":
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "当前导出入口必须校验已准备的报告版本",
            retryable=False,
        )
    if not all(
        _is_sha256_token(value)
        for value in (
            body.expected_projection_token,
            body.expected_surface_token,
            body.client_surface_receipt_sha256,
        )
    ):
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "缺少有效的报告版本或页面内容凭据",
            retryable=False,
        )
    try:
        if isinstance(body.theme, dict) and body.theme.get("kind") == "chart-style-freshness":
            theme = parse_chart_style_export_freshness_v2(body.theme)
            chart_style_contract = 2
        else:
            if isinstance(body.theme, dict) and body.theme.get("schemaVersion") == 2:
                raise ChartStyleExportError("clients cannot submit a resolved V2 export theme")
            theme = parse_chart_style_export_theme(body.theme)
            if theme.get("schemaVersion") != 1:
                raise ChartStyleExportError("clients cannot submit a resolved V2 export theme")
            chart_style_contract = 1
    except ChartStyleExportError as exc:
        raise _export_v2_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "template_profile_not_supported",
            "图表风格快照无效，请刷新后重试",
            retryable=False,
        ) from exc
    visual_receipts = _validate_client_visual_receipt_shape(
        body.client_visual_receipts,
        chart_style_contract=chart_style_contract,
    )
    if not isinstance(body.options, dict) or "include_history" in body.options:
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "当前版本不支持该导出选项",
            retryable=False,
        )
    if set(body.options) - {"include_appendix", "chart_hidden_series"}:
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "当前版本不支持该导出选项",
            retryable=False,
        )
    include_appendix = body.options.get("include_appendix", True)
    if not isinstance(include_appendix, bool):
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "include_appendix 必须是布尔值",
            retryable=False,
        )
    options = _export_v2_options(
        include_appendix,
        body.options.get("chart_hidden_series"),
        exported_section_ids={receipt["section_id"] for receipt in visual_receipts},
    )
    return (
        fmt,
        profile,
        parity_contract,
        body.locale,
        body.timezone,
        theme,
        options,
        visual_receipts,
        chart_style_contract,
    )


def _export_v2_options(
    include_appendix: bool,
    chart_hidden_series: Any,
    *,
    exported_section_ids: set[str],
) -> dict[str, Any]:
    """Normalized export options. ``chart_hidden_series`` is present only when something is hidden, so exports that
    hide nothing keep their request hash (and idempotency key behaviour) exactly as before."""
    options: dict[str, Any] = {"include_appendix": include_appendix}
    hidden = _validate_chart_hidden_series(chart_hidden_series, exported_section_ids=exported_section_ids)
    if hidden:
        options["chart_hidden_series"] = hidden
    return options


# A report rarely has more than a handful of multi-series charts with something hidden; these caps bound the request
# (and the snapshot that stores it) far below the receipt limits.
_MAX_HIDDEN_SERIES_SECTIONS = 50
_MAX_HIDDEN_SERIES_PER_CHART = 32
_MAX_HIDDEN_SERIES_TOTAL = 200
_MAX_HIDDEN_SERIES_BYTES = 8 * 1024
_MAX_SERIES_KEY_CHARS = 128


def _validate_chart_hidden_series(value: Any, *, exported_section_ids: set[str]) -> dict[str, list[str]]:
    """Series the exporting user hid in each report chart's legend: ``{section_id: [series key, ...]}``.

    Per-viewer screen state, so it travels in the export request rather than the section: the export draws those charts
    the way this user sees them and nobody else's export changes. ``export_ir`` folds it into the chart's visual payload
    (``hidden_series``), which is part of the visual identity hash. Normalized (sorted, de-duplicated, empty entries
    dropped) so the same screen state always yields the same request hash and visual identity. Only sections that this
    export actually renders as exact visuals (``client_visual_receipts``) are kept; anything else is dropped.
    """
    if value is None:
        return {}
    invalid = _export_v2_error(
        status.HTTP_400_BAD_REQUEST,
        "invalid_options",
        "chart_hidden_series 格式无效",
        retryable=False,
    )
    if not isinstance(value, dict) or len(value) > _MAX_HIDDEN_SERIES_SECTIONS:
        raise invalid
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise invalid from None
    if size > _MAX_HIDDEN_SERIES_BYTES:
        raise invalid
    out: dict[str, list[str]] = {}
    total = 0
    for section_id, keys in value.items():
        if not isinstance(section_id, str) or not section_id or len(section_id) > 128:
            raise invalid
        if not isinstance(keys, list) or len(keys) > _MAX_HIDDEN_SERIES_PER_CHART:
            raise invalid
        if not all(isinstance(key, str) and key and len(key) <= _MAX_SERIES_KEY_CHARS for key in keys):
            raise invalid
        total += len(keys)
        if total > _MAX_HIDDEN_SERIES_TOTAL:
            raise invalid
        if keys and section_id in exported_section_ids:
            out[section_id] = sorted(set(keys))
    return dict(sorted(out.items()))


def _validate_client_visual_receipt_shape(
    value: Any,
    *,
    chart_style_contract: int,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) > 500:
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "页面视觉内容凭据无效",
            retryable=False,
        )
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    required_keys = (
        {
            "section_id",
            "section_rev",
            "surface_token",
            "screen_visual_input_sha256",
            "render_disposition",
        }
        if chart_style_contract == 2
        else {
            "section_id",
            "section_rev",
            "visual_input_sha256",
            "render_disposition",
        }
    )
    for item in value:
        if not isinstance(item, dict):
            raise _export_v2_error(
                status.HTTP_400_BAD_REQUEST,
                "invalid_options",
                "页面视觉内容凭据无效",
                retryable=False,
            )
        section_id = item.get("section_id")
        section_rev = item.get("section_rev")
        if (
            set(item) != required_keys
            or not isinstance(section_id, str)
            or not isinstance(section_rev, int)
            or isinstance(section_rev, bool)
            or section_rev < 1
            or not _is_sha256_token(item.get("screen_visual_input_sha256" if chart_style_contract == 2 else "visual_input_sha256"))
            or (chart_style_contract == 2 and not _is_sha256_token(item.get("surface_token")))
            or item.get("render_disposition") != "rendered"
            or section_id in seen
        ):
            raise _export_v2_error(
                status.HTTP_400_BAD_REQUEST,
                "invalid_options",
                "页面视觉内容凭据无效",
                retryable=False,
            )
        seen.add(section_id)
        receipts.append(dict(item))
    return tuple(receipts)


def _validate_candidate_visual_receipts(
    *,
    candidate: Any,
    receipts: tuple[dict[str, Any], ...],
    effective_chart_style: dict[str, Any],
    chart_style_contract: int,
) -> None:
    payload = json.loads(candidate.payload_json)
    # Receipts describe the verified screen input, before export-only frame,
    # annotation and frozen-style materialization changes composed children.
    sections = candidate.screen_sections
    request_payload = payload.get("request")
    if not isinstance(request_payload, dict):
        raise RuntimeError("snapshot candidate payload is invalid")
    section_by_id = {section.get("id"): section for section in sections if isinstance(section, dict) and isinstance(section.get("id"), str)}
    theme = request_payload.get("theme")
    print_theme = request_payload.get("print_theme")
    if not isinstance(theme, dict) or not isinstance(print_theme, dict):
        raise RuntimeError("snapshot candidate theme is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for entry in candidate.surface_projection.entries:
        if entry.parity_requirement != "exact_visual":
            continue
        section = section_by_id.get(entry.section_id)
        if not isinstance(section, dict):
            raise RuntimeError("snapshot candidate surface is invalid")
        section_payload = section
        if chart_style_contract == 2:
            visual_hash = compute_screen_visual_input_sha256_v2(
                section_kind=entry.effective_kind,
                payload=section_payload,
                locale=candidate.locale,
                surface_token=candidate.surface_projection.surface_token,
                effective_chart_style=effective_chart_style,
            )
        else:
            visual_hash = compute_visual_input_sha256(
                section_kind=entry.effective_kind,
                payload=section_payload,
                locale=candidate.locale,
                theme=theme,
                print_theme=print_theme,
            )
        if visual_hash is None:
            raise RuntimeError("exact visual section has no visual identity")
        expected[entry.section_id] = (
            entry.section_rev,
            visual_hash,
        )
    received = {}
    for receipt in receipts:
        if chart_style_contract == 2 and receipt["surface_token"] != candidate.surface_projection.surface_token:
            raise _export_v2_error(
                status.HTTP_409_CONFLICT,
                "surface_projection_conflict",
                "页面视觉内容与待导出版本不一致，请刷新后重试",
                retryable=True,
            )
        received[str(receipt["section_id"])] = (
            int(receipt["section_rev"]),
            str(receipt["screen_visual_input_sha256" if chart_style_contract == 2 else "visual_input_sha256"]),
        )
    if received != expected:
        raise _export_v2_error(
            status.HTTP_409_CONFLICT,
            "surface_projection_conflict",
            "页面视觉内容与待导出版本不一致，请刷新后重试",
            retryable=True,
        )


def _is_sha256_token(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _require_export_runner(request: Request, fmt: str, profile: str) -> Any:
    runner = getattr(request.app.state, "export_runner", None)
    if runner is None or not runner.is_available_for(fmt, profile):
        raise _export_v2_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "export_runner_unavailable",
            "该导出格式的执行器暂不可用",
            retryable=True,
            context={"format": fmt, "profile": profile},
        )
    return runner


def _available_export_v2_capabilities(
    request: Request,
) -> list[CanvasExportV2CapabilityDTO]:
    if getattr(request.app.state, "export_store", None) is None:
        return []
    runner = getattr(request.app.state, "export_runner", None)
    if runner is None:
        return []
    capabilities: list[CanvasExportV2CapabilityDTO] = []
    for fmt, profile in _EXPORT_PARITY_CONTRACTS:
        if runner.is_available_for(fmt, profile):
            capabilities.append(
                CanvasExportV2CapabilityDTO(
                    format=cast(Literal["pdf", "pptx", "xlsx", "docx"], fmt),
                    profile=profile,
                )
            )
    return capabilities


async def _resolve_export_user(request: Request, user_id: str) -> Any | None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return None
    try:
        return await user_store.get_user(user_id)
    except Exception:
        logger.warning("canvas.export.v2.user_lookup_failed", exc_info=True)
        return None


async def _resolve_export_requester_locale(
    request: Request,
    user_id: str,
    explicit_locale: object,
) -> str:
    """Resolve once before snapshot freeze; invalid candidates never stop fallback."""

    user_locale: object = None
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is not None:
        try:
            user = await user_store.get_user(user_id)
            user_locale = getattr(user, "locale", None)
        except Exception:
            logger.warning("canvas.export.v2.user_locale_lookup_failed", exc_info=True)
    return resolve_presentation_locale(explicit_locale, user_locale)


def _map_export_create_error(exc: Exception) -> HTTPException:
    from bowhead_core.export_assets import ExportAssetError
    from bowhead_core.export_snapshot import SnapshotError
    from bowhead_core.export_store import ExportStoreError

    code = getattr(exc, "code", "")
    if isinstance(exc, ExportAssetError):
        if code == "asset_missing_or_forbidden":
            return _export_v2_error(
                status.HTTP_403_FORBIDDEN,
                code,
                "报告引用的文件不可用或无权访问",
                retryable=False,
            )
        if code in {"asset_too_large", "snapshot_assets_too_large"}:
            return _export_v2_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                code,
                "报告引用的文件超过导出限制",
                retryable=False,
            )
    if isinstance(exc, SnapshotError):
        if code == "canvas_not_owned":
            return _export_v2_error(
                status.HTTP_403_FORBIDDEN,
                code,
                "只有报告所有者可以导出",
                retryable=False,
            )
        if code in {"projection_conflict", "surface_projection_conflict"}:
            return _export_v2_error(
                status.HTTP_409_CONFLICT,
                code,
                "报告在准备导出后又发生变化，请刷新后重试",
                retryable=True,
            )
        if code in {"snapshot_too_large", "snapshot_section_limit"}:
            return _export_v2_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "snapshot_too_large",
                "报告内容超过当前导出限制",
                retryable=False,
            )
        if code == "snapshot_quarantined":
            return _export_v2_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "quarantined_sections",
                "报告包含已隔离内容，无法安全导出",
                retryable=False,
            )
        if code in {
            "snapshot_empty",
            "snapshot_unsupported_section_kind",
            "snapshot_invalid_payload",
            "snapshot_invalid_chart_plan",
            "chart_plan_projection_required",
        }:
            return _export_v2_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "unsupported_required_capability",
                "报告包含当前导出器无法可靠处理的必要内容",
                retryable=False,
            )
        if code == "snapshot_id_conflict":
            return _export_v2_error(
                status.HTTP_409_CONFLICT,
                "snapshot_busy",
                "报告快照正在创建，请重试",
                retryable=True,
            )
    if isinstance(exc, ExportStoreError):
        if code == "idempotency_conflict":
            return _export_v2_error(
                status.HTTP_409_CONFLICT,
                code,
                "同一幂等键已用于不同的导出请求",
                retryable=False,
            )
        if code in {"invalid_idempotency_key", "invalid_request_hash"}:
            return _export_v2_error(
                status.HTTP_400_BAD_REQUEST,
                "invalid_options",
                "幂等键或请求摘要无效",
                retryable=False,
            )
        if code == "export_capacity_exceeded":
            return _export_v2_error(
                status.HTTP_429_TOO_MANY_REQUESTS,
                code,
                "当前导出队列已满，请稍后重试",
                retryable=True,
                context={
                    "scope": exc.params.get("scope"),
                    "limit": exc.params.get("limit"),
                },
                headers={"Retry-After": "5"},
            )
    return _export_v2_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "export_runner_unavailable",
        "导出服务暂不可用，请稍后重试",
        retryable=True,
    )


@router.post(
    "/{canvas_id}/exports",
    response_model=ExportJobV2DTO,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_export_v2(
    canvas_id: str,
    body: ExportCreateV2Body,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> ExportJobV2DTO:
    """Freeze the prepared Canvas projection and atomically queue a durable V2 job."""
    canvas = await _load_owned_v2(request, store, canvas_id)
    (
        fmt,
        profile,
        parity_contract,
        locale,
        timezone,
        theme,
        options,
        visual_receipts,
        chart_style_contract,
    ) = _validate_export_v2_body(body)
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not 1 <= len(idempotency_key.encode("utf-8")) <= 128:
        raise _export_v2_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_options",
            "Idempotency-Key 必须为 1 到 128 字节",
            retryable=False,
        )

    user_id = _user_id(request)
    locale = await _resolve_export_requester_locale(request, user_id, locale)
    normalized_request = {
        "canvas_id": canvas.id,
        "trust_copy_contract": TRUST_COPY_CONTRACT,
        "format": fmt,
        "profile": profile,
        "locale": locale,
        "timezone": timezone,
        "theme": theme,
        "consistency": "require_projection",
        "expected_projection_token": body.expected_projection_token,
        "expected_surface_token": body.expected_surface_token,
        "client_surface_receipt_sha256": body.client_surface_receipt_sha256,
        "client_visual_receipts": visual_receipts,
        "options": options,
    }
    from bowhead_core.export_snapshot import ExportSnapshotRequest, SnapshotFormat
    from bowhead_core.export_store import (
        ExportStoreError,
        compute_export_request_sha256,
    )

    request_sha256 = compute_export_request_sha256(normalized_request)
    export_store = _export_store(request)
    existing = await export_store.get_idempotent_job(
        requested_by=user_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        if existing.canvas_id != canvas.id or existing.request_sha256 != request_sha256:
            raise _map_export_create_error(ExportStoreError("idempotency_conflict"))
        versions = await _classify_copy_versions(request, [existing]) if existing.status == "done" else {}
        return _job_v2_dto(existing, copy_contract_version=versions.get(existing.id))

    runner = _require_export_runner(request, fmt, profile)
    export_user = await _resolve_export_user(request, user_id)
    team_id = str(export_user.team_id) if export_user is not None and export_user.team_id else None
    team_settings_store = getattr(request.app.state, "team_settings_store", None)
    user_settings_store = getattr(request.app.state, "user_settings_store", None)
    if team_settings_store is None or user_settings_store is None:
        raise _export_v2_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "export_runner_unavailable",
            "图表风格服务暂不可用，请稍后重试",
            retryable=True,
        )
    # The team's base theme (Community: product default; Business: published brand) is frozen
    # into the job, its checksum, and every visual identity hash. Legacy contract-1 clients
    # compute their visual receipts against the product default print theme, so those jobs keep
    # it; otherwise their receipts could never match a branded snapshot.
    try:
        from bowhead_gateway.routes.theme import resolve_export_print_theme

        print_theme = await resolve_export_print_theme(
            request,
            team_store=team_settings_store,
            team_id=team_id if chart_style_contract == 2 else None,
        )
    except Exception as exc:
        logger.exception("canvas.export.print_theme_unavailable team_id=%s", team_id)
        raise _export_v2_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "export_runner_unavailable",
            "图表风格服务暂不可用，请稍后重试",
            retryable=True,
        ) from exc
    if chart_style_contract == 2:
        chart_style_service = getattr(
            request.app.state,
            "chart_style_read_service",
            None,
        )
        if chart_style_service is None:
            raise _export_v2_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "export_runner_unavailable",
                "图表风格服务暂不可用，请稍后重试",
                retryable=True,
            )
        try:
            if team_id is None or export_user is None:
                raise RuntimeError("export requester has no team")
            from bowhead_gateway.routes.theme import (
                resolve_chart_style_v2_for_current_theme,
            )

            effective_chart_style, _ = await resolve_chart_style_v2_for_current_theme(
                request,
                service=chart_style_service,
                team_store=team_settings_store,
                user_store=user_settings_store,
                user=export_user,
            )
            require_current_chart_style_v2(theme, effective_chart_style)
            export_effective_chart_style = copy.deepcopy(
                effective_chart_style,
            )
            export_snapshot = export_effective_chart_style["style"]["resolvedPresetSnapshot"]
            export_effective_chart_style["modeColors"]["light"] = actual_surface_projection(
                export_snapshot,
                mode="light",
                # Export PNGs sit on white paper whatever the team surface is.
                surface=PRINT_PAPER,
                color_scheme_checksum=print_theme["checksum"],
            )
            frozen_theme = build_chart_style_export_theme_v2(
                export_effective_chart_style,
                print_theme=print_theme,
            )
        except ChartStyleExportError as exc:
            raise _export_v2_error(
                status.HTTP_409_CONFLICT,
                "chart_style_projection_conflict",
                "图表风格已变化，请刷新后重试",
                retryable=True,
            ) from exc
        except Exception as exc:
            logger.exception(
                "canvas.export.v2.chart_style_resolution_failed canvas_id=%s",
                canvas_id,
            )
            raise _export_v2_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "export_runner_unavailable",
                "图表风格服务暂不可用，请稍后重试",
                retryable=True,
            ) from exc
    else:
        effective_chart_style, _ = await resolve_chart_style_for_user(
            team_store=team_settings_store,
            user_store=user_settings_store,
            team_id=team_id,
            user_id=user_id,
        )
        try:
            require_current_chart_style(theme, effective_chart_style)
        except ChartStyleExportError as exc:
            raise _export_v2_error(
                status.HTTP_409_CONFLICT,
                "surface_projection_conflict",
                "图表风格已变化，请刷新后重试",
                retryable=True,
            ) from exc
        frozen_theme = theme
    try:
        snapshot_request = ExportSnapshotRequest(
            requested_by=user_id,
            team_id=team_id,
            fmt=cast(SnapshotFormat, fmt),
            profile=profile,
            parity_contract=parity_contract,
            locale=locale,
            timezone=timezone,
            client_surface_receipt_sha256=str(body.client_surface_receipt_sha256),
            theme=frozen_theme,
            options=options,
            client_visual_receipts=visual_receipts,
            print_theme=print_theme,
        )

        async def project_export_sections(stable_canvas: Any, stable_sections: list[Any]) -> list[Any]:
            # Use the same stable read as snapshot identity, never a later Canvas read.
            refs = await _build_source_refs_for_session(request, stable_canvas.session_id)
            projector = await _viewer_safe_projector(request, stable_canvas, stable_sections, refs)
            projected, _ = projector.project([section.to_report_section() for section in stable_sections])
            return [
                replace(section, payload={key: value for key, value in dto.items() if key not in {"id", "kind", "rev", "superseded", "turn_id"}})
                for section, dto in zip(stable_sections, projected, strict=True)
            ]

        candidate = await export_store.snapshot_store.build_candidate(
            canvas_id=canvas.id,
            request=snapshot_request,
            project_sections=project_export_sections,
        )
        _validate_candidate_visual_receipts(
            candidate=candidate,
            receipts=visual_receipts,
            effective_chart_style=effective_chart_style,
            chart_style_contract=chart_style_contract,
        )
        result = await export_store.create_export_job(
            candidate=candidate,
            expected_projection_token=str(body.expected_projection_token),
            expected_surface_token=str(body.expected_surface_token),
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
    except HTTPException:
        raise
    except Exception as exc:
        mapped = _map_export_create_error(exc)
        if isinstance(mapped.detail, dict) and mapped.detail.get("code") == "export_runner_unavailable":
            logger.exception("canvas.export.v2.create_failed canvas_id=%s", canvas_id)
        raise mapped from exc
    if result.created:
        await runner.enqueue(result.job.id)
    return _job_v2_dto(result.job)


@router.get("/{canvas_id}/exports/{job_id}", response_model=ExportJobV2DTO)
async def get_export_job_v2(
    canvas_id: str,
    job_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> ExportJobV2DTO:
    await _load_owned_v2(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    job = await _export_store(request).get_job(job_id)
    if job is None or job.canvas_id != canvas_id or job.requested_by != _user_id(request):
        raise _export_v2_error(
            status.HTTP_404_NOT_FOUND,
            "export_job_not_found",
            "导出任务不存在",
            retryable=False,
        )
    versions = await _classify_copy_versions(request, [job]) if job.status == "done" else {}
    return _job_v2_dto(job, copy_contract_version=versions.get(job.id))


@router.post(
    "/{canvas_id}/exports/{job_id}/cancel",
    response_model=ExportJobV2DTO,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_export_job_v2(
    canvas_id: str,
    job_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> ExportJobV2DTO:
    await _load_owned_v2(request, store, canvas_id)
    try:
        job = await _export_store(request).cancel_job(
            job_id=job_id,
            canvas_id=canvas_id,
            requested_by=_user_id(request),
        )
    except Exception as exc:
        if getattr(exc, "code", "") == "job_not_cancelable":
            raise _export_v2_error(
                status.HTTP_409_CONFLICT,
                "job_not_cancelable",
                "该导出任务已经结束，不能取消",
                retryable=False,
            ) from exc
        logger.exception("canvas.export.v2.cancel_failed job_id=%s", job_id)
        raise _export_v2_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "export_runner_unavailable",
            "导出服务暂不可用，请稍后重试",
            retryable=True,
        ) from exc
    if job is None:
        raise _export_v2_error(
            status.HTTP_404_NOT_FOUND,
            "export_job_not_found",
            "导出任务不存在",
            retryable=False,
        )
    runner = getattr(request.app.state, "export_runner", None)
    if runner is not None:
        await runner.cancel(job.id)
    return _job_v2_dto(job)


@router.post("/{canvas_id}/export", response_model=ExportJobDTO)
async def export_canvas(
    canvas_id: str,
    body: ExportBody = Body(default_factory=ExportBody),
    request: Request = None,  # type: ignore[assignment]
    store: CanvasStore = Depends(_store),
) -> ExportJobDTO:
    """导出 Canvas 为 pdf / pptx / xlsx。异步跑 skill runtime，立刻返 jobId."""
    # ⚠ Keep this call: _load_owned is **owner authorization** (404/403); its return value is incidental.
    # After M3-3 removes i18n export-locale, no reader remains; discard the result, **retain the call**.
    await _load_owned(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.EXECUTE,
    )
    fmt = body.fmt.lower()
    if fmt not in ("pdf", "pptx", "xlsx", "docx"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "fmt must be pdf|pptx|xlsx|docx")

    template_id: str | None = None
    use_clone_render = False
    if fmt == "pptx" and body.mode == "auto" and body.template_artifact_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "mode=auto 不接受 template_artifact_id；请显式选择 clone",
        )
    if fmt == "pptx" and body.mode == "clone":
        if not body.template_artifact_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "mode=clone 需 template_artifact_id (用户上传的 .pptx)",
            )
        clone_flag = os.environ.get("FEATURE_CANVAS_PPTX_CLONE")
        if clone_flag is None:
            clone_flag = os.environ.get("BOWHEAD_PPTX_USER_TEMPLATE_ENABLED", "0")
        if clone_flag != "1":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "mode=clone 当前未启用",
            )
        template_id = body.template_artifact_id
        use_clone_render = True

    # citation-audit M3-3 (§4.5): previously resolved export target_locale here for
    # render_inline_numbers, effective only with workspace flag inline_numbers_v1 enabled
    # (globally default off; no prod spaces enabled). Retire i18n inline-numbers v1 entirely:
    # remove locale resolution, flag query and export_locale argument. Permanent bake_payload_strings
    # inside canvas_export handles retired markers in export bodies (M1-6②).
    from bowhead_core.canvas_export import enqueue_export

    job = await enqueue_export(
        canvas_id=canvas_id,
        fmt=fmt,
        store=store,
        catalog=getattr(request.app.state, "skill_catalog", None),
        runtime=getattr(request.app.state, "skill_runtime", None),
        artifact_store=getattr(request.app.state, "artifact_store", None),
        # W0-A: standard export no longer infers templates from session attachments; clone uses explicit template_id only.
        session_store=None,
        template_artifact_id=template_id,
        use_clone_render=use_clone_render,
        locale=_resolve_reload_locale(request),
    )
    return _job_dto(job)


@router.get("/{canvas_id}/export/{job_id}", response_model=ExportJobDTO)
async def get_export_job(
    canvas_id: str,
    job_id: str,
    request: Request,
    store: CanvasStore = Depends(_store),
) -> ExportJobDTO:
    """轮询导出进度。"""
    await _load_owned(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    from bowhead_core.canvas_export import get_job

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.canvas_id != canvas_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "job not in this canvas")
    versions = await _classify_copy_versions(request, [job]) if job.status == "done" else {}
    return _job_dto(job, copy_contract_version=versions.get(job.id))


@router.get("/{canvas_id}/exports", response_model=ExportJobsV2ListDTO)
async def list_export_jobs(
    canvas_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    store: CanvasStore = Depends(_store),
) -> ExportJobsV2ListDTO:
    await _load_owned_v2(
        request,
        store,
        canvas_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    from bowhead_core.canvas_export import list_jobs_for_canvas

    # V1/V2 share the additive table during migration. Read each contract via
    # its own store, then merge without converting V2 back to the weaker DTO.
    jobs = await list_jobs_for_canvas(canvas_id, limit=limit)
    export_store = getattr(request.app.state, "export_store", None)
    v2_items: list[ExportJobV2DTO] = []
    if export_store is not None:
        v2_jobs = await export_store.list_jobs(
            canvas_id=canvas_id,
            requested_by=_user_id(request),
            limit=limit,
        )
    else:
        v2_jobs = []
    all_jobs = [job for job in jobs if job.contract_version == 1] + list(v2_jobs)
    versions = await _classify_copy_versions(request, all_jobs)
    legacy_items = [_job_dto(job, copy_contract_version=versions.get(job.id)) for job in jobs if job.contract_version == 1]
    v2_items = [_job_v2_dto(job, copy_contract_version=versions.get(job.id)) for job in v2_jobs]
    items: list[ExportJobV2DTO | ExportJobDTO] = [*v2_items, *legacy_items]
    items.sort(
        key=lambda item: item.created_at if isinstance(item, ExportJobV2DTO) else item.createdAt,
        reverse=True,
    )
    return ExportJobsV2ListDTO(items=items[:limit])
