"""数值单位的规范形 —— 一套词表，各数据源映射进来，各渲染面投影出去。

单位知识此前每个渲染面各存一套（marker 的 `CNY → 元`、行内数字的分支、前端的
`¥/$/€` 前缀 + 名称族），而**推断**只长在某一个数据源的 client 里，别的源够不着。
接第二个源就得重写一遍。

这里只做两件事：

- **入口** `resolve()` —— 数据源适配器把自己的原生字段（货币代码 / 数值形状 /
  单位标签）喂进来，拿到 `UnitSpec`。字段按语义命名，不按任何一个源的字段名。
- **出口** `to_unit_type()` / `to_chart_column()` —— 各渲染面按自己的表示法投影。
  同一个事实两种投影：图表面要符号 `¥`，marker 面要 `CNY`。

新接一个源只需要提供原生字段，不写第二套推断；新增一个渲染面只需要加一个投影函数。

## 一个必须记住的坑

`unit_type == "percent"` 在渲染面隐含「原始值是 fraction」——前端 `formatCell` 的
percent 分支是无条件 `v * 100`。所以 points 形状的百分比（2.89 就是 2.89%）**绝不能**
标成 `percent`，否则渲成 289%。`to_unit_type` 只让 fraction 走 `percent`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bowhead_core.chart.numeric_display import currency_identity

UnitKind = Literal["currency", "percent", "count", "duration", "plain"]
PercentScale = Literal["fraction", "points"]


@dataclass(frozen=True)
class UnitSpec:
    """一个数值的单位语义（与任何数据源、任何渲染面无关）."""

    kind: UnitKind
    code: str = ""
    """货币 ISO 代码（`CNY` / `USD`），仅 kind == "currency" 有值."""
    percent_scale: PercentScale | None = None
    """百分比落库形状。`fraction`：0.83 表示 83%；`points`：13.5 表示 13.5%。
    **光看值推不出来**——达成率 1.02 是 fraction（102%）、市场份额 2.45 是 points
    （2.45%），值域重叠、含义相反，所以只能来自元数据或名称族。"""
    label: str = ""
    """数据源自带的显示标签（`元` / `人次` / `%`）；duration 用它存粒度（days/hours）."""
    source: Literal["metadata", "name_guess"] = "metadata"
    """出处。`metadata` = 数据源的真字段；`name_guess` = 按名字猜的。
    留着是为了能回答「这个单位是查出来的还是蒙的」——闸可以断言「有元数据时不许走猜」。"""


# ─────────────────────────────────────────────────────────────
# Input.
# ─────────────────────────────────────────────────────────────
def from_source_metadata(
    *,
    currency_code: str = "",
    value_format: str = "",
    unit_name: str = "",
) -> UnitSpec | None:
    """把数据源的原生元数据规范化；一个字段都没有则 None.

    Args:
        currency_code: 货币 ISO 代码。
        value_format: 数值形状。`"percentage"` 约定为**原始值是 fraction、显示时 ×100**
            （BI 源普遍这么定义；2026-08-24 在一个真实例上实测确认：标 percentage 的
            指标返回 value=0.17109、显示 "17.11%"）。
        unit_name: 人填的单位标签。
    """
    ccy = (currency_code or "").strip().upper()
    if currency_identity(ccy):
        return UnitSpec(kind="currency", code=ccy)

    if (value_format or "").strip().lower() == "percentage":
        return UnitSpec(kind="percent", percent_scale="fraction")

    label = (unit_name or "").strip()
    if label:
        # Manually entered `%` means already scaled (otherwise source uses format=percentage) ⇒ points.
        if label in ("%", "％"):
            return UnitSpec(kind="percent", percent_scale="points", label=label)
        return UnitSpec(kind="plain", label=label)

    return None


# Name fallback: **last tier**, only without real metadata.
# ⚠ A guess, not a fact. Retain because many real-deployment metrics lack metadata and would otherwise lose units entirely.
# "Money term ⇒ CNY" is an **assumption** ("收入" implies money, not a country's currency);
# multicurrency deployments must supply correct source currency codes, not rely on this table.
_NAME_KEYWORDS: tuple[tuple[tuple[str, ...], UnitKind, str], ...] = (
    # ⚠ Order is correctness: `利润` is a substring of `利润率`; currency-first would swallow
    # monetary-root ratios (`利润率` → CNY). Percent family must precede currency; reverse matching is safe since no currency term
    # is a substring of a percent-family term.
    # Include "份额" here, not just currency suffix "额": shares are ratios, not money.
    # Positive control caught adding generic "额": `市场份额` became currency:CNY,
    # collapsing "营业额 + 市场份额" from two unit families to one, hiding share on the shared axis.
    # ⚠ 116 corpus had no `份额` column; **real-data measurements missed this**, so construct adversarial cases.
    # `pct` is **the model's most common** percent suffix (116 corpus: `同比增速_pct` / `QoQ_pct` /
    # `MoM_pct` / `YoY_pct` / `Margin_pct` …), previously unrecognized here but recognized by frontend `format.ts`
    # `PERCENT_TOKENS`: **two vocabularies for one concept, opposite judgments**.
    # （[[feedback_one_concept_many_implementations]]）。
    #
    # Measured false positives (116: 3015 chart specs, 1072 unique numeric column names; compare real function before/after):
    # of 70 `pct` names, 33 already percent; **37 names / 65 column occurrences changed after addition,
    # all manually checked as actual percentages; zero false positives**.
    # ⚠ **9 previously classified `currency`** (`营业额同比_pct` / `gmv_mom_pct` /
    # `Revenue_YoY_pct` / `Profit_YoY_pct` …), swallowed by generic money suffixes "额 / gmv / revenue /
    # profit". Percent mistaken for money is worst: it shares a monetary axis,
    # flattened nearly to zero, worse than plain. Another instance of "order is correctness":
    # percent before currency, so adding it here restores the right classification.
    #
    # Frontend uses **token** matching (`_pct` has no `\b`, see `format.ts:152`); here retain
    # existing substring style: zero false positives on 1072 real names; token matching would disagree with other terms
    # in this table. Tighten only upon an actual `xxpctxx` false positive.
    (("率", "比例", "占比", "份额", "rate", "ratio", "percentage", "share", "pct", "%"), "percent", ""),
    (
        # "额" is a **generic** money suffix, more robust than enumerating `金额/营业额/销售额`: real corpus
        # `退款额` / `环比变化额` / `单笔退款额` were all missed, becoming plain and joining a different unit family
        # from monetary `利润`, making the chart "three dimensions" and refusing dual axes.
        # Measured false positives (116: 349 unique numeric names): only 9 changed, **all real money**,
        # zero false positives. `份额` unaffected: percent matches first, demonstrating "order is correctness"
        # again.
        ("额", "金额", "营收", "营业额", "销售额", "收入", "利润", "成本", "revenue", "sales", "income", "profit", "cost", "gmv"),
        "currency",
        "CNY",
    ),
    (("数量", "次数", "个数", "人数", "订单数", "件数", "count", "num_", "total"), "count", ""),
    (("天数", "days"), "duration", "days"),
    (("小时", "hours"), "duration", "hours"),
)


def infer_from_name(name: str | None) -> UnitSpec | None:
    """按名字猜单位；猜不出返回 None.

    宁可不标也不要标错：下游把单位当作**机器断言的事实**，猜错的单位比没有单位更糟。
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    for keywords, kind, extra in _NAME_KEYWORDS:
        if not any(k in n for k in keywords):
            continue
        if kind == "percent":
            # Names identify percent, not fraction vs points: leave scale None;
            # downstream infers from value range/name family; do not decide here.
            return UnitSpec(kind="percent", percent_scale=None, source="name_guess")
        if kind == "currency":
            return UnitSpec(kind="currency", code=extra, source="name_guess")
        if kind == "duration":
            return UnitSpec(kind="duration", label=extra, source="name_guess")
        return UnitSpec(kind=kind, source="name_guess")
    return None


def resolve(
    name: str | None = None,
    *,
    currency_code: str = "",
    value_format: str = "",
    unit_name: str = "",
) -> UnitSpec | None:
    """元数据优先、名称兜底。数据源适配器的标准入口."""
    spec = from_source_metadata(
        currency_code=currency_code,
        value_format=value_format,
        unit_name=unit_name,
    )
    return spec if spec is not None else infer_from_name(name)


# ─────────────────────────────────────────────────────────────
# Output.
# ─────────────────────────────────────────────────────────────
def to_unit_type(spec: UnitSpec | None) -> str | None:
    """投影成 `unit_type` 字符串（行内数字 / marker 面用）.

    ⚠ 见模块 docstring：`"percent"` 隐含 fraction。points 故意退回裸 `%` 标签，
    走「未知单位 + 后缀」那条路，不会被任何渲染面 ×100。
    """
    if spec is None:
        return None
    if spec.kind == "currency":
        return spec.code or None
    if spec.kind == "percent":
        if spec.percent_scale == "points":
            return spec.label or "%"
        # fraction or unknown scale uses percent (unknown retains historical behavior).
        return "percent"
    if spec.kind == "duration":
        return f"duration_{spec.label or 'days'}"
    if spec.kind == "count":
        return "count"
    return spec.label or None


# Currencies the platform **recognizes**: single source of truth.
# Each renderer has its own table (marker CNY→元, inline-number zh_map, frontend symbol prefixes);
# all **must** cover every code here, or that currency falls through to unknown unit:
# markers render 11-digit raw numbers such as `7016893124HKD` (citation_marks
# `_CURRENCY_SCALE_UNITS` comment records this production bug).
# test_units_contract.py compares each currency across three render paths, preventing divergence.
KNOWN_CURRENCIES = frozenset({"CNY", "USD", "EUR", "JPY", "GBP", "HKD", "TWD", "KRW", "SGD"})

_CURRENCY_SYMBOLS = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "JPY": "¥",
    "GBP": "£",
    "HKD": "HK$",
    "TWD": "NT$",
    "KRW": "₩",
    "SGD": "S$",
}


def to_chart_column(spec: UnitSpec | None) -> dict[str, str]:
    """投影成 ChartColumn 的字段补丁（图表面用）.

    前端只把 `¥/$/€` 当前缀，拿到 `"CNY"` 会渲成 `12,345CNY`，所以这里给**符号**
    不是代码——同一个事实两种投影，正是本模块存在的理由。

    只返回**有把握**的键；调用方按「空位才填」合并，不覆盖产出方已声明的值。
    ⚠ 不返回 `format="percent"`：前端另有一条 `formatCell` 对 percent 无条件 ×100，
    points 列被标上就渲成 289%。形状只走 `percent_scale`（`formatPercentCell` 会按它
    决定要不要放大），不动 format。
    """
    if spec is None:
        return {}
    if spec.kind == "currency":
        return {"format": "currency", "unit": _CURRENCY_SYMBOLS.get(spec.code, spec.code), "currency_code": spec.code}
    if spec.kind == "percent":
        return {"percent_scale": spec.percent_scale} if spec.percent_scale else {}
    if spec.kind == "duration":
        return {}
    return {"unit": spec.label} if spec.label else {}
