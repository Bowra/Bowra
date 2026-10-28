"""Quality-of-Earnings deterministic checks — python_sandbox template.

analyst-method-earnings-quality reads this (skill_read ref) and runs it in OUR python_sandbox
to offload ratio computation + threshold flagging OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body + a `fin=...` line + the print>",
                                    "inputs": {"financials": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py, and downloads each input to a file path. So fill `fin` either:
  (a) INLINE from bi_query scalars (net_income/ocf/ar/... are ~8 numbers — just paste them), or
  (b) from an input artifact: read INPUTS['financials']['path'] (a JSON/CSV bi_query dumped).

Outputs structured JSON to stdout — metrics + flags + explicit data gaps. The LLM reads it back
and writes the narrative + payload risk_flags; it does NOT recompute ratios by hand.

Local test only: `python qoe_checks.py '{"net_income":1.44,...}'` (argv path is a dev convenience,
NOT how the sandbox invokes it).
"""

from __future__ import annotations

import json
from typing import Any


def _growth(end: float | None, begin: float | None) -> float | None:
    if end is None or begin in (None, 0):
        return None
    return (end - begin) / begin


def run_qoe(fin: dict[str, Any]) -> dict[str, Any]:
    ni = fin.get("net_income")
    ocf = fin.get("ocf")
    rev, rev_prior = fin.get("revenue"), fin.get("revenue_prior")
    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}

    # Check 1 — Cash conversion ratio (OCF / NI)
    if ocf is not None and ni not in (None, 0):
        ocf_ni = round(ocf / ni, 3)
        metrics["ocf_to_ni"] = ocf_ni
        if ocf_ni < 0.5:
            flags.append({"level": "high", "check": "净现比", "value": str(ocf_ni), "note": "净现比<0.5 红色警报: 近半利润未变现为现金"})
        elif ocf_ni < 1.0:
            flags.append({"level": "medium", "check": "净现比", "value": str(ocf_ni), "note": "净现比<1 利润含金量存疑"})
    else:
        gaps.append("缺经营现金流(OCF)或净利润 — 净现比无法验证")

    # Check 2 — Working capital absorption (receivables/inventory growth vs revenue growth; incremental funding needs)
    rev_g = _growth(rev, rev_prior)
    ar_g = _growth(fin.get("ar_end"), fin.get("ar_begin"))
    inv_g = _growth(fin.get("inv_end"), fin.get("inv_begin"))
    metrics["revenue_growth"] = None if rev_g is None else round(rev_g, 3)
    metrics["ar_growth"] = None if ar_g is None else round(ar_g, 3)
    metrics["inv_growth"] = None if inv_g is None else round(inv_g, 3)
    if rev_g is not None and ar_g is not None and ar_g > rev_g + 0.20:
        flags.append(
            {
                "level": "high",
                "check": "应收背离",
                "value": f"AR+{ar_g:.0%} vs Rev+{rev_g:.0%}",
                "note": "应收增速远超营收 → 放宽信用/压货/激进确认收入, 坏账风险",
            }
        )
    if rev_g is not None and inv_g is not None and inv_g > rev_g + 0.20:
        flags.append(
            {"level": "medium", "check": "存货背离", "value": f"Inv+{inv_g:.0%} vs Rev+{rev_g:.0%}", "note": "存货增速远超营收 → 周转恶化/跌价风险"}
        )
    # Incremental working capital absorption = Δreceivables + Δinventory − Δpayables.
    d_ar = _delta(fin.get("ar_end"), fin.get("ar_begin"))
    d_inv = _delta(fin.get("inv_end"), fin.get("inv_begin"))
    d_ap = _delta(fin.get("ap_end"), fin.get("ap_begin"))
    if d_ar is not None and d_inv is not None:
        nwc_use = round(d_ar + d_inv - (d_ap or 0.0), 3)
        metrics["nwc_increase"] = nwc_use
        if ni and nwc_use > ni:
            flags.append(
                {"level": "high", "check": "营运资本占用", "value": str(nwc_use), "note": f"新增营运资本占用 {nwc_use} 超净利润 {ni} — 增长靠垫资"}
            )
        if d_ap is None:
            gaps.append("缺应付账款 — 营运资本占用未扣应付(偏保守)")

    # Check 3 — Recurring net income
    nonrec = fin.get("nonrecurring")
    if nonrec is not None and ni is not None:
        recurring = round(ni - nonrec, 3)
        metrics["recurring_ni"] = recurring
        metrics["recurring_ratio"] = round(recurring / ni, 3) if ni else None
        if ni and recurring / ni < 0.8:
            flags.append(
                {"level": "medium", "check": "非经常性占比高", "value": f"{recurring / ni:.0%}", "note": "经常性利润<80% — 账面利润含较大一次性"}
            )
    else:
        gaps.append("缺非经常性损益明细 — 经常性利润无法剔除核算")

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": _verdict(flags)}


def _delta(end: float | None, begin: float | None) -> float | None:
    if end is None or begin is None:
        return None
    return end - begin


def _verdict(flags: list[dict[str, str]]) -> str:
    if any(f["level"] == "high" for f in flags):
        return "盈利质量偏低/红色警报"
    if any(f["level"] == "medium" for f in flags):
        return "盈利质量中等, 有存疑项"
    return "盈利质量未见明显警报(注意数据缺口)"


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
