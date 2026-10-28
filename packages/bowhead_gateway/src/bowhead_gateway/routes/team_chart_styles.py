"""Administrative API for team-owned chart-style drafts and revisions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.audit_actions import AuditAction
from bowhead_core.chart_style_models import ChartStyleDomainError, revision_to_dict
from bowhead_core.chart_style_store import ChartStyleStore
from bowhead_core.chart_style_uow import ChartStyleMutationResult, ChartStyleUnitOfWork
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _require_team_role_dep
from bowhead_gateway.routes.auth import _store as _user_store

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from bowhead_core.user_store import User, UserStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/team-settings/chart-styles", tags=["team-chart-styles"])
_REQUIRE_CHART_STYLE_ADMIN = _require_team_role_dep("owner", "team_admin")
_MAX_MANAGEMENT_BODY_BYTES = 64 * 1024
_PREVIEW_RATE_LIMIT = 10
_PREVIEW_RATE_WINDOW_SECONDS = 1


class ChartStyleSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schemaVersion: int
    name: str
    description: str
    overrides: dict[str, Any]


class CreateChartStyleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    requestId: str
    basePresetId: str
    source: ChartStyleSourceBody


class ReplaceDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: ChartStyleSourceBody


class PreviewChartStyleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    styleId: str
    candidate: ChartStyleSourceBody
    surface: Literal["team-base", "current-admin-personal"]


class PublishOrRestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    acknowledgedValidationFingerprint: str | None = None


def _uow(request: Request) -> ChartStyleUnitOfWork:
    value = getattr(request.app.state, "chart_style_uow", None)
    if not isinstance(value, ChartStyleUnitOfWork):
        raise HTTPException(503, "chart_style_uow not configured")
    return value


def _store(request: Request) -> ChartStyleStore:
    value = getattr(request.app.state, "chart_style_store", None)
    if not isinstance(value, ChartStyleStore):
        raise HTTPException(503, "chart_style_store not configured")
    return value


async def _check_body_size(request: Request) -> None:
    size = len(await request.body())
    if size > _MAX_MANAGEMENT_BODY_BYTES:
        raise HTTPException(
            413,
            detail={"error": {"code": "chart_style_request_too_large", "maximumBytes": _MAX_MANAGEMENT_BODY_BYTES}},
        )


async def _domain(awaitable: Awaitable[Any]) -> Any:
    try:
        return await awaitable
    except ChartStyleDomainError as exc:
        error = {"code": exc.code, **exc.details}
        raise HTTPException(exc.status_code, detail={"error": error}) from exc


async def _audit(
    *,
    request: Request,
    user_store: UserStore,
    user: User,
    action: str,
    result: ChartStyleMutationResult | None,
    style_id: str,
    operation: str,
) -> None:
    detail = result.detail if result is not None else None
    revision = detail.get("activeRevision") if detail else None
    try:
        await user_store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=action,
            target_kind="team_chart_style",
            target_id=style_id,
            target_team_id=user.team_id,
            scope="team",
            payload={
                "style_id": style_id,
                "revision_id": revision.get("id") if isinstance(revision, dict) else None,
                "revision_checksum": revision.get("revisionChecksum") if isinstance(revision, dict) else None,
                "operation": operation,
            },
            ip=request.client.host if request.client else None,
        )
    except Exception:
        logger.exception("chart_style_audit_emit_failed", extra={"style_id": style_id, "operation": operation})


def _mutation_response(response: Response, result: ChartStyleMutationResult) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["ETag"] = result.response_etag
    return result.body()


@router.get("/")
async def list_chart_styles(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    after: str | None = None,
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    return await _domain(_store(request).list_styles(team_id=user.team_id, limit=limit, after=after))


@router.post("/", dependencies=[Depends(require_not_readonly)])
async def create_chart_style(
    body: CreateChartStyleBody,
    request: Request,
    response: Response,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    await _check_body_size(request)
    result = await _domain(
        _uow(request).create(
            team_id=user.team_id,
            actor_id=user.id,
            request_id=body.requestId,
            base_preset_id=body.basePresetId,
            source=body.source.model_dump(),
        )
    )
    style_id = str(result.detail["style"]["id"])
    if result.performed:
        await _audit(
            request=request,
            user_store=user_store,
            user=user,
            action=AuditAction.TEAM_CHART_STYLE_CREATED,
            result=result,
            style_id=style_id,
            operation="create",
        )
    return _mutation_response(response, result)


@router.post("/preview")
async def preview_chart_style(
    body: PreviewChartStyleBody,
    request: Request,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    await _check_body_size(request)
    allowed = await user_store.rate_limit_check(
        f"chart_style_preview:user:{user.id}:team:{user.team_id}",
        limit=_PREVIEW_RATE_LIMIT,
        window_s=_PREVIEW_RATE_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            429,
            detail={"error": {"code": "chart_style_preview_rate_limited"}},
            headers={"Retry-After": str(_PREVIEW_RATE_WINDOW_SECONDS)},
        )
    return await _domain(
        _uow(request).preview(
            team_id=user.team_id,
            style_id=body.styleId,
            source=body.candidate.model_dump(),
        )
    )


@router.get("/{style_id}")
async def get_chart_style(
    style_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    detail = await _domain(_uow(request).detail(team_id=user.team_id, style_id=style_id))
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["ETag"] = str(detail["resourceEtag"])
    return detail


@router.post("/{style_id}/draft", dependencies=[Depends(require_not_readonly)])
async def create_chart_style_draft(
    style_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    result = await _domain(
        _uow(request).create_draft(
            team_id=user.team_id,
            actor_id=user.id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
        )
    )
    return _mutation_response(response, result)


@router.put("/{style_id}/draft", dependencies=[Depends(require_not_readonly)])
async def replace_chart_style_draft(
    style_id: str,
    body: ReplaceDraftBody,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    await _check_body_size(request)
    result = await _domain(
        _uow(request).replace_draft(
            team_id=user.team_id,
            actor_id=user.id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
            source=body.source.model_dump(),
        )
    )
    return _mutation_response(response, result)


@router.delete("/{style_id}/draft", dependencies=[Depends(require_not_readonly)])
async def discard_chart_style_draft(
    style_id: str,
    request: Request,
    response: Response,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    result = await _domain(
        _uow(request).discard_draft(
            team_id=user.team_id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
        )
    )
    await _audit(
        request=request,
        user_store=user_store,
        user=user,
        action=AuditAction.TEAM_CHART_STYLE_DRAFT_DELETED,
        result=result,
        style_id=style_id,
        operation="discard_draft",
    )
    return _mutation_response(response, result)


@router.post("/{style_id}/publish", dependencies=[Depends(require_not_readonly)])
async def publish_chart_style(
    style_id: str,
    body: PublishOrRestoreBody,
    request: Request,
    response: Response,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    await _check_body_size(request)
    result = await _domain(
        _uow(request).publish(
            team_id=user.team_id,
            actor_id=user.id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
            acknowledged_validation_fingerprint=body.acknowledgedValidationFingerprint,
        )
    )
    await _audit(
        request=request,
        user_store=user_store,
        user=user,
        action=AuditAction.TEAM_CHART_STYLE_PUBLISHED,
        result=result,
        style_id=style_id,
        operation="publish",
    )
    return _mutation_response(response, result)


@router.get("/{style_id}/revisions")
async def list_chart_style_revisions(
    style_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    after: str | None = None,
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    return await _domain(_store(request).list_revisions(team_id=user.team_id, style_id=style_id, limit=limit, after=after))


@router.get("/{style_id}/revisions/{revision_id}")
async def get_chart_style_revision(
    style_id: str,
    revision_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    revision = await _domain(_store(request).revision(team_id=user.team_id, style_id=style_id, revision_id=revision_id))
    value = revision_to_dict(revision)
    if value is None:  # pragma: no cover - repository returns a concrete revision
        raise HTTPException(404)
    return value


@router.post("/{style_id}/revisions/{revision_id}/restore", dependencies=[Depends(require_not_readonly)])
async def restore_chart_style_revision(
    style_id: str,
    revision_id: str,
    body: PublishOrRestoreBody,
    request: Request,
    response: Response,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    await _check_body_size(request)
    result = await _domain(
        _uow(request).restore(
            team_id=user.team_id,
            actor_id=user.id,
            style_id=style_id,
            revision_id=revision_id,
            if_match=request.headers.get("if-match"),
            acknowledged_validation_fingerprint=body.acknowledgedValidationFingerprint,
        )
    )
    await _audit(
        request=request,
        user_store=user_store,
        user=user,
        action=AuditAction.TEAM_CHART_STYLE_RESTORED,
        result=result,
        style_id=style_id,
        operation="restore",
    )
    return _mutation_response(response, result)


@router.post("/{style_id}/deactivate", dependencies=[Depends(require_not_readonly)])
async def deactivate_chart_style(
    style_id: str,
    request: Request,
    response: Response,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    result = await _domain(
        _uow(request).deactivate(
            team_id=user.team_id,
            actor_id=user.id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
        )
    )
    await _audit(
        request=request,
        user_store=user_store,
        user=user,
        action=AuditAction.TEAM_CHART_STYLE_DEACTIVATED,
        result=result,
        style_id=style_id,
        operation="deactivate",
    )
    return _mutation_response(response, result)


@router.post("/{style_id}/activate", dependencies=[Depends(require_not_readonly)])
async def activate_chart_style(
    style_id: str,
    request: Request,
    response: Response,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> dict[str, Any]:
    result = await _domain(
        _uow(request).activate(
            team_id=user.team_id,
            actor_id=user.id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
        )
    )
    await _audit(
        request=request,
        user_store=user_store,
        user=user,
        action=AuditAction.TEAM_CHART_STYLE_REACTIVATED,
        result=result,
        style_id=style_id,
        operation="activate",
    )
    return _mutation_response(response, result)


@router.delete("/{style_id}", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_draft_only_chart_style(
    style_id: str,
    request: Request,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_CHART_STYLE_ADMIN),
) -> Response:
    await _domain(
        _uow(request).delete_draft_only(
            team_id=user.team_id,
            style_id=style_id,
            if_match=request.headers.get("if-match"),
        )
    )
    await _audit(
        request=request,
        user_store=user_store,
        user=user,
        action=AuditAction.TEAM_CHART_STYLE_DRAFT_DELETED,
        result=None,
        style_id=style_id,
        operation="delete_draft_only",
    )
    return Response(status_code=204)
