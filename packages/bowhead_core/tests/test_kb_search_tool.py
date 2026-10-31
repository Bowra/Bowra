"""KB tool registry test — kb.search handler integration.

Verifies build_default_registry registers kb.search tool with proper schema +
handler integrates retriever + emit ScopeContext correctly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.kb.schemas import KbSearchResult, KbSearchResultChunk
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    build_default_registry,
)


def test_kb_search_registered_in_default_registry() -> None:
    """Sprint Day 16: kb.search must be in build_default_registry."""
    reg = build_default_registry()
    assert "kb_search" in reg._specs, "kb.search not registered"
    spec = reg._specs["kb_search"]
    assert spec.schema.name == "kb_search"
    # 0.40.3: description rewritten — accept 知识库 / knowledge / KB / chunks (any KB marker).
    desc = spec.schema.description
    assert any(m in desc for m in ("知识库", "KB", "chunks")) or "knowledge" in desc.lower(), (
        f"kb_search description missing KB marker: {desc[:120]!r}"
    )


def test_kb_search_schema_input_query_required() -> None:
    reg = build_default_registry()
    spec = reg._specs["kb_search"]
    schema = spec.schema.input_schema
    assert "query" in schema["properties"]
    assert "query" in schema["required"]
    assert schema["properties"]["top_k"]["minimum"] == 1
    assert schema["properties"]["top_k"]["maximum"] == 50


def _make_ctx(kb_retriever=None, **overrides) -> ToolExecutionContext:
    """Build minimal ctx for kb.search handler test."""
    defaults = {
        "workspace_id": "ws_42",
        "member_uid": "member_test",
        "user_id": "user_a",
        "turn_id": "turn_test",
        "datarc": MagicMock(),
        "kb_retriever": kb_retriever,
        "team_id": "team_5",
    }
    defaults.update(overrides)
    return ToolExecutionContext(**defaults)


@pytest.mark.asyncio
async def test_kb_search_handler_no_retriever_returns_soft_empty() -> None:
    """[Day 16 graceful] ctx.kb_retriever None → soft empty result + note."""
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler

    ctx = _make_ctx(kb_retriever=None)
    result = await handler({"query": "test"}, ctx)

    assert result["chunks"] == []
    assert result["total_candidates"] == 0
    assert "note" in result
    # 0.40.2: refreshed note — KB is yaml-driven now; the legacy
    # "ALIYUN_DASHSCOPE_API_KEY 未设" message was misleading (Bug 3 surface)
    # because the actual cause is usually missing _peer_ctx wire.
    assert "not wired" in result["note"] or "_peer_ctx" in result["note"]


@pytest.mark.asyncio
async def test_kb_search_handler_invokes_retriever_with_scope_ctx() -> None:
    """Handler should construct ScopeContext from ctx + invoke retriever."""
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler

    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[
                KbSearchResultChunk(
                    chunk_id="chunk_abc",
                    content="GMV 定义",
                    file_id="kbf_x",
                    file_name="defs.pdf",
                    page=1,
                    section_title="GMV",
                    role="glossary",
                    score=0.9,
                    scope="workspace",
                )
            ],
            total_candidates=5,
            rerank_used=True,
            latency_ms=120,
        )
    )
    ctx = _make_ctx(kb_retriever=mock_retriever)

    result = await handler({"query": "什么是 GMV?", "top_k": 3}, ctx)

    # Verify retriever invoked correctly
    assert mock_retriever.invoke.called
    call_kwargs = mock_retriever.invoke.call_args.kwargs
    assert call_kwargs["query"] == "什么是 GMV?"
    assert call_kwargs["top_k"] == 3
    scope_ctx = call_kwargs["scope_ctx"]
    assert scope_ctx.user_id == "user_a"
    assert scope_ctx.workspace_id == "ws_42"
    assert scope_ctx.team_id == "team_5"

    # Verify result serialized correctly
    assert result["total_candidates"] == 5
    assert result["rerank_used"] is True
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["chunk_id"] == "chunk_abc"
    assert result["chunks"][0]["content"] == "GMV 定义"
    assert result["chunks"][0]["score"] == 0.9


@pytest.mark.asyncio
async def test_kb_search_handler_filters_denied_file_chunks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_5",
        resource_kind="kb_file",
        resource_id="kbf_secret",
        principal_type="user",
        principal_id="user_a",
        permission="read",
        effect="deny",
        created_by="admin",
    )

    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[
                KbSearchResultChunk(
                    chunk_id="chunk_secret",
                    content="secret",
                    file_id="kbf_secret",
                    file_name="secret.pdf",
                    role="intro",
                    score=0.9,
                    scope="workspace",
                ),
                KbSearchResultChunk(
                    chunk_id="chunk_public",
                    content="public",
                    file_id="kbf_public",
                    file_name="public.pdf",
                    role="intro",
                    score=0.8,
                    scope="workspace",
                ),
            ],
            total_candidates=2,
            rerank_used=True,
            latency_ms=12,
        )
    )
    access_ctx = AccessContext(
        user_id="user_a",
        team_id="team_5",
        workspace_id="ws_42",
        workspace_role="member",
        group_ids=(),
    )
    ctx = _make_ctx(
        kb_retriever=mock_retriever,
        access_control_store=access,
        access_context=access_ctx.__dict__,
    )

    result = await handler({"query": "secret"}, ctx)

    assert [chunk["chunk_id"] for chunk in result["chunks"]] == ["chunk_public"]


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [5, 15, 50])
async def test_kb_search_handler_uses_configured_default(top_k: int) -> None:
    """An omitted request limit uses the retriever's configured default."""
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler

    mock_retriever = MagicMock(default_top_k=top_k)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=10,
        )
    )
    ctx = _make_ctx(kb_retriever=mock_retriever)

    await handler({"query": "test"}, ctx)
    assert mock_retriever.invoke.call_args.kwargs["top_k"] == top_k


@pytest.mark.asyncio
async def test_kb_search_handler_team_id_fail_closed_no_workspace_fallback() -> None:
    """批1/c5: team_id 缺省 (空 string) → 保持 "" fail-closed —— space id 永不冒充 team id
    (旧 V1 mock 1-1 映射兜底会把 c4 清掉的串号重新抹回来)."""
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler

    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=0,
        )
    )
    ctx = _make_ctx(kb_retriever=mock_retriever, team_id="")  # explicit empty
    await handler({"query": "test"}, ctx)
    scope_ctx = mock_retriever.invoke.call_args.kwargs["scope_ctx"]
    assert scope_ctx.team_id == "", "team_id must stay empty — no workspace_id fallback"


# ─── 0.40.3 Bug 6: actionable hint pattern (Anthropic agent tools guide) ───


@pytest.mark.asyncio
async def test_kb_search_empty_result_has_hint() -> None:
    """0.40.3 Bug 6 — empty kb_search result includes actionable hint steering
    agent toward kb_list_tables or query reformulation. Per Anthropic agent
    tools guidance: structured output hints > prompt rules.
    """
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=20,
            rerank_used=True,
            latency_ms=500,
        )
    )
    ctx = _make_ctx(kb_retriever=mock_retriever)
    result = await handler({"query": "specific entity"}, ctx)
    assert result["chunks"] == []
    assert "hint" in result
    # Hint should suggest concrete next actions.
    hint = result["hint"]
    assert "kb_list_tables" in hint or "different" in hint.lower() or "reformulat" in hint.lower()


@pytest.mark.asyncio
async def test_kb_search_strong_result_has_no_hint() -> None:
    """Strong score (>=0.3) → no hint (agent has good signal already)."""
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    from bowhead_core.kb.schemas import KbSearchResultChunk

    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[
                KbSearchResultChunk(
                    chunk_id="c1",
                    content="x",
                    file_id="f1",
                    file_name="d.pdf",
                    page=1,
                    section_title=None,
                    role="intro",
                    score=0.85,
                    scope="personal",
                )
            ],
            total_candidates=1,
            rerank_used=True,
            latency_ms=200,
        )
    )
    ctx = _make_ctx(kb_retriever=mock_retriever)
    result = await handler({"query": "x"}, ctx)
    assert len(result["chunks"]) == 1
    assert "hint" not in result, f"strong result should have no hint, got: {result.get('hint')}"


# ─── 0.40.4 Bug 8 step (a): kb_files_store wire + cross-ws workspaces ───


@pytest.mark.asyncio
async def test_kb_search_passes_extra_personal_workspaces() -> None:
    """0.40.4 Bug 8 step (a): _kb_search resolves user's distinct
    personal-scope workspaces via kb_files_store, passes to ScopeContext.
    """
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=10,
        )
    )
    mock_files_store = MagicMock()
    mock_files_store.list_distinct_workspaces_for_user = AsyncMock(
        return_value=["ws_a", "ws_b", ""],
    )
    ctx = _make_ctx(kb_retriever=mock_retriever, kb_files_store=mock_files_store)
    await handler({"query": "test"}, ctx)
    scope_ctx = mock_retriever.invoke.call_args.kwargs["scope_ctx"]
    assert scope_ctx.extra_personal_workspaces == ["ws_a", "ws_b", ""], (
        f"Expected list_distinct_workspaces_for_user result, got: {scope_ctx.extra_personal_workspaces}"
    )


@pytest.mark.asyncio
async def test_kb_search_falls_back_when_files_store_raises() -> None:
    """0.40.4 Bug 8: kb_files_store DB query failure (lock contention /
    schema drift) MUST NOT crash kb_search — degrades gracefully to
    legacy single-ws behavior. Warning logged for observability.
    """
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=10,
        )
    )
    failing_store = MagicMock()
    failing_store.list_distinct_workspaces_for_user = AsyncMock(
        side_effect=RuntimeError("database is locked"),
    )
    ctx = _make_ctx(kb_retriever=mock_retriever, kb_files_store=failing_store)
    # MUST NOT raise
    result = await handler({"query": "test"}, ctx)
    assert "chunks" in result
    # Retriever was still called with empty extras (fallback to single-ws)
    scope_ctx = mock_retriever.invoke.call_args.kwargs["scope_ctx"]
    assert scope_ctx.extra_personal_workspaces == [], f"Expected fallback to empty extras, got: {scope_ctx.extra_personal_workspaces}"


@pytest.mark.asyncio
async def test_kb_search_no_files_store_legacy_path() -> None:
    """ctx.kb_files_store=None (pre-0.40.4 wiring) → no crash, extras=[]."""
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=10,
        )
    )
    ctx = _make_ctx(kb_retriever=mock_retriever, kb_files_store=None)
    await handler({"query": "test"}, ctx)
    scope_ctx = mock_retriever.invoke.call_args.kwargs["scope_ctx"]
    assert scope_ctx.extra_personal_workspaces == []


@pytest.mark.asyncio
async def test_kb_search_many_workspaces_no_crash() -> None:
    """0.40.4 Bug 8: stress — user has 30 personal-scope workspaces (e.g.,
    moved between many datarc workspaces). Should pass all to scope_ctx
    without truncation (caller's responsibility to bound).
    """
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    mock_retriever = MagicMock(default_top_k=15)
    mock_retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=10,
        )
    )
    many_ws = [f"ws_{i:03d}" for i in range(30)]
    mock_files_store = MagicMock()
    mock_files_store.list_distinct_workspaces_for_user = AsyncMock(
        return_value=many_ws,
    )
    ctx = _make_ctx(kb_retriever=mock_retriever, kb_files_store=mock_files_store)
    await handler({"query": "test"}, ctx)
    scope_ctx = mock_retriever.invoke.call_args.kwargs["scope_ctx"]
    assert len(scope_ctx.extra_personal_workspaces) == 30


@pytest.mark.asyncio
async def test_kb_list_tables_includes_hint_field() -> None:
    """0.40.3 Bug 6 — kb_list_tables output ALWAYS includes hint field steering
    agent toward kb_search for content queries.
    """
    reg = build_default_registry()
    handler = reg._specs["kb_list_tables"].handler
    # Empty store (no doc_type_store wired)
    ctx = _make_ctx(kb_doc_type_store=None)
    result = await handler({}, ctx)
    assert result["items"] == []
    assert "hint" in result
    assert "kb_search" in result["hint"]


def _result_chunk(chunk_id: str, file_id: str) -> KbSearchResultChunk:
    return KbSearchResultChunk(
        chunk_id=chunk_id,
        content="季度预算",
        file_id=file_id,
        file_name=f"{file_id}.pdf",
        role="data",
        score=0.9,
        scope="space",
    )


@pytest.mark.asyncio
async def test_kb_search_drops_chunks_of_soft_deleted_files() -> None:
    """兜底：文件行已软删（删除与解析写入交错漏网）的分块不交给模型。"""
    from types import SimpleNamespace

    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    retriever = MagicMock(default_top_k=15)
    retriever.invoke = AsyncMock(
        return_value=KbSearchResult(
            chunks=[_result_chunk("c_del", "f_del"), _result_chunk("c_live", "f_live")],
            total_candidates=2,
            rerank_used=True,
            latency_ms=5,
        )
    )
    rows = {
        "f_del": SimpleNamespace(soft_deleted_at="2026-09-26T00:00:00+00:00"),
        "f_live": SimpleNamespace(soft_deleted_at=None),
    }
    files_store = MagicMock()
    files_store.list_distinct_workspaces_for_user = AsyncMock(return_value=[])
    files_store.get = AsyncMock(side_effect=lambda fid: rows.get(fid))
    ctx = _make_ctx(kb_retriever=retriever, kb_files_store=files_store)

    result = await handler({"query": "季度预算"}, ctx)

    assert [c["chunk_id"] for c in result["chunks"]] == ["c_live"]


@pytest.mark.asyncio
async def test_kb_search_partial_result_carries_chinese_note() -> None:
    """有检索范围超时被跳过：工具结果如实说明结果不完整，模型不能据此断言知识库里没有。"""
    from bowhead_core.tool_registry import KB_SEARCH_PARTIAL_NOTE

    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    retriever = MagicMock(default_top_k=15)
    retriever.invoke = AsyncMock(return_value=KbSearchResult(chunks=[], total_candidates=0, rerank_used=False, latency_ms=3000, partial=True))

    result = await handler({"query": "季度预算"}, _make_ctx(kb_retriever=retriever))

    assert result["partial"] is True
    assert result["note"] == KB_SEARCH_PARTIAL_NOTE
    assert "不完整" in result["note"]


@pytest.mark.asyncio
async def test_kb_search_complete_result_has_no_partial_note() -> None:
    reg = build_default_registry()
    handler = reg._specs["kb_search"].handler
    retriever = MagicMock(default_top_k=15)
    retriever.invoke = AsyncMock(return_value=KbSearchResult(chunks=[], total_candidates=0, rerank_used=False, latency_ms=5))

    result = await handler({"query": "季度预算"}, _make_ctx(kb_retriever=retriever))

    assert "partial" not in result
    assert "note" not in result
