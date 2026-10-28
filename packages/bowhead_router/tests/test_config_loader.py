"""config_loader 测试：YAML → Router."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_router.config_loader import (
    REQUIRED_AGENT_TYPES,
    RoutingConfigError,
    _deep_merge,
    _interpolate,
    load_routing_config,
)

from bowhead_router import build_router_from_config

if TYPE_CHECKING:
    from pathlib import Path

SAMPLE_YAML = """\
providers:
  aliyun_qwen:
    type: openai_compatible
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_ref: "env://ALIYUN_DASHSCOPE_API_KEY"
    timeout_sec: 60
    supports:
      tool_use: true

  secondary:
    type: openai_compatible
    base_url: "https://api.moonshot.cn/v1"
    api_key_ref: "env://MOONSHOT_API_KEY:default-if-missing"

agents:
  lead:
    primary:  { provider: aliyun_qwen, model: qwen-plus }
    fallback: [{ provider: secondary,  model: moonshot-v1-8k }]
    max_output_tokens: 4000

  analyst:
    default:
      primary: { provider: aliyun_qwen, model: qwen-plus }
      max_output_tokens: 3000
    profiles:
      compute:
        primary: { provider: secondary, model: moonshot-v1-32k }
"""


@pytest.mark.unit
def test_interpolate_env_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert _interpolate("${FOO}") == "bar"
    assert _interpolate("${UNSET:default}") == "default"
    assert _interpolate({"x": "${FOO}"}) == {"x": "bar"}
    assert _interpolate(["${FOO}", "${UNSET:y}"]) == ["bar", "y"]


@pytest.mark.unit
def test_load_providers_and_agents(tmp_path: Path) -> None:
    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(SAMPLE_YAML)
    cfg = load_routing_config(cfg_path)

    # Both providers are parsed.
    names = [p.name for p in cfg.providers]
    assert "aliyun_qwen" in names and "secondary" in names

    # Preserve api_key_ref; Registry._build_adapter resolves the secret, not the loader.
    aliyun = next(p for p in cfg.providers if p.name == "aliyun_qwen")
    assert aliyun.type == "openai_compatible"
    assert aliyun.api_key_ref == "env://ALIYUN_DASHSCOPE_API_KEY"
    assert aliyun.supports == {"tool_use": True}

    # Agent bindings
    agent_types = {(b.agent_type, b.profile) for b in cfg.agent_bindings}
    assert ("lead", None) in agent_types
    assert ("analyst", None) in agent_types  # default
    assert ("analyst", "compute") in agent_types  # profile override

    lead = next(b for b in cfg.agent_bindings if b.agent_type == "lead")
    assert lead.primary == ("aliyun_qwen", "qwen-plus")
    assert lead.fallback == [("secondary", "moonshot-v1-8k")]
    assert lead.max_output_tokens == 4000


@pytest.mark.unit
def test_load_custom_provider_preserves_model_transport_bindings(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(
        """\
providers:
  enterprise:
    type: custom_http
    base_url: https://gateway.example.com
    auth: {type: none}
    transports:
      chat:
        contract: openai_chat_completions
        request:
          url_template: "{base_url}/chat"
          body_template:
            messages: {$bowhead: messages}
    models:
      - id: enterprise-chat
        capabilities: [chat]
        capability_transports: {chat: chat}
agents: {}
""",
    )

    provider = load_routing_config(cfg_path).providers[0]

    assert provider.models == [
        {
            "id": "enterprise-chat",
            "capabilities": ["chat"],
            "capability_transports": {"chat": "chat"},
        }
    ]
    assert "models" not in provider.extra


@pytest.mark.unit
def test_load_custom_provider_allows_full_url_without_base_url(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(
        """\
providers:
  enterprise:
    type: custom_http
    auth: {type: none}
    transports:
      chat:
        contract: openai_chat_completions
        request:
          url_template: "https://gateway.example.com/chat"
          body_template:
            messages: {$bowhead: messages}
    models:
      - id: enterprise-chat
        capabilities: [chat]
        capability_transports: {chat: chat}
agents: {}
""",
    )

    provider = load_routing_config(cfg_path).providers[0]

    assert provider.base_url is None


@pytest.mark.unit
async def test_build_router_from_config_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIYUN_DASHSCOPE_API_KEY", "sk-dummy")
    monkeypatch.setenv("MOONSHOT_API_KEY", "moon-dummy")

    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(SAMPLE_YAML)

    # The test YAML binds only lead/analyst; skip the REQUIRED_AGENT_TYPES check.
    router, registry, _loaded = build_router_from_config(
        cfg_path,
        required_agents=(),
    )
    try:
        assert set(registry.list_names()) == {"aliyun_qwen", "secondary"}
        # The Lead binding can be resolved.
        lead = router.resolve("lead")
        assert lead.primary == ("aliyun_qwen", "qwen-plus")
        # The Analyst compute profile overrides the default binding.
        compute = router.resolve("analyst", profile="compute")
        assert compute.primary == ("secondary", "moonshot-v1-32k")
        # An unlisted Analyst profile falls back to the binding without a profile.
        default = router.resolve("analyst", profile="unlisted_profile")
        assert default.primary == ("aliyun_qwen", "qwen-plus")
    finally:
        await registry.close_all()


@pytest.mark.unit
def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_routing_config(tmp_path / "nope.yaml")


# ─────────────────────────────────────────────────────────
# #138: inheritance via extends
# ─────────────────────────────────────────────────────────


_BASE_YAML = """\
providers:
  prov_a:
    type: openai_compatible
    base_url: "https://a.example.com/v1"
    api_key_ref: "env://A_KEY"
  prov_b:
    type: openai_compatible
    base_url: "https://b.example.com/v1"
    api_key_ref: "env://B_KEY"

agents:
  lead:
    primary:  { provider: prov_a, model: a-large }
    fallback: [{ provider: prov_b, model: b-large }]
    max_output_tokens: 4000
  modeler:
    primary: { provider: prov_a, model: a-large }
    fallback: [{ provider: prov_b, model: b-large }]
  narrative:
    primary: { provider: prov_a, model: a-large }
    fallback: [{ provider: prov_b, model: b-large }]
  analyst:
    default:
      primary: { provider: prov_a, model: a-mid }
      fallback: [{ provider: prov_b, model: b-mid }]
  echo:
    primary: { provider: prov_a, model: a-tiny }
    fallback: [{ provider: prov_b, model: b-tiny }]
  judge:
    primary: { provider: prov_a, model: a-large }
    fallback: [{ provider: prov_b, model: b-large }]
"""


_CHILD_YAML_OVERRIDES_LEAD = """\
extends: ./base.yaml

agents:
  lead:
    primary: { provider: prov_b, model: b-XL }
    fallback: [{ provider: prov_a, model: a-large }]
"""


@pytest.mark.unit
def test_deep_merge_dict_recursive_list_replace() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "lst": [1, 2, 3]}
    over = {"nested": {"y": 99, "z": 3}, "lst": [9]}
    out = _deep_merge(base, over)
    assert out == {"a": 1, "nested": {"x": 1, "y": 99, "z": 3}, "lst": [9]}


@pytest.mark.unit
def test_extends_inherits_base_and_overrides_child(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text(_BASE_YAML)
    (tmp_path / "child.yaml").write_text(_CHILD_YAML_OVERRIDES_LEAD)

    cfg = load_routing_config(tmp_path / "child.yaml")
    # Inherit the provider section from base.
    names = {p.name for p in cfg.providers}
    assert names == {"prov_a", "prov_b"}
    # Inherit echo / judge / modeler / analyst from base.
    by_type = {(b.agent_type, b.profile): b for b in cfg.agent_bindings}
    assert ("echo", None) in by_type
    assert ("judge", None) in by_type
    assert ("modeler", None) in by_type
    # child overrides lead.
    lead = by_type[("lead", None)]
    assert lead.primary == ("prov_b", "b-XL")
    assert lead.fallback == [("prov_a", "a-large")]


@pytest.mark.unit
def test_extends_cycle_detected(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("extends: ./b.yaml\nproviders: {}\n")
    (tmp_path / "b.yaml").write_text("extends: ./a.yaml\nproviders: {}\n")
    with pytest.raises(RoutingConfigError, match="cycle"):
        load_routing_config(tmp_path / "a.yaml")


# ─────────────────────────────────────────────────────────
# #137: startup validation (REQUIRED_AGENT_TYPES + provider references)
# ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_raises_on_missing_required_agent(tmp_path: Path) -> None:
    # SAMPLE_YAML has only lead + analyst; modeler/echo/judge are missing.
    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(SAMPLE_YAML)
    with pytest.raises(RoutingConfigError, match="missing required agent_type"):
        build_router_from_config(cfg_path)


@pytest.mark.unit
def test_build_raises_on_unknown_provider_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.setenv("B_KEY", "y")
    bad_yaml = _BASE_YAML.replace(
        "primary:  { provider: prov_a, model: a-large }",
        "primary:  { provider: ghost_provider, model: a-large }",
        1,  # only the lead.primary
    )
    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(bad_yaml)
    with pytest.raises(RoutingConfigError, match="unknown providers"):
        build_router_from_config(cfg_path)


@pytest.mark.unit
async def test_build_passes_with_full_required_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.setenv("B_KEY", "y")
    cfg_path = tmp_path / "base.yaml"
    cfg_path.write_text(_BASE_YAML)
    router, registry, _loaded = build_router_from_config(cfg_path)
    try:
        for agent_type in REQUIRED_AGENT_TYPES:
            assert router.resolve(agent_type).primary[0] in {"prov_a", "prov_b"}
    finally:
        await registry.close_all()


@pytest.mark.unit
def test_required_agent_types_membership_lock() -> None:
    """写作收敛 T3(review P2): narrative 是报告写手的角色绑定,必须留在 REQUIRED
    集合里(fail-fast)——从集合里删掉它,其余校验测试仍然全绿,故需成员锁。"""
    assert "narrative" in REQUIRED_AGENT_TYPES
