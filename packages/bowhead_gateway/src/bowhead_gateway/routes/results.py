"""Authenticated capability negotiation for the Results read model."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from bowhead_gateway.results_capabilities import resolve_results_capabilities
from bowhead_gateway.results_contracts import (
    ResultsCapabilitiesResponseV1,
    ResultsQueryRequestV1,
    ResultsQueryResponseV1,
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


__all__ = ["owner_router", "router"]
