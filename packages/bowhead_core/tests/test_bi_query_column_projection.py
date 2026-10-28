"""Keep complete query results when a requested column cannot be resolved.

Synthetic columns cover exact matches, unique containment, and ambiguity.
Projection must not hide a real dimension just because its requested label differs.
"""

from __future__ import annotations

from bowhead_core.tool_registry import _resolve_keep_cols


class TestResolveKeepCols:
    def test_exact_match(self) -> None:
        got, missing, amb = _resolve_keep_cols(["代表姓名", "销售金额"], ["代表姓名", "销售金额"])
        assert got == ["代表姓名", "销售金额"]
        assert not missing and not amb

    def test_unique_containment_resolves_the_real_case(self) -> None:
        """★ 本 sprint 的正主：`季度` 要能对上 `每季度`。"""
        got, missing, amb = _resolve_keep_cols(
            ["代表姓名", "季度", "销售金额"],
            ["每季度", "代表姓名", "销售金额"],
        )
        assert got == ["代表姓名", "每季度", "销售金额"]
        assert not missing and not amb

    def test_exact_wins_over_containment(self) -> None:
        """Exact matching takes precedence over broader containment candidates."""
        actual = ["销售数量", "LY销售数量", "标准销售数量"]
        got, missing, amb = _resolve_keep_cols(["销售数量"], actual)
        assert got == ["销售数量"]
        assert not missing and not amb

    def test_ambiguous_containment_is_reported_not_guessed(self) -> None:
        """多个候选时**不猜**：宁可不投影多给几列，也不能给错列。"""
        got, missing, amb = _resolve_keep_cols(["销售"], ["销售数量", "销售金额"])
        assert not got
        assert not missing
        assert amb and amb[0][0] == "销售"
        assert set(amb[0][1]) == {"销售数量", "销售金额"}

    def test_genuinely_missing_is_reported(self) -> None:
        got, missing, amb = _resolve_keep_cols(["不存在的列"], ["代表姓名"])
        assert not got and missing == ["不存在的列"] and not amb

    def test_normalization_handles_case_and_width(self) -> None:
        got, _, _ = _resolve_keep_cols(["ｇｍｖ"], ["GMV"])
        assert got == ["GMV"]


class TestSummaryTellsTheTruth:
    """step 卡不能拿投影后 + 截断后的数当真相报。"""

    def test_truncated_and_projected_shows_both_numbers(self) -> None:
        from bowhead_core.subagent import _prettify_output_preview

        s = _prettify_output_preview(
            "bi_query",
            {"rows": [{}] * 30, "columns": ["a", "b"], "total_rows": 4180, "_columns_before_projection": 3},
            None,
            ui_language="zh-CN",
        )
        assert "4180" in s, "全量行数被吞了 —— 用户会以为只有 30 行"
        assert "原 3 列" in s, "投影没说明 —— 用户会以为数据只有 2 列"

    def test_normal_case_stays_terse(self) -> None:
        """正对照：没截断也没投影时**不要**加噪音。"""
        from bowhead_core.subagent import _prettify_output_preview

        s = _prettify_output_preview(
            "bi_query",
            {"rows": [{}] * 30, "columns": ["a", "b", "c"], "total_rows": 30, "_columns_before_projection": 3},
            None,
            ui_language="zh-CN",
        )
        assert s == "30 行 · 3 列"

    def test_english_locale(self) -> None:
        from bowhead_core.subagent import _prettify_output_preview

        s = _prettify_output_preview(
            "bi_query",
            {"rows": [{}] * 30, "columns": ["a", "b"], "total_rows": 4180, "_columns_before_projection": 3},
            None,
            ui_language="en-US",
        )
        assert "4180" in s and "projected from 3" in s
