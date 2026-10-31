"""Current source checks for Agent reads and retained typed evidence."""

from __future__ import annotations

import copy
from typing import Any

from bowhead_core.agent_knowledge_policy import KnowledgeAccessError, KnowledgeScope
from bowhead_core.kb.permission import KbPermission, ScopeContext

KNOWLEDGE_TOOLS = frozenset({"kb_search", "kb_list_tables", "kb_query_records", "kb_get_record"})


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
    if tool_name == "subagent_spawn" and isinstance(result, dict) and ctx.knowledge_source_uses:
        sources = dict(ctx.knowledge_source_uses)
        await validate_retained_sources(ctx, sources)
        return {
            **result,
            "knowledge_provenance": {"schema_version": 1, "policy_fingerprint": retained_knowledge_scope(ctx).fingerprint, "sources": sources},
        }
    if ctx.knowledge_scope is None or tool_name not in KNOWLEDGE_TOOLS or not isinstance(result, dict):
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
    else:
        # Recheck the complete input set, including aggregate/metadata contributors.
        for file_id in ctx.knowledge_readable_file_ids or ():
            if not await readable_source(ctx, file_id, generation=ctx.knowledge_source_generations[file_id]):
                raise KnowledgeAccessError("knowledge_source_changed")
            sources[file_id] = ctx.knowledge_source_generations[file_id]
    if result.get("error") or result.get("ok") is False:
        return result
    evidence = {"schema_version": 1, "policy_fingerprint": ctx.knowledge_scope.fingerprint, "sources": sources}
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


async def require_evidence(ctx, evidence: dict) -> None:
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or not isinstance(evidence.get("sources"), dict):
        raise KnowledgeAccessError("knowledge_source_provenance_unavailable")
    for file_id, generation in evidence["sources"].items():
        if not await readable_source(ctx, file_id, generation=generation, enforce=True):
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


async def validate_retained_sources(ctx, sources: dict) -> None:
    await require_evidence(ctx, {"schema_version": 1, "sources": sources})


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


async def validate_source_messages(ctx, messages) -> None:
    import json

    from bowhead_core.subagent_resume import _CLEARED

    names = {}
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
        evidence = result.get("knowledge_provenance")
        if evidence is not None:
            await require_evidence(ctx, evidence)
            record_source_uses(ctx, evidence["sources"])
        elif ctx.knowledge_scope is not None and names.get(message.tool_call_id) in KNOWLEDGE_TOOLS and not result.get("error"):
            raise KnowledgeAccessError("knowledge_source_provenance_unavailable")
