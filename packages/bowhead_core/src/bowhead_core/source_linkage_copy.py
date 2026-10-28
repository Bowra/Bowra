"""Finite bilingual copy for B1a source-linkage presentation surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from bowhead_core.embedded_data_provenance import EmbeddedDataProvenance
from bowhead_core.presentation_locale import SupportedPresentationLocale, resolve_presentation_locale
from bowhead_core.source_linkage_presentation import SourceLinkagePresentation

ReportLifecycleStatus = Literal["active", "verified", "stale", "archived", "quarantined_stale"]

# Batch 0 0-C leak#2: after static-export redaction, entries retain only {source_ref, generic_type, ordinal};
# labels previously all became generic_source ("来源"/"Source"), yielding N PDF rows of uninformative placeholders.
# generic_type is an intentionally retained nonsensitive field in the redaction contract; use it for bounded copy:
# retain rows (preserve [N] anchors), readable types, and irreversible real identities.
SOURCE_GENERIC_TYPE_COPY: dict[SupportedPresentationLocale, dict[str, str]] = {
    "zh-CN": {
        "query_result": "查询结果",
        "document": "知识库文档",
        "web_source": "网页来源",
        "generated_artifact": "分析生成的数据",
        "other_source": "来源",
    },
    "en-US": {
        "query_result": "Query result",
        "document": "Knowledge base document",
        "web_source": "Web source",
        "generated_artifact": "Generated analysis data",
        "other_source": "Source",
    },
    "de-DE": {
        "query_result": "Abfrageergebnis",
        "document": "Wissensdatenbankdokument",
        "web_source": "Webquelle",
        "generated_artifact": "Generierte Analysedaten",
        "other_source": "Quelle",
    },
    "fr-FR": {
        "query_result": "Résultat de requête",
        "document": "Document de la base de connaissances",
        "web_source": "Source web",
        "generated_artifact": "Données générées par l’analyse",
        "other_source": "Source",
    },
}

# ⚠ "figure" in key names is frozen historical spelling (`figure_review` / `figures_to_review` /
# `figure_review_count` through the frontend and IM adapter); it has always meant **numbers**, not charts.
# English copy previously literally said "figures to review"; reports full of charts made this read as N charts awaiting review,
# unlike Chinese "N 个数字待核对", which means numbers to check (I personally misread the counting logic because of this).
# Copy now uses number/check; key names remain unchanged (contract identifiers across FE/core/gateway).
SOURCE_LINKAGE_COPY: dict[SupportedPresentationLocale, dict[str, str]] = {
    "zh-CN": {
        "citation": "{count} 处来源引用",
        "figure_review": "{count} 个数字待核对",
        "source": "{count} 条来源",
        "definition": "来源引用不代表数字已经过数值核验。",
        "figures_to_review": "⚠ 数字待核对：{items}",
        "sources_heading": "来源",
        "generic_source": "来源",
        "kb_source_suffix": "知识库",
        "restricted_source": "受限来源",
        "embedded_injected_sources": "已附带生成数据 · 附带来源引用 {sources}",
        "embedded_injected_no_sources": "已附带生成数据 · 未提供来源引用",
        "embedded_computed_sources": "静态内容未与工具输出核对 · 附带来源引用 {sources}",
        "embedded_computed_no_sources": "静态内容未与工具输出核对",
        "lifecycle_active": "报告状态：活跃",
        "lifecycle_verified": "报告状态：所有者已标记为已核对",
        "lifecycle_stale": "报告状态：可能已过时",
        "lifecycle_archived": "报告状态：已归档",
        "lifecycle_quarantined_stale": "报告状态：含隔离内容，可能已过时",
    },
    "en-US": {
        "citation": "{count} source citation{suffix}",
        "figure_review": "{count} number{suffix} to check",
        "source": "{count} source{suffix}",
        "definition": "Source citations do not mean the numbers were numerically verified.",
        "figures_to_review": "⚠ Numbers to check: {items}",
        "sources_heading": "Sources",
        "generic_source": "Source",
        "kb_source_suffix": "Knowledge base",
        "restricted_source": "Restricted source",
        "embedded_injected_sources": "Generation data attached · Source citations {sources}",
        "embedded_injected_no_sources": "Generation data attached · No source citations provided",
        "embedded_computed_sources": ("Static content was not checked against tool output · Source citations {sources}"),
        "embedded_computed_no_sources": "Static content was not checked against tool output",
        "lifecycle_active": "Report status: active",
        "lifecycle_verified": "Report status: marked reviewed by owner",
        "lifecycle_stale": "Report status: may be out of date",
        "lifecycle_archived": "Report status: archived",
        "lifecycle_quarantined_stale": "Report status: may be out of date due to quarantined content",
    },
    "de-DE": {
        "citation": "{count} Quellenverweise",
        "figure_review": "{count} zu prüfende Zahlen",
        "source": "{count} Quellen",
        "definition": "Quellenverweise bedeuten nicht, dass die Zahlen rechnerisch verifiziert wurden.",
        "figures_to_review": "⚠ Zu prüfende Zahlen: {items}",
        "sources_heading": "Quellen",
        "generic_source": "Quelle",
        "kb_source_suffix": "Wissensdatenbank",
        "restricted_source": "Eingeschränkte Quelle",
        "embedded_injected_sources": "Generierte Daten beigefügt · Quellenverweise {sources}",
        "embedded_injected_no_sources": "Generierte Daten beigefügt · Keine Quellenverweise angegeben",
        "embedded_computed_sources": "Statischer Inhalt wurde nicht mit Tool-Ausgaben abgeglichen · Quellenverweise {sources}",
        "embedded_computed_no_sources": "Statischer Inhalt wurde nicht mit Tool-Ausgaben abgeglichen",
        "lifecycle_active": "Berichtsstatus: aktiv",
        "lifecycle_verified": "Berichtsstatus: vom Owner als geprüft markiert",
        "lifecycle_stale": "Berichtsstatus: möglicherweise veraltet",
        "lifecycle_archived": "Berichtsstatus: archiviert",
        "lifecycle_quarantined_stale": "Berichtsstatus: möglicherweise wegen isolierter Inhalte veraltet",
    },
    "fr-FR": {
        "citation": "{count} référence{suffix} à des sources",
        "figure_review": "{count} nombre{suffix} à vérifier",
        "source": "{count} source{suffix}",
        "definition": "Les références aux sources ne signifient pas que les valeurs numériques ont été vérifiées.",
        "figures_to_review": "⚠ Nombres à vérifier : {items}",
        "sources_heading": "Sources",
        "generic_source": "Source",
        "kb_source_suffix": "Base de connaissances",
        "restricted_source": "Source à accès restreint",
        "embedded_injected_sources": "Données de génération jointes · Références aux sources {sources}",
        "embedded_injected_no_sources": "Données de génération jointes · Aucune référence aux sources fournie",
        "embedded_computed_sources": "Le contenu statique n’a pas été comparé aux résultats des outils · Références aux sources {sources}",
        "embedded_computed_no_sources": "Le contenu statique n’a pas été comparé aux résultats des outils",
        "lifecycle_active": "État du rapport : actif",
        "lifecycle_verified": "État du rapport : marqué comme vérifié par le propriétaire",
        "lifecycle_stale": "État du rapport : peut être obsolète",
        "lifecycle_archived": "État du rapport : archivé",
        "lifecycle_quarantined_stale": "État du rapport : peut être obsolète en raison de contenus mis en quarantaine",
    },
}


def _locale(value: object) -> SupportedPresentationLocale:
    return resolve_presentation_locale(value)


def _count_token(key: Literal["citation", "figure_review", "source"], count: int, locale: object) -> str:
    selected = _locale(locale)
    template = SOURCE_LINKAGE_COPY[selected][key]
    suffix = "" if count == 1 else "s"
    return template.format(count=count, suffix=suffix)


def format_source_linkage(
    presentation: SourceLinkagePresentation,
    locale: object,
    *,
    include_definition: bool = True,
) -> str:
    """Format only positive, independently-known counts in a fixed order."""

    tokens: list[str] = []
    for key, count in (
        ("citation", presentation.citation_count),
        ("figure_review", presentation.figure_review_count),
        ("source", presentation.source_count),
    ):
        if count is not None and count > 0:
            tokens.append(_count_token(key, count, locale))  # type: ignore[arg-type]
    if not tokens:
        return ""
    if include_definition:
        tokens.append(SOURCE_LINKAGE_COPY[_locale(locale)]["definition"])
    return " · ".join(tokens)


def format_figure_review_caveat(items: Sequence[str], locale: object) -> str:
    cleaned = [item.strip() for item in items if isinstance(item, str) and item.strip()]
    if not cleaned:
        return ""
    selected = _locale(locale)
    separator = "、" if selected == "zh-CN" else ", "
    return SOURCE_LINKAGE_COPY[selected]["figures_to_review"].format(items=separator.join(cleaned))


def format_report_lifecycle(status: object, locale: object) -> str:
    if status not in {"active", "verified", "stale", "archived", "quarantined_stale"}:
        return ""
    return SOURCE_LINKAGE_COPY[_locale(locale)][f"lifecycle_{status}"]


def source_list_copy(locale: object) -> tuple[str, str]:
    copy = SOURCE_LINKAGE_COPY[_locale(locale)]
    return copy["sources_heading"], copy["generic_source"]


def generic_source_type_copy(generic_type: object, locale: object) -> str:
    """Per-type display copy for a redacted source entry; unknown → generic."""
    table = SOURCE_GENERIC_TYPE_COPY[_locale(locale)]
    if isinstance(generic_type, str) and generic_type in table:
        return table[generic_type]
    return table["other_source"]


def format_embedded_data_provenance(
    provenance: EmbeddedDataProvenance,
    locale: object,
) -> str:
    copy = SOURCE_LINKAGE_COPY[_locale(locale)]
    status_key = "injected" if provenance.status == "injected" else "computed"
    if provenance.has_sources:
        sources = " ".join(f"[{index}]" for index in provenance.source_indexes)
        return copy[f"embedded_{status_key}_sources"].format(sources=sources)
    return copy[f"embedded_{status_key}_no_sources"]


__all__ = [
    "ReportLifecycleStatus",
    "SOURCE_GENERIC_TYPE_COPY",
    "SOURCE_LINKAGE_COPY",
    "format_embedded_data_provenance",
    "format_figure_review_caveat",
    "format_report_lifecycle",
    "format_source_linkage",
    "generic_source_type_copy",
    "source_list_copy",
]
