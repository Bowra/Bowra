"""embed-via-openapi (R1.2 catch-up) — embed_search 的 datarc 工作区走 datarc-project chokepoint.

schema collection 命名是 `v1_{tenant}_p{project}`；维度值召回打 datarc
`/workspaces/{project}/keywords/recall/`。两条路的 project 都必须经
`ctx.resolve_datarc_project()` 解析——1c 迁移后 ctx.workspace_id 是 Bowhead `ws_*`，
拿它当 datarc 工作区会静默全 miss。
stub ctx（无该方法）按 term_hook _FakeCtx 教训（8879222）必须仍然工作。
"""

from __future__ import annotations

import types
from typing import Any

from bowhead_core.embed_search import query as embed_query

from bowhead_bi_bridge import KeywordRecall


class _CapturingVC:
    def __init__(self) -> None:
        self.collections: list[str] = []

    async def query(self, *, collection: str, **_kw: Any) -> dict[str, Any]:
        self.collections.append(collection)
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}


class _CapturingDatarc:
    def __init__(self) -> None:
        self.workspaces: list[str] = []

    async def recall_keywords(self, ws: str, words: list[str], *, n_results: int | None = None) -> list[KeywordRecall]:
        self.workspaces.append(ws)
        return [KeywordRecall(source=w) for w in words]


async def test_schema_collection_uses_resolved_datarc_project() -> None:
    vc = _CapturingVC()
    # Post-migration shape: workspace_id is ws_*; resolve the active project through the chokepoint.
    ctx = types.SimpleNamespace(
        vector_client=vc,
        tenant="demo",
        workspace_id="ws_abc123",
        resolve_datarc_project=lambda: "42",
        ar2_thresholds=None,
    )
    await embed_query(ctx, query_texts=["店"], collection="schema", n_results=3)
    assert vc.collections == ["v1_demo_p42"]


async def test_dim_value_recall_uses_resolved_datarc_project() -> None:
    datarc = _CapturingDatarc()
    ctx = types.SimpleNamespace(
        datarc=datarc,
        workspace_id="ws_abc123",
        resolve_datarc_project=lambda: "42",
        ar2_thresholds=None,
    )
    await embed_query(ctx, query_texts=["店"], collection="keywords", n_results=3)
    assert datarc.workspaces == ["42"]


async def test_stub_ctx_without_resolver_misses_cleanly() -> None:
    vc = _CapturingVC()
    datarc = _CapturingDatarc()
    # Batch 1/c3: stub ctx without resolve_datarc_project no longer falls back to workspace_id; a Bowhead space ID
    # creates a nonexistent collection / targets the wrong workspace (silent all-miss). Now return a clean early miss without calling either backend.
    ctx = types.SimpleNamespace(
        vector_client=vc,
        datarc=datarc,
        tenant="demo",
        workspace_id="7",
        ar2_thresholds=None,
    )
    for collection in ("schema", "keywords"):
        res = await embed_query(ctx, query_texts=["店"], collection=collection, n_results=3)  # type: ignore[arg-type]
        assert res and res[0].miss is True  # Explicit miss.
        assert res[0].unavailable_reason == "no_datarc_workspace"
    assert vc.collections == []  # Vector store was never queried.
    assert datarc.workspaces == []  # Datarc recall was never called either.
