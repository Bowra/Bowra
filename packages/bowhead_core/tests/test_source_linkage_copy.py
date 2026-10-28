from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_core.embedded_data_provenance import EmbeddedDataProvenance
from bowhead_core.source_access_projection import SOURCE_PRESENTATION_PATHS
from bowhead_core.source_linkage_copy import (
    SOURCE_GENERIC_TYPE_COPY,
    SOURCE_LINKAGE_COPY,
    format_embedded_data_provenance,
    format_figure_review_caveat,
    format_report_lifecycle,
    format_source_linkage,
    generic_source_type_copy,
    source_list_copy,
)
from bowhead_core.source_linkage_presentation import SourceLinkagePresentation

_COPY_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/report_trust/b1a_copy_contract_v1.json"


def test_source_linkage_copy_is_neutral_bilingual_and_ignores_zero_unknown() -> None:
    presentation = SourceLinkagePresentation(citation_count=2, figure_review_count=1, source_count=None)
    zh = format_source_linkage(presentation, "zh-Hant-TW")
    en = format_source_linkage(presentation, "EN_gb")
    assert zh == "2 处来源引用 · 1 个数字待核对 · 来源引用不代表数字已经过数值核验。"
    assert en == "2 source citations · 1 number to check · Source citations do not mean the numbers were numerically verified."
    assert "100%" not in zh + en
    assert "✓" not in zh + en


def test_source_linkage_copy_plural_lifecycle_caveat_and_sources() -> None:
    presentation = SourceLinkagePresentation(citation_count=1, figure_review_count=2, source_count=3)
    assert "1 source citation" in format_source_linkage(presentation, "en-US")
    assert "2 numbers to check" in format_source_linkage(presentation, "en-US")
    assert "3 sources" in format_source_linkage(presentation, "en-US")
    assert format_figure_review_caveat(("12%", "¥9"), "en") == "⚠ Numbers to check: 12%, ¥9"
    assert format_report_lifecycle("verified", "en") == "Report status: marked reviewed by owner"
    assert format_report_lifecycle("unknown", "en") == ""
    assert source_list_copy("en") == ("Sources", "Source")


def test_generic_source_type_copy_is_finite_and_falls_back() -> None:
    """批0 0-C leak#2：脱敏 entry 的 label 按 generic_type 出有限文案。"""
    assert generic_source_type_copy("query_result", "zh") == "查询结果"
    assert generic_source_type_copy("document", "en-US") == "Knowledge base document"
    assert generic_source_type_copy("web_source", "zh-CN") == "网页来源"
    assert generic_source_type_copy("generated_artifact", "en") == "Generated analysis data"
    # Unknown/missing → generic fallback: no exception and no raw values leaked into user-visible text.
    assert generic_source_type_copy("mystery_kind", "zh-CN") == "来源"
    assert generic_source_type_copy(None, "en-US") == "Source"


@pytest.mark.parametrize(
    ("status", "sources", "locale", "expected"),
    [
        ("injected", (2, 6), "zh-CN", "已附带生成数据 · 附带来源引用 [2] [6]"),
        ("injected", (), "en-US", "Generation data attached · No source citations provided"),
        (
            "computed_unchecked",
            (1,),
            "en-US",
            "Static content was not checked against tool output · Source citations [1]",
        ),
        ("computed_unchecked", (), "zh-CN", "静态内容未与工具输出核对"),
    ],
)
def test_embedded_data_copy_is_finite(
    status: str,
    sources: tuple[int, ...],
    locale: str,
    expected: str,
) -> None:
    provenance = EmbeddedDataProvenance(status=status, source_indexes=sources)  # type: ignore[arg-type]
    assert format_embedded_data_provenance(provenance, locale) == expected


def test_embedded_copy_catalog_matches_shared_b1a_fixture() -> None:
    fixture = json.loads(_COPY_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["contract"] == "source-linkage-copy/B1a-v1"
    for locale, expected in fixture["embedded_data"].items():
        actual = SOURCE_LINKAGE_COPY[locale]
        assert actual["embedded_injected_sources"] == expected["injected_sources"]
        assert actual["embedded_injected_no_sources"] == expected["injected_no_sources"]
        assert actual["embedded_computed_sources"] == expected["computed_sources"]
        assert actual["embedded_computed_no_sources"] == expected["computed_no_sources"]


def test_source_copy_and_presentation_registry_match_shared_b1a_fixture() -> None:
    fixture = json.loads(_COPY_FIXTURE.read_text(encoding="utf-8"))
    for locale, expected in fixture["source_linkage"].items():
        actual = SOURCE_LINKAGE_COPY[locale]
        assert actual["definition"] == expected["short_definition"]
        assert actual["lifecycle_verified"] == expected["lifecycle_verified"]
    assert [
        {
            "section_kind": item.section_kind,
            "path": list(item.path),
            "category": item.category,
        }
        for item in SOURCE_PRESENTATION_PATHS
    ] == fixture["source_presentation_paths"]


def test_finite_copy_catalog_obeys_semantic_deny_fixture() -> None:
    fixture = json.loads(_COPY_FIXTURE.read_text(encoding="utf-8"))
    rendered: list[str] = []
    for locale in ("zh-CN", "en-US"):
        rendered.extend(
            [
                format_source_linkage(SourceLinkagePresentation(2, 1, 3), locale),
                format_figure_review_caveat(("12%", "¥9"), locale),
                *(
                    format_report_lifecycle(status, locale)
                    for status in (
                        "active",
                        "verified",
                        "stale",
                        "archived",
                        "quarantined_stale",
                    )
                ),
                *(
                    format_embedded_data_provenance(
                        EmbeddedDataProvenance(status=status, source_indexes=sources),
                        locale,
                    )
                    for status in ("injected", "computed_unchecked")
                    for sources in ((1, 2), ())
                ),
                *source_list_copy(locale),
                *SOURCE_GENERIC_TYPE_COPY[locale].values(),
            ]
        )
    surface = "\n".join(rendered).casefold()
    assert [phrase for phrase in fixture["semantic_deny"] if phrase.casefold() in surface] == []
