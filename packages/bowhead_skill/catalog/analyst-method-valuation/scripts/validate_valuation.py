"""Valuation Method Selection & DCF Assumption Discipline — python_sandbox template.

analyst-method-valuation reads this and runs it in python_sandbox
to offload valuation logic + assumption validation OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract: the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body>",
                                    "inputs": {"valuation": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top.

Outputs structured JSON to stdout — method recommendations + flags + explicit data gaps.

Local test only: `python validate_valuation.py '{"business_type":"stable","wacc":0.09,...}'`
(argv is dev convenience, NOT how sandbox invokes it).
"""

from __future__ import annotations

import json
from typing import Any


def run_valuation(params: dict[str, Any]) -> dict[str, Any]:
    """
    Validate valuation method selection and DCF assumptions.

    Args:
        params: dict with keys:
            - business_type: str ('stable', 'cyclical', 'growth', 'financial', 'real_estate')
            - wacc: float (e.g., 0.09 for 9%)
            - terminal_growth: float (e.g., 0.03 for 3%)
            - ev_total: float (enterprise value in base units)
            - terminal_value: float (PV of terminal value)
            - current_multiple: float (e.g., 8.0 for 8x P/E)
            - historical_median_multiple: float (optional, for cycle detection)
            - current_earnings: float (optional, for cycle detection)
            - historical_median_earnings: float (optional, for cycle detection)
            - comparable_multiples: list[float] (optional, peer multiples for validation)
            - dcf_implied_price: float (optional)
            - comps_implied_price: float (optional)

    Returns:
        dict with keys: method_recommendation, flags, metrics, data_gaps, verdict
    """

    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}

    business_type = params.get("business_type", "").lower()
    wacc = params.get("wacc")
    terminal_growth = params.get("terminal_growth")
    ev_total = params.get("ev_total")
    terminal_value = params.get("terminal_value")
    current_multiple = params.get("current_multiple")
    historical_median_multiple = params.get("historical_median_multiple")
    current_earnings = params.get("current_earnings")
    historical_median_earnings = params.get("historical_median_earnings")
    comparable_multiples = params.get("comparable_multiples") or []
    dcf_implied_price = params.get("dcf_implied_price")
    comps_implied_price = params.get("comps_implied_price")

    # ===== Check 1: Estimate valuation method by business type =====
    method_rec = _recommend_method(business_type)
    metrics["recommended_method"] = method_rec

    # ===== Check 2: DCF assumption discipline (CRITICAL) =====
    if wacc is not None and terminal_growth is not None:
        if terminal_growth >= wacc:
            flags.append(
                {
                    "level": "high",
                    "check": "DCF终值增长约束",
                    "value": f"g={terminal_growth:.2%} >= WACC={wacc:.2%}",
                    "note": "终值增长 >= WACC 导致数学无效(无穷大), 违反DCF基本假设",
                }
            )
        else:
            metrics["g_vs_wacc_spread"] = round(wacc - terminal_growth, 4)
            if wacc - terminal_growth < 0.01:  # <100bps spread
                flags.append(
                    {
                        "level": "high",
                        "check": "DCF假设敏感性",
                        "value": f"spread={wacc - terminal_growth:.2%}",
                        "note": "终值增长与WACC差距<100bps → 估值对g极敏感, 需宽敏感性区间",
                    }
                )

        # Check WACC reasonableness
        if wacc < 0.05 or wacc > 0.20:
            flags.append(
                {"level": "medium", "check": "WACC范围异常", "value": str(round(wacc, 4)), "note": "WACC<5% 或 >20% 超出典型范围(5-20%), 需解释"}
            )

        metrics["wacc"] = round(wacc, 4)
        metrics["terminal_growth"] = round(terminal_growth, 4)
    else:
        gaps.append("缺WACC或终值增长 — DCF假设无法验证")

    # ===== Check 3: Terminal value proportion of EV =====
    if terminal_value is not None and ev_total is not None and ev_total > 0:
        tv_proportion = terminal_value / ev_total
        metrics["terminal_value_proportion"] = round(tv_proportion, 3)
        if tv_proportion > 0.75:
            flags.append(
                {
                    "level": "medium",
                    "check": "终值占比过高",
                    "value": f"{tv_proportion:.1%}",
                    "note": f"PV终值占EV {tv_proportion:.1%} > 70%上限 → 估值过度依赖终值假设",
                }
            )
        elif tv_proportion < 0.40:
            flags.append(
                {
                    "level": "low",
                    "check": "终值占比偏低",
                    "value": f"{tv_proportion:.1%}",
                    "note": f"PV终值占EV {tv_proportion:.1%} < 40% → 终值假设过于保守",
                }
            )
        else:
            metrics["tv_proportion_ok"] = True
    else:
        gaps.append("缺企业价值或终值 — 终值占比无法验证")

    # ===== Check 4: Cyclical stock P/E trap detection =====
    if business_type == "cyclical":
        if (
            current_multiple is not None
            and historical_median_multiple is not None
            and current_earnings is not None
            and historical_median_earnings is not None
        ):
            multiple_discount = (current_multiple - historical_median_multiple) / historical_median_multiple
            earnings_deviation = (current_earnings - historical_median_earnings) / historical_median_earnings

            metrics["current_vs_historical_multiple_ratio"] = round(current_multiple / historical_median_multiple, 3)
            metrics["current_vs_historical_earnings_ratio"] = round(current_earnings / historical_median_earnings, 3)

            # Trap: low current multiple + high current earnings = peak of cycle
            if multiple_discount < -0.25 and earnings_deviation > 0.20:  # 25% cheaper multiple, earnings 20%+ higher
                flags.append(
                    {
                        "level": "high",
                        "check": "周期股低P/E陷阱",
                        "value": f"Multiple {current_multiple:.1f}x vs hist median {historical_median_multiple:.1f}x; Earnings +{earnings_deviation:.0%} vs median",
                        "note": "当前P/E低于历史但盈利高于历史 → 处周期顶部, P/E低是因盈利高不是便宜。用normalized盈利重定价。",
                    }
                )
        else:
            gaps.append("缺周期对比数据(历史P/E中值、历史盈利中值) — 周期陷阱无法检测")

    # ===== Check 5: Relative vs Absolute valuation divergence =====
    divergence = None
    if dcf_implied_price is not None and comps_implied_price is not None and comps_implied_price > 0:
        divergence = (dcf_implied_price - comps_implied_price) / comps_implied_price
        metrics["dcf_vs_comps_divergence"] = round(divergence, 3)

        if abs(divergence) > 0.30:  # >30% divergence
            direction = "高估" if divergence > 0 else "低估"
            flags.append(
                {
                    "level": "medium",
                    "check": "相对vs绝对估值背离",
                    "value": f"DCF {divergence:+.0%} vs Comps",
                    "note": f"DCF与可比倍数法背离 {abs(divergence):.0%} → 市场给{direction}还是DCF假设{direction if divergence > 0 else '乐观'}了",
                }
            )
    elif dcf_implied_price is None or comps_implied_price is None:
        gaps.append("缺DCF或可比法估值 — 相对vs绝对背离无法分析")

    # ===== Check 6: Comparable multiples validation =====
    if comparable_multiples and len(comparable_multiples) > 0 and current_multiple is not None:
        comp_median = sorted(comparable_multiples)[len(comparable_multiples) // 2]
        comp_p25 = sorted(comparable_multiples)[int(len(comparable_multiples) * 0.25)]
        comp_p75 = sorted(comparable_multiples)[int(len(comparable_multiples) * 0.75)]

        metrics["comps_median"] = round(comp_median, 2)
        metrics["comps_p25"] = round(comp_p25, 2)
        metrics["comps_p75"] = round(comp_p75, 2)

        if current_multiple < comp_p25:
            flags.append(
                {
                    "level": "low",
                    "check": "倍数分位异常",
                    "value": f"当前 {current_multiple:.1f}x < 可比P25 {comp_p25:.1f}x",
                    "note": "当前倍数在可比公司下四分位 → 相对便宜, 但需检验是否有基本面原因",
                }
            )
        elif current_multiple > comp_p75:
            flags.append(
                {
                    "level": "low",
                    "check": "倍数分位异常",
                    "value": f"当前 {current_multiple:.1f}x > 可比P75 {comp_p75:.1f}x",
                    "note": "当前倍数在可比公司上四分位 → 相对昂贵, 需检验增长或质量溢价是否合理",
                }
            )
    elif not comparable_multiples or len(comparable_multiples) == 0:
        gaps.append("缺可比公司倍数 — 相对估值范围无法建立")

    return {
        "method_recommendation": method_rec,
        "metrics": metrics,
        "flags": flags,
        "data_gaps": gaps,
        "verdict": _verdict(flags, gaps, business_type),
    }


def _recommend_method(business_type: str) -> str:
    """
    Recommend valuation method based on business characteristics.
    """
    recommendations = {
        "stable": "P/E (盈利稳定可信, 用P/E) 或 EV/EBITDA (剔除非经常性)",
        "cyclical": "EV/EBITDA (基于normalized EBITDA, 不用当期P/E) 或 PB (资产价值稳定)",
        "growth": "PS (收入倍数) 或 PEG (增长调整P/E) 或 DCF (敏感性必须宽)",
        "financial": "P/B (账面价值) 或 ROE贴现 (特定行业模型)",
        "real_estate": "P/NAV (账面净值/FFO倍数) 或 Cap Rate (租金收益率)",
    }
    return recommendations.get(business_type, "需按具体生意模式分析")


def _verdict(flags: list[dict[str, str]], gaps: list[str], business_type: str) -> str:
    """
    Synthesize verdict based on flags and data gaps.
    """
    high_flags = [f for f in flags if f["level"] == "high"]
    medium_flags = [f for f in flags if f["level"] == "medium"]

    if len(gaps) > 2:
        return "数据缺口过多, 估值结论无法充分支撑"

    if len(high_flags) > 0:
        return "估值假设有重大风险, 需调整或扩大敏感性范围"

    if len(medium_flags) > 0:
        return "估值合理但含中等风险, 需对假设敏感性有清醒认识"

    return "估值框架自洽, 假设在合理范围内"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    params: dict[str, Any] = {}

    # python_sandbox path: read inputs artifact if passed
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]  # sandbox auto-injected
        _val = _inputs.get("valuation") or {}
        _p = _val.get("path") or ""
        if _p and Path(_p).exists():
            params = json.loads(Path(_p).read_text())
    except Exception:  # not in sandbox / no artifact → fall through to argv  # noqa: S110
        pass

    if not params and len(sys.argv) > 1:  # local-test convenience only
        params = json.loads(sys.argv[1])
