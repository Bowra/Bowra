"""名称兜底的语料 —— 从 `bowhead_bi_bridge/tests/test_unit_type_inference.py` 移植。

推断逻辑 2026-08-25 从数据源 client 搬到了 `bowhead_core.units`（源只吐原生字段，
「这算不算钱」是跨源判断）。语料跟着搬，覆盖不随重构丢。

断言走 `to_unit_type(infer_from_name(...))`，即**最终产出的那个串**，跟旧文件同口径。
"""

from __future__ import annotations

import pytest

from bowhead_core import units


def _guess(name: str | None) -> str | None:
    return units.to_unit_type(units.infer_from_name(name))


class TestCurrency:
    @pytest.mark.parametrize(
        "name",
        [
            "营业额",
            "本月营收",
            "Q3 销售额",
            "净利润",
            "总成本",
            "Total Revenue",  # Also contains the count word "total"; the currency family must win.
            "monthly sales",
            "Q1 income",
            "GMV",
            "客户营业额",
            "广告费收入",
        ],
    )
    def test_cny(self, name: str) -> None:
        assert _guess(name) == "CNY"


class TestPercent:
    @pytest.mark.parametrize(
        "name",
        [
            "增长率",
            "毛利率",
            "市场占比",
            "同比增长率",
            "growth rate",
            "conversion ratio",
            "Q3 percentage",
            "完成度比例",
        ],
    )
    def test_percent(self, name: str) -> None:
        assert _guess(name) == "percent"


class TestCount:
    @pytest.mark.parametrize(
        "name",
        ["订单数量", "用户人数", "活跃用户次数", "总订单数", "user count", "num_orders", "total customers"],
    )
    def test_count(self, name: str) -> None:
        assert _guess(name) == "count"


class TestDuration:
    def test_days(self) -> None:
        assert _guess("平均订单周期天数") == "duration_days"
        assert _guess("avg days") == "duration_days"

    def test_hours(self) -> None:
        assert _guess("响应小时数") == "duration_hours"
        assert _guess("processing hours") == "duration_hours"


class TestUnknown:
    @pytest.mark.parametrize("name", [None, "", "未分类指标", "random_measure"])
    def test_returns_none(self, name: str | None) -> None:
        assert _guess(name) is None


class TestPriority:
    def test_rate_beats_money_when_both_present(self) -> None:
        """★旧实现这里是**错的**，而且原作者知道。

        旧文件写的是 `assert result in ("CNY", "percent")  # tolerate, document
        trade-off`，还留了句「M3 可调整 keyword 顺序若实测错率高」——货币族先判，
        `营收增长率` 被判成钱。搬家时把顺序修了（`利润` ⊂ `利润率` 是同一族问题），
        所以这里改成严格断言。
        """
        assert _guess("营收增长率") == "percent"
        assert _guess("利润率") == "percent"
        # Avoid false positives in the other direction: amount names without ratio terms still denote currency.
        assert _guess("净利润") == "CNY"
