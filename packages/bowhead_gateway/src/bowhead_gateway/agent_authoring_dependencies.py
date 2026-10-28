"""Reusable current-scope dependency preflight for Agent and Plugin authoring."""

import re
from typing import Any, Literal

import aiosqlite
from bowhead_core.agent_dependency_compiler import (
    AgentDependencyCompiler,
    SkillReadContext,
    parse_exact_skill_grants,
    with_python_sandbox_skill_grant,
)
from bowhead_core.resource_access import resource_grant_targets_for_tool
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.subagent_authoring_catalog import COMPILER_DERIVED_SKILL_TOOLS, SubagentAuthoringCatalogService
from bowhead_core.subagent_draft_store import SubagentDraft
from bowhead_core.user_store import User
from fastapi import Request

from bowhead_gateway._entitlement_gate import is_feature_licensed_request
from bowhead_gateway.agent_coach_resources import AuthoringResourceCatalog


class AuthoringDependencyError(ValueError):
    """A dependency failure whose `str()` stays the stable code, plus safe details naming the item."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = {key: value for key, value in details.items() if value is not None}


_SAFE_CODE_RE = re.compile(r"^[a-z0-9_]{1,80}$")


async def compile_authoring_resources(
    resource: AuthoringResourceCatalog,
    candidate: dict,
    *,
    audience_mode: Literal["private", "teamwide", "custom"],
    observed_tools: list[dict] | None = None,
) -> dict:
    parse_exact_skill_grants(candidate.get("skill_grants", []))
    tools, actor = await resource._items("tool")
    skills, _ = await resource._items("skill")
    visible_tools = {item["reference"]["id"]: item for item in tools}
    # A Skill follows its latest approved revision: the compiler rebases an
    # older grant revision, so eligibility is checked on the Skill identity.
    visible_skills = {(item["reference"]["source_kind"], item["reference"]["id"]): item for item in skills}
    if observed_tools is not None:
        if len(observed_tools) != len(candidate.get("tool_refs", [])) or {item["id"] for item in observed_tools} != set(
            candidate.get("tool_refs", [])
        ):
            raise ValueError("plugin_tool_selection_invalid")
        for reference in observed_tools:
            item = visible_tools.get(reference["id"])
            if item is None or item["reference"] != {key: value for key, value in reference.items() if value is not None}:
                raise ValueError("plugin_tool_unavailable_or_changed")
    # Stored Skill wrapper Tool refs are accepted and ignored; Skill grants decide them.
    unavailable_tools = [ref for ref in candidate.get("tool_refs", []) if ref not in visible_tools and ref not in COMPILER_DERIVED_SKILL_TOOLS]
    if unavailable_tools:
        raise AuthoringDependencyError("plugin_dependencies_unavailable", rule="tool_unavailable", tool_refs=unavailable_tools[:16])
    for grant in candidate.get("skill_grants", []):
        reference = grant["skill_ref"]
        item = visible_skills.get((reference["kind"], reference["id"]))
        if item is None:
            rule = "skill_unavailable"
        elif reference["name"] != item["skill_name"]:
            rule = "skill_renamed"
        elif audience_mode not in item["allowed_audiences"]:
            rule = "skill_audience_not_allowed"
        else:
            continue
        raise AuthoringDependencyError("plugin_dependencies_unavailable", rule=rule, skill_name=str(reference.get("name") or ""))
    catalog = await SubagentAuthoringCatalogService().build(
        store=resource.store,
        team_id=resource.user.team_id,
        actor_user_id=resource.user.id,
        actor_active=True,
        mcp_entitled=await is_feature_licensed_request(resource.request, "mcp"),
    )
    compiler = AgentDependencyCompiler(personal_skill_store=SkillInstanceStore(resource.store.db_path), team_skill_store=resource.store)
    compilation = await compiler.compile(
        candidate,
        context=SkillReadContext(team_id=resource.user.team_id, owner_user_id=resource.user.id, audience_mode=audience_mode),
        tool_catalog=catalog,
    )
    if actor != await resource._access():
        raise ValueError("plugin_dependencies_changed")
    # Permissions for implied Skill wrappers must be checked as well as the
    # explicitly selected Tools. No discovery result grants execution. The
    # wrappers are not listed for selection, so check the member's access to
    # them directly.
    denied = False
    for tool in compilation.tools:
        if tool.stable_ref in COMPILER_DERIVED_SKILL_TOOLS:
            if not await resource._allowed(actor, resource_grant_targets_for_tool(tool.stable_ref, include_argument_targets=False)):
                denied = True
        elif not visible_tools.get(tool.stable_ref, {}).get("can_select"):
            denied = True
    return {
        "status": "blocked" if denied else compilation.status,
        "reason_codes": ["plugin_tool_unavailable"] if denied else list(compilation.reason_codes),
        "blocked_items": [] if denied else list(compilation.blocked_items),
        "fingerprint": compilation.dependency_fingerprint,
        "compiler_version": compilation.compiler_version,
        "risk": compilation.risk.to_dict(),
        "effective_tool_refs": [tool.stable_ref for tool in compilation.tools],
        "skill_grants": [skill.grant.to_canonical_dict() for skill in compilation.skills],
        "safety_check": "not_run",
        "business_trial": "not_run",
    }


def _preflight_failure_details(exc: Exception) -> dict[str, Any]:
    """The failed check and the item it failed on; codes that are not stable tokens are not shown."""

    if isinstance(exc, PermissionError):
        return {"reason": "dependency_permission_denied"}
    code = str(getattr(exc, "code", None) or exc)
    details: dict[str, Any] = {"reason": code if _SAFE_CODE_RE.fullmatch(code) else "dependency_unavailable"}
    details.update(getattr(exc, "details", None) or {})
    return details


async def draft_dependency_preflight(
    request: Request, user: User, draft: SubagentDraft, definition: dict, catalog_fingerprint: str, *, connection: aiosqlite.Connection | None = None
) -> dict:
    """Bind validation to the exact currently permitted dependency closure.

    The final creator calls this again under its write transaction. That check
    fences database resource and permission changes between review and create.
    Ordinary draft completion does not depend on the Coach admission flag.
    """
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.subagent_draft_service import definition_fingerprint
    from bowhead_core.team_plugin_store import expand_plugins_conn
    from bowhead_core.team_subagent_store import TeamSubagentStore
    from bowhead_core.team_subagent_validation import TeamSubagentValidationError

    from bowhead_gateway.builtin_plugin_fields import expand_selected_builtin_plugins, has_resource_selection

    if not has_resource_selection(definition):
        return {"effective_tool_refs": list(definition.get("tool_refs", [])), "fingerprint": catalog_fingerprint}
    try:
        store = request.app.state.user_store
        if not draft.workspace_id or not await TeamSubagentStore(store.db_path).release_semantics_active():
            raise ValueError("exact_bindings_require_revisioned_creation")
        if connection is None:
            async with open_shared_ctx(str(store.db_path)) as db:
                expanded = await expand_plugins_conn(db, team_id=user.team_id, definition=definition)
        else:
            expanded = await expand_plugins_conn(connection, team_id=user.team_id, definition=definition)
        expanded = expand_selected_builtin_plugins(expanded)
        # Selecting the Python sandbox Tool directly requests its built-in Skill, as selecting
        # Code and computation does; the draft never has to name that Skill itself.
        expanded = with_python_sandbox_skill_grant(expanded)
        resource = AuthoringResourceCatalog(request, user, draft.workspace_id, "")
        result = await compile_authoring_resources(resource, expanded, audience_mode="private")
        if result["status"] != "ready":
            raise AuthoringDependencyError(
                "dependency_compilation_blocked", reason_codes=result["reason_codes"], blocked_items=result["blocked_items"]
            )
    except (ValueError, PermissionError) as exc:
        raise TeamSubagentValidationError(
            "subagent_dependencies_unavailable_or_changed",
            "Selected Skills or Plugins are unavailable or changed.",
            field="skill_grants",
            details=_preflight_failure_details(exc),
        ) from exc
    return {
        "effective_tool_refs": result["effective_tool_refs"],
        "fingerprint": definition_fingerprint(
            {
                "catalog": catalog_fingerprint,
                "dependencies": result["fingerprint"],
                "plugins": expanded.get("resolved_capabilities", []),
                **({"builtin_plugins": expanded["resolved_builtin_plugins"]} if expanded.get("resolved_builtin_plugins") else {}),
            }
        ),
    }
