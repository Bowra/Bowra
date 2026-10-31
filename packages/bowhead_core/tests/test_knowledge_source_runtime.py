"""File ceilings apply before computation and survive delegation and retained evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError, bind_knowledge_scope, require_supported_knowledge_access
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore, RowInput
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.retriever import KbRetriever
from bowhead_core.knowledge_source_runtime import (
    filter_inherited_knowledge,
    finish_source_read,
    prepare_source_read,
    readable_source,
    validate_retained_sources,
)
from bowhead_core.subagent_resume import decode_material, encode_material
from bowhead_core.tool_registry import build_default_registry
from test_kb_search_tool import _make_ctx


def selected(*ids):
    return require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "selected_files", "file_ids": list(ids)}})


def source_context(*ids):
    files = {
        identity: SimpleNamespace(
            file_id=identity, team_id="team_5", scope="space", scope_id="ws_42", soft_deleted_at=None, active_generation_id=None
        )
        for identity in ["kbf_allowed", "kbf_other"]
    }
    ctx = _make_ctx(
        kb_files_store=SimpleNamespace(get=AsyncMock(side_effect=lambda identity: files.get(identity))),
        kb_file_authorizer=AsyncMock(return_value=True),
    )
    ctx.knowledge_scope = bind_knowledge_scope(ctx, selected(*ids), revision="arv_parent")
    return ctx, files


def test_child_intersection_never_mutates_parent_or_sibling():
    parent, _ = source_context("kbf_allowed")
    assert bind_knowledge_scope(parent, selected("kbf_other"), revision="arv_child").file_ids == ()
    context_policy = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "context_readable"}})
    assert bind_knowledge_scope(parent, context_policy, revision="arv_child").file_ids == ("kbf_allowed",)
    assert bind_knowledge_scope(parent, None, revision="arv_legacy") == parent.knowledge_scope
    assert parent.knowledge_scope.file_ids == ("kbf_allowed",)


async def test_current_acl_context_and_personal_owner_are_independent_of_author_policy():
    ctx, files = source_context("kbf_allowed")
    assert await readable_source(ctx, "kbf_allowed")
    assert not await readable_source(ctx, "kbf_other")
    files["kbf_allowed"].scope_id = "ws_other"
    assert not await readable_source(ctx, "kbf_allowed")
    files["kbf_allowed"].scope = "personal"
    files["kbf_allowed"].scope_id = ctx.user_id
    assert await readable_source(ctx, "kbf_allowed")
    ctx.kb_file_authorizer.return_value = False
    assert not await readable_source(ctx, "kbf_allowed")
    ctx.user_id = "user_different"
    with pytest.raises(KnowledgeAccessError, match="context_changed"):
        await readable_source(ctx, "kbf_allowed")


async def test_source_generation_and_revocation_checked_before_result_and_citation_delivery():
    ctx, files = source_context("kbf_allowed")
    prepared = await prepare_source_read(ctx, "kb_query_records")
    citation = MagicMock()
    prepared.knowledge_pending_citations.append((citation, "row_1", "Allowed row"))
    files["kbf_allowed"].active_generation_id = "new_generation"
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await finish_source_read(prepared, "kb_query_records", {"aggregate_result": [{"value": 5}]})
    citation.register.assert_not_called()
    assert ctx.knowledge_source_uses == {}
    files["kbf_allowed"].active_generation_id = None
    result = await finish_source_read(prepared, "kb_query_records", {"aggregate_result": [{"value": 5}]})
    assert result["knowledge_provenance"]["sources"] == {"kbf_allowed": None}
    citation.register.assert_called_once()
    ctx.kb_file_authorizer.return_value = False
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_retained_sources(ctx, ctx.knowledge_source_uses)


async def test_parent_snippets_are_filtered_and_retained_evidence_rechecked():
    ctx, files = source_context("kbf_allowed")
    invocation = SimpleNamespace(tool_name="kb_search", result={"chunks": [{"file_id": identity, "content": identity} for identity in files]})
    inherited = await filter_inherited_knowledge(ctx, [invocation])
    assert len(invocation.result["chunks"]) == 2
    assert [c["file_id"] for c in inherited[0].result["chunks"]] == ["kbf_allowed"]
    assert inherited[0].result["knowledge_provenance"]["sources"] == ctx.knowledge_source_uses == {"kbf_allowed": None}
    files["kbf_allowed"].soft_deleted_at = "deleted"
    with pytest.raises(KnowledgeAccessError):
        await validate_retained_sources(ctx, ctx.knowledge_source_uses)


async def test_direct_handler_cannot_bypass_source_preparation():
    ctx, _ = source_context()
    registry = build_default_registry()
    for name in ["kb_search", "kb_query_records", "kb_get_record", "kb_list_tables"]:
        with pytest.raises(KnowledgeAccessError, match="authorization_unavailable"):
            await registry._specs[name].handler({}, ctx)


async def test_sql_filters_precede_count_sort_pagination_aggregation_and_coverage(tmp_path):
    rows = KbExtractedRowsStore(tmp_path / "rows.db")
    records = KbExtractedRecordStore(tmp_path / "records.db")
    await rows.initialize()
    await records.initialize()
    for file_id, amount in [("kbf_allowed", 10), ("kbf_other", 1000)]:
        fields = {"amount": {"value": amount, "confidence": 1}}
        await rows.create_many(
            rows=[RowInput(row_seq=1, page=1, fields=fields, dedup_key_hash=amount)], file_id=file_id, doc_type_id="dt_test", workspace_id="ws_test"
        )
        await records.create(workspace_id="ws_test", doc_type_id="dt_test", file_id=file_id, fields=fields)
    scoped = {"workspace_id": "ws_test", "doc_type_id": "dt_test", "allowed_file_ids": ("kbf_allowed",)}
    assert await rows.count_for_doc_type(**scoped) == 1
    assert await records.count_for_doc_type(**scoped) == 1
    result = await rows.query_rows(**scoped, sort_by="amount", sort_dir="DESC", limit=1)
    assert [row.file_id for row in result] == ["kbf_allowed"]
    assert await rows.query_rows(**scoped, offset=1) == []
    one = await records.query_by_field(**scoped, sort_by="amount", sort_dir="DESC", limit=1)
    assert [row.file_id for row in one] == ["kbf_allowed"]
    assert (await rows.aggregate(**scoped, field="amount", op="SUM"))[0].value == 10
    coverage = await rows.aggregate_coverage(**scoped, field="amount")
    assert coverage["total"] == 1
    assert await rows.count_for_doc_type(**{**scoped, "allowed_file_ids": ()}) == 0
    assert await rows.query_rows(**{**scoped, "allowed_file_ids": ()}) == []


async def test_candidates_outside_ceiling_never_reach_reranker_even_if_vector_backend_ignores_filter(monkeypatch):
    monkeypatch.setenv("FEATURE_KB_USE_GUARD", "0")
    ctx, files = source_context("kbf_allowed")
    candidates = [
        {
            "chunk_id": identity,
            "content": identity,
            "distance": 0.1,
            "metadata": {"file_id": identity, "scope": "space", "scope_id": "ws_42", "file_name": identity},
        }
        for identity in files
    ]
    index = SimpleNamespace(search=AsyncMock(return_value=candidates))
    embedder = SimpleNamespace(embed_batch=AsyncMock(return_value=[[0.1]]))
    reranker = SimpleNamespace(rerank=AsyncMock(return_value=[(0, 0.9)]))
    retriever = KbRetriever(embedder=embedder, store=index, reranker=reranker, files_store=ctx.kb_files_store)
    result = await retriever.invoke(
        "source revenue",
        ScopeContext(user_id=ctx.user_id, team_id=ctx.team_id, workspace_id=ctx.workspace_id),
        allowed_file_ids=("kbf_allowed",),
        enforce_source_access=True,
        authorize_file=AsyncMock(return_value=True),
    )
    assert [chunk.file_id for chunk in result.chunks] == ["kbf_allowed"]
    assert "kbf_other" not in str(reranker.rerank.call_args)
    assert index.search.call_args.kwargs["allowed_file_ids"] == ("kbf_allowed",)
    embedder.embed_batch.reset_mock()
    assert (
        await retriever.invoke(
            "source revenue", ScopeContext(user_id=ctx.user_id, team_id=ctx.team_id, workspace_id=ctx.workspace_id), allowed_file_ids=()
        )
    ).chunks == []
    embedder.embed_batch.assert_not_called()


def test_helper_material_keeps_scope_after_gate_closes_and_rejects_downgrade(monkeypatch):
    import json
    import zlib

    from bowhead_router.types import Message

    ctx, _ = source_context("kbf_allowed")
    packed, _ = encode_material(
        [Message(role="user", content="Continue reading.")],
        [],
        max_sealed=100000,
        knowledge_scope=ctx.knowledge_scope,
        knowledge_sources={"kbf_allowed": None},
    )
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "0")
    material = decode_material(packed)
    assert material.knowledge_scope == ctx.knowledge_scope
    assert material.knowledge_sources == {"kbf_allowed": None}
    value = json.loads(zlib.decompress(packed))
    value["schema_version"] = 2
    with pytest.raises(ValueError):
        decode_material(zlib.compress(json.dumps(value).encode()))


async def test_helper_evidence_propagates_to_parent_without_polluting_sibling_ceiling():
    from copy import copy

    parent, _ = source_context("kbf_allowed", "kbf_other")
    child = copy(parent)
    child.knowledge_source_ancestors = (parent.knowledge_source_uses,)
    child.knowledge_source_uses = {}
    child.knowledge_scope = bind_knowledge_scope(child, selected("kbf_allowed"), revision="arv_child")
    prepared = await prepare_source_read(child, "kb_search")
    await finish_source_read(prepared, "kb_search", {"chunks": [{"file_id": "kbf_allowed", "content": "Allowed"}]})
    assert parent.knowledge_source_uses == {"kbf_allowed": None}
    assert parent.knowledge_scope.file_ids == ("kbf_allowed", "kbf_other")
    parent.kb_file_authorizer.return_value = False
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_retained_sources(parent, parent.knowledge_source_uses)


async def test_legacy_parent_retains_restricted_helper_provenance_in_model_messages():
    import json

    from bowhead_core.knowledge_source_runtime import retained_knowledge_scope, validate_source_messages
    from bowhead_router.types import Message

    ctx, _ = source_context()
    ctx.knowledge_scope = None
    ctx.knowledge_source_uses = {"kbf_allowed": None}
    result = await finish_source_read(ctx, "subagent_spawn", {"summary": "Allowed evidence"})
    assert result["knowledge_provenance"]["sources"] == ctx.knowledge_source_uses
    assert retained_knowledge_scope(ctx).file_ids is None
    ctx.kb_file_authorizer.return_value = False
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_source_messages(ctx, [Message(role="tool", tool_call_id="helper", content=json.dumps(result))])


async def test_missing_parent_reader_cannot_turn_a_child_into_an_unrestricted_root():
    from bowhead_core.knowledge_source_runtime import inherited_execution_scope

    ctx, _ = source_context()
    ctx.knowledge_scope = None
    ctx.session_store = None
    with pytest.raises(KnowledgeAccessError, match="lineage_unavailable"):
        await inherited_execution_scope(ctx, SimpleNamespace(parent_execution_id="aex_parent"))
