"""飞书07.06 #11 — gateway 侧引用修复回归锁。

- `_closure_with_source_refs`: aggregator 批量 ref 不再塌缩出顶层
  artifact_id 后, 分享报告/导出的 artifact 闭包必须遍历 sub_sources —
  漏了 = 批量 call 的子表在分享报告里 403。
- IM `resolve_markers`: `[figure:sec_x]` (report 正文图锚) 此前漏在
  strip 清单外, 原样漏进飞书卡片文本。
"""

from __future__ import annotations

from bowhead_gateway.im.blocks import resolve_markers
from bowhead_gateway.routes.canvas import _closure_with_source_refs


def test_closure_iterates_sub_sources() -> None:
    refs = [
        {  # Batch ref: no top-level artifact_id (collapse removed), only sub_sources.
            "id": "call_b",
            "tool": "bi_query",
            "sub_sources": [
                {"artifact_id": "art_branch01", "query": "一级分行"},
                {"artifact_id": "art_yoy02", "query": "同比"},
            ],
        },
        {"id": "call_s", "tool": "bi_query", "artifact_id": "art_single"},
    ]
    closure = _closure_with_source_refs(["sec_art"], refs)
    assert set(closure) == {"sec_art", "art_branch01", "art_yoy02", "art_single"}


def test_closure_dedupes_and_tolerates_junk() -> None:
    refs = [
        {"artifact_id": "art_x", "sub_sources": [{"artifact_id": "art_x"}, "junk", {}]},
        "not_a_dict",
    ]
    closure = _closure_with_source_refs([], refs)  # type: ignore[arg-type]
    assert closure == ["art_x"]


def test_resolve_markers_strips_figure_anchor() -> None:
    out = resolve_markers("结论见下图。[figure:sec_275edeb6daaf]\n**图1**：3月同比。")
    assert "[figure:" not in out
    assert "结论见下图" in out and "图1" in out
