"""report-engine Phase-2 §13.1 — turn-global Sources registry (multi-writer P0①).

The substrate that gives multiple writers ONE numbering space: dedup by content
id, render-time single-pass numbering, and the previously-DEAD section
`source_refs` field flowing into a real Sources list. Pure/deterministic — no LLM."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from bowhead_core.sources_resolver import (
    TurnSourcesRegistry,
    ensure_turn_sources_registry,
)


@dataclass
class _Inv:
    """Minimal ToolInvocation stand-in (build_sources_list/resolve_in_text tolerate
    dataclass or dict via _get_attr)."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


# ── registration + dedup ─────────────────────────────────────
def test_register_source_dedups_by_content_id() -> None:
    reg = TurnSourcesRegistry()
    n1 = reg.register_source("art_abc", tool="bi_query", query="2025 revenue")
    n2 = reg.register_source("art_def", tool="bi_query", query="2025 cost")
    n3 = reg.register_source("art_abc", tool="bi_query")  # same id again
    assert (n1, n2) == (1, 2)
    assert n3 == n1  # dedup — same N, not a 3rd row
    assert reg.size() == 2
    assert [s["artifact_id"] for s in reg.sources] == ["art_abc", "art_def"]


@contextmanager
def _answer_language(lang: str) -> Iterator[None]:
    from bowhead_core.agents._lead_context import reset_current_output_language, set_current_output_language

    token = set_current_output_language(lang)
    try:
        yield
    finally:
        reset_current_output_language(token)


def test_register_source_synthesizes_label_when_absent() -> None:
    with _answer_language("zh-CN"):
        reg = TurnSourcesRegistry()
        reg.register_source("art_x", tool="kb_search", query="政策")
    assert reg.sources[0]["label"] == "搜索知识库: 政策"  # Display-name prefix, not tool ID.
    # No answer language on the turn → English display name.
    reg = TurnSourcesRegistry()
    reg.register_source("art_x", tool="kb_search", query="政策")
    assert reg.sources[0]["label"] == "Search Knowledge Base: 政策"


def test_empty_registry() -> None:
    reg = TurnSourcesRegistry()
    assert reg.is_empty() and reg.size() == 0 and reg.sources == []


# ── cross-payload (cross-peer) dedup — the §13.1.2 fix ───────
def test_ingest_two_payloads_dedups_shared_source() -> None:
    reg = TurnSourcesRegistry()
    # peer A and peer B both cite the same artifact art_shared + one unique each
    reg.ingest_payload_sources(
        [
            {"label": "A1", "artifact_id": "art_shared"},
            {"label": "A2", "artifact_id": "art_a_only"},
        ]
    )
    reg.ingest_payload_sources(
        [
            {"label": "B1", "artifact_id": "art_shared"},  # dup across peers
            {"label": "B2", "artifact_id": "art_b_only"},
        ]
    )
    ids = [s["artifact_id"] for s in reg.sources]
    assert ids == ["art_shared", "art_a_only", "art_b_only"]  # shared collapsed
    assert reg.id_to_index["art_shared"] == 1  # first-write wins


# ── shared numbering across writers — the §13.1.3 single-pass ─
def test_resolve_text_shares_numbering_across_writers() -> None:
    reg = TurnSourcesRegistry()
    # writer 1 references art_1 then art_2; writer 2 references art_2 then art_3
    t1 = reg.resolve_text("rev was [ref:art_1] and cost [ref:art_2]")
    t2 = reg.resolve_text("cost again [ref:art_2], margin [ref:art_3]")
    assert t1 == "rev was [1] and cost [2]"
    # art_2 keeps N=2 across writers; art_3 is the next free N=3 (NOT restarting)
    assert t2 == "cost again [2], margin [3]"
    assert reg.size() == 3


def test_resolve_text_num_marker_to_bracket_n() -> None:
    reg = TurnSourcesRegistry()
    out = reg.resolve_text("利润率 [num:93.35:%:src:tl_abc] 同比")
    assert out == "利润率 93.35 % [1] 同比"
    assert reg.sources[0]["artifact_id"] == "tl_abc"


def test_resolve_text_lazy_label_from_tool_invocations() -> None:
    reg = TurnSourcesRegistry()
    inv = _Inv(call_id="tl_q", tool_name="bi_query", arguments={"query": "客流量"}, result={"artifact_id": "tl_q"})
    out = reg.resolve_text("数据 [ref:tl_q]", tool_invocations=[inv])
    assert out == "数据 [1]"
    # entry was lazily materialised from the invocation, not a bare synth
    assert reg.sources[0]["artifact_id"] == "tl_q"
    assert reg.sources[0]["tool"] == "bi_query"


# ── ensure_turn_sources_registry lazy-init + sharing ─────────
def test_ensure_registry_is_per_ctx_singleton() -> None:
    class _Ctx:
        pass

    ctx = _Ctx()
    r1 = ensure_turn_sources_registry(ctx)
    r2 = ensure_turn_sources_registry(ctx)
    assert r1 is r2  # same instance → siblings share one numbering space


def test_ensure_registry_tolerates_frozen_ctx() -> None:
    class _Frozen:
        __slots__ = ()  # cannot setattr

    reg = ensure_turn_sources_registry(_Frozen())
    assert isinstance(reg, TurnSourcesRegistry)  # transient, no crash
    reg.register_source("art_z")
    assert reg.size() == 1


def test_ingest_tool_invocations_eagerly_registers_artifacts() -> None:
    """The writer's narrative has no [num:] markers, so build_sources_list would
    leave the Sources section empty. ingest_tool_invocations eagerly registers
    each data artifact (bi_query/bi_fetch_rows results carry artifact_id)."""
    reg = TurnSourcesRegistry()
    invs = [
        _Inv(call_id="c1", tool_name="bi_fetch_rows", arguments={"query": "各省营业额"}, result={"artifact_id": "art_1"}),
        _Inv(call_id="c2", tool_name="bi_fetch_rows", arguments={"query": "各品牌营业额"}, result={"artifact_id": "art_2"}),
        _Inv(call_id="c3", tool_name="add_chart", arguments={}, result={"section_id": "sec_x"}),  # not a data source → skipped
        _Inv(call_id="c4", tool_name="bi_fetch_rows", arguments={"query": "各省营业额"}, result={"artifact_id": "art_1"}),  # dup
    ]
    reg.ingest_tool_invocations(invs)
    ids = [s["artifact_id"] for s in reg.sources]
    assert ids == ["art_1", "art_2"]  # chart skipped (no artifact_id), dup collapsed
    assert reg.sources[0]["tool"] == "bi_fetch_rows"


def test_ingest_tool_invocations_batch_shape_gives_human_query_labels() -> None:
    """Batch bi result {results:[{artifact_id, query_text}]} → each sub-result is
    its own source with a READABLE query label (not a bare 'artifact: <hex>')."""
    reg = TurnSourcesRegistry()
    invs = [
        _Inv(
            call_id="c1",
            tool_name="bi_query",
            arguments={},
            result={
                "results": [
                    {"artifact_id": "art_a", "query_text": "各省营业额"},
                    {"artifact_id": "art_b", "query_text": "各品牌营业额"},
                ]
            },
        )
    ]
    with _answer_language("zh-CN"):
        reg.ingest_tool_invocations(invs)
    assert [s["artifact_id"] for s in reg.sources] == ["art_a", "art_b"]
    assert reg.sources[0]["query"] == "各省营业额"
    assert reg.sources[0]["label"] == "查询数据: 各省营业额"  # Human, not hex; prefix readable too.
    assert "art_" not in reg.sources[0]["label"]


def test_ingest_tool_invocations_captures_web_search_pages() -> None:
    """web_search results {title, url, snippet} register as sources (id=url,
    label=title) — so a market/web report cites real sources, not nothing."""
    reg = TurnSourcesRegistry()
    invs = [
        _Inv(
            call_id="w",
            tool_name="web_search",
            arguments={},
            result={
                "results": [
                    {"title": "比亚迪5月销量", "url": "https://a.com/byd", "snippet": "…"},
                    {"title": "特斯拉中国", "url": "https://b.com/tsla", "snippet": "…"},
                    {"title": "比亚迪5月销量", "url": "https://a.com/byd"},  # dup url
                ]
            },
        )
    ]
    reg.ingest_tool_invocations(invs)
    assert [s["artifact_id"] for s in reg.sources] == ["https://a.com/byd", "https://b.com/tsla"]
    assert reg.sources[0]["label"] == "比亚迪5月销量" and reg.sources[0]["tool"] == "web_search"


def test_dead_source_refs_now_flow_to_sources() -> None:
    """The WP-B section `source_refs` field was inert (stored, never built into
    Sources). Registering them here makes them a real numbered Sources list."""
    reg = TurnSourcesRegistry()
    for ref in ["art_p", "art_q", "art_p"]:  # a section's source_refs, with a dup
        reg.register_source(ref, tool="bi_query")
    assert [s["artifact_id"] for s in reg.sources] == ["art_p", "art_q"]


# ── citation-hover original view: inline-data preview from bi_query sample_rows ──
def test_ingest_batch_attaches_sample_rows_preview() -> None:
    """A bi_query sub-result's sample_rows fold into a compact preview on the
    Sources entry so the citation hover can show 原文 rows (the tl_ artifact_id
    is a datarc ledger ref that 404s on /files, so carrying the rows is the only
    way the popover can render data without a dead link)."""
    reg = TurnSourcesRegistry()
    invs = [
        _Inv(
            call_id="c1",
            tool_name="bi_query",
            arguments={},
            result={
                "results": [
                    {"artifact_id": "tl_x", "query_text": "2026年1-6月 销售金额", "sample_rows": [{"销售金额": "9608715342.51"}], "total_rows": 1},
                ]
            },
        )
    ]
    reg.ingest_tool_invocations(invs)
    e = reg.sources[0]
    assert e["preview_columns"] == ["销售金额"]
    assert e["preview_rows"] == [{"销售金额": "9608715342.51"}]
    assert e["total_rows"] == 1


def test_preview_augments_entry_registered_before_rows_seen() -> None:
    """payload.sources registers the bi_query artifact_id FIRST (no rows), then
    ingest_tool_invocations sees the same id WITH sample_rows — first-write-wins
    dedup must still graft the preview onto the existing row, else the popover
    data is lost (writer-loop calls ingest_payload_sources then _invocations)."""
    reg = TurnSourcesRegistry()
    reg.ingest_payload_sources([{"label": "bi_query: q", "tool": "bi_query", "artifact_id": "tl_x", "query": "q"}])
    assert "preview_rows" not in reg.sources[0]  # no rows yet
    reg.ingest_tool_invocations(
        [
            _Inv(
                call_id="c",
                tool_name="bi_query",
                arguments={},
                result={"results": [{"artifact_id": "tl_x", "query_text": "q", "sample_rows": [{"a": 1}, {"a": 2}]}]},
            )
        ]
    )
    assert reg.size() == 1  # still one row (deduped)
    assert reg.sources[0]["preview_rows"] == [{"a": "1"}, {"a": "2"}]  # grafted
    assert reg.sources[0]["total_rows"] == 2  # falls back to len(sample_rows)


def test_preview_caps_rows_cols_and_cell_length() -> None:
    """Preview stays tiny — ≤5 rows, ≤6 cols, bounded cell length — so a fat
    query result can't bloat the persisted entry or the hover popover."""
    reg = TurnSourcesRegistry()
    wide = {f"col{i}": i for i in range(10)}  # 10 cols → cap 6
    long_cell = {"c": "x" * 200}  # 200 chars → cap 80+…
    rows = [wide] * 8 + [long_cell]  # 9 rows → cap 5
    reg.ingest_tool_invocations(
        [
            _Inv(
                call_id="c",
                tool_name="bi_query",
                arguments={},
                result={"results": [{"artifact_id": "tl_x", "query_text": "q", "sample_rows": rows, "total_rows": 999}]},
            )
        ]
    )
    e = reg.sources[0]
    assert len(e["preview_columns"]) == 6
    assert len(e["preview_rows"]) == 5
    assert e["total_rows"] == 999  # true total preserved even though rows capped
    # long cell truncated with ellipsis
    reg2 = TurnSourcesRegistry()
    reg2.ingest_tool_invocations(
        [
            _Inv(
                call_id="c",
                tool_name="bi_query",
                arguments={},
                result={"results": [{"artifact_id": "tl_y", "query_text": "q", "sample_rows": [{"c": "x" * 200}]}]},
            )
        ]
    )
    assert reg2.sources[0]["preview_rows"][0]["c"].endswith("…")
    assert len(reg2.sources[0]["preview_rows"][0]["c"]) == 81  # 80 + ellipsis


def test_no_preview_keys_when_no_sample_rows() -> None:
    """Sources with no sample_rows (kb_search, web_search, bare artifacts) stay
    lean — no empty preview_* keys polluting the entry."""
    reg = TurnSourcesRegistry()
    reg.register_source("art_bare", tool="bi_query", query="q")  # no rows
    reg.register_source("art_empty", tool="bi_query", query="q", sample_rows=[])
    for e in reg.sources:
        assert "preview_rows" not in e and "preview_columns" not in e
        assert "total_rows" not in e
