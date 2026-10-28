"""Contract Risk Assessment — python_sandbox template.

analyst-method-contract-risk reads this (skill_read ref) and runs it in OUR python_sandbox
to offload deterministic risk taxonomy scoring OFF the LLM.

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body>",
                                    "inputs": {"contract_text": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py, and downloads each input to a file path.

Inputs: 8 risk category coverage indicators (boolean or % coverage per sub-item).
Example input structure:
{
  "pe_contract_earnout": {
    "subject_clarity": true,          # 对赌主体是否清晰
    "cash_vs_equity": "cash_only",    # 现金补偿 vs 股权调整
    "escrow_mechanism": false,        # Escrow/担保机制
    "capital_maintenance_compliant": false  # 资本维持原则检查
  },
  "non_compete": {
    "geography_defined": true,
    "duration_specified": true,
    "competitor_definition": false,
    "consideration_provided": true
  },
  "contingent_liability": {
    "litigation_allocation": "partial",  # none/partial/full
    "environmental_tax_liability": false,
    "escrow_account_setup": false,
    "tail_period_months": null
  },
  "representations_warranties": {
    "financial_statements": true,
    "legal_compliance": false,
    "key_contracts": true,
    "employee_labor": false
  },
  "indemnification": {
    "cap_amount_defined": false,
    "tail_period_defined": true,
    "deductible_threshold": false,
    "basket_threshold": false
  },
  "closing_conditions": {
    "antitrust_approval": "required",
    "key_consents": "partial",
    "asset_transferability": true,
    "license_continuity": false
  },
  "mac_clause": {
    "definition_clear": false,
    "carve_outs_specified": true,
    "threshold_amount": null
  },
  "post_closing": {
    "working_capital_trueup": true,
    "security_mechanism": false,
    "breach_remedy": "damages_only"  # damages_only/specific_performance/both
  }
}

Outputs: structured JSON with risk taxonomy, coverage %, severity ranking, gaps.
"""

from __future__ import annotations

import json
from typing import Any


def run_contract_risk(inputs: dict[str, Any]) -> dict[str, Any]:
    """
    Score 8 risk categories: coverage, severity, gaps, mitigation.
    Returns: {
      "categories": [{"name": "...", "coverage": 85, "severity": "medium", "gaps": [...], "mitigation": [...]}],
      "total_coverage": 78,
      "risk_ranking": [{"category": "...", "severity": "high", "gap_count": 3}, ...],
      "data_gaps": [...],
      "verdict": "..."
    }
    """
    categories_config = {
        "对赌主体与资本维持": {
            "items": ["subject_clarity", "cash_vs_equity_check", "escrow_mechanism", "capital_maintenance_check"],
            "severity_base": "high",  # An invalid earnout agreement directly creates high risk.
        },
        "竞业限制完整性": {
            "items": ["geography_defined", "duration_specified", "competitor_definition", "consideration_provided"],
            "severity_base": "medium",
        },
        "或有负债承担": {
            "items": ["litigation_allocation_clear", "env_tax_liability_allocated", "escrow_account", "tail_period_defined"],
            "severity_base": "high",
        },
        "陈述与保证": {"items": ["financial_statements", "legal_compliance", "key_contracts", "employee_labor"], "severity_base": "medium"},
        "赔偿责任": {"items": ["indemnification_cap", "indemnification_tail", "deductible_defined", "basket_defined"], "severity_base": "medium"},
        "交割先决条件": {
            "items": ["antitrust_approval_clear", "key_consents_covered", "asset_transfer_feasible", "license_continuity"],
            "severity_base": "high",
        },
        "MAC条款": {"items": ["mac_definition_clear", "carve_outs_explicit", "threshold_amount_specified"], "severity_base": "medium"},
        "交割与补救": {"items": ["working_capital_trueup", "security_mechanism", "breach_remedy_specified"], "severity_base": "medium"},
    }

    categories = []
    total_coverage_points = 0
    total_items = 0
    risk_ranking = []

    for cat_name, config in categories_config.items():
        items = config["items"]
        covered = 0
        gaps_detail = []

        # Map input keys → item coverage
        cat_key = _normalize_category_key(cat_name)
        cat_input = inputs.get(cat_key, {})

        for item in items:
            val = cat_input.get(item)
            # Treat truthy (True, non-empty strings, positive numbers) as covered
            if val and val not in ("none", "false", False, None, ""):
                covered += 1
            else:
                gaps_detail.append(item)

        coverage = round(100 * covered / len(items)) if items else 0
        total_coverage_points += covered
        total_items += len(items)

        # Determine severity: base + coverage discount
        severity = _compute_severity(config["severity_base"], coverage)

        # Mitigation suggestions
        mitigation = _mitigation_for_category(cat_name, gaps_detail)

        categories.append(
            {
                "category": cat_name,
                "coverage": coverage,
                "severity": severity,
                "gaps": gaps_detail,
                "mitigation": mitigation,
                "gap_count": len(gaps_detail),
            }
        )

        risk_ranking.append({"category": cat_name, "severity": severity, "gap_count": len(gaps_detail), "coverage": coverage})

    # Sort risk_ranking by severity (high→medium→low) then by gap_count (desc)
    severity_order = {"high": 0, "medium": 1, "low": 2}
    risk_ranking.sort(key=lambda x: (severity_order.get(x["severity"], 999), -x["gap_count"]))

    total_coverage = round(100 * total_coverage_points / total_items) if total_items else 0
    verdict = _compute_verdict(risk_ranking, total_coverage)

    data_gaps = []
    if "contract_text" not in inputs or not inputs.get("contract_text"):
        data_gaps.append("缺合同全文或关键条款原文 — 本评分基于已提供信息，无法完整验证")

    return {"categories": categories, "total_coverage": total_coverage, "risk_ranking": risk_ranking, "data_gaps": data_gaps, "verdict": verdict}


def _normalize_category_key(cat_name: str) -> str:
    """Map category name to input key."""
    mapping = {
        "对赌主体与资本维持": "earnout",
        "竞业限制完整性": "non_compete",
        "或有负债承担": "contingent_liability",
        "陈述与保证": "representations_warranties",
        "赔偿责任": "indemnification",
        "交割先决条件": "closing_conditions",
        "MAC条款": "mac_clause",
        "交割与补救": "post_closing",
    }
    return mapping.get(cat_name, cat_name.lower().replace(" ", "_"))


def _compute_severity(base: str, coverage: int) -> str:
    """
    Adjust severity based on coverage.
    base='high': stays high if coverage <50%, else medium.
    base='medium': stays medium if coverage <70%, else low.
    """
    if base == "high":
        return "high" if coverage < 50 else ("medium" if coverage < 85 else "low")
    if base == "medium":
        return "medium" if coverage < 70 else "low"
    return "low"


def _mitigation_for_category(cat_name: str, gaps: list[str]) -> list[str]:
    """Suggest mitigation actions for gaps in each category."""
    mitigations = {
        "对赌主体与资本维持": [
            "明确对赌条款的赔偿主体(PE/创始人/目标公司)",
            "确保现金补偿遵循资本维持原则(仅从净利润/未分配利润支付)",
            "建立Escrow账户, 设立 12-24 个月托管期",
            "补充担保机制或第三方托管证明",
        ],
        "竞业限制完整性": [
            "明确竞业限制的地域范围(具体城市/国家/全球)",
            "规定限制期限(通常 1-3 年)",
            "精确定义竞争对手范围(产品线/市场定位)",
            "规定竞业补偿或停薪留职安排",
        ],
        "或有负债承担": [
            "明确分界线: 交割日前发生的诉讼/罚款由卖方承担",
            "设立独立 Escrow 账户处理或有负债(环保/税务/劳动)",
            "设置后续追偿期(通常 12-36 个月)",
            "罗列清单: 已知诉讼/潜在罚款/未清缴社保",
        ],
        "陈述与保证": [
            "补齐财务表格的真实性陈述(含或有项)",
            "穷尽法律合规陈述(无隐瞒诉讼/环保罚款/IP侵权)",
            "补充关键合同维持陈述(大客户/供应商协议)",
            "补充员工与社保陈述(无重大劳动纠纷)",
        ],
        "赔偿责任": [
            "设定赔偿金上限(通常交易价的 10-30%)",
            "明确赔偿时效(交割后 12-24 个月)",
            "设定免赔额(Deductible, 如 500K)",
            "设定累计触发门槛(Basket, 如 1M 以上才可赔偿)",
        ],
        "交割先决条件": [
            "列举所有前置审批需求(反垄断/外资/行业监管)",
            "确认关键方同意(大客户/供应商/银行/政府)",
            "评估资产/许可证转移可行性",
            "确保经营许可、营业执照、商标等可平稳交割",
        ],
        "MAC条款": [
            "明确 MAC 的定义(具体业务指标下降幅度)",
            "列明排除项(经济下行/行业波动等不触发)",
            "设定触发金额阈值(如 EBITDA 下降 >10%)",
            "规定 MAC 触发时的补救措施或退出权",
        ],
        "交割与补救": [
            "约定 post-closing working capital true-up(调整逻辑、时间)",
            "设立质押或留置权保障卖方追偿",
            "明确违反承诺时的救济(赔偿金/强制履行/解除)",
            "规定争议解决机制(仲裁/诉讼/调解)",
        ],
    }
    return mitigations.get(cat_name, ["建议律师确认本条款的完整性和执行性"])


def _compute_verdict(risk_ranking: list[dict[str, Any]], total_coverage: int) -> str:
    """Compute overall verdict based on severity distribution and coverage."""
    high_count = sum(1 for r in risk_ranking if r["severity"] == "high")
    medium_count = sum(1 for r in risk_ranking if r["severity"] == "medium")

    if high_count >= 3 or (high_count >= 2 and total_coverage < 60):
        return "合同风险等级: 红色警报 — 多个关键条款缺失，建议律师介入补充"
    if high_count >= 1 or (medium_count >= 4 and total_coverage < 70):
        return "合同风险等级: 橙色提示 — 存在需要关注的风险条款缺口，建议补充"
    if total_coverage < 70:
        return "合同风险等级: 黄色关注 — 整体覆盖度有限，建议完整审查"
    return "合同风险等级: 绿色 — 主要条款覆盖完整，继续关注执行细节"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    inputs: dict[str, Any] = {}

    # python_sandbox path: read inputs
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]
        _ct = _inputs.get("contract_data") or {}
        _p = _ct.get("path") or ""
        if _p and Path(_p).exists():
            inputs = json.loads(Path(_p).read_text())
    except Exception:  # noqa: S110
        pass

    # local-test convenience: argv
    if not inputs and len(sys.argv) > 1:
        inputs = json.loads(sys.argv[1])

    result = run_contract_risk(inputs)
