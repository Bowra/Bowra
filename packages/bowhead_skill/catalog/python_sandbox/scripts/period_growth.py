"""「谁增长最快 / 同比 / 增长排名」的口径 helper —— python_sandbox 沙箱内可 import（仅 pandas）。

2026-09-25 116 同题两跑结论相反（`t_239e8b6cf3dc` / `t_6acd16abddee`），两个主结论都是口径产物：
① 年初至今比上一年**全年**（两年各自 `groupby().sum()`）；② 月度序列拟合斜率——把季节性当趋势，
且让一个没过完的月份决定了唯一的「正增长」；排名里还混进了负增长、极小品类和基数为 0 的 inf。
这些都不是模型不会算，是每次都要自己重新想一遍口径，想漏一处结论就反过来。

所以把口径做成结构：`compare_growth()` 自己决定可比时段，调用方不用、也没法再选错——

- **不完整期间一律剔除后再算**（期末晚于 `as_of` / 数据截止日的月份），并在结论里写明剔除了哪几个月；
- 有上一年数据时**只用同期同比**：本年完整月份 × 上一年相同月份，绝不拿年初至今比全年；
- 没有上一年时退回「近 k 月 vs 前 k 月」，并明写「无去年同期、无法排除季节性」；
- 「增长最快」只在**正增长**里排；基数为 0 的不算增长率（单列为「新增」）；规模占比低于
  `min_size_vs_median` 的单列为「规模过小」不参与排名（只是不排名，照样计入「几个在涨」）；
  正增长不足 N 个就只给几个，并如实说出来；变化不到 ±2% 的写明「基本持平」。

沙箱隔离：runner.py 运行时把本文件 copy 进 workdir，user 代码 `from period_growth import compare_growth`
即用。**不 import bowhead_core**（沙箱够不到），逻辑自足。
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

_PERIOD_NAME_HINTS = ("月份", "月", "日期", "时间", "期间", "年月", "date", "month", "period", "time")
_CN_YM_RE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})")
_YYYYMM_RE = re.compile(r"^(\d{4})(\d{2})$")

STATUS_OK = "ok"
STATUS_NEW = "基数为0"
STATUS_TINY = "规模过小"
STATUS_NO_DATA = "无数据"
#: Same threshold as compute G2.6: |change| < 2% is essentially flat, not significant growth or decline.
MICRO_DELTA = 0.02


@dataclass
class GrowthResult:
    """`compare_growth` 的返回。`summary` 给人读（直接 print），`table` 可 `to_csv` 后 `add_chart`。"""

    method: str
    current_label: str
    base_label: str
    excluded_periods: list[str]
    total_base: float
    total_current: float
    total_growth: float | None
    table: pd.DataFrame
    top: list[dict[str, Any]]
    positive_count: int
    ranked_count: int
    group_count: int
    notes: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "current_period": self.current_label,
            "base_period": self.base_label,
            "excluded_periods": list(self.excluded_periods),
            "total_base": self.total_base,
            "total_current": self.total_current,
            "total_growth": self.total_growth,
            "positive_count": self.positive_count,
            "ranked_count": self.ranked_count,
            "group_count": self.group_count,
            "top": list(self.top),
            "notes": list(self.notes),
        }


def compare_growth(
    data: pd.DataFrame | list[pd.DataFrame],
    *,
    value: str,
    period: str | None = None,
    group: str | None = None,
    year: str | None = None,
    top_n: int = 3,
    as_of: dt.date | str | None = None,
    min_size_vs_median: float = 0.01,
    recent_months: int = 3,
) -> GrowthResult:
    """按可比口径算各组增长并排名。

    Args:
        data: 一张长表（每行 = 某期 × 某组），或几张同列名的表（如今年一份、去年一份）——会先拼起来。
        value: 数值列名（如营业额）。
        period: 期间列名（日期 / 年月 / 「2026年1月」/ 202601 均可）。不传就按列名和值自动找。
        group: 分组列名（如品类）。不传只算总体。
        year: 年份单独成列、期间列只有「1月」这种不带年份的值时，传年份列名。
        top_n: 「增长最快」要几个。正增长不够 N 个时只返回实际有的。
        as_of: 截止日，默认今天。期末晚于它的月份视为没过完、剔除。
        min_size_vs_median: 某组两期合计不到「各组两期合计的中位数」的这个比例 → 「规模过小」，不参与排名。
            按中位数而不按占全体比例：品类规模常是长尾（一个大类占四成），按占比划线会把
            大多数正常品类当成零头（116 真数据：0.1% 占比线排除了 38 个里的 21 个）。
        recent_months: 没有上一年数据时，比较「近 k 月 vs 前 k 月」的 k。
    """
    import pandas as pd

    frames = data if isinstance(data, list) else [data]
    df = pd.concat([f for f in frames if f is not None], ignore_index=True)
    if value not in df.columns:
        raise KeyError(f"{value!r} 不在列里；真实列名：{list(df.columns)}")
    if group is not None and group not in df.columns:
        raise KeyError(f"{group!r} 不在列里；真实列名：{list(df.columns)}")
    period = period or _detect_period_column(df, exclude={value, group, year})
    if period is None or period not in df.columns:
        raise KeyError(f"找不到期间列，请用 period= 指定；真实列名：{list(df.columns)}")

    as_of_date = _as_date(as_of) or dt.date.today()
    raw_period = df[period]
    if year is not None:
        if year not in df.columns:
            raise KeyError(f"{year!r} 不在列里；真实列名：{list(df.columns)}")
        raw_period = df[year].astype(str).str.extract(r"(\d{4})")[0] + "年" + df[period].astype(str)
    work = pd.DataFrame(
        {
            "_ts": _parse_periods(raw_period),
            "_v": _to_number(df[value]),
            "_g": df[group].astype(str) if group else "合计",
        }
    )
    work = work.dropna(subset=["_ts"])
    if work.empty:
        raise ValueError(f"期间列 {period!r} 解析不出任何日期；示例值：{df[period].head(3).tolist()}")
    work["_v"] = work["_v"].fillna(0.0)

    daily = bool((work["_ts"].dt.day != 1).any())
    data_end = work["_ts"].max().date() if daily else None
    cutoff = min(as_of_date, data_end) if data_end else as_of_date
    work["_ym"] = work["_ts"].dt.year * 100 + work["_ts"].dt.month
    months = sorted(work["_ym"].unique().tolist())
    complete = [m for m in months if _month_end(m) <= cutoff]
    excluded = [m for m in months if m not in complete]
    if not complete:
        raise ValueError(f"没有已经过完的完整月份（截止 {cutoff.isoformat()}），无法比较增长")

    latest_year = max(m // 100 for m in complete)
    cur_months = [m for m in complete if m // 100 == latest_year]
    prior_months_avail = {m for m in complete if m // 100 == latest_year - 1}
    same_period_prior = [m - 100 for m in cur_months if (m - 100) in prior_months_avail]
    notes: list[str] = []

    if same_period_prior:
        method = "yoy_same_period"
        base_months = same_period_prior
        cur_used = [m + 100 for m in same_period_prior]
        if len(cur_used) < len(cur_months):
            missing = [_ym_label(m - 100) for m in cur_months if m not in cur_used]
            notes.append(f"去年缺 {'、'.join(missing)} 的数据，这几个月不进同比")
    else:
        method = "recent_vs_previous"
        k = max(1, min(recent_months, len(complete) // 2))
        if len(complete) < 2:
            raise ValueError("完整月份不足 2 个且没有去年同期数据，无法比较增长")
        cur_used = complete[-k:]
        base_months = complete[-2 * k : -k]
        notes.append("没有去年同期数据，只能比较相邻时段，无法排除季节性——结论只能说「近期较前期」，不能说成趋势性增长或下滑")

    base_agg = work[work["_ym"].isin(base_months)].groupby("_g")["_v"].sum()
    cur_agg = work[work["_ym"].isin(cur_used)].groupby("_g")["_v"].sum()
    groups = sorted(set(base_agg.index) | set(cur_agg.index))
    table = pd.DataFrame({"_g": groups})
    table["基期"] = table["_g"].map(base_agg).fillna(0.0)
    table["本期"] = table["_g"].map(cur_agg).fillna(0.0)
    table["变化额"] = table["本期"] - table["基期"]
    sizes = table["基期"] + table["本期"]
    median_size = float(sizes[sizes > 0].median()) if (sizes > 0).any() else 0.0

    def _status(row: Any) -> str:
        if row["基期"] == 0 and row["本期"] == 0:
            return STATUS_NO_DATA
        if row["基期"] <= 0:
            return STATUS_NEW
        if median_size > 0 and (row["基期"] + row["本期"]) < median_size * min_size_vs_median:
            return STATUS_TINY
        return STATUS_OK

    table["状态"] = table.apply(_status, axis=1)
    table["增长率"] = [(r["变化额"] / r["基期"]) if r["基期"] > 0 else float("nan") for _, r in table.iterrows()]
    has_base = table[table["状态"].isin([STATUS_OK, STATUS_TINY])]
    ok = table[table["状态"] == STATUS_OK]
    positive = ok[ok["增长率"] > 0].sort_values(["增长率", "变化额", "_g"], ascending=[False, False, True])
    table["增长排名"] = table["_g"].map({g: i + 1 for i, g in enumerate(positive["_g"])}).astype("Int64")
    table = table.sort_values(["增长排名", "增长率"], ascending=[True, False], na_position="last").reset_index(drop=True)
    table = table.rename(columns={"_g": group or "范围"})

    total_base = float(table["基期"].sum())
    total_cur = float(table["本期"].sum())
    total_growth = (total_cur - total_base) / total_base if total_base > 0 else None
    top = [
        {"name": str(r["_g"]), "growth": float(r["增长率"]), "base": float(r["基期"]), "current": float(r["本期"])}
        for _, r in positive.head(max(0, int(top_n))).iterrows()
    ]

    res = GrowthResult(
        method=method,
        current_label=_range_label(cur_used),
        base_label=_range_label(base_months),
        excluded_periods=[_ym_label(m) for m in excluded],
        total_base=total_base,
        total_current=total_cur,
        total_growth=total_growth,
        table=table,
        top=top,
        positive_count=int((has_base["增长率"] > 0).sum()),
        ranked_count=int(len(has_base)),
        group_count=int((table["状态"] != STATUS_NO_DATA).sum()),
        notes=notes,
    )
    res.summary = _render_summary(res, table, group=group, top_n=top_n, cutoff=cutoff)
    return res


def _render_summary(res: GrowthResult, table: pd.DataFrame, *, group: str | None, top_n: int, cutoff: dt.date) -> str:
    gcol = group or "范围"
    kind = "同期同比" if res.method == "yoy_same_period" else "近期 vs 前期（非同比）"
    lines = [f"[period_growth] 口径：{kind}，本期 {res.current_label}，对比 {res.base_label}。"]
    if res.excluded_periods:
        lines.append(f"已剔除未过完的期间 {'、'.join(res.excluded_periods)}（截止 {cutoff.isoformat()}），不参与任何计算。")
    flat = " —— 变化不到 ±2%，应表述为基本持平" if res.total_growth is not None and abs(res.total_growth) < MICRO_DELTA else ""
    # Put each period beside its value: on 116, `t_67c48a17a6a8` labeled this January–August total as January–September in the answer.
    # The first line stated the comparison basis, but copied values lost their periods. Do not rely on rechecking; keep periods with values.
    lines.append(
        f"整体（{res.current_label} 比 {res.base_label}）：{_fmt_pct(res.total_growth)}"
        f"（{res.base_label}合计 {_fmt_num(res.total_base)} → {res.current_label}合计 {_fmt_num(res.total_current)}）{flat}。"
    )
    if group:
        lines.append(f"{res.ranked_count} 个有基期数据的{gcol}中 {res.positive_count} 个正增长。")
        if not res.top and res.positive_count == 0:
            lines.append(f"没有任何{gcol}正增长——不存在「增长最快」的{gcol}，请如实这样说。")
        elif not res.top:
            lines.append(f"只有规模过小的{gcol}在增长，可排名的{gcol}里没有正增长——请如实这样说，别把零头说成增长最快。")
        else:
            names = "；".join(f"{t['name']} {_fmt_pct(t['growth'])}" for t in res.top) + f"（均为{res.current_label}比{res.base_label}）"
            if len(res.top) < top_n:
                lines.append(f"只有 {len(res.top)} 个{gcol}在增长（要的是前 {top_n}，不要用负增长凑数）：{names}。")
            else:
                lines.append(f"增长最快的 {len(res.top)} 个：{names}。")
            if max(t["growth"] for t in res.top) < MICRO_DELTA:
                lines.append("排名靠前的增幅也都不到 2%，差异不显著——应说「各品类基本持平」，别把排名当成明显增长。")
        for status, why in ((STATUS_NEW, "基数为 0，增长率无意义，不参与排名"), (STATUS_TINY, "规模过小，不参与排名")):
            names = table.loc[table["状态"] == status, gcol].astype(str).tolist()
            if names:
                shown = "、".join(names[:8]) + (f" 等 {len(names)} 个" if len(names) > 8 else "")
                lines.append(f"{why}：{shown}。")
    lines.extend(res.notes)
    return "\n".join(lines)


def _detect_period_column(df: pd.DataFrame, *, exclude: set[Any]) -> str | None:
    cols = [c for c in df.columns if c not in exclude]
    for c in cols:
        name = str(c).lower()
        if any(h in name for h in _PERIOD_NAME_HINTS) and _parse_periods(df[c].head(20)).notna().any():
            return c
    for c in cols:
        if _parse_periods(df[c].head(20)).notna().mean() >= 0.8:
            return c
    return None


def _parse_periods(s: pd.Series) -> pd.Series:
    import pandas as pd

    def one(v: Any) -> Any:
        if v is None or (isinstance(v, float) and v != v):
            return pd.NaT
        if isinstance(v, (dt.date, pd.Timestamp)):
            return pd.Timestamp(v)
        text = str(v).strip()
        if isinstance(v, (int, float)) or text.isdigit():
            m = _YYYYMM_RE.match(text.split(".")[0])
            if m and 1 <= int(m.group(2)) <= 12:
                return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
            return pd.NaT
        m = _CN_YM_RE.search(text)
        if m and 1 <= int(m.group(2)) <= 12:
            day = re.search(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})", text)
            if day:
                try:
                    return pd.Timestamp(int(day.group(1)), int(day.group(2)), int(day.group(3)))
                except ValueError:
                    pass
            return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
        try:
            return pd.Timestamp(text)
        except (ValueError, TypeError):
            return pd.NaT

    return pd.to_datetime(s.map(one), errors="coerce")


def _to_number(s: pd.Series) -> pd.Series:
    import pandas as pd

    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")


def _as_date(v: Any) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _month_end(ym: int) -> dt.date:
    y, m = divmod(ym, 100)
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def _ym_label(ym: int) -> str:
    return f"{ym // 100}年{ym % 100}月"


def _range_label(months: list[int]) -> str:
    if not months:
        return "（无）"
    first, last = min(months), max(months)
    if first == last:
        return _ym_label(first)
    if first // 100 == last // 100:
        return f"{first // 100}年{first % 100}–{last % 100}月"
    return f"{_ym_label(first)}–{_ym_label(last)}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "无法计算（基数为 0）"
    pct = v * 100
    # Keep two decimals below 1% so -0.04% does not become "-0.0%", obscuring both direction and magnitude.
    return f"{pct:+.2f}%" if abs(pct) < 1 else f"{pct:+.1f}%"


def _fmt_num(v: float) -> str:
    return f"{v:,.0f}"
