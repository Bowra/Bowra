"""ROE DuPont decomposition + value creation (ROIC vs WACC) — python_sandbox template.

analyst-method-roe-dupont reads this and runs it in python_sandbox
to offload ratio computation + threshold flagging OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract: the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body>",
                                    "inputs": {"financials": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py. So fill `fin` either:
  (a) INLINE from bi_query scalars (net_income/revenue/ebit/... scalars — just paste them), or
  (b) from an input artifact: read INPUTS['financials']['path'] (a JSON/CSV bi_query dumped).

Outputs structured JSON to stdout — metrics + flags + explicit data gaps. The LLM reads it back
and writes the narrative + payload risk_flags; it does NOT recompute ratios by hand.

Local test only: `python roe_dupont.py '{"net_income":1.44,...}'` (argv path is a dev convenience,
NOT how the sandbox invokes it).
"""

from __future__ import annotations

import json
from typing import Any


def run_roe_dupont(fin: dict[str, Any]) -> dict[str, Any]:
    """DuPont 3-step + 5-step + ROIC + EVA + value creation verdict."""
    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}

    # Extract key inputs
    ni = fin.get("net_income")  # Net income
    rev = fin.get("revenue")  # Revenue
    ta = fin.get("total_assets")  # Total assets
    equity = fin.get("equity")  # Shareholders' equity
    ebit = fin.get("ebit")  # Earnings before interest and taxes
    tax_rate = fin.get("tax_rate")  # Effective tax rate
    ic = fin.get("invested_capital")  # Invested capital (IC)
    wacc = fin.get("wacc")  # Weighted average cost of capital
    interest_expense = fin.get("interest_expense")  # Interest expense

    # === THREE-STEP DUPONT ===
    # ROE = (NI/Rev) × (Rev/TA) × (TA/E)
    roe = None
    if ni is not None and equity not in (None, 0):
        roe = round(ni / equity, 4)
        metrics["roe"] = roe

    net_margin = None
    if ni is not None and rev not in (None, 0):
        net_margin = round(ni / rev, 4)
        metrics["net_margin"] = net_margin

    asset_turnover = None
    if rev is not None and ta not in (None, 0):
        asset_turnover = round(rev / ta, 4)
        metrics["asset_turnover"] = asset_turnover

    equity_multiplier = None
    if ta is not None and equity not in (None, 0):
        equity_multiplier = round(ta / equity, 4)
        metrics["equity_multiplier"] = equity_multiplier

    roa = None
    if ni is not None and ta not in (None, 0):
        roa = round(ni / ta, 4)
        metrics["roa"] = roa

    # Verify reconciliation: ROE ≈ ROA × equity multiplier.
    if roe is not None and roa is not None and equity_multiplier is not None:
        roe_check = round(roa * equity_multiplier, 4)
        metrics["roe_reconciliation_check"] = roe_check
        if abs(roe - roe_check) > 0.001:
            gaps.append("三步杜邦恒等式不完全相等(四舍五入差异或数据口径不统一)")

    # === Flag: High leverage ===
    if equity_multiplier is not None and equity_multiplier > 2.5:
        flags.append(
            {
                "level": "medium",
                "check": "权益乘数",
                "value": str(equity_multiplier),
                "note": f"权益乘数 {equity_multiplier} > 2.5 — 杠杆较高, 需核对ROIC vs WACC",
            }
        )

    # === FIVE-STEP DUPONT ===
    # NI/Rev = (EBIT/Rev) × (EBT/EBIT) × (NI/EBT)
    ebit_margin = None
    if ebit is not None and rev not in (None, 0):
        ebit_margin = round(ebit / rev, 4)
        metrics["ebit_margin"] = ebit_margin

    # Interest burden = EBT / EBIT = (EBIT - Interest) / EBIT.
    interest_burden = None
    if ebit is not None and interest_expense is not None and ebit not in (0,):
        ebt = ebit - interest_expense
        interest_burden = round(ebt / ebit, 4)
        metrics["interest_burden"] = interest_burden
        metrics["ebt"] = ebt
        if interest_burden < 0.85:
            flags.append(
                {
                    "level": "medium",
                    "check": "利息负担",
                    "value": str(interest_burden),
                    "note": f"利息负担 {interest_burden} < 0.85 — 利息支出显著侵蚀利润, 杠杆成本高",
                }
            )
    elif ebit is not None and interest_expense is None:
        gaps.append("缺利息费用 — 利息负担无法计算")

    # Tax burden = NI / EBT = (1 - effective tax rate) if simplified.
    tax_burden = None
    if ni is not None and ebit is not None and interest_expense is not None:
        ebt = ebit - interest_expense
        if ebt not in (None, 0):
            tax_burden = round(ni / ebt, 4)
            metrics["tax_burden"] = tax_burden
            # Infer actual tax rate from (1 - NI/EBT)
            effective_rate = round(1 - (ni / ebt), 4) if ebt > 0 else None
            metrics["effective_tax_rate"] = effective_rate
            if tax_rate is not None and effective_rate is not None and effective_rate < tax_rate * 0.85:
                flags.append(
                    {
                        "level": "medium",
                        "check": "税率异常低",
                        "value": f"实际{effective_rate:.1%} vs 标称{tax_rate:.1%}",
                        "note": "实际税率远低于标称 — 递延税/优惠可能不可持续",
                    }
                )
    elif ni is None or ebit is None or interest_expense is None:
        gaps.append("缺净利润/EBIT/利息费用 — 五步拆解无法完整计算")

    # === ROIC + EVA (Value creation) ===
    nopat = None
    if ebit is not None and tax_rate is not None:
        nopat = round(ebit * (1 - tax_rate), 4)
        metrics["nopat"] = nopat

    roic = None
    if nopat is not None and ic is not None and ic not in (0,):
        roic = round(nopat / ic, 4)
        metrics["roic"] = roic

    eva = None
    if nopat is not None and ic is not None and wacc is not None:
        eva = round(nopat - (ic * wacc), 4)
        metrics["eva"] = eva

    # === Value creation verdict ===
    value_verdict = None
    if roic is not None and wacc is not None:
        if roic < wacc:
            value_verdict = "价值毁灭(ROIC < WACC)"
            if equity_multiplier is not None and equity_multiplier > 2.5:
                flags.append(
                    {
                        "level": "high",
                        "check": "价值创造",
                        "value": f"ROIC {roic:.2%} << WACC {wacc:.2%}",
                        "note": f"ROE虚高: ROIC严重低于WACC(毁灭{eva:.4f}), 权益乘数{equity_multiplier}堆杠杆掩盖经营无能",
                    }
                )
        elif abs(roic - wacc) <= 0.015:
            value_verdict = "价值中性(ROIC ≈ WACC)"
            flags.append(
                {"level": "low", "check": "价值创造", "value": f"ROIC {roic:.2%} ≈ WACC {wacc:.2%}", "note": "ROIC勉强覆盖成本, 创造价值空间有限"}
            )
        else:
            value_verdict = "价值创造(ROIC > WACC)"
            metrics["eva_positive"] = True
    elif roic is not None or wacc is not None:
        gaps.append("缺ROIC或WACC — 价值创造判断无法完整")
    else:
        gaps.append("缺EBIT/投入资本/WACC — 价值创造无法评估")

    metrics["value_verdict"] = value_verdict

    # === Leverage sensitivity (simplified: interest rate +100bp shock) ===
    if wacc is not None and roic is not None and eva is not None and ic is not None:
        wacc_stressed = round(wacc + 0.01, 4)  # +100bp
        eva_stressed = round(nopat - (ic * wacc_stressed), 4) if nopat else None
        if eva_stressed is not None:
            metrics["eva_base"] = eva
            metrics["eva_stressed_+100bp"] = eva_stressed
            eva_drop = eva - eva_stressed
            metrics["eva_drop"] = round(eva_drop, 4)
            if eva_drop > 0 and eva > 0:
                flags.append(
                    {
                        "level": "medium",
                        "check": "利率敏感性",
                        "value": f"利率+100bp EVA下降{eva_drop:.4f}",
                        "note": "高杠杆下, 利率上升对价值冲击显著",
                    }
                )

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": _verdict(flags, value_verdict)}


def _verdict(flags: list[dict[str, str]], value_verdict: str | None) -> str:
    """Synthesize verdict from flags and value creation check."""
    if any(f["level"] == "high" for f in flags):
        return "ROE虚高/价值毁灭风险 — 杠杆堆砌或ROIC严重不足"
    if value_verdict and "毁灭" in value_verdict:
        return "ROE虚高: ROIC低于WACC导致价值毁灭"
    if any(f["level"] == "medium" for f in flags):
        return "ROE质量中等, 存疑项需核查 (杠杆/利率/税优可持续性)"
    if value_verdict:
        return f"ROE质量可接受({value_verdict}), 注意数据缺口"
    return "无法完整评估(缺关键数据)"


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
