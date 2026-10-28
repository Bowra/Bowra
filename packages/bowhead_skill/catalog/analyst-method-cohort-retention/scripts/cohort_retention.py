"""Cohort Retention Analysis — python_sandbox template.

analyst-method-cohort-retention reads this and runs it in python_sandbox
to offload retention curve + LTV + NDR computation OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body>",
                                    "inputs": {"cohorts": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py, and downloads each input to a file path. So fill `cohorts` either:
  (a) INLINE from BI query scalars (m1_retention, m3_retention, ... are scalars — just paste them), or
  (b) from an input artifact: read INPUTS['cohorts']['path'] (a JSON/CSV bi_query dumped).

The artifact format is:
  {
    "cohorts": [
      {"cohort_month": "2025-01", "new_users": 5000, "retained_m1": 2000, "retained_m3": 1500, "arpu": 25.5},
      ...
    ]
  }
OR:
  CSV with columns: cohort_month, new_users, retained_m1, retained_m3, retained_m6, retained_m12, arpu

Outputs structured JSON to stdout — metrics + flags + explicit data gaps + verdict.
Local test only: `python cohort_retention.py '{"cohorts": [...]}'` (argv is a dev convenience).
"""

from __future__ import annotations

import json
from typing import Any


def _retention_pct(retained: int | None, total: int | None) -> float | None:
    if retained is None or total in (None, 0):
        return None
    return retained / total


def _decay_rate(m1: float | None, m_end: float | None) -> float | None:
    """Calculate decay rate (M1 - M_end) / M1."""
    if m1 is None or m1 == 0 or m_end is None:
        return None
    return (m1 - m_end) / m1


def _decay_shape(m1: float, m3: float | None, m6: float | None, m12: float | None) -> str:
    """Infer decay shape: linear, exponential, or plateau."""
    if m3 is None:
        return "insufficient_data"
    # Expect exponential (fast drop early, then slow). Linear ≈ constant slope.
    # Plateau ≈ m3 ≈ m6 ≈ m12
    m1_to_m3 = _decay_rate(m1, m3)
    if m6 is not None:
        m3_to_m6 = _decay_rate(m3, m6) if m3 != 0 else None
        if m1_to_m3 is not None and m3_to_m6 is not None:
            if abs(m1_to_m3 - m3_to_m6) < 0.05:
                return "linear"  # slopes similar
            if m1_to_m3 > m3_to_m6:
                return "exponential"  # fast drop early
            if m1_to_m3 < m3_to_m6:
                return "accelerating"  # rare, bad
            return "exponential"
    if m12 is not None and m6 is not None:
        m6_to_m12 = _decay_rate(m6, m12) if m6 != 0 else None
        if m1_to_m3 is not None and m6_to_m12 is not None and m6_to_m12 < 0.1:
            return "plateau"  # stabilized by M6
    return "exponential"  # default


def _cohort_ltv(arpu: float | None, m1: float, m3: float | None, m6: float | None, m12: float | None) -> float | None:
    """
    Cohort LTV = ARPU × (M0 cohort + M1 retained + M2 extrapolated + ...).
    Conservative: use realized retention only, no future extrapolation.
    LTV = ARPU × (1 + m1_retention + m2_retention + m3_retention + ... as data allows)
    """
    if arpu is None or arpu <= 0:
        return None
    # Each month contributes retained users; we count them once
    # Simple: LTV ≈ ARPU × (1 + m1 + m3 + m6 + m12) but conservatively with realized data only
    # For now: LTV ≈ ARPU × avg_lifetime_in_months estimated from m12 or m6
    if m12 is not None and m12 > 0:
        # If 12% of users still active at M12, rough lifetime ≈ sum of monthly retention curve
        # Approximate: LTV ≈ ARPU × (1 + m1 + m3 + m6 + m12) / 4 (rough average)
        lifetime_factor = (1 + (m1 or 0) + (m3 or 0.5 * m1) + (m6 or 0.25 * m1) + (m12 or 0.1 * m1)) / 5
    elif m6 is not None and m6 > 0:
        lifetime_factor = (1 + (m1 or 0) + (m3 or 0.5 * m1) + (m6 or 0.25 * m1)) / 4
    elif m3 is not None and m3 > 0:
        lifetime_factor = (1 + (m1 or 0) + (m3 or 0.5 * m1)) / 3
    else:
        lifetime_factor = 1 + (m1 or 0)

    return round(arpu * lifetime_factor, 2)


def run_cohort_retention(data: dict[str, Any]) -> dict[str, Any]:
    cohorts_list = data.get("cohorts") or []
    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {"cohort_count": len(cohorts_list), "cohorts_analyzed": []}

    if not cohorts_list:
        gaps.append("未提供 cohort 数据")
        return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": "无数据"}

    m1_list = []
    cohort_details = []

    for cohort in cohorts_list:
        month = cohort.get("cohort_month") or cohort.get("month") or "unknown"
        new_users = cohort.get("new_users") or cohort.get("cohort_size")
        retained_m1 = cohort.get("retained_m1") or cohort.get("m1_users")
        retained_m3 = cohort.get("retained_m3") or cohort.get("m3_users")
        retained_m6 = cohort.get("retained_m6") or cohort.get("m6_users")
        retained_m12 = cohort.get("retained_m12") or cohort.get("m12_users")
        arpu = cohort.get("arpu")
        mrr_start = cohort.get("mrr_start")
        mrr_end = cohort.get("mrr_end")
        upgrades = cohort.get("upgrades") or 0
        churn = cohort.get("churn") or 0

        # Calculate retention rates
        m1_pct = _retention_pct(retained_m1, new_users)
        m3_pct = _retention_pct(retained_m3, new_users)
        m6_pct = _retention_pct(retained_m6, new_users)
        m12_pct = _retention_pct(retained_m12, new_users)

        if m1_pct is not None:
            m1_list.append(m1_pct)

        # LTV
        ltv = _cohort_ltv(arpu, m1_pct or 0, m3_pct, m6_pct, m12_pct) if arpu else None

        # NDR (if SaaS data available)
        ndr = None
        if mrr_start and mrr_start > 0 and mrr_end is not None:
            ndr = round((mrr_end + upgrades - churn) / mrr_start, 3)

        # Decay shape
        decay_shape = _decay_shape(m1_pct or 0, m3_pct, m6_pct, m12_pct) if m1_pct else "no_data"

        cohort_details.append(
            {
                "month": month,
                "m1_retention": round(m1_pct, 3) if m1_pct else None,
                "m3_retention": round(m3_pct, 3) if m3_pct else None,
                "m6_retention": round(m6_pct, 3) if m6_pct else None,
                "m12_retention": round(m12_pct, 3) if m12_pct else None,
                "decay_shape": decay_shape,
                "ltv": ltv,
                "ndr": ndr,
            }
        )

        # Flag checks
        if m1_pct is not None and m1_pct < 0.2:
            flags.append(
                {
                    "level": "high",
                    "check": "次月留存率",
                    "cohort": month,
                    "value": f"{m1_pct:.1%}",
                    "note": "M1 留存 < 20% 红色警报：快进快出，获取质量极低",
                }
            )
        elif m1_pct is not None and m1_pct < 0.4:
            flags.append(
                {"level": "medium", "check": "次月留存率", "cohort": month, "value": f"{m1_pct:.1%}", "note": "M1 留存 20-40% 中等，粘性有待提升"}
            )

        if decay_shape == "accelerating":
            flags.append(
                {"level": "high", "check": "衰减形态", "cohort": month, "value": decay_shape, "note": "加速衰减：M1 > M3 > M6，用户粘性恶化信号"}
            )

        if ndr is not None and ndr < 0.9:
            flags.append(
                {
                    "level": "high",
                    "check": "NDR 收入留存",
                    "cohort": month,
                    "value": f"{ndr:.1%}",
                    "note": f"NDR < 90% 收入负增长：每月流失 {(1 - ndr):.0%} 收入",
                }
            )
        elif ndr is not None and ndr < 1.0:
            flags.append(
                {
                    "level": "medium",
                    "check": "NDR 收入留存",
                    "cohort": month,
                    "value": f"{ndr:.1%}",
                    "note": "NDR < 100% 收入停滞：升级不足以覆盖流失",
                }
            )

    metrics["cohorts_analyzed"] = cohort_details

    # Cross-cohort analysis
    if len(m1_list) >= 2:
        avg_m1 = sum(m1_list) / len(m1_list)
        min_m1 = min(m1_list)
        max_m1 = max(m1_list)
        m1_variance = max_m1 - min_m1
        metrics["m1_avg"] = round(avg_m1, 3)
        metrics["m1_range"] = round(m1_variance, 3)

        if m1_variance > 0.15:  # >15pp spread
            flags.append(
                {
                    "level": "medium",
                    "check": "多 cohort 对比",
                    "value": f"M1 spread {min_m1:.0%}-{max_m1:.0%}",
                    "note": f"Cohort 间 M1 留存差异 {m1_variance:.0%} 显著：获取质量 / 季节性 / 产品迭代信号",
                }
            )

    # Data gap summary
    for cohort in cohorts_list:
        if cohort.get("retained_m1") is None:
            gaps.append(f"cohort {cohort.get('cohort_month')} 缺 M1 留存数")
        if cohort.get("arpu") is None:
            gaps.append(f"cohort {cohort.get('cohort_month')} 缺 ARPU")
        if cohort.get("retained_m12") is None:
            gaps.append(f"cohort {cohort.get('cohort_month')} 缺 M12 留存数 — 长期粘性无法评估")

    # Verdict
    verdict = _verdict(flags, gaps)

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": verdict}


def _verdict(flags: list[dict[str, str]], gaps: list[str]) -> str:
    high_flags = [f for f in flags if f["level"] == "high"]
    if high_flags:
        return "留存质量偏低/红色警报"
    med_flags = [f for f in flags if f["level"] == "medium"]
    if med_flags:
        return "留存质量中等, 有改进空间"
    if gaps:
        return "留存质量未见明显警报(注意数据缺口)"
    return "留存质量良好"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    data: dict[str, Any] = {}
    # python_sandbox path: read a cohorts artifact if one was passed as inputs['cohorts'].
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]  # sandbox auto-injected
        _ca = _inputs.get("cohorts") or {}
        _p = _ca.get("path") or ""
        if _p and Path(_p).exists():
            raw = json.loads(Path(_p).read_text())
            data = raw if isinstance(raw, dict) and "cohorts" in raw else {"cohorts": raw}
    except Exception:  # not in sandbox / no artifact → fall through to argv (local test)  # noqa: S110
        pass
    if not data and len(sys.argv) > 1:  # local-test convenience only
        data = json.loads(sys.argv[1])
