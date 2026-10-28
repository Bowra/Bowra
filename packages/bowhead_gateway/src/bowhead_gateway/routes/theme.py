"""Effective theme resolution endpoint."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, NoReturn

from bowhead_core.chart_style import (
    ACTIVE_CHART_STYLE_PRESET_ID_SET,
    KEY_CHART_ACCESSIBILITY_PREFERENCES,
    KEY_TEAM_CHART_STYLE_POLICY,
    ChartStyleError,
    default_team_chart_style_policy,
    normalize_chart_accessibility_preferences,
    normalize_user_team_chart_style_selection,
    parse_chart_accessibility_preferences,
    parse_team_chart_style_policy,
    user_team_chart_style_selection_key,
)
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    LIGHT_STANDARD_SURFACE,
)
from bowhead_core.chart_style_models import ChartStyleDomainError
from bowhead_core.chart_style_read_service import ChartStyleReadService
from bowhead_core.chart_style_v2 import (
    CHART_STYLE_CONTRACT_HEADER,
    normalize_user_team_chart_style_selection_v2,
)
from bowhead_core.effective_theme import RESOLVER_SCHEMA_VERSION, ThemeResolver
from bowhead_core.export_native_palette import native_export_palette
from bowhead_core.personal_appearance_service import (
    PersonalAppearanceBase,
    PersonalAppearanceBaseIncompatible,
    PersonalAppearanceCompileStale,
    PersonalAppearanceConflict,
    PersonalAppearanceInvalidDocument,
    PersonalAppearancePreconditionRequired,
    PersonalAppearanceService,
    PersonalAppearanceUndoConflict,
    PersonalAppearanceUndoExpired,
    PersonalAppearanceValidationFailed,
)
from bowhead_core.personal_appearance_store import PersonalAppearanceBaseGuard
from bowhead_core.personal_theme_models import PersonalThemeModelError
from bowhead_core.print_theme import build_print_theme_snapshot, current_print_theme_snapshot
from bowhead_core.theme_branding_flags import is_team_branding_render_safe_mode
from bowhead_core.theme_config import (
    KEY_APPEARANCE_THEME_CONFIG,
    KEY_TEAM_THEME_POLICY,
    ThemeConfigError,
    parse_appearance_theme_config,
    parse_team_theme_policy,
    resolve_effective_theme,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.chart_style_resolution import (
    resolve_chart_style_for_user,
    resolve_chart_style_for_user_v2,
)
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_settings_store import UserSettingsStore
    from bowhead_core.user_store import User
    from fastapi import FastAPI

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/theme", tags=["theme"])
_PERSONAL_BODY_BYTES = 8192


class ChartStyleSelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = None
    presetId: str | None = None
    styleId: str | None = None


class ChartAccessibilityPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contrast: str | None = None
    colorVision: str | None = None


class PersonalThemePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any]


class PersonalThemeCompileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseEtag: str
    candidate: dict[str, Any]
    candidateId: str


class PersonalThemeUndoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resetId: str


def _user_settings_store(request: Request) -> UserSettingsStore:
    store = getattr(request.app.state, "user_settings_store", None)
    if store is None:
        raise HTTPException(503, "user_settings_store not configured")
    return store


def _team_settings_store(request: Request) -> TeamSettingsStore:
    store = getattr(request.app.state, "team_settings_store", None)
    if store is None:
        raise HTTPException(503, "team_settings_store not configured")
    return store


def _effective_resolver(request: Request) -> ThemeResolver | None:
    return getattr(request.app.state, "effective_theme_resolver", None)


def _chart_style_read_service(request: Request) -> ChartStyleReadService:
    service = getattr(request.app.state, "chart_style_read_service", None)
    if isinstance(service, ChartStyleReadService):
        return service
    store = getattr(request.app.state, "chart_style_store", None)
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        raise HTTPException(503, "chart_style_read_service not configured")
    return ChartStyleReadService(db_path)


def _chart_style_contract(request: Request) -> int:
    value = request.headers.get(CHART_STYLE_CONTRACT_HEADER)
    if value in (None, "1"):
        return 1
    if value == "2":
        return 2
    raise HTTPException(
        400,
        detail={"error": {"code": "chart_style_contract_unsupported"}},
    )


def _chart_style_vary() -> str:
    return f"Cookie, X-Team-Id, X-Bowhead-Public-Entry, {CHART_STYLE_CONTRACT_HEADER}, Accept-Language, Accept-Encoding"


def _raise_chart_style_domain(exc: ChartStyleDomainError) -> NoReturn:
    detail: dict[str, Any] = {"code": exc.code}
    detail.update(exc.details)
    raise HTTPException(
        exc.status_code,
        detail={"error": detail},
    ) from exc


def _personal_service(request: Request) -> PersonalAppearanceService:
    service = getattr(request.app.state, "personal_appearance_service", None)
    if service is None:
        raise HTTPException(
            503,
            detail={"error": {"code": "personal_theme_store_unavailable"}},
        )
    return service


def _parse_user_config(raw: str | None, *, user_id: str) -> dict[str, Any]:
    if not raw:
        return {"version": 1}
    try:
        return parse_appearance_theme_config(raw)
    except ThemeConfigError as exc:
        logger.warning(
            "theme.effective.invalid_user_theme user_id=%s key=%s err=%s",
            user_id,
            KEY_APPEARANCE_THEME_CONFIG,
            str(exc),
        )
        return {"version": 1}


def _parse_team_policy(raw: str | None, *, team_id: str) -> dict[str, Any]:
    if not raw:
        return {"version": 1}
    try:
        return parse_team_theme_policy(raw)
    except ThemeConfigError as exc:
        logger.warning(
            "theme.effective.invalid_team_theme team_id=%s key=%s err=%s",
            team_id,
            KEY_TEAM_THEME_POLICY,
            str(exc),
        )
        return {"version": 1}


def _parse_chart_style_team_policy(raw: str | None, *, team_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        return parse_team_chart_style_policy(raw)
    except ChartStyleError as exc:
        logger.warning(
            "chart_style.invalid_team_policy team_id=%s key=%s err=%s",
            team_id,
            KEY_TEAM_CHART_STYLE_POLICY,
            str(exc),
        )
        return None


def _parse_chart_accessibility(raw: str | None, *, user_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        return parse_chart_accessibility_preferences(raw)
    except ChartStyleError as exc:
        logger.warning(
            "chart_style.invalid_accessibility user_id=%s key=%s err=%s",
            user_id,
            KEY_CHART_ACCESSIBILITY_PREFERENCES,
            str(exc),
        )
        return None


@router.get("/personal", response_model=None)
async def get_personal_theme(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    service: PersonalAppearanceService = Depends(_personal_service),
    user: User = Depends(_current_user),
) -> dict[str, Any] | Response:
    base, _ = await _resolve_personal_base(request, team_store=team_store, user=user)
    resource = await service.get(user_id=user.id, team_id=user.team_id, base=base)
    headers = _personal_headers(resource.etag)
    if request.headers.get("If-None-Match") == resource.etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return resource.payload


@router.patch(
    "/personal",
    response_model=None,
    dependencies=[Depends(require_not_readonly)],
)
async def patch_personal_theme(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    service: PersonalAppearanceService = Depends(_personal_service),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    body = await _personal_body(request, PersonalThemePatchBody)
    base, _ = await _resolve_personal_base(request, team_store=team_store, user=user)
    try:
        resource = await service.patch(
            user_id=user.id,
            team_id=user.team_id,
            base=base,
            if_match=request.headers.get("If-Match"),
            changes=body.changes,
        )
    except Exception as exc:
        _raise_personal_http(exc)
    _apply_headers(response, _personal_headers(resource.etag))
    return resource.payload


@router.post("/personal/compile", response_model=None)
async def compile_personal_theme(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    service: PersonalAppearanceService = Depends(_personal_service),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    body = await _personal_body(request, PersonalThemeCompileBody)
    if not _opaque_ascii(body.candidateId, maximum=64):
        raise HTTPException(
            400,
            detail={"error": {"code": "personal_theme_invalid_json", "field": "candidateId"}},
        )
    base, _ = await _resolve_personal_base(request, team_store=team_store, user=user)
    try:
        result = await service.compile_candidate(
            user_id=user.id,
            team_id=user.team_id,
            base=base,
            base_etag=body.baseEtag,
            candidate=body.candidate,
            candidate_id=body.candidateId,
        )
    except Exception as exc:
        _raise_personal_http(exc)
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.delete(
    "/personal",
    response_model=None,
    dependencies=[Depends(require_not_readonly)],
)
async def reset_personal_theme(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    service: PersonalAppearanceService = Depends(_personal_service),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key or not _opaque_ascii(idempotency_key, maximum=128):
        raise HTTPException(
            400,
            detail={"error": {"code": "personal_theme_idempotency_required"}},
        )
    base, _ = await _resolve_personal_base(request, team_store=team_store, user=user)
    try:
        result = await service.reset(
            user_id=user.id,
            team_id=user.team_id,
            base=base,
            if_match=request.headers.get("If-Match"),
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        _raise_personal_http(exc)
    _apply_headers(response, _personal_headers(result.resource.etag))
    return {
        "resource": result.resource.payload,
        "undo": ({"resetId": result.reset_id, "expiresAt": result.expires_at} if result.reset_id is not None else None),
    }


@router.post(
    "/personal/undo",
    response_model=None,
    dependencies=[Depends(require_not_readonly)],
)
async def undo_personal_theme(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    service: PersonalAppearanceService = Depends(_personal_service),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    body = await _personal_body(request, PersonalThemeUndoBody)
    base, _ = await _resolve_personal_base(request, team_store=team_store, user=user)
    try:
        resource = await service.undo(
            user_id=user.id,
            team_id=user.team_id,
            base=base,
            if_match=request.headers.get("If-Match"),
            reset_id=body.resetId,
        )
    except Exception as exc:
        _raise_personal_http(exc)
    _apply_headers(response, _personal_headers(resource.etag))
    return resource.payload


@router.put(
    "/chart-style/selection",
    dependencies=[Depends(require_not_readonly)],
)
async def set_chart_style_selection(
    body: ChartStyleSelectionBody,
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserSettingsStore = Depends(_user_settings_store),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    key = user_team_chart_style_selection_key(user.team_id)
    contract = _chart_style_contract(request)
    if contract == 2:
        if body.model_fields_set != {"version", "styleId"} or body.version != 2:
            raise HTTPException(
                400,
                detail={"error": {"code": "chart_style_selection_invalid"}},
            )
        if body.styleId is None:
            await user_store.delete(user.id, key)
            return {"ok": True}
        try:
            chart_style_service = _chart_style_read_service(request)
            fallback = await chart_style_service.selection_fallback(
                team_id=user.team_id,
                style_id=body.styleId,
            )
            normalized = normalize_user_team_chart_style_selection_v2(
                style_id=body.styleId,
                fallback_builtin_preset_id=fallback,
            )
        except ChartStyleDomainError as exc:
            _raise_chart_style_domain(exc)
        except ChartStyleError as exc:
            raise HTTPException(
                400,
                detail={"error": {"code": "chart_style_selection_invalid"}},
            ) from exc
        await user_store.set(user.id, key, normalized)
        return {"ok": True}

    if body.model_fields_set != {"presetId"}:
        raise HTTPException(400, "body must contain exactly presetId")
    if body.presetId is None:
        await user_store.delete(user.id, key)
        return {"ok": True}
    if body.presetId not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise HTTPException(400, "presetId must reference an active chart-style preset")
    raw_policy = await team_store.get(user.team_id, KEY_TEAM_CHART_STYLE_POLICY)
    policy = _parse_chart_style_team_policy(raw_policy, team_id=user.team_id) or default_team_chart_style_policy()
    if body.presetId not in policy["enabledPresetIds"]:
        raise HTTPException(
            409,
            detail={"error": {"code": "chart_style_preset_disabled_by_team"}},
        )
    normalized = normalize_user_team_chart_style_selection(json.dumps({"version": 1, "presetId": body.presetId}, separators=(",", ":")))
    await user_store.set(user.id, key, normalized)
    return {"ok": True}


@router.get("/chart-style/options", response_model=None)
async def get_chart_style_options(
    request: Request,
    response: Response,
    service: ChartStyleReadService = Depends(_chart_style_read_service),
    user: User = Depends(_current_user),
) -> dict[str, Any] | Response:
    if _chart_style_contract(request) != 2:
        raise HTTPException(
            400,
            detail={"error": {"code": "chart_style_contract_2_required"}},
        )
    body, etag = await service.options(team_id=user.team_id)
    headers = {
        "Cache-Control": "private, no-cache",
        "ETag": etag,
        "Vary": _chart_style_vary(),
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    _apply_headers(response, headers)
    return body


@router.put(
    "/chart-style/accessibility",
    dependencies=[Depends(require_not_readonly)],
)
async def patch_chart_accessibility(
    body: ChartAccessibilityPatchBody,
    user_store: UserSettingsStore = Depends(_user_settings_store),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(400, "at least one accessibility field is required")

    def mutate(raw: str | None) -> str | None:
        current = _parse_chart_accessibility(raw, user_id=user.id) or {"version": 1}
        for field in fields:
            value = getattr(body, field)
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value
        if current == {"version": 1}:
            return None
        return normalize_chart_accessibility_preferences(json.dumps(current, ensure_ascii=True, separators=(",", ":")))

    try:
        await user_store.update_value(
            user.id,
            KEY_CHART_ACCESSIBILITY_PREFERENCES,
            mutate,
        )
    except ChartStyleError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.get("/effective", response_model=None)
async def get_effective_theme(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserSettingsStore = Depends(_user_settings_store),
    user: User = Depends(_current_user),
) -> dict[str, Any] | Response:
    chart_style_contract = _chart_style_contract(request)
    team_policy = _parse_team_policy(
        await team_store.get(user.team_id, KEY_TEAM_THEME_POLICY),
        team_id=user.team_id,
    )
    raw_user_config, user_setting_updated_at = await user_store.get_with_updated_at(
        user.id,
        KEY_APPEARANCE_THEME_CONFIG,
    )
    user_config = _parse_user_config(
        raw_user_config,
        user_id=user.id,
    )
    chart_style: dict[str, Any] | None = None
    chart_style_etag_material = ""
    if chart_style_contract == 1:
        chart_style, chart_style_etag_material = await resolve_chart_style_for_user(
            team_store=team_store,
            user_store=user_store,
            user_id=user.id,
            team_id=user.team_id,
        )

    resolver = _effective_resolver(request)
    if resolver is None:
        resolved = resolve_effective_theme(team_policy, user_config)
        brand_name = team_policy.get("brand", {}).get("name")
        resolved["team"] = {"brandName": brand_name}
        if chart_style_contract == 2:
            chart_style, chart_style_etag_material = await _resolve_chart_style_v2(
                request,
                payload=resolved,
                user=user,
            )
        resolved["chartStyle"] = chart_style
        if chart_style_contract == 2:
            etag = _effective_etag(
                team_id=user.team_id,
                revision_id="legacy-no-resolver",
                revision_checksum="legacy-no-resolver",
                raw_user_config=raw_user_config,
                user_setting_updated_at=user_setting_updated_at,
                chart_style_material=chart_style_etag_material,
                strong=True,
            )
            headers = {
                "Cache-Control": "private, no-cache",
                "ETag": etag,
                "Vary": _chart_style_vary(),
            }
            if request.headers.get("If-None-Match") == etag:
                return Response(status_code=304, headers=headers)
            _apply_headers(response, headers)
        return resolved

    personal_service = getattr(request.app.state, "personal_appearance_service", None)
    if personal_service is not None:
        base, base_payload = await _resolve_personal_base(
            request,
            team_store=team_store,
            user=user,
        )
        personal = await personal_service.get(
            user_id=user.id,
            team_id=user.team_id,
            base=base,
        )
        payload = _personal_effective_payload(
            base_payload=base_payload,
            personal_payload=personal.payload,
            legacy_user_config=user_config,
        )
        if chart_style_contract == 2:
            chart_style, chart_style_etag_material = await _resolve_chart_style_v2(
                request,
                payload=payload,
                user=user,
            )
        etag = _effective_etag(
            team_id=user.team_id,
            revision_id=str(base.team.get("revisionId") or "legacy"),
            revision_checksum=base.identity,
            raw_user_config=raw_user_config,
            user_setting_updated_at=user_setting_updated_at,
            chart_style_material=chart_style_etag_material,
            personal_material=personal.etag,
            strong=chart_style_contract == 2,
        )
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": etag,
            "Vary": _chart_style_vary(),
        }
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers=headers)
        _apply_headers(response, headers)
        if chart_style is not None:
            payload["chartStyle"] = chart_style
        return payload

    bundle = await resolver.resolve(
        team_id=user.team_id,
        team_policy=team_policy,
        user_config=user_config,
        allow_custom=await _can_render_custom_branding(request, user.team_id),
        render_safe_mode=is_team_branding_render_safe_mode(),
    )
    payload = bundle.payload
    if chart_style_contract == 2:
        chart_style, chart_style_etag_material = await _resolve_chart_style_v2(
            request,
            payload=payload,
            user=user,
        )
    etag = _effective_etag(
        team_id=user.team_id,
        revision_id=bundle.revision_id,
        revision_checksum=bundle.revision_checksum,
        raw_user_config=raw_user_config,
        user_setting_updated_at=user_setting_updated_at,
        chart_style_material=chart_style_etag_material,
        strong=chart_style_contract == 2,
    )
    headers = {
        "Cache-Control": "private, no-cache",
        "ETag": etag,
        "Vary": _chart_style_vary(),
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return {**payload, "chartStyle": chart_style}


async def _resolve_chart_style_v2(
    request: Request,
    *,
    payload: dict[str, Any],
    user: User,
) -> tuple[dict[str, Any], str]:
    surfaces, color_scheme_checksum = _chart_style_surface_context(payload)
    try:
        return await resolve_chart_style_for_user_v2(
            service=_chart_style_read_service(request),
            team_id=user.team_id,
            user_id=user.id,
            surfaces=surfaces,
            color_scheme_checksum=color_scheme_checksum,
        )
    except ChartStyleDomainError as exc:
        _raise_chart_style_domain(exc)


def _chart_style_surface_context(
    payload: dict[str, Any],
) -> tuple[dict[str, str], str]:
    effective = payload.get("effective")
    scheme = effective.get("colorScheme") if isinstance(effective, dict) else None
    surfaces = {
        "light": LIGHT_STANDARD_SURFACE,
        "dark": DARK_STANDARD_SURFACE,
    }
    color_scheme_checksum = "structural-standard-surfaces"
    if isinstance(scheme, dict):
        for mode in ("light", "dark"):
            mode_tokens = scheme.get(mode)
            surface = mode_tokens.get("surface") if isinstance(mode_tokens, dict) else None
            if isinstance(surface, str):
                surfaces[mode] = surface
        checksum = scheme.get("checksum")
        if isinstance(checksum, str) and checksum:
            color_scheme_checksum = checksum
        else:
            color_scheme_checksum = hashlib.sha256(json.dumps(scheme, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()
    return surfaces, color_scheme_checksum


async def resolve_chart_style_v2_for_current_theme(
    request: Request,
    *,
    service: ChartStyleReadService,
    team_store: TeamSettingsStore,
    user_store: UserSettingsStore,
    user: User,
) -> tuple[dict[str, Any], str]:
    """Resolve screen colors through the same structural-theme path as the API."""

    team_policy = _parse_team_policy(
        await team_store.get(user.team_id, KEY_TEAM_THEME_POLICY),
        team_id=user.team_id,
    )
    raw_user_config, _ = await user_store.get_with_updated_at(
        user.id,
        KEY_APPEARANCE_THEME_CONFIG,
    )
    user_config = _parse_user_config(raw_user_config, user_id=user.id)
    resolver = _effective_resolver(request)
    if resolver is None:
        payload = resolve_effective_theme(team_policy, user_config)
    else:
        personal_service = getattr(
            request.app.state,
            "personal_appearance_service",
            None,
        )
        if personal_service is not None:
            base, base_payload = await _resolve_personal_base(
                request,
                team_store=team_store,
                user=user,
            )
            personal = await personal_service.get(
                user_id=user.id,
                team_id=user.team_id,
                base=base,
            )
            payload = _personal_effective_payload(
                base_payload=base_payload,
                personal_payload=personal.payload,
                legacy_user_config=user_config,
            )
        else:
            bundle = await resolver.resolve(
                team_id=user.team_id,
                team_policy=team_policy,
                user_config=user_config,
                allow_custom=await _can_render_custom_branding(
                    request,
                    user.team_id,
                ),
                render_safe_mode=is_team_branding_render_safe_mode(),
            )
            payload = bundle.payload
    surfaces, color_scheme_checksum = _chart_style_surface_context(payload)
    return await resolve_chart_style_for_user_v2(
        service=service,
        team_id=user.team_id,
        user_id=user.id,
        surfaces=surfaces,
        color_scheme_checksum=color_scheme_checksum,
    )


async def resolve_export_print_theme(
    request: Request,
    *,
    team_store: TeamSettingsStore,
    team_id: str | None,
) -> dict[str, Any]:
    """Freeze the export print theme from the team's base theme.

    The fixed edition composition decides what the base theme is: the Community resolver
    always renders the product default, while the Business resolver installed by the branding
    adapter returns the team's published brand (or its fallback revision when custom branding
    is not licensed). Personal appearance never reaches exports, so the user configuration is
    empty, exactly like the personal-appearance base. Without a team or a resolver the product
    default applies.
    """

    resolver = _effective_resolver(request)
    if team_id is None or resolver is None:
        return current_print_theme_snapshot()
    bundle = await resolver.resolve(
        team_id=team_id,
        team_policy=_parse_team_policy(
            await team_store.get(team_id, KEY_TEAM_THEME_POLICY),
            team_id=team_id,
        ),
        user_config={"version": 1},
        allow_custom=await _can_render_custom_branding(request, team_id),
        render_safe_mode=is_team_branding_render_safe_mode(),
    )
    snapshot = build_print_theme_snapshot(bundle.payload["effective"]["colorScheme"]["light"])
    # Fail before freezing a job whose native layouts could not meet the contrast floors.
    native_export_palette(snapshot)
    return snapshot


async def _resolve_personal_base(
    request: Request,
    *,
    team_store: TeamSettingsStore,
    user: User,
) -> tuple[PersonalAppearanceBase, dict[str, Any]]:
    return await resolve_personal_base_for_user(request.app, team_store=team_store, user=user)


async def resolve_personal_base_for_user(
    app: FastAPI,
    *,
    team_store: TeamSettingsStore,
    user: User,
) -> tuple[PersonalAppearanceBase, dict[str, Any]]:
    """Resolve the same guarded Team base for HTTP and admitted Chat settings."""
    resolver = getattr(app.state, "effective_theme_resolver", None)
    if resolver is None:
        raise HTTPException(
            503,
            detail={"error": {"code": "personal_theme_store_unavailable"}},
        )

    for _ in range(2):
        raw_policy_before = await team_store.get(user.team_id, KEY_TEAM_THEME_POLICY)
        state_before = await resolver.read_state(user.team_id)
        bundle = await resolver.resolve(
            team_id=user.team_id,
            team_policy=_parse_team_policy(raw_policy_before, team_id=user.team_id),
            user_config={"version": 1},
            allow_custom=await _can_render_custom_branding_for_app(app, user.team_id),
            render_safe_mode=is_team_branding_render_safe_mode(),
        )
        raw_policy_after = await team_store.get(user.team_id, KEY_TEAM_THEME_POLICY)
        state_after = await resolver.read_state(user.team_id)
        if raw_policy_before != raw_policy_after or state_before != state_after:
            continue

        selected_revision_id = None if bundle.revision_id == "legacy" else bundle.revision_id
        selected_revision_checksum = bundle.revision_checksum
        if selected_revision_id is not None:
            selected_checksum = await resolver.revision_checksum(
                user.team_id,
                selected_revision_id,
            )
            if selected_checksum is None:
                continue
            selected_revision_checksum = selected_checksum
        source = bundle.payload.get("source", {})
        effective = bundle.payload["effective"]
        base = PersonalAppearanceBase(
            identity=f"{bundle.revision_id}:{bundle.revision_checksum}",
            color_scheme=copy.deepcopy(effective["colorScheme"]),
            effective={
                field: copy.deepcopy(effective[field])
                for field in (
                    "mode",
                    "typographyProfile",
                    "typeScale",
                    "density",
                    "radius",
                    "motion",
                )
            },
            sources={
                **{
                    field: str(source.get(field, "product"))
                    for field in (
                        "mode",
                        "typographyProfile",
                        "typeScale",
                        "density",
                        "radius",
                        "motion",
                    )
                },
                "colorScheme": str(source.get("colorScheme", source.get("colorProfile", "product"))),
            },
            team={
                "revisionId": bundle.payload["team"].get("revisionId"),
                "revisionNumber": bundle.revision_number,
            },
            guard=PersonalAppearanceBaseGuard(
                team_id=user.team_id,
                team_policy_raw=raw_policy_after,
                active_revision_id=(state_after.active_revision_id if state_after is not None else None),
                fallback_revision_id=(state_after.fallback_revision_id if state_after is not None else None),
                selected_revision_id=selected_revision_id,
                selected_revision_checksum=selected_revision_checksum,
            ),
        )
        return base, copy.deepcopy(bundle.payload)
    raise HTTPException(
        503,
        detail={"error": {"code": "personal_theme_store_unavailable"}},
    )


def _personal_headers(etag: str) -> dict[str, str]:
    return {
        "Cache-Control": "private, no-cache",
        "ETag": etag,
        "Vary": "Cookie, X-Team-Id, X-Bowhead-Public-Entry",
    }


async def _personal_body(
    request: Request,
    model_type: type[BaseModel],
) -> Any:
    raw = await request.body()
    if len(raw) > _PERSONAL_BODY_BYTES:
        raise HTTPException(
            400,
            detail={"error": {"code": "personal_theme_invalid_json"}},
        )
    try:
        payload = json.loads(raw)
        return model_type.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise HTTPException(
            400,
            detail={"error": {"code": "personal_theme_invalid_json"}},
        ) from exc


def _apply_headers(response: Response, headers: dict[str, str]) -> None:
    for key, value in headers.items():
        response.headers[key] = value


def _opaque_ascii(value: str, *, maximum: int) -> bool:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return 1 <= len(encoded) <= maximum and all(0x21 <= byte <= 0x7E for byte in encoded)


def _raise_personal_http(exc: Exception) -> NoReturn:
    if isinstance(exc, PersonalAppearanceBaseIncompatible):
        raise HTTPException(
            409,
            detail={"error": {"code": "personal_theme_base_incompatible"}},
        ) from exc
    if isinstance(exc, PersonalAppearanceCompileStale):
        raise HTTPException(
            409,
            detail={
                "error": {
                    "code": "personal_theme_compile_stale",
                    "currentEtag": exc.current_etag,
                    "rowVersion": exc.row_version,
                }
            },
        ) from exc
    if isinstance(exc, PersonalAppearancePreconditionRequired):
        raise HTTPException(
            428,
            detail={"error": {"code": "personal_theme_precondition_required"}},
        ) from exc
    if isinstance(exc, PersonalAppearanceConflict):
        raise HTTPException(
            409,
            detail={
                "error": {
                    "code": "personal_theme_conflict",
                    "currentEtag": exc.current_etag,
                    "rowVersion": exc.row_version,
                }
            },
        ) from exc
    if isinstance(exc, PersonalAppearanceUndoExpired):
        raise HTTPException(
            410,
            detail={"error": {"code": "personal_theme_undo_expired"}},
        ) from exc
    if isinstance(exc, PersonalAppearanceUndoConflict):
        raise HTTPException(
            409,
            detail={"error": {"code": "personal_theme_undo_conflict"}},
        ) from exc
    if isinstance(exc, PersonalAppearanceValidationFailed):
        raise HTTPException(
            422,
            detail={
                "error": {
                    "code": "personal_theme_validation_failed",
                    "validation": exc.validation,
                }
            },
        ) from exc
    if isinstance(exc, PersonalAppearanceInvalidDocument):
        raise HTTPException(
            409,
            detail={"error": {"code": "personal_theme_conflict"}},
        ) from exc
    if isinstance(exc, PersonalThemeModelError):
        raise HTTPException(
            400,
            detail={"error": {"code": "personal_theme_invalid_json"}},
        ) from exc
    raise exc


def _personal_effective_payload(
    *,
    base_payload: dict[str, Any],
    personal_payload: dict[str, Any],
    legacy_user_config: dict[str, Any],
) -> dict[str, Any]:
    payload = copy.deepcopy(base_payload)
    personal_effective = personal_payload["effective"]
    for field in (
        "mode",
        "typographyProfile",
        "typeScale",
        "density",
        "radius",
        "motion",
        "colorScheme",
    ):
        payload["effective"][field] = copy.deepcopy(personal_effective[field])

    overrides = personal_payload["overrides"]
    compat: dict[str, Any] = {"version": 1}
    for field in (
        "mode",
        "typographyProfile",
        "typeScale",
        "density",
        "radius",
        "motion",
    ):
        if field in overrides:
            compat[field] = copy.deepcopy(overrides[field])
    accent = overrides.get("accent")
    if accent is not None:
        profile_id = accent["id"] if accent["kind"] == "preset" else "bowhead"
        compat["colorProfile"] = {"type": "preset", "id": profile_id}
        payload["effective"]["colorProfile"] = copy.deepcopy(compat["colorProfile"])
    if "chartPalette" in legacy_user_config:
        compat["chartPalette"] = legacy_user_config["chartPalette"]
    payload["userConfig"] = compat

    locks = payload.get("locks")
    if isinstance(locks, dict):
        locks.pop("colorProfile", None)
        locks.pop("colorScheme", None)
    source = payload.get("source")
    if isinstance(source, dict):
        for field in (
            "mode",
            "typographyProfile",
            "typeScale",
            "density",
            "radius",
            "motion",
        ):
            source[field] = personal_payload["sources"][field]
        source["colorProfile"] = personal_payload["sources"]["accent"]
        source["colorScheme"] = personal_payload["sources"]["accent"]
    return payload


async def _can_render_custom_branding(request: Request, team_id: str) -> bool:
    return await _can_render_custom_branding_for_app(request.app, team_id)


async def _can_render_custom_branding_for_app(app: FastAPI, team_id: str) -> bool:
    entitlements = getattr(app.state, "entitlements", None)
    deployment_mode = getattr(app.state, "deployment_mode", "saas")
    if entitlements is None:
        return deployment_mode != "local"
    try:
        snapshot = await entitlements.get(team_id)
        if snapshot.health not in {"active", "grace"}:
            return False
        return entitlements.is_feature_licensed_from_snapshot(
            snapshot,
            "custom_branding",
        )
    except Exception:
        logger.exception(
            "theme.effective.entitlement_unavailable team_id=%s",
            team_id,
        )
        return False


def _effective_etag(
    *,
    team_id: str,
    revision_id: str,
    revision_checksum: str,
    raw_user_config: str | None,
    user_setting_updated_at: str | None,
    chart_style_material: str = "",
    personal_material: str = "",
    strong: bool = False,
) -> str:
    user_hash = hashlib.sha256((raw_user_config or "").encode()).hexdigest()[:16]
    chart_style_hash = hashlib.sha256(chart_style_material.encode()).hexdigest()[:16]
    value = (
        f"{team_id}:{revision_id}:{revision_checksum}:{user_setting_updated_at or 'none'}:"
        f"{user_hash}:{chart_style_hash}:{personal_material}:{RESOLVER_SCHEMA_VERSION}"
    )
    digest = hashlib.sha256(value.encode()).hexdigest()
    prefix = "" if strong else "W/"
    return f'{prefix}"effective:{digest}"'
