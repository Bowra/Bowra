"""飞书 bug list(Wea1wLZ…) 追#12/#14 — 裸 artifact id 泄露修复回归锁。

prod 实证:
- 追#12: narrative 正文 "数据汇总来自artifact art_7205168aa21c，月度明细6条
  数据来自art_0622b…"(s_5499c1b85eaa seq16) — writer 把内部 id 写进 prose。
- 追#14: sources 段 13/13 条全是兜底标签 `artifact: art_xxx`(同 session seq32)
  — 来源清单成 id 墙。

修复: resolve_bare_artifact_ids(裸 id → [N], 与 marker 改写共享编号空间) +
enrich_source_labels(兜底标签用 artifact 文件名补人话)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.sources_resolver import (
    TurnSourcesRegistry,
    enrich_source_labels,
    resolve_bare_artifact_ids,
)


def _inv(call_id: str, aid: str, query: str) -> Any:
    return SimpleNamespace(
        call_id=call_id,
        tool_name="bi_query",
        error=None,
        arguments={},
        result={"results": [{"artifact_id": aid, "query_text": query, "sample_rows": [{"v": 1}], "total_rows": 6}]},
    )


# ── Follow-up #12 resolve_bare_artifact_ids ────────────────────────────────────────


def test_bare_id_rewritten_to_numbered_cite_via_lazy_add() -> None:
    text = "数据汇总来自artifact art_7205168aa21c，其余见附录。"
    new, sources, idx = resolve_bare_artifact_ids(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[_inv("c1", "art_7205168aa21c", "2026年月度明细")],
    )
    assert new == "数据汇总来自[1]，其余见附录。"
    assert idx["art_7205168aa21c"] == 1
    assert sources[0]["artifact_id"] == "art_7205168aa21c"


def test_bare_id_uses_existing_index() -> None:
    sources = [{"label": "x", "artifact_id": "art_0622b1234567"}]
    idx = {"art_0622b1234567": 1}
    new, _, _ = resolve_bare_artifact_ids(
        "月度明细6条数据来自art_0622b1234567。",
        sources=sources,
        id_to_index=idx,
    )
    assert new == "月度明细6条数据来自[1]。"


def test_unresolvable_bare_id_left_verbatim() -> None:
    text = "来自 art_deadbeef9999 的数据。"
    new, sources, _ = resolve_bare_artifact_ids(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert new == text  # If replacement fails, do not delete the bare id and damage the sentence.
    assert sources == []


def test_bare_id_adjacent_numbered_cite_drops_token_only() -> None:
    # prod s_b3eeb671e914: "导出文件为 art_39ceadd61c20[1]"; writer already numbered it
    idx = {"art_39ceadd61c20": 1}
    new, _, _ = resolve_bare_artifact_ids(
        "导出文件为 art_39ceadd61c20[1]，可直接使用。",
        sources=[{"label": "png", "artifact_id": "art_39ceadd61c20"}],
        id_to_index=idx,
    )
    assert new == "导出文件为 [1]，可直接使用。"


def test_bare_id_in_url_and_link_target_untouched() -> None:
    """review F2: URL 路径/markdown 链接目标里的 id 是链接不是引用, 改写会 404。"""
    idx = {"art_39ceadd61c20": 1}
    src = [{"label": "png", "artifact_id": "art_39ceadd61c20"}]
    t1 = "下载: /api/v1/files/art_39ceadd61c20?disposition=inline 即可"
    new1, _, _ = resolve_bare_artifact_ids(t1, sources=src, id_to_index=idx)
    assert new1 == t1
    t2 = "[下载导出文件](/api/v1/files/art_39ceadd61c20)"
    new2, _, _ = resolve_bare_artifact_ids(t2, sources=src, id_to_index=idx)
    assert new2 == t2


def test_bare_id_inside_marker_and_code_untouched() -> None:
    idx = {"art_aaaabbbbcccc": 1}
    src = [{"label": "x", "artifact_id": "art_aaaabbbbcccc"}]
    marker = "增长 [num:4.18%:src:art_aaaabbbbcccc] 明显"
    new, _, _ = resolve_bare_artifact_ids(marker, sources=src, id_to_index=idx)
    assert new == marker
    fenced = "```\nload('art_aaaabbbbcccc')\n```\n正文 art_aaaabbbbcccc 引用"
    new2, _, _ = resolve_bare_artifact_ids(fenced, sources=src, id_to_index=idx)
    assert "load('art_aaaabbbbcccc')" in new2  # Untouched inside fences
    assert "正文 [1] 引用" in new2  # Replaced outside fences
    inline_code = "用 `art_aaaabbbbcccc` 这个 id 调工具"
    new3, _, _ = resolve_bare_artifact_ids(inline_code, sources=src, id_to_index=idx)
    assert new3 == inline_code


def test_registry_resolve_bare_ids_shares_numbering_with_resolve_text() -> None:
    reg = TurnSourcesRegistry()
    inv = _inv("c1", "art_7205168aa21c", "2026年月度明细")
    reg.ingest_tool_invocations([inv])
    # Marker path allocated [1] first.
    t1 = reg.resolve_text("同比 [num:4.18:%:src:art_7205168aa21c]", [inv])
    assert "[1]" in t1
    # Bare-id path must reuse the same number.
    t2 = reg.resolve_bare_ids("数据来自artifact art_7205168aa21c。", [inv])
    assert t2 == "数据来自[1]。"
    assert reg.size() == 1


# ── Follow-up #14 enrich_source_labels ─────────────────────────────────────────────


class _FakeArtifactStore:
    def __init__(self, names: dict[str, str]) -> None:
        self._names = names
        self.seen_kwargs: list[dict[str, Any]] = []

    async def get(self, aid: str, **kw: Any) -> Any:
        self.seen_kwargs.append(dict(kw))
        if aid not in self._names:
            raise KeyError(aid)
        return SimpleNamespace(id=aid, filename=self._names[aid])


@pytest.mark.asyncio
async def test_fallback_label_replaced_by_filename() -> None:
    entries = [
        {"label": "artifact: art_5d4378add618", "artifact_id": "art_5d4378add618"},
        {"label": "数据源 art_dd014ae86", "artifact_id": "art_dd014ae86c06"},
        {"label": "bi_query: 2025年 每月 营业额", "artifact_id": "art_keepme000000"},
    ]
    store = _FakeArtifactStore(
        {
            "art_5d4378add618": "2026年4月刷卡金额一级分行_643d3d8.csv",
            "art_dd014ae86c06": "2025年一级分行客户数_3f95968.csv",
            "art_keepme000000": "should_not_be_used.csv",
        }
    )
    out = await enrich_source_labels(entries, store)
    assert out[0]["label"] == "2026年4月刷卡金额一级分行_643d3d8.csv"
    assert out[1]["label"] == "2025年一级分行客户数_3f95968.csv"
    # Keep existing human-readable labels.
    assert out[2]["label"] == "bi_query: 2025年 每月 营业额"


@pytest.mark.asyncio
async def test_enrich_covers_tool_prefixed_fallback_and_passes_owner_check() -> None:
    """review F4: `bi_query: art_x` 兜底形状也要补; review F1: 必带 owner 校验。"""
    entries = [
        {"label": "bi_query: art_dd014ae86c06", "artifact_id": "art_dd014ae86c06"},
    ]
    store = _FakeArtifactStore({"art_dd014ae86c06": "2025年一级分行客户数.csv"})
    out = await enrich_source_labels(entries, store, user_id="u_owner")
    assert out[0]["label"] == "2025年一级分行客户数.csv"
    # Owner check really propagates (#54 contract).
    assert store.seen_kwargs == [{"check_created_by": "u_owner"}]


@pytest.mark.asyncio
async def test_enrich_tolerates_store_miss_and_none_store() -> None:
    entries = [{"label": "artifact: art_missing00000", "artifact_id": "art_missing00000"}]
    out = await enrich_source_labels(entries, _FakeArtifactStore({}))
    assert out[0]["label"] == "artifact: art_missing00000"  # miss → unchanged
    out2 = await enrich_source_labels(entries, None)
    assert out2 is entries


def test_registry_dedups_label_only_entries() -> None:
    # review[2]-fix: repeated ingestion of label-only sources (no artifact_id) yields one source entry.
    reg = TurnSourcesRegistry()
    src = [{"label": "行业月报 PDF", "tool": "kb_search"}]  # No artifact_id
    reg.ingest_payload_sources(src, [])
    reg.ingest_payload_sources(src, [])
    reg.ingest_payload_sources(src, [])
    labels = [e.get("label") for e in reg.sources]
    assert labels.count("行业月报 PDF") == 1, labels
