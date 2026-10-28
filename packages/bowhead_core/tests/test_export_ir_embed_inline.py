"""embed-inline pass (export-embed-inline tech-design §1/§2) — report_embedded 图块内联.

修的问题：V2 PDF/PPTX 里 `[figure:sec_*]` 锚点字面残留、图表按投影序堆在正文前
（116 dogfood 实拍）。pass 把 embedded 图块移进宿主 children 的锚位；semantic
tokens 加兜底带（任何路径不字面渲染锚）。
"""

from __future__ import annotations

from bowhead_core.export_ir import (
    _semantic_tokens,
    iter_export_blocks,
    normalize_export_snapshot,
)
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.print_theme import current_print_theme_snapshot


def _snapshot(sections: list[dict], entries: list[dict]) -> ExportSnapshot:
    token = f"sha256:{'a' * 64}"
    for section in sections:
        section.setdefault("rev", 1)
        section.setdefault("source_refs", [])
    for order, (entry, section) in enumerate(zip(entries, sections, strict=True)):
        entry.setdefault("section_id", section["id"])
        entry.setdefault("section_rev", section["rev"])
        entry.setdefault("effective_kind", section["kind"])
        entry.setdefault("order", order)
        entry.setdefault("placement", "report_body")
        entry.setdefault("parent_section_id", None)
    payload = {
        "request": {
            "format": "pdf",
            "profile": "full_report",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
            "options": {"include_appendix": True},
        },
        "surface_projection": {
            "contract_version": 1,
            "token": token,
            "entries": entries,
            "exclusions": [],
        },
        "sections": sections,
    }
    return ExportSnapshot(
        id="exs_inline",
        schema_version=1,
        canvas_id="cv_inline",
        session_id="s_inline",
        team_id="team_i",
        workspace_id="ws_i",
        canvas_owner="u_o",
        requested_by="u_o",
        fmt="pdf",
        profile="full_report",
        title="inline fixture",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'b' * 64}",
        surface_contract_version=1,
        surface_token=token,
        parity_contract="full_content_v1",
        payload=payload,
        payload_sha256=f"sha256:{'c' * 64}",
        size_bytes=1024,
        section_count=len(sections),
        created_at="2026-07-31T00:00:00+00:00",
        expires_at="2026-08-07T00:00:00+00:00",
    )


def _chart(section_id: str) -> dict:
    return {"id": section_id, "kind": "chart", "payload": {"spec": {"kind": "bar", "title": f"图 {section_id}", "rows": [{"x": 1}]}}}


def _host_with_charts(markdown: str, chart_ids: list[str]) -> ExportSnapshot:
    sections = [
        {"id": "sec_host", "kind": "narrative", "payload": {"markdown": markdown}},
        *[_chart(cid) for cid in chart_ids],
    ]
    entries: list[dict] = [{}] + [{"placement": "report_embedded", "parent_section_id": "sec_host"} for _ in chart_ids]
    return _snapshot(sections, entries)


def test_multi_anchor_split_interleaves_segments_and_visuals() -> None:
    ir = normalize_export_snapshot(
        _host_with_charts("开头段\n\n[figure:sec_c1]\n\n中段\n\n[figure:sec_c2]\n\n尾段", ["sec_c1", "sec_c2"]),
    )
    assert len(ir.blocks) == 1  # All visual blocks move into the host; only the host remains at top level.
    host = ir.blocks[0]
    assert host.content["markdown"].strip() == "开头段"
    child_shape = [(c.block_kind, c.source.section_id) for c in host.children]
    assert child_shape == [
        ("visual", "sec_c1"),
        ("paragraph", "sec_host"),
        ("visual", "sec_c2"),
        ("paragraph", "sec_host"),
    ]
    assert host.children[1].content["markdown"].strip() == "中段"
    assert host.children[3].content["markdown"].strip() == "尾段"
    # Flattened order = display order (PPTX consumer).
    flat = [b.block_key for b in iter_export_blocks(ir.blocks)]
    assert flat == ["sec_host", "sec_c1", "sec_host#seg1", "sec_c2", "sec_host#seg2"]
    # No token contains a literal anchor.
    assert not any("[figure:" in t for b in iter_export_blocks(ir.blocks) for t in b.semantic_tokens)


def test_moved_visual_keeps_identity_and_no_duplicate() -> None:
    ir = normalize_export_snapshot(_host_with_charts("正文 [figure:sec_c1] 后文", ["sec_c1"]))
    flat = iter_export_blocks(ir.blocks)
    visuals = [b for b in flat if b.block_kind == "visual"]
    assert len(visuals) == 1  # Move, do not copy.
    assert visuals[0].block_key == "sec_c1"
    assert visuals[0].visual_input_sha256 is not None
    # Moving blocks does not affect asset/source collection: identical to the same non-inline content (all report_body).
    sections = [
        {"id": "sec_host", "kind": "narrative", "payload": {"markdown": "正文 [figure:sec_c1] 后文"}},
        _chart("sec_c1"),
    ]
    baseline = normalize_export_snapshot(_snapshot(sections, [{}, {}]))
    assert ir.visual_assets == baseline.visual_assets
    assert ir.sources == baseline.sources


def test_dangling_anchor_is_stripped_with_warning() -> None:
    ir = normalize_export_snapshot(
        _host_with_charts("前 [figure:sec_ghost] 后 [figure:sec_c1] 尾", ["sec_c1"]),
    )
    assert "dangling_figure_anchor" in ir.normalization_warnings
    all_text = " ".join(t for b in iter_export_blocks(ir.blocks) for t in b.semantic_tokens)
    assert "[figure:" not in all_text
    assert "sec_ghost" not in all_text


def test_unanchored_embedded_appended_to_tail_with_warning() -> None:
    ir = normalize_export_snapshot(_host_with_charts("正文没有锚", ["sec_c1"]))
    assert "unanchored_embedded_visual" in ir.normalization_warnings
    host = ir.blocks[0]
    assert [c.source.section_id for c in host.children] == ["sec_c1"]


def test_no_embedded_relations_keeps_blocks_identical() -> None:
    sections = [
        {"id": "sec_a", "kind": "narrative", "payload": {"markdown": "普通正文"}},
        _chart("sec_c1"),
    ]
    entries: list[dict] = [{}, {}]  # Both report_body, with no parent.
    ir = normalize_export_snapshot(_snapshot(sections, entries))
    assert [b.block_key for b in ir.blocks] == ["sec_a", "sec_c1"]
    assert all(not b.children for b in ir.blocks)


def test_semantic_tokens_belt_strips_anchor_literal() -> None:
    tokens = _semantic_tokens(
        block_kind="paragraph",
        content={"markdown": "前文 [figure:sec_x] 后文"},
    )
    joined = " ".join(tokens)
    assert "[figure:" not in joined
    assert "前文" in joined and "后文" in joined
