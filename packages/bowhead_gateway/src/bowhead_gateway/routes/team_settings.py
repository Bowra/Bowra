"""Shared team settings for language, chart styles and retention."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from bowhead_core.agents.analysis_depth import (
    DEPTH_LABELS,
    KEY_TEAM_ANALYSIS_DEPTH,
    normalize_depth,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.chart_style import (
    KEY_TEAM_CHART_STYLE_POLICY,
    ChartStyleError,
    chart_style_policy_etag,
    chart_style_setting_version,
    default_team_chart_style_policy,
    normalize_team_chart_style_policy,
    parse_team_chart_style_policy,
)
from bowhead_core.chart_style_models import ChartStyleDomainError
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.chart_style_uow import ChartStyleUnitOfWork
from bowhead_core.chart_style_v2 import (
    CHART_STYLE_CONTRACT_HEADER,
    chart_style_policy_etag_v2,
    default_team_chart_style_policy_v2,
    parse_team_chart_style_policy_v2,
)
from bowhead_core.reference_retention_policy import (
    ReferenceRetentionPolicyError,
    get_retention_policy,
    set_retention_policy,
)
from bowhead_core.team_language_policy import (
    KEY_TEAM_LANGUAGE_POLICY,
    TeamLanguagePolicyError,
    normalize_team_language_policy,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core import agent_run_limits as run_limits
from bowhead_core import execution_limits
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.conditional_headers import entity_tags_match
from bowhead_gateway.routes.auth import _current_user, _require_team_role_dep
from bowhead_gateway.routes.auth import _store as _user_store

if TYPE_CHECKING:
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_store import User, UserStore

router = APIRouter(prefix="/api/v1/team-settings", tags=["team-settings"])

_REQUIRE_TEAM_CHART_STYLE_ADMIN = _require_team_role_dep("owner", "team_admin")
_REQUIRE_TEAM_ANALYSIS_DEPTH_ADMIN = _require_team_role_dep("owner", "team_admin")
_REQUIRE_TEAM_LANGUAGE_ADMIN = _require_team_role_dep("owner", "team_admin")
_REQUIRE_TEAM_REFERENCE_RETENTION_ADMIN = _require_team_role_dep("owner", "team_admin")
_REQUIRE_TEAM_RUN_LIMITS_ADMIN = _require_team_role_dep("owner", "team_admin")


class TeamReferenceRetentionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    days: int = Field(ge=1, le=180)
    expected_version: int = Field(ge=0)


class TeamReferenceRetentionView(BaseModel):
    days: int
    version: int


@router.get("/reference-retention", response_model=TeamReferenceRetentionView)
async def get_team_reference_retention(request: Request, response: Response, user: User = Depends(_current_user)) -> TeamReferenceRetentionView:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        policy = await get_retention_policy(_user_store(request).db_path, team_id=user.team_id, user_id=user.id)
    except ReferenceRetentionPolicyError as exc:
        raise HTTPException(exc.status, detail={"error": {"code": exc.code}}) from exc
    return TeamReferenceRetentionView(days=policy.days, version=policy.version)


@router.put("/reference-retention", response_model=TeamReferenceRetentionView, dependencies=[Depends(require_not_readonly)])
async def put_team_reference_retention(
    body: TeamReferenceRetentionBody,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_TEAM_REFERENCE_RETENTION_ADMIN),
) -> TeamReferenceRetentionView:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        policy = await set_retention_policy(
            _user_store(request).db_path, team_id=user.team_id, user_id=user.id, days=body.days, expected_version=body.expected_version
        )
    except ReferenceRetentionPolicyError as exc:
        raise HTTPException(exc.status, detail={"error": {"code": exc.code}}) from exc
    return TeamReferenceRetentionView(days=policy.days, version=policy.version)


class TeamSettingsValue(BaseModel):
    value: str | None = Field(None, description="Team setting value as a compact JSON string")


class TeamChartStylePolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    defaultPresetId: str
    enabledPresetIds: list[str]


class TeamChartStylePolicyV2Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2]
    defaultStyleId: str
    enabledStyleIds: list[str]
    fallbackBuiltInPresetId: str


class TeamChartStylePolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: TeamChartStylePolicyBody
    etag: str


class TeamChartStylePolicyV2View(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: TeamChartStylePolicyV2Body
    etag: str
    registryVersion: int
    builtInRegistryChecksum: str


def _team_settings_store(request: Request) -> TeamSettingsStore:
    store = getattr(request.app.state, "team_settings_store", None)
    if store is None:
        raise HTTPException(503, "team_settings_store not configured")
    return store


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


def _chart_style_uow(request: Request) -> ChartStyleUnitOfWork:
    value = getattr(request.app.state, "chart_style_uow", None)
    if not isinstance(value, ChartStyleUnitOfWork):
        raise HTTPException(503, "chart_style_uow not configured")
    return value


def _raise_chart_style_domain(exc: ChartStyleDomainError) -> NoReturn:
    detail: dict[str, Any] = {"code": exc.code}
    detail.update(exc.details)
    raise HTTPException(
        exc.status_code,
        detail={"error": detail},
    ) from exc


def _sha256_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chart_style_changed_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [
        key
        for key in (
            "defaultPresetId",
            "enabledPresetIds",
            "defaultStyleId",
            "enabledStyleIds",
            "fallbackBuiltInPresetId",
        )
        if before.get(key) != after.get(key)
    ]


async def _audit_chart_style_policy_change(
    *,
    request: Request,
    user_store: UserStore,
    actor: User,
    before: dict[str, Any],
    after: dict[str, Any],
    before_raw: str | None,
    after_raw: str,
) -> None:
    await user_store.write_admin_audit_log(
        actor_user_id=actor.id,
        team_id=actor.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_settings",
        target_id=KEY_TEAM_CHART_STYLE_POLICY,
        target_team_id=actor.team_id,
        scope="team",
        payload={
            "setting_key": KEY_TEAM_CHART_STYLE_POLICY,
            "operation": "put",
            "changed_keys": _chart_style_changed_keys(before, after),
            "before_hash": _sha256_or_none(before_raw),
            "after_hash": _sha256_or_none(after_raw),
        },
        ip=request.client.host if request.client else None,
    )


# ─────────────────────────────────────────────────────────────
# analysis depth — agent thoroughness knob (fast / standard / thorough).
# Stored as a plain depth-key string; maps to internal budget profiles
# (analysis_depth.py). Same admin gate + audit shape as theme-policy.
# ─────────────────────────────────────────────────────────────
class AnalysisDepthValue(BaseModel):
    value: str = Field(..., description="fast | standard | thorough")
    label: str | None = Field(None, description="display label (read-only)")


async def _audit_analysis_depth_change(
    *,
    request: Request,
    user_store: UserStore,
    actor: User,
    operation: str,
    before: str | None,
    after: str | None,
) -> None:
    await user_store.write_admin_audit_log(
        actor_user_id=actor.id,
        team_id=actor.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_settings",
        target_id=KEY_TEAM_ANALYSIS_DEPTH,
        target_team_id=actor.team_id,
        scope="team",
        payload={
            "setting_key": KEY_TEAM_ANALYSIS_DEPTH,
            "operation": operation,
            "before": before,
            "after": after,
        },
        ip=request.client.host if request.client else None,
    )


@router.get("/analysis-depth", response_model=AnalysisDepthValue)
async def get_analysis_depth_setting(
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user: User = Depends(_current_user),
) -> AnalysisDepthValue:
    raw = await team_store.get(user.team_id, KEY_TEAM_ANALYSIS_DEPTH)
    depth = normalize_depth(raw)  # unknown/unset → standard
    return AnalysisDepthValue(value=depth, label=DEPTH_LABELS.get(depth))


@router.put(
    "/analysis-depth",
    response_model=AnalysisDepthValue,
    dependencies=[Depends(require_not_readonly)],
)
async def set_analysis_depth_setting(
    body: AnalysisDepthValue,
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_ANALYSIS_DEPTH_ADMIN),
) -> AnalysisDepthValue:
    depth = normalize_depth(body.value)
    if depth != (body.value or "").strip().lower():
        raise HTTPException(400, "value must be one of: fast, standard, thorough")
    before = await team_store.get(user.team_id, KEY_TEAM_ANALYSIS_DEPTH)
    await team_store.set(user.team_id, KEY_TEAM_ANALYSIS_DEPTH, depth, updated_by=user.id)
    await _audit_analysis_depth_change(
        request=request,
        user_store=user_store,
        actor=user,
        operation="put",
        before=before,
        after=depth,
    )
    return AnalysisDepthValue(value=depth, label=DEPTH_LABELS.get(depth))


@router.delete("/analysis-depth", dependencies=[Depends(require_not_readonly)])
async def delete_analysis_depth_setting(
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_ANALYSIS_DEPTH_ADMIN),
) -> dict[str, Any]:
    before = await team_store.get(user.team_id, KEY_TEAM_ANALYSIS_DEPTH)
    await team_store.delete(user.team_id, KEY_TEAM_ANALYSIS_DEPTH)
    await _audit_analysis_depth_change(
        request=request,
        user_store=user_store,
        actor=user,
        operation="delete",
        before=before,
        after=None,
    )
    return {"ok": True, "key": KEY_TEAM_ANALYSIS_DEPTH}


@router.get(
    "/chart-style-policy",
    response_model=TeamChartStylePolicyView | TeamChartStylePolicyV2View,
)
async def get_chart_style_policy(
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user: User = Depends(_REQUIRE_TEAM_CHART_STYLE_ADMIN),
) -> TeamChartStylePolicyView | TeamChartStylePolicyV2View | dict[str, Any]:
    raw = await team_store.get(user.team_id, KEY_TEAM_CHART_STYLE_POLICY)
    if _chart_style_contract(request) == 2:
        try:
            policy_v2 = parse_team_chart_style_policy_v2(raw) if raw is not None else default_team_chart_style_policy_v2()
        except ChartStyleError:
            policy_v2 = default_team_chart_style_policy_v2()
        etag_v2 = chart_style_policy_etag_v2(raw)
        uow = _chart_style_uow(request)
        version = await uow.store.registry_version(team_id=user.team_id)
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers["ETag"] = etag_v2
        response.headers["Vary"] = CHART_STYLE_CONTRACT_HEADER
        return {
            "policy": policy_v2,
            "etag": etag_v2,
            "registryVersion": version,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
        }
    try:
        policy = parse_team_chart_style_policy(raw) if raw is not None else default_team_chart_style_policy()
    except ChartStyleError:
        # A corrupt stored row remains addressable by its ETag so an admin can
        # repair it without a destructive read-time migration.
        policy = default_team_chart_style_policy()
    etag = chart_style_policy_etag(raw)
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["ETag"] = etag
    return TeamChartStylePolicyView(
        policy=TeamChartStylePolicyBody(**policy),
        etag=etag,
    )


@router.put(
    "/chart-style-policy",
    response_model=TeamChartStylePolicyView | TeamChartStylePolicyV2View,
    dependencies=[Depends(require_not_readonly)],
)
async def set_chart_style_policy(
    body: TeamChartStylePolicyBody | TeamChartStylePolicyV2Body,
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_CHART_STYLE_ADMIN),
) -> TeamChartStylePolicyView | TeamChartStylePolicyV2View | dict[str, Any]:
    if_match = request.headers.get("If-Match")
    if _chart_style_contract(request) == 2:
        if not isinstance(body, TeamChartStylePolicyV2Body):
            raise HTTPException(
                400,
                detail={"error": {"code": "chart_style_policy_invalid"}},
            )
        try:
            result = await _chart_style_uow(request).set_policy_v2(
                team_id=user.team_id,
                actor_id=user.id,
                policy=body.model_dump(),
                if_match=if_match,
            )
        except ChartStyleDomainError as exc:
            _raise_chart_style_domain(exc)
        except ChartStyleError as exc:
            raise HTTPException(
                400,
                detail={"error": {"code": "chart_style_policy_invalid"}},
            ) from exc
        before_policy: dict[str, Any] = default_team_chart_style_policy_v2()
        if result.before_raw is not None:
            with suppress(ChartStyleError):
                before_policy = parse_team_chart_style_policy_v2(result.before_raw)
        after_raw = json.dumps(
            result.policy,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        await _audit_chart_style_policy_change(
            request=request,
            user_store=user_store,
            actor=user,
            before=before_policy,
            after=result.policy,
            before_raw=result.before_raw,
            after_raw=after_raw,
        )
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers["ETag"] = result.policy_etag
        response.headers["Vary"] = CHART_STYLE_CONTRACT_HEADER
        return result.body()
    if not if_match:
        raise HTTPException(
            428,
            detail={"error": {"code": "chart_style_policy_precondition_required"}},
        )
    try:
        normalized = normalize_team_chart_style_policy(json.dumps(body.model_dump(), ensure_ascii=True, separators=(",", ":")))
        policy = parse_team_chart_style_policy(normalized)
    except ChartStyleError as exc:
        raise HTTPException(400, str(exc)) from exc

    before_raw = await team_store.get(user.team_id, KEY_TEAM_CHART_STYLE_POLICY)
    try:
        before_version = chart_style_setting_version(before_raw) if before_raw is not None else None
    except ChartStyleError:
        before_version = None
    if before_version == 2:
        raise HTTPException(
            409,
            detail={"error": {"code": "chart_style_policy_requires_newer_client"}},
        )
    before_policy = default_team_chart_style_policy()
    if before_raw is not None:
        with suppress(ChartStyleError):
            before_policy = parse_team_chart_style_policy(before_raw)
    if not entity_tags_match(if_match, chart_style_policy_etag(before_raw)):
        raise _chart_style_conflict(before_raw, before_policy)
    updated = await team_store.compare_and_set(
        user.team_id,
        KEY_TEAM_CHART_STYLE_POLICY,
        expected_value=before_raw,
        value=normalized,
        updated_by=user.id,
    )
    if not updated:
        current_raw = await team_store.get(user.team_id, KEY_TEAM_CHART_STYLE_POLICY)
        try:
            current_policy = parse_team_chart_style_policy(current_raw) if current_raw is not None else default_team_chart_style_policy()
        except ChartStyleError:
            current_policy = default_team_chart_style_policy()
        raise _chart_style_conflict(current_raw, current_policy)

    await _audit_chart_style_policy_change(
        request=request,
        user_store=user_store,
        actor=user,
        before=before_policy,
        after=policy,
        before_raw=before_raw,
        after_raw=normalized,
    )
    etag = chart_style_policy_etag(normalized)
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["ETag"] = etag
    return TeamChartStylePolicyView(
        policy=TeamChartStylePolicyBody(**policy),
        etag=etag,
    )


def _chart_style_conflict(
    raw: str | None,
    policy: dict[str, Any],
) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "error": {
                "code": "chart_style_policy_conflict",
                "current": {
                    "policy": policy,
                    "etag": chart_style_policy_etag(raw),
                },
            }
        },
    )


async def _audit_language_policy_change(
    *,
    request: Request,
    user_store: UserStore,
    actor: User,
    operation: str,
    before: str | None,
    after: str | None,
) -> None:
    await user_store.write_admin_audit_log(
        actor_user_id=actor.id,
        team_id=actor.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_settings",
        target_id=KEY_TEAM_LANGUAGE_POLICY,
        target_team_id=actor.team_id,
        scope="team",
        payload={
            "setting_key": KEY_TEAM_LANGUAGE_POLICY,
            "operation": operation,
            "before": before,
            "after": after,
        },
        request=request,
    )


@router.get("/language-policy", response_model=TeamSettingsValue)
async def get_language_policy(
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user: User = Depends(_current_user),
) -> TeamSettingsValue:
    """读团队输出语言策略。任何成员可读——前端要据此显示"由团队设定"。"""
    value = await team_store.get(user.team_id, KEY_TEAM_LANGUAGE_POLICY)
    return TeamSettingsValue(value=value)


@router.put(
    "/language-policy",
    response_model=TeamSettingsValue,
    dependencies=[Depends(require_not_readonly)],
)
async def set_language_policy(
    body: TeamSettingsValue,
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_LANGUAGE_ADMIN),
) -> TeamSettingsValue:
    if body.value is None:
        raise HTTPException(400, "value is required")
    try:
        normalized = normalize_team_language_policy(body.value)
    except TeamLanguagePolicyError as exc:
        raise HTTPException(400, str(exc)) from exc

    before = await team_store.get(user.team_id, KEY_TEAM_LANGUAGE_POLICY)
    await team_store.set(user.team_id, KEY_TEAM_LANGUAGE_POLICY, normalized, updated_by=user.id)
    await _audit_language_policy_change(
        request=request,
        user_store=user_store,
        actor=user,
        operation="put",
        before=before,
        after=normalized,
    )
    return TeamSettingsValue(value=normalized)


@router.delete("/language-policy", dependencies=[Depends(require_not_readonly)])
async def delete_language_policy(
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_LANGUAGE_ADMIN),
) -> dict[str, Any]:
    before = await team_store.get(user.team_id, KEY_TEAM_LANGUAGE_POLICY)
    await team_store.delete(user.team_id, KEY_TEAM_LANGUAGE_POLICY)
    await _audit_language_policy_change(
        request=request,
        user_store=user_store,
        actor=user,
        operation="delete",
        before=before,
        after=None,
    )
    return {"ok": True, "key": KEY_TEAM_LANGUAGE_POLICY}


# ─────────────────────────────────────────────────────────────
# Agent run limits (FEATURE_AGENT_RUN_LIMITS): turn time, subagent steps, tool calls.
# ─────────────────────────────────────────────────────────────
class AgentRunLimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated by bowhead_core.agent_run_limits.validate so errors name the field and range
    # (pydantic's lax int would also accept booleans).
    turn_time_limit_s: Any
    step_limit: Any
    tool_call_limit: Any
    model_call_time_limit_s: Any = None
    expected_version: str = Field(min_length=1, max_length=64)


def _run_limits_version(raw: str | None) -> str:
    return "default" if raw is None else hashlib.sha256(raw.encode()).hexdigest()[:16]


def _require_run_limits_feature() -> None:
    if not run_limits.agent_run_limits_enabled():
        raise HTTPException(404, detail={"error": {"code": "not_found"}})


def _run_limits_view(raw: str | None) -> dict[str, Any]:
    limits = run_limits.parse_stored(raw)
    return {
        "turn_time_limit_s": limits.turn_time_limit_s,
        "step_limit": limits.step_limit,
        "tool_call_limit": limits.tool_call_limit,
        "model_call_time_limit_s": limits.model_call_time_limit_s,
        "wrap_up_reserve_s": limits.wrap_up_reserve_s,
        "is_default": raw is None,
        "version": _run_limits_version(raw),
        "defaults": {
            "turn_time_limit_s": run_limits.DEFAULT_LIMITS.turn_time_limit_s,
            "step_limit": run_limits.DEFAULT_LIMITS.step_limit,
            "tool_call_limit": run_limits.DEFAULT_LIMITS.tool_call_limit,
            "model_call_time_limit_s": run_limits.DEFAULT_LIMITS.model_call_time_limit_s,
        },
        "ranges": {
            "turn_time_limit_s": {"min": run_limits.MIN_TURN_TIME_LIMIT_S, "max": run_limits.MAX_TURN_TIME_LIMIT_S},
            "step_limit": {"min": 1, "max": run_limits.MAX_STEP_LIMIT},
            "tool_call_limit": {"min": 1, "max": run_limits.MAX_TOOL_CALL_LIMIT},
            "model_call_time_limit_s": {"min": run_limits.MIN_MODEL_CALL_TIME_LIMIT_S, "max": run_limits.MAX_MODEL_CALL_TIME_LIMIT_S},
        },
    }


async def _audit_run_limits_change(
    *, request: Request, user_store: UserStore, actor: User, operation: str, before: str | None, after: str | None
) -> None:
    await user_store.write_admin_audit_log(
        actor_user_id=actor.id,
        team_id=actor.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_settings",
        target_id=run_limits.SETTINGS_KEY,
        target_team_id=actor.team_id,
        scope="team",
        payload={"setting_key": run_limits.SETTINGS_KEY, "operation": operation, "before": before, "after": after},
        ip=request.client.host if request.client else None,
    )


@router.get("/agent-run-limits")
async def get_agent_run_limits(
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user: User = Depends(_REQUIRE_TEAM_RUN_LIMITS_ADMIN),
) -> dict[str, Any]:
    _require_run_limits_feature()
    return _run_limits_view(await team_store.get(user.team_id, run_limits.SETTINGS_KEY))


@router.put("/agent-run-limits", dependencies=[Depends(require_not_readonly)])
async def put_agent_run_limits(
    body: AgentRunLimitsBody,
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_RUN_LIMITS_ADMIN),
) -> dict[str, Any]:
    _require_run_limits_feature()
    before = await team_store.get(user.team_id, run_limits.SETTINGS_KEY)
    payload = body.model_dump(exclude={"expected_version"})
    if "model_call_time_limit_s" not in body.model_fields_set:
        payload["model_call_time_limit_s"] = run_limits.parse_stored(before).model_call_time_limit_s
    try:
        limits = run_limits.validate(payload)
    except run_limits.AgentRunLimitsInvalid as exc:
        raise HTTPException(
            422,
            detail={"error": {"code": "agent_run_limits_invalid", "field": exc.field, "min": exc.minimum, "max": exc.maximum}},
        ) from exc
    if _run_limits_version(before) != body.expected_version:
        raise HTTPException(409, detail={"error": {"code": "agent_run_limits_conflict"}})
    after = limits.to_json()
    if not await team_store.compare_and_set(user.team_id, run_limits.SETTINGS_KEY, expected_value=before, value=after, updated_by=user.id):
        raise HTTPException(409, detail={"error": {"code": "agent_run_limits_conflict"}})
    await _audit_run_limits_change(request=request, user_store=user_store, actor=user, operation="put", before=before, after=after)
    return _run_limits_view(after)


@router.delete("/agent-run-limits", dependencies=[Depends(require_not_readonly)])
async def delete_agent_run_limits(
    request: Request,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_RUN_LIMITS_ADMIN),
    expected_version: str | None = None,
) -> dict[str, Any]:
    _require_run_limits_feature()
    before = await team_store.get(user.team_id, run_limits.SETTINGS_KEY)
    if expected_version is not None and _run_limits_version(before) != expected_version:
        raise HTTPException(409, detail={"error": {"code": "agent_run_limits_conflict"}})
    if not await team_store.compare_and_set(user.team_id, run_limits.SETTINGS_KEY, expected_value=before, value=None, updated_by=user.id):
        raise HTTPException(409, detail={"error": {"code": "agent_run_limits_conflict"}})
    await _audit_run_limits_change(request=request, user_store=user_store, actor=user, operation="delete", before=before, after=None)
    return _run_limits_view(None)


class ExecutionLimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null restores deployment defaults, using the same optimistic concurrency guard.
    limits: dict[str, Any] | None
    expected_version: str = Field(min_length=1, max_length=64)


def _execution_limits_view(raw: str | None) -> dict[str, Any]:
    from dataclasses import asdict

    from bowhead_core.html_preview_policy import MAX_HTML_PREVIEW_BYTES

    return {
        **asdict(execution_limits.parse_stored(raw)),
        "defaults": asdict(execution_limits.defaults()),
        "ranges": {
            "sandbox_default_timeout_s": {"min": 5, "max": execution_limits.sandbox_ceiling_s()},
            "sandbox_max_timeout_s": {"min": 5, "max": execution_limits.sandbox_ceiling_s()},
            "html_max_bytes": {"min": 1, "max": MAX_HTML_PREVIEW_BYTES},
        },
        "is_default": raw in (None, "null"),
        "version": _run_limits_version(raw),
    }


@router.get("/execution-limits")
async def get_execution_limits(
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user: User = Depends(_REQUIRE_TEAM_RUN_LIMITS_ADMIN),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return _execution_limits_view(await team_store.get(user.team_id, execution_limits.SETTINGS_KEY))


@router.put("/execution-limits", dependencies=[Depends(require_not_readonly)])
async def put_execution_limits(
    body: ExecutionLimitsBody,
    request: Request,
    response: Response,
    team_store: TeamSettingsStore = Depends(_team_settings_store),
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_RUN_LIMITS_ADMIN),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    before = await team_store.get(user.team_id, execution_limits.SETTINGS_KEY)
    try:
        if body.limits is not None and set(body.limits) != {"sandbox_default_timeout_s", "sandbox_max_timeout_s", "html_max_bytes"}:
            raise HTTPException(422, detail={"error": {"code": "execution_limits_fields_invalid"}})
        after = "null" if body.limits is None else execution_limits.validate(body.limits).to_json()
    except execution_limits.ExecutionLimitsInvalid as exc:
        raise HTTPException(
            422, detail={"error": {"code": "execution_limits_invalid", "field": exc.field, "min": exc.minimum, "max": exc.maximum}}
        ) from exc
    if _run_limits_version(before) != body.expected_version or not await team_store.compare_and_set(
        user.team_id, execution_limits.SETTINGS_KEY, expected_value=before, value=after, updated_by=user.id
    ):
        raise HTTPException(409, detail={"error": {"code": "execution_limits_conflict"}})
    await user_store.write_admin_audit_log(
        actor_user_id=user.id,
        team_id=user.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_settings",
        target_id=execution_limits.SETTINGS_KEY,
        target_team_id=user.team_id,
        scope="team",
        payload={
            "setting_key": execution_limits.SETTINGS_KEY,
            "operation": "reset" if body.limits is None else "put",
            "before": before,
            "after": after,
        },
        ip=request.client.host if request.client else None,
    )
    return _execution_limits_view(after)
