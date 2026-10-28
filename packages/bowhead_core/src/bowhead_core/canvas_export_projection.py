"""Versioned current-content projection used by Canvas export."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.canvas_rank import canonical_order, is_seq_authoritative

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_core.canvas_store import Section


CONTENT_PROJECTION_CONTRACT_VERSION = 1
SURFACE_PROJECTION_CONTRACT_VERSION = 1

SurfacePlacement = Literal[
    "report_body",
    "report_embedded",
    "data_tab",
    "code_tab",
    "sources",
    "excluded",
]
ParityRequirement = Literal[
    "semantic_content",
    "exact_visual",
    "typed_data",
    "interaction_fallback",
    "summary_policy",
]
SurfaceExclusionReason = Literal[
    "hidden_for_chat_mode",
    "superseded_history",
    "quarantined_rejected",
]


@dataclass(frozen=True, slots=True)
class SurfaceKindPolicy:
    placement: SurfacePlacement
    parity_requirement: ParityRequirement


SURFACE_KIND_POLICIES: dict[str, SurfaceKindPolicy] = {
    "eyebrow": SurfaceKindPolicy("report_body", "semantic_content"),
    "h1": SurfaceKindPolicy("report_body", "semantic_content"),
    "deck": SurfaceKindPolicy("report_body", "semantic_content"),
    "meta": SurfaceKindPolicy("report_body", "semantic_content"),
    "kpi_row": SurfaceKindPolicy("report_body", "typed_data"),
    "section": SurfaceKindPolicy("report_body", "semantic_content"),
    "narrative": SurfaceKindPolicy("report_body", "semantic_content"),
    "data_table": SurfaceKindPolicy("data_tab", "typed_data"),
    "callout": SurfaceKindPolicy("report_body", "semantic_content"),
    "next_list": SurfaceKindPolicy("report_body", "semantic_content"),
    "datarc_iframe": SurfaceKindPolicy("report_body", "interaction_fallback"),
    "html_artifact": SurfaceKindPolicy("report_body", "interaction_fallback"),
    "external_iframe": SurfaceKindPolicy("report_body", "interaction_fallback"),
    "image": SurfaceKindPolicy("report_body", "exact_visual"),
    "design": SurfaceKindPolicy("report_body", "exact_visual"),
    "chart": SurfaceKindPolicy("report_body", "exact_visual"),
    "spec_block": SurfaceKindPolicy("report_body", "exact_visual"),
    "code_block": SurfaceKindPolicy("code_tab", "semantic_content"),
    "sources": SurfaceKindPolicy("sources", "semantic_content"),
    "insight_card": SurfaceKindPolicy("report_body", "semantic_content"),
    "recommendations": SurfaceKindPolicy("report_body", "semantic_content"),
}
_UNKNOWN_KIND_POLICY = SurfaceKindPolicy("report_body", "semantic_content")


@dataclass(frozen=True, slots=True)
class ContentProjectionSection:
    section_id: str
    kind: str
    rev: int
    seq: int
    quarantined: bool
    superseded: bool
    hidden_for_chat_mode: bool


@dataclass(frozen=True, slots=True)
class CanvasContentProjection:
    contract_version: int
    projection_token: str
    title: str | None
    sections: tuple[ContentProjectionSection, ...]


@dataclass(frozen=True, slots=True)
class SurfaceProjectionEntry:
    section_id: str
    section_rev: int
    order: int
    placement: SurfacePlacement
    parent_section_id: str | None
    effective_kind: str
    parity_requirement: ParityRequirement


@dataclass(frozen=True, slots=True)
class SurfaceProjectionExclusion:
    section_id: str
    reason: SurfaceExclusionReason


@dataclass(frozen=True, slots=True)
class CanvasSurfaceProjection:
    contract_version: int
    content_projection_token: str
    surface_token: str
    entries: tuple[SurfaceProjectionEntry, ...]
    exclusions: tuple[SurfaceProjectionExclusion, ...]


def build_canvas_content_projection(
    *,
    title: str | None,
    sections: Sequence[Section],
) -> CanvasContentProjection:
    """Build the deterministic CurrentCanvasContentSet v1 identity.

    Payload bytes and timestamps are intentionally absent. Persisted content
    changes are represented by ``rev``; lifecycle transitions change the
    included section set. Sorting makes the token stable across DB row order.
    """
    current = sorted(
        (section for section in sections if not section.hidden_for_chat_mode and not section.superseded),
        key=lambda section: (section.seq, section.id),
    )
    entries = tuple(
        ContentProjectionSection(
            section_id=section.id,
            kind=section.kind,
            rev=section.rev,
            seq=section.seq,
            quarantined=section.quarantined,
            superseded=section.superseded,
            hidden_for_chat_mode=section.hidden_for_chat_mode,
        )
        for section in current
    )
    token_payload = {
        "title": title,
        "sections": [
            {
                "id": entry.section_id,
                "kind": entry.kind,
                "rev": entry.rev,
                "seq": entry.seq,
                "quarantined": entry.quarantined,
                "superseded": entry.superseded,
                "hidden_for_chat_mode": entry.hidden_for_chat_mode,
            }
            for entry in entries
        ],
    }
    digest = hashlib.sha256(_canonical_json(token_payload).encode("utf-8")).hexdigest()
    return CanvasContentProjection(
        contract_version=CONTENT_PROJECTION_CONTRACT_VERSION,
        projection_token=f"sha256:{digest}",
        title=title,
        sections=entries,
    )


def build_canvas_surface_projection(
    *,
    content_projection: CanvasContentProjection,
    sections: Sequence[Section],
) -> CanvasSurfaceProjection:
    """Build the v1 page/export surface shared plan.

    The ordering mirrors ``ReportView.preprocessSections`` for the report
    surface, then appends Data and Code tab entries. Insight-referenced chart
    and image sections are emitted once under their first visible parent;
    every other current chart/image stays a top-level report entry, exactly
    like the page (decision #4, 2026-08-24).
    """
    if content_projection.contract_version != CONTENT_PROJECTION_CONTRACT_VERSION:
        raise ValueError("unsupported content projection contract")

    ordered_sections = sorted(sections, key=lambda section: (section.seq, section.id))
    section_by_id = {section.id: section for section in ordered_sections}
    if len(section_by_id) != len(ordered_sections):
        raise ValueError("duplicate section id in surface projection")
    _validate_content_projection(content_projection, section_by_id)

    content_ids = {entry.section_id for entry in content_projection.sections}
    reason_by_id: dict[str, SurfaceExclusionReason] = {}
    for section in ordered_sections:
        if section.hidden_for_chat_mode:
            reason_by_id[section.id] = "hidden_for_chat_mode"
        elif section.superseded:
            reason_by_id[section.id] = "superseded_history"
        elif section.quarantined and section.id in content_ids:
            reason_by_id[section.id] = "quarantined_rejected"

    eligible = [section for section in ordered_sections if section.id in content_ids and section.id not in reason_by_id]
    eligible_by_id = {section.id: section for section in eligible}
    # Hosts claim in database order (seq, id). Host kinds (insight_card / narrative / deck / callout)
    # all have body rank; canonical sorting preserves their relative order, so first-come claims match emission order.
    report_sections = [section for section in eligible if _kind_policy(section.kind).placement in {"report_body", "sources"}]

    embedded_by_parent: dict[str, list[str]] = {}
    parent_by_visual: dict[str, str] = {}
    for section in report_sections:
        if section.kind != "insight_card":
            continue
        for visual_id in _insight_visual_refs(section):
            visual = eligible_by_id.get(visual_id)
            if visual is None or visual.kind not in {"chart", "image"}:
                continue
            if visual_id in parent_by_visual:
                continue
            parent_by_visual[visual_id] = section.id
            embedded_by_parent.setdefault(section.id, []).append(visual_id)
    # Page `preprocessSections` folds only visuals referenced by insight evidence **before** sorting and segmentation;
    # anchor-referenced visuals are hidden later in canvasToPMDoc. Remove only the former from the segmentation sequence.
    insight_visual_ids = frozenset(parent_by_visual)
    # embed-inline r3 (export-embed-inline tech-design): `[figure:id]` Markdown anchors in the narrative family
    # (narrative/deck/callout) also establish inline relationships. On the page, canvasToPMDoc inlines by ID
    # and hides standalone blocks. Projection previously recognized only insight evidence links, so export
    # piled visuals before the body by seq (116 dogfood screenshots). Insights take priority (first come);
    # parent_by_visual deduplicates repeated/cross-host anchors; the kind allowlist matches A2 cascade rules.
    for section in report_sections:
        if section.kind not in _MARKDOWN_ANCHOR_HOST_KINDS:
            continue
        for visual_id in _markdown_visual_refs(section):
            visual = eligible_by_id.get(visual_id)
            if visual is None or visual.kind not in {"chart", "image"}:
                continue
            if visual_id in parent_by_visual:
                continue
            parent_by_visual[visual_id] = section.id
            embedded_by_parent.setdefault(section.id, []).append(visual_id)

    # Decision #4 (plan-report-assembly batch 2-d, 2026-08-24): page `preprocessSections` removed the rule
    # that folded all unreferenced charts whenever insight_card existed. Calling add_chart means placing the chart
    # on the Canvas; charts without evidence references still appear at top level. This projection kept the old rule
    # and excluded them, silently omitting page-visible charts from PDF / PPTX / XLSX. Now it matches the page:
    # only referenced chart/image sections embed in hosts; all others remain top-level entries.

    exclusions = tuple(
        SurfaceProjectionExclusion(section_id=section.id, reason=reason_by_id[section.id])
        for section in ordered_sections
        if section.id in reason_by_id
    )

    planned: list[tuple[Section, SurfacePlacement, str | None]] = []
    page_sequence = [section for section in eligible if section.id not in insight_visual_ids]
    for section in _report_top_level_order(page_sequence):
        if section.id in parent_by_visual or _kind_policy(section.kind).placement not in {"report_body", "sources"}:
            continue
        planned.append((section, _kind_policy(section.kind).placement, None))
        planned.extend((eligible_by_id[visual_id], "report_embedded", section.id) for visual_id in embedded_by_parent.get(section.id, []))

    for placement in ("data_tab", "code_tab"):
        planned.extend(
            (section, placement, None) for section in eligible if section.id not in reason_by_id and _kind_policy(section.kind).placement == placement
        )

    entries = tuple(
        SurfaceProjectionEntry(
            section_id=section.id,
            section_rev=section.rev,
            order=order,
            placement=placement,
            parent_section_id=parent_section_id,
            effective_kind=section.kind,
            parity_requirement=_kind_policy(section.kind).parity_requirement,
        )
        for order, (section, placement, parent_section_id) in enumerate(planned)
    )
    surface_token = compute_surface_projection_token(
        surface_contract_version=SURFACE_PROJECTION_CONTRACT_VERSION,
        content_projection_token=content_projection.projection_token,
        entries=entries,
        exclusions=exclusions,
    )
    return CanvasSurfaceProjection(
        contract_version=SURFACE_PROJECTION_CONTRACT_VERSION,
        content_projection_token=content_projection.projection_token,
        surface_token=surface_token,
        entries=entries,
        exclusions=exclusions,
    )


def compute_surface_projection_token(
    *,
    surface_contract_version: int,
    content_projection_token: str,
    entries: Sequence[SurfaceProjectionEntry],
    exclusions: Sequence[SurfaceProjectionExclusion],
) -> str:
    token_payload = {
        "surface_contract_version": surface_contract_version,
        "content_projection_token": content_projection_token,
        "entries": [
            {
                "section_id": entry.section_id,
                "section_rev": entry.section_rev,
                "order": entry.order,
                "placement": entry.placement,
                "parent_section_id": entry.parent_section_id,
                "effective_kind": entry.effective_kind,
                "parity_requirement": entry.parity_requirement,
            }
            for entry in entries
        ],
        "exclusions": [
            {
                "section_id": exclusion.section_id,
                "reason": exclusion.reason,
            }
            for exclusion in exclusions
        ],
    }
    digest = hashlib.sha256(_canonical_json(token_payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _validate_content_projection(
    content_projection: CanvasContentProjection,
    section_by_id: dict[str, Section],
) -> None:
    for entry in content_projection.sections:
        section = section_by_id.get(entry.section_id)
        if section is None:
            raise ValueError(f"content projection section missing: {entry.section_id}")
        actual = (
            section.kind,
            section.rev,
            section.seq,
            section.quarantined,
            section.superseded,
            section.hidden_for_chat_mode,
        )
        expected = (
            entry.kind,
            entry.rev,
            entry.seq,
            entry.quarantined,
            entry.superseded,
            entry.hidden_for_chat_mode,
        )
        if actual != expected:
            raise ValueError(f"content projection section changed: {entry.section_id}")


def _kind_policy(kind: str) -> SurfaceKindPolicy:
    return SURFACE_KIND_POLICIES.get(kind, _UNKNOWN_KIND_POLICY)


def _report_top_level_order(sections: Sequence[Section]) -> list[Section]:
    """报告顶层出块序，与页面 ``preprocessSections`` 同一套排序。

    ``sections`` 与页面 ``preprocessSections`` 的输入同形：按 (seq, id) 排好的当前段，
    只去掉了被 insight evidence 引用的 chart/image；被 ``[figure:]`` 锚引用的图、
    data_table、code_block 仍留在序列里。分段边界（第二个 h1 及其紧邻的 eyebrow）
    按这份序列算——eyebrow 与 h1 之间夹着锚图或数据/代码块时就不随 h1 进新段，
    与页面一致。调用方排好序后再去掉锚内嵌图和非正文位置的段。

    A3：seq 权威化后**不得再强排**（画布与 legacy 导出都在 flag 开时改信 seq），
    否则 V2 导出的段序会与用户在画布上拖出来的序不一致。flag 关时走
    ``canvas_rank.canonical_order``：rank 单源、并按报告分段各自排序——多腿计划
    每章各带 h1，全局强排会把各章的 h1 / KPI 各自归堆、章节互插，而页面是分段排的。
    """
    if is_seq_authoritative():
        return list(sections)
    by_id = {section.id: section for section in sections}
    return [by_id[section_id] for section_id in canonical_order(sections)]


# Same literal as canvas_store._INLINE_FIGURE_RE / export_ir._FIGURE_ANCHOR_RE.
_FIGURE_ANCHOR_RE = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")
# Page canvasToPMDoc anchor-host kinds (text-based: narrative/deck/callout).
_MARKDOWN_ANCHOR_HOST_KINDS = frozenset({"narrative", "deck", "callout"})


def _markdown_visual_refs(section: Section) -> list[str]:
    markdown = section.payload.get("markdown")
    if not isinstance(markdown, str):
        return []
    refs: list[str] = []
    for ref in _FIGURE_ANCHOR_RE.findall(markdown):
        if ref not in refs:
            refs.append(ref)
    return refs


def _insight_visual_refs(section: Section) -> list[str]:
    evidence = section.payload.get("evidence")
    if not isinstance(evidence, list):
        return []
    refs: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        ref = item.get("chart_section_id")
        if isinstance(ref, str) and ref:
            refs.append(ref)
    return refs


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
