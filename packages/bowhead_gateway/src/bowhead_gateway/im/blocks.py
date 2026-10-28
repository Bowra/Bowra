"""Channel-neutral block model + marker resolution for the IM layer.

Rather than making an adapter re-derive its provider payload from raw events, the event->block
machine (`im/turn_render.py`) folds a turn into a list of :class:`NeutralBlock`, and the adapter's
``send(blocks)`` renders those to its own payload. This module owns:

* the neutral output types represented by the :class:`NeutralBlock` union;
* **marker resolution (R-1)** — :func:`resolve_markers` strips/resolves every inline marker
  (``[num:…]`` / ``[cite:N]`` / ``[ref:…]`` / ``[chart:…]`` / ``[data:…]`` / ``[memory:…]``) before
  any text reaches a provider payload. This is today's #1 Feishu defect: chat ``final_answer`` keeps
  ``[cite:N]`` raw and the Feishu renderer has zero marker resolution, so raw markers leak into
  cards. The hard invariant (property-tested): **no unresolved marker survives into payload text.**

Trust computation (port of ``report-trust.ts``) is added to this module in a later slice (T2).

Grounded in: ``bowhead_core/citation_marks.py`` (historical marker handling),
``lead_orchestrator.py`` ``_resolve_citations`` (the citation/strip semantics), frontend
``format-num.ts`` (NUM_NEW/OLD regexes), tech-design §3/§4.4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from bowhead_core.agents.response_kind_scanner import strip_response_kind_markers
from bowhead_core.citation_marks import strip_legacy  # Authoritative legacy-marker bake semantics.
from bowhead_core.presentation_locale import SupportedPresentationLocale
from bowhead_core.report_trust import (  # single authoritative trust impl (shared with the composer)
    ReportTrustStats,
    compute_report_trust,
)
from bowhead_core.source_linkage_copy import ReportLifecycleStatus
from bowhead_core.source_linkage_presentation import SourceLinkagePresentation

# ───────────────────────── neutral block model ─────────────────────────
# A turn folds into an ordered list[NeutralBlock]; each adapter.send(blocks) renders to its payload.
# Provider adapters map these kinds to their native card components.


@dataclass(frozen=True)
class TextBlock:
    """Prose / narrative with markers already resolved."""

    markdown: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True)
class SourceLinkageLine:
    """Neutral source-linkage facts; deliberately contains no percentage."""

    presentation: SourceLinkagePresentation
    locale: SupportedPresentationLocale
    kind: Literal["source_linkage"] = "source_linkage"


@dataclass(frozen=True)
class ReportLifecycleLine:
    """Owner/report lifecycle, kept separate from source-linkage facts."""

    effective_status: ReportLifecycleStatus
    locale: SupportedPresentationLocale
    kind: Literal["report_lifecycle"] = "report_lifecycle"


@dataclass(frozen=True)
class Caveat:
    """Figures-to-review caveat."""

    items: tuple[str, ...]
    locale: SupportedPresentationLocale = "en-US"
    kind: Literal["caveat"] = "caveat"


@dataclass(frozen=True)
class KpiCell:
    label: str
    value: str
    delta: str | None = None


@dataclass(frozen=True)
class KpiRow:
    """KPI cells."""

    cells: tuple[KpiCell, ...]
    kind: Literal["kpi"] = "kpi"


@dataclass(frozen=True)
class TableBlock:
    """Tabular data."""

    caption: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    source: str | None = None
    kind: Literal["table"] = "table"


@dataclass(frozen=True)
class ImageBlock:
    """Image or server-rendered chart PNG (``download_url``, §7)."""

    url: str
    caption: str | None = None
    alt: str | None = None
    kind: Literal["image"] = "image"


@dataclass(frozen=True)
class SourceEntry:
    n: int
    # KB answer citations: entries with kb=True use the recipient's KB read permission. Label has the filename only
    # when readable, otherwise a restricted-source label. Other entries retain generic source labels (IM data citations show only ordinals).
    kb: bool = False
    label: str | None = None


@dataclass(frozen=True)
class SourcesBlock:
    """Fail-closed citation/source list backing the ``[N]`` refs."""

    entries: tuple[SourceEntry, ...]
    locale: SupportedPresentationLocale = "en-US"
    kind: Literal["sources"] = "sources"


@dataclass(frozen=True)
class ActionBlock:
    """An interactive HITL prompt — clarification / approval / access request.

    ``request_id`` ties the callback back to the clarification/approval store row.
    """

    action_kind: Literal["clarification", "approval", "access"]
    prompt: str
    options: tuple[str, ...] = ()
    request_id: str | None = None
    kind: Literal["action"] = "action"


@dataclass(frozen=True)
class FileBlock:
    """A deliverable file (pptx/xlsx/…)."""

    url: str
    name: str
    mime: str | None = None
    kind: Literal["file"] = "file"


@dataclass(frozen=True)
class CapabilityLink:
    """A deep-link into the web app for a capability with no IM-native authoring surface.

    Events carry a hint only, with no structured state (``events.py:76-83``).
    """

    label: str
    url: str
    kind: Literal["capability"] = "capability"


@dataclass(frozen=True)
class ErrorBlock:
    """Localized primary error plus finite diagnostics for host disclosure."""

    message: str
    code: str
    locale: str = "en-US"
    occurrence_id: str | None = None
    turn_id: str | None = None
    safe_detail: str | None = None
    retryable: bool | None = None
    kind: Literal["error"] = "error"


NeutralBlock = (
    TextBlock
    | SourceLinkageLine
    | ReportLifecycleLine
    | Caveat
    | KpiRow
    | TableBlock
    | ImageBlock
    | SourcesBlock
    | ActionBlock
    | FileBlock
    | CapabilityLink
    | ErrorBlock
)


# ───────────────────────── marker resolution (R-1, §4.4) ─────────────────────────
# citation-audit M3-2/M3-3: legacy-marker IM rendering narrows to **one** strip_legacy
# (same as canvas/chat/migration, with cross-surface golden guards). Remove both former gen2 branches
# (`_NUM_SRC_RE` per-number ✓ + render_inline_numbers localization/salvage) alongside
# reader shutdown and i18n v1 retirement. Aggregated SourceLinkageLine owns linkage semantics
# (changed in M1-5), also eliminating salvage's old unit-loss issue.
_CITE_RE = re.compile(r"\[cite:(\d+)\]")
# canvas-only widgets + orphan/legacy refs that must never reach a card text field.
# Feishu 07.06 #11 sweep: `[figure:sec_x]` (report-body figure anchor persisted by report_writer_loop/report_author)
# was previously missing from the list, leaking unchanged into IM card text.
_STRIP_RE = re.compile(r"\[(?:ref|chart|data|memory|figure):[^\]]*\]")
# defensive final sweep: any residual num/ref/chart/data/memory/figure marker (e.g. pathological
# `[num:]` that neither INLINE_RE nor the salvage matched). `[cite:N]` is already converted above.
_RESIDUAL_RE = re.compile(r"\[(?:num|ref|chart|data|memory|figure):[^\]]*\]")

# Card-text sanitization (review P1): the Feishu answer card renders `lark_md`, so LLM/document
# content could inject a Feishu mention (`<at ...>`) or a dangerous-scheme markdown link. Strip the
# mention tags and neutralize javascript:/data:/vbscript: link schemes — normal markdown (tables,
# bold, http/https links) is untouched, so the card's intended rich rendering still works.
_AT_TAG_RE = re.compile(r"</?at\b[^>]*>", re.IGNORECASE)
_DANGEROUS_LINK_RE = re.compile(r"\]\(\s*(?:javascript|data|vbscript):", re.IGNORECASE)

DEFAULT_LOCALE = "en-US"


def resolve_markers(
    text: str,
    *,
    locale: str = DEFAULT_LOCALE,
) -> str:
    """Resolve/strip every inline marker so clean text can go into a card payload (R-1).

    * 退役标记族（``[num:V:U(:src:S)]`` / ``[ref:…]`` / ``[chart:…]`` / ``[fact:|bi:|memory:…]``）
      -> ``citation_marks.strip_legacy``：``[num:]`` bake 成「值+单位」纯文本（**单位必保**），
      其余整删。**无逐数字 ✓/⚠**：来源关联语义由 :class:`SourceLinkageLine` + :class:`Caveat` 承担
      （M1-5 换口径；对齐 ``trust-strip.tsx`` 的聚合条，而非每个数字后挂字形）。
    * ``[cite:N]`` -> ``[N]``（来源列表渲染真正的引用）。
    * ``<response_kind>…</response_kind>`` / ``<headline>…</headline>`` -> stripped.
    * ``[data:…]`` / ``[figure:…]`` -> stripped（canvas-only widget / orphan；``[data:art_…]``
      上游已改写为 ``[cite:N]``，这里只兜漏网的）。

    Invariant (property-tested): no unresolved ``[num:`` / ``[cite:`` / ``[ref:`` / ``[chart:`` /
    ``[data:`` / ``[memory:`` / ``[figure:`` marker survives the call.

    ``locale`` 保留在签名里（调用方遍布 turn_render/feishu）：卡片其余部分仍按 locale 组装；
    退役标记的 bake 本身**无 locale 维度**（落库死文本已定死 zh 口径，见 citation_marks）。
    """
    if not text:
        return text

    # Internal Lead protocol markers (`<response_kind>` / `<headline>`): status cards
    # concatenate raw narrative deltas and old persisted events may still carry them.
    text = strip_response_kind_markers(text)
    text = strip_legacy(text)
    text = _CITE_RE.sub(lambda m: f"[{m.group(1)}]", text)
    text = _STRIP_RE.sub("", text)
    # belt-and-suspenders: kill any residual marker the passes above couldn't parse.
    text = _RESIDUAL_RE.sub("", text)
    # sanitize card-injection vectors (Feishu lark_md): strip <at> mentions + neutralize dangerous
    # link schemes. Normal markdown is preserved.
    text = _AT_TAG_RE.sub("", text)
    text = _DANGEROUS_LINK_RE.sub("](#", text)
    return text


# `ReportTrustStats` + `compute_report_trust` now live in `bowhead_core.report_trust` (imported above)
# so the canvas composer can compute trust at compose-time over raw sections and ride it on
# canvas.done; this module re-exports them (below) for the IM fold + existing importers.


__all__ = [
    "ActionBlock",
    "CapabilityLink",
    "Caveat",
    "FileBlock",
    "ErrorBlock",
    "ImageBlock",
    "KpiCell",
    "KpiRow",
    "NeutralBlock",
    "ReportLifecycleLine",
    "ReportTrustStats",
    "SourceEntry",
    "SourceLinkageLine",
    "SourcesBlock",
    "TableBlock",
    "TextBlock",
    "compute_report_trust",
    "resolve_markers",
]


def format_source_lines(entries: tuple[SourceEntry, ...] | list[SourceEntry], locale: object) -> tuple[str, list[str]]:
    """来源列表的标题与各行——飞书答案卡与 Teams 卡片共用这一份格式。"""
    from bowhead_core.presentation_locale import resolve_presentation_locale
    from bowhead_core.source_linkage_copy import SOURCE_LINKAGE_COPY, source_list_copy

    heading, generic = source_list_copy(locale)
    copy = SOURCE_LINKAGE_COPY[resolve_presentation_locale(locale)]
    lines: list[str] = []
    for e in entries:
        if e.kb and e.label:
            lines.append(f"[{e.n}] {e.label} · {copy['kb_source_suffix']}")
        elif e.kb:
            lines.append(f"[{e.n}] {copy['restricted_source']}")
        else:
            lines.append(f"[{e.n}] {generic}")
    return heading, lines
