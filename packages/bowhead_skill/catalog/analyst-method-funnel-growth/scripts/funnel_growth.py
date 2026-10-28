"""Funnel-Growth deterministic analysis — python_sandbox template.

analyst-method-funnel-growth reads this (skill_read ref) and runs it in OUR python_sandbox
to offload conversion/decomposition computation + threshold flagging OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body + a `data=...` line + the print>",
                                    "inputs": {"funnel_data": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py, and downloads each input to a file path. So fill `data` either:
  (a) INLINE from BI scalars (funnel_stages list + users/revenue scalars — paste them), or
  (b) from an input artifact: read INPUTS['funnel_data']['path'] (JSON/CSV BI output).

Outputs structured JSON to stdout — metrics + flags + explicit data gaps. The LLM reads it back
and writes the narrative + payload risk_flags; it does NOT recompute ratios by hand.

Local test only: `python funnel_growth.py '{"funnel_stages":[{"name":"visit","count":10000},...], ...}'` (argv path is a dev convenience,
NOT how the sandbox invokes it).
"""

from __future__ import annotations

import json
from typing import Any


def _conversion_rate(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _growth_rate(end: float | None, begin: float | None) -> float | None:
    if end is None or begin in (None, 0):
        return None
    return (end - begin) / begin


def run_funnel_growth(data: dict[str, Any]) -> dict[str, Any]:
    """
    Inputs (from bi_fetch or inline):
    - funnel_stages: list[{"name": str, "count": float}] — ordered stages (visit, register, activate, purchase, repurchase)
    - users: int — current period active users
    - users_prior: int — prior period active users
    - new_users: int (optional) — newly acquired users
    - churned_users: int (optional) — churned users
    - revenue: float — current period revenue
    - revenue_prior: float — prior period revenue
    - arpu: float (optional) — current average revenue per user
    - arpu_prior: float (optional) — prior ARPU
    - avg_price: float (optional) — average selling price current
    - avg_price_prior: float (optional) — prior avg price
    - avg_units_per_user: float (optional) — units per user current
    - avg_units_prior: float (optional) — units per user prior
    - high_price_product_ratio: float (optional) — % of revenue from high-price SKUs current
    - high_price_product_ratio_prior: float (optional) — prior ratio
    """
    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}

    # === Check 1: Funnel Conversion Rates & Bottleneck ===
    stages = data.get("funnel_stages") or []
    if len(stages) >= 2:
        conversions = []
        for i in range(len(stages) - 1):
            stage_a = stages[i]
            stage_b = stages[i + 1]
            if stage_a["count"] and stage_b["count"]:
                cr = _conversion_rate(stage_b["count"], stage_a["count"])
                if cr is not None:
                    conversions.append(
                        {
                            "from": stage_a["name"],
                            "to": stage_b["name"],
                            "rate": round(cr, 4),
                            "count_from": stage_a["count"],
                            "count_to": stage_b["count"],
                        }
                    )
        metrics["funnel_conversions"] = conversions

        # Find bottleneck (lowest conversion rate)
        if conversions:
            bottleneck = min(conversions, key=lambda x: x["rate"])
            metrics["bottleneck"] = f"{bottleneck['from']} → {bottleneck['to']}"
            metrics["bottleneck_rate"] = bottleneck["rate"]
            if bottleneck["rate"] < 0.3:  # <30% is critical
                flags.append(
                    {
                        "level": "high",
                        "check": "漏斗短板",
                        "value": f"{bottleneck['from']} → {bottleneck['to']}: {bottleneck['rate']:.1%}",
                        "note": "转化率<30% — 该环节严重制约增长",
                    }
                )
            elif bottleneck["rate"] < 0.5:  # <50% is medium
                flags.append(
                    {
                        "level": "medium",
                        "check": "漏斗短板",
                        "value": f"{bottleneck['from']} → {bottleneck['to']}: {bottleneck['rate']:.1%}",
                        "note": "转化率<50% — 重点优化环节",
                    }
                )
    else:
        gaps.append("缺漏斗各阶段计数 — 转化率无法验证")

    # === Check 2: User Growth Attribution (New vs Churn) ===
    users = data.get("users")
    users_prior = data.get("users_prior")
    new_users = data.get("new_users")
    churned_users = data.get("churned_users")

    user_growth = _growth_rate(users, users_prior)
    if user_growth is not None:
        metrics["user_growth_rate"] = round(user_growth, 4)

    if users is not None and users_prior is not None:
        user_delta = users - users_prior
        metrics["user_delta_absolute"] = user_delta
        if new_users is not None and churned_users is not None:
            # Sanity check: Δ users ≈ new - churned
            new_users - churned_users
            implied_churn_rate = (churned_users / users_prior) if users_prior else None
            metrics["new_users"] = new_users
            metrics["churned_users"] = churned_users
            metrics["implied_churn_rate"] = round(implied_churn_rate, 4) if implied_churn_rate else None
            if implied_churn_rate and implied_churn_rate > 0.15:  # >15% churn is concerning
                flags.append(
                    {"level": "medium", "check": "用户流失", "value": f"流失率 {implied_churn_rate:.1%}", "note": "流失率>15% — 新增拉新增长压力大"}
                )
            # Check if growth is driven by new users or just slowing churn
            if new_users > 0 and user_delta > 0:
                growth_quality = round(new_users / (user_delta + churned_users), 4)  # new vs total inflow
                metrics["growth_quality"] = growth_quality
        else:
            gaps.append("缺新增/流失用户数 — 用户归因无法详细拆分")
    else:
        gaps.append("缺当期/同期用户数 — 用户增速无法验证")

    # === Check 3: ARPU Decomposition (Price × Units × Mix) ===
    revenue = data.get("revenue")
    revenue_prior = data.get("revenue_prior")
    arpu = data.get("arpu")
    arpu_prior = data.get("arpu_prior")

    if revenue is not None and users is not None and users > 0:
        implied_arpu = round(revenue / users, 2)
        metrics["arpu_implied"] = implied_arpu
    if arpu is not None and arpu_prior is not None:
        arpu_growth = _growth_rate(arpu, arpu_prior)
        metrics["arpu_growth_rate"] = round(arpu_growth, 4) if arpu_growth is not None else None

    # ARPU = Price × Units × Mix
    avg_price = data.get("avg_price")
    avg_price_prior = data.get("avg_price_prior")
    avg_units_per_user = data.get("avg_units_per_user")
    avg_units_prior = data.get("avg_units_prior")

    price_growth = _growth_rate(avg_price, avg_price_prior)
    units_growth = _growth_rate(avg_units_per_user, avg_units_prior)

    if price_growth is not None:
        metrics["price_growth"] = round(price_growth, 4)
    if units_growth is not None:
        metrics["units_growth"] = round(units_growth, 4)

    # ARPU decomposition narrative
    arpu_comps = []
    if price_growth is not None and abs(price_growth) > 0.02:
        arpu_comps.append(f"价格{'+' if price_growth > 0 else ''}{price_growth:.1%}")
    if units_growth is not None and abs(units_growth) > 0.02:
        arpu_comps.append(f"用量{'+' if units_growth > 0 else ''}{units_growth:.1%}")
    if arpu_comps:
        metrics["arpu_components"] = arpu_comps

    # === Check 4: Product Mix Shift ===
    high_price_ratio = data.get("high_price_product_ratio")
    high_price_ratio_prior = data.get("high_price_product_ratio_prior")

    if high_price_ratio is not None and high_price_ratio_prior is not None:
        mix_shift = high_price_ratio - high_price_ratio_prior
        metrics["mix_shift"] = round(mix_shift, 4)
        if abs(mix_shift) > 0.05:  # >5pp shift is material
            direction = "高价产品占比↑" if mix_shift > 0 else "低价产品占比↑"
            flags.append(
                {
                    "level": "medium",
                    "check": "产品mix变化",
                    "value": f"{direction} {abs(mix_shift):.1%}",
                    "note": "产品mix显著变化 — 拆解ARPU增长中结构性因素",
                }
            )
    elif high_price_ratio is None and high_price_ratio_prior is None:
        gaps.append("缺高价产品占比 — mix shift 无法识别")

    # === Cross-check: Revenue Growth = User Growth × ARPU Growth ===
    revenue_growth = _growth_rate(revenue, revenue_prior)
    if revenue_growth is not None:
        metrics["revenue_growth_rate"] = round(revenue_growth, 4)

    if revenue_growth is not None and user_growth is not None and (arpu_growth := _growth_rate(arpu, arpu_prior)) is not None:
        # Revenue growth ≈ (1 + user growth) × (1 + ARPU growth) - 1
        implied_rev_growth = (1 + user_growth) * (1 + arpu_growth) - 1
        metrics["revenue_growth_formula_check"] = round(implied_rev_growth, 4)
        # If actual >> implied, signal mix shift or data inconsistency
        if revenue_growth > implied_rev_growth + 0.05:
            flags.append(
                {
                    "level": "medium",
                    "check": "营收增长异常",
                    "value": f"实际{revenue_growth:.1%} vs 公式推导{implied_rev_growth:.1%}",
                    "note": "实际营收增长超出用户×ARPU推导 — 检查数据或mix结构",
                }
            )

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": _verdict(flags, gaps)}


def _verdict(flags: list[dict[str, str]], gaps: list[str]) -> str:
    if any(f["level"] == "high" for f in flags):
        return "增长瓶颈明确/需重点优化"
    if any(f["level"] == "medium" for f in flags):
        return "增长结构有疑点/需深入拆解"
    if gaps:
        return "增长基本健康但数据缺口"
    return "增长结构未见明显警报"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    data: dict[str, Any] = {}
    # python_sandbox path: read a funnel artifact if one was passed as inputs['funnel_data'].
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]  # sandbox auto-injected
        _fa = _inputs.get("funnel_data") or {}
        _p = _fa.get("path") or ""
        if _p and Path(_p).exists():
            data = json.loads(Path(_p).read_text())
    except Exception:  # not in sandbox / no artifact → fall through to argv (local test)  # noqa: S110
        pass
    if not data and len(sys.argv) > 1:  # local-test convenience only
        data = json.loads(sys.argv[1])
