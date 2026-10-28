"""Solvency & Liquidity & Cashflow Quality deterministic checks — python_sandbox template.

analyst-method-solvency-cashflow reads this (skill_read ref) and runs it in python_sandbox
to offload ratio computation + threshold flagging OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body + fin=... + print>",
                                    "inputs": {"financials": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py, and downloads each input to a file path.

Outputs structured JSON to stdout — metrics + flags + explicit data gaps. The LLM reads it back
and writes the narrative + payload risk_flags; it does NOT recompute ratios by hand.

Local test only: `python solvency_checks.py '{"current_assets":100,...}'` (argv path is a dev convenience,
NOT how the sandbox invokes it).
"""

from __future__ import annotations

import json
from typing import Any


def run_solvency(fin: dict[str, Any]) -> dict[str, Any]:
    """
    Compute solvency/liquidity/cashflow metrics and composite distress flags.

    Expected input keys (all optional, but missing values create data_gaps):
    - current_assets, current_liabilities, inventory: balance sheet liquidity
    - total_debt, ebit, interest, ebitda: debt service capacity
    - cfo (operating CF), cfi (investing CF), cff (financing CF), capex: cashflow segments
    - net_income: for FCF context

    Returns: {metrics, flags, data_gaps, verdict}
    """
    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}
    distress_count = 0
    # Fix (review): init all ratios so the composite-distress block never hits
    # UnboundLocalError when a given input is missing.
    current_ratio = quick_ratio = interest_coverage = net_debt_ebitda = fcf = None

    # === CHECK 1: Liquidity Ratios ===
    ca = fin.get("current_assets")
    cl = fin.get("current_liabilities")
    inv = fin.get("inventory")

    if ca is not None and cl is not None and cl > 0:
        current_ratio = round(ca / cl, 3)
        metrics["current_ratio"] = current_ratio
        if current_ratio < 1.0:
            flags.append(
                {"level": "high", "check": "流动比率", "value": str(current_ratio), "note": "流动比率<1.0 红色警报: 流动资产不足覆盖流动负债"}
            )
            distress_count += 1
        elif current_ratio < 1.5:
            flags.append({"level": "medium", "check": "流动比率", "value": str(current_ratio), "note": "流动比率<1.5 黄色警戒: 流动资产覆盖度有限"})
    else:
        gaps.append("缺流动资产或流动负债 — 流动比率无法计算")

    if inv is not None and ca is not None and cl is not None and cl > 0:
        quick_ratio = round((ca - inv) / cl, 3)
        metrics["quick_ratio"] = quick_ratio
        if quick_ratio < 0.7:
            flags.append({"level": "high", "check": "速动比率", "value": str(quick_ratio), "note": "速动比率<0.7 红色警报: 除存货外流动资产紧张"})
            distress_count += 1
        elif quick_ratio < 1.0:
            flags.append({"level": "medium", "check": "速动比率", "value": str(quick_ratio), "note": "速动比率<1.0 黄色警戒: 对存货依赖度高"})
    else:
        gaps.append("缺存货数据 — 速动比率无法计算")

    # === CHECK 2: Debt Service Capacity ===
    ebit = fin.get("ebit")
    interest = fin.get("interest")

    if ebit is not None and interest is not None and interest > 0:
        interest_coverage = round(ebit / interest, 3)
        metrics["interest_coverage"] = interest_coverage
        if interest_coverage < 1.5:
            flags.append(
                {"level": "high", "check": "利息保障倍数", "value": str(interest_coverage), "note": "利息保障倍数<1.5 危险: 利息都有风险还不起"}
            )
            distress_count += 1
        elif interest_coverage < 3.0:
            flags.append(
                {"level": "medium", "check": "利息保障倍数", "value": str(interest_coverage), "note": "利息保障倍数3-1.5间 黄色: 利息压力显著"}
            )
    else:
        gaps.append("缺EBIT或利息支出 — 利息保障倍数无法验证")

    # === CHECK 2b: Debt Ratio ===
    total_debt = fin.get("total_debt")
    total_assets = fin.get("total_assets")

    if total_debt is not None and total_assets is not None and total_assets > 0:
        debt_ratio = round(total_debt / total_assets, 3)
        metrics["debt_ratio"] = debt_ratio
        if debt_ratio > 0.70:
            flags.append({"level": "high", "check": "资产负债率", "value": f"{debt_ratio:.0%}", "note": "资产负债率>70% 警戒: 杠杆过高"})
        elif debt_ratio > 0.60:
            flags.append({"level": "medium", "check": "资产负债率", "value": f"{debt_ratio:.0%}", "note": "资产负债率60-70% 黄色: 杠杆偏高"})
    else:
        gaps.append("缺总负债或总资产 — 资产负债率无法计算")

    # === CHECK 2c: Net Debt / EBITDA ===
    ebitda = fin.get("ebitda")
    cash = fin.get("cash")

    if total_debt is not None and cash is not None and ebitda is not None and ebitda > 0:
        net_debt = total_debt - cash
        net_debt_ebitda = round(net_debt / ebitda, 3)
        metrics["net_debt"] = net_debt
        metrics["net_debt_to_ebitda"] = net_debt_ebitda
        if net_debt_ebitda > 4.0:
            flags.append(
                {"level": "high", "check": "净负债/EBITDA", "value": str(net_debt_ebitda), "note": "净负债/EBITDA>4 高杠杆: 融资困难, 债权人风险大"}
            )
            distress_count += 1
        elif net_debt_ebitda > 2.0:
            flags.append(
                {"level": "medium", "check": "净负债/EBITDA", "value": str(net_debt_ebitda), "note": "净负债/EBITDA>2 中等杠杆: 需关注偿债能力"}
            )
    else:
        gaps.append("缺总负债、现金或EBITDA — 净负债/EBITDA无法计算")

    # === CHECK 3: Cashflow Three Segments ===
    cfo = fin.get("cfo")
    cfi = fin.get("cfi")
    cff = fin.get("cff")

    metrics["cfo"] = cfo
    metrics["cfi"] = cfi
    metrics["cff"] = cff

    if cfo is not None:
        if cfo < 0:
            flags.append({"level": "high", "check": "经营现金流(CFO)", "value": str(cfo), "note": "经营现金流为负: 核心业务不产生现金, 借钱续命信号"})
            distress_count += 1
        else:
            metrics["cfo_status"] = "正常"
    else:
        gaps.append("缺经营现金流(CFO) — 现金流分析不完整")

    if cfi is not None and cfo is not None and cfo < 0 and cfi > 0:
        flags.append({"level": "medium", "check": "投资现金流(CFI)", "value": str(cfi), "note": "CFO负 + CFI正: 可能抛资产自救信号"})

    if cff is not None and cfo is not None and cfo < 0 and cff > 0:
        flags.append({"level": "high", "check": "筹资现金流(CFF)", "value": str(cff), "note": "CFO负 + CFF正: 借钱维持运营, 债务压力加重"})
        distress_count += 1

    # === CHECK 4: Free Cashflow (FCF) Gap ===
    capex = fin.get("capex")
    ni = fin.get("net_income")

    if cfo is not None and capex is not None:
        fcf = cfo - capex
        metrics["fcf"] = fcf
        if fcf < 0:
            flags.append({"level": "high", "check": "自由现金流(FCF)", "value": str(fcf), "note": "FCF<0: 无法自给自足, 无法分红/还债"})
            distress_count += 1
        elif ni is not None and fcf < ni:
            flags.append(
                {
                    "level": "medium",
                    "check": "FCF vs 净利润",
                    "value": f"FCF={fcf} vs NI={ni}",
                    "note": "FCF<净利润: 利润含金量存疑, 资本支出吞噬现金",
                }
            )
    else:
        gaps.append("缺经营现金流或资本支出 — FCF无法计算")

    # === COMPOSITE DISTRESS JUDGMENT ===
    distress_flags = []
    if cfo is not None and cfo < 0:
        distress_flags.append("CFO负")
    if interest_coverage is not None and interest_coverage < 1.5:
        distress_flags.append("利息保障<1.5")
    if current_ratio is not None and current_ratio < 1.0:
        distress_flags.append("流动比率<1.0")
    if quick_ratio is not None and quick_ratio < 0.7:
        distress_flags.append("速动比率<0.7")
    if net_debt_ebitda is not None and net_debt_ebitda > 4.0:
        distress_flags.append("净负债/EBITDA>4")
    if fcf is not None and fcf < 0:
        distress_flags.append("FCF<0")

    metrics["distress_count"] = distress_count
    metrics["distress_signals"] = distress_flags

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": _verdict(flags, distress_count, distress_flags)}


def _verdict(flags: list[dict[str, str]], distress_count: int, distress_signals: list[str]) -> str:
    """Composite verdict: solvency health check."""
    if distress_count >= 2:
        return f"流动性风险偏高(P0): {len(distress_signals)}项困境信号命中({', '.join(distress_signals)})"
    if any(f["level"] == "high" for f in flags):
        return "偿债能力有风险, 需重点关注"
    if any(f["level"] == "medium" for f in flags):
        return "偿债能力中等, 有存疑项"
    return "偿债能力未见明显警报(注意数据缺口)"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    fin: dict[str, Any] = {}
    # python_sandbox path: read a financials artifact if one was passed as inputs['financials'].
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]  # sandbox auto-injected
        _fa = _inputs.get("financials") or {}
        _p = _fa.get("path") or ""
        if _p and Path(_p).exists():
            fin = json.loads(Path(_p).read_text())
    except Exception:  # not in sandbox / no artifact → fall through to argv (local test)  # noqa: S110
        pass
    if not fin and len(sys.argv) > 1:  # local-test convenience only
        fin = json.loads(sys.argv[1])

    # Compute metrics & flags
    result = run_solvency(fin)
