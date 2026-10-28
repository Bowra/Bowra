"""Internal Control & AML compliance deterministic checks — python_sandbox template.

analyst-method-internal-control-aml reads this and runs it in the python_sandbox
to offload control-gap scoring + severity flagging OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract: the agent calls skill_invoke with code + inputs dict containing
detected control conditions. The sandbox auto-injects INPUTS from /workdir/input.json.

Input format:
- controls: dict of detected conditions per domain (SoD/CDD/STR/Access)
  Example: {
    "sod_segregation": {"has_duty_matrix": true, "violations_count": 2, "log_retention_months": 0},
    "cdd_coverage": {"total_customers": 150, "cdd_complete": 140, "edd_required": 20, "edd_complete": 15},
    "str_monitoring": {"threshold_defined": true, "alerts_30d": 5, "reported_30d": 3},
    "data_access": {"rbac_defined": true, "audit_log_months": 2, "anomaly_alerts_active": false}
  }

Outputs structured JSON: metrics + flags + explicit data gaps + control_maturity_score.
"""

from __future__ import annotations

import json
from typing import Any


def run_aml_control_checks(controls: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministic AML/internal control assessment.

    Returns: {
      "metrics": {maturity_score, sod_score, cdd_score, str_score, access_score},
      "flags": [{level, domain, scenario, regulation, note}, ...],
      "data_gaps": [...],
      "verdict": str,
      "control_maturity": int (1-5)
    }
    """
    gaps: list[str] = []
    flags: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}

    # ============ DOMAIN 1: SoD (Segregation of Duties) ============
    sod = controls.get("sod_segregation", {})
    sod_score = 0.0

    has_matrix = sod.get("has_duty_matrix", False)
    violations = sod.get("violations_count", 0)
    log_months = sod.get("log_retention_months", 0)

    if not has_matrix:
        gaps.append("缺权限矩阵/岗位职责对应 — SoD 无法验证")
        sod_score = 0.0
    else:
        if violations > 0:
            flags.append(
                {
                    "level": "critical" if violations > 3 else "high",
                    "domain": "SoD",
                    "scenario": f"检测到 {violations} 处职责分离违规(同人兼任录入+审批/审批+执行)",
                    "regulation": "COSO §4.2 / GB/T35273 §6.4",
                    "note": "直接高危: 单人同时拥有相互制衡权力",
                }
            )
            sod_score = 0.3
        else:
            sod_score = 0.7

        if log_months == 0:
            gaps.append("缺操作日志或日志留痕不足 3 个月 — SoD 执行轨迹无法追溯")
            sod_score *= 0.5
        elif log_months < 3:
            flags.append(
                {
                    "level": "medium",
                    "domain": "SoD",
                    "scenario": f"操作日志仅保留 {log_months} 月，未达最少 3 个月审计窗口",
                    "regulation": "GB/T35273 §7.1",
                    "note": "无法完整追溯超 3 个月的异常操作",
                }
            )
            sod_score = 0.5
        else:
            sod_score = 1.0

    metrics["sod_score"] = round(sod_score, 2)

    # ============ DOMAIN 2: CDD/EDD (Customer Due Diligence) ============
    cdd = controls.get("cdd_coverage", {})
    cdd_score = 0.0

    total_cust = cdd.get("total_customers", 0)
    cdd_complete = cdd.get("cdd_complete", 0)
    edd_required = cdd.get("edd_required", 0)
    edd_complete = cdd.get("edd_complete", 0)

    if total_cust == 0:
        gaps.append("缺客户名单数据 — CDD 覆盖率无法评估")
        cdd_score = 0.0
    else:
        cdd_rate = cdd_complete / total_cust
        if cdd_rate < 0.8:
            flags.append(
                {
                    "level": "critical",
                    "domain": "CDD",
                    "scenario": f"CDD 覆盖率仅 {cdd_rate:.0%}, {total_cust - cdd_complete} 客户缺乏身份验证/UBO 披露",
                    "regulation": "PIPL §6 / AML Law §7",
                    "note": "违反实名制 + 客户尽调强制要求",
                }
            )
            cdd_score = 0.2
        else:
            cdd_score = 0.8

        # EDD escalation check
        if edd_required > 0:
            edd_rate = edd_complete / edd_required if edd_required else 1.0
            if edd_rate < 1.0:
                flags.append(
                    {
                        "level": "high",
                        "domain": "CDD",
                        "scenario": f"EDD 触发客户 {edd_required}, 仅 {edd_complete} 完成尽调; 缺 {edd_required - edd_complete} 项高风险评估",
                        "regulation": "PIPL §14 / GDPR Art 22 / AML Law §8",
                        "note": "PEP/跨境/高净值客户尽调不足, 监管处罚风险",
                    }
                )
                cdd_score = 0.5
            else:
                cdd_score = 1.0

    metrics["cdd_score"] = round(cdd_score, 2)

    # ============ DOMAIN 3: STR & Large Transaction Monitoring ============
    str_mon = controls.get("str_monitoring", {})
    str_score = 0.0

    threshold_defined = str_mon.get("threshold_defined", False)
    alerts_30d = str_mon.get("alerts_30d", 0)
    reported_30d = str_mon.get("reported_30d", 0)

    if not threshold_defined:
        gaps.append("缺大额现金监测阈值定义(≥5万人民币/≥1万美元) — STR 监测无法启动")
        str_score = 0.0
    else:
        str_score = 0.7

        # Check STR reporting rate
        if alerts_30d == 0:
            # No alerts in 30d might be normal (low-risk org) or gap (false negatives)
            str_score = 0.6
        else:
            report_rate = reported_30d / alerts_30d if alerts_30d else 0.0
            if report_rate < 0.8:
                flags.append(
                    {
                        "level": "high",
                        "domain": "STR",
                        "scenario": f"可疑交易报告率仅 {report_rate:.0%}: {alerts_30d} 笔可疑, 仅上报 {reported_30d} 笔; {alerts_30d - reported_30d} 笔可能漏报",
                        "regulation": "AML Law §16 / GB/T35273 §7.3",
                        "note": "可疑交易延报/漏报 = 违反反洗钱义务",
                    }
                )
                str_score = 0.4
            else:
                str_score = 1.0

    metrics["str_score"] = round(str_score, 2)

    # ============ DOMAIN 4: Data Access Control & Audit Trail ============
    access = controls.get("data_access", {})
    access_score = 0.0

    rbac_defined = access.get("rbac_defined", False)
    audit_log_months = access.get("audit_log_months", 0)
    alerts_active = access.get("anomaly_alerts_active", False)

    if not rbac_defined:
        gaps.append("缺角色权限矩阵(RBAC) 定义 — 数据访问控制无法评估")
        access_score = 0.0
    else:
        access_score = 0.6

        if audit_log_months < 3:
            flags.append(
                {
                    "level": "high",
                    "domain": "Data Access",
                    "scenario": f"数据访问日志仅保留 {audit_log_months} 月, 未达 3 个月最低审计窗口; 泄露溯源能力受限",
                    "regulation": "PIPL §28 / PIPL §38 / PIPL §55",
                    "note": "数据泄露后无法完整追溯访问链路 → 监管处罚(罚款 + 通报)",
                }
            )
            access_score = 0.3
        elif audit_log_months < 6:
            flags.append(
                {
                    "level": "medium",
                    "domain": "Data Access",
                    "scenario": f"审计日志保留期 {audit_log_months} 月, 建议至少 6 个月以应对跨季度调查",
                    "regulation": "PIPL §28",
                    "note": "优化项: 提升溯源能力",
                }
            )
            access_score = 0.7
        else:
            access_score = 0.9

        if not alerts_active:
            flags.append(
                {
                    "level": "medium",
                    "domain": "Data Access",
                    "scenario": "异常访问告警机制未启用(如非工作时间高权限导出、离职员工仍可访问)",
                    "regulation": "PIPL §38",
                    "note": "无实时监控 → 数据泄露风险难以及时发现",
                }
            )
            access_score *= 0.7
        else:
            access_score = min(1.0, access_score * 1.1)

    metrics["access_score"] = round(access_score, 2)

    # ============ OVERALL MATURITY & VERDICT ============
    # CMMC 1-5: 1=initial/absent, 2=basic/partial, 3=process/orderly, 4=managed/quantified, 5=optimized/continuous.
    scores = [sod_score, cdd_score, str_score, access_score]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    if avg_score < 0.3:
        maturity = 1
        verdict = "内控成熟度 CMMC L1 (初始) — 控制缺失/形同虚设, 监管处罚直接风险"
    elif avg_score < 0.6:
        maturity = 2
        verdict = "内控成熟度 CMMC L2 (初级) — 部分控制存在但执行不一致, 有合规缺口"
    elif avg_score < 0.8:
        maturity = 3
        verdict = "内控成熟度 CMMC L3 (过程) — 核心控制已建立, 需补强执行与监测"
    elif avg_score < 0.95:
        maturity = 4
        verdict = "内控成熟度 CMMC L4 (管理) — 控制量化, 运行较为有序, 个别优化项"
    else:
        maturity = 5
        verdict = "内控成熟度 CMMC L5 (优化) — 控制完善, 持续改进中"

    metrics["control_maturity_score"] = round(avg_score, 2)

    return {"metrics": metrics, "flags": flags, "data_gaps": gaps, "verdict": verdict, "control_maturity": maturity}


if __name__ == "__main__":
    import sys
    from pathlib import Path

    controls: dict[str, Any] = {}

    # Try reading from python_sandbox input.json first
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]
        _controls = _inputs.get("controls") or {}
        _path = _controls.get("path")
        if _path and Path(_path).exists():
            controls = json.loads(Path(_path).read_text())
    except Exception:  # noqa: S110
        pass

    # Fallback to argv for local testing
    if not controls and len(sys.argv) > 1:
        controls = json.loads(sys.argv[1])

    result = run_aml_control_checks(controls)
