"""Skills the user selected with `/` stay the package they selected.

A selection names one exact source (built-in, personal or team) at one version.
Lead and every helper of the Turn inherit `selected_skill_resources` from the
Lead context, and name-keyed Skill lookups (`skill_read`, `skill_invoke`, the
execution gate, the per-Turn read cache) use that source instead of resolving
the name again. A personal copy with the same name therefore never stands in
for a selected team Skill. The version is checked on every use: a Skill edited
after the selection is refused with a correction, not read at its new version.
See docs/sprints/skill-bundle-read-2026-11-20/tech-design.md §6.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import anyio
from bowhead_skill.loader import load_skill

from bowhead_core.chat_references import SkillResourceRef

logger = logging.getLogger(__name__)
HISTORY_TURNS_SEARCHED = 100


def selected_resource(ctx: Any, name: str) -> SkillResourceRef | None:
    """The source the user selected under this name in the Turn, if any."""
    resources = getattr(ctx, "selected_skill_resources", None)
    if not isinstance(resources, Mapping) or not isinstance(name, str):
        return None
    resource = resources.get(name.strip())
    return resource if isinstance(resource, SkillResourceRef) else None


def selected_skill_changed(name: str) -> dict:
    return {
        "ok": False,
        "error_kind": "selected_skill_changed",
        "error": (
            f"The Skill '{name}' the user selected with / was edited, disabled or removed after it was selected, "
            "so it cannot be used at its new state in this request. Tell the user to select it again with /."
        ),
    }


async def resolve_selected_skill(ctx: Any, name: str) -> Any:
    """Return the selected source's `SkillSpec`, a refusal dict, or None when `name` was not selected.

    The exact source resolver rechecks visibility and use, and the package bytes
    must still match the selected version.
    """
    resource = selected_resource(ctx, name)
    if resource is None:
        return None
    from bowhead_core.ordinary_lead_material import _scope, _sources
    from bowhead_core.skill_package_material import SkillPackageMaterialError
    from bowhead_core.skill_read_resolution import SkillReadResolutionError, repeat_once_if_source_changed

    catalog_name = resource.id.removeprefix("builtin:") if resource.source_kind == "builtin" else ""
    try:
        sources = _sources(ctx, resource, catalog_name)
        # Access that changed during the check (another session editing groups) is checked again, not reported as an edit.
        package = await repeat_once_if_source_changed(lambda: sources.prepare_runtime_package(resource, scope=_scope(ctx)))
        if package.path is None or package.proof.name != name.strip():
            raise ValueError("selected_skill_source_unavailable")
        if resource.source_kind == "builtin":
            spec = ctx.skill_catalog.get(catalog_name)
            if spec is None:
                raise ValueError("selected_skill_source_unavailable")
            return spec
        return await anyio.to_thread.run_sync(load_skill, package.path)
    except (SkillReadResolutionError, SkillPackageMaterialError, ValueError, OSError) as error:
        logger.info("selected_skill.unavailable name=%s cause=%s", name, getattr(error, "code", None) or type(error).__name__)
        return selected_skill_changed(name.strip())
    except Exception as error:
        # Lookup machinery failed (no store, no catalog); keep the refusal actionable.
        logger.warning("selected_skill.resolve_failed name=%s cause=%s", name, type(error).__name__)
        return selected_skill_changed(name.strip())


async def history_selected_resources(ctx: Any, *, session_id: str, turn_id: str, names: Sequence[str]) -> dict[str, SkillResourceRef]:
    """The newest earlier selection of each name still shown in Lead's history."""
    wanted = {name for name in names if isinstance(name, str) and name}
    store = getattr(ctx, "session_store", None)
    user_id, workspace_id = getattr(ctx, "user_id", None), getattr(ctx, "workspace_id", None)
    found: dict[str, SkillResourceRef] = {}
    if not wanted or store is None or not user_id or not workspace_id or not session_id:
        return found
    try:
        turns = await store.list_turns(session_id=session_id)
    except Exception as error:
        logger.warning("selected_skill.history_unavailable session=%s cause=%s", session_id, type(error).__name__)
        return found
    names_by_id: dict[str, str | None] = {}
    # A name not found in the newest Turns keeps the earlier name-based lookup.
    for turn in list(reversed(turns))[:HISTORY_TURNS_SEARCHED]:
        if wanted <= set(found):
            break
        earlier = getattr(turn, "id", None) or getattr(turn, "turn_id", None)
        if not earlier or earlier == turn_id:
            continue
        try:
            source = await store.get_chat_turn_user_source(turn_id=earlier, user_id=user_id, workspace_id=workspace_id)
        except Exception as error:
            # One unreadable earlier Turn leaves its names to the name-based lookup.
            logger.warning("selected_skill.history_turn_unavailable turn=%s cause=%s", earlier, type(error).__name__)
            continue
        for resource in getattr(source, "selected_skills", None) or ():
            if resource.id not in names_by_id:
                names_by_id[resource.id] = await _resource_name(ctx, resource)
            name = names_by_id[resource.id]
            if name in wanted and name not in found:
                found[name] = resource
    return found


async def _resource_name(ctx: Any, resource: SkillResourceRef) -> str | None:
    """The current name of a selected source; None when it is gone (its use is then refused)."""
    if resource.source_kind == "builtin":
        return resource.id.removeprefix("builtin:")
    try:
        if resource.source_kind == "user":
            record = await ctx.skill_instance_store.get_by_id(user_id=ctx.user_id, skill_id=resource.id)
            return getattr(record, "name", None)
        for record in await ctx.team_skill_store.list(ctx.team_id):
            if getattr(record, "id", None) == resource.id:
                return getattr(record, "name", None)
    except Exception as error:
        logger.warning("selected_skill.history_name_unavailable cause=%s", type(error).__name__)
    return None
