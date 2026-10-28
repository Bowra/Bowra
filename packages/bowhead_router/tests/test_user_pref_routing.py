"""M2.3 · UserModelPref 优先级层单测.

设计文档: docs/M157-routing-user-pref-design.md
覆盖: admin override > user_pref > yaml binding 优先级链.
"""

from __future__ import annotations

from bowhead_router.agent_tiers import PRIMARY_TIER_AGENTS
from bowhead_router.registry import ProviderRegistry
from bowhead_router.router import (
    USER_PREF_APPLICABLE_AGENTS,
    AgentBinding,
    Router,
    UserModelPref,
)


def _new_router() -> Router:
    reg = ProviderRegistry()
    r = Router(reg)
    r.bind(
        AgentBinding(
            agent_type="lead",
            profile=None,
            primary=("aliyun_qwen", "qwen-plus"),
            fallback=[("volces_doubao", "doubao-pro-32k")],
        )
    )
    # narrative (report writing) uses the primary tier; decision C applies user_pref.
    r.bind(
        AgentBinding(
            agent_type="narrative",
            profile=None,
            primary=("aliyun_qwen", "qwen-plus"),
        )
    )
    # echo (intent classification) uses the fast tier; user_pref does not apply to this internal subagent task.
    r.bind(
        AgentBinding(
            agent_type="echo",
            profile=None,
            primary=("aliyun_qwen", "qwen-turbo"),
        )
    )
    return r


def test_user_pref_applies_to_lead():
    """lead agent_type 走 user_pref override."""
    r = _new_router()
    pref = UserModelPref(provider="openai", model="gpt-4o")
    binding = r.resolve("lead", user_pref=pref)
    assert binding.primary == ("openai", "gpt-4o")
    # Preserve the YAML fallback chain so it remains available if the user's provider fails.
    assert binding.fallback == [("volces_doubao", "doubao-pro-32k")]


def test_user_pref_applies_to_narrative():
    """决策 C：narrative（报告写作）属 primary 档，受 user_pref 覆盖。"""
    r = _new_router()
    pref = UserModelPref(provider="anthropic", model="claude-opus-4-7")
    binding = r.resolve("narrative", user_pref=pref)
    assert binding.primary == ("anthropic", "claude-opus-4-7")


def test_user_pref_does_NOT_apply_to_echo():
    """echo（意图分类）属 fast 档，用户不该控制它的 model. 仅 primary 档受 user_pref."""
    r = _new_router()
    pref = UserModelPref(provider="openai", model="gpt-4o")
    binding = r.resolve("echo", user_pref=pref)
    # Preserve the binding's primary; user_pref must not override it.
    assert binding.primary == ("aliyun_qwen", "qwen-turbo")


def test_no_user_pref_falls_back_to_yaml():
    r = _new_router()
    binding = r.resolve("lead", user_pref=None)
    assert binding.primary == ("aliyun_qwen", "qwen-plus")


def test_from_request_fields_returns_none_when_either_missing():
    assert UserModelPref.from_request_fields(None, "qwen-plus") is None
    assert UserModelPref.from_request_fields("aliyun_qwen", None) is None
    assert UserModelPref.from_request_fields("", "qwen-plus") is None
    assert UserModelPref.from_request_fields("aliyun_qwen", "") is None


def test_from_request_fields_returns_pref_when_both_set():
    pref = UserModelPref.from_request_fields("aliyun_qwen", "qwen-plus")
    assert pref == UserModelPref(provider="aliyun_qwen", model="qwen-plus")


def test_USER_PREF_APPLICABLE_AGENTS_is_primary_tier():
    """决策 C：router 的 user_pref 适用集 = 整个 primary 档（lead/modeler/narrative/analyst）。
    guard 它与 AGENT_TIER_MAP 的 primary 档不漂移。"""
    assert USER_PREF_APPLICABLE_AGENTS == PRIMARY_TIER_AGENTS
    assert "lead" in USER_PREF_APPLICABLE_AGENTS
    assert "narrative" in USER_PREF_APPLICABLE_AGENTS
    assert "echo" not in USER_PREF_APPLICABLE_AGENTS
    assert "judge" not in USER_PREF_APPLICABLE_AGENTS
