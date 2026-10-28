"""User manifest 安全检查 (sprint multi-agent-v1 §5.3 PR-#2).

3 类检查:
1. **Strong-constraint enforce** (T-SEC): user yaml 不可设 trust_class /
   output_schema / tools.deny / 高 plan tier 的 model
2. **Anti prompt-injection lint** (T-SEC-P1.1): user system.append 反 inject 模式
   匹配 reject. **Caveat**: 是 best-effort, 真护栏靠 trust_class + tools.deny
3. **Plan tier 校验** (PM-P1.6): user manifest 上传 endpoint 仅 enterprise tier
   admin 可调

`enforce_user_constraints(raw_yaml_dict, plan_tier)`: 在 Pydantic validate 之前
跑, 防 user 设置不该设的字段.
"""

from __future__ import annotations

import re
from typing import Any

from .exceptions import ManifestSecurityError, ManifestValidationError

# Strict constraints: fields user YAML cannot set
_USER_FORBIDDEN_TOP_FIELDS = frozenset(
    [
        "trust_class",  # T-SEC: prevent users claiming write-leaf to steal canvas write access
        "output_schema",  # Prevent LLM injection of unbounded string fields
        "plugins",
        "skill_grants",  # Exact system grants are code-configured only; space overrides cannot add/remove them.
        "listing",  # Marketplace/@ listing and Team override policy belong to system manifests only.
    ]
)
_USER_FORBIDDEN_TOOLS_FIELDS = frozenset(
    [
        "deny",  # tools.deny is a system-level permanent prohibition
    ]
)

# Plan tier → allowed model aliases
_TIER_MODEL_ALLOWLIST: dict[str, frozenset[str]] = {
    "free": frozenset(["analyst"]),  # free permits only cheap analyst
    "pro": frozenset(["analyst", "modeler"]),
    "enterprise": frozenset(["lead", "analyst", "modeler", "narrative", "skill"]),
    # licensing §5.4: `ultra` is the common whitelist tier (auth.py / oauth.py) AND a license
    # default_user_tier — it MUST exist here or clamping a license tier raises (SEC-6/CMP-7). Superset of enterprise.
    "ultra": frozenset(["lead", "analyst", "modeler", "narrative", "skill"]),
}


# Anti-prompt-injection patterns (T-SEC-P1.1)
# Note: best effort; infinitely many Chinese/encoded LLM injection bypasses exist; trust_class is the real guardrail.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)ignore\s+(all\s+)?prior\s+instructions?"),
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|earlier)"),
    re.compile(r"(?i)forget\s+(all\s+)?(previous|prior|earlier)"),
    re.compile(r"(?i)you\s+are\s+now\s+"),
    re.compile(r"(?i)disregard\s+(the\s+)?(above|prior)"),
    re.compile(r"\[INST\]"),
    re.compile(r"<\s*system\s*>"),
    re.compile(r"<\s*\|\s*system\s*\|\s*>"),
    # Common Chinese patterns
    re.compile(r"忽略(所有|之前|前面)?(以上|前述|之前|前面)?(的)?(指令|规则)"),
    re.compile(r"忘记(所有|之前|前面)?(以上|之前|前面)?(的)?(指令|规则)"),
    re.compile(r"你现在是"),
    re.compile(r"以下指令(覆盖|优先于)"),
]


def lint_user_append(content: str) -> None:
    """T-SEC-P1.1: 反 prompt-injection lint.

    Best-effort 防御. 命中 pattern → reject 上传, 触发 ManifestSecurityError.
    真护栏靠 trust_class + tools.deny + 单写权限 leaf 隔离 (不是 lint).

    Args:
        content: user manifest 的 system.append 字段值.

    Raises:
        ManifestSecurityError 命中 inject 模式.
    """
    if not content:
        return
    for pat in _INJECTION_PATTERNS:
        if pat.search(content):
            raise ManifestSecurityError(f"forbidden injection pattern in system.append: matched {pat.pattern!r}")


def enforce_user_constraints(
    raw_yaml: dict[str, Any],
    *,
    plan_tier: str,
) -> None:
    """User manifest 强约束校验 (T-SEC + PM-P1.6).

    在 Pydantic validate 之前跑, 防 user 设置不该设的字段.

    Args:
        raw_yaml: user 上传的 yaml dict (未过 Pydantic).
        plan_tier: workspace plan tier ("free" / "pro" / "enterprise" / "ultra").

    Raises:
        ManifestSecurityError: 触发强约束 (user 设了禁字段 / 超 tier model)
        ManifestValidationError: tier 不识别
    """
    # 1. Forbidden top-level fields
    for field in _USER_FORBIDDEN_TOP_FIELDS:
        if field in raw_yaml:
            raise ManifestSecurityError(f"user manifest cannot set `{field}` (system-only)")

    # 2. Prohibit tools.deny (system-level permanent denylist)
    tools = raw_yaml.get("tools")
    if isinstance(tools, dict):
        for forbidden in _USER_FORBIDDEN_TOOLS_FIELDS:
            if forbidden in tools:
                raise ManifestSecurityError(f"user manifest cannot set `tools.{forbidden}` (system-only)")

    # 3. Plan-tier model validation
    if plan_tier not in _TIER_MODEL_ALLOWLIST:
        raise ManifestValidationError(f"unknown plan_tier: {plan_tier!r} (expect 'free' / 'pro' / 'enterprise' / 'ultra')")
    requested_model = raw_yaml.get("model")
    allowed = _TIER_MODEL_ALLOWLIST[plan_tier]
    if requested_model and requested_model not in allowed:
        raise ManifestSecurityError(f"plan_tier={plan_tier!r} cannot use model={requested_model!r}; allowed: {sorted(allowed)}")

    # 4. Anti-injection lint for system.append
    system = raw_yaml.get("system")
    if isinstance(system, dict):
        append = system.get("append")
        if append:
            lint_user_append(append)


def merge_user_over_system(
    user_manifest: Any,
    system_manifest: Any,
) -> Any:
    """User overrides system fields (但强约束已在 enforce_user_constraints reject).

    用户配置保留自己的文案和工具配置；系统显式精确 Skill 约束必须继承。
    其他复杂 merge (字段级 diff) 留 v2.

    Args:
        user_manifest: AgentManifest from user yaml.
        system_manifest: AgentManifest from system yaml (same name).

    Returns:
        AgentManifest — 带原系统 Skill 约束的用户配置副本；原系统未声明时
        保持既有直接返回 user 的行为。
    """
    # v1: enforce already prevents user-set trust_class / output_schema / tools.deny
    # Exact Skill restrictions belong to the system profile. A workspace
    # override cannot erase an explicit empty or restricted original baseline.
    # The system listing (marketplace, @ list, Team override policy) is inherited
    # the same way; user manifests cannot set it.
    from copy import deepcopy

    update: dict[str, Any] = {}
    if system_manifest.skill_grants is not None:
        update["skill_grants"] = deepcopy(system_manifest.skill_grants)
    if getattr(system_manifest, "listing", None) is not None:
        update["listing"] = system_manifest.listing.model_copy(deep=True)
    return user_manifest.model_copy(update=update) if update else user_manifest
