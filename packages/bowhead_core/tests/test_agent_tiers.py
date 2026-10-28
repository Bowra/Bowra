"""Tests for capability-tier config expansion (agent_tiers.py)."""

from __future__ import annotations

import pytest
from bowhead_core.team_llm_config import (
    TeamLlmConfigError,
    build_catalog,
    build_router_from_v2_config,
    validate_v2_config,
)
from bowhead_router.agent_tiers import (
    AGENT_TIER_MAP,
    PRIMARY_TIER_AGENTS,
    TIER_NAMES,
    ResolvedTierRef,
    TierConfigError,
    is_tier_config,
    resolve_tiers,
    tier_agent_bindings,
    validate_tiers,
)


def _providers() -> dict:
    return {
        "deepseek": {
            "type": "anthropic_compatible",
            "base_url": "https://x/anthropic",
            "api_key": "k",
            "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        },
        "qwen": {"type": "openai_compatible", "base_url": "https://x/v1", "api_key": "k", "models": ["qwen3.6-plus", "qwen3.6-flash"]},
        "doubao": {"type": "anthropic_compatible", "base_url": "https://x/coding", "api_key": "k", "models": ["Doubao-Seed-2.0-pro"]},
    }


def _cfg(tiers: dict) -> dict:
    return {"schema_version": 2, "providers": _providers(), "tiers": tiers}


# ─── map integrity ──────────────────────────────────────────────────


def test_every_tier_in_map_is_known() -> None:
    for tier in AGENT_TIER_MAP.values():
        assert tier in TIER_NAMES


def test_required_agent_types_all_present() -> None:
    # The sole authority must cover all agent_type/profile values used at actual call sites.
    keys = set(AGENT_TIER_MAP)
    for k in [
        ("lead", None),
        ("modeler", None),
        ("analyst", None),
        ("analyst", "structured"),
        ("narrative", None),
        ("echo", None),
        ("home_proposal", None),
        ("judge", None),
    ]:
        assert k in keys, f"missing {k}"


def test_primary_tier_agents_for_decision_c() -> None:
    # Decision C: picker covers the entire primary tier, excluding fast/judge.
    assert "lead" in PRIMARY_TIER_AGENTS
    assert "narrative" in PRIMARY_TIER_AGENTS
    assert "modeler" in PRIMARY_TIER_AGENTS
    assert "analyst" in PRIMARY_TIER_AGENTS
    assert "echo" not in PRIMARY_TIER_AGENTS
    assert "judge" not in PRIMARY_TIER_AGENTS


def test_is_tier_config() -> None:
    assert is_tier_config({"tiers": {}})
    assert not is_tier_config({"agents": {}})
    assert not is_tier_config({})
    assert not is_tier_config(None)


# ─── expansion ──────────────────────────────────────────────────────


def test_expand_full_binding_set() -> None:
    cfg = _cfg(
        {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "primary_fallback": [{"provider": "qwen", "model": "qwen3.6-plus"}],
            "fast": {"provider": "qwen", "model": "qwen3.6-flash"},
            "judge": {"provider": "doubao", "model": "Doubao-Seed-2.0-pro"},
        }
    )
    bindings = {(b.agent_type, b.profile): b for b in tier_agent_bindings(cfg)}
    # Expand the full set.
    assert set(bindings) == set(AGENT_TIER_MAP)
    # primary tier → deepseek-v4-pro + fallback
    lead = bindings[("lead", None)]
    assert lead.primary == ("deepseek", "deepseek-v4-pro")
    assert lead.fallback == [("qwen", "qwen3.6-plus")]
    # fast tier (echo) → qwen3.6-flash, fallback = [primary]
    echo = bindings[("echo", None)]
    assert echo.primary == ("qwen", "qwen3.6-flash")
    assert echo.fallback == [("deepseek", "deepseek-v4-pro")]
    # judge tier → doubao
    assert bindings[("judge", None)].primary == ("doubao", "Doubao-Seed-2.0-pro")
    # narrative (report writing) uses primary (not split).
    assert bindings[("narrative", None)].primary == ("deepseek", "deepseek-v4-pro")
    # structured profile uses fast.
    assert bindings[("analyst", "structured")].primary == ("qwen", "qwen3.6-flash")


def test_fast_follow_primary_not_copied_at_rest() -> None:
    # follow sentinel → resolves to primary at build time; changing primary changes it too.
    cfg = _cfg(
        {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "fast": {"follow": "primary"},
            "judge": {"auto": True},
        }
    )
    bindings = {(b.agent_type, b.profile): b for b in tier_agent_bindings(cfg)}
    assert bindings[("echo", None)].primary == ("deepseek", "deepseek-v4-pro")
    # Change primary → fast follows.
    cfg["tiers"]["primary"] = {"provider": "qwen", "model": "qwen3.6-plus"}
    bindings2 = {(b.agent_type, b.profile): b for b in tier_agent_bindings(cfg)}
    assert bindings2[("echo", None)].primary == ("qwen", "qwen3.6-plus")


def test_judge_auto_picks_heterogeneous() -> None:
    cfg = _cfg(
        {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "fast": {"follow": "primary"},
            "judge": {"auto": True},
        }
    )
    bindings = {(b.agent_type, b.profile): b for b in tier_agent_bindings(cfg)}
    judge = bindings[("judge", None)]
    assert judge.primary[0] != "deepseek"  # Heterogeneous


def test_judge_auto_single_provider_falls_back_to_primary() -> None:
    cfg = {
        "schema_version": 2,
        "providers": {"only": {"type": "openai_compatible", "base_url": "https://x/v1", "api_key": "k", "models": ["m1"]}},
        "tiers": {
            "primary": {"provider": "only", "model": "m1"},
            "fast": {"follow": "primary"},
            "judge": {"auto": True},
        },
    }
    bindings = {(b.agent_type, b.profile): b for b in tier_agent_bindings(cfg)}
    # No heterogeneous candidate → fall back to primary.
    assert bindings[("judge", None)].primary == ("only", "m1")


def test_judge_auto_picks_another_model_at_the_same_provider() -> None:
    """One connection with two chat models: Auto must not silently grade with the main model."""
    cfg = {
        "schema_version": 2,
        "providers": {
            "dashscope": {"type": "openai_compatible", "base_url": "https://x/v1", "api_key": "k", "models": ["qwen3.6-plus", "qwen3.6-flash"]},
        },
        "tiers": {"primary": {"provider": "dashscope", "model": "qwen3.6-plus"}, "judge": {"auto": True}},
    }

    resolved = resolve_tiers(cfg)

    assert resolved.judge == ResolvedTierRef("dashscope", "qwen3.6-flash")
    assert resolved.judge_used_primary_fallback is False


def test_judge_auto_prefers_another_provider_when_one_exists() -> None:
    resolved = resolve_tiers(_cfg({"primary": {"provider": "deepseek", "model": "deepseek-v4-pro"}, "judge": {"auto": True}}))

    assert resolved.judge.provider != "deepseek"
    assert resolved.judge_used_primary_fallback is False


def test_judge_auto_ignores_heterogeneous_non_chat_model() -> None:
    cfg = {
        "schema_version": 2,
        "providers": {
            "primary": {
                "type": "openai_compatible",
                "base_url": "https://x/v1",
                "api_key": "k",
                "models": ["chat"],
            },
            "embedding": {
                "type": "openai_compatible",
                "base_url": "https://y/v1",
                "api_key": "k",
                "models": [{"id": "embed", "capabilities": ["embed"]}],
            },
        },
        "tiers": {
            "primary": {"provider": "primary", "model": "chat"},
            "judge": {"auto": True},
        },
    }

    resolved = resolve_tiers(cfg)

    assert resolved.judge == resolved.primary
    assert resolved.judge_used_primary_fallback is True


def test_judge_auto_ignores_provider_without_team_secret() -> None:
    cfg = _cfg(
        {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "judge": {"auto": True},
        }
    )
    cfg["providers"]["qwen"]["api_key"] = ""
    cfg["providers"]["doubao"]["api_key"] = ""

    resolved = resolve_tiers(cfg)

    # The other providers can't be called; the main provider's other chat model is used instead.
    assert resolved.judge == ResolvedTierRef("deepseek", "deepseek-v4-flash")
    assert resolved.judge_used_primary_fallback is False


def test_defaults_follow_and_auto_when_omitted() -> None:
    cfg = _cfg({"primary": {"provider": "deepseek", "model": "deepseek-v4-pro"}})
    bindings = {(b.agent_type, b.profile): b for b in tier_agent_bindings(cfg)}
    assert bindings[("echo", None)].primary == ("deepseek", "deepseek-v4-pro")  # fast defaults to follow
    assert bindings[("judge", None)].primary[0] != "deepseek"  # judge defaults to auto heterogeneous


# ─── validation ─────────────────────────────────────────────────────


def test_validate_ok() -> None:
    validate_tiers(
        {"primary": {"provider": "deepseek", "model": "deepseek-v4-pro"}, "fast": {"follow": "primary"}, "judge": {"auto": True}},
        _providers(),
    )


def test_validate_rejects_missing_primary() -> None:
    with pytest.raises(TierConfigError):
        validate_tiers({"fast": {"follow": "primary"}}, _providers())


def test_validate_rejects_unknown_provider() -> None:
    with pytest.raises(TierConfigError):
        validate_tiers({"primary": {"provider": "ghost", "model": "m"}}, _providers())


def test_validate_rejects_empty_model() -> None:
    with pytest.raises(TierConfigError):
        validate_tiers({"primary": {"provider": "deepseek", "model": ""}}, _providers())


def test_validate_rejects_primary_in_fallback() -> None:
    with pytest.raises(
        TierConfigError,
        match=r"tiers\.primary_fallback\[0\] must differ from tiers\.primary",
    ):
        validate_tiers(
            {
                "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
                "primary_fallback": [
                    {"provider": "deepseek", "model": "deepseek-v4-pro"},
                ],
            },
            _providers(),
        )


def test_validate_rejects_duplicate_fallback() -> None:
    with pytest.raises(
        TierConfigError,
        match=r"tiers\.primary_fallback\[1\] must not duplicate an earlier fallback",
    ):
        validate_tiers(
            {
                "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
                "primary_fallback": [
                    {"provider": "qwen", "model": "qwen3.6-flash"},
                    {"provider": "qwen", "model": "qwen3.6-flash"},
                ],
            },
            _providers(),
        )


def test_validate_rejects_bad_follow_target() -> None:
    with pytest.raises(TierConfigError):
        validate_tiers(
            {"primary": {"provider": "deepseek", "model": "deepseek-v4-pro"}, "fast": {"follow": "judge"}},
            _providers(),
        )


def test_validate_explicit_fast_and_judge() -> None:
    validate_tiers(
        {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "fast": {"provider": "qwen", "model": "qwen3.6-flash"},
            "judge": {"provider": "doubao", "model": "Doubao-Seed-2.0-pro"},
        },
        _providers(),
    )


# ─── end-to-end wiring through team_llm_config ──────────────────────
# Exercise the real build path with trusted vendor base_url (SSRF-allowlisted, skips DNS).


def _tier_cfg_trusted() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "deepseek": {
                "type": "anthropic_compatible",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "k",
                "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
            },
            "qwen": {
                "type": "openai_compatible",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "k",
                "models": ["qwen3.6-plus", "qwen3.6-flash"],
            },
            "doubao": {
                "type": "anthropic_compatible",
                "base_url": "https://ark.cn-beijing.volces.com/api/coding",
                "api_key": "k",
                "models": ["Doubao-Seed-2.0-pro"],
            },
        },
        "tiers": {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "primary_fallback": [{"provider": "qwen", "model": "qwen3.6-plus"}],
            "fast": {"provider": "qwen", "model": "qwen3.6-flash"},
            "judge": {"provider": "doubao", "model": "Doubao-Seed-2.0-pro"},
        },
    }


def test_validate_v2_accepts_tier_config() -> None:
    validate_v2_config(_tier_cfg_trusted())  # No exception = tier branch accepted


def test_build_router_stamps_owner_team_id() -> None:
    """credit gap23 步1：团队 router 必须带上出生证明；不传则保持无归属。

    链条的另一半在 `test_team_router_registry_stamps_owner`（唯一生产构造点真的传了它）
    与 `bowhead_router/tests/test_usage_metering.py`（chokepoint 真的用了它）。
    """
    assert build_router_from_v2_config(_tier_cfg_trusted(), owner_team_id="team_x").owner_team_id == "team_x"
    assert build_router_from_v2_config(_tier_cfg_trusted()).owner_team_id is None


@pytest.mark.asyncio
async def test_team_router_registry_stamps_owner() -> None:
    """★ 唯一生产构造点（TeamRouterRegistry）必须把它按 team_id 键控的那个 id 盖上去。

    这条断言守的是：**没有任何调用点需要申报 team**——registry 拿的 team_id 直接进 router。
    """
    from bowhead_core.team_router_registry import TeamRouterRegistry

    registry = TeamRouterRegistry()
    router = await registry.get("team_from_registry", _tier_cfg_trusted(), version=1)

    assert router is not None
    assert router.owner_team_id == "team_from_registry"


def test_build_router_from_tier_config_resolves_all_tiers() -> None:
    router = build_router_from_v2_config(_tier_cfg_trusted())
    assert router.resolve("lead").primary == ("deepseek", "deepseek-v4-pro")
    assert router.resolve("lead").fallback == [("qwen", "qwen3.6-plus")]
    assert router.resolve("echo").primary == ("qwen", "qwen3.6-flash")
    assert router.resolve("judge").primary == ("doubao", "Doubao-Seed-2.0-pro")
    assert router.resolve("narrative").primary == ("deepseek", "deepseek-v4-pro")
    assert router.resolve("analyst", profile="structured").primary == ("qwen", "qwen3.6-flash")
    # Concrete proof of issue B's fix: home_proposal always has a binding under tier configuration (legacy migration missed it).
    assert router.resolve("home_proposal").primary == ("qwen", "qwen3.6-flash")


def test_build_catalog_default_model_from_primary_tier() -> None:
    cat = build_catalog(config=_tier_cfg_trusted(), version=7)
    assert cat["default_model"] == {"provider": "deepseek", "model": "deepseek-v4-pro"}
    assert cat["version"] == 7
    assert {p["id"] for p in cat["providers"]} == {"deepseek", "qwen", "doubao"}


def test_legacy_duplicate_fallbacks_are_runtime_compatible_but_not_writable() -> None:
    cfg = _tier_cfg_trusted()
    cfg["tiers"]["primary_fallback"] = [
        {"provider": "deepseek", "model": "deepseek-v4-pro"},
        {"provider": "qwen", "model": "qwen3.6-plus"},
        {"provider": "qwen", "model": "qwen3.6-plus"},
    ]

    with pytest.raises(TeamLlmConfigError, match="must differ from tiers.primary"):
        validate_v2_config(cfg)

    router = build_router_from_v2_config(cfg)
    assert router.resolve("lead").fallback == [("qwen", "qwen3.6-plus")]

    catalog = build_catalog(config=cfg, version=8)
    assert catalog["default_model"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
    }
    assert {provider["id"] for provider in catalog["providers"]} == {
        "deepseek",
        "qwen",
        "doubao",
    }


def test_build_router_rejects_empty_tier_primary() -> None:
    import pytest as _pytest

    bad = _tier_cfg_trusted()
    bad["tiers"]["primary"] = {"provider": "deepseek", "model": ""}
    with _pytest.raises(TeamLlmConfigError):
        build_router_from_v2_config(bad)
