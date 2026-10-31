"""Canvas ExportIR v1 normalization and capability planning.

The IR is format-neutral: it preserves semantic text, typed values, source
paths, and visual input identity without introducing pages, slides, or cells.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from bowhead_core.canvas_export_projection import SURFACE_KIND_POLICIES
from bowhead_core.chart_style_export import (
    CHART_EXPORT_SURFACE_CONTRACT_VERSION,
    ChartStyleExportError,
    parse_chart_style_export_theme,
    parse_frozen_chart_presentation_v2,
)
from bowhead_core.citation_marks import strip_cite_markers
from bowhead_core.embedded_data_provenance import project_embedded_data_provenance
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_capabilities import (
    CapabilityAction,
    ExportTarget,
    capability_for_kind,
)
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.export_visual_identity import (
    EXPORT_VISUAL_RENDERER_VERSION,
    build_visual_input_identity,
    compute_visual_theme_hash,
    sha256_canonical,
    visual_typed_data,
)
from bowhead_core.presentation_locale import resolve_presentation_locale
from bowhead_core.provenance import strip_provenance_tags
from bowhead_core.source_linkage_copy import (
    format_embedded_data_provenance,
    generic_source_type_copy,
    source_list_copy,
)

EXPORT_IR_SCHEMA_VERSION = 1

ExportBlockKind = Literal[
    "heading",
    "paragraph",
    "kpi_group",
    "table",
    "visual",
    "callout",
    "recommendations",
    "code",
    "source_list",
    "interactive_fallback",
    "container",
]


class ExportIRError(CodedError):
    """Stable normalization failure; no renderer may consume a partial IR."""


# Same literal as canvas_store._INLINE_FIGURE_RE / report_writer_loop._FIGURE_REF_RE:
# [figure:id] is a cross-module body-anchor syntax (embed-inline tech-design F6).
_FIGURE_ANCHOR_RE = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")

# Batch 1 2.1 (chart-table-ux): caption attachment; this and FE
# `lib/canvas-pm-adapter/extensions/figure-caption.ts` CAPTION_RE are **duplicated cross-language
# constants**: change both together. test_figure_caption_parity.py enforces this
# (extract TS regex literal + run identical samples through both engines).
# (\*\*)? supports Markdown originals ("**图1**：") and plain text.
# ⚠ `[0-9]` deliberately narrows; never use `\d`: Python matches all Unicode decimal digits
# (including full-width ０-９), JS only ASCII, so identical literals differ across engines: 648 of 986 samples
# diverged in review. Consequence: `图１：` remains ordinary canvas text with prefix shown,
# but exports attach it as a caption and strip the prefix.
FIGURE_CAPTION_PREFIX_RE = re.compile(r"^(\*\*)?图[ \t]?[0-9]+(\*\*)?[ \t]*[:：][ \t]*")

# `[cite:N]` regex belongs to citation_marks (single citation-marker authority, FE parity-tested).


def split_figure_caption(trailing_markdown: str) -> tuple[str | None, str]:
    """锚后紧邻段命中「图N：」前缀 → (剥前缀的图注, 余下 markdown)；不中 → (None, 原文)。

    紧邻 = 只允许空白相隔；首个非空段（到空行为止）整段作为图注。系统从没给图
    编过号，正文却写着「图1/图2」——前缀是对不存在编号的引用，剥掉；图注句本身
    是结论，保留并随图渲染（不再孤零零独立成段——真 prod PDF「图注还在、图没了」
    的另一半病）。
    """
    stripped = trailing_markdown.lstrip("\n")
    first, _sep, rest = stripped.partition("\n\n")
    match = FIGURE_CAPTION_PREFIX_RE.match(first.strip())
    if not match:
        return None, trailing_markdown
    caption = first.strip()[match.end() :].strip()
    if not caption:
        return None, trailing_markdown
    return caption, rest


@dataclass(frozen=True, slots=True)
class ExportIRSource:
    section_id: str
    section_rev: int
    section_kind: str
    section_path: tuple[int, ...]
    surface_placement: str


@dataclass(frozen=True, slots=True)
class ExportIRBlock:
    block_key: str
    block_kind: ExportBlockKind
    order: int
    source: ExportIRSource
    source_refs: tuple[str, ...]
    content: dict[str, Any]
    semantic_tokens: tuple[str, ...]
    semantic_content_sha256: str
    typed_data_sha256: str | None
    visual_input_sha256: str | None
    warnings: tuple[str, ...]
    children: tuple[ExportIRBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportIRDocument:
    title: str
    locale: str
    timezone: str
    profile: str


@dataclass(frozen=True, slots=True)
class ExportIR:
    schema_version: int
    snapshot_id: str
    input_sha256: str
    surface_token: str
    document: ExportIRDocument
    blocks: tuple[ExportIRBlock, ...]
    visual_assets: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]
    normalization_warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CapabilityPlanEntry:
    block_key: str
    source_section_id: str
    source_section_kind: str
    action: CapabilityAction
    reason_code: str | None
    is_leaf: bool


@dataclass(frozen=True, slots=True)
class _AdapterContext:
    top_section_id: str
    top_section_rev: int
    kind: str
    payload: dict[str, Any]
    source_refs: tuple[str, ...]
    block_key: str
    order: int
    section_path: tuple[int, ...]
    surface_placement: str
    locale: str
    theme_hash: str
    renderer_version: str
    asset_content_hashes: dict[str, str]
    resolved_chart_rows: dict[str, list[dict[str, Any]]]
    renderer_contract_version: int
    frozen_chart_presentation: dict[str, Any] | None


class SectionAdapter(Protocol):
    def __call__(self, context: _AdapterContext) -> ExportIRBlock: ...


SECTION_ADAPTERS: dict[str, SectionAdapter] = {}


def _chart_hidden_series(options: Any) -> dict[str, list[str]]:
    """``options.chart_hidden_series`` from the snapshot request (validated and normalized by the export route)."""
    value = options.get("chart_hidden_series") if isinstance(options, dict) else None
    if not isinstance(value, dict):
        return {}
    return {
        section_id: list(keys)
        for section_id, keys in value.items()
        if isinstance(section_id, str) and isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys)
    }


def normalize_export_snapshot(
    snapshot: ExportSnapshot,
    *,
    asset_content_hashes: dict[str, str] | None = None,
    resolved_chart_rows: dict[str, list[dict[str, Any]]] | None = None,
    renderer_version: str = EXPORT_VISUAL_RENDERER_VERSION,
) -> ExportIR:
    if snapshot.schema_version != 1:
        raise ExportIRError("unsupported_snapshot_schema")
    payload = snapshot.payload
    request = _dict(payload.get("request"), code="invalid_snapshot_request")
    theme = _dict(request.get("theme"), code="invalid_snapshot_request")
    print_theme = _dict(request.get("print_theme"), code="invalid_snapshot_request")
    try:
        # V1 snapshots predate the strict chart-style envelope and must remain
        # readable byte-for-byte (some durable jobs contain {"kind":"default"}).
        # V2 is server-frozen and therefore safe to validate as the complete
        # export contract before any renderer sees it.
        if theme.get("schemaVersion") == 2:
            theme = parse_chart_style_export_theme(theme)
        theme_hash = compute_visual_theme_hash(
            theme=theme,
            print_theme=print_theme,
        )
    except (ChartStyleExportError, ValueError) as exc:
        raise ExportIRError("invalid_snapshot_request") from exc
    renderer_contract_version = 2 if theme.get("schemaVersion") == 2 else 1
    sections = _list_of_dicts(payload.get("sections"), code="invalid_snapshot_sections")
    surface = _dict(payload.get("surface_projection"), code="invalid_snapshot_surface")
    entries = _list_of_dicts(surface.get("entries"), code="invalid_snapshot_surface")
    _validate_surface_sections(snapshot, entries=entries, sections=sections)

    hidden_series = _chart_hidden_series(request.get("options"))
    hashes = dict(asset_content_hashes or {})
    blocks: list[ExportIRBlock] = []
    normalization_warnings: list[str] = []
    for section, entry in zip(sections, entries, strict=True):
        section_id = _required_str(section, "id", code="invalid_snapshot_sections")
        kind = _required_str(section, "kind", code="invalid_snapshot_sections")
        section_payload = _dict(section.get("payload"), code="invalid_snapshot_sections")
        if kind == "chart":
            # The exporting user's legend-hidden series (per-viewer screen state from the export request) — and only
            # that. A `hidden_series` persisted on the section (section PATCHes accept arbitrary keys) is dropped, so
            # stored data can never hide series in someone's export. Part of the chart's visual payload, hence of its
            # visual identity: a cached receipt can never serve another hidden set.
            section_payload = {key: value for key, value in section_payload.items() if key != "hidden_series"}
            if section_id in hidden_series:
                section_payload["hidden_series"] = hidden_series[section_id]
        adapter = SECTION_ADAPTERS.get(kind)
        if adapter is None:
            normalization_warnings.append("unknown_section_kind")
            adapter = _adapt_unknown
        context = _AdapterContext(
            top_section_id=section_id,
            top_section_rev=_required_int(section, "rev", code="invalid_snapshot_sections"),
            kind=kind,
            payload=section_payload,
            source_refs=_source_refs(section.get("source_refs")),
            block_key=section_id,
            order=_required_int(entry, "order", code="invalid_snapshot_surface"),
            section_path=(_required_int(entry, "order", code="invalid_snapshot_surface"),),
            surface_placement=_required_str(
                entry,
                "placement",
                code="invalid_snapshot_surface",
            ),
            locale=snapshot.locale,
            theme_hash=theme_hash,
            renderer_version=renderer_version,
            asset_content_hashes=hashes,
            resolved_chart_rows=dict(resolved_chart_rows or {}),
            renderer_contract_version=renderer_contract_version,
            frozen_chart_presentation=_frozen_chart_presentation(
                section,
                kind=kind,
                locale=snapshot.locale,
                renderer_contract_version=renderer_contract_version,
            ),
        )
        block = adapter(context)
        blocks.append(block)
        normalization_warnings.extend(block.warnings)

    # embed-inline (tech-design §1): move report_embedded chart blocks into host-body anchor positions.
    # UI already renders inline and hides standalone blocks; IR lost parent links, piling PDF/PPTX
    # charts before body with literal anchors remaining. Move, never copy: preserve block_key/visual sha/receipt marker
    # and evidence/capability-plan/asset-collection invariants (F7).
    blocks, inline_warnings = _inline_embedded_visuals(blocks, entries)
    normalization_warnings.extend(inline_warnings)

    return ExportIR(
        schema_version=EXPORT_IR_SCHEMA_VERSION,
        snapshot_id=snapshot.id,
        input_sha256=snapshot.payload_sha256,
        surface_token=snapshot.surface_token,
        document=ExportIRDocument(
            title=snapshot.title,
            locale=snapshot.locale,
            timezone=snapshot.timezone,
            profile=snapshot.profile,
        ),
        blocks=tuple(blocks),
        visual_assets=_collect_visual_assets(blocks),
        sources=_collect_sources(blocks),
        normalization_warnings=tuple(sorted(set(normalization_warnings))),
    )


def _inline_embedded_visuals(
    blocks: list[ExportIRBlock],
    entries: list[dict[str, Any]],
) -> tuple[list[ExportIRBlock], list[str]]:
    """embed-inline pass: 按 [figure:id] 锚把 embedded 图块内联为宿主 children.

    - blocks 与 entries 按组装序平行 (zip 产物)。
    - 悬空锚 (引用不在本宿主 embedded 集) → 剥 token + warning, 绝不字面渲染。
    - 有 embedded 关系但正文无锚 → 追加宿主 children 尾部 + warning (宁多显不静默丢)。
    - 无任何 embedded 关系 → 原样返回。
    """
    warnings: list[str] = []
    embedded_indexes_by_parent: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.get("placement") != "report_embedded":
            continue
        parent = entry.get("parent_section_id")
        if isinstance(parent, str) and parent and index < len(blocks):
            embedded_indexes_by_parent.setdefault(parent, []).append(index)
    if not embedded_indexes_by_parent:
        return blocks, warnings

    moved: set[int] = set()
    result: list[ExportIRBlock] = []
    for index, block in enumerate(blocks):
        if index in moved:
            continue
        child_indexes = embedded_indexes_by_parent.get(block.source.section_id)
        if not child_indexes:
            result.append(block)
            continue
        embedded_by_id = {blocks[i].source.section_id: i for i in child_indexes}
        used_ids: set[str] = set()
        new_children: list[ExportIRBlock] = []
        markdown = block.content.get("markdown")
        host = block
        anchors_expected = block.block_kind == "paragraph" and isinstance(markdown, str)
        if anchors_expected:
            # split result = [text0, id1, text1, id2, text2, ...].
            parts = _FIGURE_ANCHOR_RE.split(markdown)
            head_text = parts[0]
            segment_number = 0
            for anchor_position in range(1, len(parts), 2):
                anchor_id = parts[anchor_position]
                trailing_text = parts[anchor_position + 1]
                child_index = embedded_by_id.get(anchor_id)
                figure_inlined = child_index is not None and anchor_id not in used_ids
                if figure_inlined:
                    used_ids.add(anchor_id)
                    new_children.append(blocks[child_index])
                else:
                    warnings.append("dangling_figure_anchor")
                # Batch 1 2.1: adjacent post-anchor paragraph matching "图N：" becomes figure caption
                # (strip prefix + mark role; render immediately after chart), only if chart actually inlined.
                # A dangling anchor's "caption" has no figure; leave paragraph unchanged.
                if figure_inlined:
                    caption, trailing_text = split_figure_caption(trailing_text)
                    if caption is not None:
                        segment_number += 1
                        new_children.append(
                            _figure_caption_block(
                                host=block,
                                caption_markdown=caption,
                                caption_for=blocks[child_index].block_key,
                                segment_number=segment_number,
                            )
                        )
                    # The report gate's persisted qualification belongs to this
                    # figure, including reports created before this renderer.
                    from bowhead_core.report_final_gate import STALE_NOTE

                    first, _, rest = trailing_text.lstrip().partition("\n\n")
                    if first.strip() == STALE_NOTE:
                        segment_number += 1
                        new_children.append(
                            _figure_caption_block(
                                host=block,
                                caption_markdown=first.strip(),
                                caption_for=blocks[child_index].block_key,
                                segment_number=segment_number,
                            )
                        )
                        trailing_text = rest
                if trailing_text.strip():
                    segment_number += 1
                    new_children.append(
                        _paragraph_segment_block(
                            host=block,
                            markdown=trailing_text,
                            segment_number=segment_number,
                        )
                    )
            host = _rebuild_paragraph_block(block, markdown=head_text)
        # Unanchored embedded child blocks append to host children, never silently drop. Anomaly only for
        # Markdown-paragraph hosts (anchors expected); insight_card and other evidence-chain hosts
        # naturally lack anchors, so tail attachment is intended without warning.
        for child_index in child_indexes:
            child_id = blocks[child_index].source.section_id
            if child_id not in used_ids:
                if anchors_expected:
                    warnings.append("unanchored_embedded_visual")
                new_children.append(blocks[child_index])
        moved.update(child_indexes)
        result.append(replace(host, children=host.children + tuple(new_children)))
    return result, warnings


def _rebuild_paragraph_block(block: ExportIRBlock, *, markdown: str) -> ExportIRBlock:
    content = {**block.content, "markdown": markdown}
    semantic_tokens = _semantic_tokens(block_kind=block.block_kind, content=content)
    return replace(
        block,
        content=content,
        semantic_tokens=semantic_tokens,
        semantic_content_sha256=_sha256(semantic_tokens),
    )


def _figure_caption_block(
    *,
    host: ExportIRBlock,
    caption_markdown: str,
    caption_for: str,
    segment_number: int,
) -> ExportIRBlock:
    """批1 2.1：吸附后的图注块——paragraph 兼容渲染（不认识 role 的渲染器照常
    出段落，前缀已剥），认识 role 的渲染器可按 figcaption 版式收紧。"""
    content = {"markdown": caption_markdown, "role": "figure_caption", "caption_for": caption_for}
    semantic_tokens = _semantic_tokens(block_kind="paragraph", content=content)
    return ExportIRBlock(
        block_key=f"{host.block_key}#seg{segment_number}",
        block_kind="paragraph",
        order=host.order,
        source=replace(host.source, section_path=(*host.source.section_path, segment_number)),
        source_refs=host.source_refs,
        content=content,
        semantic_tokens=semantic_tokens,
        semantic_content_sha256=_sha256(semantic_tokens),
        typed_data_sha256=None,
        visual_input_sha256=None,
        warnings=(),
    )


def _paragraph_segment_block(
    *,
    host: ExportIRBlock,
    markdown: str,
    segment_number: int,
) -> ExportIRBlock:
    content = {"markdown": markdown}
    semantic_tokens = _semantic_tokens(block_kind="paragraph", content=content)
    return ExportIRBlock(
        block_key=f"{host.block_key}#seg{segment_number}",
        block_kind="paragraph",
        order=host.order,
        source=replace(host.source, section_path=(*host.source.section_path, segment_number)),
        source_refs=host.source_refs,
        content=content,
        semantic_tokens=semantic_tokens,
        semantic_content_sha256=_sha256(semantic_tokens),
        typed_data_sha256=None,
        visual_input_sha256=None,
        warnings=(),
    )


def build_capability_plan(
    export_ir: ExportIR,
    *,
    target: ExportTarget,
    include_appendix: bool,
) -> tuple[CapabilityPlanEntry, ...]:
    planned: list[CapabilityPlanEntry] = []
    for block in iter_export_blocks(export_ir.blocks):
        action = capability_for_kind(
            block.source.section_kind,
            target,
            include_appendix=include_appendix,
        )
        if action == "fallback":
            reason_code = (
                "interaction_fallback"
                if block.source.section_kind in SECTION_ADAPTERS
                and SURFACE_KIND_POLICIES[block.source.section_kind].parity_requirement == "interaction_fallback"
                else "unknown_section_kind"
            )
        elif action == "exclude":
            reason_code = "profile_policy"
        elif action == "appendix":
            reason_code = "appendix_policy"
        elif action == "summary_candidate":
            reason_code = "summary_selection_pending"
        else:
            reason_code = None
        planned.append(
            CapabilityPlanEntry(
                block_key=block.block_key,
                source_section_id=block.source.section_id,
                source_section_kind=block.source.section_kind,
                action=action,
                reason_code=reason_code,
                is_leaf=not block.children,
            )
        )
    return tuple(planned)


def iter_export_blocks(
    blocks: tuple[ExportIRBlock, ...],
) -> tuple[ExportIRBlock, ...]:
    flattened: list[ExportIRBlock] = []
    for block in blocks:
        flattened.append(block)
        flattened.extend(iter_export_blocks(block.children))
    return tuple(flattened)


def _frozen_chart_presentation(
    section: dict[str, Any],
    *,
    kind: str,
    locale: str,
    renderer_contract_version: int,
) -> dict[str, Any] | None:
    payload = section.get("payload", section)
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if kind == "chart" and isinstance(spec, dict) and spec.get("views"):
        return None  # Child charts own their frozen presentations.
    value = section.get("frozen_chart_presentation")
    if kind != "chart":
        if value is not None:
            raise ExportIRError("invalid_frozen_chart_presentation")
        return None
    if renderer_contract_version == 1:
        if value is not None:
            raise ExportIRError("invalid_frozen_chart_presentation")
        return None
    try:
        return parse_frozen_chart_presentation_v2(value, locale=locale)
    except ChartStyleExportError as exc:
        raise ExportIRError("invalid_frozen_chart_presentation") from exc


def _adapt_heading(context: _AdapterContext, *, level: int) -> ExportIRBlock:
    text = context.payload.get("text")
    if not isinstance(text, str):
        text = context.payload.get("markdown")
    return _make_block(
        context,
        block_kind="heading",
        content={"level": level, "text": text if isinstance(text, str) else ""},
    )


def _adapt_paragraph(context: _AdapterContext) -> ExportIRBlock:
    markdown = context.payload.get("markdown")
    if not isinstance(markdown, str):
        text = context.payload.get("text")
        return _make_block(context, block_kind="paragraph", content={"text": text if isinstance(text, str) else ""})
    return _make_block(
        context,
        block_kind="paragraph",
        content={"markdown": markdown if isinstance(markdown, str) else ""},
    )


def _adapt_meta(context: _AdapterContext) -> ExportIRBlock:
    pills = context.payload.get("pills")
    return _make_block(
        context,
        block_kind="paragraph",
        content={"pills": pills if isinstance(pills, list) else []},
    )


def _adapt_kpi(context: _AdapterContext) -> ExportIRBlock:
    cells = context.payload.get("cells")
    typed = cells if isinstance(cells, list) else []
    return _make_block(
        context,
        block_kind="kpi_group",
        content={"cells": typed},
        typed_data=_kpi_visible_data(typed),
    )


def _adapt_table(context: _AdapterContext) -> ExportIRBlock:
    raw_rows = context.payload.get("rows")
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    columns = _normalize_columns(context.payload.get("columns"), rows)
    total_row = context.payload.get("total_row", context.payload.get("totalRow"))
    meta = context.payload.get("meta")
    artifact_id = meta.get("artifact_id") if isinstance(meta, dict) else None
    content: dict[str, Any] = {
        "title": context.payload.get("caption"),
        "subtitle": context.payload.get("sub_caption", context.payload.get("subCaption")),
        "columns": columns,
        "rows": rows,
        "total_row": total_row if isinstance(total_row, dict) else None,
        "provenance": {
            "artifact_id": artifact_id if isinstance(artifact_id, str) else None,
            "row_count": len(rows),
        },
    }
    return _make_block(
        context,
        block_kind="table",
        content=content,
        typed_data={"columns": columns, "rows": rows, "total_row": content["total_row"]},
    )


def _adapt_visual(context: _AdapterContext) -> ExportIRBlock:
    spec = context.payload.get("spec")
    if context.kind == "chart" and isinstance(spec, dict) and "presentation_plan" in spec:
        from bowhead_core.chart.guided_reading import validate_guides
        from bowhead_core.chart.presentation_plan import validate_projected_notes_plan

        plan = spec["presentation_plan"]
        try:
            validate_projected_notes_plan(plan, static=True)
            validate_guides(plan, spec, projected=True)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ExportIRError("invalid_chart_plan", section_id=context.top_section_id) from exc
        child_spec = {key: value for key, value in spec.items() if key != "presentation_plan"}
        from bowhead_core.chart.prepared_frames import static_frame_views

        if spec.get("views"):
            child_spec["views"] = static_frame_views(spec, locale=context.locale)
            from bowhead_core.chart.annotations import annotation_views

            child_spec["views"] = annotation_views({**spec, "views": child_spec["views"]})
            from bowhead_core.chart.contextual_reading import contextual_views

            child_spec["views"] = contextual_views({**spec, "views": child_spec["views"]})
            if plan.get("analytical_views"):
                from bowhead_core.chart.analytical import materialize_analytical

                child_spec["views"] = materialize_analytical({**spec, "views": child_spec["views"]})["views"]
        if plan.get("scale_groups"):
            from bowhead_core.chart.shared_scales import domains_for_view

            child_spec["views"] = [
                {**view, "spec": {**view["spec"], "_export_axis_domains": domains_for_view(plan["scale_groups"], view["id"])}}
                for view in child_spec["views"]
            ]
        visual = {"kind": "chart", **context.payload, "spec": child_spec, "source_refs": list(context.source_refs)}
        if context.frozen_chart_presentation is not None:
            visual["frozen_chart_presentation"] = context.frozen_chart_presentation
        labels = {
            column["key"]: column["header"]
            for chart in [spec, *(view["spec"] for view in spec.get("views", []))]
            for column in chart.get("columns", [])
        }
        body = [visual]
        if plan.get("interactions") and plan["dataset"]["availability"] != "available":
            body.append(
                {
                    "kind": "narrative",
                    "markdown": "原数据已变化或当前不可访问；此处保留已保存的观测，联动不可用。"
                    if context.locale.lower().startswith("zh")
                    else "The original dataset has changed or is unavailable. Saved observations remain readable; linked exploration is unavailable.",
                }
            )
        unavailable = (
            "说明原文已变化或当前不可访问" if context.locale.lower().startswith("zh") else "The original note has changed or is currently unavailable"
        )
        for index, step in enumerate(plan.get("guide_steps", []), start=1):
            if not step["title"]:
                continue
            view = next(view for view in spec["views"] if view["id"] == step["view_id"])
            text = f"{index}. {step['title']}" + (f" · {view['spec']['title']}" if view["spec"].get("title") else "")
            body.append({"kind": "narrative", "text": text})
        for note in plan["evidence_notes"]:
            # Literal evidence is plain content, never executable Markdown/HTML.
            text = " · ".join(labels.get(key, key) for key in note["measure_keys"])
            cited = [
                item
                for item in plan.get("annotations", [])
                if item.get("availability") == "available" and note["id"] in item.get("note_ids", []) and item.get("label")
            ]
            if cited:
                prefix = {"zh": "图中备注", "de": "Anmerkung", "fr": "Annotation"}.get(context.locale[:2].lower(), "Annotation")
                text += "\n\n" + prefix + ": " + " · ".join(item["label"] for item in cited)
            supplied = next((item for item in cited if item.get("role") in {"event", "coverage"}), None)
            if supplied:
                # The validated relation exactly matches the source's text and dates.
                text += "\n\n" + (str(supplied["tuple"][0]) if supplied["role"] == "event" else f"{supplied['lower']} – {supplied['upper']}")
            else:
                text += "\n\n" + (note["excerpt"] if note["availability"] == "available" else unavailable)
            if note.get("locator"):
                text += "\n\n" + note["locator"]
            token = note["source_ref"]["source_ref"]
            text += "\n\n[" + token.split(":")[-1] + "]"
            body.append({"kind": "narrative", "text": text, "source_refs": [token]})
        return _adapt_container(replace(context, payload={"body": body}))
    if context.kind == "chart" and isinstance(spec, dict) and spec.get("views"):
        body = []
        for view in spec["views"]:
            child = dict(view)
            child["spec"] = {**view["spec"], "source_meta": spec.get("source_meta")}
            child["source_refs"] = list(context.source_refs)
            body.append(child)
        return _adapt_container(replace(context, payload={"h2": spec.get("title"), "body": body}))
    if context.kind == "chart" and isinstance(spec, dict) and context.frozen_chart_presentation is None:
        from bowhead_core.chart.analytical import static_analytical_panels

        panels = static_analytical_panels(spec, locale=context.locale)
        if panels:
            return _adapt_container(replace(context, payload={"body": [{**panel, "source_refs": list(context.source_refs)} for panel in panels]}))
    artifact_ids = _visual_artifact_ids(
        context.kind,
        context.payload,
        section_rev=context.top_section_rev,
    )
    required_ids = _required_visual_artifact_ids(
        context.kind,
        context.payload,
        section_rev=context.top_section_rev,
    )
    if context.kind == "image" and not required_ids:
        raise ExportIRError(
            "required_visual_asset_unresolved",
            section_id=context.top_section_id,
            roles=1,
        )
    if context.kind == "design" and not required_ids:
        raise ExportIRError(
            "required_visual_asset_unresolved",
            section_id=context.top_section_id,
            roles=1,
        )
    missing = sorted(artifact_id for artifact_id in required_ids if artifact_id not in context.asset_content_hashes)
    if missing:
        raise ExportIRError(
            "required_visual_asset_unresolved",
            section_id=context.top_section_id,
            roles=len(missing),
        )
    artifact_hashes = {artifact_id: context.asset_content_hashes.get(artifact_id) for artifact_id in artifact_ids}
    typed_data = visual_typed_data(context.kind, context.payload)
    content: dict[str, Any] = {
        "visual_kind": context.kind,
        "payload": context.payload,
        "artifact_ids": artifact_ids,
        "artifact_content_hashes": artifact_hashes,
    }
    if context.frozen_chart_presentation is not None:
        content["frozen_chart_presentation"] = context.frozen_chart_presentation
    if context.kind == "chart":
        spec = context.payload.get("spec")
        artifact_id = spec.get("data_artifact_id") if isinstance(spec, dict) else None
        if isinstance(spec, dict) and isinstance(artifact_id, str) and artifact_id in context.resolved_chart_rows:
            rows = [dict(row) for row in context.resolved_chart_rows[artifact_id]]
            declared_count = spec.get("row_count")
            # spec.row_count is the chart's *plotted* row count (often a Top-N /
            # preview subset); the resolved data workbook rows are the FULL query
            # result. declared < len(rows) is therefore normal and correct -- only
            # a declared count that EXCEEDS the available rows is a real anomaly.
            if isinstance(declared_count, int) and not isinstance(declared_count, bool) and declared_count > len(rows):
                raise ExportIRError(
                    "chart_data_row_count_mismatch",
                    section_id=context.top_section_id,
                )
            content["resolved_chart_rows"] = rows
            typed_data = rows
    visual_input = build_visual_input_identity(
        section_kind=context.kind,
        payload=context.payload,
        locale=context.locale,
        theme_hash=context.theme_hash,
        renderer_version=context.renderer_version,
        frozen_chart_presentation=context.frozen_chart_presentation,
        export_surface_contract_version=(CHART_EXPORT_SURFACE_CONTRACT_VERSION if context.renderer_contract_version == 2 else None),
    )
    return _make_block(
        context,
        block_kind="visual",
        content=content,
        typed_data=typed_data,
        visual_input=visual_input,
    )


def _adapt_callout(context: _AdapterContext) -> ExportIRBlock:
    if context.kind == "insight_card":
        evidence = context.payload.get("evidence")
        visible_evidence: list[dict[str, Any]] = []
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                item_content = {key: item[key] for key in ("caption", "subcaption", "callout_text") if key in item}
                if item_content:
                    visible_evidence.append(item_content)
        content = {
            key: context.payload[key]
            for key in (
                "title",
                "thesis",
                "narrative",
                "impact",
                "confidence",
            )
            if key in context.payload
        }
        if visible_evidence:
            content["evidence"] = visible_evidence
    else:
        content = {key: context.payload[key] for key in ("label", "markdown") if key in context.payload}
    return _make_block(
        context,
        block_kind="callout",
        content=content,
    )


def _adapt_recommendations(context: _AdapterContext) -> ExportIRBlock:
    items = context.payload.get("items")
    return _make_block(
        context,
        block_kind="recommendations",
        content={"items": items if isinstance(items, list) else []},
    )


def _adapt_code(context: _AdapterContext) -> ExportIRBlock:
    content = {
        key: context.payload[key]
        for key in (
            "lang",
            "code",
            "sql",
            "stdout_preview",
            "summary_payload",
            "input_artifact_ids",
            "output_artifact_ids",
        )
        if key in context.payload
    }
    if context.kind == "code_block" and "sql" not in content:
        content["sql"] = ""
    return _make_block(context, block_kind="code", content=content)


def _adapt_sources(context: _AdapterContext) -> ExportIRBlock:
    raw_entries = context.payload.get("entries")
    heading, _ = source_list_copy(context.locale)
    entries: list[dict[str, Any]] = []
    if isinstance(raw_entries, list):
        for position, entry in enumerate(raw_entries, start=1):
            if not isinstance(entry, dict):
                continue
            ordinal = entry.get("ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
                ordinal = position
            # Batch 0 0-C leak#2: labels no longer contain zero-information "[N] Source";
            # bounded wording uses generic_type retained by redaction contract (real identity remains irreversible).
            generic = generic_source_type_copy(entry.get("generic_type"), context.locale)
            entries.append(
                {
                    "source_ref": entry.get("source_ref"),
                    "generic_type": entry.get("generic_type", "other_source"),
                    "ordinal": ordinal,
                    "label": f"[{ordinal}] {generic}",
                }
            )
    return _make_block(
        context,
        block_kind="source_list",
        content={
            "label": heading,
            "entries": entries,
        },
    )


_INTERACTIVE_FALLBACK_COPY: dict[str, str] = {
    "zh-CN": "请在在线报告中查看交互内容",
    "en-US": "Please view the interactive content in the online report",
    "de-DE": "Bitte öffnen Sie die interaktiven Inhalte im Online-Bericht.",
    "fr-FR": "Consultez le contenu interactif dans le rapport en ligne.",
}


def _adapt_interactive(context: _AdapterContext) -> ExportIRBlock:
    # Google Workspace cards: exported reports link to the file itself, not the in-canvas preview URL.
    raw_url = context.payload.get("open_url") or context.payload.get("url", context.payload.get("card_url", context.payload.get("cardUrl")))
    safe_url = _safe_url(raw_url)
    warnings: tuple[str, ...] = ("interactive_content_not_executed",)
    if isinstance(raw_url, str) and safe_url is None:
        warnings += ("interactive_url_removed",)
    data = context.payload.get("data")
    provenance = project_embedded_data_provenance(context.payload)
    message = (
        format_embedded_data_provenance(provenance, context.locale)
        if context.kind == "html_artifact"
        else _INTERACTIVE_FALLBACK_COPY[resolve_presentation_locale(context.locale)]
    )
    return _make_block(
        context,
        block_kind="interactive_fallback",
        content={
            "title": context.payload.get("title"),
            "interactive_kind": context.kind,
            "safe_url": safe_url,
            "data": data if isinstance(data, (dict, list)) else None,
            "message": message,
            "embedded_data_provenance": asdict(provenance) if context.kind == "html_artifact" else None,
        },
        typed_data=data if isinstance(data, (dict, list)) else None,
        warnings=warnings,
    )


def _adapt_container(context: _AdapterContext) -> ExportIRBlock:
    raw_body = context.payload.get("body")
    body = raw_body if isinstance(raw_body, list) else []
    children: list[ExportIRBlock] = []
    for index, raw_child in enumerate(body):
        if not isinstance(raw_child, dict):
            raise ExportIRError("invalid_nested_section")
        kind = raw_child.get("kind")
        if not isinstance(kind, str):
            raise ExportIRError("invalid_nested_section")
        adapter = SECTION_ADAPTERS.get(kind, _adapt_unknown)
        child_payload = {
            key: value
            for key, value in raw_child.items()
            if key
            not in {
                "id",
                "kind",
                "rev",
                "source_refs",
                "frozen_chart_presentation",
            }
        }
        children.append(
            adapter(
                _AdapterContext(
                    top_section_id=context.top_section_id,
                    top_section_rev=context.top_section_rev,
                    kind=kind,
                    payload=child_payload,
                    source_refs=_source_refs(raw_child.get("source_refs")),
                    block_key=f"{context.block_key}/{index}",
                    order=index,
                    section_path=(*context.section_path, index),
                    surface_placement=context.surface_placement,
                    locale=context.locale,
                    theme_hash=context.theme_hash,
                    renderer_version=context.renderer_version,
                    asset_content_hashes=context.asset_content_hashes,
                    resolved_chart_rows=context.resolved_chart_rows,
                    renderer_contract_version=context.renderer_contract_version,
                    frozen_chart_presentation=_frozen_chart_presentation(
                        raw_child,
                        kind=kind,
                        locale=context.locale,
                        renderer_contract_version=context.renderer_contract_version,
                    ),
                )
            )
        )
    return _make_block(
        context,
        block_kind="container",
        content={
            "label": context.payload.get("label"),
            "title": context.payload.get("h2"),
        },
        children=tuple(children),
    )


def _adapt_unknown(context: _AdapterContext) -> ExportIRBlock:
    return _make_block(
        context,
        block_kind="interactive_fallback",
        content={
            "title": f"Unsupported section: {context.kind}",
            "interactive_kind": "unknown",
            "safe_url": None,
            "data": None,
            "message": "此内容暂无法可靠导出，请在在线报告中查看",
        },
        warnings=("unknown_section_kind",),
    )


def _make_block(
    context: _AdapterContext,
    *,
    block_kind: ExportBlockKind,
    content: dict[str, Any],
    typed_data: Any = None,
    visual_input: Any = None,
    warnings: tuple[str, ...] = (),
    children: tuple[ExportIRBlock, ...] = (),
) -> ExportIRBlock:
    semantic_tokens = _semantic_tokens(
        block_kind=block_kind,
        content=content,
    )
    return ExportIRBlock(
        block_key=context.block_key,
        block_kind=block_kind,
        order=context.order,
        source=ExportIRSource(
            section_id=context.top_section_id,
            section_rev=context.top_section_rev,
            section_kind=context.kind,
            section_path=context.section_path,
            surface_placement=context.surface_placement,
        ),
        source_refs=context.source_refs,
        content=content,
        semantic_tokens=semantic_tokens,
        semantic_content_sha256=_sha256(semantic_tokens),
        typed_data_sha256=_sha256(typed_data) if typed_data is not None else None,
        visual_input_sha256=(sha256_canonical(visual_input) if visual_input is not None else None),
        warnings=warnings,
        children=children,
    )


def _semantic_tokens(
    *,
    block_kind: ExportBlockKind,
    content: dict[str, Any],
) -> tuple[str, ...]:
    if block_kind == "heading":
        return _visible_tokens(
            [content.get("text")],
        )
    if block_kind == "paragraph":
        if "text" in content:
            # Source excerpts are literal text. Renderers escape markup themselves.
            text = " ".join(_FIGURE_ANCHOR_RE.sub(" ", content["text"]).split())
            return (text,) if text else ()
        value = content.get("markdown") if "markdown" in content else content.get("pills")
        if isinstance(value, str):
            # Fallback (embed-inline §1): no path may render literal [figure:id] anchors
            # in export text; normal inline pass consumes them, this protects unforeseen paths.
            value = _FIGURE_ANCHOR_RE.sub(" ", value)
        return _visible_tokens([value])
    if block_kind == "code":
        ordered = [
            content.get("lang"),
            content.get("sql"),
            content.get("code"),
            content.get("stdout_preview"),
            content.get("summary_payload"),
        ]
        return _visible_tokens(ordered, preserve_lines=True)
    if block_kind == "table":
        return _visible_tokens(
            [
                content.get("title"),
                content.get("subtitle"),
                [column.get("label") for column in content.get("columns", []) if isinstance(column, dict)],
            ]
        )
    if block_kind == "visual":
        payload = content.get("payload")
        return _visible_tokens(
            [
                payload.get("title") if isinstance(payload, dict) else None,
                payload.get("caption") if isinstance(payload, dict) else None,
            ]
        )
    if block_kind == "kpi_group":
        return _visible_tokens(
            [_kpi_visible_data(content.get("cells"))],
        )
    if block_kind == "source_list":
        return _source_list_visible_tokens(content)
    if block_kind == "recommendations":
        return _recommendation_visible_tokens(content)
    if block_kind == "callout":
        # Emit in **card order**. Avoid `_visible_tokens([content])` fallback at function end:
        # its `sorted(value)` sorts dict fields **alphabetically**, yielding export
        # confidence → impact → narrative → thesis → title, versus screen
        # `insight-card.tsx` order title → thesis → evidence → narrative → impact.
        # 2026-08-15 116 PDF showed isolated `high` first, then disordered content.
        #
        # **Omit** `confidence`: screen renders a localized badge (`ConfidenceBadge`, FE i18n
        # `confidence_high_title`), never a bare body word. Printing "high" alone is worse
        # than omission: no unit or subject. Paper display needs designed wording first,
        # a separate task; do not invent it incidentally here.
        #
        # First two keys describe **non-insight_card** callouts (`_adapt_callout` else branch);
        # keys of the two shapes do not intersect, so one ordered table suffices.
        return _visible_tokens(
            [
                content.get("label"),
                content.get("markdown"),
                content.get("title"),
                content.get("thesis"),
                content.get("evidence"),
                content.get("narrative"),
                content.get("impact"),
            ]
        )
    if block_kind == "interactive_fallback":
        return _visible_tokens(
            [
                content.get("title"),
                content.get("message"),
                content.get("safe_url"),
                content.get("data"),
            ]
        )
    return _visible_tokens([content])


def _kpi_visible_data(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    visible_keys = (
        "lbl",
        "label",
        "val",
        "value",
        "unit",
        "sub",
        "delta",
    )
    return [{key: cell[key] for key in visible_keys if key in cell} for cell in value if isinstance(cell, dict)]


def _source_list_visible_tokens(
    content: dict[str, Any],
) -> tuple[str, ...]:
    values: list[Any] = [content.get("label")]
    entries = content.get("entries")
    if not isinstance(entries, list):
        return _visible_tokens(values)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        if isinstance(tool, str):
            values.append(tool)
        tokens = entry.get("tokens")
        if isinstance(tokens, list):
            words = [token.get("word") for token in tokens if isinstance(token, dict)]
            if words:
                values.append(words)
                continue
        values.append(entry.get("query") or entry.get("label") or entry.get("artifact_id"))
    return _visible_tokens(values)


def _recommendation_visible_tokens(
    content: dict[str, Any],
) -> tuple[str, ...]:
    items = content.get("items")
    if not isinstance(items, list):
        return ()
    values: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        values.append(
            [
                item.get("priority"),
                item.get("title"),
                item.get("text"),
                item.get("label"),
                item.get("action"),
                item.get("detail"),
                item.get("owner"),
                item.get("timeline"),
                item.get("expected_impact"),
                item.get("rationale"),
            ]
        )
    return _visible_tokens(values)


def _visible_tokens(
    values: list[Any],
    *,
    preserve_lines: bool = False,
) -> tuple[str, ...]:
    tokens: list[str] = []

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            raw_tokens = value.splitlines() if preserve_lines else [value]
            for raw in raw_tokens:
                visible = _visible_text(raw)
                if visible:
                    tokens.append(visible)
            return
        if isinstance(value, bool):
            tokens.append("true" if value else "false")
            return
        if isinstance(value, (int, float)):
            tokens.append(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return
        if isinstance(value, list | tuple):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key])

    for item in values:
        visit(item)
    return tuple(tokens)


def _visible_text(value: str) -> str:
    text = re.sub(r"!\[[^\]]*]\([^)]*\)", "", value)
    text = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", text)
    # `[cite:N]` is a chat-path marker (Lead final_answer), occasionally leaking into canvas narrative.
    # Canvas already hides it (`citation-plugin` decoration), but **export lacks
    # that layer**: 2026-08-15 116 PDF printed literal `[cite:1]` in body.
    # This is the sole Markdown → visible-export-text boundary (neighbor of image/link syntax stripping),
    # so strip here.
    # ⚠ Strip only `[cite:N]`, **never `[N]`**: canonical canvas citation numbers
    # belong in PDF (observed correct "从19.82亿降至9.78亿 [1]").
    # Same cross-language literal as FE `lib/format-inline-tokens.ts` / `citation-plugin.ts` `CITE_MARKER_RE`,
    # parity-tested by test_cite_marker_parity.py.
    text = strip_cite_markers(text)
    # Strip validation-layer provenance markers such as [llm-std:] and [bi:art_...]
    # from human-readable exports.
    text = strip_provenance_tags(text)
    text = re.sub(r"[*_`~#>]+", "", text)
    return " ".join(text.split())


def _validate_surface_sections(
    snapshot: ExportSnapshot,
    *,
    entries: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> None:
    if len(entries) != len(sections) or _count_snapshot_sections(sections) != snapshot.section_count:
        raise ExportIRError("snapshot_surface_accounting_mismatch")
    seen: set[str] = set()
    for entry, section in zip(entries, sections, strict=True):
        section_id = _required_str(section, "id", code="invalid_snapshot_sections")
        if section_id in seen:
            raise ExportIRError("duplicate_snapshot_section")
        seen.add(section_id)
        if (
            entry.get("section_id") != section_id
            or entry.get("section_rev") != section.get("rev")
            or entry.get("effective_kind") != section.get("kind")
        ):
            raise ExportIRError("snapshot_surface_accounting_mismatch")
    if snapshot.surface_token != _required_str(
        _dict(snapshot.payload.get("surface_projection"), code="invalid_snapshot_surface"),
        "token",
        code="invalid_snapshot_surface",
    ):
        raise ExportIRError("snapshot_surface_token_mismatch")


def _count_snapshot_sections(sections: list[dict[str, Any]]) -> int:
    count = 0
    pending = list(sections)
    while pending:
        section = pending.pop()
        count += 1
        payload = section.get("payload") if "payload" in section else section
        if not isinstance(payload, dict):
            continue
        spec = payload.get("spec")
        if section.get("kind") == "chart" and isinstance(spec, dict) and isinstance(spec.get("views"), list):
            pending.extend(view for view in spec["views"] if isinstance(view, dict))
        body = payload.get("body")
        if isinstance(body, list):
            pending.extend(item for item in body if isinstance(item, dict))
    return count


def _normalize_columns(value: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_columns = value if isinstance(value, list) else []
    columns: list[dict[str, Any]] = []
    for index, column in enumerate(raw_columns):
        if isinstance(column, str):
            key = column
            label = column
            declared_type = None
            number_format = None
        elif isinstance(column, dict):
            raw_key = column.get("key", column.get("id", column.get("field")))
            key = raw_key if isinstance(raw_key, str) and raw_key else f"column_{index + 1}"
            raw_label = column.get("label", column.get("header", column.get("title", key)))
            label = raw_label if isinstance(raw_label, str) else key
            declared_type = column.get("value_type", column.get("type"))
            number_format = column.get("format", column.get("number_format"))
        else:
            continue
        value_type = (
            declared_type
            if declared_type in {"string", "number", "boolean", "date", "datetime", "mixed"}
            else _infer_value_type([row.get(key) for row in rows])
        )
        columns.append(
            {
                "key": key,
                "label": label,
                "value_type": value_type,
                "format": number_format if isinstance(number_format, str) else None,
                **(
                    {name: column[name] for name in ("unit", "currency_code", "display", "percent_scale", "date_pattern") if name in column}
                    if isinstance(column, dict)
                    else {}
                ),
            }
        )
    return columns


def _infer_value_type(values: list[Any]) -> str:
    types: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            types.add("boolean")
        elif isinstance(value, (int, float)):
            types.add("number")
        elif isinstance(value, str):
            types.add("string")
        else:
            types.add("mixed")
    if not types:
        return "string"
    return next(iter(types)) if len(types) == 1 else "mixed"


def _visual_artifact_ids(
    kind: str,
    payload: dict[str, Any],
    *,
    section_rev: int,
) -> list[str]:
    values: list[Any] = []
    if kind == "image":
        values.append(payload.get("artifact_id"))
    elif kind == "design":
        if payload.get("flattened_source_rev") == section_rev - 1:
            values.append(payload.get("flattened_artifact_id"))
        background = payload.get("background")
        if isinstance(background, dict):
            values.append(background.get("artifact_id"))
    elif kind == "chart":
        spec = payload.get("spec")
        if isinstance(spec, dict):
            values.append(spec.get("data_artifact_id"))
    return sorted({value for value in values if isinstance(value, str) and value})


def _required_visual_artifact_ids(
    kind: str,
    payload: dict[str, Any],
    *,
    section_rev: int,
) -> set[str]:
    if kind == "image":
        artifact_id = payload.get("artifact_id")
        return {artifact_id} if isinstance(artifact_id, str) and artifact_id else set()
    if kind == "design":
        return set(
            _visual_artifact_ids(
                kind,
                payload,
                section_rev=section_rev,
            )
        )
    if kind == "chart":
        spec = payload.get("spec")
        if isinstance(spec, dict):
            artifact_id = spec.get("data_artifact_id")
            if isinstance(artifact_id, str) and artifact_id:
                return {artifact_id}
    return set()


def _collect_sources(blocks: list[ExportIRBlock]) -> tuple[dict[str, Any], ...]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in iter_export_blocks(tuple(blocks)):
        for source_ref in block.source_refs:
            if source_ref in seen:
                continue
            seen.add(source_ref)
            sources.append({"source_ref": source_ref})
        if block.block_kind == "source_list":
            entries = block.content.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    digest = _sha256(entry)
                    if digest in seen:
                        continue
                    seen.add(digest)
                    sources.append({"entry": entry})
    return tuple(sources)


def _collect_visual_assets(
    blocks: list[ExportIRBlock],
) -> tuple[dict[str, Any], ...]:
    assets: dict[str, str] = {}
    for block in iter_export_blocks(tuple(blocks)):
        raw = block.content.get("artifact_content_hashes")
        if not isinstance(raw, dict):
            continue
        for artifact_id, content_hash in raw.items():
            if isinstance(artifact_id, str) and isinstance(content_hash, str):
                assets[artifact_id] = content_hash
    return tuple(
        {
            "artifact_id": artifact_id,
            "content_sha256": assets[artifact_id],
        }
        for artifact_id in sorted(assets)
    )


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))


def _source_refs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    refs: set[str] = set()
    for item in value:
        if isinstance(item, str) and item:
            refs.add(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ("source_ref", "tool_call_id", "call_id", "id"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate:
                refs.add(candidate)
                break
    return tuple(sorted(refs))


def _dict(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExportIRError(code)
    return value


def _list_of_dicts(value: Any, *, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ExportIRError(code)
    return value


def _required_str(value: dict[str, Any], key: str, *, code: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ExportIRError(code)
    return item


def _required_int(value: dict[str, Any], key: str, *, code: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ExportIRError(code)
    return item


def _sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExportIRError("invalid_ir_value") from exc
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


SECTION_ADAPTERS.update(
    {
        "eyebrow": lambda context: _adapt_heading(context, level=3),
        "h1": lambda context: _adapt_heading(context, level=1),
        "deck": _adapt_paragraph,
        "meta": _adapt_meta,
        "kpi_row": _adapt_kpi,
        "section": _adapt_container,
        "narrative": _adapt_paragraph,
        "data_table": _adapt_table,
        "callout": _adapt_callout,
        "next_list": _adapt_recommendations,
        "datarc_iframe": _adapt_interactive,
        "html_artifact": _adapt_interactive,
        "external_iframe": _adapt_interactive,
        "image": _adapt_visual,
        "design": _adapt_visual,
        "chart": _adapt_visual,
        "spec_block": _adapt_visual,
        "code_block": _adapt_code,
        "sources": _adapt_sources,
        "insight_card": _adapt_callout,
        "recommendations": _adapt_recommendations,
    }
)
if SECTION_ADAPTERS.keys() != SURFACE_KIND_POLICIES.keys():
    raise RuntimeError("ExportIR adapter registry must cover the exact surface kind registry")


__all__ = [
    "EXPORT_IR_SCHEMA_VERSION",
    "EXPORT_VISUAL_RENDERER_VERSION",
    "CapabilityPlanEntry",
    "ExportBlockKind",
    "ExportIR",
    "ExportIRBlock",
    "ExportIRDocument",
    "ExportIRError",
    "ExportIRSource",
    "SECTION_ADAPTERS",
    "build_capability_plan",
    "iter_export_blocks",
    "normalize_export_snapshot",
]
