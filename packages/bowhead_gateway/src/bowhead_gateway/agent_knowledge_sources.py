"""The original Agent writer's source-selection authorization adapter."""

from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_knowledge_policy import MAX_SELECTED_FILES, KnowledgeAccess, source_bindings_enabled
from bowhead_core.kb.retrieval_policy import MAX_TOP_K

from bowhead_gateway.kb_authorization import kb_file_authorizer


def source_validator(request, user_store):
    files = getattr(request.app.state, "kb_files_store", None)
    authorize = kb_file_authorizer(user_store, getattr(request.app.state, "access_control_store", None) or AccessControlStore(user_store.db_path))

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
