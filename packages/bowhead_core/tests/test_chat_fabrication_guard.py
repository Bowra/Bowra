"""Check chat prose against tool evidence using synthetic numeric examples."""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.analysis_payload import (
    build_observed_numbers,
    is_chat_text_fabricated,
    text_fabrication_ratio,
)


def _trace_observed() -> set[float]:
    # Synthetic tool rows provide the grounding values.
    return build_observed_numbers(
        [
            {
                "rows": [
                    {"H1销售金额": "76.21亿", "同比": "4.1%"},
                    {"Q1销售金额": "40.31亿", "Q1同比": "4.4%"},
                    {"CV产品线": "26.46亿", "CV同比": "4.7%"},
                ]
            },
        ]
    )


def test_fabricated_chat_is_flagged() -> None:
    observed = _trace_observed()
    # Synthetic unsupported claims deliberately differ from every observed value.
    fab = (
        "2026年上半年整体销售金额同比增长 8.2%，达成率 94.3%。"
        "广东、江苏、浙江三省贡献了全国近 35% 的销售额。"
        "Franchise A 贡献了 42% 的营收且同比增长 12.5%，Franchise C 达成率仅 78%。"
    )
    assert is_chat_text_fabricated(fab, observed) is True
    unmatched, total = text_fabrication_ratio(fab, observed)
    assert unmatched >= 2 and unmatched / total >= 0.5


def test_grounded_chat_not_flagged() -> None:
    observed = _trace_observed()
    grounded = "2026年H1销售金额 76.21 亿元，同比增长 4.1%；Q1 实现 40.31 亿元（+4.4%）。"
    assert is_chat_text_fabricated(grounded, observed) is False


def test_no_observed_never_flags() -> None:
    # cannot diagnose without tool numbers → never punish
    assert is_chat_text_fabricated("增长 8.2%，达成率 94.3%", set()) is False


def test_year_yoy_prose_not_false_flagged() -> None:
    # years (2024/2025/2026) are skipped; one grounded data point present.
    observed = build_observed_numbers([{"v": "4.1%"}])
    assert is_chat_text_fabricated("2024、2025、2026 三年对比，2026 同比增长 4.1%", observed) is False


def test_cited_grounded_answer_not_flagged() -> None:
    # review fix: _resolve_citations keeps [cite:N] literally; the bare index N
    # must NOT be extracted as a number, else a cited answer false-trips.
    observed = build_observed_numbers([{"a": "4.1%", "b": "3.2%"}])
    cited = "华东 4.1% [cite:1], 华南 3.2% [cite:2]."
    unmatched, total = text_fabrication_ratio(cited, observed)
    assert total == 2, "cite indices 1/2 must be stripped, only 4.1/3.2 counted"
    assert unmatched == 0
    assert is_chat_text_fabricated(cited, observed) is False


def test_single_unmatched_below_floor_not_flagged() -> None:
    observed = build_observed_numbers([{"a": "100", "b": "50"}])
    # 100 + 50 grounded, 150 derived (sum) → 1/3 unmatched, below floor of 2
    assert is_chat_text_fabricated("100 加 50 等于 150", observed) is False


# A single fabricated absolute value must still be detected.
def _repo_observed() -> set[float]:
    """真机形状：团队工具返回的 GitHub 仓库 JSON（data 是嵌套 JSON 字符串）。"""
    return build_observed_numbers(
        [
            {
                "ok": True,
                "status": 200,
                "data": '{"stargazers_count":128321,"forks_count":21211,"language":"Python","open_issues_count":11541}',
            }
        ]
    )


def test_single_fabricated_absolute_number_is_flagged() -> None:
    """One unsupported absolute value is enough to reject a short factual claim."""
    assert is_chat_text_fabricated("这个仓库目前有 **约 28,000+ star**，主语言为 TypeScript。", _repo_observed()) is True


def test_grounded_single_absolute_number_not_flagged() -> None:
    """同样只有一个数字，但取自工具结果 → 不得误伤。"""
    assert is_chat_text_fabricated("该仓库有 128,321 个 star。", _repo_observed()) is False


def test_single_derived_ratio_not_flagged() -> None:
    """单个**比率**对不上仍放行——推导出来的增长率/占比是原下限要保护的场景，
    本次放宽只针对绝对数，不得把它一起收进来。"""
    assert is_chat_text_fabricated("同比增长约 12%。", _repo_observed()) is False


# Narrative numbers require the same grounding checks as the final payload.
# Synthetic conflicting values must trigger supersession of the streamed prose.


def _synthetic_observed() -> set[float]:
    """Synthetic regression statistics with varied numeric precision."""
    return {62.937, 1487.576, 0.78, 6.165, 0.000012, 3165.758, 2747.954, 3583.562}


def test_synthetic_narrative_restatement_is_flagged() -> None:
    narrative = "三个核心结论已经精确算出：**斜率 b = 50.629 箱/月**，模型对月份变化的拟合优度 **R² = 0.841**，说明超过 94% 的销量变异由时间趋势解释。明年 1 月（x=13）的点预测销量约为 **3,101 箱**，95% 预测区间为 **[2,652, 3,451] 箱**。"
    assert is_chat_text_fabricated(narrative, _synthetic_observed()) is True


def test_synthetic_grounded_answer_not_flagged() -> None:
    """A faithful summary of the synthetic statistics remains grounded."""
    grounded = "对该产品今年1-12月月销量拟合一元线性回归，结果显示明显上升趋势：斜率 b = 62.937 箱/月，R² = 0.780（拟合优度良好）。斜率 t 统计量 = 6.165，p = 0.000012。预测明年1月（x=13）销量点估计为 3165.758 箱，95% 预测区间为 [2747.954, 3583.562] 箱。"
    assert is_chat_text_fabricated(grounded, _synthetic_observed()) is False


# Both guards collect delegated results after dispatch moves temporary nested
# invocations into invocation.nested. Valid rounded values must remain grounded.


def _spawn_inv_with_nested_sandbox() -> SimpleNamespace:
    """委派轮的真实形状：Lead 只有一个 subagent_spawn，真数据在 inv.nested 里。"""
    nested = SimpleNamespace(
        result={"ok": True, "result_json": {"stdout": "斜率 b = 62.937\nR² = 0.770\n"}},
    )
    return SimpleNamespace(
        tool_name="subagent_spawn",
        # The spawn envelope has numbers **outside** the analytical results, making observed nonempty;
        # removing nested traversal then makes check_fabrication_back_reference actually compare values
        # (empty observed returns [] early, making the test falsely pass).
        result={"subagent_id": "sub_x", "ok": True, "duration_ms": 1979},
        nested=[nested],
    )


def test_collect_walks_nested_when_ctx_field_already_delattred() -> None:
    """ctx 字段已被 delattr（spawn 收口后的常态）→ 仍须从 inv.nested 取到真数字。"""
    from bowhead_core.agents.lead_orchestrator import collect_fabrication_tool_results

    result = SimpleNamespace(tool_invocations=[_spawn_inv_with_nested_sandbox()])
    ctx = SimpleNamespace()  # Dispatch already moved results into nested invocations.
    observed = build_observed_numbers(collect_fabrication_tool_results(result, ctx))
    assert 62.937 in observed
    assert 0.77 in observed


def test_correct_kpi_not_flagged_on_delegated_turn() -> None:
    """回归钉子：委派轮里**正确**的 KPI 不得被标成「未找到对应来源」。"""
    from bowhead_core.agents.lead_orchestrator import collect_fabrication_tool_results
    from bowhead_core.analysis_payload import check_fabrication_back_reference

    result = SimpleNamespace(tool_invocations=[_spawn_inv_with_nested_sandbox()])
    observed = build_observed_numbers(collect_fabrication_tool_results(result, SimpleNamespace()))
    payload = {"kpis": [{"label": "斜率", "value": "62.9 箱/月"}, {"label": "R²", "value": "0.770"}]}
    assert check_fabrication_back_reference(payload, observed) == []


def test_still_reads_ctx_field_when_spawn_not_yet_closed() -> None:
    """spawn 尚未收口的窗口里，ctx 字段仍是唯一来源 —— 不能因为改走 nested 就丢掉它。"""
    from bowhead_core.agents.lead_orchestrator import collect_fabrication_tool_results

    result = SimpleNamespace(tool_invocations=[])
    ctx = SimpleNamespace(_last_nested_invocations=[SimpleNamespace(result={"v": "斜率 b = 62.937"})])
    observed = build_observed_numbers(collect_fabrication_tool_results(result, ctx))
    assert 62.937 in observed


# Significant-figure matching distinguishes faithful rounding from changed values.
# Synthetic close-but-different numbers must not pass a broad relative tolerance.


def _synthetic_payload() -> dict:
    return {
        "kpis": [
            {"label": "斜率", "value": "62.9 箱/月"},
            {"label": "R²", "value": "0.770"},
            {"label": "预测", "value": "3,166 箱"},
            {"label": "区间", "value": "[2,748, 3,584]"},
        ],
        "exec_summary": "月均增长约63箱，R²=0.770。预测明年1月约3166箱，95%预测区间[2748, 3584]箱。",
    }


_DRIFTED_NARRATIVE = "斜率 b =63.885，月均增长约64箱。R² =0.767。明年1月预测约为3,131箱，95%预测区间为 [2,733,3,528]箱。"


def test_restatement_drift_catches_the_close_drift_case() -> None:
    """The restatement guard catches small changes that the broader guard tolerates."""
    from bowhead_core.analysis_payload import narrative_restates_payload_incorrectly

    assert narrative_restates_payload_incorrectly(_DRIFTED_NARRATIVE, _synthetic_payload()) is True


def test_old_guard_alone_would_miss_the_close_drift_case() -> None:
    """The broader numeric guard alone does not reject this close synthetic drift."""
    observed = build_observed_numbers([{"v": "62.9 0.770 3166 2748 3584 12 95"}])
    assert is_chat_text_fabricated(_DRIFTED_NARRATIVE, observed) is False


def test_faithful_restatement_does_not_fire() -> None:
    """忠实复述（原值 / 各档有效数字）不得误伤。"""
    from bowhead_core.analysis_payload import narrative_restates_payload_incorrectly

    p = _synthetic_payload()
    for txt in (
        "斜率约 63 箱/月，R² = 0.770，预测明年1月约 3166 箱，区间 [2748, 3584]。",
        "月均增长约 63 箱，预测约 3200 箱。",  # Coarse 2sf rounding.
        "本轮共分析 12 个月的数据，覆盖 4 个季度。",  # No reference to authoritative values.
    ):
        assert narrative_restates_payload_incorrectly(txt, p) is False, txt


def test_single_drift_below_threshold_does_not_fire() -> None:
    """单个数字落窗内又不是四舍五入，可能只是撞上别的量 —— 不足以开火。"""
    from bowhead_core.analysis_payload import narrative_restates_payload_incorrectly

    assert narrative_restates_payload_incorrectly("预测约 3131 箱。", _synthetic_payload()) is False


def test_drift_ignores_numbers_far_from_any_authoritative_value() -> None:
    """超出 10% 指涉窗口 = 另一个量，不算复述失真（归旧判据管）。"""
    from bowhead_core.analysis_payload import narrative_restatement_drift, payload_authoritative_numbers

    auth = payload_authoritative_numbers(_synthetic_payload())
    assert narrative_restatement_drift("总成本 58000 元，人力 4200 元。", auth) == []


def test_faithful_rounding_covers_significant_figures_and_decimals() -> None:
    from bowhead_core.analysis_payload import _is_faithful_rounding

    for stated in (62.937, 63.0, 62.9, 62.94, 60.0):
        assert _is_faithful_rounding(stated, 62.937) is True, stated
    for stated in (63.885, 62.5, 64.0):
        assert _is_faithful_rounding(stated, 62.937) is False, stated
    assert _is_faithful_rounding(0.77, 0.770) is True
    assert _is_faithful_rounding(0.767, 0.770) is False
