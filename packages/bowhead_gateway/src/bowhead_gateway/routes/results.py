"""Authenticated capability negotiation for the Results read model."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from bowhead_gateway.presentation_edit_service import PresentationEditsRequestV1, PresentationInventoryRequestV1
from bowhead_gateway.results_capabilities import resolve_results_capabilities
from bowhead_gateway.results_contracts import (
    ContentSharingCapabilityV1,
    ResultFileVersionV1,
    ResultsCapabilitiesResponseV1,
    ResultsQueryRequestV1,
    ResultsQueryResponseV1,
    ResultVersionsRequestV1,
    ResultVersionsResponseV1,
)
from bowhead_gateway.results_cursor import ResultsCursorCodec, ResultsCursorUnavailable
from bowhead_gateway.results_query_http import (
    coded_results_error,
    record_results_query_metrics,
)
from bowhead_gateway.results_query_service import ResultsQueryError, query_owner_results
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.user_store import User

router = APIRouter()
owner_router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/capabilities", response_model=ResultsCapabilitiesResponseV1)
async def results_capabilities(
    response: Response,
    _user: User = Depends(_current_user),
) -> ResultsCapabilitiesResponseV1:
    from bowhead_core.share_service import content_sharing_enabled

    sharing_enabled = content_sharing_enabled()
    capabilities = resolve_results_capabilities()
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    try:
        from bowhead_core import metrics

        metrics.inc(
            "results_capability_effective_total",
            labels={
                "complete": str(capabilities.complete_list).lower(),
                "scale": str(capabilities.scale_nav).lower(),
            },
        )
    except Exception:  # noqa: S110 - observability must not break negotiation
        pass
    return ResultsCapabilitiesResponseV1(
        interactive_html=capabilities.interactive_html,
        content_sharing=ContentSharingCapabilityV1(enabled=sharing_enabled, reason=None if sharing_enabled else "deployment_disabled"),
        workspace=capabilities.workspace,
        complete_list=capabilities.complete_list,
        scale_nav=capabilities.scale_nav,
    )


@owner_router.post(
    "/sessions/{session_id}/results/query",
    response_model=ResultsQueryResponseV1,
)
async def owner_results_query(
    session_id: str,
    body: ResultsQueryRequestV1,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> ResultsQueryResponseV1:
    started_at = time.perf_counter()
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    capabilities = resolve_results_capabilities()
    if not capabilities.complete_list:
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        )
    session_store = getattr(request.app.state, "session_store", None)
    canvas_store = getattr(request.app.state, "canvas_store", None)
    artifact_store = getattr(request.app.state, "artifact_store", None)
    if session_store is None or canvas_store is None or artifact_store is None:
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        )
    try:
        session = await session_store.get_session(session_id)
    except Exception as exc:
        logger.exception(
            "results.owner_scope_unavailable error_type=%s",
            type(exc).__name__,
        )
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        ) from exc
    if session is None or session.user_id != user.id:
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_scope_unavailable",
        )
        raise coded_results_error(
            status.HTTP_404_NOT_FOUND,
            "results_scope_unavailable",
        )
    from bowhead_gateway.routes.sessions import _require_workspace_access

    try:
        await _require_workspace_access(
            request,
            user_id=user.id,
            workspace_id=session.workspace_id,
            operation=SpaceOperation.READ_HISTORY,
        )
    except HTTPException as exc:
        if exc.status_code in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        }:
            record_results_query_metrics(
                started_at=started_at,
                scope="owner",
                surface=body.surface,
                error_class="results_scope_unavailable",
            )
            raise coded_results_error(
                status.HTTP_404_NOT_FOUND,
                "results_scope_unavailable",
            ) from exc
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        ) from exc
    workspace_writable = True
    try:
        await _require_workspace_access(
            request,
            user_id=user.id,
            workspace_id=session.workspace_id,
            operation=SpaceOperation.WRITE,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            workspace_writable = False
        elif exc.status_code in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        }:
            record_results_query_metrics(
                started_at=started_at,
                scope="owner",
                surface=body.surface,
                error_class="results_scope_unavailable",
            )
            raise coded_results_error(
                status.HTTP_404_NOT_FOUND,
                "results_scope_unavailable",
            ) from exc
        else:
            record_results_query_metrics(
                started_at=started_at,
                scope="owner",
                surface=body.surface,
                error_class="results_read_model_unavailable",
            )
            raise coded_results_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "results_read_model_unavailable",
            ) from exc
    try:
        cursor_codec = ResultsCursorCodec.from_environment()
        result = await query_owner_results(
            session=session,
            team_id=user.team_id,
            request_body=body,
            scale_nav=capabilities.scale_nav,
            session_store=session_store,
            canvas_store=canvas_store,
            artifact_store=artifact_store,
            cursor_codec=cursor_codec,
            writable=workspace_writable,
        )
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="none",
            result=result,
        )
        return result
    except ResultsCursorUnavailable as exc:
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        ) from exc
    except ResultsQueryError as exc:
        status_by_code = {
            "results_query_invalid": status.HTTP_400_BAD_REQUEST,
            "results_snapshot_stale": status.HTTP_409_CONFLICT,
            "results_anchor_not_in_view": status.HTTP_409_CONFLICT,
            "results_filter_unavailable": status.HTTP_409_CONFLICT,
            "results_read_model_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_artifact_contract_unsupported": status.HTTP_409_CONFLICT,
        }
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class=exc.code,
        )
        raise coded_results_error(status_by_code[exc.code], exc.code) from exc
    except Exception as exc:
        logger.exception(
            "results.owner_query_unavailable error_type=%s",
            type(exc).__name__,
        )
        record_results_query_metrics(
            started_at=started_at,
            scope="owner",
            surface=body.surface,
            error_class="results_read_model_unavailable",
        )
        raise coded_results_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "results_read_model_unavailable",
        ) from exc


@owner_router.post(
    "/sessions/{session_id}/results/versions",
    response_model=ResultVersionsResponseV1,
)
async def owner_result_versions(
    session_id: str,
    body: ResultVersionsRequestV1,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> ResultVersionsResponseV1:
    """File versions (R2): the live versions of one file in the owner's Results, newest first.

    The same owner fences as the Results query: the session owner, history read
    access in its Space, and per-row membership, deletion and internal visibility.
    An unknown or foreign file and a foreign session are indistinguishable (404).
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    session_store = getattr(request.app.state, "session_store", None)
    artifact_store = getattr(request.app.state, "artifact_store", None)
    if session_store is None or artifact_store is None:
        raise coded_results_error(status.HTTP_503_SERVICE_UNAVAILABLE, "results_read_model_unavailable")
    session = await session_store.get_session(session_id)
    if session is None or session.user_id != user.id:
        raise coded_results_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable")
    from bowhead_gateway.routes.sessions import _require_workspace_access

    try:
        await _require_workspace_access(
            request,
            user_id=user.id,
            workspace_id=session.workspace_id,
            operation=SpaceOperation.READ_HISTORY,
        )
    except HTTPException as exc:
        if exc.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}:
            raise coded_results_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable") from exc
        raise coded_results_error(status.HTTP_503_SERVICE_UNAVAILABLE, "results_read_model_unavailable") from exc
    versions = await artifact_store.list_result_versions(session_id=session.id, user_id=user.id, artifact_id=body.artifact_id)
    if not versions:
        raise coded_results_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable")
    return ResultVersionsResponseV1(
        versions=[
            ResultFileVersionV1(
                artifact_id=artifact.id,
                version_no=artifact.version_no or 1,
                filename=artifact.filename,
                mime=artifact.mime,
                size=artifact.size,
                created_at=artifact.created_at,
                latest=index == 0,
            )
            for index, artifact in enumerate(versions)
        ]
    )


__all__ = ["owner_router", "router"]


async def _presentation_edit_session(request: Request, session_id: str, user: User) -> Any:
    """The owner fences of the Results routes, with write access to the Space (pptx-inline-edit design §6)."""
    from bowhead_gateway.presentation_edit_service import require_feature

    require_feature()
    session_store = getattr(request.app.state, "session_store", None)
    if session_store is None or getattr(request.app.state, "artifact_store", None) is None:
        raise coded_results_error(status.HTTP_503_SERVICE_UNAVAILABLE, "results_read_model_unavailable")
    session = await session_store.get_session(session_id)
    if session is None or session.user_id != user.id:
        raise coded_results_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable")
    from bowhead_gateway.routes.sessions import _require_workspace_access

    try:
        await _require_workspace_access(request, user_id=user.id, workspace_id=session.workspace_id, operation=SpaceOperation.WRITE)
    except HTTPException as exc:
        if exc.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}:
            raise coded_results_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable") from exc
        raise coded_results_error(status.HTTP_503_SERVICE_UNAVAILABLE, "results_read_model_unavailable") from exc
    return session


@owner_router.post("/sessions/{session_id}/results/presentation-edit/inventory")
async def owner_presentation_edit_inventory(
    session_id: str,
    body: PresentationInventoryRequestV1,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """What a person can edit in place on each slide of a deck in their Results (design §6.2)."""
    from bowhead_gateway.presentation_edit_service import inventory, resolve_target

    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    session = await _presentation_edit_session(request, session_id, user)
    store = request.app.state.artifact_store
    target = await resolve_target(request.app, store, session_id=session.id, user_id=user.id, artifact_id=body.artifact_id)
    from bowhead_gateway.error_handler import _resolve_ui_language

    return await inventory(store, target, session_id=session.id, user_id=user.id, language=await _resolve_ui_language(request))


@owner_router.post("/sessions/{session_id}/results/presentation-edits", status_code=status.HTTP_201_CREATED)
async def owner_presentation_edits(
    session_id: str,
    body: PresentationEditsRequestV1,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Save a person's edits as the next version, or as a new file for an upload (design §6.3, §6.4)."""
    from bowhead_gateway.presentation_edit_service import resolve_target, save_edits

    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    session = await _presentation_edit_session(request, session_id, user)
    store = request.app.state.artifact_store
    target = await resolve_target(request.app, store, session_id=session.id, user_id=user.id, artifact_id=body.artifact_id)
    from bowhead_gateway.error_handler import _resolve_ui_language

    language = await _resolve_ui_language(request)
    return await save_edits(request.app, store, target, session=session, user_id=user.id, edits=body.edits, language=language)
