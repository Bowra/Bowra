"""Report KPI scope and final-answer regressions from native 116 verification."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.report_kpis import reconcile_report_kpi_claims, report_kpi_count, user_excludes_report_kpis

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text",
    [
        "只展示图表，不要 KPI。",
        "不要HTML/PDF/图片/KPI，其余结构留空。",
        "不加 KPI 卡片。",
        "Create a report without KPI cards.",
        "Do not add any KPIs.",
        "Ein Bericht ohne KPI-Karten.",
        "Un rapport sans KPI.",
    ],
)
def test_explicit_kpi_exclusion(text: str) -> None:
    assert user_excludes_report_kpis(text)


@pytest.mark.parametrize(
    "text",
    [
        "Add two KPI cards with the chart.",
        "不要修改现有KPI，只增加一章。",
        "解释公司没有KPI的原因。",
        'Explain the phrase "不要KPI".',
        "```text\n不要KPI\n```\nKeep the current report.",
        "> Do not add KPIs.\nExplain that quoted instruction.",
    ],
)
def test_other_requests_do_not_exclude_report_cards(text: str) -> None:
    assert not user_excludes_report_kpis(text)


@pytest.mark.parametrize(
    ("text", "count", "language", "expected"),
    [
        ("文件为119字节。其余结构留空，无 HTML/PDF/图片/KPI。", 4, "zh-CN", "文件为119字节。报告包含 4 张 KPI 卡片。"),
        ("CSV: 119 bytes. The report has no KPI cards.", 4, "en-US", "CSV: 119 bytes. The report contains 4 KPI cards."),
        ("The report contains 2 KPI cards.", 0, "en-US", "The report contains no KPI cards."),
        ("The report contains 2 KPI cards.", 4, "en-US", "The report contains 4 KPI cards."),
        ("The report includes KPI cards.", 0, "en-US", "The report contains no KPI cards."),
        ("报告已添加KPI卡片。", 0, "zh-CN", "报告未包含 KPI 卡片。"),
        ("Der Bericht enthält keine KPI-Karten.", 4, "de-DE", "Der Bericht enthält 4 KPI-Karten."),
        ("Le rapport est sans KPI.", 4, "fr-FR", "Le rapport contient 4 cartes KPI."),
    ],
)
def test_false_structure_claim_uses_saved_report(text: str, count: int, language: str, expected: str) -> None:
    assert reconcile_report_kpi_claims(text, count=count, language=language) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Revenue is 12.3. Profit is 2.4.\n\nThe chart is ready.",
        "The company has no KPI targets.",
        'The quoted requirement was "no KPI cards".',
        "```text\nNo KPI cards.\n```\nThe report is ready.",
        "The report contains 4 KPI cards.",
    ],
)
def test_unrelated_prose_and_correct_claims_are_preserved(text: str) -> None:
    assert reconcile_report_kpi_claims(text, count=4, language="en-US") == text


def test_saved_kpi_count_excludes_history_and_chat() -> None:
    def section(**flags: Any) -> SimpleNamespace:
        return SimpleNamespace(kind="kpi_row", payload={"cells": [{"lbl": "CSV bytes", "val": "119"}]}, **flags)

    assert report_kpi_count([section(), section(superseded=True), section(hidden_for_chat_mode=True), section(quarantined=True)]) == 1
