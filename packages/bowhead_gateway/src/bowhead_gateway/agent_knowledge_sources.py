"""The original Agent writer's source-selection authorization adapter.

Selected files are checked one by one in each file's own Space. Selected knowledge bases
(``FEATURE_KNOWLEDGE_BASES``, phase 1 slice 5) are the ones the editor can use across their
Spaces: My knowledge bases, each Space's knowledge bases they can use as a member of it, and the
Team's. The mode waits for the Team's chunk key backfill (C7), and unknown and unusable IDs give
one generic error, so a save never tells whether a knowledge base exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_knowledge_policy import (
    MAX_SELECTED_FILES,
    MAX_SELECTED_KNOWLEDGE_BASES,
    KnowledgeAccess,
    KnowledgeAccessError,
    knowledge_base_mode_enabled,
    source_bindings_enabled,
)
from bowhead_core.kb.knowledge_base_access import KNOWLEDGE_USE
from bowhead_core.kb.knowledge_base_store import DEFAULT_AUDIENCE, KnowledgeBaseStore, default_knowledge_base_id
from bowhead_core.kb.retrieval_policy import MAX_TOP_K

from bowhead_gateway.kb_authorization import kb_file_authorizer

logger = logging.getLogger(__name__)


def source_validator(request, user_store):
    files = getattr(request.app.state, "kb_files_store", None)
    authorize = kb_file_authorizer(
        user_store, getattr(request.app.state, "access_control_store", None) or AccessControlStore(user_store.db_path), files_store=files
    )

    async def validate(file_ids, team_id, actor_user_id):
        if files is None:
            return False
        for identity in file_ids:
            file = await files.get(identity)
            if file is None or not await authorize(
                file,
                actor_user_id,
                team_id,
                file.scope_id if file.scope in {"space", "workspace"} else "",
            ):
                return False
        return True

    return validate


async def source_catalog(request, user_store, *, team_id, user_id, query, selected, offset, limit):
    files = getattr(request.app.state, "kb_files_store", None)
    validate = source_validator(request, user_store)
    items = []
    selected_items = []
    if files is not None:
        for file in await files.list_active_for_team_all_scopes(team_id):
            if file.file_id not in selected and query.casefold() not in file.filename.casefold():
                continue
            if not await validate((file.file_id,), team_id, user_id):
                continue
            item = {"id": file.file_id, "name": file.filename, "scope": "space" if file.scope == "workspace" else file.scope}
            if file.file_id in selected:
                selected_items.append(item)
            if query.casefold() in file.filename.casefold():
                items.append(item)
    items.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return {
        "enabled": source_bindings_enabled(),
        "available": files is not None,
        "items": items[offset : offset + limit],
        "total": len(items),
        "selected_items": selected_items,
        "max_selected": MAX_SELECTED_FILES,
        "top_k_min": 1,
        "top_k_max": MAX_TOP_K,
        "schema": KnowledgeAccess.model_json_schema(),
    }


# ─── Selected knowledge bases ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UsableKnowledgeBase:
    id: str
    home: str
    home_id: str


async def _spaces(user_store, user_id: str, team_id: str) -> list[dict[str, Any]]:
    return [row for row in await user_store.list_workspaces_for_user(user_id, team_id=team_id) if row.get("workspace_id")]


async def usable_knowledge_bases(
    request, user_store, *, team_id: str, user_id: str, wanted: tuple[str, ...] | None = None
) -> tuple[dict[str, UsableKnowledgeBase], list[dict[str, Any]]] | None:
    """The knowledge bases the person can use across their Spaces, and their Spaces; None when unreadable.

    The resolver answers per access context, so My and Team knowledge bases are read once without a
    Space and each Space's knowledge bases inside that Space (as its member). ``wanted`` limits the
    Space contexts to the homes of those IDs, for a save; the catalog reads every Space.
    """
    files = getattr(request.app.state, "kb_files_store", None)
    if files is None or not team_id or not user_id:
        return None
    access = getattr(request.app.state, "access_control_store", None) or AccessControlStore(user_store.db_path)
    authorize = kb_file_authorizer(user_store, access, files_store=files)
    try:
        spaces = await _spaces(user_store, user_id, team_id)
    except Exception:
        logger.warning("agent.knowledge_bases_spaces_unavailable team=%s", team_id, exc_info=True)
        return None
    space_ids = [str(row["workspace_id"]) for row in spaces]
    contexts = ["", *space_ids]
    if wanted is not None:
        rows = await KnowledgeBaseStore(files._db_path).get_many(wanted) if wanted else {}
        homes = {row.home_id for row in rows.values() if row.home == "space"}
        homes |= {space_id for space_id in space_ids if default_knowledge_base_id(team_id, "space", space_id) in wanted}
        contexts = ["", *(space_id for space_id in space_ids if space_id in homes)]
    found: dict[str, UsableKnowledgeBase] = {}
    for space_id in contexts:
        levels = await authorize.knowledge_levels(user_id, team_id, space_id)
        if levels is None:
            if not space_id:
                return None
            continue
        for identity in levels:
            home = levels.home_of(identity)
            if home is None or levels[identity] < KNOWLEDGE_USE or (home[0] == "space" and home[1] != space_id):
                continue
            found[identity] = UsableKnowledgeBase(identity, *home)
    return found, spaces


def _item(row: Any, usable: UsableKnowledgeBase) -> dict[str, Any]:
    if row is None:
        # Defaults are created lazily; their fixed audience is known without a row.
        audience, audience_level = DEFAULT_AUDIENCE[usable.home]
        return {
            "id": usable.id,
            "is_default": True,
            "home": usable.home,
            "home_id": usable.home_id,
            "name": None,
            "audience": audience,
            "audience_level": audience_level,
        }
    return {
        "id": row.id,
        "is_default": row.is_default,
        "home": row.home,
        "home_id": row.home_id,
        "name": row.name,
        "audience": row.audience,
        "audience_level": row.audience_level,
    }


async def knowledge_base_catalog(request, user_store, *, team_id: str, user_id: str, selected: list[str]) -> dict[str, Any]:
    """The selected-knowledge-base picker: whether the mode is offered, and the usable knowledge bases, grouped.

    ``state``: ``disabled`` (the switch is off: nothing is listed), ``preparing`` (the Team's chunk key
    backfill has not finished; reading this starts it), ``unavailable`` (no knowledge runtime or the
    knowledge bases cannot be read) or ``ready``. Groups are My, each Space (with its name) and the
    Team; a default knowledge base has no name and is named after its home by the interface.
    Selected IDs the person cannot use are left out of ``selected_items`` without saying why.
    """
    from bowhead_gateway.kb_maintenance import chunk_key_backfill_state

    base: dict[str, Any] = {"max_selected": MAX_SELECTED_KNOWLEDGE_BASES, "groups": [], "selected_items": []}
    if not knowledge_base_mode_enabled():
        return {**base, "enabled": False, "state": "disabled"}
    state = await chunk_key_backfill_state(request.app, team_id)
    resolved = await usable_knowledge_bases(request, user_store, team_id=team_id, user_id=user_id)
    if resolved is None:
        return {**base, "enabled": True, "state": "unavailable"}
    found, spaces = resolved
    files = request.app.state.kb_files_store
    rows = await KnowledgeBaseStore(files._db_path).get_many(found)
    items = {identity: _item(rows.get(identity), usable) for identity, usable in found.items()}

    def ordered(home: str, home_id: str) -> list[dict[str, Any]]:
        chosen = [item for item in items.values() if item["home"] == home and item["home_id"] == home_id]
        return sorted(chosen, key=lambda item: (not item["is_default"], (item["name"] or "").casefold(), item["id"]))

    groups = [{"home": "personal", "home_id": user_id, "name": None, "items": ordered("personal", user_id)}]
    for space in sorted(spaces, key=lambda row: (str(row.get("name") or "").casefold(), str(row["workspace_id"]))):
        space_id = str(space["workspace_id"])
        if entries := ordered("space", space_id):
            groups.append({"home": "space", "home_id": space_id, "name": space.get("name"), "items": entries})
    groups.append({"home": "team", "home_id": team_id, "name": None, "items": ordered("team", team_id)})
    return {
        **base,
        "enabled": True,
        "state": state,
        "groups": [group for group in groups if group["items"]],
        "selected_items": [items[identity] for identity in dict.fromkeys(selected) if identity in items],
    }


def knowledge_base_validator(request, user_store):
    """The save check of a selected-knowledge-base policy, run by the original writer.

    Refuses before the Team's chunk key backfill finished (and starts it), and with one generic
    ``agent_knowledge_base_unavailable`` for any ID the actor cannot use, whether it is unknown,
    deleted, another Team's, in a Space they are not a member of, or members-only.
    """

    async def validate(knowledge_base_ids, team_id, actor_user_id):
        from bowhead_gateway.kb_maintenance import chunk_key_backfill_state

        if not knowledge_base_mode_enabled():
            raise KnowledgeAccessError("agent_knowledge_bases_disabled")
        state = await chunk_key_backfill_state(request.app, team_id)
        if state == "unavailable":
            raise KnowledgeAccessError("agent_knowledge_bases_unavailable")
        if state != "ready":
            raise KnowledgeAccessError("agent_knowledge_bases_preparing")
        wanted = tuple(dict.fromkeys(knowledge_base_ids))
        if not wanted:
            return
        resolved = await usable_knowledge_bases(request, user_store, team_id=team_id, user_id=actor_user_id, wanted=wanted)
        if resolved is None:
            raise KnowledgeAccessError("agent_knowledge_bases_unavailable")
        if not set(wanted).issubset(resolved[0]):
            raise KnowledgeAccessError("agent_knowledge_base_unavailable")

    return validate
