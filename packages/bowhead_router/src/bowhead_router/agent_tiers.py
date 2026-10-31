"""Capability-tier model config — the single authority for agent→tier mapping.

模型设置页档位化 sprint (2026-07-22, tech-design
``docs/models-settings-tier-tech-design-2026-07-22.md``).

把配置轴从"agent 身份"（lead/echo/judge/...，内部实现名）换成 3 个用户面**能力
档位**（primary/fast/judge）。``AGENT_TIER_MAP`` 是唯一权威：构建 router 时把每个
已知 agent_type 展开成指向其档位模型的 ``AgentBinding``，调用点仍写
``invoke("lead")``/``invoke("echo")`` 不变。

反碾平核心：``fast`` 可存 ``{"follow": "primary"}``、``judge`` 可存
``{"auto": true}``——存的是**意图**不是主模型值的拷贝，只在这里（构建期）解析。

这是叶子模块：``AgentBinding`` 惰性 import（在 ``tier_agent_bindings`` 内）——
本模块与 ``bowhead_router.router`` 互引，若 top-level import 会**循环**
（router.py 反过来 import ``PRIMARY_TIER_AGENTS`` 做 user_pref 门）。不 import
``team_llm_config``（后者反过来 import 本模块）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_router.model_capabilities import (
    effective_model_capabilities,
    provider_has_team_secret,
)

if TYPE_CHECKING:
    from bowhead_router.router import AgentBinding

TIER_PRIMARY = "primary"
TIER_FAST = "fast"
TIER_JUDGE = "judge"
TIER_NAMES: tuple[str, ...] = (TIER_PRIMARY, TIER_FAST, TIER_JUDGE)


# (agent_type, profile) -> tier. The single source of truth, replacing the two manually copied REQUIRED_AGENT_TYPES lists
# in config_loader / team_llm_config. Register new agent_type values here; otherwise build_router
# has no binding for them (KeyError on invocation). test_agent_tiers guards that the complete set is nonempty.
AGENT_TIER_MAP: dict[tuple[str, str | None], str] = {
    ("lead", None): TIER_PRIMARY,
    ("modeler", None): TIER_PRIMARY,
    ("analyst", None): TIER_PRIMARY,  # analyst.default
    ("analyst", "compute"): TIER_PRIMARY,
    ("analyst", "narrative"): TIER_PRIMARY,  # Report analysis → quality first.
    ("narrative", None): TIER_PRIMARY,  # All report writing → primary (no split).
    ("echo", None): TIER_FAST,
    ("home_proposal", None): TIER_FAST,
    ("analyst", "structured"): TIER_FAST,  # Concept resolution uses a fast, non-reasoning model.
    ("judge", None): TIER_JUDGE,
}


# Decision C (2026-07-22): the Composer picker covers **the entire primary tier** (user-visible work),
# excluding fast/judge (cheap internal tasks; judge stays heterogeneous). router.USER_PREF_APPLICABLE_AGENTS
# is derived from this, replacing the hardcoded {"lead","composer"}.
PRIMARY_TIER_AGENTS: frozenset[str] = frozenset(agent_type for (agent_type, _profile), tier in AGENT_TIER_MAP.items() if tier == TIER_PRIMARY)


class TierConfigError(ValueError):
    """档位配置结构非法（tier 缺失 / provider 引用不存在 / follow-auto 形状错）。"""


@dataclass(frozen=True, slots=True)
class ResolvedTierRef:
    provider: str
    model: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.provider, self.model)


@dataclass(frozen=True, slots=True)
class ResolvedTiers:
    primary: ResolvedTierRef
    fast: ResolvedTierRef
    judge: ResolvedTierRef
    primary_fallbacks: tuple[ResolvedTierRef, ...]
    judge_used_primary_fallback: bool


def is_tier_config(config: Any) -> bool:
    """tier-shaped v2 config 判定：有 ``tiers`` object 即是。"""
    return isinstance(config, dict) and isinstance(config.get("tiers"), dict)


def tier_agent_bindings(config: dict[str, Any]) -> list[AgentBinding]:
    """把 tier-shaped config 展开成全套 ``AgentBinding``（AGENT_TIER_MAP 全集）。

    - primary 档带上 ``tiers.primary_fallback``；
    - 其它档 fallback 隐式 = ``[primary]``（便宜档挂了回主模型，不断服务）。

    调用前应先 ``validate_tiers``；这里假定结构已合法（未过校验会在解析时抛
    ``TierConfigError``）。
    """
    from bowhead_router.router import AgentBinding  # Lazy import breaks the router↔agent_tiers cycle.

    resolved = resolve_tiers(config)
    primary_pair = resolved.primary.pair
    primary_fallback = [ref.pair for ref in resolved.primary_fallbacks]

    bindings: list[AgentBinding] = []
    for (agent_type, profile), tier in AGENT_TIER_MAP.items():
        pair = getattr(resolved, tier).pair
        fallback = list(primary_fallback) if tier == TIER_PRIMARY else [primary_pair]
        bindings.append(
            AgentBinding(
                agent_type=agent_type,
                profile=profile,
                primary=pair,
                fallback=fallback,
            )
        )
    return bindings


def tiers_schema() -> dict[str, Any]:
    """Describe supported assignments; readiness and reference checks remain in validate_tiers."""
    pair = {
        "type": "object",
        "required": ["provider", "model"],
        "additionalProperties": False,
        "properties": {
            "provider": {"type": "string", "description": "Existing Team connection supplying the assigned model."},
            "model": {"type": "string", "description": "Declared model in the selected connection, subject to capability and readiness checks."},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            TIER_PRIMARY: {**pair, "description": "Main model for user-facing work; select an explicit connection and model."},
            "primary_fallback": {
                "type": "array",
                "items": pair,
                "description": "Ordered fallback models; exclude the primary model and duplicate pairs.",
            },
            TIER_FAST: {
                "description": "Fast internal tasks use an explicit model or follow the primary assignment.",
                "oneOf": [
                    pair,
                    {
                        "type": "object",
                        "required": ["follow"],
                        "additionalProperties": False,
                        "properties": {
                            "follow": {
                                "type": "string",
                                "const": TIER_PRIMARY,
                                "description": "Follow future changes to the primary assignment without copying its current value.",
                            }
                        },
                    },
                ],
            },
            TIER_JUDGE: {
                "description": "Evaluation uses an explicit model or automatic selection.",
                "oneOf": [
                    pair,
                    {
                        "type": "object",
                        "required": ["auto"],
                        "additionalProperties": False,
                        "properties": {
                            "auto": {
                                "type": "boolean",
                                "const": True,
                                "description": "Let runtime choose an evaluation model using the available Team models and fallback policy.",
                            }
                        },
                    },
                ],
            },
        },
    }


def validate_tiers(
    tiers: Any,
    providers: dict[str, Any],
    *,
    allow_legacy_duplicate_fallbacks: bool = False,
) -> None:
    """校验 ``tiers`` 段结构 + provider 引用存在。provider 本身（SSRF/type）由
    ``team_llm_config.validate_v2_config`` 负责，这里只查 tier 形状与交叉引用。"""
    if not isinstance(tiers, dict):
        raise TierConfigError("tiers must be an object")
    # primary is required and explicit.
    primary = _require_pair(tiers.get(TIER_PRIMARY), providers, "tiers.primary")
    # primary_fallback is an optional list of pairs.
    fb = tiers.get("primary_fallback")
    if fb is not None:
        if not isinstance(fb, list):
            raise TierConfigError("tiers.primary_fallback must be a list")
        seen = {primary}
        for i, item in enumerate(fb):
            path = f"tiers.primary_fallback[{i}]"
            pair = _require_pair(item, providers, path)
            if not allow_legacy_duplicate_fallbacks:
                if pair == primary:
                    raise TierConfigError(f"{path} must differ from tiers.primary")
                if pair in seen:
                    raise TierConfigError(f"{path} must not duplicate an earlier fallback")
            seen.add(pair)
    # fast: follow(primary) or an explicit pair.
    _validate_optional_tier(tiers.get(TIER_FAST), providers, "tiers.fast", follow_ok=True)
    # judge: auto or an explicit pair.
    _validate_optional_tier(tiers.get(TIER_JUDGE), providers, "tiers.judge", auto_ok=True)


def _validate_optional_tier(
    spec: Any,
    providers: dict[str, Any],
    path: str,
    *,
    follow_ok: bool = False,
    auto_ok: bool = False,
) -> None:
    if spec is None:
        return  # Default = follow/auto.
    if not isinstance(spec, dict):
        raise TierConfigError(f"{path} must be an object")
    if follow_ok and spec.get("follow"):
        if spec["follow"] != TIER_PRIMARY:
            raise TierConfigError(f"{path}.follow only supports 'primary'")
        return
    if auto_ok and spec.get("auto"):
        return
    _require_pair(spec, providers, path)


def _require_pair(spec: Any, providers: dict[str, Any], path: str) -> tuple[str, str]:
    if not isinstance(spec, dict):
        raise TierConfigError(f"{path} must be an object")
    provider = spec.get("provider")
    model = spec.get("model")
    if not isinstance(provider, str) or not provider:
        raise TierConfigError(f"{path}.provider must be non-empty")
    if provider not in providers:
        raise TierConfigError(f"{path}.provider unknown: {provider}")
    if not isinstance(model, str) or not model:
        raise TierConfigError(f"{path}.model must be non-empty")
    return provider, model


def resolve_tiers(config: dict[str, Any]) -> ResolvedTiers:
    """Resolve tier intent into concrete refs using capability-aware judge auto."""
    tiers = config.get("tiers") or {}
    providers = config.get("providers") or {}
    primary_pair = _pair(tiers.get(TIER_PRIMARY) or {})
    if not primary_pair[0] or not primary_pair[1]:
        raise TierConfigError("tiers.primary must resolve to (provider, model)")
    primary = ResolvedTierRef(*primary_pair)

    fast_spec = tiers.get(TIER_FAST)
    if _is_follow(fast_spec) or fast_spec is None:
        fast = primary
    else:
        fast = ResolvedTierRef(*_pair(fast_spec))

    judge_spec = tiers.get(TIER_JUDGE)
    if _is_auto(judge_spec) or judge_spec is None:
        heterogeneous = _pick_heterogeneous(providers, primary.provider, primary.model)
        judge = ResolvedTierRef(*heterogeneous) if heterogeneous else primary
        judge_used_primary_fallback = heterogeneous is None
    else:
        judge = ResolvedTierRef(*_pair(judge_spec))
        judge_used_primary_fallback = False

    fallback_pairs: list[tuple[str, str]] = []
    seen_fallbacks = {primary_pair}
    for spec in tiers.get("primary_fallback") or []:
        pair = _pair(spec)
        if pair in seen_fallbacks:
            continue
        seen_fallbacks.add(pair)
        fallback_pairs.append(pair)
    primary_fallbacks = tuple(ResolvedTierRef(*pair) for pair in fallback_pairs)
    return ResolvedTiers(
        primary=primary,
        fast=fast,
        judge=judge,
        primary_fallbacks=primary_fallbacks,
        judge_used_primary_fallback=judge_used_primary_fallback,
    )


def _pick_heterogeneous(
    providers: dict[str, Any],
    primary_provider: str,
    primary_model: str,
) -> tuple[str, str] | None:
    """Judge auto: a usable chat model other than the main model.

    What matters is a different model, so the judge never grades its own answers. A model at another
    provider is tried first (the most independent judge); otherwise another chat model at the main model's
    provider. Only a team whose single chat model is the main model gets None; the caller then falls back
    to the main model and reports it (``judge_used_primary_fallback``).
    """
    for name, spec in providers.items():
        if name == primary_provider or not isinstance(spec, dict):
            continue
        if not provider_has_team_secret(providers, str(name)):
            continue
        model = _first_chat_model_id(providers, str(name), spec.get("models"))
        if model:
            return (str(name), model)
    spec = providers.get(primary_provider)
    if isinstance(spec, dict) and provider_has_team_secret(providers, primary_provider):
        model = _first_chat_model_id(providers, primary_provider, spec.get("models"), exclude=primary_model)
        if model:
            return (primary_provider, model)
    return None


def _first_chat_model_id(
    providers: dict[str, Any],
    provider_name: str,
    models: Any,
    *,
    exclude: str | None = None,
) -> str | None:
    if not isinstance(models, list):
        return None
    for m in models:
        model_id = m if isinstance(m, str) else str(m.get("id") or "") if isinstance(m, dict) else ""
        if exclude is not None and model_id == exclude:
            continue
        capabilities = effective_model_capabilities(providers, provider_name, model_id)
        if model_id and capabilities is not None and "chat" in capabilities:
            return model_id
    return None


def _is_follow(spec: Any) -> bool:
    return isinstance(spec, dict) and spec.get("follow") == TIER_PRIMARY


def _is_auto(spec: Any) -> bool:
    return isinstance(spec, dict) and bool(spec.get("auto"))


def _pair(spec: Any) -> tuple[str, str]:
    if not isinstance(spec, dict):
        return ("", "")
    return (str(spec.get("provider", "")), str(spec.get("model", "")))
