"""Shared resource-grant helpers for Bowhead-managed resources.

This module is intentionally pure and small: gateway routes use it to validate
admin input, while runtime code uses it to map tool calls to grant targets.
Datarc data permissions are not represented here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

GrantTarget = tuple[str, str, str]


@dataclass(frozen=True)
class BuiltinCapability:
    id: str
    label: str
    description: str
    tool_names: frozenset[str]
    tool_prefixes: tuple[str, ...] = ()
    skill_names: frozenset[str] = frozenset()


BUILTIN_CAPABILITIES: dict[str, BuiltinCapability] = {
    "kb_search": BuiltinCapability(
        id="kb_search",
        label="Knowledge base",
        description="Search and query Bowhead knowledge-base content.",
        tool_names=frozenset(
            {
                "kb_search",
                "kb_list_tables",
                "kb_query_records",
                "kb_get_record",
            }
        ),
    ),
    "skill_invoke": BuiltinCapability(
        id="skill_invoke",
        label="Skills",
        description="List, read, and invoke Bowhead skills.",
        tool_names=frozenset(
            {
                "skill_list",
                "skill_read",
                "skill_invoke",
                "skill_save_generated",
            }
        ),
    ),
    "web_search": BuiltinCapability(
        id="web_search",
        label="Web search",
        description="Search the public web (web_search), and hand research questions to web_deep_research.",
        # web_deep_research searches and reads pages: both web_search and web_fetch grants apply; disabling either makes it unavailable.
        tool_names=frozenset({"web_search", "web_deep_research"}),
    ),
    "web_fetch": BuiltinCapability(
        id="web_fetch",
        label="Web fetch",
        description="Read known URLs and sites: web_fetch, plus the Tavily-backed web_extract / web_map / web_crawl / web_deep_research.",
        # web_extract / web_map / web_crawl / web_deep_research also read pages by URL (Tavily backend); use the same authorization surface,
        # or teams with web_fetch disabled can bypass the restriction through them.
        tool_names=frozenset({"web_fetch", "web_extract", "web_map", "web_crawl", "web_deep_research"}),
    ),
    "export": BuiltinCapability(
        id="export",
        label="Export",
        description="Generate downloadable or externally portable deliverables.",
        tool_names=frozenset(
            {
                "canvas_add_html_artifact",
                "canvas_add_external_iframe",
            }
        ),
    ),
    "python_sandbox": BuiltinCapability(
        id="python_sandbox",
        label="Python sandbox",
        description="Run Python code through the python_sandbox skill.",
        tool_names=frozenset(),
        skill_names=frozenset({"python_sandbox"}),
    ),
    "subagent": BuiltinCapability(
        id="subagent",
        label="Subagents",
        description="Delegate work to Bowhead callable peers.",
        tool_names=frozenset({"subagent_spawn"}),
    ),
}

_CAPABILITY_ALIASES: dict[str, str] = {
    "kb": "kb_search",
    "knowledge_base": "kb_search",
    "skills": "skill_invoke",
    "skill": "skill_invoke",
    "web": "web_search",
    "browser_search": "web_search",
    "fetch": "web_fetch",
    "read_url": "web_fetch",
    "web_read": "web_fetch",
    "python": "python_sandbox",
    "sandbox": "python_sandbox",
    "subagents": "subagent",
}

_PERMISSION_ALIASES: dict[str, str] = {
    "view": "read",
    "read": "read",
    "use": "use",
    # ``invoke`` remains a compatibility alias for non-Skill resources.  Skill
    # resources have an operation-specific ticket; see ``canonical_permission``.
    "invoke": "use",
    "edit": "write",
    "write": "write",
    "manage": "admin",
    "share": "admin",
    "admin": "admin",
}


def canonical_permission(permission: str, *, resource_kind: str | None = None) -> str | None:
    """Canonicalize an admin-facing permission in its resource namespace.

    ``invoke`` historically collapsed into the generic ``use`` permission.  Keep
    that compatibility for tools, connections, and other resource kinds, while
    allowing Skill grants to persist a distinct invocation ticket.  Runtime
    adoption is staged separately; accepting the ticket must not silently change
    existing ``use`` rows or the script feature-flag defaults.
    """
    raw = (permission or "").strip().lower()
    if raw == "invoke" and resource_kind in {"skill", "team_skill"}:
        return "invoke"
    return _PERMISSION_ALIASES.get(raw)


# Content sharing (docs/sprints/content-permissions/): kinds a resource owner may
# share via the share API, mapped to the single wave-1 grant permission per kind.
# resource_id formats: team_subagent=osa_*, team_skill=oskill_*, report=session_id.
SHAREABLE_KINDS: dict[str, str] = {
    "team_subagent": "use",
    "team_skill": "use",
    "report": "read",
}


def canonical_builtin_capability_id(capability_id: str) -> str | None:
    raw = (capability_id or "").strip().lower()
    cid = _CAPABILITY_ALIASES.get(raw, raw)
    return cid if cid in BUILTIN_CAPABILITIES else None


def list_builtin_capability_ids() -> list[str]:
    return sorted(BUILTIN_CAPABILITIES)


def builtin_capability_ids_for_tool(tool_name: str) -> list[str]:
    out: list[str] = []
    for cid, cap in BUILTIN_CAPABILITIES.items():
        if tool_name in cap.tool_names or any(tool_name.startswith(p) for p in cap.tool_prefixes):
            out.append(cid)
    return sorted(out)


def builtin_capability_ids_for_skill(skill_name: str) -> list[str]:
    out: list[str] = []
    for cid, cap in BUILTIN_CAPABILITIES.items():
        if skill_name in cap.skill_names:
            out.append(cid)
    return sorted(out)


async def resolve_skill_grant_target(
    ctx: Any,
    skill_name: str,
    *,
    permission: str = "use",
) -> tuple[GrantTarget, str | None]:
    """skill-team-governance P1a：skill grant 键按**实际 resolve origin** 定.

    catalog/user origin → ``("skill", name, perm)``（名字键，现状不变）；team origin →
    ``("team_skill", rec.id, perm)``（与 content-sharing 同键，修 audit #11 双体系
    keying 不一致——enforce ON 下 id 键 allowlist 对名字键查询恒 miss=纸面化）。
    返回 ``(target, team_created_by)``：created_by 供调用方做**收窄版 owner 豁免**
    （仅 ``not_in_allowlist`` 时放行作者本人；显式 deny 仍生效——admin 可 deny 作者）。

    origin 判定与 resolve_skill/effective 优先级逐字一致（catalog > user 占名 > team），
    **禁按名字反查 team 表**：同名 shadow 下作者个人执行必须按名字键。任何异常回退
    名字键（现状行为，fail-open 到旧语义）。
    """
    default_target: GrantTarget = ("skill", skill_name, permission)
    grants = getattr(ctx, "skill_runtime_grants", None)
    if grants is not None:
        from bowhead_core.agent_dependency_compiler import parse_exact_skill_grants
        from bowhead_core.user_store import UserStore

        # A published source does not participate in legacy same-name priority.
        # Malformed/missing exact grants must never fall back to the name resolver.
        parsed = parse_exact_skill_grants([grant.to_canonical_dict() for grant in grants])
        grant = next((item for item in parsed if item.name == skill_name), None)
        if grant is None:
            raise ValueError("assigned_skill_unavailable")
        if grant.kind == "team":
            users = UserStore(ctx.access_control_store.db_path)
            record = await users.get_team_skill(ctx.team_id, grant.id, only_enabled=False)
            return ("team_skill", grant.id, permission), record.created_by if record else None
        return default_target, None
    try:
        catalog = getattr(ctx, "skill_catalog", None)
        if catalog is not None and catalog.get(skill_name) is not None:
            return default_target, None
        uid = getattr(ctx, "user_id", None)
        ustore = getattr(ctx, "skill_instance_store", None)
        if uid and ustore is not None:
            from bowhead_skill.effective import is_user_skill_instance

            inst = await ustore.get(user_id=uid, name=skill_name)
            if is_user_skill_instance(inst):
                return default_target, None
        tid = getattr(ctx, "team_id", None)
        tstore = getattr(ctx, "team_skill_store", None)
        if tid and tstore is not None:
            rec = await tstore.get_by_name(tid, skill_name, only_enabled=True)
            rec_id = getattr(rec, "id", None) if rec is not None else None
            if rec_id:
                return ("team_skill", str(rec_id), permission), getattr(rec, "created_by", None)
    except Exception:
        logger.warning("resource_grant.skill_origin_resolve_failed skill=%s — falling back to name key", skill_name)
    return default_target, None


# Batch 5ⓑ: flat sandbox tool name and the actual executed skill name (authorize by the latter).
_FLAT_SANDBOX_TOOL = "python_exec"
_FLAT_SANDBOX_SKILL = "python_sandbox"


def resource_grant_targets_for_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    include_argument_targets: bool = True,
) -> list[GrantTarget]:
    """Map a tool call/schema to resource grants.

    ``include_argument_targets=False`` is used while building visible tool
    schemas: there is no concrete skill name yet for ``skill_invoke``.
    Execute-time checks pass real arguments and therefore include skill-specific
    grants such as ``skill:python_sandbox``.
    """
    args = arguments or {}
    capability_ids = builtin_capability_ids_for_tool(tool_name)
    targets: list[GrantTarget] = [("tool", tool_name, "use")]
    for capability_id in capability_ids:
        targets.append(("builtin_capability", capability_id, "use"))

    if tool_name == "skill_save_generated":
        # Saving a generated skill mutates per-user skill storage. Keep the
        # normal invoke/use gate, but require write as well so "can invoke
        # skills" does not imply "can create or overwrite skills".
        targets.append(("tool", tool_name, "write"))
        for capability_id in capability_ids:
            targets.append(("builtin_capability", capability_id, "write"))
        if include_argument_targets:
            from bowhead_core.skill_upload import peek_skill_name

            skill_name = peek_skill_name(str(args.get("body_md") or ""))
            if skill_name:
                targets.append(("skill", skill_name, "write"))
        return targets

    if tool_name == _FLAT_SANDBOX_TOOL:
        # Batch 5ⓑ: the flat sandbox executes `python_sandbox`; authorization must be **identical** to the old
        # `skill_invoke(name="python_sandbox")` path, or teams denied the python_sandbox capability
        # can still execute arbitrary Python via another entry (same root cause as the role-binding hole, missed on grants).
        targets.append(("skill", _FLAT_SANDBOX_SKILL, "invoke"))
        for capability_id in builtin_capability_ids_for_skill(_FLAT_SANDBOX_SKILL):
            targets.append(("builtin_capability", capability_id, "use"))
        for capability_id in builtin_capability_ids_for_tool("skill_invoke"):
            targets.append(("builtin_capability", capability_id, "use"))
        return targets

    if tool_name in {"skill_read", "skill_invoke"}:
        if include_argument_targets:
            skill_name = str(args.get("name") or args.get("skill_id") or "").strip()
            if skill_name:
                operation = "read" if tool_name == "skill_read" else "invoke"
                targets.append(("skill", skill_name, operation))
                for capability_id in builtin_capability_ids_for_skill(skill_name):
                    targets.append(("builtin_capability", capability_id, "use"))
        return targets

    if tool_name.startswith("mcp."):
        parts = tool_name.split(".", 2)
        if len(parts) >= 3 and parts[1]:
            targets.append(("mcp_server", parts[1], "use"))
        return targets

    if tool_name.startswith("teammcp."):
        parts = tool_name.split(".", 2)
        if len(parts) >= 3 and parts[1]:
            targets.extend(
                [
                    ("team_tool_source", parts[1], "use"),
                    ("team_tool_op", tool_name, "use"),
                ]
            )
        return targets

    if tool_name == "kb_search":
        # The handler performs per-scope filtering so a user with workspace KB
        # access but no team KB access can still search the allowed collections.
        return targets

    if tool_name in {"kb_list_tables", "kb_query_records", "kb_get_record"}:
        targets.append(("__kb_current_workspace__", "", "read"))

    return targets
