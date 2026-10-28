"""`period_growth.compare_growth` —— 增长 / 同比排名的可比口径。

依据：116 同题两跑结论相反（`t_239e8b6cf3dc` 年初至今比全年；`t_6acd16abddee` 月度斜率把季节性当趋势、
靠没过完的 9 月造出唯一「正增长」），排名里还混进负增长、极小品类和基数为 0 的 inf。
夹具刻意让错口径与对口径分道扬镳：两年逐月形状相同（强季节性）、本年最后一个月没过完。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pg = _load("_period_growth_under_test", "period_growth.py")

SEASON = [20, 17, 15, 13, 12, 11, 10, 10, 12, 13, 15, 18]
# This year relative to the same month last year: A +10%, B +5%, C -10%.
FACTOR = {"A": 1.10, "B": 1.05, "C": 0.90}


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    prior, cur = [], []
    for m in range(1, 13):
        for g, f in FACTOR.items():
            prior.append((f"2025年{m}月", g, SEASON[m - 1] * 100.0))
            if m <= 9:
                v = SEASON[m - 1] * 100.0 * f
                cur.append((f"2026年{m}月", g, v * (0.2 if m == 9 else 1)))  # Only a few days of September have elapsed.
        prior.append((f"2025年{m}月", "零头", 1.0))
        if m <= 9:
            cur.append((f"2026年{m}月", "零头", 5.0))
            cur.append((f"2026年{m}月", "新品", 300.0))
    cols = ["月份", "品类", "营业额"]
    return pd.DataFrame(cur, columns=cols), pd.DataFrame(prior, columns=cols)


def test_same_period_yoy_excludes_incomplete_month_and_ignores_prior_full_year() -> None:
    cur, prior = _frames()
    r = pg.compare_growth([cur, prior], value="营业额", period="月份", group="品类", as_of="2026-09-26")
    assert r.method == "yoy_same_period"
    assert (r.current_label, r.base_label) == ("2026年1–8月", "2025年1–8月")
    assert r.excluded_periods == ["2026年9月"]
    # Comparing YTD with a full year suggests a sharp overall decline; comparable-period YoY weights A/B/C at 1/3 ⇒ (10+5-10)/3 ≈ +1.7%.
    abc = sum(SEASON[:8]) * 100.0
    expected = (abc * (1.10 + 1.05 + 0.90) + 8 * 5 + 8 * 300 - (abc * 3 + 8)) / (abc * 3 + 8)
    assert r.total_growth == pytest.approx(expected)
    assert [t["name"] for t in r.top] == ["A", "B"]
    assert r.top[0]["growth"] == pytest.approx(0.10)


def test_ranking_never_pads_with_decliners_tiny_groups_or_zero_base() -> None:
    cur, prior = _frames()
    r = pg.compare_growth([cur, prior], value="营业额", period="月份", group="品类", top_n=3, as_of="2026-09-26")
    assert [t["name"] for t in r.top] == ["A", "B"]  # Only two are growing; do not add C as a third.
    # "零头" grows +400% but is too small to rank; still count it among growing categories.
    assert r.positive_count == 3 and r.ranked_count == 4
    status = dict(zip(r.table["品类"], r.table["状态"], strict=True))
    assert status["零头"] == pg.STATUS_TINY  # +400%, but negligible size.
    assert status["新品"] == pg.STATUS_NEW  # Last year was 0; do not emit inf.
    assert not r.table["增长率"].isin([float("inf")]).any()
    assert "只有 2 个品类在增长" in r.summary
    assert "4 个有基期数据的品类中 3 个正增长" in r.summary
    assert "2026年9月" in r.summary


def test_no_positive_group_says_so() -> None:
    cur, prior = _frames()
    cur = cur.assign(营业额=cur["营业额"] * 0.1)
    r = pg.compare_growth([cur, prior], value="营业额", period="月份", group="品类", as_of="2026-09-26")
    assert r.top == [] and r.positive_count == 0
    assert "没有任何品类正增长" in r.summary


def test_without_prior_year_it_compares_adjacent_windows_and_flags_seasonality() -> None:
    cur, _ = _frames()
    r = pg.compare_growth(cur, value="营业额", period="月份", group="品类", as_of="2026-09-26")
    assert r.method == "recent_vs_previous"
    assert (r.current_label, r.base_label) == ("2026年6–8月", "2026年3–5月")
    assert "无法排除季节性" in r.summary


def test_daily_data_uses_its_own_last_date_as_cutoff() -> None:
    """日粒度数据截止到月中 = 那个月没过完，哪怕今天早已过了月底。"""
    days = pd.date_range("2025-01-01", "2026-03-15", freq="D")
    df = pd.DataFrame({"日期": days, "营业额": 1.0})
    r = pg.compare_growth(df, value="营业额", as_of="2026-09-26")
    assert r.excluded_periods == ["2026年3月"]
    assert (r.current_label, r.base_label) == ("2026年1–2月", "2025年1–2月")
    assert r.total_growth == pytest.approx(0.0)


def test_separate_year_column_and_yyyymm_ints() -> None:
    df = pd.DataFrame({"年": ["2025年", "2025年", "2026年", "2026年"], "月": ["1月", "2月", "1月", "2月"], "v": [10, 10, 11, 12]})
    r = pg.compare_growth(df, value="v", period="月", year="年", as_of="2026-09-26")
    assert r.total_growth == pytest.approx(0.15)
    df2 = pd.DataFrame({"ym": [202501, 202601], "v": [10, 12]})
    assert pg.compare_growth(df2, value="v", period="ym", as_of="2026-09-26").total_growth == pytest.approx(0.2)


def test_runner_stages_period_growth_helper(tmp_path: Path) -> None:
    runner = _load("_runner_stage_pg", "runner.py")
    runner._stage_sandbox_helpers(tmp_path)
    assert "def compare_growth" in (tmp_path / "period_growth.py").read_text(encoding="utf-8")


def test_long_tail_sizes_do_not_mark_normal_groups_as_tiny() -> None:
    """116 真数据：一个大类占四成，按占全体比例划线会排除 38 个里的 21 个。按中位数判只排真零头。"""
    rows = []
    sizes = {"大": 4000.0, **{f"中{i}": 10.0 + i for i in range(10)}, "零头": 0.001}
    for y, f in ((2025, 1.0), (2026, 1.01)):
        for m in range(1, 4):
            rows += [(f"{y}年{m}月", g, v * f) for g, v in sizes.items()]
    df = pd.DataFrame(rows, columns=["月份", "品类", "营业额"])
    r = pg.compare_growth(df, value="营业额", period="月份", group="品类", as_of="2026-09-26")
    tiny = r.table.loc[r.table["状态"] == pg.STATUS_TINY, "品类"].tolist()
    assert tiny == ["零头"]
    assert r.positive_count == 12 and r.ranked_count == 12


def test_micro_changes_are_called_flat() -> None:
    rows = [(f"{y}年1月", g, 100.0 * (1.005 if y == 2026 and g == "A" else 1)) for y in (2025, 2026) for g in ("A", "B")]
    df = pd.DataFrame(rows, columns=["月份", "品类", "营业额"])
    r = pg.compare_growth(df, value="营业额", period="月份", group="品类", as_of="2026-09-26")
    assert "基本持平" in r.summary
    assert "差异不显著" in r.summary


def test_every_total_carries_its_own_period() -> None:
    """合计数旁边必须写着它的时段——模型抄数时只抄那一行（116 把 1–8 月合计标成了 1–9 月）。"""
    cur, prior = _frames()
    r = pg.compare_growth([cur, prior], value="营业额", period="月份", group="品类", as_of="2026-09-26")
    line = next(ln for ln in r.summary.splitlines() if ln.startswith("整体"))
    assert f"2026年1–8月合计 {r.total_current:,.0f}" in line
    assert f"2025年1–8月合计 {r.total_base:,.0f}" in line
    top_line = next(ln for ln in r.summary.splitlines() if "在增长" in ln or "增长最快" in ln)
    assert "2026年1–8月比2025年1–8月" in top_line
