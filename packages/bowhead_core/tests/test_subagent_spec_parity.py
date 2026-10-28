"""Parity tests for subagent-unification Phase 1.

These lock the additive contract of Phase 1 (SubagentProfile new fields +
manifest_to_profile mapping + get_compute_profile deck_inputs + the two
registry-resolve feature flags defaulting OFF) so later phases can't silently
regress it.

TODO(subagent-unification Phase B entry gate): once the registry-compile path is
wired into _resolve_profile with a live registry handle, add the full byte-parity
matrix -- compute system_prompt across progressive_load x provenance (4 combos),
plus modeler -- snapshotting the legacy builder as golden and asserting the
registry-compiled SubagentProfile.system_prompt is byte-identical. See
tech-design section 5.1.
"""

from __future__ import annotations

import os
from unittest import mock

from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.manifest import AgentManifest
from bowhead_core.subagent import (
    PROFILES,
    SubagentProfile,
    get_compute_profile,
    is_subagent_spec_resolve_enabled,
    is_subagent_spec_routing_enabled,
)


def _minimal_manifest(name: str, description: str = "desc", produces: list[str] | None = None) -> AgentManifest:
    """Build a minimal valid AgentManifest for `name`."""
    return AgentManifest.model_validate(
        {
            "name": name,
            "model": "analyst",
            "trust_class": "read-mostly",
            "max_iterations": 10,
            "max_output_tokens": 2000,
            "description": description,
            "tools": {"allow": ["bi_query"]},
            "system": None,
            "produces": produces or [],
        }
    )


def test_subagent_profile_has_new_fields_with_defaults():
    """SubagentProfile gains description + deck_inputs, both defaulted."""
    p = SubagentProfile(
        name="x",
        llm_agent_type="analyst",
        tool_whitelist=(),
        system_prompt="sp",
    )
    assert p.description == ""
    assert p.deck_inputs is False


def test_manifest_to_profile_maps_description():
    """description flows manifest → profile."""
    m = _minimal_manifest("kb-analyst", description="KB stuff")
    p = manifest_to_profile(m)
    assert p.description == "KB stuff"


def test_manifest_to_profile_deck_inputs_is_declaration_driven():
    """deck_inputs 由 `produces` **声明**决定，不再看名字（capability-routing 2a）。

    ★ 这条测试原本断言的是「name-derived True for compute/consultant」——那正是被
    退休掉的第 ④ 处名字硬编码。改判据而不是删测试：它真正要守的东西没变，
    是「deck 钩子只属于确实会产出 .pptx 的那些 agent」。多给一个 = 收尾去翻不存在的
    产物；少给一个 = **用户拿不到文件**（「23 次要文件只出成 4 次」那个老伤）。

    ⚠ 名字不再有特权：一个叫 "compute" 但**没声明** produces 的 manifest（例如团队用
    user manifest 覆盖了同名内置）拿不到 deck 钩子。这与既有的覆盖语义一致 ——
    `manifest_security` v1 是「user manifest 整份替换 system」，`description` /
    `examples` 本来也会一起丢，覆盖方本就要把想要的都写全。
    真 system manifest 已声明 `produces: [pptx]`，集合相等由
    `test_agent_capabilities.test_deck_inputs_derived_from_produces_*` 钉住。
    """
    assert manifest_to_profile(_minimal_manifest("compute", produces=["pptx"])).deck_inputs is True
    assert manifest_to_profile(_minimal_manifest("consultant", produces=["pptx"])).deck_inputs is True
    # Declaring another format does not imply deck capability.
    assert manifest_to_profile(_minimal_manifest("consultant", produces=["xlsx"])).deck_inputs is False
    # Correct name without declaration → no hook.
    assert manifest_to_profile(_minimal_manifest("compute")).deck_inputs is False
    assert manifest_to_profile(_minimal_manifest("kb-analyst")).deck_inputs is False


def test_get_compute_profile_deck_inputs_true():
    """compute profile carries deck_inputs=True (Phase 4 deck-hook prep)."""
    assert get_compute_profile().deck_inputs is True


def test_profiles_compute_deck_inputs_true():
    """The module-level PROFILES['compute'] also carries deck_inputs=True."""
    assert PROFILES["compute"].deck_inputs is True


def test_profiles_modeler_removed():
    """modeler profile 已删（2026-07-21，随 4 个 BI 写工具整族删除）。

    原断言是 deck_inputs=False（非 deck 生产者）；profile 不复存在后，
    改为守住「不许悄悄加回来」——它一旦回来就是 0 可见工具的坏 profile。
    """
    assert "modeler" not in PROFILES


# ── Feature-flag rollback-rope contract (2026-06-05: routing flag flip defaults ON) ──


def test_spec_flags_default_clean_env():
    """两个 spec flag 现在都**默认 ON**，显式 `=0` 才是回退绳。

    routing：2026-06-05 用户拍板「加 peer = 纯配置」，动态 peer block 成为默认路径。
    resolve：2026-08-04 用户拍板。安全依据是
    `test_registry_first_matches_profiles_after_boot` —— 启动后两条内置来源逐字段等价。
    """
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED", None)
        os.environ.pop("BOWHEAD_SUBAGENT_SPEC_ROUTING_ENABLED", None)
        assert is_subagent_spec_resolve_enabled() is True
        assert is_subagent_spec_routing_enabled() is True


def test_spec_routing_flag_off_when_env_falsy():
    """Explicit zero selects manifest-only peers for home proposals."""
    with mock.patch.dict(
        os.environ,
        {"BOWHEAD_SUBAGENT_SPEC_ROUTING_ENABLED": "0"},
        clear=False,
    ):
        assert is_subagent_spec_routing_enabled() is False


def test_spec_flags_independent_of_provenance_master():
    """Flags 不与 provenance 主开关耦合 —— 只认自己的 env var。

    主开关强制 ON/OFF 都不该动摇 resolve flag 的取值：不设时按自身默认（ON），
    显式 `=0` 时 OFF。（原断言写的是「主开关 ON 时 spec flag 仍 OFF」，那只是当时默认值
    恰好为 OFF 的副产品，不是「不耦合」这个性质本身；改成两向都钉才真的在测解耦。）
    """
    with mock.patch.dict(
        os.environ,
        {"BOWHEAD_PROVENANCE_HITL_ENABLED": "true"},
        clear=False,
    ):
        os.environ.pop("BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED", None)
        assert is_subagent_spec_resolve_enabled() is True
    with mock.patch.dict(
        os.environ,
        {
            "BOWHEAD_PROVENANCE_HITL_ENABLED": "true",
            "BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED": "0",
        },
        clear=False,
    ):
        assert is_subagent_spec_resolve_enabled() is False


def test_spec_resolve_flag_on_when_env_truthy():
    """Explicit truthy env turns the resolve flag ON."""
    with mock.patch.dict(
        os.environ,
        {"BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED": "true"},
        clear=False,
    ):
        assert is_subagent_spec_resolve_enabled() is True
    with mock.patch.dict(
        os.environ,
        {"BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED": "1"},
        clear=False,
    ):
        assert is_subagent_spec_resolve_enabled() is True


def test_spec_routing_flag_on_when_env_truthy():
    """Explicit truthy env turns the routing flag ON (reader works; no consumer yet)."""
    with mock.patch.dict(
        os.environ,
        {"BOWHEAD_SUBAGENT_SPEC_ROUTING_ENABLED": "on"},
        clear=False,
    ):
        assert is_subagent_spec_routing_enabled() is True


def test_registry_first_matches_profiles_after_boot() -> None:
    """`BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED` 默认 ON 的**安全依据**：启动后两条内置来源等价。

    `_resolve_profile` 的 registry-first 分支在 PROFILES 分支之前 return。默认开之所以不改变
    行为，是因为 `register_all_system_manifests` 在 boot 时就是用同一个 `manifest_to_profile`
    把 manifest 灌进 PROFILES 的 —— 两边同源。（模块级那份手写 `get_compute_profile()` 只是
    未启动时的占位，boot 时被覆盖；拿未启动的 PROFILES 去比会看到假差异。）

    这条一旦红，意味着有人让某个内置的两条来源分叉了 —— 此时**默认 ON 就不再安全**，
    要么修分叉，要么把默认改回 OFF。别只把断言放宽。
    """
    import copy

    from bowhead_core.agents.agent_runner_adapter import (
        manifest_to_profile,
        register_all_system_manifests,
    )
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES

    snapshot = copy.copy(PROFILES)  # Global dict: restore after testing to avoid contaminating other tests in this process.
    try:
        registry = AgentRegistry.bootstrap()
        register_all_system_manifests(registry)
        compared = 0
        for name in sorted(PROFILES):
            if name == "lead":  # The orchestrator itself, not a dispatched peer.
                continue
            live = PROFILES[name]
            from_registry = manifest_to_profile(registry.get(name))
            for field in (
                "llm_agent_type",
                "max_iterations",
                "max_output_tokens",
                "deck_inputs",
                "system_prompt",
                "tool_whitelist",
            ):
                assert getattr(live, field) == getattr(from_registry, field), f"{name}.{field} 两条来源分叉"
            compared += 1
        # After removing canvas-writer in c4d50b03b, seven non-lead built-in manifests remain (see test_specialist_count).
        assert compared >= 7, f"只比到 {compared} 个内置，manifest 加载可能回归了"
    finally:
        PROFILES.clear()
        PROFILES.update(snapshot)
