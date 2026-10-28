"""E-fix 2026-05-25 (canvas-empty bug E1) — `_prettify_output_preview` tests.

Cover per-tool human-friendly summary extraction. Tool result shapes mirror
real prod data from t_6055cc76db7d and dev gateway smoke tests.
"""

from __future__ import annotations

import pytest
from bowhead_core.subagent import _prettify_output_preview


@pytest.fixture(autouse=True)
def _zh_ui_turn():
    """These goldens model a zh-CN interface turn. Without language context the step previews
    use English (docs/design/language-policy.md, "AI answers": deterministic product copy without
    browser context or saved preferences uses English; covered by test_output_preview_i18n)."""
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    token = set_current_ui_language("zh-CN")
    yield
    reset_current_ui_language(token)


def test_error_passthrough() -> None:
    assert _prettify_output_preview(
        "bi_query",
        None,
        "tool_timeout",
    ).startswith("失败: ")


def test_none_result() -> None:
    assert _prettify_output_preview("bi_query", None, None) == "无返回"


def test_bi_schema() -> None:
    result = {
        "measures": [{"id": i} for i in range(27)],
        "dimensions": [{"id": i} for i in range(8)],
    }
    assert _prettify_output_preview("bi_schema", result, None) == "27 测度 + 8 维度"


def test_bi_query_batch_single_query_single_row_kpi() -> None:
    result = {
        "results": [
            {
                "ok": True,
                "sample_rows": [
                    {"DSM岗位负责人": "陈一鸣(Alex)", "协访代表覆盖率": 0.5},
                ],
            }
        ]
    }
    out = _prettify_output_preview("bi_query", result, None)
    assert "1 查询" in out
    assert "1 成功" in out
    assert "陈一鸣(Alex)" in out or "协访代表覆盖率: 50.0%" in out


def test_bi_query_empty_sample() -> None:
    result = {"results": [{"ok": True, "sample_rows": []}]}
    out = _prettify_output_preview("bi_query", result, None)
    assert "无数据" in out


def test_bi_query_multi_rows() -> None:
    result = {
        "results": [
            {
                "ok": True,
                "sample_rows": [
                    {"DSM岗位负责人": "刘思远", "协访代表覆盖率": 1.0},
                    {"DSM岗位负责人": "陈一鸣(Alex)", "协访代表覆盖率": 0.5},
                ],
            }
        ]
    }
    out = _prettify_output_preview("bi_query", result, None)
    assert "1 查询" in out
    assert "2 行" in out


def test_bi_query_batch_with_failures() -> None:
    result = {
        "results": [
            {"ok": True, "sample_rows": [{"x": 1}]},
            {"ok": False, "error_kind": "domain_mismatch"},
            {"ok": True, "sample_rows": [{"y": 2}]},
        ]
    }
    out = _prettify_output_preview("bi_query", result, None)
    assert "3 查询" in out
    assert "2 成功" in out
    assert "1 出错" in out


# Since e4f261f8e (08-31), bi_concept_resolve's per-token shape is `matches` + `verdict` (ConceptResult);
# the old `candidates` / `kind` keys no longer exist.
def test_bi_concept_resolve_zero_candidates() -> None:
    result = {"results": [{"token": "陈一鸣", "verdict": "not_found", "matches": []}]}
    out = _prettify_output_preview("bi_concept_resolve", result, None)
    assert "陈一鸣" in out
    assert "0 候选" in out
    assert "没找到" in out


def test_bi_concept_resolve_with_candidates() -> None:
    result = {"results": [{"token": "陈一鸣", "verdict": "unique", "matches": [{"value": "陈一鸣(Alex)"}]}]}
    out = _prettify_output_preview("bi_concept_resolve", result, None)
    assert "陈一鸣" in out
    assert "1 候选" in out


def test_bi_fetch_rows_with_size() -> None:
    result = {
        "rows": [{"x": i} for i in range(12)],
        "size_bytes": 2048,
    }
    out = _prettify_output_preview("bi_fetch_rows", result, None)
    assert "12 行" in out
    assert "KB" in out


def test_kb_search() -> None:
    result = {"results": [{"id": "doc1"}, {"id": "doc2"}, {"id": "doc3"}]}
    assert _prettify_output_preview("kb_search", result, None) == "找到 3 条"


def test_kb_list_tables() -> None:
    result = {"tables": [{"id": "t1"}, {"id": "t2"}]}
    assert _prettify_output_preview("kb_list_tables", result, None) == "2 表"


def test_add_chart() -> None:
    result = {"section_id": "sec_abc", "kind": "chart"}
    out = _prettify_output_preview("add_chart", result, None)
    assert "已添加" in out
    assert "chart" in out


def test_python_sandbox_success() -> None:
    result = {"ok": True, "exit_code": 0, "outputs": [{"kind": "stdout"}]}
    out = _prettify_output_preview("python_sandbox", result, None)
    assert "成功" in out
    assert "1 输出" in out


def test_python_sandbox_error() -> None:
    result = {"ok": False, "exit_code": 1, "stderr": "Traceback..."}
    out = _prettify_output_preview("python_sandbox", result, None)
    assert "出错" in out


def test_subagent_spawn() -> None:
    result = {"profile": "compute", "status": "completed", "tool_count": 5}
    assert _prettify_output_preview("subagent_spawn", result, None) == "派 compute"


def test_subagent_spawn_non_completed() -> None:
    result = {"profile": "compute", "status": "failed"}
    out = _prettify_output_preview("subagent_spawn", result, None)
    assert "派 compute" in out
    assert "failed" in out


def test_unknown_tool_fallback() -> None:
    """Unknown tool → truncated raw JSON, ≤ 80 chars."""
    result = {"some_field": "x" * 200}
    out = _prettify_output_preview("brand_new_tool_2030", result, None)
    assert len(out) <= 80


def test_long_summary_truncated() -> None:
    """bi_query with many sub-queries — still ≤80 chars."""
    result = {"results": [{"ok": True, "sample_rows": [{"k": "v" * 100}]} for _ in range(50)]}
    out = _prettify_output_preview("bi_query", result, None)
    assert len(out) <= 80


def test_format_value_currency() -> None:
    from bowhead_core.subagent import _format_value

    # 5_180_339_952 = 51.80 亿 (1 亿 = 10^8)
    assert _format_value(5_180_339_952.4) == "51.80 亿"
    assert _format_value(2_3456.78) == "2.3 万"
    assert _format_value(0.5) == "50.0%"
    assert _format_value(0.123) == "12.3%"


# ──────────────────────────────────────────────────────────────────
# chat-polish-r2 Phase 3 T-2 (2026-05-26) — added prettifier rules
# ──────────────────────────────────────────────────────────────────


def test_skill_list_for_role() -> None:
    """skill_list_for_role returns a list of skill manifests."""
    result = [
        {"name": "analyst-framework", "description": "..."},
        {"name": "analyst-sop-business-review", "description": "..."},
        {"name": "analyst-sop-output-contract", "description": "..."},
    ]
    assert _prettify_output_preview("skill_list_for_role", result, None) == "3 个 skill"
    # also aliased names
    assert _prettify_output_preview("skill_list", result, None) == "3 个 skill"


def test_subagent_return_with_summary() -> None:
    result = {
        "ok": True,
        "__terminal": True,
        "summary": {
            "title": "2025年全年经营情况复盘报告",
            "insights_count": 5,
            "recommendations_count": 4,
        },
    }
    out = _prettify_output_preview("subagent_return", result, None)
    assert "5 洞察" in out
    assert "4 建议" in out


def test_subagent_return_no_summary() -> None:
    assert _prettify_output_preview("subagent_return", {"ok": True}, None) == "结果已交付"


def test_memory_search_hits() -> None:
    result = {"hits": [{"id": "m1"}, {"id": "m2"}]}
    assert _prettify_output_preview("memory_search", result, None) == "找到 2 条记忆"


def test_memory_write_ok_never_shows_the_memory_id() -> None:
    result = {"ok": True, "memory_id": "mem_abc"}
    assert _prettify_output_preview("memory_write", result, None) == "已存入记忆"


def test_memory_write_failed() -> None:
    assert _prettify_output_preview("memory_write", {"ok": False}, None) == "未存入记忆"


def test_remember_summary_is_words_not_the_raw_result() -> None:
    """116 1.5.19: the remember step fell through to raw JSON with the `mem_…` id."""
    result = {"ok": True, "id": "mem_85133efb3d33", "stored": "事实：用户口径：GMV 指含税成交额。", "kind": "rule"}
    out = _prettify_output_preview("remember", result, None)
    assert out == "已存入记忆"
    assert "mem_" not in out and "{" not in out
    assert _prettify_output_preview("remember", result, None, ui_language="en-US") == "Saved to memory"
    assert _prettify_output_preview("remember", {"ok": False, "error": "locked"}, None) == "未存入记忆"


def test_recall_counts_items() -> None:
    assert _prettify_output_preview("recall", {"found": True, "items": [{"id": "mem_1"}]}, None) == "找到 1 条记忆"
    assert _prettify_output_preview("recall", {"found": False, "items": []}, None, ui_language="en-US") == "No matching memories"


def test_bi_ask_question_confidence() -> None:
    result = {"answer": "...", "confidence": 0.87}
    assert _prettify_output_preview("bi_ask_question", result, None) == "置信 0.87"


# 2026-09-05 (tool-inventory): this previously had three summary cases for bi_attribute / bi_get_card / bi_call_llm_async.
# All three tools and their registrations were retired; their summary branches were removed in the same batch, so were these cases
# (without replacing them with assertions that retired tools lack summaries: that would add tests for nonexistent features).
# Still dual-registered, bi_list_measures / bi_list_dimensions share the generic branch covered by the cases below.


def test_bi_list_measures_dict_with_count() -> None:
    assert _prettify_output_preview("bi_list_measures", {"ok": True, "count": 5}, None) == "5 项"


def test_lead_thinking_writing_python_short_code() -> None:
    result = {"code": "import pandas as pd\nprint('hi')", "lang": "python"}
    out = _prettify_output_preview("lead_thinking_writing_python", result, None)
    assert "import pandas" in out


def test_unknown_tool_falls_back_to_preview() -> None:
    """Unknown tool name → truncated raw _preview."""
    out = _prettify_output_preview("some_new_tool", {"foo": "bar"}, None)
    assert "foo" in out  # truncated raw OK


def test_web_search_summary_only_not_zero_success() -> None:
    """D14: a successful web_search returns {summary, results:[]} (DashScope gives a
    sourceless AI summary). It must NOT render as '0 查询 · 0 成功' — that's the bug.
    """
    result = {
        "query": "2025年中国GDP",
        "summary": "2025年中国GDP总量约140.19万亿元，增长5.0%[1][2].",
        "results": [],
    }
    out = _prettify_output_preview("web_search", result, None)
    assert "0 查询" not in out and "0 成功" not in out
    assert out == "已检索"


def test_web_search_with_sources_counts_them() -> None:
    """When the provider does return structured sources, surface the count."""
    result = {
        "query": "q",
        "summary": "answer",
        "results": [{"url": "a"}, {"url": "b"}],
    }
    assert _prettify_output_preview("web_search", result, None) == "已检索 · 2 来源"
