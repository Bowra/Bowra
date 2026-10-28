"""请求区间含**尚未结束的当期** → 取数时就告诉模型，别让它把半个月当成一个月。

取证（09-10 116 dogfood `s_ff7b38fe79ca`）：问「今年各品类表现」，取 1-9 月而当天是 9 月 10 号。
产品把半个月的 9 月与完整月份直接比，写出「9月环比骤降 60%-76%、季节性收缩显著」，做成 KPI 卡，
据此给了采购建议。09-19 复跑量出量级：含半个月 9 月 → 全年同比 -29.7%「系统性下滑」；排除后 +0.08%。

⚠ 所有用例都**冻结「今天」**：拿真实日期写断言的话，这份测试下个月会自己变红
（同族教训见 `feedback_baseline_metric_must_survive_the_change`）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.model_clock import viewer_clock_scope
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_core import tool_registry as tr
from bowhead_router import model_clock as model_clock_module


@pytest.fixture()
def today_0910(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tr, "model_today", lambda: date(2026, 9, 10))


@pytest.mark.usefixtures("today_0910")
@pytest.mark.parametrize(
    "query",
    [
        "2026年1-9月 按种类 营业额",  # Final month of the range = current month.
        "2026年 各品类 营业额",  # Year only ⇒ YTD, extending through today.
        "2026年7-9月 各门店 销售额",  # Quarter includes the current month.
        "2026年1-12月 各省 营业额",  # Full-year requests also include the current month.
    ],
)
def test_range_covering_the_current_month_is_flagged(query: str) -> None:
    note = tr._unfinished_period_note(query)
    assert note is not None, f"{query} 含未结束的 9 月，必须提示"
    assert "9月" in note and "2026-09-10" in note


@pytest.mark.usefixtures("today_0910")
@pytest.mark.parametrize(
    "query",
    [
        "2026年1-8月 按种类 营业额",  # All months have ended.
        "2025年1-12月 各省 营业额",  # Previous year
        "2026年10月 各省 营业额",  # Future months are outside this criterion.
        "各省 营业额",  # No specified time ⇒ fail open; do not guess.
        "",
    ],
)
def test_finished_or_unparseable_ranges_are_silent(query: str) -> None:
    assert tr._unfinished_period_note(query) is None


def test_last_day_of_month_still_counts_as_unfinished(monkeypatch: pytest.MonkeyPatch) -> None:
    """月末当天也算没完：当日数据通常还没入完，这里宁可多提示一次。"""
    monkeypatch.setattr(tr, "model_today", lambda: date(2026, 9, 30))
    assert tr._unfinished_period_note("2026年1-9月 按种类 营业额") is not None


def test_month_coverage_and_unfinished_period_are_different_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """9 个月一个不少 ⇒ 覆盖度判据沉默；但尾月只过了一半 ⇒ 本判据必须出声。

    两条判据各管一件事，这条测试防的是「以为已经有人管了」。
    """
    monkeypatch.setattr(tr, "model_today", lambda: date(2026, 9, 10))
    q = "2026年1-9月 按种类 营业额 每月"
    rows = [{"每月": f"2026年{m}月", "营业额": 1} for m in range(1, 10)]
    assert tr._month_coverage_note(q, rows) is None, "月份一个不缺，覆盖度判据本就不该响"
    assert tr._unfinished_period_note(q) is not None


class _ArtifactStore:
    async def put_bytes(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id="art_1", filename=kwargs["filename"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("today_0910")
async def test_note_reaches_the_model_via_preflight_hint() -> None:
    """接线测试：判据写对了但没挂进 `preflight_notes`，模型一个字也看不到。"""
    dl = MagicMock()
    dl.rows = [{"种类": "外套", "营业额": 1}]
    dl.columns = ["种类", "营业额"]
    dl.total_rows = 1
    dl.csv_filename = "rows.csv"
    dl.task_uid = "task"
    dl.download_ms = 10
    dl.explain_json = None
    dl.explain_message = None

    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut unavailable"))
    datarc.download_data = AsyncMock(return_value=dl)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t-unfinished-period",  # ⚠ Unique across files: bi_query has a process-wide, turn-scoped cache.
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )
    out = await build_default_registry().get("bi_query").handler({"queries": ["2026年1-9月 按种类 营业额"]}, ctx)
    assert "尚未结束" in out["_preflight_hint"]


# The advice reads the model clock, so a viewer turn judges "the current month" on the
# viewer's calendar. 2026-09-30T20:00Z is still September in Los Angeles but already
# October 1 in Shanghai: September is unfinished for one viewer and complete for the other.
_MONTH_EDGE = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)


@pytest.fixture()
def frozen_month_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return _MONTH_EDGE.astimezone(tz) if tz is not None else _MONTH_EDGE.replace(tzinfo=None)

    monkeypatch.setattr(model_clock_module, "datetime", _Frozen)


@pytest.mark.usefixtures("frozen_month_edge")
def test_unfinished_period_advice_follows_the_viewer_calendar() -> None:
    with viewer_clock_scope("America/Los_Angeles"):
        note = tr._unfinished_period_note("2026年9月 各品类 营业额")
        assert note is not None and "今天 2026-09-30" in note
    with viewer_clock_scope("Asia/Shanghai"):
        assert tr._unfinished_period_note("2026年9月 各品类 营业额") is None


@pytest.mark.usefixtures("frozen_month_edge")
def test_target_window_advice_follows_the_viewer_calendar() -> None:
    query, cols = "2026年9月 内销指标金额 按渠道", ["渠道"]
    with viewer_clock_scope("America/Los_Angeles"):
        note = tr._uncomparable_target_note(query, cols)
        assert note is not None and "而今天是 2026年9月30日" in note
    with viewer_clock_scope("Asia/Shanghai"):
        assert tr._uncomparable_target_note(query, cols) is None


@pytest.mark.usefixtures("frozen_month_edge")
def test_advice_without_a_viewer_zone_uses_the_deploy_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "8")  # 2026-10-01 04:00 at +8
    assert tr._unfinished_period_note("2026年9月 各品类 营业额") is None
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "-7")  # 2026-09-30 13:00 at -7
    assert tr._unfinished_period_note("2026年9月 各品类 营业额") is not None
