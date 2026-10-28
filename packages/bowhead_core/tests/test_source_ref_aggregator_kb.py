"""SourceRef aggregator — KB + provenance extraction, grounded in the REAL
persisted tool_result event shape.

Prod-shape lesson (2026-06-26 116 dogfood): a tool_result event persists
  - result_preview : 500-char *string* (truncated JSON, useless for extraction)
  - result_meta    : _tool_result_meta(full_result) — lightweight citeable dict
  - result_inline  : full result dict, but ONLY when ≤ 4KB (else None)
The aggregator must extract from result_meta / result_inline (dicts), never the
string preview. Earlier fixtures put the chunks in result_preview *as a dict*,
so they passed green while prod returned bare refs. These tests build events via
the real `_tool_result_meta` so the fixture shape == prod shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bowhead_core.source_ref_aggregator import aggregate_source_refs_from_events
from bowhead_core.subagent import _citeable_meta, _preview, _tool_result_meta


@dataclass
class MockEvent:
    event_type: str
    payload: dict[str, Any]


_INLINE_CAP = 4096


def tool_result_event(call_id: str, tool: str, full_result: Any) -> MockEvent:
    """Build a tool_result event exactly as the subagent emit does: string
    preview + lightweight result_meta + size-capped result_inline."""
    inline = full_result if len(json.dumps(full_result, ensure_ascii=False, default=str)) <= _INLINE_CAP else None
    return MockEvent(
        "tool_result",
        {
            "call_id": call_id,
            "tool_name": tool,
            "result_preview": _preview(full_result),  # STRING (prod shape)
            "result_meta": _tool_result_meta(full_result),  # lightweight dict
            "result_inline": inline,
        },
    )


def tool_call_event(call_id: str, tool: str, query: str) -> MockEvent:
    return MockEvent(
        "tool_call",
        {
            "call_id": call_id,
            "tool_name": tool,
            "arguments_preview": {"query": query},
        },
    )


# ─── KB ────────────────────────────────────────────────────────────────────


def _kb_result(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"chunks": chunks, "total_candidates": len(chunks), "rerank_used": True, "latency_ms": 130}


def test_kb_search_yields_one_source_ref_per_chunk() -> None:
    full = _kb_result(
        [
            {
                "chunk_id": "chunk_aaa111",
                "file_id": "kbf_x",
                "file_name": "defs.pdf",
                "page": 3,
                "section_title": "GMV",
                "role": "glossary",
                "content": "GMV 定义...",
                "score": 0.91,
            },
            {
                "chunk_id": "chunk_bbb222",
                "file_id": "kbf_x",
                "file_name": "defs.pdf",
                "page": 4,
                "section_title": "GMV 公式",
                "role": "glossary",
                "content": "GMV = sum(订单)...",
                "score": 0.85,
            },
        ]
    )
    events = [
        tool_call_event("call_kb_1", "kb_search", "GMV 定义"),
        tool_result_event("call_kb_1", "kb_search", full),
    ]
    refs = aggregate_source_refs_from_events("turn_test", events)
    assert len(refs) == 2, f"expected 2 refs, got {len(refs)}"
    by_id = {r["id"]: r for r in refs}
    r1 = by_id["chunk_aaa111"]
    assert r1["tool"] == "kb_search"
    assert r1["kb_chunk_id"] == "chunk_aaa111"
    assert r1["kb_file_id"] == "kbf_x"
    assert r1["kb_filename"] == "defs.pdf"
    assert r1["kb_page"] == 3
    assert r1["kb_section_title"] == "GMV"
    assert r1["query"] == "GMV 定义"
    assert r1["turn_id"] == "turn_test"


def test_kb_search_survives_when_full_result_exceeds_inline_cap() -> None:
    """The whole point: a content-heavy kb_search (> 4KB → result_inline=None)
    must STILL yield kb refs, because result_meta carries the lightweight refs."""
    big_content = "条款" * 1500  # ~6KB per chunk → full result far exceeds 4KB cap
    full = _kb_result(
        [
            {
                "chunk_id": "chunk_big1",
                "file_id": "kbf_c",
                "file_name": "contract.pdf",
                "page": 5,
                "section_title": "付款",
                "content": big_content,
                "score": 0.9,
            },
            {
                "chunk_id": "chunk_big2",
                "file_id": "kbf_c",
                "file_name": "contract.pdf",
                "page": 6,
                "section_title": "违约",
                "content": big_content,
                "score": 0.8,
            },
        ]
    )
    ev = tool_result_event("call_big", "kb_search", full)
    assert ev.payload["result_inline"] is None, "precondition: full result must exceed inline cap"
    refs = aggregate_source_refs_from_events(
        "turn_big",
        [
            tool_call_event("call_big", "kb_search", "付款义务"),
            ev,
        ],
    )
    assert len(refs) == 2
    assert {r["kb_chunk_id"] for r in refs} == {"chunk_big1", "chunk_big2"}
    assert all(r["kb_file_id"] == "kbf_c" for r in refs)


def test_string_result_preview_alone_yields_no_enrichment() -> None:
    """Regression: when ONLY the string result_preview is present (no meta /
    inline), extraction must not crash and yields no chunks — proving the old
    code path (reading the string) produced bare refs."""
    events = [
        tool_call_event("call_s", "kb_search", "q"),
        MockEvent(
            "tool_result",
            {
                "call_id": "call_s",
                "tool_name": "kb_search",
                "result_preview": json.dumps(
                    _kb_result(
                        [
                            {"chunk_id": "chunk_s", "file_id": "f", "file_name": "d.pdf", "content": "x"},
                        ]
                    )
                )[:500],  # STRING, as in prod
                "result_meta": None,
                "result_inline": None,
            },
        ),
    ]
    refs = aggregate_source_refs_from_events("turn_s", events)
    assert refs == []  # no dict source → nothing extracted (graceful)


def test_kb_search_no_chunks_returns_no_refs() -> None:
    events = [
        tool_call_event("call_kb_2", "kb_search", "no match"),
        tool_result_event("call_kb_2", "kb_search", _kb_result([])),
    ]
    assert aggregate_source_refs_from_events("turn_x", events) == []


def test_kb_search_chunk_without_chunk_id_skipped() -> None:
    full = _kb_result(
        [
            {"chunk_id": "chunk_ok", "file_id": "kbf_a", "file_name": "f.pdf"},
            {"file_id": "kbf_b"},  # missing chunk_id
        ]
    )
    refs = aggregate_source_refs_from_events(
        "turn_x",
        [
            tool_call_event("call_x", "kb_search", "q"),
            tool_result_event("call_x", "kb_search", full),
        ],
    )
    assert len(refs) == 1
    assert refs[0]["kb_chunk_id"] == "chunk_ok"


# ─── BI / artifact ─────────────────────────────────────────────────────────


def test_bi_tool_extracts_sql_and_artifact_from_real_shape() -> None:
    full = {"sql": "SELECT SUM(gmv) FROM orders", "artifact_id": "art_csv", "rows": [{"gmv": 1}], "columns": ["gmv"]}
    refs = aggregate_source_refs_from_events(
        "turn_bi",
        [
            tool_call_event("bi_call", "bi_ask_question", "Q4 GMV"),
            tool_result_event("bi_call", "bi_ask_question", full),
        ],
    )
    assert len(refs) == 1
    assert refs[0]["tool"] == "bi_ask_question"
    assert refs[0]["sql"] == "SELECT SUM(gmv) FROM orders"
    assert refs[0]["artifact_id"] == "art_csv"
    assert "kb_chunk_id" not in refs[0]


def test_bi_artifact_survives_inline_cap() -> None:
    """A large bi_query (many rows → result_inline=None) must still surface its
    artifact_id, because result_meta carries it from the full result."""
    full = {"artifact_id": "art_big", "columns": ["a", "b"], "rows": [{"a": i, "b": "x" * 50} for i in range(400)]}
    ev = tool_result_event("bi_big", "bi_query", full)
    assert ev.payload["result_inline"] is None
    refs = aggregate_source_refs_from_events(
        "turn_bi2",
        [
            tool_call_event("bi_big", "bi_query", "all rows"),
            ev,
        ],
    )
    assert len(refs) == 1
    assert refs[0]["artifact_id"] == "art_big"


# ─── bi_query BATCH (real multi-query shape — the 0.71.6 dogfood gap) ────────


def _bi_batch(n: int = 3) -> dict:
    """REAL bi_query batch envelope (captured from live turn s_9a16d47a01ac): one
    tool call, N sub-queries, each its own artifact_id + _ref_artifact_id + query_text."""
    return {
        "results": [
            {
                "ok": True,
                "artifact_id": f"art_{i}",
                "_ref_artifact_id": f"art_{i}",
                "query_text": f"2026年 指标{i} 按省",
                "sample_rows": [{"省": "上海", "v": i}],
                "total_rows": 5,
            }
            for i in range(n)
        ]
    }


def test_bi_query_batch_lists_all_sub_sources() -> None:
    refs = aggregate_source_refs_from_events(
        "turn_batch",
        [
            tool_call_event("call_b", "bi_query", "对比各地区"),
            tool_result_event("call_b", "bi_query", _bi_batch(3)),
        ],
    )
    assert len(refs) == 1
    r = refs[0]
    assert r["tool"] == "bi_query"
    assert [s["artifact_id"] for s in r["sub_sources"]] == ["art_0", "art_1", "art_2"]
    assert r["sub_sources"][0]["query"] == "2026年 指标0 按省"
    # Feishu 07.06 #11: batch refs no longer collapse to a single artifact_id link (the old backward-compatible
    # "first sub-artifact" caused numbers to point at the wrong table). Consumers iterate sub_sources.
    assert "artifact_id" not in r


def test_bi_query_batch_legacy_event_enriches_from_result_inline() -> None:
    """Old event whose result_meta predates this still enriches: the aggregator
    derives sub_sources from the batch result_inline.results[]."""
    full = _bi_batch(2)
    ev = MockEvent(
        "tool_result",
        {
            "call_id": "call_legacy",
            "tool_name": "bi_query",
            "result_preview": json.dumps(full)[:500],  # STRING (prod)
            "result_meta": None,  # legacy: no citeable subset
            "result_inline": full,  # batch dict
        },
    )
    refs = aggregate_source_refs_from_events(
        "turn_legacy",
        [
            tool_call_event("call_legacy", "bi_query", "q"),
            ev,
        ],
    )
    assert [s["artifact_id"] for s in refs[0]["sub_sources"]] == ["art_0", "art_1"]


def test_bi_query_batch_falls_back_to_ref_artifact_id() -> None:
    full = {"results": [{"ok": True, "_ref_artifact_id": "art_ref_only", "query_text": "q1"}]}
    refs = aggregate_source_refs_from_events(
        "turn_r",
        [
            tool_call_event("call_r", "bi_query", "q"),
            tool_result_event("call_r", "bi_query", full),
        ],
    )
    assert refs[0]["sub_sources"][0]["artifact_id"] == "art_ref_only"


def test_flat_single_query_has_no_sub_sources() -> None:
    """Regression: the flat single-query shape (that already worked) stays single."""
    refs = aggregate_source_refs_from_events(
        "turn_flat",
        [
            tool_call_event("call_f", "bi_query", "q"),
            tool_result_event("call_f", "bi_query", {"artifact_id": "art_solo", "rows": [{"a": 1}], "columns": ["a"]}),
        ],
    )
    assert refs[0]["artifact_id"] == "art_solo"
    assert "sub_sources" not in refs[0]


# ─── _tool_result_meta / _citeable_meta unit ───────────────────────────────


def test_citeable_meta_extracts_lightweight_chunks_without_content() -> None:
    meta = _citeable_meta(
        _kb_result(
            [
                {"chunk_id": "c1", "file_id": "f1", "file_name": "a.pdf", "page": 2, "section_title": "S", "content": "HEAVY" * 1000, "score": 0.9},
            ]
        )
    )
    assert meta["chunks"] == [
        {"chunk_id": "c1", "file_id": "f1", "file_name": "a.pdf", "page": 2, "section_title": "S"},
    ]
    # content must NOT leak into the lightweight ref
    assert "content" not in meta["chunks"][0]


def test_citeable_meta_reads_nested_result_json() -> None:
    meta = _citeable_meta({"result_json": {"sql": "SELECT 1", "artifact_id": "art_z"}})
    assert meta["sql"] == "SELECT 1"
    assert meta["artifact_id"] == "art_z"


def test_citeable_meta_batch_sub_sources() -> None:
    meta = _citeable_meta(_bi_batch(2))
    assert [s["artifact_id"] for s in meta["sub_sources"]] == ["art_0", "art_1"]
    assert meta["sub_sources"][0]["query"] == "2026年 指标0 按省"
    assert meta["artifact_id"] == "art_0"  # backward-compat first


def test_tool_result_meta_keeps_legacy_count_summary() -> None:
    """Backward compat: existing list/rows count summaries must still be emitted."""
    assert _tool_result_meta([1, 2, 3]) == {"type": "array", "count": 3}
    meta = _tool_result_meta({"rows": [{"x": 1}], "measure_name": "gmv", "artifact_id": "art_q"})
    assert meta["type"] == "attribute"
    assert meta["row_count"] == 1
    assert meta["artifact_id"] == "art_q"  # citeable subset added alongside


def test_tool_result_meta_keeps_bounded_sandbox_output_refs_without_content() -> None:
    meta = _tool_result_meta(
        {
            "stdout": "sensitive output stays out of metadata",
            "output_artifacts": [
                {
                    "artifact_id": "art_stdout",
                    "name": "sandbox-stdout.txt",
                    "mime": "text/plain",
                    "size": 551,
                    "data_role": "stdout",
                    "preview": "must not leak",
                },
                {
                    "artifact_id": "art_markdown",
                    "name": "calc.md",
                    "mime": "text/markdown",
                    "size": 17,
                },
                {"artifact_id": "art_stdout", "name": "duplicate.txt"},
                {"name": "missing-id.txt"},
                {"artifact_id": "x" * 201, "name": "oversized-id.txt"},
            ],
        }
    )

    assert meta == {
        "output_artifacts": [
            {
                "artifact_id": "art_stdout",
                "filename": "sandbox-stdout.txt",
                "mime": "text/plain",
                "size": 551,
                "data_role": "stdout",
            },
            {
                "artifact_id": "art_markdown",
                "filename": "calc.md",
                "mime": "text/markdown",
                "size": 17,
            },
        ]
    }
    assert "stdout" not in meta
    assert "preview" not in str(meta)


def test_tool_result_meta_caps_sandbox_output_refs() -> None:
    meta = _tool_result_meta({"output_artifacts": [{"artifact_id": f"art_{index}", "name": f"{index}.txt"} for index in range(40)]})

    assert len(meta["output_artifacts"]) == 32
    assert meta["output_artifacts"][-1]["artifact_id"] == "art_31"


def test_tool_result_meta_none_when_nothing() -> None:
    assert _tool_result_meta({"unrelated": True}) is None
    assert _tool_result_meta("a string") is None
