"""Expose the current user's Skill catalog and supported management actions.

GET /api/v1/skills lists built-in, uploaded and generated Skills.
GET /api/v1/skills/{name} returns details including the Skill instructions.
PATCH /api/v1/skills/update changes enabled state or description by body name.
DELETE /api/v1/skills/{skill_id} removes eligible uploaded or generated Skills.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite
from bowhead_core.access_control import resource_grants_enforce_enabled
from bowhead_core.audit_actions import AuditAction
from bowhead_core.resource_file_quarantine import (
    finalize_cleanup,
    quarantine_paths,
    restore_quarantined,
    retry_pending_cleanup,
    write_cleanup_receipts,
)
from bowhead_core.resource_impact import (
    ResourceChangeIdempotencyConflict,
    ResourceImpactKeyUnavailable,
    ResourceImpactTokenInvalid,
    ResourceImpactTokenStale,
    issue_impact_token,
    read_change_receipt,
    resource_change_request_digest,
    verify_impact_token,
    write_change_receipt,
)
from bowhead_core.resource_usage_flags import resource_impact_guard_enabled
from bowhead_core.resource_usage_projection import (
    ResourceUsageProjectionService,
    ResourceUsageSelector,
    ResourceUsageUnavailableError,
)
from bowhead_core.skill_access import skill_visible_by_grants
from bowhead_core.skill_bundle_lock import skill_bundle_lock
from bowhead_core.skill_import.hook import apply_import_inventory
from bowhead_core.skill_import_provenance import build_import_provenance
from bowhead_core.skill_upload import (
    MAX_DESC_LEN,
    MAX_SKILL_SIZE,
    NAME_PATTERN,
    SkillUploadError,
    _check_user_quota,
    bundle_sha256,
    copy_validated_bundle,
    peek_skill_name,
    uploaded_skill_name,
    validate_and_extract,
    validate_and_save_generated,
)
from bowhead_skill.builtin_display import listing_description_fields
from bowhead_skill.effective import effective_description
from bowhead_skill.loader import load_skill
from bowhead_skill.script_langs import script_execution_mode
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.access_context import (
    access_context_to_dict,
    build_access_context_from_request,
    has_workspace_access_source,
)
from bowhead_gateway.dtos import (
    ResourceUsageSummaryDTO,
    SkillDetailDTO,
    SkillListDTO,
    SkillSummaryDTO,
    SkillUsedByDTO,
)
from bowhead_gateway.resource_impact_http import (
    affected_skill_schedule_ids,
    delete_has_live_dependencies,
    impact_http_error,
    pause_affected_schedules,
    require_impact_headers,
    usage_dependency_impact,
)
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.skill_bundle_files import (
    BundleFileChange,
    change_bundle_file,
    display_body,
    read_bundle_file_snapshot,
    read_bundle_manifest,
    read_bundle_snapshot,
    require_bundle_authoring,
    validate_file_ref,
    write_bundle_body,
    write_bundle_file,
)
from bowhead_gateway.skill_import_sources import build_resolution_sources
from bowhead_gateway.skill_workflow_errors import skill_workflow_http_error

if TYPE_CHECKING:
    from bowhead_core.user_store import User

    from bowhead_core import SkillInstanceStore

router = APIRouter()

USER_SKILL_SOURCE_TYPES = {"user_upload", "user_generated"}
_PERSONAL_SETTINGS_SCOPE = "personal"


def _store(request: Request) -> SkillInstanceStore:
    s: SkillInstanceStore | None = getattr(request.app.state, "skill_instance_store", None)
    if s is None:
        raise skill_workflow_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "skill_store_unavailable",
        )
    return s


def _catalog(request: Request) -> Any:
    cat = getattr(request.app.state, "skill_catalog", None)
    if cat is None:
        raise skill_workflow_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "skill_catalog_unavailable",
        )
    return cat


class SkillPromoteRequest(BaseModel):
    """提交团队审核的个人 Skill 快照。

    名称、可读标题和内容均位于请求体，名称不再放进 URL。服务端据此定位已保存的个人
    bundle，并校验提交内容仍是该版本。
    """

    name: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=MAX_SKILL_SIZE)


class SkillPatchByNameRequest(BaseModel):
    """修改个人 Skill 的请求；用户定义的名称不作为 URL 的一部分。"""

    name: str = Field(min_length=1, max_length=64)
    team_skill_id: str | None = Field(None, min_length=1, max_length=128)
    enabled: bool | None = None
    description: str | None = Field(None, max_length=MAX_DESC_LEN)


class SkillImpactPreviewBody(BaseModel):
    action: str
    proposed_change: dict[str, Any] = Field(default_factory=dict)
    team_skill_id: str | None = Field(None, min_length=1, max_length=128)


def _skill_instance_state(inst: Any | None) -> dict[str, Any] | None:
    if inst is None:
        return None
    return {
        "id": inst.id,
        "name": inst.name,
        "source_type": inst.source_type,
        "source_path": inst.source_path,
        "version": inst.version,
        "description": inst.description,
        "enabled": bool(inst.enabled),
        "source_turn_id": inst.source_turn_id,
        "size_bytes": int(inst.size_bytes or 0),
        "verdict": inst.verdict,
        "verdict_json": inst.verdict_json,
        "risk_report_json": inst.risk_report_json,
        "updated_at": inst.updated_at,
    }


def _personal_skill_state(spec: Any, inst: Any | None, source_type: str) -> dict[str, Any]:
    source_dir = Path(str(spec.source_dir))
    content_digest: str | None = None
    if source_dir.is_dir():
        with contextlib.suppress(OSError):
            content_digest = bundle_sha256(source_dir)
    return {
        "name": spec.name,
        "source_type": source_type,
        "version": spec.version,
        "content_digest": content_digest,
        "instance": _skill_instance_state(inst),
    }


def _team_skill_preference_state(rec: Any, inst: Any | None) -> dict[str, Any]:
    return {
        "id": rec.id,
        "name": rec.name,
        "visibility": rec.visibility,
        "status": rec.status,
        "approval": rec.approval,
        "approved_sha256": rec.approved_sha256,
        "updated_at": rec.updated_at,
        "preference": _skill_instance_state(inst),
    }


def _canonical_skill_patch(body: SkillPatchByNameRequest) -> dict[str, Any]:
    change: dict[str, Any] = {}
    if body.enabled is not None:
        change["enabled"] = body.enabled
    if body.description is not None:
        change["description"] = body.description
    return {"action": "patch", "proposed_change": change}


async def _skill_access_ctx(request: Request, user: User) -> Any | None:
    # Personal skill management is user-scoped: users can view/manage their skills even if the browser retains
    # a space they left. Actual skill discovery/invocation within spaces still uses runtime access-context authorization.
    if request.query_params.get("scope") == _PERSONAL_SETTINGS_SCOPE:
        return None
    if not has_workspace_access_source(request):
        return None
    # Batch 3/c2: prefer X-Space-Id, fall back to X-Workspace-Id (one-version compatibility for old FE/scripts).
    workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    if not workspace_id:
        user_store = getattr(request.app.state, "user_store", None)
        if user_store is not None and callable(getattr(user_store, "list_workspaces_for_user", None)):
            workspaces = await user_store.list_workspaces_for_user(user.id)
            workspace_id = str(workspaces[0].get("workspace_id") or "") if workspaces else ""
        if not workspace_id:
            if resource_grants_enforce_enabled():
                raise skill_workflow_http_error(
                    status.HTTP_400_BAD_REQUEST,
                    "missing_workspace_context",
                )
            return None
    access_ctx = await build_access_context_from_request(
        request,
        user=user,
        workspace_id=workspace_id,
    )
    access_ctx_dict = access_context_to_dict(access_ctx)
    request.state.bowhead_access_context = access_ctx_dict
    return SimpleNamespace(
        access_control_store=getattr(request.app.state, "access_control_store", None),
        access_context=access_ctx_dict,
    )


async def _skill_visible_for_request(request: Request, user: User, name: str) -> bool:
    ctx = await _skill_access_ctx(request, user)
    if ctx is None:
        return True
    return await skill_visible_by_grants(ctx, name)


async def _require_skill_grant_for_request(
    request: Request,
    user: User,
    name: str,
    *,
    permission: str,
    team_skill: Any | None = None,
) -> None:
    ctx = await _skill_access_ctx(request, user)
    if ctx is None:
        return
    if await skill_visible_by_grants(
        ctx,
        name,
        permission=permission,
        audit=True,
        audit_payload={"route": "skills"},
        team_skill_id=(str(team_skill.id) if team_skill is not None else None),
        team_skill_created_by=(team_skill.created_by if team_skill is not None else None),
    ):
        return
    raise skill_workflow_http_error(
        status.HTTP_403_FORBIDDEN,
        "resource_grant_denied",
        nested_error=True,
        resource_kind="skill",
        resource_id=name,
        permission=permission,
    )


async def _require_skill_capability_for_request(
    request: Request,
    user: User,
    *,
    permission: str,
) -> None:
    ctx = await _skill_access_ctx(request, user)
    if ctx is None:
        return
    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is None or not access_ctx:
        return
    decision = await access_store.explain_decision(
        access_ctx,
        "builtin_capability",
        "skill_invoke",
        permission,
    )
    if decision.get("allowed"):
        return
    enforce = resource_grants_enforce_enabled()
    await access_store.record_runtime_denied(
        access_ctx,
        resource_kind="builtin_capability",
        resource_id="skill_invoke",
        permission=permission,
        reason=str(decision.get("reason") or "denied"),
        target_kind="skill",
        target_id="skill_invoke",
        payload={"route": "skills", "enforce": enforce},
    )
    if enforce:
        raise skill_workflow_http_error(
            status.HTTP_403_FORBIDDEN,
            "resource_grant_denied",
            nested_error=True,
            resource_kind="builtin_capability",
            resource_id="skill_invoke",
            permission=permission,
        )


def _classify_source(
    source_path: str,
    builtin_dir: Path,
    user_dir_for_user: Path,
) -> str:
    """判 skill 来源类型."""
    p = Path(source_path).resolve()
    try:
        p.relative_to(user_dir_for_user)
        # Under user_skills/<user_id>/... → initially classify as upload (generated skills need source_turn_id to distinguish).
        return "user_upload"
    except ValueError:
        pass
    try:
        p.relative_to(builtin_dir.resolve())
        return "builtin"
    except ValueError:
        return "builtin"  # Fallback; theoretically unreachable.


def _builtin_dir(request: Request) -> Path:
    """git catalog 位置（以 settings 的 repo root 推导）."""
    from bowhead_skill.catalog import builtin_catalog_dir  # lazy import

    return builtin_catalog_dir()


def _user_dir_for(user_id: str) -> Path:
    """`.data/user_skills/<user_id>/`；不强制存在."""
    return (Path(".data") / "user_skills" / user_id).resolve()


def _catalog_names(catalog: Any) -> set[str]:
    if callable(getattr(catalog, "list_names", None)):
        return set(catalog.list_names())
    return {str(s.name) for s in catalog.list_specs()}


def _is_user_skill_instance(inst: Any | None) -> bool:
    return inst is not None and inst.source_type in USER_SKILL_SOURCE_TYPES


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _load_user_skill_spec(inst: Any, user_dir: Path) -> Any | None:
    """Load a per-user skill only from the authenticated user's directory."""
    if not _is_user_skill_instance(inst):
        return None
    source_dir = Path(str(inst.source_path))
    if not _path_under(source_dir, user_dir):
        return None
    try:
        spec = load_skill(source_dir.resolve())
    except Exception:
        return None
    if spec.name != inst.name:
        return None
    return spec


async def _resolve_skill_for_user(
    *,
    name: str,
    user_id: str,
    catalog: Any,
    store: SkillInstanceStore,
) -> tuple[Any | None, Any | None]:
    """Resolve global skills first, then current-user skills from SkillInstanceStore.

    User-uploaded/generated skills are intentionally not loaded from the process
    catalog. A user skill with the same name as a global skill is treated as a
    stale collision and cannot shadow the global definition.
    """
    global_spec = catalog.get(name)
    inst = await store.get(user_id=user_id, name=name)
    if global_spec is not None:
        return global_spec, (None if _is_user_skill_instance(inst) else inst)
    if not _is_user_skill_instance(inst):
        return None, None
    return _load_user_skill_spec(inst, _user_dir_for(user_id)), inst


async def _resolve_team_skill_preference_target(
    request: Request,
    user: User,
    name: str,
    store: SkillInstanceStore,
    *,
    team_skill_id: str | None = None,
) -> tuple[Any, Any | None] | None:
    """Resolve the per-user preference row without exposing hidden team Skills."""

    from bowhead_core.workflow_creator_trigger import workflow_creator_enabled

    if not workflow_creator_enabled() or not user.team_id:
        return None
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return None
    if team_skill_id:
        rec = await user_store.get_team_skill(user.team_id, team_skill_id)
        if rec is not None and rec.name != name:
            return None
    else:
        rec = await user_store.get_team_skill_by_name(user.team_id, name, only_enabled=False)
    if rec is None:
        return None
    if (rec.visibility or "team_wide") != "team_wide" and rec.created_by != user.id:
        access = getattr(request.app.state, "access_control_store", None)
        if access is None:
            return None
        try:
            from bowhead_core.share_service import grants_for_viewer

            granted = await grants_for_viewer(
                access,
                team_id=user.team_id,
                user_id=user.id,
                kind="team_skill",
            )
        except Exception:
            return None
        if rec.id not in granted:
            return None
    raw = await store.get(user_id=user.id, name=_team_skill_preference_key(rec.id))
    legacy = await store.get(user_id=user.id, name=name)
    if raw is None and getattr(legacy, "source_type", None) == "team_shared":
        raw = legacy
    if not team_skill_id and _is_user_skill_instance(legacy):
        return None
    return rec, raw


def _team_skill_preference_key(team_skill_id: str) -> str:
    """Stable, collision-free key inside the legacy per-user Skill registry."""

    return f"team:{team_skill_id}"


def _effective_description(inst: Any | None, spec: Any) -> str:
    return effective_description(inst, spec)


def _builtin_listing_fields(spec: Any, *, source_type: str, shown_description: str) -> dict[str, str]:
    """Localized listing text for an unmodified built-in; a personal override is shown as written."""
    if source_type != "builtin" or shown_description != (spec.description or ""):
        return {}
    return listing_description_fields(spec)


def _import_report_dict(inst: Any | None) -> dict[str, Any] | None:
    raw = getattr(inst, "import_report_json", None) if inst is not None else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def _run_import_inventory(request: Request, store: SkillInstanceStore, user: User, *, name: str, skill_dir: Path) -> None:
    """skill-import 批1：个人 skill 每条写路径落盘后重跑清点（content-based）。**永不让写失败**."""
    try:
        sources = await build_resolution_sources(request, user)
        await apply_import_inventory(store, user_id=user.id, name=name, skill_dir=skill_dir, sources=sources)
    except Exception as e:  # noqa: BLE001
        # A failed inspection cannot clear an existing or newer pending review.
        logging.getLogger(__name__).error("skill_import.inventory_failed skill=%s err=%s", name, type(e).__name__)


def _spec_to_summary(
    spec: Any,
    *,
    skill_id: str | None = None,
    source_type: str,
    enabled: bool,
    size_bytes: int = 0,
    created_at: str | None = None,
    description: str | None = None,
    import_status: str | None = None,
) -> SkillSummaryDTO:
    shown = description if description is not None else (spec.description or "")
    return SkillSummaryDTO(
        id=skill_id,
        name=spec.name,
        description=shown,
        version=spec.version,
        source_type=source_type,
        enabled=enabled,
        size_bytes=size_bytes,
        created_at=created_at,
        title=str(getattr(spec, "title", "") or ""),
        title_zh=str(getattr(spec, "title_zh", "") or "") if source_type == "builtin" else "",
        **_builtin_listing_fields(spec, source_type=source_type, shown_description=shown),
        category=str(getattr(spec, "category", "") or ""),
        import_status=import_status,
    )


def _usage_service(request: Request) -> ResourceUsageProjectionService:
    user_store = getattr(request.app.state, "user_store", None)
    db_path = getattr(user_store, "db_path", None)
    if not db_path:
        raise skill_workflow_http_error(503, "resource_usage_unavailable")
    return ResourceUsageProjectionService(str(db_path))


def _skill_resource_ref(*, name: str, source_type: str, skill_id: str | None) -> str | None:
    if source_type == "builtin":
        return f"builtin:builtin:{name}"
    if skill_id:
        return f"user:{skill_id}"
    return None


async def _used_by_subagents(
    request: Request,
    user: User,
    *,
    skill_name: str,
    source_type: str,
    skill_id: str | None,
    enabled: bool,
) -> list[SkillUsedByDTO]:
    """Compatibility field backed by the typed actor-scoped projection."""
    if not user.team_id:
        return []
    resource_ref = _skill_resource_ref(
        name=skill_name,
        source_type=source_type,
        skill_id=skill_id,
    )
    if resource_ref is None:
        return []
    try:
        view = await _usage_service(request).usage(
            team_id=user.team_id,
            actor_user_id=user.id,
            selector=ResourceUsageSelector(
                key=skill_name,
                kind="skill",
                resource_id=skill_id or skill_name,
                display_name=skill_name,
                resource_ref=resource_ref,
                available=enabled,
            ),
        )
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_usage_unavailable") from exc
    return [SkillUsedByDTO(id=item.agent_id, display_name=item.display_name) for item in view.groups.get("current", ()) if item.kind == "agent"]


async def _attach_skill_usage_summaries(
    request: Request,
    user: User,
    groups: tuple[list[SkillSummaryDTO], ...],
) -> None:
    if not user.team_id:
        return
    selectors: list[ResourceUsageSelector] = []
    by_key: dict[str, SkillSummaryDTO] = {}
    for group in groups:
        for summary in group:
            resource_ref = _skill_resource_ref(
                name=summary.name,
                source_type=summary.source_type,
                skill_id=summary.id,
            )
            if resource_ref is None:
                continue
            key = f"{summary.source_type}:{summary.id or summary.name}"
            by_key[key] = summary
            selectors.append(
                ResourceUsageSelector(
                    key=key,
                    kind="skill",
                    resource_id=summary.id or summary.name,
                    display_name=summary.title or summary.name,
                    resource_ref=resource_ref,
                    available=summary.enabled,
                )
            )
    try:
        usage = await _usage_service(request).summaries(
            team_id=user.team_id,
            actor_user_id=user.id,
            selectors=selectors,
        )
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_usage_unavailable") from exc
    for key, result in usage.items():
        by_key[key].usage_summary = ResourceUsageSummaryDTO.model_validate(result.to_dict())


def _team_preference_detail(rec: Any, *, enabled: bool) -> SkillDetailDTO:
    return SkillDetailDTO(
        name=rec.name,
        description=rec.description or "",
        version=None,
        source_type="team_shared",
        enabled=enabled,
        size_bytes=rec.size_bytes or 0,
        created_at=rec.created_at,
        title=rec.display_name or "",
        category="",
        body_md="",
        allowed_tools=[],
        scripts=[],
        trigger_examples=[],
        references=[],
        evals_count=0,
        has_evals=False,
        used_by_subagents=[],
    )


def _instance_from_row(row: Any | None) -> Any | None:
    if row is None:
        return None
    return SimpleNamespace(**dict(row))


async def _resolve_skill_impact_target(
    *,
    name: str,
    request: Request,
    user: User,
    store: SkillInstanceStore,
    team_skill_id: str | None = None,
) -> Any | None:
    if team_skill_id:
        team_target = await _resolve_team_skill_preference_target(
            request,
            user,
            name,
            store,
            team_skill_id=team_skill_id,
        )
        if team_target is None:
            return None
        rec, preference = team_target
        return SimpleNamespace(
            kind="team_preference",
            spec=None,
            instance=preference,
            team_skill=rec,
            source_type="team_shared",
            resource_id=rec.id,
            resource_ref=f"team:{rec.id}",
            preference_key=_team_skill_preference_key(rec.id),
            enabled=(preference.enabled if preference is not None else True),
        )
    spec, inst = await _resolve_skill_for_user(
        name=name,
        user_id=user.id,
        catalog=_catalog(request),
        store=store,
    )
    if spec is not None:
        if not await _skill_visible_for_request(request, user, name):
            return None
        source_type = (
            inst.source_type
            if inst is not None
            else _classify_source(
                str(spec.source_dir),
                _builtin_dir(request),
                _user_dir_for(user.id),
            )
        )
        return SimpleNamespace(
            kind="personal",
            spec=spec,
            instance=inst,
            team_skill=None,
            source_type=source_type,
            resource_id=(f"builtin:{name}" if source_type == "builtin" else inst.id),
            resource_ref=_skill_resource_ref(
                name=name,
                source_type=source_type,
                skill_id=(inst.id if inst is not None else None),
            ),
            preference_key=None,
            enabled=(inst.enabled if inst is not None else True),
        )
    team_target = await _resolve_team_skill_preference_target(request, user, name, store)
    if team_target is None:
        return None
    rec, preference = team_target
    return SimpleNamespace(
        kind="team_preference",
        spec=None,
        instance=preference,
        team_skill=rec,
        source_type="team_shared",
        resource_id=rec.id,
        resource_ref=f"team:{rec.id}",
        preference_key=_team_skill_preference_key(rec.id),
        enabled=(preference.enabled if preference is not None else True),
    )


def _impact_target_state(target: Any) -> dict[str, Any]:
    if target.kind == "team_preference":
        return _team_skill_preference_state(target.team_skill, target.instance)
    return _personal_skill_state(target.spec, target.instance, target.source_type)


def _impact_selector(target: Any, *, name: str) -> ResourceUsageSelector:
    display_name = (
        target.team_skill.display_name or target.team_skill.name
        if target.kind == "team_preference"
        else str(getattr(target.spec, "title", "") or target.spec.name)
    )
    globally_available = True
    revision: str | None = None
    if target.kind == "team_preference":
        globally_available = target.team_skill.status == "enabled" and target.team_skill.approval == "approved"
        revision = target.team_skill.approved_sha256
    else:
        source_dir = Path(str(target.spec.source_dir))
        if source_dir.is_dir():
            with contextlib.suppress(OSError):
                revision = bundle_sha256(source_dir)
    return ResourceUsageSelector(
        key=target.resource_id,
        kind="skill",
        resource_id=target.resource_id,
        display_name=display_name,
        resource_ref=target.resource_ref,
        revision=revision,
        available=bool(target.enabled and globally_available),
    )


def _impact_response(
    *,
    action: str,
    view: Any,
    schedule_ids: list[str],
    command: dict[str, Any],
    token: str | None,
    blocking: bool,
) -> dict[str, Any]:
    return {
        "kind": "resource_change_impact",
        "schema_version": 1,
        "action": action,
        "blocking": blocking,
        "summary": view.summary.to_dict(),
        "new_admission": "blocked_for_affected_agents",
        "running_execution": "future_dispatch_blocked",
        "already_sent_effect": "not_reversible",
        "required_follow_up": [{"kind": "schedule_reconfirm", "job_id": job_id} for job_id in schedule_ids],
        "impact_token": token,
        "expires_in_seconds": 300,
        "command": command,
        "reason_codes": [
            *view.summary.reason_codes,
            *(("resource_delete_has_live_dependencies",) if action == "delete" and delete_has_live_dependencies(view) else ()),
        ],
    }


@router.post("/{name}/import-override", dependencies=[Depends(require_not_readonly)])
async def override_skill_import(
    name: str,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> dict[str, Any]:
    """skill-import 批1（tech-design r3 §2/§4.5）：本人明知清点有红仍启用 → `overridden`。

    钉在当前 findings 指纹上：内容没变保持 overridden；改了内容且仍红会重新 pending。
    只对本人的个人 skill；不是 pending 直接 409。这是用户对自己私有资产的偏好动作，不放大授权
    （脚本能否执行仍由 script_execution 与 grant 决定）。
    """
    response.headers["Cache-Control"] = "private, no-store"
    await _require_skill_grant_for_request(request, user, name, permission="write")
    inst = await store.get(user_id=user.id, name=name)
    if not _is_user_skill_instance(inst):
        raise skill_workflow_http_error(status.HTTP_404_NOT_FOUND, "skill_not_found")
    if getattr(inst, "import_status", None) != "pending":
        raise skill_workflow_http_error(status.HTTP_409_CONFLICT, "skill_import_not_pending")
    await store.set_import_status(
        user_id=user.id,
        name=name,
        import_status="overridden",
        import_report_json=getattr(inst, "import_report_json", None),
    )
    return {"name": name, "import_status": "overridden"}


@router.post("/{name}/impact-preview")
async def preview_skill_impact(
    name: str,
    body: SkillImpactPreviewBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> dict[str, Any]:
    """Preview a personal Skill disable/delete or a per-user team Skill disable."""

    response.headers["Cache-Control"] = "private, no-store"
    if not user.team_id:
        raise skill_workflow_http_error(403, "team_required")
    target = await _resolve_skill_impact_target(
        name=name,
        request=request,
        user=user,
        store=store,
        team_skill_id=body.team_skill_id,
    )
    if target is None:
        raise skill_workflow_http_error(404, "skill_not_found")
    await _require_skill_grant_for_request(
        request,
        user,
        name,
        permission="write",
        team_skill=target.team_skill if target.kind == "team_preference" else None,
    )
    if body.action == "patch":
        proposed = body.proposed_change
        if set(proposed) - {"enabled", "description"}:
            raise skill_workflow_http_error(422, "resource_command_invalid")
        if "enabled" in proposed and not isinstance(proposed["enabled"], bool):
            raise skill_workflow_http_error(422, "resource_command_invalid")
        if target.kind == "team_preference" and "description" in proposed:
            raise skill_workflow_http_error(422, "team_skill_description_governed")
        try:
            patch = SkillPatchByNameRequest(
                name=name,
                team_skill_id=body.team_skill_id,
                **proposed,
            )
        except ValueError as exc:
            raise skill_workflow_http_error(422, "resource_command_invalid") from exc
        command = _canonical_skill_patch(patch)
    elif body.action == "delete":
        if target.kind != "personal" or not _is_user_skill_instance(target.instance):
            raise skill_workflow_http_error(422, "resource_command_invalid")
        command = {"action": "delete", "proposed_change": {}}
    else:
        raise skill_workflow_http_error(422, "resource_command_invalid")
    from bowhead_core._db import open_shared_ctx

    try:
        async with open_shared_ctx(store.db_path) as db:
            selector = _impact_selector(target, name=name)
            views = await ResourceUsageProjectionService(store.db_path).usage_many_in_connection(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                selectors=(selector,),
                governance_action_allowed=True,
                include_groups=True,
            )
            schedule_ids = await affected_skill_schedule_ids(
                db,
                team_id=user.team_id,
                resource_ref=target.resource_ref,
            )
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_impact_projection_incomplete") from exc
    view = views[target.resource_id]
    dependency_impact = usage_dependency_impact(view, schedule_ids)
    blocking = view.summary.completeness != "complete" or (body.action == "delete" and delete_has_live_dependencies(view))
    token: str | None = None
    if not blocking:
        try:
            token = issue_impact_token(
                team_id=user.team_id,
                actor_user_id=user.id,
                resource_kind="skill",
                resource_id=target.resource_id,
                command=command,
                resource_state=_impact_target_state(target),
                dependency_impact=dependency_impact,
            )
        except ResourceImpactKeyUnavailable as exc:
            raise impact_http_error(exc) from exc
    return _impact_response(
        action=body.action,
        view=view,
        schedule_ids=schedule_ids,
        command=command,
        token=token,
        blocking=blocking,
    )


async def _reload_skill_impact_target_in_connection(
    db: Any,
    *,
    target: Any,
    user: User,
) -> Any:
    db.row_factory = aiosqlite.Row
    lookup_name = target.preference_key if target.kind == "team_preference" else target.spec.name
    async with db.execute(
        "SELECT * FROM skill_instances WHERE user_id=? AND name=?",
        (user.id, lookup_name),
    ) as cursor:
        preference_row = await cursor.fetchone()
    if preference_row is None and target.kind == "team_preference":
        async with db.execute(
            "SELECT * FROM skill_instances WHERE user_id=? AND name=? AND source_type='team_shared'",
            (user.id, target.team_skill.name),
        ) as cursor:
            preference_row = await cursor.fetchone()
    instance = _instance_from_row(preference_row)
    if target.kind == "team_preference":
        from bowhead_core.user_store import _row_to_team_skill

        async with db.execute(
            "SELECT * FROM team_skill WHERE team_id=? AND id=?",
            (user.team_id, target.resource_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise skill_workflow_http_error(404, "skill_not_found")
        rec = _row_to_team_skill(row)
        return SimpleNamespace(
            **{
                **vars(target),
                "instance": instance,
                "team_skill": rec,
                "enabled": (instance.enabled if instance is not None else True),
            }
        )
    if target.source_type in USER_SKILL_SOURCE_TYPES:
        if instance is None or instance.id != target.resource_id or not _is_user_skill_instance(instance):
            raise skill_workflow_http_error(404, "skill_not_found")
    elif instance is not None and instance.source_type != "builtin":
        raise skill_workflow_http_error(409, "skill_name_conflict")
    return SimpleNamespace(
        **{
            **vars(target),
            "instance": instance,
            "enabled": (instance.enabled if instance is not None else True),
        }
    )


async def _write_skill_preference_in_connection(
    db: Any,
    *,
    target: Any,
    user: User,
    change: dict[str, Any],
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    current = target.instance
    enabled = bool(change.get("enabled", target.enabled))
    description = change.get("description", current.description if current is not None else None)
    if current is not None:
        await db.execute(
            "UPDATE skill_instances SET enabled=?,description=?,updated_at=? WHERE user_id=? AND name=?",
            (1 if enabled else 0, description, now, user.id, current.name),
        )
        return
    if target.kind == "team_preference":
        name = target.preference_key
        source_type = "team_shared"
        source_path = ""
        version = None
    else:
        name = target.spec.name
        source_type = target.source_type
        source_path = str(target.spec.source_dir)
        version = target.spec.version
    await db.execute(
        "INSERT INTO skill_instances("
        "id,user_id,name,source_type,source_path,version,description,enabled,source_turn_id,"
        "size_bytes,risk_report_json,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"sk_{uuid4().hex[:12]}",
            user.id,
            name,
            source_type,
            source_path,
            version,
            description,
            1 if enabled else 0,
            None,
            0,
            None,
            now,
            now,
        ),
    )


async def _protected_patch_skill(
    *,
    body: SkillPatchByNameRequest,
    request: Request,
    user: User,
    store: SkillInstanceStore,
    target: Any,
) -> SkillDetailDTO:
    token, idempotency_key = require_impact_headers(request)
    command = _canonical_skill_patch(body)
    request_digest = resource_change_request_digest(
        resource_kind="skill",
        resource_id=target.resource_id,
        command=command,
    )
    from bowhead_core._db import open_shared_ctx

    try:
        async with open_shared_ctx(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cached = await read_change_receipt(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if cached is not None:
                await db.rollback()
            else:
                current = await _reload_skill_impact_target_in_connection(
                    db,
                    target=target,
                    user=user,
                )
                selector = _impact_selector(current, name=body.name)
                views = await ResourceUsageProjectionService(store.db_path).usage_many_in_connection(
                    db,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    selectors=(selector,),
                    governance_action_allowed=True,
                    include_groups=True,
                )
                view = views[current.resource_id]
                if view.summary.completeness != "complete":
                    raise ResourceUsageUnavailableError("resource_dependency_projection_incomplete")
                schedule_ids = await affected_skill_schedule_ids(
                    db,
                    team_id=user.team_id,
                    resource_ref=current.resource_ref,
                )
                dependency_impact = usage_dependency_impact(view, schedule_ids)
                verified = verify_impact_token(
                    token,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    resource_kind="skill",
                    resource_id=current.resource_id,
                    command=command,
                    resource_state=_impact_target_state(current),
                    dependency_impact=dependency_impact,
                )
                await pause_affected_schedules(db, schedule_ids)
                await _write_skill_preference_in_connection(
                    db,
                    target=current,
                    user=user,
                    change=command["proposed_change"],
                )
                receipt_response = {
                    "ok": True,
                    "name": body.name,
                    "enabled": command["proposed_change"].get("enabled", current.enabled),
                    "paused_schedule_ids": schedule_ids,
                }
                await write_change_receipt(
                    db,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    idempotency_key=idempotency_key,
                    resource_kind="skill",
                    resource_id=current.resource_id,
                    action="patch",
                    request_digest=request_digest,
                    impact_digest=verified.dependency_impact_digest,
                    response=receipt_response,
                )
                await db.commit()
    except (
        ResourceChangeIdempotencyConflict,
        ResourceImpactKeyUnavailable,
        ResourceImpactTokenInvalid,
        ResourceImpactTokenStale,
    ) as exc:
        raise impact_http_error(exc) from exc
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_impact_projection_incomplete") from exc
    with contextlib.suppress(Exception):
        await retry_pending_cleanup(store.db_path)
    if target.kind == "team_preference":
        fresh = await store.get(user_id=user.id, name=target.preference_key)
        if fresh is None:
            legacy = await store.get(user_id=user.id, name=body.name)
            fresh = legacy if getattr(legacy, "source_type", None) == "team_shared" else None
        return _team_preference_detail(target.team_skill, enabled=(fresh.enabled if fresh else True))
    return await get_skill(name=body.name, request=request, user=user, store=store)


@router.get("", response_model=SkillListDTO)
async def list_skills(
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillListDTO:
    """列当前 user 可见的所有 skill，按来源分组."""
    uid = user.id
    catalog = _catalog(request)
    builtin_dir = _builtin_dir(request)
    user_dir = _user_dir_for(uid)
    access_ctx = await _skill_access_ctx(request, user)

    # 1. Fetch all this user's DB overrides (enabled=false builtins + all user-level entries).
    overrides = {inst.name: inst for inst in await store.list_for_user(user_id=uid)}

    out = SkillListDTO(impact_guard_enabled=resource_impact_guard_enabled())
    seen_global: set[str] = set()
    for spec in catalog.list_specs():
        seen_global.add(spec.name)
        if access_ctx is not None and not await skill_visible_by_grants(access_ctx, spec.name):
            continue
        raw_inst = overrides.get(spec.name)
        inst = None if _is_user_skill_instance(raw_inst) else raw_inst
        # Determine source_type.
        source_type = inst.source_type if inst is not None else _classify_source(str(spec.source_dir), builtin_dir, user_dir)
        enabled = inst.enabled if inst is not None else True
        summary = _spec_to_summary(
            spec,
            skill_id=(inst.id if inst is not None else None),
            source_type=source_type,
            enabled=enabled,
            size_bytes=(inst.size_bytes if inst else 0),
            created_at=(inst.created_at if inst else None),
            description=_effective_description(inst, spec),
            import_status=(getattr(inst, "import_status", None) if inst is not None else None),
        )
        if source_type == "user_upload":
            out.user_upload.append(summary)
        elif source_type == "user_generated":
            out.user_generated.append(summary)
        else:
            out.builtin.append(summary)

    for inst in overrides.values():
        if not _is_user_skill_instance(inst) or inst.name in seen_global:
            continue
        if access_ctx is not None and not await skill_visible_by_grants(access_ctx, inst.name):
            continue
        spec = _load_user_skill_spec(inst, user_dir)
        if spec is None:
            continue
        summary = _spec_to_summary(
            spec,
            skill_id=inst.id,
            source_type=inst.source_type,
            enabled=inst.enabled,
            size_bytes=inst.size_bytes,
            created_at=inst.created_at,
            description=_effective_description(inst, spec),
            import_status=getattr(inst, "import_status", None),
        )
        if inst.source_type == "user_generated":
            out.user_generated.append(summary)
        else:
            out.user_upload.append(summary)
    await _attach_skill_usage_summaries(
        request,
        user,
        (out.builtin, out.user_upload, out.user_generated),
    )
    return out


@router.post(
    "/promote",
    dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("mcp"))],
)
async def promote_skill(
    body: SkillPromoteRequest,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> dict[str, Any]:
    """将请求体中的个人 Skill 快照提交到团队审批。"""
    return await _promote_skill(body=body, request=request, user=user, store=store)


@router.patch(
    "/update",
    response_model=SkillDetailDTO,
    dependencies=[Depends(require_not_readonly)],
)
async def patch_skill_by_name(
    body: SkillPatchByNameRequest,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillDetailDTO:
    target = await _resolve_skill_impact_target(
        name=body.name,
        request=request,
        user=user,
        store=store,
        team_skill_id=body.team_skill_id,
    )
    has_guard_headers = bool(request.headers.get("X-Bowhead-Impact-Token") or request.headers.get("Idempotency-Key"))
    if resource_impact_guard_enabled() and target is not None and ((body.enabled is False and target.enabled) or has_guard_headers):
        if not user.team_id:
            raise skill_workflow_http_error(403, "team_required")
        await _require_skill_grant_for_request(
            request,
            user,
            body.name,
            permission="write",
            team_skill=target.team_skill if target.kind == "team_preference" else None,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return await _protected_patch_skill(
            body=body,
            request=request,
            user=user,
            store=store,
            target=target,
        )
    if body.team_skill_id and target is not None:
        await _require_skill_grant_for_request(
            request,
            user,
            body.name,
            permission="write",
            team_skill=target.team_skill,
        )
    return await _patch_skill_detail(
        name=body.name,
        body=SkillPatchRequest(enabled=body.enabled, description=body.description),
        request=request,
        user=user,
        store=store,
        team_skill_id=body.team_skill_id,
    )


@router.get("/{name}/usage")
async def get_skill_usage(
    name: str,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> dict[str, Any]:
    if not user.team_id:
        raise skill_workflow_http_error(403, "team_required")
    spec, inst = await _resolve_skill_for_user(
        name=name,
        user_id=user.id,
        catalog=_catalog(request),
        store=store,
    )
    if spec is None or not await _skill_visible_for_request(request, user, name):
        raise skill_workflow_http_error(404, "skill_not_found")
    source_type = inst.source_type if inst else "builtin"
    resource_ref = _skill_resource_ref(
        name=spec.name,
        source_type=source_type,
        skill_id=(inst.id if inst else None),
    )
    if resource_ref is None:
        raise skill_workflow_http_error(404, "skill_not_found")
    try:
        view = await _usage_service(request).usage(
            team_id=user.team_id,
            actor_user_id=user.id,
            selector=ResourceUsageSelector(
                key=name,
                kind="skill",
                resource_id=(inst.id if inst else name),
                display_name=str(getattr(spec, "title", "") or spec.name),
                resource_ref=resource_ref,
                revision=(bundle_sha256(Path(str(spec.source_dir))) if Path(str(spec.source_dir)).exists() else None),
                available=(inst.enabled if inst else True),
            ),
        )
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_usage_unavailable") from exc
    return view.to_dict()


@router.get("/{name}", response_model=SkillDetailDTO)
async def get_skill(
    name: str,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillDetailDTO:
    uid = user.id
    catalog = _catalog(request)
    spec, inst = await _resolve_skill_for_user(
        name=name,
        user_id=uid,
        catalog=catalog,
        store=store,
    )
    if spec is None:
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    if not await _skill_visible_for_request(request, user, name):
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )

    builtin_dir = _builtin_dir(request)
    user_dir = _user_dir_for(uid)
    source_type = (
        inst.source_type
        if inst
        else _classify_source(
            str(spec.source_dir),
            builtin_dir,
            user_dir,
        )
    )
    enabled = inst.enabled if inst else True

    src_dir = Path(str(spec.source_dir))
    snapshot = read_bundle_snapshot(src_dir, editable=_is_user_skill_instance(inst))
    if not _is_user_skill_instance(inst):
        snapshot["body_md"] = spec.body_md
        snapshot["body_raw"] = snapshot["body_raw"] or spec.body_md
    trigger_examples = list(getattr(spec, "trigger_examples", None) or [])

    shown_description = _effective_description(inst, spec)
    return SkillDetailDTO(
        id=(inst.id if inst else None),
        name=spec.name,
        description=shown_description,
        version=spec.version,
        source_type=source_type,
        enabled=enabled,
        size_bytes=(inst.size_bytes if inst else 0),
        created_at=(inst.created_at if inst else None),
        title=str(getattr(spec, "title", "") or ""),
        title_zh=str(getattr(spec, "title_zh", "") or "") if source_type == "builtin" else "",
        **_builtin_listing_fields(spec, source_type=source_type, shown_description=shown_description),
        category=str(getattr(spec, "category", "") or ""),
        import_status=(getattr(inst, "import_status", None) if inst is not None else None),
        import_report=_import_report_dict(inst),
        script_execution=script_execution_mode(
            has_scripts=bool(spec.scripts), builtin=source_type == "builtin", knowledge=getattr(spec, "kind", "tool") == "knowledge"
        ),
        allowed_tools=list(spec.allowed_tools or []),
        source_turn_id=(inst.source_turn_id if inst else None),
        trigger_examples=trigger_examples,
        used_by_subagents=await _used_by_subagents(
            request,
            user,
            skill_name=spec.name,
            source_type=source_type,
            skill_id=(inst.id if inst else None),
            enabled=enabled,
        ),
        **snapshot,
    )


class SkillFileContentDTO(BaseModel):
    path: str
    content: str
    size: int
    mime: str
    content_sha256: str | None = None


@router.get("/{name}/files/{kind}/{filename:path}", response_model=SkillFileContentDTO)
async def get_skill_file(
    name: str,
    kind: str,
    filename: str,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillFileContentDTO:
    """读取 skill 的 references/{filename} 或 scripts/{filename} 或 evals/{filename} 内容.

    #82 skill preview 用 —— 前端点击 references/scripts/evals 条目查看内容.
    """
    validate_file_ref(kind, filename)  # Kind allowlist + path-traversal protection, before skill lookup.
    catalog = _catalog(request)
    spec, _inst = await _resolve_skill_for_user(
        name=name,
        user_id=user.id,
        catalog=catalog,
        store=store,
    )
    if spec is None:
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    if not await _skill_visible_for_request(request, user, name):
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    return SkillFileContentDTO(**read_bundle_file_snapshot(Path(str(spec.source_dir)), kind, filename, editable=_is_user_skill_instance(_inst)))


class PersonalSkillFileChange(BundleFileChange):
    expected_instance_id: str | None = Field(None, pattern=r"^sk_[A-Za-z0-9_-]+$", max_length=80)


class SkillFileWriteRequest(BaseModel):
    """编辑面写体。粗上限对齐 bundle 总量闸；细粒度上限（正文/脚本/总量）在 bundle 校验里。"""

    content: str = Field(max_length=MAX_SKILL_SIZE)
    expected_content_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    expected_instance_id: str | None = Field(None, pattern=r"^sk_[A-Za-z0-9_-]+$", max_length=80)


async def _editable_user_skill(
    *,
    name: str,
    request: Request,
    user: User,
    store: SkillInstanceStore,
) -> tuple[Path, Any]:
    """定位一个**可编辑**的个人 skill：返回 (bundle 目录, skill_instances 行).

    内置 skill 落在 git catalog 目录里（升级会被覆盖），团队 skill 的编辑入口在
    /team-skills（有审批门）——两者在这里一律 403 ``skill_not_editable``，不是 404：
    读侧已经把它们展示给了这个用户，藏起来只会让「保存没反应」更难解释。
    """
    catalog = _catalog(request)
    spec, inst = await _resolve_skill_for_user(name=name, user_id=user.id, catalog=catalog, store=store)
    if spec is None or not await _skill_visible_for_request(request, user, name):
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    # Same per-skill write authorization as PATCH/DELETE (capability gate at route entry; resource granularity here).
    await _require_skill_grant_for_request(request, user, name, permission="write")
    if not _is_user_skill_instance(inst):
        raise skill_workflow_http_error(
            status.HTTP_403_FORBIDDEN,
            "skill_not_editable",
        )
    return Path(str(spec.source_dir)), inst


async def _personal_quota_check(store: SkillInstanceStore, user: User, instance: Any, expected_instance_id: str | None) -> Any:
    existing = await store.list_for_user(user_id=user.id)
    sizes = {row.name: row.size_bytes or 0 for row in existing if _is_user_skill_instance(row)}

    def check(validated: Any) -> None:
        # This callback runs inside the original package lock, before publishing bytes.
        with contextlib.closing(sqlite3.connect(Path(store.db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT id,source_path,source_type FROM skill_instances WHERE user_id=? AND name=?", (user.id, instance.name)).fetchone()
        if row != (instance.id, instance.source_path, instance.source_type) or expected_instance_id is not None and row[0] != expected_instance_id:
            raise skill_workflow_http_error(409, "skill_write_conflict")
        _check_user_quota(sizes, name=validated.name, new_size_bytes=validated.size_bytes)

    return check


async def _restamp_user_skill(
    request: Request, store: SkillInstanceStore, user: User, inst: Any, validated: Any, *, body_changed: bool = False
) -> None:
    """编辑落盘后刷新行上的派生字段（size/version/risk）并让 catalog 重读.

    保存完整 manifest 时 description 以新定义为准；只改附属文件保留显式描述。
    内容发生变化后旧试运行证据失效。
    """
    await store.upsert(
        user_id=user.id,
        name=inst.name,
        source_type=inst.source_type,
        source_path=inst.source_path,
        version=validated.version,
        description=validated.description if body_changed else inst.description,
        enabled=inst.enabled,
        source_turn_id=inst.source_turn_id,
        size_bytes=validated.size_bytes,
        expected_content_hash=validated.content_hash,
        expected_instance_id=inst.id,
        risk_report_json=json.dumps(validated.risk_report, ensure_ascii=False) if validated.risk_report else None,
        reset_verdict=True,
    )
    await _run_import_inventory(request, store, user, name=inst.name, skill_dir=Path(str(inst.source_path)))
    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is not None:
        with contextlib.suppress(Exception):
            catalog.refresh()
    return


@router.post("/{name}/files", response_model=SkillFileContentDTO, dependencies=[Depends(require_not_readonly)])
async def change_skill_file(
    name: str,
    body: PersonalSkillFileChange,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillFileContentDTO:
    require_bundle_authoring()
    await _require_skill_capability_for_request(request, user, permission="write")
    src_dir, inst = await _editable_user_skill(name=name, request=request, user=user, store=store)
    payload, validated = change_bundle_file(
        src_dir, **body.model_dump(exclude={"expected_instance_id"}), check=await _personal_quota_check(store, user, inst, body.expected_instance_id)
    )
    await _restamp_user_skill(request, store, user, inst, validated)
    return SkillFileContentDTO(**payload)


@router.put("/{name}/files/{kind}/{filename:path}", response_model=SkillFileContentDTO, dependencies=[Depends(require_not_readonly)])
async def put_skill_file(
    name: str,
    kind: str,
    filename: str,
    body: SkillFileWriteRequest,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillFileContentDTO:
    """覆写个人 skill 的 references/scripts/evals 单文件（与 GET 对称）."""
    validate_file_ref(kind, filename)
    await _require_skill_capability_for_request(request, user, permission="write")
    src_dir, inst = await _editable_user_skill(name=name, request=request, user=user, store=store)
    payload, validated = write_bundle_file(
        src_dir,
        kind,
        filename,
        body.content,
        expected_content_sha256=body.expected_content_sha256,
        check=await _personal_quota_check(store, user, inst, body.expected_instance_id),
    )
    await _restamp_user_skill(request, store, user, inst, validated)
    return SkillFileContentDTO(**payload)


@router.put("/{name}/body", response_model=SkillFileContentDTO, dependencies=[Depends(require_not_readonly)])
async def put_skill_body(
    name: str,
    body: SkillFileWriteRequest,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillFileContentDTO:
    """覆写个人 skill 的 SKILL.md 正文（配置页直接编辑；名字不可改）.

    单文件更新先整包校验，再只发布 SKILL.md，保留其他文件。
    """
    await _require_skill_capability_for_request(request, user, permission="write")
    src_dir, inst = await _editable_user_skill(name=name, request=request, user=user, store=store)
    payload, validated = write_bundle_body(
        src_dir,
        body.content,
        expected_name=inst.name,
        expected_content_sha256=body.expected_content_sha256,
        check=await _personal_quota_check(store, user, inst, body.expected_instance_id),
    )
    await _restamp_user_skill(request, store, user, inst, validated, body_changed=True)
    return SkillFileContentDTO(**payload)


class SkillPatchRequest(BaseModel):
    enabled: bool | None = None
    # Unused-field fix (§3.8): after enabling reads, add write-length guard matching team/frontmatter MAX_DESC_LEN.
    description: str | None = Field(None, max_length=MAX_DESC_LEN)


class SkillBulkPatchItem(SkillPatchRequest):
    name: str


class SkillBulkPatchRequest(BaseModel):
    items: list[SkillBulkPatchItem]


class SkillBulkPatchError(BaseModel):
    name: str
    status: int
    detail: Any


class SkillBulkPatchResponse(BaseModel):
    items: list[SkillDetailDTO] = Field(default_factory=list)
    errors: list[SkillBulkPatchError] = Field(default_factory=list)


async def _patch_team_skill_user_toggle(
    request: Request,
    user: User,
    name: str,
    body: SkillPatchRequest,
    store: SkillInstanceStore,
    *,
    team_skill_id: str | None = None,
) -> SkillDetailDTO | None:
    """团队 skill 的 per-user 启停（settings 团队组开关，只影响自己——PRD §2.3/验收 8）.

    写 skill_instances 一条 ``source_type='team_shared'`` 记录：非 USER_SKILL_SOURCE_TYPES
    → 不进个人分组、不占名遮蔽 resolve；effective 视图与 invoke 面按 ``enabled`` 过滤
    （批A #17 对称过滤的写路径补全）。只认 ``enabled``——description 对团队行走
    /team-skills PATCH（元数据审批门在那边）。返回 None = 走不通（flag off / 无行 /
    未批 / 无权见），调用方回落 404（反枚举）。"""
    target = await _resolve_team_skill_preference_target(
        request,
        user,
        name,
        store,
        team_skill_id=team_skill_id,
    )
    if target is None:
        return None
    rec, raw = target
    if body.description is not None:
        raise skill_workflow_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "team_skill_description_governed",
        )
    enabled = body.enabled if body.enabled is not None else (raw.enabled if raw else True)
    preference_key = raw.name if getattr(raw, "source_type", None) == "team_shared" else _team_skill_preference_key(rec.id)
    await store.upsert(
        user_id=user.id,
        name=preference_key,
        source_type="team_shared",
        source_path="",  # Team content is outside personal directories; empty path prevents bundle reads.
        version=None,
        description=None,
        enabled=enabled,
    )
    return _team_preference_detail(rec, enabled=enabled)


async def _patch_skill_detail(
    name: str,
    body: SkillPatchRequest,
    request: Request,
    user: User,
    store: SkillInstanceStore,
    team_skill_id: str | None = None,
) -> SkillDetailDTO:
    """改 enabled / description. 若该 user 还没有对应 row → 先 upsert（常见：第一次 disable built-in）."""
    uid = user.id
    if resource_impact_guard_enabled() and body.enabled is False:
        # Bulk and internal callers have no per-item impact token.  Never let
        # them bypass the protected single-resource command when the guard is on.
        raise skill_workflow_http_error(
            status.HTTP_412_PRECONDITION_FAILED,
            "resource_impact_required",
            nested_error=True,
        )
    if team_skill_id:
        team_detail = await _patch_team_skill_user_toggle(
            request,
            user,
            name,
            body,
            store,
            team_skill_id=team_skill_id,
        )
        if team_detail is not None:
            return team_detail
        raise skill_workflow_http_error(status.HTTP_404_NOT_FOUND, "skill_not_found")
    catalog = _catalog(request)
    spec, inst = await _resolve_skill_for_user(
        name=name,
        user_id=uid,
        catalog=catalog,
        store=store,
    )
    if spec is None:
        # skill-team-governance P3 (PRD acceptance 8): per-user enable/disable write path for team skills.
        # Batch A #17 filters team names on effective/invoke via skill_instances, but personal PATCH
        # could never resolve team names (missing write path). Add a team branch; unresolved → existing 404.
        team_detail = await _patch_team_skill_user_toggle(request, user, name, body, store)
        if team_detail is not None:
            return team_detail
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    if not await _skill_visible_for_request(request, user, name):
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    await _require_skill_grant_for_request(request, user, name, permission="write")

    raw_inst = await store.get(user_id=uid, name=name)
    if inst is None and _is_user_skill_instance(raw_inst):
        raise skill_workflow_http_error(
            status.HTTP_409_CONFLICT,
            "skill_name_conflict",
        )
    builtin_dir = _builtin_dir(request)
    user_dir = _user_dir_for(uid)

    if inst is None:
        # First modification (usually disabling builtin) → upsert. Unused description fix (§3.8):
        # previously this branch always wrote spec.description, silently dropping body.description on first PATCH;
        # freezing frontmatter into row values also creates false overrides (users see old builtin descriptions after upgrades,
        # review P3). Store None unless description explicitly changed; reads fall back to live frontmatter.
        source_type = _classify_source(str(spec.source_dir), builtin_dir, user_dir)
        await store.upsert(
            user_id=uid,
            name=name,
            source_type=source_type,
            source_path=str(spec.source_dir),
            version=spec.version,
            description=body.description,
            enabled=(body.enabled if body.enabled is not None else True),
        )
    else:
        # Existing row → patch specified fields.
        new_enabled = body.enabled if body.enabled is not None else inst.enabled
        new_desc = body.description if body.description is not None else inst.description
        await store.upsert(
            user_id=uid,
            name=name,
            source_type=inst.source_type,
            source_path=inst.source_path,
            version=inst.version,
            description=new_desc,
            enabled=new_enabled,
            source_turn_id=inst.source_turn_id,
            size_bytes=inst.size_bytes,
            risk_report_json=inst.risk_report_json,
        )

    # Reuse get_skill to assemble the response.
    return await get_skill(name=name, request=request, user=user, store=store)


@router.patch("/bulk", response_model=SkillBulkPatchResponse, dependencies=[Depends(require_not_readonly)])
async def patch_skills_bulk(
    body: SkillBulkPatchRequest,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillBulkPatchResponse:
    """Batch patch skill settings with per-item partial failure reporting."""
    if len(body.items) > 200:
        raise skill_workflow_http_error(
            status.HTTP_400_BAD_REQUEST,
            "skill_bulk_limit_exceeded",
        )
    out = SkillBulkPatchResponse()
    for item in body.items:
        try:
            detail = await _patch_skill_detail(
                name=item.name,
                body=SkillPatchRequest(
                    enabled=item.enabled,
                    description=item.description,
                ),
                request=request,
                user=user,
                store=store,
            )
            out.items.append(detail)
        except HTTPException as exc:
            out.errors.append(
                SkillBulkPatchError(
                    name=item.name,
                    status=exc.status_code,
                    detail=exc.detail,
                )
            )
    return out


@router.post(
    "/upload",
    response_model=SkillDetailDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_not_readonly)],
)
async def upload_skill(
    request: Request,
    file: UploadFile,
    overwrite: int = Query(0, ge=0, le=1, description="同名覆盖：0=拒绝，1=覆盖"),
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillDetailDTO:
    """上传 SKILL.md 或含 SKILL.md 的 zip（PRD Phase 2）. 见 /docs/skill-management-prd.md §4.2."""
    uid = user.id
    await _require_skill_capability_for_request(request, user, permission="write")
    data = await file.read()
    if len(data) == 0:
        raise skill_workflow_http_error(
            status.HTTP_400_BAD_REQUEST,
            "skill_upload_empty",
        )
    if len(data) > MAX_SKILL_SIZE:
        raise skill_workflow_http_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "E_SKILL_TOO_LARGE",
            max_mb=MAX_SKILL_SIZE // 1024 // 1024,
        )

    # Measure existing user usage (name → size_bytes; validator subtracts old size on same-name overwrite).
    parsed_name = uploaded_skill_name(data, file.filename or "skill.zip")
    if parsed_name:
        await _require_skill_grant_for_request(request, user, parsed_name, permission="write")
    existing = await store.list_for_user(user_id=uid)
    user_skills = {i.name: (i.size_bytes or 0) for i in existing if i.source_type in ("user_upload", "user_generated")}

    dest_root = (Path(".data") / "user_skills").resolve()
    try:
        vs = validate_and_extract(
            data=data,
            filename=file.filename or "skill.zip",
            user_id=uid,
            dest_root=dest_root,
            existing_user_skills=user_skills,
            overwrite=bool(overwrite),
            reserved_skill_names=_catalog_names(_catalog(request)),
        )
    except SkillUploadError:
        # Let FastAPI exception handler render in user's ui_language (i18n v1).
        raise

    # Persist to DB.
    await store.upsert(
        user_id=uid,
        name=vs.name,
        source_type="user_upload",
        source_path=str(vs.source_dir),
        version=vs.version,
        description=vs.description,
        enabled=True,
        size_bytes=vs.size_bytes,
        expected_content_hash=vs.content_hash,
        # skill-multiruntime S1 (G5): persist upload risk report (languages/keywords/shebang); OFF→empty.
        risk_report_json=json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
        reset_verdict=True,
    )
    await _run_import_inventory(request, store, user, name=vs.name, skill_dir=Path(str(vs.source_dir)))

    # Refresh catalog for immediate GET / visibility.
    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is not None:
        try:
            catalog.refresh()
        except Exception:
            import logging

            logging.getLogger(__name__).warning("skill_catalog.refresh_failed")

    # Reuse get_skill response logic.
    return await get_skill(name=vs.name, request=request, user=user, store=store)


class SkillScriptContent(BaseModel):
    name: str
    content: str


class SkillFilePatch(BaseModel):
    path: str
    content: str | None


class SkillGenerateRequest(BaseModel):
    files: list[SkillFilePatch] | None = None
    body_md: str
    scripts: list[SkillScriptContent] | None = None
    source_turn_id: str | None = None
    overwrite: bool = False


@router.post(
    "/generate",
    response_model=SkillDetailDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_not_readonly)],
)
async def generate_skill(
    request: Request,
    body: SkillGenerateRequest,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> SkillDetailDTO:
    """对话式生成 skill —— Lead 或前端把 LLM 产出的 SKILL.md + scripts 提交过来保存.

    PRD §4.3 Phase 3. 验证规则跟 upload 走同一层（schema + AST + 配额）.
    """
    uid = user.id
    await _require_skill_capability_for_request(request, user, permission="write")

    parsed_name = peek_skill_name(body.body_md)
    if parsed_name:
        await _require_skill_grant_for_request(request, user, parsed_name, permission="write")
    existing = await store.list_for_user(user_id=uid)
    user_skills = {i.name: (i.size_bytes or 0) for i in existing if i.source_type in ("user_upload", "user_generated")}

    dest_root = (Path(".data") / "user_skills").resolve()
    # None (omitted) and [] (explicitly empty) have opposite meanings; never flatten with or []: overwrite retains old scripts
    # for the former and removes all for the latter (see skill_upload._adopt_unmanaged_files' managed calculation).
    scripts = None if body.scripts is None else [{"name": s.name, "content": s.content} for s in body.scripts]

    try:
        vs = validate_and_save_generated(
            body_md=body.body_md,
            scripts=scripts,
            files=None if body.files is None else [f.model_dump() for f in body.files],
            user_id=uid,
            dest_root=dest_root,
            existing_user_skills=user_skills,
            source_turn_id=body.source_turn_id,
            overwrite=body.overwrite,
            reserved_skill_names=_catalog_names(_catalog(request)),
        )
    except SkillUploadError:
        # Let FastAPI exception handler render in user's ui_language (i18n v1).
        raise

    await store.upsert(
        user_id=uid,
        name=vs.name,
        source_type="user_generated",
        source_path=str(vs.source_dir),
        version=vs.version,
        description=vs.description,
        enabled=True,
        size_bytes=vs.size_bytes,
        expected_content_hash=vs.content_hash,
        source_turn_id=body.source_turn_id,
        # skill-multiruntime S1 (G5): generation also persists risk reports (same as upload/team).
        risk_report_json=json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
        reset_verdict=True,
    )
    await _run_import_inventory(request, store, user, name=vs.name, skill_dir=Path(str(vs.source_dir)))

    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is not None:
        with contextlib.suppress(Exception):
            catalog.refresh()

    return await get_skill(name=vs.name, request=request, user=user, store=store)


# mcp license gate is on fixed POST /promote, matching /team-skills write endpoints (security review
# P2: promote creates team skill rows; without gating, unlicensed teams bypass entitlement to create/directly approve rows).
async def _promote_skill(
    body: SkillPromoteRequest,
    request: Request,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> dict[str, Any]:
    """个人 skill 提交到团队（PRD §3.4 promote；腿定案 tech-design §1）.

    个人 router 无条件挂载 → handler 内 flag check→404（review P1-7）。分支按团队侧
    现状定腿：无团队行→L1 首提；同名本人 creator_only→L1 提交（转 team_wide/pending，
    **不建候选槽**）；同名本人 pending/rejected→L1（等审）重提（覆盖 source_path，CAS
    键=盘 hash 自然刷新）；同名本人已批 team_wide→L2 提交更新（覆盖候选槽）；同名非
    本人→422 E_SKILL_NAME_TAKEN_BY_OTHER（指向对话改名重存）。admin 提交自己的=直批
    （PRD §3.1）。盘操作走 ``copy_validated_bundle`` 整目录快照——禁用
    validate_and_save_generated 的 dest/overwrite 语义（会丢 references/、rmtree live
    已批目录，review P1-5）；全程 per-skill 锁内，绝不触碰已批目录（L2 只写候选槽）。
    """
    from bowhead_core.workflow_creator_trigger import workflow_creator_enabled

    from bowhead_gateway._team_skill_locks import team_skill_lock
    from bowhead_gateway.routes import team_skills as team_routes

    if not workflow_creator_enabled():
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "workflow_creator_disabled",
        )
    if not user.team_id:
        raise skill_workflow_http_error(
            status.HTTP_403_FORBIDDEN,
            "team_required",
        )
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise skill_workflow_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "user_store_unavailable",
        )
    if NAME_PATTERN.fullmatch(body.name) is None:
        raise skill_workflow_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "E_SKILL_NAME_INVALID",
        )

    name = body.name
    uid = user.id
    inst = await store.get(user_id=uid, name=name)
    if not _is_user_skill_instance(inst):
        # Builtin/missing rows both return 404 (anti-enumeration; promotion does not apply to builtins).
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    spec = _load_user_skill_spec(inst, _user_dir_for(uid))
    if spec is None:
        raise skill_workflow_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "skill_bundle_invalid",
        )
    # skill-import batch 1: pending imports cannot be shared; lifecycle prerequisite, not authorization (tech-design r3 §3.4).
    if getattr(inst, "import_status", None) == "pending":
        raise skill_workflow_http_error(
            status.HTTP_409_CONFLICT,
            "skill_import_pending",
        )
    # Compare against the same body projection GET /skills/{name} serves (display_body), not the loader's
    # spec.body_md: the loader regex also strips blank lines after the closing ``---``, so a standard
    # SKILL.md with a blank line there could never match what the drawer submits.
    shown_body = display_body(read_bundle_manifest(Path(str(inst.source_path))))
    if body.title != (str(getattr(spec, "title", "") or "") or name) or shown_body != body.content:
        raise skill_workflow_http_error(
            status.HTTP_409_CONFLICT,
            "skill_promote_content_stale",
        )
    src_dir = Path(str(inst.source_path))
    is_admin = await team_routes._is_team_admin(user_store, user)
    team_root = team_routes._TEAM_SKILLS_ROOT / user.team_id
    # description feeds team rows (Lead routing): personal DB overrides take priority (same §3.8 semantics), with matching write guards.
    desc = str(inst.description or spec.description or "")[:MAX_DESC_LEN]

    event: str | None = None  # submitted | direct_approved (audit/notify uniformly outside lock).
    created = False
    l1_vs: Any = None  # Validated copy of an L1 re-submission; its title and risk report describe what reviewers now see.
    refresh_desc = False  # Refresh row description only for L1 submit/resubmit; never L2 (updated in approve 4b).
    async with team_skill_lock(user.team_id, name):
        rec = await user_store.get_team_skill_by_name(user.team_id, name, only_enabled=False, only_approved=False)
        if rec is not None and rec.created_by != uid:
            # Same name, different author → 422. The copy names no author: pending/rejected/creator-only ownership must not
            # leak (security review P2), and the actionable path is the same either way — rename in chat, save, resubmit
            # (there is no personal Skill rename endpoint).
            raise team_routes._coded_http(request, status.HTTP_422_UNPROCESSABLE_ENTITY, "E_SKILL_NAME_TAKEN_BY_OTHER", name=name)
        try:
            if rec is None:
                # ── L1 first submission: write team/<name>/ + create row (absence proven inside lock; existing directories can only be
                # crash orphans, cleaned by overwrite=True).
                from bowhead_core.user_store import TeamSkill, _id, _now

                vs = copy_validated_bundle(src_dir, team_root / name, expected_name=name, overwrite=True)
                sk = TeamSkill(
                    id=_id("oskill"),
                    team_id=user.team_id,
                    name=name,
                    display_name=(str(getattr(spec, "title", "") or "") or name),
                    description=desc,
                    source_path=str(vs.source_dir),
                    visibility="team_wide",
                    created_by=uid,
                    submitted_by=uid,
                    size_bytes=vs.size_bytes,
                    risk_report_json=getattr(inst, "risk_report_json", None),
                    created_at=_now(),
                    updated_at=_now(),
                )
                if is_admin:
                    sk.approved_sha256 = bundle_sha256(vs.source_dir)
                    sk.approved_by = uid
                    sk.approved_at = _now()
                else:
                    sk.approval = "pending"
                try:
                    await user_store.create_team_skill(sk)
                except ValueError as e:
                    # rec None proven inside lock: theoretically unreachable; defensive parity with create (disk rollback leaves no orphans).
                    shutil.rmtree(vs.source_dir, ignore_errors=True)
                    raise skill_workflow_http_error(
                        status.HTTP_409_CONFLICT,
                        "team_skill_create_conflict",
                    ) from e
                created = True
                if is_admin:
                    await _writethrough_team_grant(request, user_store, user, sk.id)
                    event = "direct_approved"
                else:
                    event = "submitted"
                rec_id = sk.id
            elif (rec.visibility or "team_wide") == "creator_only":
                # ── L1 submission: overwrite source_path + set team_wide/pending (no candidate, r3 P1-2).
                dest = team_routes._team_dir(user.team_id, rec.source_path)
                if dest is None:
                    raise skill_workflow_http_error(
                        status.HTTP_409_CONFLICT,
                        "team_skill_bundle_path_invalid",
                    )
                l1_vs = copy_validated_bundle(src_dir, dest, expected_name=name, overwrite=True)
                if not await user_store.submit_team_skill(user.team_id, rec.id, submitted_by=uid):
                    raise skill_workflow_http_error(
                        status.HTTP_409_CONFLICT,
                        "team_skill_submit_state_conflict",
                    )
                event = await _maybe_direct_approve_l1(request, user_store, user, rec.id, is_admin=is_admin)
                rec_id = rec.id
                refresh_desc = True
            elif rec.approval in ("pending", "rejected") and not rec.candidate_path:
                # ── L1 resubmit/pending-review resubmit: overwrite source_path, retain/restore pending (disk hash invalidates old CAS keys).
                dest = team_routes._team_dir(user.team_id, rec.source_path)
                if dest is None:
                    raise skill_workflow_http_error(
                        status.HTTP_409_CONFLICT,
                        "team_skill_bundle_path_invalid",
                    )
                l1_vs = copy_validated_bundle(src_dir, dest, expected_name=name, overwrite=True)
                if not await user_store.resubmit_team_skill(user.team_id, rec.id, submitted_by=uid):
                    raise skill_workflow_http_error(
                        status.HTTP_409_CONFLICT,
                        "team_skill_resubmit_state_conflict",
                    )
                event = await _maybe_direct_approve_l1(request, user_store, user, rec.id, is_admin=is_admin)
                rec_id = rec.id
                refresh_desc = True
            else:
                # ── L2 update submission: overwrite candidate slot <name>__candidate/ (live content/approval unchanged).
                cand = team_root / f"{name}__candidate"
                copy_validated_bundle(src_dir, cand, expected_name=name, overwrite=True)
                sha = bundle_sha256(cand)
                ts = await user_store.submit_team_skill_candidate(
                    user.team_id,
                    rec.id,
                    candidate_path=str(cand),
                    candidate_sha256=sha,
                    submitted_by=uid,
                )
                if ts is None:
                    raise skill_workflow_http_error(
                        status.HTTP_409_CONFLICT,
                        "team_skill_candidate_state_conflict",
                    )
                if is_admin:
                    rec2 = await user_store.get_team_skill(user.team_id, rec.id)
                    if rec2 is None:
                        raise skill_workflow_http_error(
                            status.HTTP_404_NOT_FOUND,
                            "team_skill_not_found",
                        )
                    await team_routes._approve_candidate(
                        request,
                        user_store,
                        user,
                        rec2,
                        team_routes.ApprovalCasBody(candidate_submitted_at=ts, candidate_sha256=sha),
                    )
                    event = "direct_approved"
                else:
                    event = "submitted"
                rec_id = rec.id
        except SkillUploadError as e:
            if e.code:
                raise team_routes._coded_http(request, status.HTTP_422_UNPROCESSABLE_ENTITY, e.code, **e.params) from e
            raise skill_workflow_http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "skill_bundle_invalid",
            ) from e
        # Refresh row description only for L1 submit/resubmit (admins review current content + routing copy; same for direct approval).
        # ⚠ Use explicit branch markers, not row state (CTO review P1): for the first L2 candidate, the initial locked read still
        # has rec.candidate_path=None; checking row state overwrites live Lead routing with unapproved personal descriptions,
        # without rollback on rejection. L2 descriptions always follow approved frontmatter through approve 4b.
        if refresh_desc:
            meta: dict[str, Any] = {"description": desc}
            if l1_vs is not None:
                # The row's title and risk chips must describe the re-submitted content, not the first submission:
                # reviewers can approve from the collapsed card, where the chips decide the outcome.
                meta["display_name"] = str(getattr(spec, "title", "") or "") or name
                meta["risk_report_json"] = json.dumps(l1_vs.risk_report, ensure_ascii=False) if l1_vs.risk_report else None
            with contextlib.suppress(Exception):
                await user_store.update_team_skill_meta(user.team_id, rec_id, **meta)
        # skill-import ⑨: carry deliberate enabling despite known blockers into team candidates (persistence/display in track B, migration 0064).
        # **Calculate server-side**, never accept from the request: this tells reviewers what authors might prefer to conceal;
        # accepting author input enables hiding it. pending already returned 409 above; only overridden/cleared/None remain here.
        # Write failure (migration 0064 missing / concurrent deletion) only loses provenance, never fails promotion.
        provenance = build_import_provenance(
            import_status=getattr(inst, "import_status", None),
            import_report_json=getattr(inst, "import_report_json", None),
        )
        # Always write, including None: provenance describes this submission, so a re-submission whose blockers were
        # cleared must not keep showing reviewers the previous "enabled despite blockers" record.
        with contextlib.suppress(Exception):
            await user_store.set_team_skill_import_provenance(user.team_id, rec_id, provenance)
    row = await user_store.get_team_skill(user.team_id, rec_id)
    if row is None:
        # Extreme concurrent DELETE after lock release: fail loudly rather than _public(None) producing 500.
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "team_skill_not_found",
        )
    if created:
        await team_routes._audit(user_store, user, AuditAction.TEAM_SKILL_CREATED, row)
    if event == "submitted":
        await team_routes._audit(user_store, user, AuditAction.TEAM_SKILL_SUBMITTED, row)
        await team_routes._notify_approval(request, store=user_store, actor=user, sk=row, event="submitted")
    elif event == "direct_approved":
        await team_routes._audit(user_store, user, AuditAction.TEAM_SKILL_APPROVED, row, extra={"direct": True, "via": "promote"})
    return team_routes._public(row, include_review=True)


async def _maybe_direct_approve_l1(
    request: Request,
    user_store: Any,
    user: User,
    skill_id: str,
    *,
    is_admin: bool,
) -> str:
    """promote L1 腿的 admin 直批：复用 approve 端点的 L1 流（锁由调用方持有）."""
    from bowhead_gateway.routes import team_skills as team_routes

    if not is_admin:
        return "submitted"
    rec = await user_store.get_team_skill(user.team_id, skill_id)
    if rec is None:
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "team_skill_not_found",
        )
    await team_routes._approve_first_submission(
        request,
        user_store,
        user,
        rec,
        team_routes.ApprovalCasBody(source_sha256=team_routes._source_sha256(rec)),
    )
    return "direct_approved"


async def _writethrough_team_grant(request: Request, user_store: Any, user: User, skill_id: str) -> None:
    """admin 直批的 team grant 写穿（best-effort，同 create 路；失败留 warning 对账）."""
    from bowhead_core import share_service

    try:
        access = getattr(request.app.state, "access_control_store", None)
        if access is not None and share_service.content_sharing_enabled():
            await share_service.set_audience(
                access,
                kind="team_skill",
                resource_id=skill_id,
                team_id=user.team_id,
                target="team_wide",
                actor_id=user.id,
                user_store=user_store,
            )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("team_skill.grant_writethrough_failed id=%s err=%s", skill_id, e)


async def _protected_delete_skill(
    *,
    skill_id: str,
    request: Request,
    user: User,
    store: SkillInstanceStore,
) -> None:
    if not user.team_id:
        raise skill_workflow_http_error(403, "team_required")
    token, idempotency_key = require_impact_headers(request)
    command = {"action": "delete", "proposed_change": {}}
    request_digest = resource_change_request_digest(
        resource_kind="skill",
        resource_id=skill_id,
        command=command,
    )
    from bowhead_core._db import open_shared_ctx

    moved: list[Any] = []
    committed = False
    replayed = False
    bundle_locks = contextlib.ExitStack()
    try:
        async with open_shared_ctx(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cached = await read_change_receipt(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if cached is not None:
                replayed = True
                await db.rollback()
            else:
                async with db.execute(
                    "SELECT * FROM skill_instances WHERE user_id=? AND id=?",
                    (user.id, skill_id),
                ) as cursor:
                    row = await cursor.fetchone()
                current_instance = _instance_from_row(row)
                if current_instance is None or not _is_user_skill_instance(current_instance):
                    raise skill_workflow_http_error(404, "skill_not_found")
                source_dir = Path(str(current_instance.source_path))
                if not _path_under(source_dir, _user_dir_for(user.id)):
                    raise skill_workflow_http_error(409, "skill_bundle_invalid")
                bundle_locks.enter_context(skill_bundle_lock(source_dir))
                try:
                    spec = load_skill(source_dir)
                except Exception as exc:
                    raise skill_workflow_http_error(409, "skill_bundle_invalid") from exc
                current = SimpleNamespace(
                    kind="personal",
                    spec=spec,
                    instance=current_instance,
                    team_skill=None,
                    source_type=current_instance.source_type,
                    resource_id=skill_id,
                    resource_ref=f"user:{skill_id}",
                    enabled=bool(current_instance.enabled),
                )
                selector = _impact_selector(current, name=current_instance.name)
                views = await ResourceUsageProjectionService(store.db_path).usage_many_in_connection(
                    db,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    selectors=(selector,),
                    governance_action_allowed=True,
                    include_groups=True,
                )
                view = views[skill_id]
                if view.summary.completeness != "complete":
                    raise ResourceUsageUnavailableError("resource_dependency_projection_incomplete")
                if delete_has_live_dependencies(view):
                    raise skill_workflow_http_error(
                        409,
                        "resource_delete_has_live_dependencies",
                        nested_error=True,
                    )
                schedule_ids = await affected_skill_schedule_ids(
                    db,
                    team_id=user.team_id,
                    resource_ref=current.resource_ref,
                )
                dependency_impact = usage_dependency_impact(view, schedule_ids)
                verified = verify_impact_token(
                    token,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    resource_kind="skill",
                    resource_id=skill_id,
                    command=command,
                    resource_state=_impact_target_state(current),
                    dependency_impact=dependency_impact,
                )
                moved = quarantine_paths(
                    [source_dir],
                    root=_user_dir_for(user.id),
                    command_identity=f"{user.team_id}:{user.id}:{idempotency_key}",
                )
                await pause_affected_schedules(db, schedule_ids)
                await db.execute(
                    "DELETE FROM skill_instances WHERE user_id=? AND id=?",
                    (user.id, skill_id),
                )
                receipt_response = {
                    "ok": True,
                    "deleted": skill_id,
                    "paused_schedule_ids": schedule_ids,
                }
                await write_change_receipt(
                    db,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    idempotency_key=idempotency_key,
                    resource_kind="skill",
                    resource_id=skill_id,
                    action="delete",
                    request_digest=request_digest,
                    impact_digest=verified.dependency_impact_digest,
                    response=receipt_response,
                )
                await write_cleanup_receipts(
                    db,
                    entries=moved,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    idempotency_key=idempotency_key,
                    resource_kind="skill",
                    resource_id=skill_id,
                )
                await db.commit()
                committed = True
    except (
        ResourceChangeIdempotencyConflict,
        ResourceImpactKeyUnavailable,
        ResourceImpactTokenInvalid,
        ResourceImpactTokenStale,
    ) as exc:
        if moved and not committed:
            restore_quarantined(moved)
        raise impact_http_error(exc) from exc
    except ResourceUsageUnavailableError as exc:
        if moved and not committed:
            restore_quarantined(moved)
        raise skill_workflow_http_error(503, "resource_impact_projection_incomplete") from exc
    except Exception:
        if moved and not committed:
            restore_quarantined(moved)
        raise
    finally:
        bundle_locks.close()
    if committed:
        await finalize_cleanup(store.db_path, moved)
    elif replayed:
        with contextlib.suppress(Exception):
            await retry_pending_cleanup(store.db_path)
    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is not None:
        with contextlib.suppress(Exception):
            catalog.refresh()


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_not_readonly)])
async def delete_skill(
    skill_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: SkillInstanceStore = Depends(_store),
) -> None:
    """删除 user_upload / user_generated；built-in 不允许删（只能 disable）."""
    if resource_impact_guard_enabled():
        current = await store.get_by_id(user_id=user.id, skill_id=skill_id)
        if current is not None:
            await _require_skill_grant_for_request(
                request,
                user,
                current.name,
                permission="write",
            )
        response.headers["Cache-Control"] = "private, no-store"
        await _protected_delete_skill(
            skill_id=skill_id,
            request=request,
            user=user,
            store=store,
        )
        return
    uid = user.id
    inst = await store.get_by_id(user_id=uid, skill_id=skill_id)
    if inst is None:
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_not_found",
        )
    if not _is_user_skill_instance(inst):
        if getattr(inst, "source_type", "") == "team_shared":
            # Per-user toggle marker (team skill), not a deletable asset; do not misleadingly call it builtin (review P3).
            raise skill_workflow_http_error(
                status.HTTP_400_BAD_REQUEST,
                "team_skill_preference_delete_forbidden",
            )
        raise skill_workflow_http_error(
            status.HTTP_400_BAD_REQUEST,
            "managed_skill_delete_forbidden",
        )
    await _require_skill_grant_for_request(request, user, inst.name, permission="write")
    with skill_bundle_lock(Path(inst.source_path)):
        # Delete files.
        try:
            p = Path(inst.source_path)
            if p.is_dir() and _path_under(p, _user_dir_for(uid)):
                shutil.rmtree(p)
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(
                "skill.delete_filesystem_failed path=%s err=%s",
                inst.source_path,
                e,
            )
        # Delete DB row.
        await store.delete_by_id(user_id=uid, skill_id=skill_id)
    # Refresh catalog so subsequent requests no longer see this entry.
    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is not None:
        with contextlib.suppress(Exception):
            catalog.refresh()
