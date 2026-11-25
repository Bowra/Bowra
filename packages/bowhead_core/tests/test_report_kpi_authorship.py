"""KPI cards follow their author (docs/sprints/report-kpi-authorship-2026-11-24).

Whether a report shows KPI cards, and which, is the model's decision. The system keeps one author's
KPIs as that author's document, and never picks, merges or creates KPI cards for a report that several
models wrote or that no model gave KPIs for.
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.lead_orchestrator import _aggregate_subagent_payloads, _select_bounded_inline
from bowhead_core.canvas_composer import _build_bi_fallback_sections


def _entry(sub: str, profile: str, payload: dict) -> dict:
    return {"subagent_id": sub, "profile": profile, "payload": payload}


_FIN = {"title": "财务", "exec_summary": "利润率 17.45%", "insights": [{"title": "利润率"}], "kpis": [{"label": "利润率", "value": "17.45%"}]}
_BIZ = {"title": "业务", "exec_summary": "营收持平", "insights": [{"title": "增长"}], "kpis": [{"label": "营收同比", "value": "+0.02%"}]}


def test_one_author_keeps_its_kpis_for_the_document() -> None:
    merged = _aggregate_subagent_payloads(None, bag=[_entry("sub_a", "financial-analyst", _FIN)])
    assert merged is not None
    assert [k["label"] for k in merged["kpis"]] == ["利润率"]


def test_several_authors_merge_no_kpis() -> None:
    merged = _aggregate_subagent_payloads(None, bag=[_entry("sub_a", "financial-analyst", _FIN), _entry("sub_b", "business-analyst", _BIZ)])
    assert merged is not None
    assert "kpis" not in merged
    assert [i["title"] for i in merged["insights"]] == ["利润率", "增长"]


def test_a_failed_helper_is_not_an_author() -> None:
    failed = {"error": {"kind": "peer_failed", "message": "timeout"}}
    merged = _aggregate_subagent_payloads(None, bag=[_entry("sub_a", "financial-analyst", _FIN), _entry("sub_b", "compute", failed)])
    assert merged is not None
    assert [k["label"] for k in merged["kpis"]] == ["利润率"]


def _bi_call(*results: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_name="bi_query", result={"results": list(results)})


def test_bi_fallback_shows_single_row_results_as_tables_not_kpi_cards() -> None:
    sections = _build_bi_fallback_sections(
        [
            _bi_call(
                {"ok": True, "query_text": "2025年 营业额", "sample_rows": [{"营业额": 163.42}]},
                {"ok": True, "query_text": "各省 营业额", "sample_rows": [{"省": "上海", "营业额": 51.2}, {"省": "北京", "营业额": 50.1}]},
                {"ok": True, "query_text": "空结果", "sample_rows": [{"营业额": None}]},
            )
        ]
    )
    assert [s["kind"] for s in sections] == ["data_table", "data_table"]
    assert sections[0]["caption"] == "2025年 营业额" and sections[0]["rows"] == [{"营业额": 163.42}]


def _sec(kind: str, sec_id: str, payload: dict | None = None) -> SimpleNamespace:
    s = SimpleNamespace(kind=kind, id=sec_id, payload=payload or {})
    s.to_report_section = lambda: {"id": sec_id, "kind": kind, **(payload or {})}
    return s


def test_inline_copy_skips_a_chapter_band() -> None:
    chapter = {"cells": [{"lbl": "章", "val": "1"}], "_meta": {"composed_by": "eager", "chapter": True}}
    only_chapter = _select_bounded_inline([_sec("kpi_row", "sec_ch", chapter)])
    assert [a for a in only_chapter if a["kind"] == "kpi"] == []
    with_cover = _select_bounded_inline([_sec("kpi_row", "sec_ch", chapter), _sec("kpi_row", "sec_cover", {"cells": [{"lbl": "封面", "val": "2"}]})])
    assert [a["id"] for a in with_cover if a["kind"] == "kpi"] == ["sec_cover"]
