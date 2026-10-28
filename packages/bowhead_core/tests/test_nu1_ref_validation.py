"""Sprint ν1 Phase 1c — _ref_validation tests.

Tests:
- _flatten_tool_invocations (incl. nested)
- _extract_kb_chunk_ids
- _scan_markers (num + ref)
- _scan_evidence (chart / table / computation)
- _validate_all_refs (full payload walk, all 11 fields)
- _strip_invalid_refs (whitespace normalize + dangling marker)
- _build_real_refs (mocked stores)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core._ref_validation import (
    _build_real_refs,
    _extract_kb_chunk_ids,
    _flatten_tool_invocations,
    _normalize_whitespace,
    _scan_evidence,
    _scan_markers,
    _strip_invalid_refs,
    _validate_all_refs,
)


# ─── Mock ToolInvocation / Artifact ───
@dataclass
class _Inv:
    call_id: str = ""
    tool_name: str = ""
    result: Any = None
    nested: list[_Inv] = field(default_factory=list)


@dataclass
class _Art:
    id: str


# ─── _flatten_tool_invocations ───


def test_flatten_empty() -> None:
    assert _flatten_tool_invocations([]) == []


def test_flatten_flat_no_nested() -> None:
    invs = [_Inv(call_id="tl_1"), _Inv(call_id="tl_2")]
    flat = _flatten_tool_invocations(invs)
    assert len(flat) == 2
    assert {i.call_id for i in flat} == {"tl_1", "tl_2"}


def test_flatten_recursive_nested() -> None:
    """subagent.spawn 返 ToolInvocation.nested 含 sub-tool invocations."""
    leaf = _Inv(call_id="tl_leaf")
    mid = _Inv(call_id="tl_mid", nested=[leaf])
    top = _Inv(call_id="tl_top", nested=[mid])
    flat = _flatten_tool_invocations([top])
    assert {i.call_id for i in flat} == {"tl_top", "tl_mid", "tl_leaf"}


# ─── _extract_kb_chunk_ids ───


def test_extract_kb_no_kb_search_inv() -> None:
    invs = [_Inv(call_id="tl_1", tool_name="bi_query")]
    assert _extract_kb_chunk_ids(invs) == set()


def test_extract_kb_chunks() -> None:
    inv = _Inv(
        call_id="tl_kb",
        tool_name="kb_search",
        result={
            "chunks": [
                {"chunk_id": "chunk_abc123", "content": "foo"},
                {"chunk_id": "chunk_def456", "content": "bar"},
            ],
        },
    )
    ids = _extract_kb_chunk_ids([inv])
    assert ids == {"chunk_abc123", "chunk_def456"}


def test_extract_kb_chunks_in_nested() -> None:
    """KB search 在 subagent.spawn nested 里."""
    kb_inv = _Inv(
        tool_name="kb_search",
        result={"chunks": [{"chunk_id": "chunk_n1"}]},
    )
    spawn = _Inv(call_id="tl_spawn", tool_name="subagent_spawn", nested=[kb_inv])
    assert _extract_kb_chunk_ids([spawn]) == {"chunk_n1"}


def test_extract_kb_handles_malformed_result() -> None:
    """result 不是 dict / chunks 不是 list / chunks 项缺 chunk_id 都不崩."""
    invs = [
        _Inv(tool_name="kb_search", result=None),
        _Inv(tool_name="kb_search", result={"chunks": "not_a_list"}),
        _Inv(tool_name="kb_search", result={"chunks": [{"foo": "bar"}]}),
        _Inv(tool_name="kb_search", result={"chunks": [{"chunk_id": ""}]}),  # empty
    ]
    assert _extract_kb_chunk_ids(invs) == set()


# ─── _scan_markers (num + ref) ───


def test_scan_markers_clean_text() -> None:
    real_refs = {"tl": {"tl_real"}, "doc": {"chunk_real"}}
    assert _scan_markers("just plain text 13.2%", "f", real_refs) == []


def test_scan_markers_num_src_in_allowlist() -> None:
    real_refs = {"tl": {"tl_real"}, "doc": set()}
    text = "营收 [num:13.9:亿元:src:tl_real] 元"
    assert _scan_markers(text, "f", real_refs) == []


def test_scan_markers_num_src_not_in_allowlist() -> None:
    real_refs = {"tl": {"tl_real"}, "doc": set()}
    text = "营收 [num:13.9:亿元:src:tl_fake]"
    errs = _scan_markers(text, "insights[0].narrative", real_refs)
    assert len(errs) == 1
    assert errs[0]["kind"] == "num_src_not_in_allowlist"
    assert errs[0]["ref_id"] == "tl_fake"
    assert errs[0]["field"] == "insights[0].narrative"


def test_scan_markers_ref_in_allowlist() -> None:
    real_refs = {"tl": set(), "doc": {"chunk_abc"}}
    text = "去年合规 [ref:chunk_abc] 强调..."
    assert _scan_markers(text, "f", real_refs) == []


def test_scan_markers_ref_not_in_allowlist() -> None:
    real_refs = {"tl": set(), "doc": {"chunk_real"}}
    text = "见 [ref:chunk_fake] 文档"
    errs = _scan_markers(text, "f", real_refs)
    assert len(errs) == 1
    assert errs[0]["kind"] == "kb_ref_not_in_allowlist"
    assert errs[0]["ref_id"] == "chunk_fake"


def test_scan_markers_multiple_refs_mixed() -> None:
    real_refs = {"tl": {"tl_ok"}, "doc": {"chunk_ok"}}
    text = "营收 [num:13.9:亿元:src:tl_ok] 来自 [ref:chunk_ok], 另 [num:1.5:倍:src:tl_fake] 跟 [ref:chunk_fake]."
    errs = _scan_markers(text, "f", real_refs)
    fake_kinds = sorted(e["kind"] for e in errs)
    assert fake_kinds == ["kb_ref_not_in_allowlist", "num_src_not_in_allowlist"]


def test_scan_markers_empty_text() -> None:
    assert _scan_markers("", "f", {"tl": set(), "doc": set()}) == []


def test_scan_markers_non_str() -> None:
    assert _scan_markers(None, "f", {"tl": set(), "doc": set()}) == []  # type: ignore[arg-type]
    assert _scan_markers(123, "f", {"tl": set(), "doc": set()}) == []  # type: ignore[arg-type]


# ─── _scan_evidence ───


def test_scan_evidence_chart_in_allowlist() -> None:
    real_refs = {"chart": {"sec_real"}, "data": set()}
    ev = {"kind": "chart", "chart_section_id": "sec_real", "caption": "ok"}
    assert _scan_evidence(ev, "insights[0].evidence[0]", real_refs) == []


def test_scan_evidence_chart_hallucinate() -> None:
    real_refs = {"chart": {"sec_real"}, "data": set()}
    ev = {"kind": "chart", "chart_section_id": "sec_fake", "caption": "x"}
    errs = _scan_evidence(ev, "insights[0].evidence[0]", real_refs)
    assert len(errs) == 1
    assert errs[0]["kind"] == "chart_ref_not_in_allowlist"
    assert errs[0]["field"] == "insights[0].evidence[0].chart_section_id"


def test_scan_evidence_table_hallucinate() -> None:
    real_refs = {"chart": set(), "data": {"art_real"}}
    ev = {"kind": "table", "table_artifact_id": "art_fake", "caption": "x"}
    errs = _scan_evidence(ev, "insights[0].evidence[0]", real_refs)
    assert len(errs) == 1
    assert errs[0]["kind"] == "data_ref_not_in_allowlist"


def test_scan_evidence_computation_hallucinate() -> None:
    real_refs = {"chart": set(), "data": {"art_real"}}
    ev = {"kind": "computation", "computation_artifact_id": "art_fake", "caption": "x"}
    errs = _scan_evidence(ev, "insights[0].evidence[0]", real_refs)
    assert len(errs) == 1
    assert errs[0]["kind"] == "data_ref_not_in_allowlist"


def test_scan_evidence_callout_no_ref_field() -> None:
    """callout kind 不查 ref field."""
    real_refs = {"chart": set(), "data": set()}
    ev = {"kind": "callout", "callout_text": "Just a note"}
    assert _scan_evidence(ev, "insights[0].evidence[0]", real_refs) == []


# ─── _validate_all_refs (complete walks) ───


def test_validate_full_payload_clean() -> None:
    real_refs = {
        "chart": {"sec_a"},
        "data": {"art_a"},
        "code": set(),
        "doc": {"chunk_a"},
        "tl": {"tl_a"},
    }
    payload = {
        "eyebrow": "业绩分析",
        "exec_summary": "营收 [num:1.2:亿元:src:tl_a]",
        "caveats": ["数据 [ref:chunk_a]"],
        "kpis": [
            {"label": "GMV", "value": "1.2 亿元", "delta": "+5%"},
        ],
        "insights": [
            {
                "title": "Title 1",
                "narrative": "见 [ref:chunk_a]",
                "thesis": "Thesis",
                "evidence": [
                    {"kind": "chart", "chart_section_id": "sec_a", "caption": "Chart"},
                    {"kind": "callout", "callout_text": "Note"},
                ],
            },
        ],
        "recommendations": [
            {"rationale": "[num:1.5:倍:src:tl_a]", "action": "Do X", "expected_impact": "high"},
        ],
    }
    errs = _validate_all_refs(payload, real_refs)
    assert errs == []


def test_validate_full_payload_eyebrow_field() -> None:
    """eyebrow field 也走 walk (Eng r2 P0-2 closes)."""
    real_refs = {"chart": set(), "data": set(), "doc": set(), "tl": set(), "code": set()}
    payload = {"eyebrow": "见 [ref:chunk_fake] 报告"}
    errs = _validate_all_refs(payload, real_refs)
    assert len(errs) == 1
    assert errs[0]["field"] == "payload.eyebrow"


def test_validate_full_payload_kpi_delta_field() -> None:
    """kpi.delta 也走 walk."""
    real_refs = {"chart": set(), "data": set(), "doc": set(), "tl": set(), "code": set()}
    payload = {"kpis": [{"label": "X", "value": "1", "delta": "[ref:chunk_fake]"}]}
    errs = _validate_all_refs(payload, real_refs)
    assert len(errs) == 1
    assert errs[0]["field"] == "kpis[0].delta"


def test_validate_full_payload_evidence_callout_text() -> None:
    """evidence.callout_text 也走 walk."""
    real_refs = {"chart": set(), "data": set(), "doc": set(), "tl": set(), "code": set()}
    payload = {"insights": [{"evidence": [{"kind": "callout", "callout_text": "[ref:fake]"}]}]}
    errs = _validate_all_refs(payload, real_refs)
    assert len(errs) == 1


def test_validate_full_payload_insight_title_field() -> None:
    """insight.title 也走 walk (CTO r2 P1-N3 closes)."""
    real_refs = {"chart": set(), "data": set(), "doc": set(), "tl": set(), "code": set()}
    payload = {"insights": [{"title": "[ref:chunk_fake]"}]}
    errs = _validate_all_refs(payload, real_refs)
    assert len(errs) == 1
    assert errs[0]["field"] == "insights[0].title"


def test_validate_full_payload_recommendations_walks() -> None:
    real_refs = {"chart": set(), "data": set(), "doc": set(), "tl": set(), "code": set()}
    payload = {
        "recommendations": [
            {
                "rationale": "[ref:bad1]",
                "action": "[ref:bad2]",
                "expected_impact": "[ref:bad3]",
            }
        ]
    }
    errs = _validate_all_refs(payload, real_refs)
    assert len(errs) == 3


# ─── _normalize_whitespace ───


def test_normalize_whitespace_collapse() -> None:
    assert _normalize_whitespace("a  b   c") == "a b c"
    assert _normalize_whitespace("  leading and trailing  ") == "leading and trailing"
    assert _normalize_whitespace("如图  所示") == "如图 所示"  # Double spaces → single (closes Eng r2 P1).


# ─── _strip_invalid_refs ───


def test_strip_invalid_refs_marker_text() -> None:
    payload = {
        "exec_summary": "营收 [num:13.9:亿元:src:tl_fake] 元和  10%",
    }
    errors = [
        {
            "kind": "num_src_not_in_allowlist",
            "field": "payload.exec_summary",
            "value": "[num:13.9:亿元:src:tl_fake]",
        }
    ]
    result = _strip_invalid_refs(payload, errors)
    assert "[num:" not in result["exec_summary"]
    assert "tl_fake" not in result["exec_summary"]
    # Normalize whitespace: double → single spaces; strip leading/trailing whitespace.
    assert "  " not in result["exec_summary"]


def test_strip_invalid_refs_evidence_field() -> None:
    payload = {
        "insights": [
            {
                "evidence": [
                    {
                        "kind": "chart",
                        "chart_section_id": "sec_fake",
                        "caption": "Chart caption",
                    }
                ],
            }
        ]
    }
    errors = [
        {
            "kind": "chart_ref_not_in_allowlist",
            "field": "insights[0].evidence[0].chart_section_id",
            "value": "sec_fake",
        }
    ]
    result = _strip_invalid_refs(payload, errors)
    ev = result["insights"][0]["evidence"][0]
    assert ev["chart_section_id"] is None
    assert ev["caption"] == "Chart caption"  # Unchanged.


def test_strip_invalid_refs_bakes_num_marker_keeping_the_value() -> None:
    """2026-09-06 review 修：`[num:V:U:src:S]` 的 src 无效时**烘焙成纯文本保留数值**，不再整个删掉
    （原来 "合规率 [num:11.39:%:src:x] 偏低" 会变成 "合规率 偏低"——用户可见的内容丢失）；`[ref:S]` 仍整个去掉。"""
    payload = {"insights": [{"narrative": "如图 [num:1:src:tl_bad] 所示，合规率 [num:11.39:%:src:tl_bad] 偏低 [ref:doc_bad]。"}]}
    errors = [
        {"kind": "num_src_not_in_allowlist", "field": "insights[0].narrative", "value": "[num:1:src:tl_bad]"},
        {"kind": "num_src_not_in_allowlist", "field": "insights[0].narrative", "value": "[num:11.39:%:src:tl_bad]"},
        {"kind": "kb_ref_not_in_allowlist", "field": "insights[0].narrative", "value": "[ref:doc_bad]"},
    ]
    result = _strip_invalid_refs(payload, errors)
    text = result["insights"][0]["narrative"]
    assert "11.39%" in text and "1 所示" in text, text
    assert "[num:" not in text and "[ref:" not in text and "tl_bad" not in text


# ─── _build_real_refs ───


class _MockCanvasStore:
    def __init__(self, by_kind: dict[str, set[str]]) -> None:
        self._by_kind = by_kind

    async def list_section_ids_by_kind(self, canvas_id: str, *, kind: str) -> set[str]:
        return self._by_kind.get(kind, set())


class _MockArtifactStore:
    def __init__(self, arts: list[Any]) -> None:
        self._arts = arts

    async def list_for_session(self, *, session_id: str, user_id: str) -> list[Any]:
        return self._arts


@pytest.mark.asyncio
async def test_build_real_refs_full() -> None:
    cs = _MockCanvasStore({"chart": {"sec_1", "sec_2"}, "code_block": {"sec_code"}})
    ars = _MockArtifactStore([_Art(id="art_1"), _Art(id="art_2")])
    invs = [
        _Inv(call_id="tl_x", tool_name="bi_query"),
        _Inv(
            call_id="tl_kb",
            tool_name="kb_search",
            result={"chunks": [{"chunk_id": "chunk_1"}]},
        ),
    ]
    real = await _build_real_refs(
        canvas_id="cv_1",
        session_id="s_1",
        user_id="u_1",
        canvas_store=cs,
        artifact_store=ars,
        tool_invocations=invs,
    )
    assert real["chart"] == {"sec_1", "sec_2"}
    assert real["code"] == {"sec_code"}
    assert real["data"] == {"art_1", "art_2"}
    assert real["doc"] == {"chunk_1"}
    assert real["tl"] == {"tl_x", "tl_kb"}


@pytest.mark.asyncio
async def test_build_real_refs_image_sections_join_chart_bucket() -> None:
    """feedback-0713 #3: chart_section_id 是视觉引用——compute peer 经
    add_image 落的 kind="image" section 必须进 chart allowlist 桶。
    此前 image 结构性不在名单, peer 守规引用真实 image id 仍 100% 被拒
    （prod s_f7f79d3d2ceb 三 turn 复现）, 重试丢引用才通过 = 图片证据永久
    失去可点溯源。回退本行为的改动会让本测试红。
    """
    cs = _MockCanvasStore(
        {
            "chart": {"sec_chart"},
            "image": {"sec_img1", "sec_img2"},
            "code_block": set(),
        }
    )
    real = await _build_real_refs(
        canvas_id="cv_1",
        session_id="s_1",
        user_id="u_1",
        canvas_store=cs,
        artifact_store=_MockArtifactStore([]),
        tool_invocations=[],
    )
    assert real["chart"] == {"sec_chart", "sec_img1", "sec_img2"}
    # End to end: evidence referencing an image section ID no longer raises chart_ref_not_in_allowlist.
    ev = {"kind": "chart", "chart_section_id": "sec_img1", "caption": "毛利率双轴图"}
    assert _scan_evidence(ev, "insights[0].evidence[0]", real) == []


@pytest.mark.asyncio
async def test_build_real_refs_store_failure_returns_empty_set() -> None:
    """Store throws → returns empty (validate falls open not closed)."""

    class _FailingCS:
        async def list_section_ids_by_kind(self, *args, **kwargs):
            raise RuntimeError("db down")

    class _FailingAS:
        async def list_for_session(self, **kwargs):
            raise RuntimeError("artifacts unavailable")

    real = await _build_real_refs(
        canvas_id="cv_1",
        session_id="s_1",
        user_id="u_1",
        canvas_store=_FailingCS(),
        artifact_store=_FailingAS(),
        tool_invocations=[],
    )
    assert real["chart"] == set()
    assert real["data"] == set()
