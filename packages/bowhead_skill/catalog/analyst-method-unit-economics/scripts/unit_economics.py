"""Unit-economics deterministic checks — python_sandbox template.

analyst-method-unit-economics reads this and runs it in OUR python_sandbox
to offload LTV/CAC/payback ratio computation + threshold flagging OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body + channels=...>",
                                    "inputs": {"channels_data": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top.

Fill channels either:
  (a) INLINE from API scalars (channels = [{name, cac, arpu, lifetime, retention, gross_margin}]), or
  (b) from input artifact: read INPUTS['channels_data']['path'] (JSON/CSV dumped).

Outputs structured JSON to stdout — per-channel metrics + health flags + verdict + recommend.

Local test only: `python unit_economics.py '[{...}]'` (argv path is dev convenience).
"""

from __future__ import annotations

import json
from typing import Any


def run_unit_econ(channels: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute LTV/CAC/payback for each channel, flag health, recommend allocation."""
    results: list[dict[str, Any]] = []
    flags: list[dict[str, str]] = []
    gaps: list[str] = []

    if not channels:
        gaps.append("无渠道数据")
        return {"channels": [], "flags": flags, "data_gaps": gaps, "verdict": "无法评估"}

    for ch in channels:
        ch_result = _evaluate_channel(ch)
        results.append(ch_result)

        # Collect flags
        if ch_result.get("health_level") == "high_risk":
            flags.append(
                {
                    "level": "high",
                    "channel": ch.get("name", "unknown"),
                    "ltv_cac_ratio": str(ch_result.get("ltv_cac_ratio")),
                    "note": ch_result.get("flag_note", ""),
                }
            )
        elif ch_result.get("health_level") == "medium_risk":
            flags.append(
                {
                    "level": "medium",
                    "channel": ch.get("name", "unknown"),
                    "ltv_cac_ratio": str(ch_result.get("ltv_cac_ratio")),
                    "note": ch_result.get("flag_note", ""),
                }
            )

    # Sort by LTV/CAC descending (best channels first)
    results.sort(key=lambda x: x.get("ltv_cac_ratio") or 0, reverse=True)

    # Recommendation allocation (simplified: allocate more budget to healthy channels)
    allocation = _recommend_allocation(results)

    return {"channels": results, "allocation_recommended": allocation, "flags": flags, "data_gaps": gaps, "verdict": _verdict_overall(results)}


def _evaluate_channel(ch: dict[str, Any]) -> dict[str, Any]:
    """Compute LTV, CAC, health for one channel."""
    result: dict[str, Any] = {"name": ch.get("name", "unknown")}

    cac = ch.get("cac")
    arpu = ch.get("arpu")
    lifetime_months = ch.get("lifetime")  # expected months of customer lifetime
    gross_margin = ch.get("gross_margin")  # 0.0-1.0

    gaps_local: list[str] = []

    # LTV = monthly_arpu * lifetime_months
    if arpu is not None and lifetime_months is not None:
        ltv_revenue = round(arpu * lifetime_months, 2)
        result["ltv_revenue_basis"] = ltv_revenue
    else:
        ltv_revenue = None
        gaps_local.append("缺ARPU或生命周期")

    # Adjust LTV by gross margin
    if ltv_revenue is not None and gross_margin is not None and gross_margin > 0:
        ltv_margin = round(ltv_revenue * gross_margin, 2)
        result["ltv_gross_margin_basis"] = ltv_margin
        ltv_for_ratio = ltv_margin
    else:
        ltv_for_ratio = ltv_revenue
        if gross_margin is None:
            gaps_local.append("缺毛利率, LTV未调整")

    # CAC
    if cac is not None:
        result["cac"] = round(cac, 2)
    else:
        cac = None
        gaps_local.append("缺CAC")

    # LTV/CAC ratio
    if ltv_for_ratio is not None and cac is not None and cac > 0:
        ltv_cac = round(ltv_for_ratio / cac, 2)
        result["ltv_cac_ratio"] = ltv_cac

        # Health level
        if ltv_cac >= 3:
            result["health_level"] = "healthy"
            result["flag_note"] = f"LTV/CAC={ltv_cac} >= 3 健康"
        elif ltv_cac >= 1.5:
            result["health_level"] = "medium_risk"
            result["flag_note"] = f"LTV/CAC={ltv_cac} 中等风险, 投资回报偏低"
        else:
            result["health_level"] = "high_risk"
            result["flag_note"] = f"LTV/CAC={ltv_cac} < 1.5 高风险亏损渠道"
    else:
        result["health_level"] = "unknown"
        result["flag_note"] = "无法计算LTV/CAC"

    # Payback period (months to recover CAC)
    if cac is not None and arpu is not None and arpu > 0:
        payback_months = round(cac / arpu, 1)
        result["payback_months"] = payback_months
        if payback_months > 24:
            result["flag_note"] += f" | 回本周期 {payback_months} 月 > 24 现金流压力大"

    result["data_gaps_local"] = gaps_local
    return result


def _recommend_allocation(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Recommend budget allocation weighted by health (simplified heuristic)."""
    healthy = [r for r in results if r.get("health_level") == "healthy"]
    medium = [r for r in results if r.get("health_level") == "medium_risk"]
    high_risk = [r for r in results if r.get("health_level") == "high_risk"]

    alloc: dict[str, Any] = {}
    if healthy:
        for ch in healthy:
            alloc[ch.get("name")] = {"recommended_pct": 60 / len(healthy), "reason": "healthy, prioritize"}
    if medium:
        for ch in medium:
            alloc[ch.get("name")] = {"recommended_pct": 30 / len(medium), "reason": "medium risk, test"}
    if high_risk:
        for ch in high_risk:
            alloc[ch.get("name")] = {"recommended_pct": 10 / len(high_risk), "reason": "high risk, pause/exit"}

    return alloc


def _verdict_overall(results: list[dict[str, Any]]) -> str:
    """Summarize overall portfolio health."""
    if not results:
        return "无渠道数据无法评估"

    health_counts = {"healthy": 0, "medium_risk": 0, "high_risk": 0}
    for r in results:
        level = r.get("health_level", "unknown")
        if level in health_counts:
            health_counts[level] += 1

    if health_counts["high_risk"] > 0:
        return f"单位经济风险: {health_counts['high_risk']} 渠道高风险亏损"
    if health_counts["medium_risk"] == len(results):
        return "单位经济中等: 全部渠道 LTV/CAC 1.5-3, 优化空间大"
    return "单位经济健康: 至少一个渠道 LTV/CAC >= 3"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    channels: list[dict[str, Any]] = []

    # python_sandbox path: read channels artifact if one was passed as inputs['channels_data'].
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]
        _ch = _inputs.get("channels_data") or {}
        _p = _ch.get("path") or ""
        if _p and Path(_p).exists():
            _data = json.loads(Path(_p).read_text())
            # Handle both {"channels": [...]} and raw [...]
            channels = _data.get("channels") if isinstance(_data, dict) else _data
    except Exception:  # noqa: S110
        pass

    if not channels and len(sys.argv) > 1:  # local-test convenience only
        channels = json.loads(sys.argv[1])
