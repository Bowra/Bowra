"""Access-aware helpers for Bowhead skills."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.resource_access import builtin_capability_ids_for_skill

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


async def skill_visible_by_grants(
    ctx: Any,
    skill_name: str,
    *,
    permission: str = "use",
    audit: bool = False,
    audit_payload: dict[str, Any] | None = None,
    team_skill_id: str | None = None,
    team_skill_created_by: str | None = None,
) -> bool:
    """Return whether resource grants allow this skill in the current context.

    In shadow mode, denied decisions are logged/audited but still return True.

    skill-team-governance P1a：调用方已知 team origin 时传 ``team_skill_id``（+
    ``team_skill_created_by``）——skill target 换 ``("team_skill", id)`` 键（对齐
    content-sharing，audit #11）；作者本人对 ``not_in_allowlist`` 拒绝豁免（显式
    deny 仍生效）。不传 = 名字键现状（catalog/user 面零变化）。
    """
    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is None or not access_ctx:
        return True
    enforce = False
    try:
        from bowhead_core.access_control import resource_grants_enforce_enabled

        enforce = resource_grants_enforce_enabled()
        from contextlib import aclosing

        from bowhead_core.grant_evaluation import iter_grant_verdicts

        targets = _skill_targets(skill_name, permission, team_skill_id)
        async with aclosing(iter_grant_verdicts(ctx, access_store, access_ctx, targets, team_skill_created_by=team_skill_created_by)) as verdicts:
            async for verdict in verdicts:
                if verdict.allowed:
                    continue
                if verdict.owner_exempt:
                    # Narrow owner exemption (tech-design P1a): exempt only not_in_allowlist, and only for
                    # skill targets (still check builtin_capability); deny remains effective for authors.
                    logger.info("resource_grant.team_skill_owner_exempt skill=%s", skill_name)
                    continue
                logger.info(
                    "resource_grant.skill_would_deny skill=%s resource=%s:%s permission=%s reason=%s enforce=%s",
                    skill_name,
                    verdict.resource_kind,
                    verdict.resource_id,
                    verdict.permission,
                    verdict.reason,
                    enforce,
                )
                if audit:
                    await access_store.record_runtime_denied(
                        access_ctx,
                        resource_kind=verdict.resource_kind,
                        resource_id=verdict.resource_id,
                        permission=verdict.permission,
                        reason=str(verdict.reason or "denied"),
                        target_kind="skill",
                        target_id=skill_name,
                        payload={"skill_name": skill_name, "enforce": enforce, **(audit_payload or {})},
                    )
                if enforce:
                    return False
        return True
    except Exception as exc:
        logger.warning(
            "resource_grant.skill_check_failed skill=%s enforce=%s err=%s",
            skill_name,
            enforce,
            exc,
        )
        return not enforce


def _skill_targets(skill_name: str, permission: str, team_skill_id: str | None = None) -> list[tuple[str, str, str]]:
    skill_target = ("team_skill", team_skill_id, permission) if team_skill_id else ("skill", skill_name, permission)
    return [skill_target, *(("builtin_capability", capability_id, "use") for capability_id in builtin_capability_ids_for_skill(skill_name))]


async def skills_visible_by_grants(ctx: Any, skill_names: Iterable[str], *, permission: str = "use") -> dict[str, bool]:
    """``skill_visible_by_grants`` for a listing, decided in one batch per resource kind.

    Each name gets the value and log lines the single check gives without audit or a team skill
    key: the first refusal is logged and, when enforced, hides the Skill; a failed decision hides
    every name when enforced and shows it in shadow mode.
    """
    names = list(dict.fromkeys(str(name) for name in skill_names))
    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is None or not access_ctx or not names:
        return dict.fromkeys(names, True)
    if not callable(getattr(access_store, "explain_decisions", None)):
        return {name: await skill_visible_by_grants(ctx, name, permission=permission) for name in names}
    enforce = False
    try:
        from bowhead_core.access_control import resource_grants_enforce_enabled
        from bowhead_core.grant_evaluation import grant_verdicts_batch

        enforce = resource_grants_enforce_enabled()
        verdict_lists = await grant_verdicts_batch(ctx, access_store, access_ctx, [_skill_targets(name, permission) for name in names])
    except Exception as exc:
        logger.warning("resource_grant.skill_check_failed skills=%d enforce=%s err=%s", len(names), enforce, exc)
        return dict.fromkeys(names, not enforce)
    visible: dict[str, bool] = {}
    for name, verdicts in zip(names, verdict_lists, strict=True):
        visible[name] = True
        for verdict in verdicts:
            if verdict.allowed:
                continue
            logger.info(
                "resource_grant.skill_would_deny skill=%s resource=%s:%s permission=%s reason=%s enforce=%s",
                name,
                verdict.resource_kind,
                verdict.resource_id,
                verdict.permission,
                verdict.reason,
                enforce,
            )
            if enforce:
                visible[name] = False
                break
    return visible


def lead_can_read_skill(spec: Any) -> bool:
    """A Skill the system Lead's index lists is one whose body it can read.

    Peer-only Skills are working details of the Agent that runs them. A Skill
    granted by a general Plugin belongs to the Agents that selected that Plugin.
    """
    from bowhead_core.system_plugins import general_plugin_skill_names

    return getattr(spec, "audience", "all") != "peer" and str(getattr(spec, "name", "")) not in general_plugin_skill_names()


class _FilteredCatalog:
    def __init__(self, specs: list[Any]) -> None:
        self._specs = specs

    def list_specs(self) -> list[Any]:
        return list(self._specs)


async def filter_skill_specs_by_grants(ctx: Any, specs: Iterable[Any]) -> list[Any]:
    specs = list(specs)
    visible = await skills_visible_by_grants(ctx, [str(spec.name) for spec in specs])
    return [spec for spec in specs if visible[str(spec.name)]]


# Method/SOP summary in the Lead catalog: explains the 17 analyst-method-* / analyst-sop-* skills hidden
# by audience filtering (the former ROUTE-5 line, previously duplicated in lead.py and this caller).
_LEAD_METHOD_SUMMARY = (
    "\n- **analyst-method-\\* / analyst-sop-\\*** (finance / business / compliance method library + analysis SOPs)"
    " — the analyst-type peer in the roster below that matches the task skill_reads them as needed; "
    "the Lead only routes to the right peer and never calls them directly."
)


def render_lead_skill_index(catalog: Any) -> str:
    """Lead 视图 skill 目录块的唯一渲染点（audience 过滤 + 路由 header + 汇总行）.

    静态缓存路径（prompts/lead.py）与 grants-override 路径
    （render_access_filtered_skill_index）共用，消除原先两处重复实现
    （docs/skills-tools-ssot.md §5-G4）。空目录返回 ""（不带汇总行）。
    """
    from bowhead_skill import render_index

    block = render_index(_FilteredCatalog([spec for spec in catalog.list_specs() if lead_can_read_skill(spec)]), audience_view="lead").strip()
    if not block:
        return ""
    return block + _LEAD_METHOD_SUMMARY


async def render_access_filtered_skill_index(
    ctx: Any,
    catalog: Any,
) -> str | None:
    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is None or not access_ctx:
        return None
    enforce = False
    try:
        from bowhead_core.access_control import resource_grants_enforce_enabled

        enforce = resource_grants_enforce_enabled()
        if not enforce:
            return None
        specs = await filter_skill_specs_by_grants(ctx, catalog.list_specs())
        return render_lead_skill_index(_FilteredCatalog(specs))
    except Exception as exc:
        logger.warning(
            "resource_grant.skill_index_filter_failed enforce=%s err=%s",
            enforce,
            exc,
        )
        if enforce:
            return ""
        return None
