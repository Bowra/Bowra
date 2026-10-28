"""cca P3 — team subagent authoring CRUD (tech-design §4.1).

A member assembles a SUBSET of the team's existing teammcp.* tools (+ native analyst
tools) into a routable expert. **Member-open** (per the locked scope — any member
may create; safe via the zero-escalation binding, S1): authz is the member-level
``_current_user`` (DB-fresh ``user.team_id``), NOT the team-admin dep, so a caller can
only ever touch their OWN team's subagents (IDOR-safe by construction — the store
methods are team_id-keyed). Mutating routes keep ``require_not_readonly`` (a frozen
team shouldn't expand its agent surface).

Release A keeps the existing HTTP and validation contract while routing every
mutation through ``TeamSubagentStore``. Definition, initial team-wide grant,
delete cascade, and route audit therefore commit or roll back together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal

import structlog
from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentRevisionError,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.custom_subagent_store import RESERVED_NAMES
from bowhead_core.share_service import (
    content_sharing_enabled,
)
from bowhead_core.subagent import PROFILES
from bowhead_core.subagent_authoring_catalog import (
    _HIDDEN_NATIVE_NAMES as _HIDDEN_NATIVE_NAMES,
)
from bowhead_core.subagent_authoring_catalog import (
    SubagentAuthoringCatalogService,
)
from bowhead_core.subagent_authoring_rollout import resolve_subagent_authoring_capabilities
from bowhead_core.team_subagent_access import (
    TeamSubagentAccessDecision,
    TeamSubagentAccessFacts,
    TeamSubagentAccessResolver,
    resolve_team_subagent_access,
)
from bowhead_core.team_subagent_store import (
    TeamSubagentAudienceForbiddenError,
    TeamSubagentAudienceInvalidError,
    TeamSubagentDeleteDependencyError,
    TeamSubagentNameConflictError,
    TeamSubagentOwnerInactiveError,
    TeamSubagentRevisionWriterRequiredError,
    TeamSubagentStore,
    TeamSubagentVersionConflictError,
)
from bowhead_core.team_subagent_validation import (
    TeamSubagentValidationError,
)
from bowhead_core.team_subagent_validation import (
    validate_common as _core_validate_common,
)
from bowhead_core.team_subagent_validation import (
    validate_model as _core_validate_model,
)
from bowhead_core.team_subagent_validation import (
    validate_name as _core_validate_name,
)
from bowhead_core.team_subagent_validation import (
    validate_output_schema as _core_validate_output_schema,
)
from bowhead_core.team_subagent_validation import (
    validate_skills as _core_validate_skills,
)
from bowhead_core.team_subagent_validation import (
    validate_tool_refs as _core_validate_tool_refs,
)
from bowhead_core.tool_name_canon import canonicalize_tool_refs
from bowhead_core.user_store import (
    TeamSubagent,
    TeamSubagentSummaryRecord,
    User,
    UserStore,
    _id,
    _now,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import (
    assert_feature_licensed,
    assert_not_readonly,
    is_feature_licensed_request,
    require_not_readonly,
)
from bowhead_gateway.conditional_headers import entity_tags_match
from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.routes.auth import _current_user, _store

router = APIRouter()
log = structlog.get_logger(__name__)

_VISIBILITY = {"team_wide", "creator_only"}
_STATUS = {"enabled", "disabled"}


def _mutation_store(request: Request, legacy_store: UserStore) -> TeamSubagentStore:
    store = getattr(request.app.state, "team_subagent_schema_store", None)
    if isinstance(store, TeamSubagentStore):
        return store
    return TeamSubagentStore(legacy_store.db_path)


def _subagent_etag(subagent_id: str, version: int) -> str:
    return f'W/"{subagent_id}:{version}"'


_DEFINITION_FIELDS: Final = (
    "display_name",
    "description",
    "system_prompt",
    "tool_refs",
    "model",
    "max_iterations",
    "skills",
    "output_schema",
)


async def _require_archive_path(
    mutation_store: TeamSubagentStore,
    *,
    subagent_id: str,
) -> None:
    """Reject hard deletion before version checks when release governance applies.

    Return agent_archive_required and the archive route. A stale-version response
    would incorrectly suggest that refreshing could make hard deletion valid.
    """
    if not await mutation_store.release_semantics_active():
        return
    raise HTTPException(
        409,
        detail={
            "error": {
                "code": "agent_archive_required",
                "message": "该 Agent 由发布流程管理，不能硬删；请改为归档（可再恢复）。",
                "details": {
                    "next": [
                        f"POST /api/v1/agents/{subagent_id}/archive",
                        f"POST /api/v1/agents/{subagent_id}/restore",
                    ],
                },
            },
        },
    )


async def _require_release_writer_path(
    mutation_store: TeamSubagentStore,
    *,
    team_id: str,
    subagent_id: str,
    body: ConfigurationPatchBody,
) -> None:
    """Require the release writer before checking a governed definition's version.

    Match store admission order: definition changes to revisioned Agents return
    agent_revision_writer_required. Retrying another version cannot open a writer
    path that release governance has closed.
    """
    if not any(field in body.model_fields_set for field in _DEFINITION_FIELDS):
        return
    state = await mutation_store.release_definition_state(team_id=team_id, subagent_id=subagent_id)
    if state is None or state[0] != "revisioned":
        return
    raise HTTPException(
        409,
        detail={
            "error": {
                "code": "agent_revision_writer_required",
                "message": "该 Agent 的定义由发布流程管理，请走工作副本 → 发布候选 → 审批。",
                "details": {
                    "next": [
                        f"PATCH /api/v1/agents/{subagent_id}/working-copy",
                        f"POST /api/v1/agents/{subagent_id}/release-candidates",
                    ],
                },
            },
        },
    )


def _version_conflict(
    subagent_id: str,
    current_version: int,
) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "error": {
                "code": "subagent_version_conflict",
                "details": {
                    "current_version": current_version,
                    "etag": _subagent_etag(subagent_id, current_version),
                },
            },
        },
    )


def _require_expected_version(
    request: Request,
    *,
    subagent_id: str,
    current_version: int,
) -> int:
    value = request.headers.get("If-Match")
    if value is None:
        raise HTTPException(
            428,
            detail={
                "error": {
                    "code": "subagent_if_match_required",
                },
            },
        )
    if not entity_tags_match(value, _subagent_etag(subagent_id, current_version)):
        raise _version_conflict(subagent_id, current_version)
    return current_version


def _require_matching_manager_snapshot(
    subagent: TeamSubagent,
    decision: TeamSubagentAccessDecision,
) -> None:
    """Bind conditional entitlement checks to the same row version as authz."""
    if subagent.row_version != decision.row_version:
        raise _version_conflict(subagent.id, decision.row_version)


async def _team_skill_catalog(store: UserStore, team_id: str) -> list[dict[str, Any]]:
    """The team's ENABLED, ``team_wide`` skills as attachable auto-read SOPs. ``creator_only``
    is excluded on purpose: a non-creator runner of an team_wide subagent couldn't
    ``skill_read`` it (the resolver's visibility gate would 404), so offering it would make
    the picker lie about what's attachable."""
    return [
        {"name": sk.name, "display_name": sk.display_name, "description": sk.description}
        for sk in await store.list_team_skills(team_id, only_enabled=True)
        if sk.visibility == "team_wide"
    ]


def _validation_http(exc: TeamSubagentValidationError) -> HTTPException:
    if exc.code == "subagent_tool_not_selectable":
        return HTTPException(422, {"code": exc.code, "tool_ref": exc.tool_ref})
    return HTTPException(422, str(exc))


async def _validate_tool_refs(
    store: UserStore,
    team_id: str,
    actor_user_id: str,
    tool_refs: list[str],
    *,
    existing_tool_refs: list[str] | tuple[str, ...] = (),
) -> None:
    catalog = await SubagentAuthoringCatalogService().build(
        store=store,
        team_id=team_id,
        actor_user_id=actor_user_id,
        actor_active=True,
        # The HTTP entitlement gate runs immediately before this validator.
        mcp_entitled=True,
    )
    try:
        _core_validate_tool_refs(
            tool_refs,
            catalog=catalog,
            require_selectable=True,
            existing_tool_refs=existing_tool_refs,
        )
    except TeamSubagentValidationError as exc:
        raise _validation_http(exc) from exc


# STATIC floor of names a P3 team subagent may NOT take (S2-SEC-1 fix): the complete
# set of built-in + system-manifest agent names. Hardcoded so the reserved-name guard
# does NOT silently degrade if the multi-agent bootstrap fails (main.py swallows that
# failure → PROFILES would degrade to its fallback subset and a member could
# create name='kb-analyst' shadowing a real Lead callable_peer). PROFILES.keys() is
# UNION'd in at runtime as defense-in-depth (catches any future system manifest), but
# the floor is the load-bearing barrier S3's resolve/advertise leans on (route-3).
# Keep in sync with manifests/system/*.yaml + lead.yaml callable_peers.
_SYSTEM_RESERVED: frozenset[str] = frozenset(
    {
        "compute",
        "modeler",
        "debate",  # retired system capability; keep reserved to prevent revival
        "analyst",
        "echo",
        "lead",
        "consultant",
        "kb-analyst",
        "financial-analyst",
        "business-analyst",
        "compliance-analyst",
        "meta-agent",
        "narrative",
        "composer",
    }
)


def _reserved_names() -> set[str]:
    """Names a P3 team subagent may NOT take: the STATIC system/built-in floor
    (boot-independent) ∪ RESERVED_NAMES ∪ the live PROFILES keys (defense-in-depth).
    Reject-at-create so an team name can never shadow a system peer in the Lead's
    routing block (route-3) — even if the multi-agent bootstrap failed."""
    return set(RESERVED_NAMES) | _SYSTEM_RESERVED | set(PROFILES.keys())


def _public(sa: TeamSubagent) -> dict[str, Any]:
    """Legacy full definition used only inside Owner-authorized manager responses."""
    # C7 consolidation: project tool_refs to FE using **current names**. Stored references may precede renames,
    # while the tool picker classifies refs absent from the live catalog as staleSel, which canSubmit requires to be empty;
    # without normalization, opening edit forces users to remove that row before saving any changes.
    # Normalize only the **read projection**, changing no stored bytes (writes still persist submitted content).
    # Rules: docs/design/工具改名迁移.md.
    _refs = canonicalize_tool_refs(sa.tool_refs, source="team_subagent_api")
    return {
        "id": sa.id,
        "team_id": sa.team_id,
        "name": sa.name,
        "display_name": sa.display_name,
        "description": sa.description,
        "system_prompt": sa.system_prompt,
        "tool_refs": _refs,
        "tool_count": len(_refs),
        "max_iterations": sa.max_iterations,
        "skills": sa.skills,
        "model": sa.model,
        "output_schema": sa.output_schema,
        "visibility": sa.visibility,
        "status": sa.status,
        "created_by": sa.created_by,
        "created_at": sa.created_at,
        "updated_at": sa.updated_at,
        "origin_mode": sa.origin_mode,
        "source_draft_id": sa.source_draft_id,
    }


_ACTION_NAMES = (
    "view_consumer_detail",
    "view_manager_detail",
    "edit",
    "change_status",
    "manage_audience",
    "transfer_owner",
    "delete",
    "validate",
    "bring_to_web_chat",
    "govern",
)


def _disabled_actions(reason: str) -> tuple[dict[str, bool], dict[str, str]]:
    return (
        dict.fromkeys(_ACTION_NAMES, False),
        dict.fromkeys(_ACTION_NAMES, reason),
    )


def _actions_for(
    decision: TeamSubagentAccessDecision | None,
    *,
    writable: bool,
    write_reason: str | None,
) -> tuple[dict[str, bool], dict[str, str]]:
    """Map the canonical access decision to actions that actually exist in Release A."""
    if decision is None:
        return _disabled_actions("access_policy_unavailable")

    resolution = decision.resolution
    actions = dict.fromkeys(_ACTION_NAMES, False)
    actions["view_manager_detail"] = resolution.can_manage
    for name in ("edit", "change_status", "manage_audience", "delete"):
        actions[name] = resolution.can_manage and writable

    reasons: dict[str, str] = {}
    manager_reason = "not_owner" if not resolution.can_manage else (write_reason or "not_available")
    for name, allowed in actions.items():
        if allowed:
            continue
        if name == "view_consumer_detail":
            reasons[name] = "not_available_release_a"
        elif name in {
            "view_manager_detail",
            "edit",
            "change_status",
            "manage_audience",
            "delete",
        }:
            reasons[name] = manager_reason
        else:
            reasons[name] = "not_available_release_a"
    return actions, reasons


def _uses_team_tools(tool_refs: list[str]) -> bool:
    return any(ref.casefold().startswith("teammcp.") for ref in tool_refs)


def _requires_mcp_license(
    subagent: TeamSubagent | TeamSubagentSummaryRecord,
) -> bool:
    if isinstance(subagent, TeamSubagentSummaryRecord):
        return subagent.team_tool_count > 0
    return _uses_team_tools(subagent.tool_refs)


async def _require_tool_entitlement(
    request: Request,
    tool_refs: list[str],
) -> None:
    if _uses_team_tools(tool_refs):
        await assert_feature_licensed(request, "mcp")


async def _write_capability(
    request: Request,
    *,
    requires_mcp: bool,
) -> tuple[bool, str | None]:
    """Apply MCP licensing only to definitions that actually use teammcp.*."""
    if requires_mcp and not await is_feature_licensed_request(request, "mcp"):
        return False, "feature_unlicensed"
    try:
        await assert_not_readonly(request)
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            error = detail.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return False, error["code"]
        return False, "team_readonly"
    return True, None


def _active_audience_grants(grants: list[Any]) -> dict[str, list[Any]]:
    """Group effective user/group allows by resource without exposing deny records."""
    now = datetime.now(UTC)
    grouped: dict[str, list[Any]] = {}
    for grant in grants:
        if grant.effect != "allow" or grant.principal_type not in {"user", "group"}:
            continue
        if grant.expires_at:
            try:
                expires_at = datetime.fromisoformat(str(grant.expires_at).replace("Z", "+00:00"))
            except ValueError:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                continue
        grouped.setdefault(grant.resource_id, []).append(grant)
    return grouped


async def _audience_grants(
    request: Request,
    user: User,
    *,
    required: bool = False,
) -> dict[str, list[Any]]:
    access = getattr(request.app.state, "access_control_store", None)
    if access is None:
        if required:
            raise HTTPException(503, "subagent audience policy unavailable")
        return {}
    try:
        grants = await access.list_resource_grants(
            user.team_id,
            resource_kind="team_subagent",
        )
    except Exception as exc:
        log.warning(
            "subagent.audience_summary_failed",
            team_id=user.team_id,
            error_type=type(exc).__name__,
        )
        if required:
            raise HTTPException(503, "subagent audience policy unavailable") from None
        return {}
    return _active_audience_grants(grants)


async def _owner_names(
    store: UserStore,
    decisions: dict[str, TeamSubagentAccessDecision],
) -> dict[str, str]:
    owner_ids = [decision.owner_user_id for decision in decisions.values() if decision.owner_user_id]
    try:
        return await store.get_user_display_names_by_ids(owner_ids)
    except Exception:
        return {}


async def _audience_principal_names(
    request: Request,
    store: UserStore,
    user: User,
    grants: list[Any],
) -> dict[tuple[str, str], str]:
    """Resolve Owner-visible roster labels without changing access semantics."""
    names: dict[tuple[str, str], str] = {}
    user_ids = [str(grant.principal_id) for grant in grants if grant.principal_type == "user"]
    try:
        user_names = await store.get_user_display_names_by_ids(user_ids)
        names.update(
            {("user", user_id): display_name for user_id, display_name in user_names.items()},
        )
    except Exception as exc:
        log.warning(
            "subagent.audience_user_names_failed",
            team_id=user.team_id,
            principal_count=len(user_ids),
            error_type=type(exc).__name__,
        )

    group_ids = {str(grant.principal_id) for grant in grants if grant.principal_type == "group"}
    access = getattr(request.app.state, "access_control_store", None)
    if group_ids and access is not None:
        try:
            groups = await access.list_groups(user.team_id)
            names.update(
                {("group", group.id): group.name for group in groups if group.id in group_ids},
            )
        except Exception as exc:
            log.warning(
                "subagent.audience_group_names_failed",
                team_id=user.team_id,
                principal_count=len(group_ids),
                error_type=type(exc).__name__,
            )
    return names


def _audience_mode(
    sa: TeamSubagent | TeamSubagentSummaryRecord,
    decision: TeamSubagentAccessDecision | None,
    grants: list[Any],
) -> Literal["private", "custom", "team_wide"]:
    if decision is not None:
        mode = decision.facts.audience_mode
        if mode == "teamwide":
            return "team_wide"
        if mode in {"private", "custom"}:
            return mode
    if (sa.visibility or "team_wide") == "team_wide":
        return "team_wide"
    return "custom" if grants else "private"


def _summary(
    sa: TeamSubagentSummaryRecord,
    *,
    decision: TeamSubagentAccessDecision | None,
    owner_names: dict[str, str],
    audience_grants: list[Any],
    writable: bool,
    write_reason: str | None,
) -> dict[str, Any]:
    """R0 field-safe list DTO. Full definitions are never read from this response."""
    owner_id = decision.owner_user_id if decision is not None else sa.created_by
    owner_active = decision.facts.owner_active if decision is not None else False
    user_count = sum(grant.principal_type == "user" for grant in audience_grants)
    group_count = sum(grant.principal_type == "group" for grant in audience_grants)
    actions, action_reasons = _actions_for(
        decision,
        writable=writable,
        write_reason=write_reason,
    )
    denial_reason = decision.resolution.denial_reason if decision is not None else "access_policy_unavailable"
    can_consume = bool(decision and decision.resolution.can_consume)
    # Keep the concurrency token aligned with the same database projection as
    # the displayed fields. A later access-policy read may observe a newer row;
    # publishing that newer version beside this older snapshot would let a
    # client submit an ETag for content it never saw.
    version = sa.row_version
    return {
        "schema_version": 1,
        "kind": "team_subagent_summary",
        "id": sa.id,
        "name": sa.name,
        "display_name": sa.display_name,
        "description": sa.description,
        "source": "team",
        "owner": {
            "id": owner_id,
            "display_name": owner_names.get(owner_id or "", owner_id),
            "status": ("active" if owner_active else ("inactive" if decision is not None else "unknown")),
        },
        "audience": _audience_mode(sa, decision, audience_grants),
        "audience_counts": {
            "users": user_count,
            "groups": group_count,
            "total": user_count + group_count,
        },
        "share_count": user_count + group_count,
        "visibility": sa.visibility,
        "status": sa.status,
        "lifecycle_state": (decision.facts.lifecycle_state if decision is not None else "active"),
        "integrity": {"status": "unknown", "issue_count": None},
        "availability": {
            "web": "unknown" if can_consume else "unavailable",
            "reason": ("validation_not_available_release_a" if can_consume else denial_reason),
        },
        "invocation_mode": "lead_routed",
        "tool_count": sa.tool_count,
        "skill_count": sa.skill_count,
        "capability_classes": {
            "team_tools": sa.team_tool_count,
            "native_tools": sa.tool_count - sa.team_tool_count,
            "skills": sa.skill_count,
        },
        "model": sa.model,
        "updated_at": sa.updated_at,
        "version": version,
        "etag": _subagent_etag(sa.id, version),
        "actions": actions,
        "action_reasons": action_reasons,
        "access_reason": (decision.resolution.access_reason if decision is not None else None),
    }


def _owner_manager_detail(
    sa: TeamSubagent,
    *,
    decision: TeamSubagentAccessDecision,
    owner_names: dict[str, str],
    audience_grants: list[Any],
    audience_principal_names: dict[tuple[str, str], str],
    writable: bool,
    write_reason: str | None,
) -> dict[str, Any]:
    """Full editable definition for the current active Owner only."""
    actions, action_reasons = _actions_for(
        decision,
        writable=writable,
        write_reason=write_reason,
    )
    owner_id = decision.owner_user_id
    return {
        "schema_version": 1,
        "kind": "team_owner_manager_detail",
        **_public(sa),
        "source": "team",
        "owner": {
            "id": owner_id,
            "display_name": owner_names.get(owner_id or "", owner_id),
            "status": "active" if decision.facts.owner_active else "inactive",
        },
        "audience": {
            "mode": _audience_mode(sa, decision, audience_grants),
            "roster": [
                {
                    "principal_type": grant.principal_type,
                    "principal_id": grant.principal_id,
                    "principal_name": audience_principal_names.get(
                        (
                            str(grant.principal_type),
                            str(grant.principal_id),
                        ),
                    ),
                    "permission": grant.permission,
                }
                for grant in audience_grants
            ],
        },
        "lifecycle_state": decision.facts.lifecycle_state,
        "version": sa.row_version,
        "etag": _subagent_etag(sa.id, sa.row_version),
        "actions": actions,
        "action_reasons": action_reasons,
    }


def _created_owner_decision(sa: TeamSubagent) -> TeamSubagentAccessDecision:
    """Project the owner proof established by the successful Create transaction."""
    facts = TeamSubagentAccessFacts(
        actor_active=True,
        is_owner=True,
        owner_active=True,
        is_team_governor=False,
        governance_action_allowed=False,
        audience_mode=("teamwide" if sa.visibility == "team_wide" else "private"),
        has_user_allow=False,
        has_group_allow=False,
        has_security_deny=False,
        lifecycle_state="active",
        status=sa.status,
    )
    return TeamSubagentAccessDecision(
        subagent_id=sa.id,
        owner_user_id=sa.created_by,
        facts=facts,
        resolution=resolve_team_subagent_access(facts),
        row_version=sa.row_version,
    )


def _validate_name(name: str) -> None:
    try:
        _core_validate_name(name, reserved_names=_reserved_names())
    except TeamSubagentValidationError as exc:
        raise _validation_http(exc) from exc


def _validate_model(model: str | None) -> None:
    try:
        _core_validate_model(model)
    except TeamSubagentValidationError as exc:
        raise _validation_http(exc) from exc


def _validate_output_schema(schema: dict[str, Any] | None) -> None:
    try:
        _core_validate_output_schema(schema)
    except TeamSubagentValidationError as exc:
        raise _validation_http(exc) from exc


def _validate_common(
    *,
    display_name: str = "",
    description: str,
    system_prompt: str,
    tool_refs: list[str],
    max_iterations: int,
    allow_empty_tools: bool = False,
) -> None:
    try:
        _core_validate_common(
            display_name=display_name,
            description=description,
            system_prompt=system_prompt,
            tool_refs=tool_refs,
            max_iterations=max_iterations,
            allow_empty_tools=allow_empty_tools,
        )
    except TeamSubagentValidationError as exc:
        raise _validation_http(exc) from exc


def _validate_skills(skills: list[str]) -> None:
    try:
        _core_validate_skills(skills)
    except TeamSubagentValidationError as exc:
        raise _validation_http(exc) from exc


class CreateBody(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    system_prompt: str = ""
    tool_refs: list[str] = Field(default_factory=list)
    max_iterations: int = 10
    skills: list[str] = Field(default_factory=list)
    visibility: str = "creator_only"
    model: str | None = None  # S4 — llm_agent_type (allowlisted); None = analyst
    output_schema: dict[str, Any] | None = None  # S4 §5 — payload JSON Schema gate


class ConfigurationPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    tool_refs: list[str] | None = None
    max_iterations: int | None = None
    skills: list[str] | None = None
    model: str | None = None  # S4
    output_schema: dict[str, Any] | None = None  # S4 §5


class PatchBody(ConfigurationPatchBody):
    """Deprecated combined mutation body kept for rolling-client compatibility."""

    status: str | None = None


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    acknowledged_warning_codes: list[str] = Field(
        default_factory=list,
        max_length=0,
    )


class AudienceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["private", "custom", "team_wide"]
    user_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)


def _team_override_fields(base: dict[str, Any]) -> tuple[str, ...]:
    """Fields a Team may change on a built-in Agent.

    With agent run limits on, the Team-wide step limit replaces per-Agent step limits, so
    ``max_iterations`` is neither offered nor accepted per Agent.
    """
    from bowhead_core.agent_run_limits import agent_run_limits_enabled

    fields = tuple(base.get("team_overrides", ()))
    return tuple(f for f in fields if f != "max_iterations") if agent_run_limits_enabled() else fields


@router.post("", dependencies=[Depends(require_not_readonly)])
async def create_subagent(
    body: CreateBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, private"
    _validate_name(body.name)
    if body.visibility not in _VISIBILITY:
        raise HTTPException(422, f"unknown visibility: {body.visibility!r}")
    _validate_common(
        display_name=body.display_name,
        description=body.description,
        system_prompt=body.system_prompt,
        tool_refs=body.tool_refs,
        max_iterations=body.max_iterations,
        allow_empty_tools=resolve_subagent_authoring_capabilities(user.team_id).text_only,
    )
    _validate_model(body.model)
    _validate_skills(body.skills)
    _validate_output_schema(body.output_schema)
    await _require_tool_entitlement(request, body.tool_refs)
    await _validate_tool_refs(store, user.team_id, user.id, body.tool_refs)
    # Preflight the response/cascade dependency before committing. A post-commit
    # roster read failure would otherwise return 503 after a successful create,
    # inviting a retry that can only conflict with the row already written.
    grouped_grants = await _audience_grants(request, user, required=True)
    sa = TeamSubagent(
        id=_id("osa"),
        team_id=user.team_id,
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        system_prompt=body.system_prompt,
        tool_refs=body.tool_refs,
        max_iterations=body.max_iterations,
        skills=body.skills,
        model=body.model,
        output_schema=body.output_schema,
        visibility=body.visibility,
        created_by=user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    try:
        access_store = getattr(request.app.state, "access_control_store", None)
        mutation_store = _mutation_store(request, store)
        revision_kwargs: dict[str, Any] = {}
        if await mutation_store.release_semantics_active():
            dependency_compiler = AgentDependencyCompiler(
                personal_skill_store=getattr(request.app.state, "skill_instance_store", None),
                team_skill_store=store,
            )
            exact_skills = await dependency_compiler.exact_grants_for_legacy_names(
                sa.skills,
                team_id=user.team_id,
                tool_refs=sa.tool_refs,
            )
            from bowhead_gateway.routes.agents import _revision_store

            revision_kwargs = {
                "revision_store": await _revision_store(request, user=user, store=store),
                "revision_definition": {
                    "max_iterations": sa.max_iterations,
                    "model": sa.model,
                    "output_schema": sa.output_schema,
                    "skill_grants": [grant.to_canonical_dict() for grant in exact_skills],
                    "system_prompt": sa.system_prompt,
                    "tool_refs": sa.tool_refs,
                },
                "revision_listing": {
                    "description": sa.description,
                    "display_name": sa.display_name or sa.name,
                },
                "revision_target_audience": AgentAudience(
                    "teamwide" if body.visibility == "team_wide" else "private",
                ),
            }
        await mutation_store.create_team_subagent(
            sa,
            initial_audience_access_store=(access_store if body.visibility == "team_wide" and content_sharing_enabled() else None),
            actor_user_id=user.id,
            audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
            audit_payload={"tool_count": len(sa.tool_refs)},
            require_active_owner=True,
            **revision_kwargs,
        )
    except TeamSubagentOwnerInactiveError as e:
        raise HTTPException(
            403,
            {"code": "team_membership_required"},
        ) from e
    except TeamSubagentNameConflictError as e:
        raise HTTPException(409, {"code": e.code}) from e
    except TeamSubagentRevisionWriterRequiredError as e:
        raise HTTPException(409, {"code": e.code}) from e
    except AgentPublicationBlockedError as e:
        raise HTTPException(
            422,
            {"code": e.code, "reason_codes": list(e.reason_codes), "blocked_items": list(e.blocked_items)},
        ) from e
    except (AgentRevisionError, ValueError) as e:
        raise HTTPException(422, {"code": getattr(e, "code", "agent_definition_invalid")}) from e
    # The store proved active actor == Owner under the same write lock as the
    # insert. Build the response from that commit snapshot: a post-commit
    # manager lookup could race member deactivation and misreport a successful
    # mutation as 403, inviting a duplicate retry.
    created = sa
    decision = _created_owner_decision(created)
    names = await _owner_names(store, {created.id: decision})
    writable, write_reason = await _write_capability(
        request,
        requires_mcp=_requires_mcp_license(created),
    )
    payload = _owner_manager_detail(
        created,
        decision=decision,
        owner_names=names,
        audience_grants=grouped_grants.get(created.id, []),
        audience_principal_names=await _audience_principal_names(
            request,
            store,
            user,
            grouped_grants.get(created.id, []),
        ),
        writable=writable,
        write_reason=write_reason,
    )
    payload["definition_mode"] = "revisioned" if revision_kwargs else "legacy_mutable"
    response.headers["ETag"] = str(payload["etag"])
    return payload


def _visible_to(
    sa: TeamSubagent | TeamSubagentSummaryRecord,
    user: User,
    granted: frozenset[str] = frozenset(),
) -> bool:
    """B1 (content-permissions): creator_only 仅 created_by 可见，team_wide 全 team 成员可见。
    与 resolve 侧（bowhead_core.subagent 的 creator_only 检查）同语义——此前管理 HTTP 面
    无过滤，creator_only 的元数据对全 team 泄漏且与 resolve 行为镜像不一致。无条件生效，
    不挂任何 feature flag（安全修复，tech-design §4 flag 表）。
    S4: 第三条件 = 调用者"分享给我"预取集命中（grant 叠加,挂 content_sharing flag）。"""
    return (sa.visibility or "team_wide") == "team_wide" or sa.created_by == user.id or sa.id in granted


@router.get("")
async def list_subagents(
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, private"
    items = await store.list_team_subagent_summaries(user.team_id)
    granted = await _granted_ids(request, user)
    decisions = await _resolve_access_decisions(
        request,
        user,
        store,
        items,
        granted,
    )
    grouped_grants = await _audience_grants(request, user, required=True)
    names = await _owner_names(store, decisions)
    visible_items = [item for item in items if _visible_to(item, user, granted)]
    native_write_capability = await _write_capability(
        request,
        requires_mcp=False,
    )
    mcp_write_capability = (
        await _write_capability(request, requires_mcp=True) if any(item.team_tool_count > 0 for item in visible_items) else native_write_capability
    )
    payload_items: list[dict[str, Any]] = []
    for item in visible_items:
        writable, write_reason = mcp_write_capability if _requires_mcp_license(item) else native_write_capability
        payload_items.append(
            _summary(
                item,
                decision=decisions.get(item.id),
                owner_names=names,
                audience_grants=grouped_grants.get(item.id, []),
                writable=writable,
                write_reason=write_reason,
            )
        )
    return {
        "schema_version": 1,
        "kind": "team_subagent_summary_list",
        "items": payload_items,
    }


async def _granted_ids(request: Request, user: User) -> frozenset[str]:
    """per-request 预取"分享给我"集(share_service 内部已挂 content_sharing flag)。"""
    access = getattr(request.app.state, "access_control_store", None)
    if access is None:
        return frozenset()
    try:
        from bowhead_core.share_service import grants_for_viewer

        return await grants_for_viewer(
            access,
            team_id=user.team_id,
            user_id=user.id,
            kind="team_subagent",
        )
    except Exception:
        return frozenset()


async def _resolve_access_decisions(
    request: Request,
    user: User,
    store: UserStore,
    items: list[TeamSubagent | TeamSubagentSummaryRecord],
    granted: frozenset[str],
) -> dict[str, TeamSubagentAccessDecision]:
    """Compare Release A visibility with the canonical resolver without activating it."""
    if not items:
        return {}
    schema_store = getattr(
        request.app.state,
        "team_subagent_schema_store",
        None,
    )
    if not isinstance(schema_store, TeamSubagentStore):
        return {}
    try:
        decisions = await TeamSubagentAccessResolver(
            store.db_path,
        ).resolve_many(
            team_id=user.team_id,
            actor_user_id=user.id,
        )
    except Exception as exc:
        log.warning(
            "subagent.access_shadow_failed",
            team_id=user.team_id,
            error_type=type(exc).__name__,
        )
        return {}
    _log_access_shadow_comparison(
        user,
        items,
        granted,
        decisions,
    )
    return decisions


def _log_access_shadow_comparison(
    user: User,
    items: list[TeamSubagent | TeamSubagentSummaryRecord],
    granted: frozenset[str],
    decisions: dict[str, TeamSubagentAccessDecision],
) -> None:
    legacy_visible = {item.id for item in items if _visible_to(item, user, granted)}
    canonical_visible = {item.id for item in items if ((decision := decisions.get(item.id)) is not None and decision.resolution.catalog_visible)}
    mismatch_count = len(legacy_visible ^ canonical_visible)
    missing_count = len({item.id for item in items} - decisions.keys())
    event = log.warning if mismatch_count or missing_count else log.info
    event(
        "subagent.access_shadow_compared",
        team_id=user.team_id,
        item_count=len(items),
        legacy_visible_count=len(legacy_visible),
        canonical_visible_count=len(canonical_visible),
        mismatch_count=mismatch_count,
        missing_count=missing_count,
    )


async def _require_manager_decision(
    store: UserStore,
    user: User,
    subagent_id: str,
) -> TeamSubagentAccessDecision:
    """Fail closed unless the canonical policy grants current-Owner management."""
    try:
        decision = await TeamSubagentAccessResolver(store.db_path).resolve(
            team_id=user.team_id,
            actor_user_id=user.id,
            subagent_id=subagent_id,
        )
    except Exception as exc:
        log.warning(
            "subagent.manager_access_failed",
            team_id=user.team_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(503, "subagent access policy unavailable") from None
    if decision is None:
        raise HTTPException(404, "subagent not found")
    if not decision.resolution.can_manage:
        raise HTTPException(403, "only the current active Owner can manage this subagent")
    return decision


@router.get("/tools")
async def list_team_tools(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Member-level picker catalog: the team's ENABLED teammcp.* tools (flat names +
    is_write/method/path/description) the member can assemble into a bundle, plus the
    native tools allowed in a bundle. team_id is the AUTHENTICATED user's (no
    client-supplied scope key → no IDOR; do NOT add a /{source_id} path variant)."""
    catalog = await SubagentAuthoringCatalogService().build(
        store=store,
        team_id=user.team_id,
        actor_user_id=user.id,
        actor_active=True,
        mcp_entitled=await is_feature_licensed_request(request, "mcp"),
    )
    legacy_team_tools = [
        {
            "tool_name": tool.stable_ref,
            "operation_id": tool.operation_id,
            "description": tool.description,
            "is_write": tool.risk in {"external_write", "destructive", "unknown"},
            "method": tool.method,
            "path": tool.path,
            "source_id": tool.stable_ref.split(".", 2)[1],
            "risk": tool.risk,
            "risk_source": tool.risk_source,
            "selectable": tool.selectable,
            "locked_reason": tool.locked_reason,
        }
        for tool in (item.for_selection() for item in catalog.team_tools)
    ]
    return {
        "text_only_allowed": resolve_subagent_authoring_capabilities(user.team_id).text_only,
        "team_tools": legacy_team_tools,
        # Skill wrapper Tools are not discoverable: each Skill's grant decides them.
        "native_tools": sorted(tool.stable_ref for tool in catalog.native_tools if tool.discoverable and tool.for_selection().selectable),
        "catalog": {
            "schema_version": catalog.schema_version,
            "fingerprint": catalog.fingerprint,
            "risk_registry_version": catalog.risk_registry_version,
            "native_tools": [tool.to_public_dict() for tool in catalog.native_tools if tool.discoverable],
            "team_tools": [tool.to_public_dict() for tool in catalog.team_tools],
        },
    }


@router.get("/skills")
async def list_team_subagent_skills(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Member-level picker catalog of SOP skills attachable to an team subagent. A selected
    skill is stored in ``skills`` and injected at runtime as the ``Auto-read skills``
    prelude (the subagent must ``skill_read`` it first; see bowhead_core.subagent). Source =
    the BUILTIN catalog (global — any runner can read) + the team's ENABLED ``team_wide``
    skills. EXCLUDES personal/user skills and ``creator_only`` team skills: an team_wide
    subagent runs with the RUNNER's perms (any member), so an attachable skill must be
    ``skill_read``-able by any member or the picker would lie (runtime → not-found).

    Create/patch apply a FORMAT gate on each skill name (the canonical slug shape) so a name
    can't smuggle prompt-injection into the runtime auto-read prelude; they do NOT gate on
    catalog membership — a well-formed but unknown name is allowed and degrades to a runtime
    not-found (the skill_resolver is the team+visibility-scoped access boundary, so a name can
    never escalate). team_id = the AUTHENTICATED user's (no client scope key → no IDOR)."""
    catalog = getattr(request.app.state, "skill_catalog", None)
    builtin: list[dict[str, Any]] = []
    if catalog is not None:
        from bowhead_core.subagent_authoring_catalog import is_platform_only_skill
        from bowhead_skill.builtin_display import listing_description_fields

        for spec in catalog.list_specs():
            if is_platform_only_skill(str(spec.name)):
                continue
            builtin.append(
                {
                    "name": str(spec.name),
                    "display_name": str(getattr(spec, "title", "") or ""),
                    "display_name_zh": str(getattr(spec, "title_zh", "") or ""),
                    "description": (getattr(spec, "description", "") or ""),
                    **listing_description_fields(spec),
                    "source_kind": "builtin",
                }
            )
        builtin.sort(key=lambda s: s["name"])
    return {
        "builtin": builtin,
        "team_skills": await _team_skill_catalog(store, user.team_id),
    }


# review-r2: manifests are process-static (builtin YAML); cache once, rather than glob/parse on every request
# (synchronous disk IO blocks the event loop and repeats on every settings-page opening). None = not loaded.
#
# ⚠️ Builtin subagent management: cache **manifest bases only** (team-independent). Fetch team deltas once per request and merge
# into responses; **never process-cache by team_id** (multiple teams + no invalidation hooks = cross-team data mixing/stale edits).
_BUILTIN_BASE_CACHE: list[dict[str, Any]] | None = None
_RETIRED_BUILTIN_SUBAGENT_NAMES = frozenset({"canvas-writer"})


def _builtin_bases() -> list[dict[str, Any]]:
    """Process-static listed system Agents, in their manifest listing order.

    The manifest ``listing`` block is the one source for the marketplace, the @
    list and this admin list; ``lead`` has none and stays out.
    """
    global _BUILTIN_BASE_CACHE  # noqa: PLW0603 — process-static manifest cache
    if _BUILTIN_BASE_CACHE is not None:
        return _BUILTIN_BASE_CACHE
    items: list[dict[str, Any]] = []
    try:
        from bowhead_core.agents.capability_roles import supported_primary_capabilities
        from bowhead_core.agents.manifest import TEAM_OVERRIDE_FIELDS
        from bowhead_core.agents.registry import AgentRegistry
        from bowhead_core.system_agent_marketplace import listed_system_agents

        registry = AgentRegistry.bootstrap()
        for name, listing in listed_system_agents().items():
            try:
                m = registry.get(name)
            except KeyError:
                continue
            items.append(
                {
                    "name": m.name,
                    "model": str(m.model),
                    "description": m.description or "",
                    "max_iterations": int(getattr(m, "max_iterations", 50) or 50),
                    "primary_capability": getattr(m, "primary_capability", None),
                    "primary_capability_options": sorted(
                        supported_primary_capabilities(
                            m.tools.allow,
                            output_formats=m.produces,
                        )
                    ),
                    "team_overrides": [field for field in TEAM_OVERRIDE_FIELDS if field in listing["team_overrides"]],
                }
            )
        _BUILTIN_BASE_CACHE = items
    except Exception as e:
        # Do not cache failures (retry next time), but log them, or manifest regressions silently appear as empty lists.
        log.warning("builtins.manifest_load_failed", err=str(e))
        return []
    return items


def _builtin_override_store(request: Request) -> Any | None:
    """override store 句柄；flag 关 / 未接线 → None（route 层据此 404）。"""

    return getattr(request.app.state, "builtin_subagent_override_store", None)


def _require_builtin_override_store(request: Request) -> Any:
    store = _builtin_override_store(request)
    if store is None:
        # 404, not 503: with flag OFF this capability does not exist for clients, rather than being temporarily broken.
        raise HTTPException(404, "builtin subagent override not available")
    return store


def _builtin_override_store_unavailable(
    exc: Exception,
    *,
    operation: str,
) -> HTTPException:
    """Publish one stable, actionable failure instead of leaking a generic 500.

    The settings list and its write actions use the same migration-owned store. If the
    list silently falls back to manifest defaults after a schema/storage failure, the UI
    incorrectly stays writable and the first toggle fails later with no useful reason.
    Fail closed at the first boundary and keep the exact exception in server logs.
    """
    log.exception(
        "builtins.override_store_unavailable",
        operation=operation,
        error_type=type(exc).__name__,
        err=str(exc),
    )
    return HTTPException(
        503,
        detail={
            "error": {
                "code": "builtin_override_store_unavailable",
                "message": ("Built-in Agent team settings are not ready. Ask a system administrator to verify the service upgrade, then retry."),
            },
        },
        headers={"Cache-Control": "no-store, private"},
    )


def _builtin_etag(name: str, version: int) -> str:
    # ConditionalHeaderCanonicalizationMiddleware strips a CDN-added W/ prefix before
    # routes read If-Match. Emit the canonical strong form here so direct and weakened
    # round-trips compare against the same value.
    return f'"builtin:{name}:{version}"'


def _require_builtin_base(name: str) -> dict[str, Any]:
    base = next((item for item in _builtin_bases() if str(item["name"]) == name), None)
    if base is not None:
        return base
    if name not in _RETIRED_BUILTIN_SUBAGENT_NAMES:
        raise HTTPException(404, "builtin subagent not found")
    raise HTTPException(
        410,
        detail={
            "error": {
                "code": "builtin_subagent_removed",
                "message": "This built-in agent has been removed. Refresh the page to load the current list.",
            }
        },
    )


def _builtin_public(base: dict[str, Any], ov: Any | None) -> dict[str, Any]:
    """一个内置的**生效值** + 默认值 + 改动标记。

    带 ``defaults`` 是为了 UI 能显示「默认 vs 当前」并实现「恢复默认」预览，不用前端硬编码一份
    内置基线（那样每次改 manifest 都要同步改前端）。
    """
    from bowhead_core.builtin_subagent_override import (
        DISABLE_WARNINGS,
        effective_description,
        effective_display_name,
        effective_max_iterations,
        effective_model,
        effective_primary_capability,
        max_iterations_ceiling,
        restrict_override_to_manifest,
    )

    name = str(base["name"])
    # Fields outside the manifest's team_overrides never take effect, even on legacy rows.
    ov = restrict_override_to_manifest(name, ov)
    desc, _ = effective_description(str(base["description"]), ov)
    version = int(getattr(ov, "row_version", 0) or 0)
    enabled = bool(getattr(ov, "enabled", True))
    payload: dict[str, Any] = {
        "name": name,
        "enabled": enabled,
        "model": effective_model(str(base["model"]), ov),
        "description": desc,
        "display_name": effective_display_name(name, ov),
        "max_iterations": effective_max_iterations(int(base["max_iterations"]), ov),
        "primary_capability": effective_primary_capability(
            base.get("primary_capability"),
            ov,
            eligible_capabilities=frozenset(base.get("primary_capability_options", ())),
        ),
        "primary_capability_options": list(base.get("primary_capability_options", ())),
        "skills": list(getattr(ov, "skills", None) or []),
        "prompt_append": getattr(ov, "prompt_append", None) or "",
        "defaults": {
            "model": str(base["model"]),
            "description": str(base["description"]),
            "max_iterations": int(base["max_iterations"]),
            "max_iterations_ceiling": max_iterations_ceiling(int(base["max_iterations"])),
            "primary_capability": base.get("primary_capability"),
        },
        "overridden_fields": list(getattr(ov, "overridden_fields", []) or []),
        # Fields a Team may change for this Agent (manifest listing.team_overrides); the
        # settings page shows only these.
        "team_overrides": list(_team_override_fields(base)),
        # ⚠ Raw delta: **FE must write this back**, never effective values above (review P1-2);
        # effective description has passed sanitization (hijack wording becomes placeholders; NFKC collapses
        # whitespace). Writing it back permanently replaces admin text with placeholders on the next enable/disable/model edit.
        # None = no delta for this team.
        "override": (
            None
            if ov is None
            else {
                "display_name": getattr(ov, "display_name", None),
                "description": getattr(ov, "description", None),
                "model": getattr(ov, "model", None),
                "skills": getattr(ov, "skills", None),
                "prompt_append": getattr(ov, "prompt_append", None),
                "max_iterations": getattr(ov, "max_iterations", None),
                "primary_capability": getattr(ov, "primary_capability", None),
            }
        ),
        "row_version": version,
        "etag": _builtin_etag(name, version),
    }
    warning = DISABLE_WARNINGS.get(name)
    payload["disable_warning"] = warning
    return payload


@router.get("/builtins")
async def list_builtin_subagents(
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """followup-context-deck #12: 列出系统**内置** subagent (manifest peers), 让用户看到系统自带
    的子代理 (consultant / financial-analyst / kb-analyst / …). 排除 lead
    (orchestrator 本体). 与 team 自建列表(GET "")分开.

    内置子代理管理: flag 开时叠加本团队的 delta 返回**生效值**（成员只读、admin 可改）。
    flag 关 → 纯 manifest 基线（与本 feature 落地前逐字段一致）。
    """
    # Each item's ETag binds its current team delta. Cached old lists leave admins with old ETags and persistent 409s
    # even after automatic refresh; team-configuration responses must not enter browser/shared-proxy caches.
    response.headers["Cache-Control"] = "no-store, private"
    bases = _builtin_bases()
    override_store = _builtin_override_store(request)
    overrides: dict[str, Any] = {}
    if override_store is not None and user.team_id:
        try:
            overrides = await override_store.list_for_team(user.team_id)
        except Exception as exc:
            # This is a management page, not runtime reads: disguising unavailable team deltas as pure baselines leaves the page
            # offering writes with version=0, then unexplained 500/409 only when enable/disable is clicked.
            raise _builtin_override_store_unavailable(exc, operation="list") from exc
    # ⚠ manageable must **actually check team-admin status**, not just flag/wiring. Writes use
    # require_team_admin; FE uses this value for write-entry visibility. Disagreement shows ordinary members
    # configure/disable/reset actions that always return 403 (verified in review P1-1).
    manageable = False
    if override_store is not None and user.team_id:
        try:
            manageable = await store.is_team_admin(user.id, user.team_id)
        except Exception as e:
            # Undetermined means unmanageable (fail-closed: fewer entries are preferable to entries guaranteed to return 403).
            log.warning("builtins.admin_check_failed", err=str(e))
    model_names = await _team_tier_model_names(store, user.team_id)
    items = []
    for b in bases:
        item = _builtin_public(b, overrides.get(str(b["name"])))
        # Polish spec 2d: Details shows the model the tier resolves to for this team; absent when unknown.
        item["model_display_name"] = model_names.get(str(item.get("model") or ""))
        items.append(item)
    return {"items": items, "manageable": manageable}


async def _team_tier_model_names(store: UserStore, team_id: str | None) -> dict[str, str]:
    """Model tier → the primary model the team's configuration binds to it (best effort, display only).

    Read failures or invalid/legacy configurations return {} so the list still renders with tier labels.
    """
    if not team_id:
        return {}
    try:
        from bowhead_core.team_llm_config import agent_bindings_from_v2, is_v2_config

        config, _version = await store.get_team_llm_config_with_version(team_id)
        if not isinstance(config, dict) or not is_v2_config(config):
            return {}
        names: dict[str, str] = {}
        for binding in agent_bindings_from_v2(config):
            model = str(binding.primary[1] or "").strip()
            if binding.profile is None and model and binding.agent_type not in names:
                names[binding.agent_type] = model
        return names
    except Exception as exc:  # noqa: BLE001 - display enrichment must never fail the list
        log.warning("builtins.model_names_unavailable", err=type(exc).__name__)
        return {}


class BuiltinOverrideBody(BaseModel):
    """全量写入一个内置的 delta（不是 PATCH —— 送来的就是目标终态）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    display_name: str | None = None
    description: str | None = None
    model: str | None = None
    skills: list[str] | None = None
    prompt_append: str | None = None
    max_iterations: int | None = None
    primary_capability: str | None = None


def _validate_builtin_override(
    body: BuiltinOverrideBody,
    base: dict[str, Any],
) -> None:
    """写入侧校验。运行时合成层还有一层静默地板（防御纵深）——这里负责给 admin 明确反馈。"""
    from bowhead_core.agents.exceptions import ManifestSecurityError
    from bowhead_core.agents.manifest_security import lint_user_append
    from bowhead_core.builtin_subagent_override import (
        MAX_DESCRIPTION_CHARS,
        MAX_DISPLAY_NAME_CHARS,
        MAX_PROMPT_APPEND_CHARS,
        MODEL_ALLOWLIST,
    )

    def _reject(code: str, message: str, field: str) -> None:
        raise HTTPException(
            422,
            detail={"error": {"code": code, "message": message, "details": {"field": field}}},
        )

    allowed = _team_override_fields(base)
    for field_name in BuiltinOverrideBody.model_fields:
        value = getattr(body, field_name)
        if field_name in allowed or value is None or (field_name == "enabled" and value is True):
            continue
        _reject(
            "builtin_override_field_not_allowed",
            f"{field_name} can't be changed for this built-in Agent. Teams may change only: {', '.join(allowed) or 'nothing'}. Clear {field_name} and save again.",
            field_name,
        )

    if body.display_name is not None and len(body.display_name) > MAX_DISPLAY_NAME_CHARS:
        _reject("builtin_display_name_too_long", f"display_name too long (>{MAX_DISPLAY_NAME_CHARS})", "display_name")
    if body.description is not None and len(body.description) > MAX_DESCRIPTION_CHARS:
        # Match peer_block._MAX_SYSTEM_DESC_CHARS: Lead cannot see excess text;
        # reject on writes rather than silently truncate (what users see equals what Lead receives).
        _reject("builtin_description_too_long", f"description too long (>{MAX_DESCRIPTION_CHARS})", "description")
    if body.model is not None and body.model not in MODEL_ALLOWLIST:
        _reject(
            "builtin_model_not_allowed",
            f"model must be one of: {sorted(MODEL_ALLOWLIST)}",
            "model",
        )
    if body.prompt_append is not None and len(body.prompt_append) > MAX_PROMPT_APPEND_CHARS:
        _reject("builtin_prompt_append_too_long", f"prompt_append too long (>{MAX_PROMPT_APPEND_CHARS})", "prompt_append")
    if body.max_iterations is not None:
        from bowhead_core.builtin_subagent_override import max_iterations_ceiling

        ceiling = max_iterations_ceiling(int(base["max_iterations"]))
        if not (1 <= body.max_iterations <= ceiling):
            _reject(
                "builtin_max_iterations_invalid",
                f"max_iterations must be a whole number from 1 to {ceiling}",
                "max_iterations",
            )
    if body.skills is not None:
        _validate_skills(body.skills)
    if body.primary_capability is not None:
        from bowhead_core.agents.capability_roles import active_capability_ids

        if body.primary_capability not in active_capability_ids():
            _reject(
                "builtin_primary_capability_invalid",
                "primary_capability must reference an active Capability Definition",
                "primary_capability",
            )
        if body.primary_capability not in base.get("primary_capability_options", ()):
            _reject(
                "builtin_primary_capability_unproven",
                "primary_capability requires the Agent's existing tools or output contract",
                "primary_capability",
            )
    # description / prompt_append enter LLM context; use the same injection lint as custom subagents.
    # description is especially important: system sources bypass lint in render_peer_block.
    for field_name, text in (
        ("description", body.description),
        ("prompt_append", body.prompt_append),
    ):
        if not text:
            continue
        try:
            lint_user_append(text)
        except ManifestSecurityError:
            _reject(
                "builtin_prompt_security_rejected",
                f"{field_name} rejected by injection lint",
                field_name,
            )
    # ⚠ review P1-2: passing lint does not ensure survival at rendering. description also passes hijack checks ('优先'
    # and similar wording is absent from lint but in the hijack table); a hit replaces the entire text with '(描述含诱导措辞, 已隐藏)'.
    # Previously silently accepted, making admins believe routing bias took effect while the whole text was discarded and overwritten on next save.
    # **Reject on writes** so admins immediately know to reword (PRD user story #2's original wording triggers it).
    if body.description:
        from bowhead_core.agents.prompts.peer_block import (
            sanitize_builtin_override_description,
        )

        cleaned = sanitize_builtin_override_description(body.description, name="preflight")
        if cleaned is None or cleaned.startswith("(描述含诱导措辞"):
            _reject(
                "builtin_description_hijack_rejected",
                "description contains routing-hijack wording (e.g. 优先 / 必须 / 总是) that the Lead prompt strips; rephrase it as a plain capability description",
                "description",
            )

    # Marketplace copy has a separate DLP boundary from runtime prompt safety.
    # Return only the stable taxonomy so validation never reflects a matched
    # secret or PII value into the response.
    from bowhead_core.agent_listing_safety import scan_agent_listing

    listing_safety = scan_agent_listing(
        name=base["name"],
        display_name=body.display_name,
        description=body.description,
    )
    if listing_safety.blocked:
        finding = listing_safety.findings[0]
        field = finding.field_path.removeprefix("listing.")
        _reject(
            "builtin_listing_safety_rejected",
            f"{field} rejected by listing safety ({finding.rule_family})",
            field,
        )


@router.put(
    "/builtins/{name}/override",
    dependencies=[Depends(require_not_readonly)],
)
async def put_builtin_override(
    name: str,
    body: BuiltinOverrideBody,
    request: Request,
    response: Response,
    user: User = Depends(require_team_admin),
) -> dict[str, Any]:
    """team admin 覆盖一个系统内置子代理（内置子代理管理 sprint）。

    ``require_team_admin`` 而非成员级：内置是全团队共享的系统能力，没有「创建者」，不该让普通成员
    改掉别人依赖的路由（与自建子代理的 owner/audience 模型刻意不同）。

    ``If-Match`` 必带：首次写入用 ``"builtin:<name>:0"``（GET 返回的就是它）。
    """
    from bowhead_core.audit_actions import AuditAction
    from bowhead_core.builtin_subagent_override_store import (
        BuiltinSubagentDisplayNameConflictError,
        BuiltinSubagentOverride,
        BuiltinSubagentOverrideVersionConflictError,
    )

    store = _require_builtin_override_store(request)
    base = _require_builtin_base(name)
    _validate_builtin_override(body, base)

    try:
        current = await store.get(user.team_id, name)
    except Exception as exc:
        raise _builtin_override_store_unavailable(exc, operation="read_before_update") from exc
    current_version = int(getattr(current, "row_version", 0) or 0)
    if request.headers.get("If-Match") is None:
        raise HTTPException(428, detail={"error": {"code": "builtin_if_match_required"}})
    if request.headers.get("If-Match") != _builtin_etag(name, current_version):
        raise HTTPException(
            409,
            detail={
                "error": {
                    "code": "builtin_override_version_conflict",
                    "details": {
                        "current_version": current_version,
                        "etag": _builtin_etag(name, current_version),
                    },
                },
            },
        )

    try:
        saved = await store.upsert(
            BuiltinSubagentOverride(
                team_id=user.team_id,
                name=name,
                enabled=body.enabled,
                display_name=body.display_name,
                description=body.description,
                model=body.model,
                skills=body.skills,
                prompt_append=body.prompt_append,
                max_iterations=body.max_iterations,
                primary_capability=body.primary_capability,
            ),
            expected_version=(current_version or None),
            actor_user_id=user.id,
            audit_action=AuditAction.BUILTIN_SUBAGENT_OVERRIDE_UPDATED,
        )
    except BuiltinSubagentOverrideVersionConflictError as exc:
        raise HTTPException(
            409,
            detail={
                "error": {
                    "code": "builtin_override_version_conflict",
                    "details": {
                        "current_version": exc.current_version,
                        "etag": _builtin_etag(name, int(exc.current_version or 0)),
                    },
                },
            },
        ) from None
    except BuiltinSubagentDisplayNameConflictError as exc:
        raise HTTPException(
            409,
            detail={"error": {"code": "display_name_unavailable"}},
        ) from exc
    except Exception as exc:
        raise _builtin_override_store_unavailable(exc, operation="update") from exc
    payload = _builtin_public(base, saved)
    response.headers["ETag"] = str(payload["etag"])
    log.info(
        "builtin_override.updated",
        name=name,
        team_id=user.team_id,
        actor=user.id,
        fields=payload["overridden_fields"],
    )
    return payload


@router.delete(
    "/builtins/{name}/override",
    dependencies=[Depends(require_not_readonly)],
)
async def delete_builtin_override(
    name: str,
    request: Request,
    user: User = Depends(require_team_admin),
) -> dict[str, Any]:
    """恢复默认 = 删 delta 行。幂等（本来就没有 delta 也返回 200）。"""
    from bowhead_core.audit_actions import AuditAction

    store = _require_builtin_override_store(request)
    base = _require_builtin_base(name)
    try:
        removed = await store.delete(
            user.team_id,
            name,
            actor_user_id=user.id,
            audit_action=AuditAction.BUILTIN_SUBAGENT_OVERRIDE_CLEARED,
        )
    except Exception as exc:
        raise _builtin_override_store_unavailable(exc, operation="clear") from exc
    log.info("builtin_override.cleared", name=name, team_id=user.team_id, actor=user.id, removed=removed)
    return _builtin_public(base, None)


@router.get("/{subagent_id}")
async def get_subagent(
    subagent_id: str,
    request: Request,
    response: Response,
    detail: Literal["consumer", "manager"] = Query("consumer"),
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, private"
    if detail == "manager":
        sa: TeamSubagent | TeamSubagentSummaryRecord | None = await store.get_team_subagent(user.team_id, subagent_id)
    else:
        sa = await store.get_team_subagent_summary(
            user.team_id,
            subagent_id,
        )
    granted = await _granted_ids(request, user)
    decisions: dict[str, TeamSubagentAccessDecision] = {}
    if sa is not None:
        decisions = await _resolve_access_decisions(
            request,
            user,
            store,
            [sa],
            granted,
        )
    if sa is None or not _visible_to(sa, user, granted):
        raise HTTPException(404, "subagent not found")  # Do not distinguish nonexistent from invisible (anti-enumeration).
    decision = decisions.get(sa.id)
    if detail == "manager":
        assert isinstance(sa, TeamSubagent)
        decision = await _require_manager_decision(store, user, sa.id)
        grouped_grants = await _audience_grants(request, user, required=True)
        names = await _owner_names(store, {sa.id: decision})
        writable, write_reason = await _write_capability(
            request,
            requires_mcp=_requires_mcp_license(sa),
        )
        payload = _owner_manager_detail(
            sa,
            decision=decision,
            owner_names=names,
            audience_grants=grouped_grants.get(sa.id, []),
            audience_principal_names=await _audience_principal_names(
                request,
                store,
                user,
                grouped_grants.get(sa.id, []),
            ),
            writable=writable,
            write_reason=write_reason,
        )
        state = await _mutation_store(request, store).release_definition_state(
            team_id=user.team_id,
            subagent_id=sa.id,
        )
        payload["definition_mode"] = state[0] if state is not None else "legacy_mutable"
        response.headers["ETag"] = str(payload["etag"])
        return payload
    grouped_grants = await _audience_grants(request, user, required=True)
    names = await _owner_names(store, decisions)
    writable, write_reason = await _write_capability(
        request,
        requires_mcp=_requires_mcp_license(sa),
    )
    payload = _summary(
        sa,
        decision=decision,
        owner_names=names,
        audience_grants=grouped_grants.get(sa.id, []),
        writable=writable,
        write_reason=write_reason,
    )
    response.headers["ETag"] = str(payload["etag"])
    return payload


async def _mutate_subagent(
    subagent_id: str,
    body: ConfigurationPatchBody,
    request: Request,
    response: Response,
    user: User,
    store: UserStore,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, private"
    existing = await store.get_team_subagent(user.team_id, subagent_id)
    if existing is None or not _visible_to(existing, user, await _granted_ids(request, user)):
        raise HTTPException(404, "subagent not found")
    decision = await _require_manager_decision(store, user, subagent_id)
    # Deliberately **before** both version gates: report unavailable paths directly, without requesting refreshes that cannot fix versions.
    await _require_release_writer_path(
        _mutation_store(request, store),
        team_id=user.team_id,
        subagent_id=subagent_id,
        body=body,
    )
    _require_matching_manager_snapshot(existing, decision)
    effective_tool_refs = body.tool_refs if body.tool_refs is not None else existing.tool_refs
    entitlement_tool_refs = [*existing.tool_refs, *effective_tool_refs] if body.tool_refs is not None else existing.tool_refs
    await _require_tool_entitlement(request, entitlement_tool_refs)
    # Definition/status mutations do not change audience or ownership in R0.
    # Read the roster before committing so a 503 cannot mask a successful
    # update and invite an unsafe retry.
    grouped_grants = await _audience_grants(request, user, required=True)
    expected_version = _require_expected_version(
        request,
        subagent_id=subagent_id,
        current_version=decision.row_version,
    )
    if status is not None and status not in _STATUS:
        raise HTTPException(422, f"unknown status: {status!r}")
    # A status-only disable must remain available even when a legacy definition
    # no longer passes today's configuration validator. Configuration writes
    # still validate the complete merged definition.
    if body.model_fields_set:
        _validate_common(
            display_name=body.display_name if body.display_name is not None else existing.display_name,
            description=body.description if body.description is not None else existing.description,
            system_prompt=body.system_prompt if body.system_prompt is not None else existing.system_prompt,
            tool_refs=body.tool_refs if body.tool_refs is not None else existing.tool_refs,
            max_iterations=body.max_iterations if body.max_iterations is not None else existing.max_iterations,
            # Closing the rollout must not strand an existing text-only definition.
            allow_empty_tools=not existing.tool_refs or resolve_subagent_authoring_capabilities(user.team_id).text_only,
        )
        if body.tool_refs is not None:
            await _validate_tool_refs(
                store,
                user.team_id,
                user.id,
                body.tool_refs,
                existing_tool_refs=existing.tool_refs,
            )
        if body.model is not None:
            _validate_model(body.model)
        if body.skills is not None:
            _validate_skills(body.skills)
        if body.output_schema is not None:
            _validate_output_schema(body.output_schema)
    # null-clear semantics (review fix): the two NULLABLE fields (model, output_schema)
    # distinguish an EXPLICIT null (clear → SQL NULL) from an OMITTED key (keep) via the
    # pydantic model_fields_set preserves omitted-vs-explicit-null semantics.
    # The NOT-NULL fields keep None = omit.
    _fields_set = body.model_fields_set
    try:
        result = await _mutation_store(
            request,
            store,
        ).compare_and_update_team_subagent(
            user.team_id,
            subagent_id,
            expected_version=expected_version,
            display_name=body.display_name,
            description=body.description,
            system_prompt=body.system_prompt,
            tool_refs=body.tool_refs,
            max_iterations=body.max_iterations,
            skills=body.skills,
            status=status,
            model=body.model,
            model_is_set="model" in _fields_set,
            output_schema=body.output_schema,
            output_schema_is_set="output_schema" in _fields_set,
            actor_user_id=user.id,
            audit_action=AuditAction.TEAM_SUBAGENT_UPDATED,
        )
    except TeamSubagentVersionConflictError as exc:
        raise _version_conflict(subagent_id, exc.current_version) from None
    except TeamSubagentNameConflictError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    except TeamSubagentRevisionWriterRequiredError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    if result is None:
        raise HTTPException(404, "subagent not found")
    got = await store.get_team_subagent(user.team_id, subagent_id)
    updated = got if got is not None else existing
    refreshed_decision = await _require_manager_decision(
        store,
        user,
        subagent_id,
    )
    names = await _owner_names(store, {subagent_id: refreshed_decision})
    writable, write_reason = await _write_capability(
        request,
        requires_mcp=_requires_mcp_license(updated),
    )
    payload = _owner_manager_detail(
        updated,
        decision=refreshed_decision,
        owner_names=names,
        audience_grants=grouped_grants.get(subagent_id, []),
        audience_principal_names=await _audience_principal_names(
            request,
            store,
            user,
            grouped_grants.get(subagent_id, []),
        ),
        writable=writable,
        write_reason=write_reason,
    )
    response.headers["ETag"] = str(payload["etag"])
    return payload


@router.patch(
    "/{subagent_id}/configuration",
    dependencies=[Depends(require_not_readonly)],
)
async def patch_subagent_configuration(
    subagent_id: str,
    body: ConfigurationPatchBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    return await _mutate_subagent(
        subagent_id,
        body,
        request,
        response,
        user,
        store,
    )


@router.put(
    "/{subagent_id}/status",
    dependencies=[Depends(require_not_readonly)],
)
async def put_subagent_status(
    subagent_id: str,
    body: StatusBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    return await _mutate_subagent(
        subagent_id,
        ConfigurationPatchBody(),
        request,
        response,
        user,
        store,
        status=body.status,
    )


@router.put(
    "/{subagent_id}/audience",
    dependencies=[Depends(require_not_readonly)],
)
async def put_subagent_audience(
    subagent_id: str,
    body: AudienceBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Replace the entire Owner-managed allow audience in one transaction."""
    response.headers["Cache-Control"] = "no-store, private"
    existing = await store.get_team_subagent(user.team_id, subagent_id)
    if existing is None or not _visible_to(
        existing,
        user,
        await _granted_ids(request, user),
    ):
        raise HTTPException(404, "subagent not found")
    await _require_manager_decision(store, user, subagent_id)
    await _require_tool_entitlement(request, existing.tool_refs)
    # The Store repeats Owner and principal validation inside BEGIN IMMEDIATE to
    # close the authorization/commit race. Audience writes intentionally use
    # last-save-wins semantics: the Owner submits a complete replacement roster.
    await _audience_grants(request, user, required=True)
    access_store = getattr(request.app.state, "access_control_store", None)
    if access_store is None:
        raise HTTPException(503, "subagent audience policy unavailable")
    try:
        result = await _mutation_store(
            request,
            store,
        ).replace_team_subagent_audience(
            user.team_id,
            subagent_id,
            mode=("teamwide" if body.mode == "team_wide" else body.mode),
            user_ids=body.user_ids,
            group_ids=body.group_ids,
            access_store=access_store,
            actor_user_id=user.id,
            audit_action=AuditAction.SUBAGENT_AUDIENCE_CHANGED,
        )
    except TeamSubagentAudienceForbiddenError:
        raise HTTPException(
            403,
            detail={
                "error": {
                    "code": "subagent_action_forbidden",
                },
            },
        ) from None
    except TeamSubagentAudienceInvalidError as exc:
        details: dict[str, Any] = {}
        if exc.invalid_user_ids:
            details["invalid_user_ids"] = list(exc.invalid_user_ids)
        if exc.invalid_group_ids:
            details["invalid_group_ids"] = list(exc.invalid_group_ids)
        error: dict[str, Any] = {
            "code": "subagent_audience_invalid",
        }
        if details:
            error["details"] = details
        raise HTTPException(
            422,
            detail={"error": error},
        ) from None
    except TeamSubagentRevisionWriterRequiredError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    if result is None:
        raise HTTPException(404, "subagent not found")

    updated = await store.get_team_subagent(user.team_id, subagent_id)
    if updated is None:
        raise HTTPException(404, "subagent not found")
    refreshed_decision = await _require_manager_decision(
        store,
        user,
        subagent_id,
    )
    grouped_grants = await _audience_grants(request, user, required=True)
    names = await _owner_names(store, {subagent_id: refreshed_decision})
    writable, write_reason = await _write_capability(
        request,
        requires_mcp=_requires_mcp_license(updated),
    )
    payload = _owner_manager_detail(
        updated,
        decision=refreshed_decision,
        owner_names=names,
        audience_grants=grouped_grants.get(subagent_id, []),
        audience_principal_names=await _audience_principal_names(
            request,
            store,
            user,
            grouped_grants.get(subagent_id, []),
        ),
        writable=writable,
        write_reason=write_reason,
    )
    response.headers["ETag"] = str(payload["etag"])
    return payload


@router.patch(
    "/{subagent_id}",
    dependencies=[Depends(require_not_readonly)],
)
async def patch_subagent(
    subagent_id: str,
    body: PatchBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Deprecated combined adapter; new clients use split R0 mutation routes."""
    configuration = ConfigurationPatchBody(
        **body.model_dump(
            exclude={"status"},
            exclude_unset=True,
        ),
    )
    return await _mutate_subagent(
        subagent_id,
        configuration,
        request,
        response,
        user,
        store,
        status=body.status,
    )


@router.delete(
    "/{subagent_id}",
    status_code=204,
    dependencies=[Depends(require_not_readonly)],
)
async def delete_subagent(
    subagent_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> None:
    existing = await store.get_team_subagent(user.team_id, subagent_id)
    if existing is None or not _visible_to(existing, user, await _granted_ids(request, user)):
        raise HTTPException(404, "subagent not found")
    decision = await _require_manager_decision(store, user, subagent_id)
    # Same as _mutate_subagent: report unavailable paths directly, without requesting refreshes that cannot fix versions.
    await _require_archive_path(_mutation_store(request, store), subagent_id=subagent_id)
    _require_matching_manager_snapshot(existing, decision)
    await _require_tool_entitlement(request, existing.tool_refs)
    # The same store owns the allow/deny cascade. Refuse the delete before its
    # transaction if policy storage is unavailable instead of leaving grants.
    await _audience_grants(request, user, required=True)
    expected_version = _require_expected_version(
        request,
        subagent_id=subagent_id,
        current_version=decision.row_version,
    )
    try:
        ok = await _mutation_store(request, store).delete_team_subagent(
            user.team_id,
            subagent_id,
            expected_version=expected_version,
            access_store=getattr(
                request.app.state,
                "access_control_store",
                None,
            ),
            actor_user_id=user.id,
            audit_action=AuditAction.TEAM_SUBAGENT_DELETED,
        )
    except TeamSubagentVersionConflictError as exc:
        raise _version_conflict(subagent_id, exc.current_version) from None
    except TeamSubagentDeleteDependencyError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    if not ok:
        raise HTTPException(404, "subagent not found")
