"""Current source checks for Agent reads and retained typed evidence."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from bowhead_core.agent_knowledge_policy import KnowledgeAccessError, KnowledgeScope
from bowhead_core.kb.permission import KbPermission, ScopeContext

if TYPE_CHECKING:
    import aiosqlite

KNOWLEDGE_TOOLS = frozenset({"kb_search", "kb_list_tables", "kb_query_records", "kb_get_record", "kb_list_sources"})


async def readable_source(ctx, file_id: str, *, generation: Any = ..., enforce: bool = False) -> bool:
    scope = ctx.knowledge_scope
    if scope is None and not enforce:
        return True
    if scope is not None:
        scope.require_context(ctx)
    if scope is not None and not scope.permits(file_id):
        return False
    if ctx.kb_files_store is None or ctx.kb_file_authorizer is None:
        raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
    file = await ctx.kb_files_store.get(file_id)
    caller = ScopeContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id)
    return bool(
        file
        and not file.soft_deleted_at
        and file.team_id == ctx.team_id
        and KbPermission.can_read(file, caller)
        and (generation is ... or file.active_generation_id == generation)
        and await ctx.kb_file_authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id)
    )


async def prepare_source_read(ctx, tool_name: str):
    if ctx.knowledge_scope is None or tool_name not in KNOWLEDGE_TOOLS:
        return ctx
    ctx.knowledge_scope.require_context(ctx)
    result = copy.copy(ctx)
    result.knowledge_read_prepared = True
    result.knowledge_pending_citations = []
    result.knowledge_source_generations = {}
    ids = ctx.knowledge_scope.file_ids
    if tool_name == "kb_list_sources":
        result.knowledge_navigation_pending = {}
        return result
    if ids is None and tool_name != "kb_search":
        if ctx.kb_files_store is None:
            raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
        files = await ctx.kb_files_store.list_for_scopes_filtered(
            team_id=ctx.team_id,
            personal_owner_id=ctx.user_id,
            space_id=ctx.workspace_id,
            team_scope_id=ctx.team_id,
        )
        ids = tuple(file.file_id for file in files)
    if ids is not None:
        for file_id in ids:
            if await readable_source(ctx, file_id):
                file = await ctx.kb_files_store.get(file_id)
                if file is not None:
                    result.knowledge_source_generations[file_id] = file.active_generation_id
    result.knowledge_readable_file_ids = None if ids is None else tuple(result.knowledge_source_generations)
    return result


def require_prepared_read(ctx) -> None:
    if ctx.knowledge_scope is not None and not ctx.knowledge_read_prepared:
        raise KnowledgeAccessError("knowledge_source_authorization_unavailable")


def retained_knowledge_scope(ctx) -> KnowledgeScope | None:
    if ctx.knowledge_scope is not None or not ctx.knowledge_source_uses:
        return ctx.knowledge_scope
    return KnowledgeScope(schema_version=1, user_id=ctx.user_id, team_id=ctx.team_id, workspace_id=ctx.workspace_id, file_ids=None, bindings=())


async def finish_source_read(ctx, tool_name: str, result):
    if tool_name == "subagent_spawn" and isinstance(result, dict) and (ctx.knowledge_source_uses or ctx.knowledge_navigation_uses):
        sources = dict(ctx.knowledge_source_uses)
        await validate_retained_sources(ctx, sources)
        decorated = copy.copy(result)
        decorated.update(
            {
                "knowledge_provenance": {
                    "schema_version": 1,
                    "policy_fingerprint": (
                        retained_knowledge_scope(ctx)
                        or KnowledgeScope(
                            schema_version=1, user_id=ctx.user_id, team_id=ctx.team_id, workspace_id=ctx.workspace_id, file_ids=None, bindings=()
                        )
                    ).fingerprint,
                    "sources": sources,
                },
                **({"knowledge_navigation_provenance": dict(ctx.knowledge_navigation_uses)} if ctx.knowledge_navigation_uses else {}),
            }
        )
        return decorated
    if (ctx.knowledge_scope is None and tool_name != "kb_list_sources") or tool_name not in KNOWLEDGE_TOOLS or not isinstance(result, dict):
        return result
    sources: dict[str, Any] = {}
    if tool_name == "kb_search":
        chunks = result.get("chunks") or []
        permitted = []
        for chunk in chunks:
            file_id = chunk.get("file_id", "")
            generation = chunk.get("generation_id")
            if await readable_source(ctx, file_id, generation=generation):
                permitted.append(chunk)
                sources[file_id] = generation
        result = {**result, "chunks": permitted}
        if len(permitted) != len(chunks):
            result.update(partial=True, hint="Some sources became unavailable before delivery. Use only the returned evidence.")
    elif tool_name == "kb_list_sources":
        from bowhead_core.kb.source_navigation import record_navigation_uses, validate_navigation_dependencies

        await validate_navigation_dependencies(ctx, ctx.knowledge_navigation_pending)
        record_navigation_uses(ctx, ctx.knowledge_navigation_pending)
        sources = dict(ctx.knowledge_source_generations)
        result = {**result, "knowledge_navigation_provenance": ctx.knowledge_navigation_pending}
    else:
        # Recheck the complete input set, including aggregate/metadata contributors.
        for file_id in ctx.knowledge_readable_file_ids or ():
            if not await readable_source(ctx, file_id, generation=ctx.knowledge_source_generations[file_id]):
                raise KnowledgeAccessError("knowledge_source_changed")
            sources[file_id] = ctx.knowledge_source_generations[file_id]
    if result.get("error") or result.get("ok") is False:
        return result
    scope = retained_knowledge_scope(ctx)
    if scope is None and sources:
        record_source_uses(ctx, sources)
        scope = retained_knowledge_scope(ctx)
    evidence = {"schema_version": 1, "policy_fingerprint": scope.fingerprint if scope else "", "sources": sources}
    record_source_uses(ctx, sources)
    for registry, row_id, label in ctx.knowledge_pending_citations:
        registry.register(row_id, label=label, kind="kb_row")
    return {**result, "knowledge_provenance": evidence}


def record_source_uses(ctx, sources: dict) -> None:
    """Retain helper evidence for every ancestor that can reuse the helper's result."""
    maps = [ctx.knowledge_source_uses, *ctx.knowledge_source_ancestors]
    state = getattr(ctx, "ordinary_lead_state", None)
    if state is not None:
        maps.append(state.knowledge_sources)
    for retained in maps:
        for file_id, generation in sources.items():
            if file_id in retained and retained[file_id] != generation:
                raise KnowledgeAccessError("knowledge_source_changed")
    for retained in maps:
        retained.update(sources)


def _evidence_sources(evidence: dict) -> dict:
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or not isinstance(evidence.get("sources"), dict):
        raise KnowledgeAccessError("knowledge_source_provenance_unavailable")
    return evidence["sources"]


async def require_evidence(ctx, evidence: dict) -> None:
    await _require_sources(ctx, [_evidence_sources(evidence)])


async def _require_sources(ctx, source_maps: list[dict]) -> None:
    """``readable_source(enforce=True)`` for every cited (file, generation), each distinct file checked once.

    Every ``readable_source`` call reads its file on a new connection and rebuilds the caller's
    access context. History validation called it once per file per Tool message, before the
    first model step: on 116 (1.7.53) a 19-Turn conversation spent 7.4 s there for 12 cited
    files. With a batch-capable store and authorizer, all files are read with one ``get_many``
    and authorized with one ``batch``; each file must pass the same checks as before, and a
    file cited at two generations must match both.
    """
    required: dict[str, set] = {}
    for sources in source_maps:
        for file_id, generation in sources.items():
            required.setdefault(file_id, set()).add(generation)
    if not required:
        return
    store, authorizer = getattr(ctx, "kb_files_store", None), getattr(ctx, "kb_file_authorizer", None)
    if not callable(getattr(store, "get_many", None)) or not callable(getattr(authorizer, "batch", None)):
        for file_id, generations in required.items():
            for generation in generations:
                if not await readable_source(ctx, file_id, generation=generation, enforce=True):
                    raise KnowledgeAccessError("knowledge_source_changed")
        return
    # readable_source's order: the context and the file ceiling first, then the stores.
    scope = ctx.knowledge_scope
    if scope is not None:
        scope.require_context(ctx)
        if not all(scope.permits(file_id) for file_id in required):
            raise KnowledgeAccessError("knowledge_source_changed")
    ids = list(required)
    files = {}
    for offset in range(0, len(ids), 256):
        files.update({file.file_id: file for file in await store.get_many(ids[offset : offset + 256])})
    caller = ScopeContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id)
    for file_id, generations in required.items():
        file = files.get(file_id)
        if (
            not file
            or file.soft_deleted_at
            or file.team_id != ctx.team_id
            or not KbPermission.can_read(file, caller)
            or any(file.active_generation_id != generation for generation in generations)
        ):
            raise KnowledgeAccessError("knowledge_source_changed")
    decisions = await authorizer.batch(list(files.values()), ctx.user_id, ctx.team_id, ctx.workspace_id)
    if decisions is None:
        # The authorizer answers None when it could not decide; that is not a changed source.
        raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
    if not all(decisions.get(file_id) for file_id in required):
        raise KnowledgeAccessError("knowledge_source_changed")


async def filter_inherited_knowledge(ctx, invocations: list) -> list:
    if ctx.knowledge_scope is None:
        return invocations
    output = []
    for invocation in invocations:
        result = getattr(invocation, "result", None)
        if getattr(invocation, "tool_name", None) != "kb_search" or not isinstance(result, dict):
            continue
        retained = []
        for chunk in result.get("chunks") or []:
            if await readable_source(ctx, chunk.get("file_id", ""), generation=chunk.get("generation_id")):
                retained.append(chunk)
        if retained:
            copied = copy.copy(invocation)
            sources = {chunk["file_id"]: chunk.get("generation_id") for chunk in retained}
            record_source_uses(ctx, sources)
            copied.result = {
                **result,
                "chunks": retained,
                "knowledge_provenance": {
                    "schema_version": 1,
                    "policy_fingerprint": ctx.knowledge_scope.fingerprint,
                    "sources": sources,
                },
            }
            output.append(copied)
    return output


async def validate_retained_sources(ctx: Any, sources: dict, *, knowledge_connection: aiosqlite.Connection | None = None) -> None:
    if (
        sources
        and callable(getattr(getattr(ctx, "kb_file_authorizer", None), "batch", None))
        and callable(getattr(ctx.kb_files_store, "get_many", None))
    ):
        scope = ctx.knowledge_scope
        if scope is not None:
            scope.require_context(ctx)
        for offset in range(0, len(sources), 256):
            ids = list(sources)[offset : offset + 256]
            files = await ctx.kb_files_store.get_many(ids, **({"connection": knowledge_connection} if knowledge_connection is not None else {}))
            decisions = await ctx.kb_file_authorizer.batch(files, ctx.user_id, ctx.team_id, ctx.workspace_id)
            # A missing, deleted, moved or re-versioned file is a change whatever the authorizer says.
            if len(files) != len(ids) or any(
                file.soft_deleted_at
                or file.team_id != ctx.team_id
                or file.active_generation_id != sources[file.file_id]
                or (scope is not None and not scope.permits(file.file_id))
                for file in files
            ):
                raise KnowledgeAccessError("knowledge_source_changed")
            if decisions is None:
                # The authorizer answers None when it could not decide; that is not a changed source.
                raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
            if any(not decisions.get(file.file_id) for file in files):
                raise KnowledgeAccessError("knowledge_source_changed")
    else:
        await require_evidence(ctx, {"schema_version": 1, "sources": sources})
    if getattr(ctx, "knowledge_navigation_uses", None):
        from bowhead_core.kb.source_navigation import validate_navigation_dependencies

        await validate_navigation_dependencies(
            ctx, ctx.knowledge_navigation_uses, **({"connection": knowledge_connection} if knowledge_connection is not None else {})
        )


async def inherited_execution_scope(ctx, binding):
    """Recover immutable parent ceilings for entry paths without an in-memory parent."""
    if binding is None or ctx.knowledge_scope is not None:
        return ctx.knowledge_scope
    db_path = getattr(getattr(ctx, "session_store", None), "db_path", None)
    if db_path is None:
        if binding.parent_execution_id is not None:
            raise KnowledgeAccessError("knowledge_source_lineage_unavailable")
        return None
    import json

    import aiosqlite

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_execution_binding import _system_snapshot_by_execution, _team_snapshot_by_execution
    from bowhead_core.agent_knowledge_policy import bind_knowledge_scope, require_supported_knowledge_access

    policies = []
    visited = {binding.execution_id}
    identity = binding.execution_id
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        for _ in range(64):
            async with db.execute("SELECT parent_execution_id FROM turn_agent_execution WHERE id=? AND team_id=?", (identity, ctx.team_id)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise KnowledgeAccessError("knowledge_source_lineage_unavailable")
            if row[0] is None:
                break
            identity = str(row[0])
            if identity in visited:
                raise KnowledgeAccessError("knowledge_source_lineage_unavailable")
            visited.add(identity)
            async with db.execute("SELECT * FROM turn_agent_execution WHERE id=? AND team_id=?", (identity, ctx.team_id)) as cursor:
                parent = await cursor.fetchone()
            if parent is None:
                raise KnowledgeAccessError("knowledge_source_lineage_unavailable")
            snapshot = await (
                _team_snapshot_by_execution(db, parent) if parent["agent_source_kind"] == "team" else _system_snapshot_by_execution(db, parent)
            )
            policies.append((require_supported_knowledge_access(json.loads(snapshot["definition_json"])), str(parent["revision_ref"])))
        else:
            raise KnowledgeAccessError("knowledge_source_lineage_unavailable")
    projected = copy.copy(ctx)
    for policy, revision in reversed(policies):
        projected.knowledge_scope = bind_knowledge_scope(projected, policy, revision=revision)
    return projected.knowledge_scope


async def validate_source_messages(ctx, messages, *, defer_authorization: bool = False) -> None:
    import json

    from bowhead_core.subagent_resume import _CLEARED

    names = {}
    # Cited sources of every Tool message; checked together after the loop, each file once.
    cited: list[dict] = []
    for message in messages:
        for call in message.tool_calls or ():
            names[call.id] = call.name
        if message.role != "tool" or not isinstance(message.content, str):
            continue
        if message.content == _CLEARED:
            continue
        try:
            result = json.loads(message.content)
        except (TypeError, ValueError):
            if ctx.knowledge_scope is not None and names.get(message.tool_call_id) in KNOWLEDGE_TOOLS:
                raise KnowledgeAccessError("knowledge_source_provenance_unavailable") from None
            continue
        if not isinstance(result, dict):
            continue
        navigation = result.get("knowledge_navigation_provenance")
        if navigation is not None:
            from bowhead_core.kb.source_navigation import record_navigation_uses, validate_navigation_dependencies

            if not isinstance(navigation, dict):
                raise KnowledgeAccessError("knowledge_source_provenance_unavailable")
            if not defer_authorization:
                await validate_navigation_dependencies(ctx, navigation)
            record_navigation_uses(ctx, navigation)
        evidence = result.get("knowledge_provenance")
        if evidence is not None:
            cited.append(_evidence_sources(evidence))
        elif ctx.knowledge_scope is not None and names.get(message.tool_call_id) in KNOWLEDGE_TOOLS and not result.get("error"):
            raise KnowledgeAccessError("knowledge_source_provenance_unavailable")
    if not defer_authorization:
        # One check for the whole history; a file cited at another generation adds a second.
        for layer in _evidence_layers(cited):
            await require_evidence(ctx, {"schema_version": 1, "sources": layer})
    for sources in cited:
        record_source_uses(ctx, sources)


def _evidence_layers(source_maps: list[dict]) -> list[dict]:
    """Merge cited sources into as few maps as possible; each map names a file once."""
    layers: list[dict] = []
    for sources in source_maps:
        for file_id, generation in sources.items():
            for layer in layers:
                if file_id not in layer:
                    layer[file_id] = generation
                    break
                if layer[file_id] == generation:
                    break
            else:
                layers.append({file_id: generation})
    return layers
