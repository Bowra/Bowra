"""Detect query-period drift from the outgoing text and returned date coverage.

Synthetic cases cover a prior-year fallback and a shortened monthly interval.
The optional parser result may be missing, so it cannot be the sole evidence.
Exercise the current bi_query entry and its compatible flat single-query result.
"""

from __future__ import annotations

import datetime as dt
from datetime import date as _date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    _extract_period,
    _month_coverage_note,
    _period_drift_note,
    _uncomparable_target_note,
    build_default_registry,
)

from bowhead_core import tool_registry as tr

# Synthetic returned filenames preserve the parser formats and date boundaries.
CSV_2026_NOV_REWRITTEN = "2025-11-01_至_30营业额_fixture.csv"  # Requested November 2026, actually queried 2025
CSV_2025_NOV_ECHOED = "2025年11月营业额_fixture.csv"  # Requested November 2025, truthfully echoed
CSV_2026_DEC_REWRITTEN = "2025-12-01_至_31营业额_fixture.csv"
CSV_2026_YEAR_ECHOED = "2026年营业额_fixture.csv"  # ← Actually calculated only January–August, but filename echoed request
CSV_2026_1_12_ECHOED = "2026年1-12月营业额每月_fixture.csv"


def test_extract_period_supported_strings() -> None:
    assert _extract_period("2026年11月 营业额") == (2026, (11,))
    assert _extract_period("2026年1-8月 营业额") == (2026, (1, 2, 3, 4, 5, 6, 7, 8))
    assert _extract_period("2026年 营业额") == (2026, ())
    assert _extract_period(CSV_2026_NOV_REWRITTEN) == (2025, (11,))
    assert _extract_period(CSV_2025_NOV_ECHOED) == (2025, (11,))
    assert _extract_period(CSV_2026_DEC_REWRITTEN) == (2025, (12,))
    # Unrecognized → None (caller fails open)
    assert _extract_period("近7天 营业额") is None
    assert _extract_period("前29天门店营业额排名top_5营业额_fixture.csv") is None
    assert _extract_period("") is None


def test_cross_year_fallback_is_flagged() -> None:
    """★ 最危险的一档：值看着完全正常，只有落盘文件名暴露它换了一年。"""
    note = _period_drift_note("2026年11月 营业额", CSV_2026_NOV_REWRITTEN)
    assert note is not None
    assert "2025" in note and "2026" in note
    assert "不要直接使用" in note  # Criterion must give an action, not just state facts

    note12 = _period_drift_note("2026年12月 营业额", CSV_2026_DEC_REWRITTEN)
    assert note12 is not None and "2025" in note12


def test_no_drift_when_datarc_answered_the_asked_year() -> None:
    """正对照：同一个判据在如实返回时必须闭嘴，否则每条 BI 查询都会挂假警告。"""
    assert _period_drift_note("2025年11月 营业额", CSV_2025_NOV_ECHOED) is None
    assert _period_drift_note("2026年1-8月 营业额", "2026年1-8月营业额_fixture.csv") is None
    assert _period_drift_note("2026年7月 各省 营业额", "2026年7月省营业额_fixture.csv") is None


def test_fail_open_when_either_side_unparseable() -> None:
    assert _period_drift_note("近7天 营业额", CSV_2025_NOV_ECHOED) is None
    assert _period_drift_note("2026年11月 营业额", "前29天门店营业额_fixture.csv") is None
    assert _period_drift_note("2026年11月 营业额", None) is None


def test_documented_blind_spot_scalar_whole_year() -> None:
    """⚠ 这条钉的是**盖不住**的那一档，别把它读成"通过"。

    `2026年 营业额` 实测只算了 1-8 月（与 `2026年1-8月` 逐字节相同），但 datarc
    把文件名原样回显成 `2026年营业额`，且返回单值 ⇒ **没有任何信号**。
    要盖住它得让上游给出实际区间；在此之前这条判据只能沉默。
    """
    assert _period_drift_note("2026年 营业额", CSV_2026_YEAR_ECHOED) is None


def test_month_coverage_truncation_is_flagged() -> None:
    """① 区间截断：文件名回显抓不到，靠返回的月份覆盖抓。"""
    rows = [{"每月": f"2026年{m}月", "营业额 / 元": 1.0} for m in range(1, 9)]  # Only January–August
    note = _month_coverage_note("2026年1-12月 营业额 每月", rows)
    assert note is not None
    assert "9" in note and "12" in note  # Name missing months explicitly
    # Filename check cannot detect this same phenomenon; enforce division of responsibilities.
    assert _period_drift_note("2026年1-12月 营业额 每月", CSV_2026_1_12_ECHOED) is None


def test_month_coverage_silent_when_complete_or_ungrouped() -> None:
    full = [{"每月": f"2026年{m}月"} for m in range(1, 13)]
    assert _month_coverage_note("2026年1-12月 营业额 每月", full) is None
    # No monthly grouping (scalar / other dimensions) ⇒ cannot compare.
    assert _month_coverage_note("2026年1-12月 营业额", [{"营业额 / 元": 1.0}]) is None
    # No range specified ⇒ do not judge.
    assert _month_coverage_note("2026年11月 营业额", [{"每月": "2025年11月"}]) is None


# ── Wiring: warnings must actually reach model, not just be correct in pure function ──
#
# Pure-function tests cannot prove this: `_debug_csv_filename` reaches only backend (`model_view` removes it; batch
# allowlist omits it), so run criterion inside `_fetch_rows` and store it in `_preflight_hint`.


@pytest.mark.asyncio
async def test_cross_year_drift_reaches_preflight_hint() -> None:
    """真机形状：问 2026年11月，datarc 落盘写 2025-11-01_至_30。"""
    fake = MagicMock()
    fake.rows = [{"营业额 / 元": 12345.67}]
    fake.columns = ["营业额 / 元"]
    fake.total_rows = 1
    fake.csv_filename = CSV_2026_NOV_REWRITTEN
    fake.task_uid = "task_1"
    fake.download_ms = 10

    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=fake)

    class _ArtifactStore:
        async def put_bytes(self, **kwargs):
            return SimpleNamespace(id="art_x", filename=kwargs["filename"])

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )
    result = await build_default_registry().get("bi_query").handler({"text": "2026年11月 营业额"}, ctx)

    assert isinstance(result, dict)
    hint = result.get("_preflight_hint") or ""
    assert "2025" in hint, f"跨年警告没走到模型面: {result.keys()}"
    assert "不要直接使用" in hint
    # Same-batch negative control: truthful datarc echo must not warn. Different question text ⇒ no turn-cache reuse.
    fake.csv_filename = CSV_2025_NOV_ECHOED
    ok = await build_default_registry().get("bi_query").handler({"text": "2025年11月 营业额"}, ctx)
    assert not (ok.get("_preflight_hint") or "")


# ══════════════════════════════════════════════════════════════════════════════
# Additions from adversarial review, 2026-08-26. Mutation results for three old guards (change one production point; check test failure):
# - Disconnect `_month_coverage_note` → all green ☠ (no wiring test existed).
# - Make month-mismatch branch never fire → all green ☠ (zero tests for that branch).
# - Reverse want/got in cross-year copy → all green ☠ (asserted only that both years appeared).
# Add cases below for all three and expanded syntax variants.
# ══════════════════════════════════════════════════════════════════════════════

# Synthetic query/filename pairs cover the supported date syntax independently
# of the parser under test. Filenames preserve normalization asymmetries without
# including captured query identifiers or customer values.
TRUTHFUL_PAIRS = [
    # (query, truthfully echoed datarc filename): criterion must remain silent.
    ("2026年7月 营业额", "2026年7月营业额_fixture.csv"),
    ("2026年8月 各门店 营业额", "2026年8月门店营业额_fixture.csv"),
    ("2026年1月-8月 营业额", "2026年1月-8月营业额_fixture.csv"),
    ("2026年6月-12月 营业额 每月", "2026年6月-12月营业额每月_fixture.csv"),
    ("2026年5月-2026年12月 营业额 每月", "2026年5月-2026年12月营业额每月_fixture.csv"),
    ("2024年1月至2026年8月 各月 各品牌 退款率", "2024年1月至2026年8月各月品牌退款率_fixture.csv"),
    ("青岚海洋科技 累计退款 2024年6月到12月 每月", "累计退款2024年6月到12月每月_fixture.csv"),
    ("2025年 与 2026年 营业额 同比", "2025年与营业额同比变化率_fixture.csv"),
    ("2026年7月 vs 2026年6月 营业额", "2026年7月vs2026年6月营业额比较_fixture.csv"),
    ("2026年Q1 营业额", "2026年q1营业额_fixture.csv"),
    ("2026年第一季度 营业额", "2026年第一季度营业额_fixture.csv"),
    ("2026年上半年 营业额", "2026年上半年营业额_fixture.csv"),
    ("2026年下半年 营业额", "2026年下半年营业额_fixture.csv"),
    ("2026年8月1日 至 2026年8月15日 营业额", "2026年8月1日___至___2026年8月15日营业额_fixture.csv"),
    ("2026-08-11 至 2026-08-17 营业额 总量", "2026-08-11_至_2026-08-17营业额_fixture.csv"),
    ("2026年8月 营业额 环比上月", "2026年8月营业额环比变化率_fixture.csv"),
    ("相比 2025年，2026年8月 各省 营业额", "2025年，2026年8月省营业额比较_fixture.csv"),
    ("2025年新店 2026年8月 营业额", "2025年新店_2026年8月营业额_fixture.csv"),
    ("近30天 营业额", "近30天营业额_fixture.csv"),
    ("去年 营业额", "去年营业额_fixture.csv"),
    # ★ English: datarc glues words (`jan` lacks a preceding word boundary), so parsing the two sides is necessarily asymmetric;
    # compare at month level only for ISO filenames to guard this; see `_period_drift_note`.
    ("2026 Jan-Jul revenue", "2026_jan-julexamplebrand营业额品牌_fixture.csv"),
    ("monthly revenue Jan-Dec 2026", "monthlyexamplebrandjan-dec_2026营业额品牌_fixture.csv"),
    ("2026 revenue by month", "2026examplebrandby_month营业额品牌_fixture.csv"),
]

DRIFT_PAIRS = [
    # (query, rewritten datarc filename with explicit ISO range, actual queried year)
    ("2026年11月 营业额", "2025-11-01_至_30营业额_fixture.csv", 2025),
    ("2026年9月 营业额", "2025-09-01_至_30营业额_fixture.csv", 2025),
    ("2026年11月 各省 营业额", "2025-11-01_至_30省营业额_fixture.csv", 2025),
    ("2026年9月-12月 营业额 每月", "2025-09-01_至_12-31营业额每月_fixture.csv", 2025),
    ("2026年第四季度 营业额", "2025-10-01_至_12-31营业额_fixture.csv", 2025),
    ("2026年13月 营业额", "2025-12-01_至_31营业额_fixture.csv", 2025),
    # English month names require the same drift protection as localized periods.
    ("November 2026 revenue", "2025-11-01_至_30examplebrand营业额品牌_fixture.csv", 2025),
    ("revenue for November 2026", "2025-11-01_至_30examplebrand营业额品牌_fixture.csv", 2025),
]


@pytest.mark.parametrize(("query", "filename"), TRUTHFUL_PAIRS)
def test_no_note_on_truthful_pairs(query: str, filename: str) -> None:
    """Truthful synthetic query/filename pairs must not produce a drift warning."""
    assert _period_drift_note(query, filename) is None


@pytest.mark.parametrize(("query", "filename", "actual_year"), DRIFT_PAIRS)
def test_drift_pairs_are_flagged_with_the_right_direction(query: str, filename: str, actual_year: int) -> None:
    """A drift warning identifies the requested and actual years in the correct direction."""
    note = _period_drift_note(query, filename)
    assert note is not None, f"漏报：{query} → {filename}"
    assert f"实际查询的是 **{actual_year} 年**" in note
    assert f"你要的是 {actual_year + 1} 年" in note
    assert "不要直接使用" in note


@pytest.mark.parametrize(("query", "filename", "_y"), DRIFT_PAIRS)
def test_note_never_echoes_the_csv_filename(query: str, filename: str, _y: int) -> None:
    """★ `model_view._BACKEND_ONLY_KEYS` 专门把 `_debug_csv_filename` 从模型面摘掉
    （artifact-addressing 2×2 实测：同一个槽位多一个"看起来也像路径的串"，
    正确率 100% → 85.9%）。这条警告文案不许把同一个串从 `_preflight_hint` 送回去。

    ⚠ 判据必须查**串在不在**，不是"键在不在" —— `test_artifact_model_view.py` 那条
    断言的是 `"_debug_csv_filename" not in out`，换个键把同一个串带回来它照绿。
    """
    note = _period_drift_note(query, filename)
    assert note is not None
    assert filename not in note
    assert ".csv" not in note


def test_extract_period_covers_the_writing_styles_that_actually_show_up() -> None:
    """Equivalent month-range syntaxes resolve to the same covered period."""
    # Four month-range syntaxes must parse into the same set.
    for q in ("2026年6-12月", "2026年6月-12月", "2026年6月到12月", "2026年6月-2026年12月"):
        assert _extract_period(q) == (2026, (6, 7, 8, 9, 10, 11, 12)), q
    # Quarter / half-year (`2026年下半年 营业额` actually queried only July–August)
    assert _extract_period("2026年Q1 营业额") == (2026, (1, 2, 3))
    assert _extract_period("2026年q1营业额_fixture.csv") == (2026, (1, 2, 3))  # datarc persists lowercase
    assert _extract_period("2026年第一季度 营业额") == (2026, (1, 2, 3))
    assert _extract_period("2026年4季度 营业额") == (2026, (10, 11, 12))
    assert _extract_period("2026年上半年 营业额") == (2026, (1, 2, 3, 4, 5, 6))
    assert _extract_period("2026年下半年 营业额") == (2026, (7, 8, 9, 10, 11, 12))
    assert _extract_period("2026年前三季度 营业额") == (2026, tuple(range(1, 10)))
    # English period forms.
    assert _extract_period("November 2026 revenue") == (2026, (11,))
    assert _extract_period("2026 Jan-Jul revenue") == (2026, (1, 2, 3, 4, 5, 6, 7))
    assert _extract_period("monthly revenue Jan-Dec 2026") == (2026, tuple(range(1, 13)))
    assert _extract_period("revenue for 2026") == (2026, ())


def test_extract_period_keeps_the_year_when_the_month_is_out_of_range() -> None:
    """★ 越界月份不能把年份一起丢掉。

    实测 `2026年13月 营业额` 在 116 上真的跨年回退到 2025-12（值与 `2025年12月` 逐字节相同）。
    旧版对 13 整条返 None ⇒ 连"年份说的是 2026"这个能用的信息也没了 ⇒ 沉默。
    """
    assert _extract_period("2026年13月 营业额") == (2026, ())
    assert _extract_period("2026年1-13月 营业额 每月") == (2026, ())
    assert _period_drift_note("2026年13月 营业额", "2025-12-01_至_31营业额_fixture.csv") is not None


def test_iso_range_keeps_its_end_month() -> None:
    """★ `_至_12-31` 里的 "12" 是**月**不是日。旧正则把它当日 ⇒ 结束月被静默丢掉。

    跨年时无害（先比年就返回了），同年改写时会让 ⚠ 文案**报错月份**——比不报更糟。
    这三个串全是 datarc 真机产物形态。
    """
    assert _extract_period("2025-09-01_至_12-31营业额每月_fixture.csv") == (2025, (9, 10, 11, 12))
    assert _extract_period("2025-10-01_至_12-31营业额_fixture.csv") == (2025, (10, 11, 12))
    assert _extract_period("2026-05-28_至_08-25营业额_e3.csv") == (2026, (5, 6, 7, 8))
    # Single-month rewrite (end has day only) is unaffected.
    assert _extract_period("2025-11-01_至_30营业额_fixture.csv") == (2025, (11,))


def test_month_mismatch_branch_only_fires_on_an_iso_filename() -> None:
    """★ ⚠ 月份不一致这条分支之前一行测试都没有（突变成 `if False` 全绿）。

    它只在**文件名是 ISO 显式区间**时才该出声——那是 datarc 真改了日期的唯一形态。
    如实回显时两侧是同一段文字的两次解析，比出来的差异只可能来自解析不对称（纯误报）。
    """
    # ISO rewrite + same year ⇒ warn, with correct direction.
    note = _period_drift_note("2026年1-12月 营业额", "2026-01-01_至_08-31营业额_x.csv")
    assert note is not None
    assert "实际查询的月份是 [1, 2, 3, 4, 5, 6, 7, 8]" in note
    assert "你要的是 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]" in note
    # Non-ISO filename (datarc only echoes/glues words) ⇒ never claim month drift.
    assert _period_drift_note("monthly revenue Jan-Dec 2026", "monthlyexamplebrandjan-dec_2026营业额品牌_1b.csv") is None


def test_fiscal_year_stays_fail_open() -> None:
    """`2026财年` 的财年口径常跨自然年（Apr-Mar）。把它当 2026 年，会在 datarc 如实返回
    `2025-04-01_至_2026-03-31` 时误报「换了一年」⇒ 认不出比认错好。"""
    assert _extract_period("2026财年 营业额") is None
    assert _period_drift_note("2026财年 营业额", "2025-04-01_至_2026-03-31营业额_x.csv") is None


def test_month_coverage_counts_quarter_grouping_too() -> None:
    """`每季度` 分组 + 月区间请求：实测 `2026年1-12月 营业额 每季度` 只返 Q1/Q2/Q3。
    旧版按 `"月" in key` 筛列 ⇒ `每季度` 这列被跳过 ⇒ 沉默。"""
    rows = [{"每季度": f"2026年{q}季度", "营业额 / 元": 1.0} for q in (1, 2, 3)]
    note = _month_coverage_note("2026年1-12月 营业额 每季度", rows)
    assert note is not None
    assert "10" in note and "12" in note
    # All four quarters present ⇒ remain silent.
    assert _month_coverage_note("2026年1-12月 营业额 每季度", [*rows, {"每季度": "2026年4季度"}]) is None


def test_month_coverage_early_exit_does_not_change_the_verdict() -> None:
    """性能修（首行筛列 + 按值记忆 + 凑齐早停）不许改结论。

    116 容器实测：旧版 400k×20 列 0.86 s **同步阻塞事件循环**；改后完整覆盖 0.00 s、
    真缺月 0.04 s。这条钉的是「快了但别快错」。
    """
    full = [{"每月": f"2026年{m}月", "门店": f"店{i}"} for m in range(1, 13) for i in range(50)]
    assert _month_coverage_note("2026年1-12月 各门店 营业额 每月", full) is None
    partial = [r for r in full if _extract_period(r["每月"])[1][0] <= 8]
    note = _month_coverage_note("2026年1-12月 各门店 营业额 每月", partial)
    assert note is not None and "缺 [9, 10, 11, 12]" in note


@pytest.mark.asyncio
async def test_month_coverage_note_reaches_preflight_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 接线测试 —— 之前只有 `_period_drift_note` 有，把 `_month_coverage_note`
    从 `_fetch_rows` 里整条拆掉，47 项测试全绿。纯函数正确证明不了它真被调用。

    ⚠ 冻结「今天」到 2027 年初：本用例问的是「2026年1-12月」，而 `_unfinished_period_note`
    会对含**当前未结束月份**的区间出声——2026 年内跑这条测试时，负对照拿到的 hint 非空
    并不是覆盖度判据漏了，而是另一条判据正确响了。冻住之后 2026 全年皆已完结，
    负对照测的还是它本来要测的那件事。
    """
    monkeypatch.setattr(tr, "model_today", lambda: _date(2027, 1, 15))
    fake = MagicMock()
    fake.rows = [{"每月": f"2026年{m}月", "营业额 / 元": float(m)} for m in range(1, 9)]
    fake.columns = ["每月", "营业额 / 元"]
    fake.total_rows = 8
    fake.csv_filename = CSV_2026_1_12_ECHOED  # Truthful filename echo escapes drift check; only month coverage catches it
    fake.task_uid = "task_2"
    fake.download_ms = 10

    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=fake)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t2",
        datarc=datarc,
        artifact_store=None,
        session_id="s1",
    )
    result = await build_default_registry().get("bi_query").handler({"text": "2026年1-12月 营业额 每月"}, ctx)
    hint = result.get("_preflight_hint") or ""
    assert "缺 [9, 10, 11, 12]" in hint, f"月份覆盖警告没走到模型面: {result.keys()}"

    # Same-batch negative control: all 12 months present ⇒ no warning.
    # ⚠ Change turn_id: bi_query caches per turn (same turn/query reuses results);
    # reusing ctx retrieves preceding truncated result, making the negative control self-confirming.
    fake.rows = [{"每月": f"2026年{m}月", "营业额 / 元": float(m)} for m in range(1, 13)]
    fake.total_rows = 12
    ctx_ok = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t2-neg",
        datarc=datarc,
        artifact_store=None,
        session_id="s1",
    )
    ok = await build_default_registry().get("bi_query").handler({"text": "2026年1-12月 营业额 每月"}, ctx_ok)
    assert not (ok.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_hint_does_not_reintroduce_the_filename_into_the_model_view() -> None:
    """★ 端到端：跑真 handler → 过真投影 → 断言那个文件名串**不在模型面 JSON 里**。

    `model_view` 把 `_debug_csv_filename` 摘掉是有实测依据的决定；这条闸挡的是
    "换个键把同一个串带回来"。判据锚在**串**上，不是键上。
    """
    import json

    from bowhead_core import model_view

    fake = MagicMock()
    fake.rows = [{"营业额 / 元": 12345.67}]
    fake.columns = ["营业额 / 元"]
    fake.total_rows = 1
    fake.csv_filename = CSV_2026_NOV_REWRITTEN
    fake.task_uid = "task_3"
    fake.download_ms = 10

    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=fake)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t3",
        datarc=datarc,
        artifact_store=None,
        session_id="s1",
    )
    out = await build_default_registry().get("bi_query").handler({"text": "2026年11月 营业额"}, ctx)
    assert "2025" in (out.get("_preflight_hint") or "")  # Positive control: warning really attached
    projected = model_view.project_tool_result("bi_query", out)
    assert CSV_2026_NOV_REWRITTEN not in json.dumps(projected, ensure_ascii=False)


# Target windows must be comparable with actual-to-date data.
# Cover future and current-month boundaries, time-grouped exemptions, and the
# preflight hint returned by the real query handler.

_REF_TODAY = dt.date(2026, 9, 15)  # Execution day of the three live replays


def test_full_year_target_without_time_column_is_flagged() -> None:
    """真机原案：这条让 Hospital 缺口 6.65 亿被算成 17.5 亿，分项加总 219%。"""
    note = _uncomparable_target_note(
        "2026年1-12月 一级品牌名称英文=Ozempic 内销指标金额 按渠道",
        ["渠道", "内销指标金额"],
        today=_REF_TODAY,
    )
    assert note is not None
    assert "不能直接和实际值相减" in note
    assert "2026年1-8月" in note, "必须给出可用的替代窗口，不能只说不行"


def test_bare_year_is_flagged_too() -> None:
    """`2026年` 是 ① ② 两条判据都刻意沉默的那一档 —— 但对「能不能做差」这个问题它有答案。"""
    assert _uncomparable_target_note("2026年 Ozempic 内销指标金额 按渠道", ["渠道"], today=_REF_TODAY) is not None


def test_window_ending_in_the_current_month_fires() -> None:
    """止于当月同样不可比：目标是整月的，实际只到今天。放宽成 `<=` 时这条转红。"""
    assert _uncomparable_target_note("2026年1-9月 内销指标金额 按渠道", ["渠道"], today=_REF_TODAY) is not None


def test_single_month_gets_a_single_month_suggestion() -> None:
    """建议要保持同一个问题：问 9 月就建议 8 月，不能把单月问题换成 1-8 月区间。"""
    note = _uncomparable_target_note("2026年9月 内销指标金额 按渠道", ["渠道"], today=_REF_TODAY)
    assert note is not None
    assert "2026年8月" in note
    assert "1-8月" not in note


def test_complete_window_stays_silent() -> None:
    """负对照：窗口在上一个完整月收口 ⇒ 可比 ⇒ 必须闭嘴。"""
    assert _uncomparable_target_note("2026年1-8月 内销指标金额 按渠道", ["渠道"], today=_REF_TODAY) is None


def test_time_grouped_result_is_exempt() -> None:
    """有 `每月` 列 ⇒ 模型能自己裁到完整月 ⇒ 不归本判据（归 `_month_coverage_note`）。"""
    assert _uncomparable_target_note("2026年1-12月 内销指标金额 每月", ["每月", "内销指标金额"], today=_REF_TODAY) is None


def test_actual_measure_is_not_flagged() -> None:
    """只取实际数时，窗口覆盖到当月完全正常 —— 误伤这条会天天误报。"""
    assert _uncomparable_target_note("2026年1-12月 内销销售金额 按渠道", ["渠道"], today=_REF_TODAY) is None


def test_unparseable_period_fails_open() -> None:
    assert _uncomparable_target_note("近30天 内销指标金额 按渠道", ["渠道"], today=_REF_TODAY) is None
    assert _uncomparable_target_note("内销指标金额 按渠道", ["渠道"], today=_REF_TODAY) is None


def test_the_other_two_notes_are_structurally_blind_here() -> None:
    """钉住分工：同一条输入，① ② 都无能为力，所以 ③ 不是重复造轮子。

    - `_month_coverage_note` 的输入是返回行；`按渠道` 没有时间列 ⇒ 它必须 fail-open
    - `_period_drift_note` 比的是请求 vs 实际执行；窗口合法且如实执行 ⇒ 它正确地沉默
    """
    q = "2026年1-12月 一级品牌名称英文=Ozempic 内销指标金额 按渠道"
    rows = [{"渠道": "Hospital", "内销指标金额": 4.36e9}, {"渠道": "E-commerce", "内销指标金额": 1.21e9}]
    assert _month_coverage_note(q, rows) is None
    assert _period_drift_note(q, "2026年1-12月内销指标金额渠道_fixture.csv") is None
    assert _uncomparable_target_note(q, ["渠道", "内销指标金额"], today=_REF_TODAY) is not None


@pytest.mark.asyncio
async def test_uncomparable_target_note_reaches_preflight_hint() -> None:
    """接线：纯函数对不算数，警告必须真的落进 `_preflight_hint` 才到得了模型面。

    窗口用「今年 1-12 月」构造 —— 无论今天是几月，(年,12) 都不早于 (今年,本月)，恒触发。
    """
    year = dt.date.today().year
    fake = MagicMock()
    fake.rows = [{"渠道": "Hospital", "内销指标金额": 4.36e9}]
    fake.columns = ["渠道", "内销指标金额"]
    fake.total_rows = 1
    fake.csv_filename = f"{year}年1-12月内销指标金额渠道_fixture.csv"
    fake.task_uid = "task_tgt"
    fake.download_ms = 10

    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=fake)

    class _ArtifactStore:
        async def put_bytes(self, **kwargs):
            return SimpleNamespace(id="art_tgt", filename=kwargs["filename"])

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_tgt",
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s_tgt",
    )
    reg = build_default_registry()
    result = await reg.get("bi_query").handler({"text": f"{year}年1-12月 内销指标金额 按渠道"}, ctx)
    hint = (result or {}).get("_preflight_hint") or ""
    assert "不能直接和实际值相减" in hint, f"③ 没走到模型面: {list((result or {}).keys())}"

    # Same-batch negative control: actual-value metrics must not receive this warning (different question text ⇒ no turn-cache reuse).
    fake.csv_filename = f"{year}年1-12月内销销售金额渠道_fixture.csv"
    ok = await reg.get("bi_query").handler({"text": f"{year}年1-12月 内销销售金额 按渠道"}, ctx)
    assert "不能直接和实际值相减" not in ((ok or {}).get("_preflight_hint") or "")
