"""Customer-structure deterministic checks — python_sandbox template.

analyst-method-customer-structure reads this (skill_read ref) and runs it in OUR python_sandbox
to offload concentration + RFM + margin-inversion analysis OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body>",
                                    "inputs": {"customers": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top.

Inputs: either (a) structured list of {revenue, margin, recency_days, frequency} per customer,
or (b) summary stats {top1_pct, top5_pct, top20_pct, big_customer_margin, longtail_margin}.

Outputs structured JSON to stdout — metrics + flags + explicit data gaps. The LLM reads it back.

Local test only: `python customer_checks.py '{"customers":[...]}'` (argv for dev convenience).
"""

from __future__ import annotations

import json
from typing import Any


def run_customer_structure(data: dict[str, Any]) -> dict[str, Any]:
    """Run all 4 customer structure checks."""
    metrics: dict[str, Any] = {}
    flags: list[dict[str, str]] = []
    gaps: list[str] = []

    customers = data.get("customers")
    top_pcts = data.get("concentration")  # {top1_pct, top5_pct, top20_pct}
    margins = data.get("margins")  # {big_customer_margin, longtail_margin}

    # Check 1 — Concentration (Top-N %)
    if top_pcts:
        top1 = top_pcts.get("top1_pct")
        top5 = top_pcts.get("top5_pct")
        top20 = top_pcts.get("top20_pct")
        metrics["top1_pct"] = top1
        metrics["top5_pct"] = top5
        metrics["top20_pct"] = top20

        if top1 is not None and top1 > 0.40:
            flags.append(
                {"level": "high", "check": "Top1 集中度", "value": f"{top1:.1%}", "note": "Top1 客户占比 > 40% 红色警报: 业绩极度依赖单一客户"}
            )
        if top5 is not None and top5 > 0.60:
            flags.append({"level": "high", "check": "Top5 集中度", "value": f"{top5:.1%}", "note": "Top5 占比 > 60% B2B 高风险: 单点失败风险"})
        elif top5 is not None and top5 > 0.50:
            flags.append({"level": "medium", "check": "Top5 集中度", "value": f"{top5:.1%}", "note": "Top5 占比 > 50% 中风险: 需重点维护关键客户"})
        if top20 is not None and top20 > 0.80:
            flags.append(
                {"level": "medium", "check": "Top20 集中度", "value": f"{top20:.1%}", "note": "Top20 占比 > 80%: 长尾客户边际化, 保留价值低"}
            )
    elif customers:
        # Compute from customer list
        revenues = [c.get("revenue", 0) for c in customers if isinstance(c, dict)]
        if revenues:
            total = sum(revenues)
            revenues_sorted = sorted(revenues, reverse=True)
            if len(revenues_sorted) >= 1:
                top1 = round(revenues_sorted[0] / total, 3) if total else None
                metrics["top1_pct"] = top1
                if top1 and top1 > 0.40:
                    flags.append({"level": "high", "check": "Top1 集中度", "value": f"{top1:.1%}", "note": "Top1 客户占比 > 40% 红色警报"})
            if len(revenues_sorted) >= 5:
                top5 = round(sum(revenues_sorted[:5]) / total, 3) if total else None
                metrics["top5_pct"] = top5
                if top5 and top5 > 0.60:
                    flags.append({"level": "high", "check": "Top5 集中度", "value": f"{top5:.1%}", "note": "Top5 占比 > 60% B2B 高风险"})
                elif top5 and top5 > 0.50:
                    flags.append({"level": "medium", "check": "Top5 集中度", "value": f"{top5:.1%}", "note": "Top5 占比 > 50% 中风险"})
            if len(revenues_sorted) >= 20:
                top20 = round(sum(revenues_sorted[:20]) / total, 3) if total else None
                metrics["top20_pct"] = top20
                if top20 and top20 > 0.80:
                    flags.append({"level": "medium", "check": "Top20 集中度", "value": f"{top20:.1%}", "note": "Top20 占比 > 80% 长尾边际化"})
    else:
        gaps.append("缺客户营收明细 — 集中度无法计算")

    # Check 2 — RFM segmentation
    if customers and isinstance(customers, list) and len(customers) > 0:
        rfm_counts = {"VIP": 0, "需维护": 0, "潜力": 0, "沉睡": 0, "衰退": 0, "新客": 0}
        for c in customers:
            if not isinstance(c, dict):
                continue
            rev = c.get("revenue", 0)
            recency = c.get("recency_days", 999)
            freq = c.get("frequency", 0)
            has_rev = rev > 0
            recent = recency <= 90  # ≤3 months
            frequent = freq >= 2

            if has_rev and recent and frequent:
                rfm_counts["VIP"] += 1
            elif has_rev and not recent and frequent:
                rfm_counts["需维护"] += 1
            elif has_rev and recent and not frequent:
                rfm_counts["潜力"] += 1
            elif has_rev and not recent and not frequent:
                rfm_counts["沉睡"] += 1
            elif not has_rev and frequent:
                rfm_counts["衰退"] += 1
            else:
                rfm_counts["新客"] += 1

        metrics["rfm_distribution"] = rfm_counts
        rfm_counts.get("VIP", 0)
        sleep_count = rfm_counts.get("沉睡", 0)
        total_cust = len(customers)
        if sleep_count > total_cust * 0.30:
            flags.append(
                {
                    "level": "medium",
                    "check": "RFM 沉睡客户",
                    "value": f"{sleep_count}/{total_cust}",
                    "note": f"沉睡客户占 {sleep_count / total_cust:.0%} > 30% 流失风险",
                }
            )
    else:
        gaps.append("缺客户 RFM 维度数据(recency_days, frequency) — RFM 分层无法验证")

    # Check 3 — Margin inversion (big vs longtail)
    if margins:
        big_margin = margins.get("big_customer_margin")
        longtail_margin = margins.get("longtail_margin")
        if big_margin is not None and longtail_margin is not None:
            metrics["big_customer_margin_pct"] = round(big_margin * 100, 1)
            metrics["longtail_margin_pct"] = round(longtail_margin * 100, 1)
            inversion = round(longtail_margin - big_margin, 3)
            metrics["margin_inversion_ppt"] = inversion
            if inversion > 0.10:  # Long-tail margin more than 10ppt above large-customer margin indicates inversion.
                flags.append(
                    {
                        "level": "high",
                        "check": "毛利倒挂",
                        "value": f"大客户 {big_margin:.1%} vs 长尾 {longtail_margin:.1%}",
                        "note": f"大客户毛利率低于长尾 {inversion:.1%} — 折扣陷阱, 规模掩盖低利率",
                    }
                )
            elif inversion > 0.05:
                flags.append(
                    {
                        "level": "medium",
                        "check": "毛利倒挂风险",
                        "value": f"大客户 {big_margin:.1%} vs 长尾 {longtail_margin:.1%}",
                        "note": f"毛利差距 {inversion:.1%} 需关注",
                    }
                )
    elif customers and isinstance(customers, list):
        # Estimate margins from customer data if available
        try:
            big_customers = sorted(customers, key=lambda c: c.get("revenue", 0), reverse=True)[:5]
            small_customers = sorted(customers, key=lambda c: c.get("revenue", 0))[5:]
            if big_customers and small_customers:
                big_margin = sum(c.get("margin", 0) for c in big_customers) / len(big_customers)
                small_margin = sum(c.get("margin", 0) for c in small_customers) / len(small_customers)
                metrics["big_customer_margin_pct"] = round(big_margin * 100, 1)
                metrics["longtail_margin_pct"] = round(small_margin * 100, 1)
                inversion = round(small_margin - big_margin, 3)
                metrics["margin_inversion_ppt"] = inversion
                if inversion > 0.10:
                    flags.append(
                        {
                            "level": "high",
                            "check": "毛利倒挂",
                            "value": f"Top5 {big_margin:.1%} vs 其他 {small_margin:.1%}",
                            "note": f"毛利倒挂 {inversion:.1%} — 折扣陷阱预警",
                        }
                    )
        except Exception:
            gaps.append("缺大客户毛利率数据 — 倒挂检测无法验证")
    else:
        gaps.append("缺客户毛利数据 — 倒挂检测无法验证")

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": _verdict(flags)}


def _verdict(flags: list[dict[str, str]]) -> str:
    if any(f["level"] == "high" for f in flags):
        return "客户结构风险高 / 集中度或倒挂双重警报"
    if any(f["level"] == "medium" for f in flags):
        return "客户结构有风险项, 需维护重点客户或优化折扣策略"
    return "客户结构未见明显警报(注意数据缺口)"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    data: dict[str, Any] = {}
    # python_sandbox path: read customer artifact if one was passed as inputs['customers'].
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]  # sandbox auto-injected
        _cust = _inputs.get("customers") or {}
        _p = _cust.get("path") or ""
        if _p and Path(_p).exists():
            data = json.loads(Path(_p).read_text())
    except Exception:  # not in sandbox / no artifact → fall through to argv (local test)  # noqa: S110
        pass
    if not data and len(sys.argv) > 1:  # local-test convenience only
        data = json.loads(sys.argv[1])
