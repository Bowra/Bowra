"""ExportIR v1 normalization, typed data, and 24-kind capability contracts."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.canvas_export_projection import SURFACE_KIND_POLICIES
from bowhead_core.export_capabilities import SECTION_CAPABILITIES
from bowhead_core.export_ir import (
    SECTION_ADAPTERS,
    ExportIRError,
    build_capability_plan,
    iter_export_blocks,
    normalize_export_snapshot,
)
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.print_theme import current_print_theme_snapshot

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/canvas_export/surface_projection_v1.json"
_BROWSER_VISUAL_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/canvas_export/browser_visual_request_v1.json"
_COPY_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/report_trust/b1a_copy_contract_v1.json"


def _fixture_snapshot() -> ExportSnapshot:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    theme = json.loads(_BROWSER_VISUAL_FIXTURE.read_text(encoding="utf-8"))["payload"]["theme"]
    by_id = {section["id"]: section for section in fixture["sections"]}
    entries = copy.deepcopy(fixture["expected_entries"])
    sections = [copy.deepcopy(by_id[entry["section_id"]]) for entry in entries]
    for section in sections:
        section.setdefault("source_refs", [])
    by_section = {section["id"]: section for section in sections}
    by_section["sec_h1"]["source_refs"] = [
        "tl_h1",
        {"tool_call_id": "tl_h1"},
        {"call_id": "call_h1"},
    ]
    by_section["sec_datarc"]["payload"]["cardUrl"] = "https://example.invalid/card?token=secret#private"
    payload = {
        "request": {
            "format": "pdf",
            "profile": "full_report",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "theme": theme,
            "print_theme": current_print_theme_snapshot(),
            "options": {"include_appendix": True},
        },
        "surface_projection": {
            "contract_version": 1,
            "token": fixture["surface_token"],
            "entries": entries,
            "exclusions": fixture["expected_exclusions"],
        },
        "sections": sections,
    }
    return _snapshot(payload, section_count=len(sections), surface_token=fixture["surface_token"])


def _snapshot(
    payload: dict,
    *,
    section_count: int,
    surface_token: str = f"sha256:{'a' * 64}",
) -> ExportSnapshot:
    return ExportSnapshot(
        id="exs_ir_fixture",
        schema_version=1,
        canvas_id="cv_ir",
        session_id="s_ir",
        team_id="team_ir",
        workspace_id="ws_ir",
        canvas_owner="u_owner",
        requested_by="u_owner",
        fmt="pdf",
        profile="full_report",
        title="IR fixture",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'b' * 64}",
        surface_contract_version=1,
        surface_token=surface_token,
        parity_contract="full_content_v1",
        payload=payload,
        payload_sha256=f"sha256:{'c' * 64}",
        size_bytes=1024,
        section_count=section_count,
        created_at="2026-07-16T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )


def test_registry_covers_exact_kind_surface_contract() -> None:
    assert len(SURFACE_KIND_POLICIES) == 21
    assert SECTION_ADAPTERS.keys() == SURFACE_KIND_POLICIES.keys()
    assert SECTION_CAPABILITIES.keys() == SURFACE_KIND_POLICIES.keys()


def test_kind_fixture_normalizes_once_with_typed_and_visual_identity() -> None:
    snapshot = _fixture_snapshot()
    export_ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )

    blocks = iter_export_blocks(export_ir.blocks)
    # embed-inline: fixture has 2 report_embedded blocks (sec_chart/sec_image -> sec_insight)
    # moved to host children: 20 top-level, 22 flat (including standalone sec_orphan_chart), bodies/order unchanged.
    assert len(export_ir.blocks) == 20
    assert len(blocks) == 22
    assert {block.source.section_kind for block in blocks} == set(SURFACE_KIND_POLICIES)
    assert sorted(block.order for block in blocks) == list(range(22))
    insight = next(block for block in export_ir.blocks if block.source.section_kind == "insight_card")
    assert [child.source.section_id for child in insight.children] == ["sec_chart", "sec_image"]
    assert export_ir.normalization_warnings == ("interactive_content_not_executed",)

    by_kind = {block.source.section_kind: block for block in blocks}
    assert by_kind["data_table"].block_kind == "table"
    assert by_kind["data_table"].typed_data_sha256 is not None
    assert by_kind["chart"].block_kind == "visual"
    assert by_kind["chart"].typed_data_sha256 is not None
    assert by_kind["chart"].visual_input_sha256 is not None
    assert by_kind["image"].content["artifact_content_hashes"] == {"art_image": f"sha256:{'1' * 64}"}
    assert by_kind["h1"].source_refs == ("call_h1", "tl_h1")
    assert by_kind["datarc_iframe"].content["safe_url"] == ("https://example.invalid/card")
    assert "datarc_iframe" not in by_kind["datarc_iframe"].semantic_tokens
    assert by_kind["kpi_row"].semantic_tokens == ("Revenue", "120")
    assert by_kind["next_list"].semantic_tokens == ("Validate pricing",)
    assert by_kind["sources"].semantic_tokens == ("来源", "[1] 来源")
    assert "Warehouse" not in repr(by_kind["sources"].content)
    assert by_kind["insight_card"].semantic_tokens == ("Growth accelerated",)
    assert by_kind["insight_card"].content == {"title": "Growth accelerated"}
    assert not {
        "sec_chart",
        "sec_image",
        "chart",
        "artifact_id",
    }.intersection(by_kind["insight_card"].semantic_tokens)
    assert "secret" not in json.dumps(export_ir.to_dict(), ensure_ascii=False)


def test_export_ir_affected_copy_obeys_shared_semantic_deny_fixture() -> None:
    fixture = json.loads(_COPY_FIXTURE.read_text(encoding="utf-8"))
    rendered: list[str] = []
    for locale in ("zh-CN", "en-US"):
        base = _fixture_snapshot()
        payload = copy.deepcopy(base.payload)
        payload["request"]["locale"] = locale
        export_ir = normalize_export_snapshot(
            replace(base, locale=locale, payload=payload),
            asset_content_hashes={
                "art_image": f"sha256:{'1' * 64}",
                "art_bg": f"sha256:{'2' * 64}",
            },
        )
        rendered.append(json.dumps(export_ir.to_dict(), ensure_ascii=False))
    surface = "\n".join(rendered).casefold()
    assert [phrase for phrase in fixture["semantic_deny"] if phrase.casefold() in surface] == []


def test_visual_input_hashes_match_shared_page_export_fixture() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    export_ir = normalize_export_snapshot(
        _fixture_snapshot(),
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )
    actual = {
        block.source.section_id: block.visual_input_sha256 for block in iter_export_blocks(export_ir.blocks) if block.visual_input_sha256 is not None
    }
    assert actual == fixture["expected_visual_input_sha256"]


def _v2_warm_neutral_print_theme() -> dict[str, object]:
    """The exact print theme frozen into export jobs created before the v3 recipe."""

    return {
        "schemaVersion": 1,
        "recipeVersion": "bowhead-print-theme-v2",
        "mode": "light",
        "tokens": {
            "page": "#FFFFFF",
            "surface": "#FFFFFF",
            "ink": "#161310",
            "mutedInk": "#56503F",
            "line": "#D9D4CB",
            "accent": "#1754DB",
            "accentInk": "#FFFFFF",
        },
        "fontBundleVersion": "bowhead-system-fonts-v1",
        "checksum": "sha256:80caed8907cc61f778aebe53bebdd0d1ce149a4a9f9c6c57ec9c9bd56417db43",
    }


@pytest.mark.parametrize(
    "print_theme",
    [None, {"schemaVersion": 1}, _v2_warm_neutral_print_theme()],
    ids=["missing", "malformed", "pre_v3_job"],
)
def test_export_ir_rejects_missing_or_malformed_print_theme(print_theme: object) -> None:
    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    if print_theme is None:
        payload["request"].pop("print_theme")
    else:
        payload["request"]["print_theme"] = print_theme

    with pytest.raises(ExportIRError, match="invalid_snapshot_request"):
        normalize_export_snapshot(
            replace(base, payload=payload),
            asset_content_hashes={
                "art_image": f"sha256:{'1' * 64}",
                "art_bg": f"sha256:{'2' * 64}",
            },
        )


def test_visual_input_is_deterministic_and_asset_bytes_are_separate_identity() -> None:
    snapshot = _fixture_snapshot()
    first = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )
    same = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={
            "art_bg": f"sha256:{'2' * 64}",
            "art_image": f"sha256:{'1' * 64}",
        },
    )
    changed = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={
            "art_image": f"sha256:{'9' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )

    def image_hash(export_ir) -> str | None:  # type: ignore[no-untyped-def]
        return next(block.visual_input_sha256 for block in iter_export_blocks(export_ir.blocks) if block.source.section_kind == "image")

    assert image_hash(first) == image_hash(same)
    assert image_hash(first) == image_hash(changed)
    assert first.visual_assets == same.visual_assets
    assert first.visual_assets != changed.visual_assets
    assert first.visual_assets == (
        {
            "artifact_id": "art_bg",
            "content_sha256": f"sha256:{'2' * 64}",
        },
        {
            "artifact_id": "art_image",
            "content_sha256": f"sha256:{'1' * 64}",
        },
    )


def test_export_text_never_leaks_chat_cite_markers() -> None:
    """导出正文里不许出现字面 `[cite:N]`，但 `[N]` 必须留着。

    2026-08-15 116 实测 PDF 正文有一处 `[cite:1]`：canvas 侧已经用 decoration 藏住，
    **导出这条路没有那一层**——`_visible_text` 是 markdown → 导出可见文本的唯一收口，
    补在那里。
    """
    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    narrative = next(s for s in payload["sections"] if s["kind"] == "narrative")
    narrative["payload"]["markdown"] = "全年持平 [cite:1]，其中 7 月最低 9.78亿 [1]。"
    export_ir = normalize_export_snapshot(
        _snapshot(payload, section_count=base.section_count, surface_token=base.surface_token),
        asset_content_hashes={"art_image": f"sha256:{'1' * 64}", "art_bg": f"sha256:{'2' * 64}"},
    )
    block = next(b for b in iter_export_blocks(export_ir.blocks) if b.source.section_kind == "narrative")
    text = " ".join(block.semantic_tokens)

    assert "[cite:" not in text, text
    # Positive control: canonical `[N]` and body must stay byte-exact (otherwise deleting everything could cheat this assertion).
    assert "[1]" in text, text
    assert "全年持平" in text and "9.78亿" in text, text


def test_literal_source_paragraph_preserves_punctuation_without_executing_markup() -> None:
    from bowhead_core.export_pdf import _render_markup

    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    narrative = next(section for section in payload["sections"] if section["kind"] == "narrative")
    literal = "Revenue_A < 100; *provisional* for 2026-01 [2]. <a href='https://example.com'>source</a>"
    narrative["payload"].pop("markdown", None)
    narrative["payload"]["text"] = literal
    export_ir = normalize_export_snapshot(
        _snapshot(payload, section_count=base.section_count, surface_token=base.surface_token),
        asset_content_hashes={"art_image": f"sha256:{'1' * 64}", "art_bg": f"sha256:{'2' * 64}"},
    )
    block = next(block for block in iter_export_blocks(export_ir.blocks) if block.source.section_kind == "narrative")
    assert block.semantic_tokens == (literal,)
    markup = _render_markup(block.semantic_tokens[0], primary=frozenset(), symbol=frozenset())
    assert "&lt;a href=" in markup and "<a href=" not in markup
    assert "Revenue_A &lt; 100; *provisional* for 2026-01 [2]." in markup


def test_insight_card_exports_in_card_order_and_never_bares_confidence() -> None:
    """insight_card 的导出文本必须按**卡片顺序**出，且不许把 confidence 印成裸词。

    2026-08-15 116 dogfood 实测 PDF：一行孤零零的 `high` 打头，后面字段顺序也乱。
    根因是 callout 落进 `_semantic_tokens` 末尾的兜底 `_visible_tokens([content])`，
    它对 dict 走 `sorted(value)` ⇒ 字母序 confidence → impact → narrative → thesis → title，
    而屏幕上 `insight-card.tsx` 是 title → thesis → evidence → narrative → impact。

    ⚠ 仓库 fixture 里的 insight_card **只有 title**，所以这个坑一直没被任何测试碰到
    ——典型「自己的 fixture 只覆盖顺利路径」。这里按线上真实形状补齐字段。
    """
    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    insight = next(section for section in payload["sections"] if section["kind"] == "insight_card")
    insight["payload"].update(
        {
            "title": "标题",
            "thesis": "论点",
            "narrative": "叙述",
            "impact": {"value": "影响"},
            "confidence": "high",
        }
    )
    export_ir = normalize_export_snapshot(
        _snapshot(payload, section_count=base.section_count, surface_token=base.surface_token),
        asset_content_hashes={"art_image": f"sha256:{'1' * 64}", "art_bg": f"sha256:{'2' * 64}"},
    )
    block = next(b for b in iter_export_blocks(export_ir.blocks) if b.source.section_kind == "insight_card")

    # ① confidence excluded from body (screen shows localized badge, not bare word).
    assert "high" not in block.semantic_tokens

    # ② Card order, not alphabetical.
    order = [t for t in block.semantic_tokens if t in {"标题", "论点", "叙述", "影响"}]
    assert order == ["标题", "论点", "叙述", "影响"], block.semantic_tokens


def test_chart_full_rows_replace_preview_for_typed_export_only() -> None:
    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    chart = next(section for section in payload["sections"] if section["kind"] == "chart")
    spec = chart["payload"]["spec"]
    preview_rows = [{"region": f"R{index}", "revenue": index} for index in range(50)]
    full_rows = [{"region": f"R{index}", "revenue": index} for index in range(60)]
    spec["rows"] = preview_rows
    spec["row_count"] = len(full_rows)
    spec["data_artifact_id"] = "art_chart_full"
    snapshot = _snapshot(
        payload,
        section_count=base.section_count,
        surface_token=base.surface_token,
    )
    asset_hashes = {
        "art_image": f"sha256:{'1' * 64}",
        "art_bg": f"sha256:{'2' * 64}",
        "art_chart_full": f"sha256:{'3' * 64}",
    }

    preview = normalize_export_snapshot(
        snapshot,
        asset_content_hashes=asset_hashes,
    )
    hydrated = normalize_export_snapshot(
        snapshot,
        asset_content_hashes=asset_hashes,
        resolved_chart_rows={"art_chart_full": full_rows},
    )
    preview_chart = next(block for block in iter_export_blocks(preview.blocks) if block.source.section_kind == "chart")
    hydrated_chart = next(block for block in iter_export_blocks(hydrated.blocks) if block.source.section_kind == "chart")

    assert hydrated_chart.visual_input_sha256 == (preview_chart.visual_input_sha256)
    assert hydrated_chart.typed_data_sha256 != (preview_chart.typed_data_sha256)
    assert hydrated_chart.content["resolved_chart_rows"] == full_rows


def test_chart_full_rows_reject_declared_count_mismatch() -> None:
    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    chart = next(section for section in payload["sections"] if section["kind"] == "chart")
    spec = chart["payload"]["spec"]
    spec["data_artifact_id"] = "art_chart_full"
    spec["row_count"] = 60
    snapshot = _snapshot(
        payload,
        section_count=base.section_count,
        surface_token=base.surface_token,
    )

    with pytest.raises(
        ExportIRError,
        match="chart_data_row_count_mismatch",
    ):
        normalize_export_snapshot(
            snapshot,
            asset_content_hashes={
                "art_image": f"sha256:{'1' * 64}",
                "art_bg": f"sha256:{'2' * 64}",
                "art_chart_full": f"sha256:{'3' * 64}",
            },
            resolved_chart_rows={
                "art_chart_full": [{"region": f"R{index}", "revenue": index} for index in range(59)],
            },
        )


def test_chart_full_rows_accept_topn_plotted_subset() -> None:
    """spec.row_count is the chart's *plotted* (Top-N / preview) count, while the
    data workbook hydrates the FULL query result, so declared < len(rows) is
    normal and must be accepted. The earlier `!=` invariant wrongly rejected
    real Top-N charts (a real 116 XLSX export failed on a Top-10 category chart
    over 35 rows). Only declared > available is an anomaly (sibling reject test).
    """
    base = _fixture_snapshot()
    payload = copy.deepcopy(base.payload)
    chart = next(section for section in payload["sections"] if section["kind"] == "chart")
    spec = chart["payload"]["spec"]
    spec["data_artifact_id"] = "art_chart_full"
    spec["row_count"] = 10
    snapshot = _snapshot(
        payload,
        section_count=base.section_count,
        surface_token=base.surface_token,
    )
    full_rows = [{"region": f"R{index}", "revenue": index} for index in range(35)]
    export_ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
            "art_chart_full": f"sha256:{'3' * 64}",
        },
        resolved_chart_rows={"art_chart_full": full_rows},
    )
    hydrated_chart = next(block for block in iter_export_blocks(export_ir.blocks) if block.source.section_kind == "chart")
    assert hydrated_chart.content["resolved_chart_rows"] == full_rows


def test_full_capabilities_have_no_exclusion_and_only_interactive_fallbacks() -> None:
    export_ir = normalize_export_snapshot(
        _fixture_snapshot(),
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )
    for target in ("pdf_full", "pptx_full", "xlsx_data"):
        plan = build_capability_plan(
            export_ir,
            target=target,
            include_appendix=True,
        )
        assert not [entry for entry in plan if entry.action == "exclude"]
        fallback_kinds = {entry.source_section_kind for entry in plan if entry.action == "fallback"}
        assert fallback_kinds == {
            "datarc_iframe",
            "html_artifact",
            "external_iframe",
        }
    pptx = build_capability_plan(
        export_ir,
        target="pptx_full",
        include_appendix=True,
    )
    assert {entry.source_section_kind for entry in pptx if entry.action == "appendix"} == {"code_block"}


def test_summary_capability_exclusions_are_explicit_policy() -> None:
    export_ir = normalize_export_snapshot(
        _fixture_snapshot(),
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )
    without_appendix = build_capability_plan(
        export_ir,
        target="pptx_summary",
        include_appendix=False,
    )
    excluded = {entry.source_section_kind for entry in without_appendix if entry.action == "exclude"}
    assert excluded == {
        "data_table",
        "next_list",
        "datarc_iframe",
        "html_artifact",
        "external_iframe",
        "code_block",
        "sources",
    }
    assert all(entry.reason_code == "profile_policy" for entry in without_appendix if entry.action == "exclude")
    with_appendix = build_capability_plan(
        export_ir,
        target="pptx_summary",
        include_appendix=True,
    )
    assert {entry.source_section_kind for entry in with_appendix if entry.action == "appendix"} == {
        "data_table",
        "code_block",
        "sources",
    }


def test_nested_container_preserves_typed_values_and_source_path() -> None:
    surface_token = f"sha256:{'d' * 64}"
    payload = {
        "request": {
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
        },
        "surface_projection": {
            "token": surface_token,
            "entries": [
                {
                    "section_id": "sec_container",
                    "section_rev": 4,
                    "order": 0,
                    "placement": "report_body",
                    "effective_kind": "section",
                }
            ],
        },
        "sections": [
            {
                "id": "sec_container",
                "kind": "section",
                "rev": 4,
                "payload": {
                    "label": "Details",
                    "h2": "Typed rows",
                    "body": [
                        {"id": "nested_title", "kind": "h1", "text": "Nested"},
                        {
                            "id": "nested_table",
                            "kind": "data_table",
                            "columns": [
                                {"key": "amount", "label": "Amount"},
                                {"key": "active", "label": "Active"},
                            ],
                            "rows": [{"amount": 12.5, "active": True}],
                        },
                    ],
                },
            }
        ],
    }
    export_ir = normalize_export_snapshot(_snapshot(payload, section_count=3, surface_token=surface_token))
    container = export_ir.blocks[0]
    assert container.block_kind == "container"
    assert [child.source.section_path for child in container.children] == [
        (0, 0),
        (0, 1),
    ]
    assert all(child.source.section_id == "sec_container" for child in container.children)
    table = container.children[1]
    assert table.content["rows"] == [{"amount": 12.5, "active": True}]
    assert [column["value_type"] for column in table.content["columns"]] == [
        "number",
        "boolean",
    ]
    assert table.typed_data_sha256 is not None


def test_required_visual_asset_and_surface_mismatch_fail_closed() -> None:
    snapshot = _fixture_snapshot()
    with pytest.raises(ExportIRError) as missing:
        normalize_export_snapshot(
            snapshot,
            asset_content_hashes={"art_bg": f"sha256:{'2' * 64}"},
        )
    assert missing.value.code == "required_visual_asset_unresolved"

    corrupted = copy.deepcopy(snapshot.payload)
    corrupted["surface_projection"]["entries"][0]["section_rev"] = 999
    with pytest.raises(ExportIRError) as mismatch:
        normalize_export_snapshot(
            _snapshot(
                corrupted,
                section_count=snapshot.section_count,
                surface_token=snapshot.surface_token,
            ),
            asset_content_hashes={
                "art_image": f"sha256:{'1' * 64}",
                "art_bg": f"sha256:{'2' * 64}",
            },
        )
    assert mismatch.value.code == "snapshot_surface_accounting_mismatch"

    missing_id_payload = copy.deepcopy(snapshot.payload)
    image = next(section for section in missing_id_payload["sections"] if section["kind"] == "image")
    image["payload"].pop("artifact_id")
    with pytest.raises(ExportIRError) as missing_id:
        normalize_export_snapshot(
            _snapshot(
                missing_id_payload,
                section_count=snapshot.section_count,
                surface_token=snapshot.surface_token,
            ),
            asset_content_hashes={
                "art_bg": f"sha256:{'2' * 64}",
            },
        )
    assert missing_id.value.code == "required_visual_asset_unresolved"


@pytest.mark.parametrize(
    ("locale", "message"),
    [
        ("de-DE", "Bitte öffnen Sie die interaktiven Inhalte im Online-Bericht."),
        ("fr-FR", "Consultez le contenu interactif dans le rapport en ligne."),
    ],
)
def test_interactive_export_fallback_uses_snapshot_locale(locale: str, message: str) -> None:
    snapshot = replace(_fixture_snapshot(), locale=locale)
    export_ir = normalize_export_snapshot(snapshot, asset_content_hashes={"art_image": f"sha256:{'1' * 64}", "art_bg": f"sha256:{'2' * 64}"})
    block = next(block for block in iter_export_blocks(export_ir.blocks) if block.source.section_kind == "datarc_iframe")
    assert block.content["message"] == message
    assert block.content["safe_url"] == "https://example.invalid/card"
    assert block.source.section_kind == "datarc_iframe"


# ── Exports follow the exporting user's legend-hidden series (user decision 2026-10-01, option b) ─────────────────


def _visual_hashes(snapshot: ExportSnapshot) -> dict[str, tuple[str | None, dict]]:
    ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={"art_image": f"sha256:{'1' * 64}", "art_bg": f"sha256:{'2' * 64}"},
    )
    return {b.source.section_id: (b.visual_input_sha256, b.content) for b in iter_export_blocks(ir.blocks) if b.visual_input_sha256}


def _with_hidden_series(snapshot: ExportSnapshot, hidden: dict[str, list[str]]) -> ExportSnapshot:
    payload = copy.deepcopy(snapshot.payload)
    payload["request"]["options"] = {**payload["request"].get("options", {}), "chart_hidden_series": hidden}
    return replace(snapshot, payload=payload)


def test_hidden_series_reach_the_chart_payload_and_its_visual_identity() -> None:
    base = _fixture_snapshot()
    before = _visual_hashes(base)
    after = _visual_hashes(_with_hidden_series(base, {"sec_chart": ["amount"]}))
    assert after["sec_chart"][1]["payload"]["hidden_series"] == ["amount"]
    # A different hidden set is a different image: the cached receipt of the unhidden chart must not be reused.
    assert after["sec_chart"][0] != before["sec_chart"][0]
    # Every other visual block — including the other chart — keeps its identity byte-for-byte.
    for section_id, (sha, _content) in before.items():
        if section_id != "sec_chart":
            assert after[section_id][0] == sha, section_id


def test_hidden_series_for_non_chart_sections_are_ignored() -> None:
    base = _fixture_snapshot()
    before = _visual_hashes(base)
    after = _visual_hashes(_with_hidden_series(base, {"sec_design": ["x"], "sec_missing": ["y"]}))
    assert {k: v[0] for k, v in after.items()} == {k: v[0] for k, v in before.items()}


def test_a_persisted_hidden_series_never_reaches_the_export() -> None:
    """Section PATCHes accept arbitrary payload keys. A `hidden_series` stored on the section is dropped: only the
    exporting user's request option may hide series."""
    base = _fixture_snapshot()
    clean = _visual_hashes(base)
    payload = copy.deepcopy(base.payload)
    for section in payload["sections"]:
        if section["id"] == "sec_chart":
            section["payload"]["hidden_series"] = ["amount"]
    stored = _visual_hashes(replace(base, payload=payload))
    assert "hidden_series" not in stored["sec_chart"][1]["payload"]
    assert stored["sec_chart"][0] == clean["sec_chart"][0]
    # The request option still applies on top of a payload that tried to set its own.
    requested = _visual_hashes(_with_hidden_series(replace(base, payload=payload), {"sec_chart": ["other"]}))
    assert requested["sec_chart"][1]["payload"]["hidden_series"] == ["other"]


def test_chart_display_precision_changes_visual_identity_but_not_typed_observations():
    snapshot = _fixture_snapshot()
    assets = {"art_image": f"sha256:{'1' * 64}", "art_bg": f"sha256:{'2' * 64}"}
    original = normalize_export_snapshot(snapshot, asset_content_hashes=assets)
    section = next(s for s in snapshot.payload["sections"] if s["id"] == "sec_chart")
    column = next(c for c in section["payload"]["spec"]["columns"] if c["type"] == "numeric")
    column.update(currency_code="USD", display={"decimal_places": 3, "decimal_mode": "fixed"})
    changed = normalize_export_snapshot(snapshot, asset_content_hashes=assets)
    before = next(b for b in iter_export_blocks(original.blocks) if b.source.section_id == "sec_chart")
    after = next(b for b in iter_export_blocks(changed.blocks) if b.source.section_id == "sec_chart")
    assert before.visual_input_sha256 != after.visual_input_sha256
    assert before.typed_data_sha256 == after.typed_data_sha256
    assert after.content["payload"]["spec"]["columns"] == section["payload"]["spec"]["columns"]
