"""Data Compliance deterministic checks — python_sandbox template.

analyst-method-data-compliance reads this (skill_read ref) and runs it in OUR python_sandbox
to offload compliance checklist + risk scoring OFF the LLM (deterministic, auditable).

⚠️ python_sandbox contract (NOT argv): the agent calls
    skill_invoke("python_sandbox", {"code": "<this file's body>",
                                    "inputs": {"compliance": {"artifact_id": "art_xxx"}}})
The sandbox auto-injects `INPUTS = json.load(open('/workdir/input.json'))['inputs']` at the top of
user.py, and downloads each input to a file path.

Outputs structured JSON to stdout — violations + flags + explicit data gaps. The LLM reads it back
and writes the narrative + payload risk_flags; it does NOT recompute by hand.

Local test only: `python compliance_checks.py '{"has_sensitive_consent":false,...}'` (argv path is a dev convenience,
NOT how the sandbox invokes it).
"""

from __future__ import annotations

import json
from typing import Any

# PIPL Article Registry (real articles only, no fabrications)
PIPL_ARTICLES = {
    "PIPL §6": {"name": "最小化原则", "scope": "信息收集最小必要"},
    "PIPL §14": {"name": "单独同意", "scope": "特定目的单独征得同意"},
    "PIPL §16": {"name": "捆绑同意限制", "scope": "禁止强制捆绑同意"},
    "PIPL §23": {"name": "第三方SDK同意", "scope": "共享给第三方需单独同意"},
    "PIPL §24": {"name": "自动化决策权利", "scope": "算法决策须有拒绝/申诉权"},
    "PIPL §28": {"name": "敏感信息特殊保护", "scope": "生物识别/宗教/性取向等需单独同意"},
    "PIPL §29": {"name": "儿童信息保护", "scope": "未成年人个人信息特殊保护"},
    "PIPL §38": {"name": "跨境安全评估", "scope": "出境需SCC/安全评估"},
    "PIPL §55": {"name": "PIA报告", "scope": "高风险处理需隐私影响评估"},
}

GDPR_ARTICLES = {
    "GDPR Art.6": {"name": "合法依据", "scope": "处理需有法律依据"},
    "GDPR Art.22": {"name": "自动化决策权", "scope": "纯自动化决策需个人同意/反对权"},
    "GDPR Art.23": {"name": "限制权利", "scope": "法律明确规定才能限制权利"},
    "GDPR Art.35": {"name": "DPIA报告", "scope": "高风险处理需做DPIA"},
    "GDPR Art.44-49": {"name": "数据跨境", "scope": "向第三国传输需充分保障"},
}


def run_compliance_check(conditions: dict[str, Any]) -> dict[str, Any]:
    """
    Checklist-based compliance scorer.

    Input: dict of detected conditions (booleans/enums):
      - has_sensitive_data: bool (biometric/religion/orientation collected?)
      - has_sensitive_consent: bool (documented single explicit consent for sensitive data?)
      - data_fields_vs_purpose: str ("aligned" / "overly_broad" / "unknown")
      - has_sdk_sharing: bool (third-party SDK embedded?)
      - has_sdk_consent: bool (documented consent for SDK data sharing?)
      - has_bundled_consent: bool ("take-it-or-leave-it" consent template found?)
      - has_automated_decisions: bool (algorithm-based pricing/recommendation/lending?)
      - has_automated_decision_rights: bool (user can opt out / appeal?)
      - has_pia_report: bool (privacy impact assessment documented?)
      - data_transfer_jurisdiction: str ("domestic" / "non_adequate" / "unknown")
      - has_transfer_safeguards: bool (SCC or security assessment in place?)

    Output: dict with violations list, risk_level, coverage gaps.
    """
    violations: list[dict[str, Any]] = []
    gaps: list[str] = []
    metrics: dict[str, Any] = {}

    # Check 1: Sensitive data without explicit single consent (PIPL §28, §29)
    if conditions.get("has_sensitive_data"):
        metrics["has_sensitive_data"] = True
        if not conditions.get("has_sensitive_consent"):
            violations.append(
                {
                    "level": "critical",
                    "article": ["PIPL §28", "PIPL §29"],
                    "scenario": "Biometric/religious/orientation data collected without explicit single consent",
                    "remediation": "Obtain documented explicit written consent separately (不可捆绑)",
                }
            )
        else:
            metrics["sensitive_data_consent_ok"] = True
    else:
        metrics["has_sensitive_data"] = False

    # Check 2: Information minimization principle (PIPL §6)
    purpose = conditions.get("data_fields_vs_purpose")
    if purpose == "overly_broad":
        violations.append(
            {
                "level": "high",
                "article": ["PIPL §6"],
                "scenario": "Data collection exceeds stated purpose (e.g., collecting ID card for phone verification only)",
                "remediation": "Audit collection scope vs use cases; delete excess fields or expand stated purpose",
            }
        )
        metrics["minimization_ok"] = False
    elif purpose == "aligned":
        metrics["minimization_ok"] = True
    else:
        gaps.append("Data field audit status unknown — cannot verify minimization principle")

    # Check 3: Third-party SDK sharing consent (PIPL §23)
    if conditions.get("has_sdk_sharing"):
        metrics["has_sdk_sharing"] = True
        if not conditions.get("has_sdk_consent"):
            violations.append(
                {
                    "level": "high",
                    "article": ["PIPL §23"],
                    "scenario": "SDK embedded (e.g., analytics/payment/ads) without documented user consent for data sharing",
                    "remediation": "Add explicit consent banner; document which SDKs; list data shared with each",
                }
            )
        else:
            metrics["sdk_consent_ok"] = True
    else:
        metrics["has_sdk_sharing"] = False

    # Check 4: Bundled consent restrictions (PIPL §14, §16)
    if conditions.get("has_bundled_consent"):
        violations.append(
            {
                "level": "high",
                "article": ["PIPL §14", "PIPL §16"],
                "scenario": "'Register or lose access' style consent; user cannot granularly opt out of optional uses",
                "remediation": "Unbundle consent; allow granular choices for marketing/analytics/third-party sharing",
            }
        )
        metrics["bundled_consent_found"] = True
    else:
        metrics["bundled_consent_ok"] = True

    # Check 5: Automated decision rights (PIPL §24)
    if conditions.get("has_automated_decisions"):
        metrics["has_automated_decisions"] = True
        if not conditions.get("has_automated_decision_rights"):
            violations.append(
                {
                    "level": "high",
                    "article": ["PIPL §24"],
                    "scenario": "Algorithmic decision (pricing/recommendation/lending) without user knowledge or opt-out/appeal right",
                    "remediation": "Notify users of automated decisions; provide human review option; document appeal process",
                }
            )
        else:
            metrics["automated_decision_rights_ok"] = True
    else:
        metrics["has_automated_decisions"] = False

    # Check 6: Privacy Impact Assessment (PIPL §55)
    if (
        conditions.get("has_sensitive_data")
        or conditions.get("has_automated_decisions")
        or (conditions.get("data_transfer_jurisdiction") == "non_adequate")
    ):
        # High-risk scenario
        if not conditions.get("has_pia_report"):
            violations.append(
                {
                    "level": "high",
                    "article": ["PIPL §55"],
                    "scenario": "High-risk processing (large-scale sensitive data / algorithmic decisions / cross-border) without PIA report",
                    "remediation": "Commission or conduct PIA; document risk assessment, mitigation measures, ongoing monitoring",
                }
            )
            metrics["pia_required"] = True
            metrics["pia_present"] = False
        else:
            metrics["pia_present"] = True
    else:
        metrics["pia_required"] = False

    # Check 7: Cross-border safeguards (PIPL §38)
    jurisdiction = conditions.get("data_transfer_jurisdiction")
    if jurisdiction == "non_adequate":
        metrics["has_cross_border"] = True
        if not conditions.get("has_transfer_safeguards"):
            violations.append(
                {
                    "level": "critical",
                    "article": ["PIPL §38"],
                    "scenario": "Personal data transfer to non-adequate jurisdiction (no PIPL-equivalent law) without SCC or security assessment",
                    "remediation": "Establish Standard Contractual Clauses (SCC) or commission adequate security assessment; document safeguards",
                }
            )
        else:
            metrics["transfer_safeguards_ok"] = True
    elif jurisdiction == "domestic":
        metrics["has_cross_border"] = False
    else:
        gaps.append("Data transfer jurisdiction unknown — cannot verify cross-border safeguards")

    # Summary
    risk_level = _risk_level(violations)

    return {
        "violations": violations,
        "metrics": metrics,
        "data_gaps": gaps,
        "risk_level": risk_level,
        "compliance_coverage": _coverage_pct(conditions),
    }


def _risk_level(violations: list[dict[str, Any]]) -> str:
    """Determine overall risk from violation levels."""
    if any(v["level"] == "critical" for v in violations):
        return "CRITICAL — 立即整改"
    if any(v["level"] == "high" for v in violations):
        return "HIGH — 30日内整改"
    if any(v["level"] == "medium" for v in violations):
        return "MEDIUM — 计划整改"
    return "LOW — 无明显风险(注意证据缺口)"


def _coverage_pct(conditions: dict[str, Any]) -> float:
    """Estimate compliance evidence coverage (0-1)."""
    # Rough heuristic: how many checks have documented evidence?
    checks = [
        conditions.get("has_sensitive_consent") or not conditions.get("has_sensitive_data"),
        conditions.get("data_fields_vs_purpose") != "unknown",
        conditions.get("has_sdk_consent") or not conditions.get("has_sdk_sharing"),
        not conditions.get("has_bundled_consent"),
        conditions.get("has_automated_decision_rights") or not conditions.get("has_automated_decisions"),
        conditions.get("has_pia_report"),  # always checked
        conditions.get("has_transfer_safeguards") or conditions.get("data_transfer_jurisdiction") != "non_adequate",
    ]
    # coerce each check to bool (a missing/None safeguard = not satisfied = False)
    return round(sum(bool(c) for c in checks) / len(checks), 2)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    conditions: dict[str, Any] = {}
    # python_sandbox path: read a compliance artifact if one was passed as inputs['compliance'].
    try:
        _inputs = json.loads(Path("/workdir/input.json").read_text())["inputs"]  # sandbox auto-injected
        _comp = _inputs.get("compliance") or {}
        _p = _comp.get("path") or ""
        if _p and Path(_p).exists():
            conditions = json.loads(Path(_p).read_text())
    except Exception:  # not in sandbox / no artifact → fall through to argv (local test)  # noqa: S110
        pass
    if not conditions and len(sys.argv) > 1:  # local-test convenience only
        conditions = json.loads(sys.argv[1])
