from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from bowhead_core.agents.exceptions import ManifestSecurityError
from bowhead_core.agents.manifest import AgentManifest
from bowhead_core.agents.manifest_security import enforce_user_constraints, merge_user_over_system
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.system_agent_marketplace import (
    SYSTEM_AGENT_CAPABILITIES,
    SYSTEM_AGENT_LISTINGS,
    listed_system_agents,
    system_agent_localized_display_name,
    system_agent_team_override_fields,
    system_manifest_names,
    system_reserved_display_names,
)
from pydantic import ValidationError


def test_compute_card_shows_report_writing() -> None:
    """compute 是主力报告写手（含续轮修订）；市场卡片只显示前 3 个标签（agent-marketplace.tsx），「撰写报告」要排进前 3。"""
    capabilities = SYSTEM_AGENT_LISTINGS["compute"]["capabilities"]
    assert "cap_report" in capabilities[:3]
    assert "cap_compute" in capabilities  # 挤出卡片的那一项仍保留在列表里


def test_every_listed_capability_has_a_label() -> None:
    for listing in SYSTEM_AGENT_LISTINGS.values():
        assert all(capability in SYSTEM_AGENT_CAPABILITIES for capability in listing["capabilities"])


def test_listing_comes_from_the_manifests_in_their_order() -> None:
    registry = AgentRegistry.bootstrap()
    expected = sorted(
        (registry.get(name).listing.order, name)
        for name in registry.list_all()
        if registry.get(name).listing is not None and registry.get(name).listing.listed
    )
    assert list(listed_system_agents()) == [name for _, name in expected]
    assert next(iter(listed_system_agents())) == "configuration-agent"
    assert "lead" not in listed_system_agents()
    assert registry.get("lead").listing is None
    assert "lead" in system_manifest_names()


def test_platform_policy_agents_allow_only_on_off_name_and_description() -> None:
    assert system_agent_team_override_fields("configuration-agent") == {"enabled", "display_name", "description"}
    # Coach also lets Teams set its per-Turn step limit; model, Skills and prompt stay platform policy.
    assert system_agent_team_override_fields("agent-coach") == {"enabled", "display_name", "description", "max_iterations"}
    assert len(system_agent_team_override_fields("compute")) == 8
    # A profile without a system listing keeps the historical behaviour.
    assert len(system_agent_team_override_fields("not-a-system-agent")) == 8


def _manifest(**listing: Any) -> dict[str, Any]:
    return {
        "name": "sample-agent",
        "model": "analyst",
        "trust_class": "read-only",
        "max_iterations": 5,
        "max_output_tokens": 1000,
        "description": "Sample",
        "tools": {"allow": ["subagent_return"]},
        "listing": {"order": 5, "display_name": "Sample", "summary": "Sample summary.", **listing},
    }


@pytest.mark.parametrize(
    "listing",
    [
        {"capabilities": ["cap_unknown"]},
        {"capabilities": ["cap_report", "cap_report"]},
        {"team_overrides": ["tools"]},
        {"team_overrides": ["enabled", "enabled"]},
        {"surprise": True},
    ],
)
def test_listing_is_validated(listing: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(_manifest(**listing))


def test_listing_keeps_capability_order_and_defaults_to_unlisted() -> None:
    manifest = AgentManifest.model_validate(_manifest(capabilities=["cap_report", "cap_analysis"]))
    assert manifest.listing is not None
    assert manifest.listing.capabilities == ["cap_report", "cap_analysis"]
    assert manifest.listing.listed is False
    assert manifest.listing.team_overrides is None


def test_user_manifest_cannot_set_or_drop_a_listing() -> None:
    with pytest.raises(ManifestSecurityError):
        enforce_user_constraints(_manifest(), plan_tier="enterprise")
    system = AgentRegistry.bootstrap().get("compute")
    user = system.model_copy(update={"listing": None})
    assert merge_user_over_system(user, system).listing == system.listing


def test_listing_never_reaches_the_runtime_profile() -> None:
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile

    manifest = AgentRegistry.bootstrap().get("compute")
    assert manifest_to_profile(manifest) == manifest_to_profile(manifest.model_copy(update={"listing": None}))


def test_importing_the_module_does_not_load_the_registry() -> None:
    code = (
        "import bowhead_core.system_agent_marketplace as m;"
        "assert m._system_manifest_listings.cache_info().currsize == 0;"
        "assert 'configuration-agent' in m.SYSTEM_AGENT_LISTINGS;"
        "assert m._system_manifest_listings.cache_info().currsize == 1"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_names_use_the_marketplace_messages_in_every_locale() -> None:
    assert system_agent_localized_display_name("configuration-agent", "zh-CN") == "配置智能体"
    assert system_agent_localized_display_name("configuration-agent", "de-DE") == "Konfigurationsagent"
    assert system_agent_localized_display_name("compute", "en-US") == SYSTEM_AGENT_LISTINGS["compute"]["display_name"]
    reserved = system_reserved_display_names()
    assert {"配置智能体", "Konfigurationsagent", "Agent de configuration", "主控", "HR"} <= reserved
