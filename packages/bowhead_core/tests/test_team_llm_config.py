"""Team-scoped BYO LLM/KB config semantics."""

from __future__ import annotations

import copy
import json

import pytest
from bowhead_core.team_kb_runtime import (
    TeamKbRuntimeRegistry,
    diagnose_kb_runtime_config,
)
from bowhead_core.team_llm_config import (
    TeamLlmConfigError,
    build_catalog,
    build_router_from_v2_config,
    is_allowed_preferred_model,
    migrate_legacy_config_to_v2,
    provider_configs_from_v2,
    redact_config,
    validate_v2_config,
)
from bowhead_router.config_loader import LoadedRoutingConfig
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, UserModelPref


def _config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "openai_org": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "sk-openai-team-secret-1234567890",
                "timeout_sec": 45,
                "max_retries": 2,
                "models": ["gpt-4.1", "gpt-4o-mini"],
                "supports": {"tools": True, "vision": True},
                "default_headers": {"X-Team": "bowhead"},
            },
            "anthropic_team": {
                "type": "anthropic_compatible",
                "base_url": "https://93.184.216.35",
                "api_key": "sk-ant-team-secret-1234567890",
                "models": ["claude-3-5-sonnet"],
                "messages_path": "/v1/messages",
            },
        },
        "agents": {
            "lead": {
                "primary": {"provider": "openai_org", "model": "gpt-4.1"},
                "fallback": [{"provider": "anthropic_team", "model": "claude-3-5-sonnet"}],
            },
            "modeler": {
                "primary": {"provider": "openai_org", "model": "gpt-4o-mini"},
                "fallback": [],
            },
            "analyst": {
                "default": {
                    "primary": {"provider": "anthropic_team", "model": "claude-3-5-sonnet"},
                    "fallback": [{"provider": "openai_org", "model": "gpt-4.1"}],
                },
                "profiles": {
                    "financial": {
                        "primary": {"provider": "openai_org", "model": "gpt-4.1"},
                        "fallback": [],
                    }
                },
            },
            "narrative": {
                "primary": {"provider": "openai_org", "model": "gpt-4o-mini"},
                "fallback": [],
            },
            "echo": {
                "primary": {"provider": "openai_org", "model": "gpt-4o-mini"},
                "fallback": [],
            },
            "judge": {
                "primary": {"provider": "anthropic_team", "model": "claude-3-5-sonnet"},
                "fallback": [],
            },
        },
        "kb": {
            "models": {
                "embed_small": {
                    "provider": "openai_org",
                    "real_id": "text-embedding-3-small",
                    "capability": "embed",
                    "dim": 1536,
                },
                "vision_fast": {
                    "provider": "openai_org",
                    "real_id": "gpt-4o-mini",
                    "capability": "vision",
                },
                "row_fast": {
                    "provider": "anthropic_team",
                    "real_id": "claude-3-5-sonnet",
                    "capability": "row_extractor",
                },
            },
            "active": {
                "embed": "embed_small",
                "vision": "vision_fast",
                "row_extractor": "row_fast",
            },
            "index_version": "kbidx_current",
        },
    }


def test_model_credit_rates_validate_and_reach_runtime_router() -> None:
    config = _config()
    config["providers"]["openai_org"]["models"][0] = {
        "id": "gpt-4.1",
        "credit_rates": {"input": "2", "cached_input": "0.25", "output": "4"},
    }
    validate_v2_config(config)
    router = build_router_from_v2_config(config)
    assert router.model_credit_rates[("openai_org", "gpt-4.1")] == {
        "input": "2",
        "cached_input": "0.25",
        "output": "4",
    }
    config["providers"]["openai_org"]["models"][0]["credit_rates"]["cached_input"] = "-1"
    with pytest.raises(TeamLlmConfigError, match="credit_rates"):
        validate_v2_config(config)


def test_validate_rejects_unknown_agent_provider() -> None:
    cfg = _config()
    cfg["agents"]["lead"]["primary"]["provider"] = "missing_provider"

    with pytest.raises(TeamLlmConfigError, match="unknown"):
        validate_v2_config(cfg)


@pytest.mark.parametrize("count", [1, 15, 30, 50])
def test_knowledge_retrieval_count_is_a_valid_team_setting(count: int) -> None:
    config = _config()
    config["kb"]["retrieval_top_k"] = count
    validate_v2_config(config)


@pytest.mark.parametrize("count", [0, 51, True, "15"])
def test_knowledge_retrieval_count_rejects_invalid_values(count) -> None:
    config = _config()
    config["kb"]["retrieval_top_k"] = count
    with pytest.raises(TeamLlmConfigError, match=r"kb.retrieval_top_k"):
        validate_v2_config(config)


def test_redact_config_removes_secret_values_and_secret_key_names() -> None:
    config = _config()
    config["providers"]["openai_org"]["default_headers"].update(
        {
            "X-Auth-Token": "tenant-secret-token",
            "Cookie": "session=private",
        }
    )
    redacted = redact_config(config)
    payload = json.dumps(redacted, sort_keys=True)

    assert "sk-openai-team-secret" not in payload
    assert "sk-ant-team-secret" not in payload
    assert "api_key" not in payload
    assert "fingerprint" in payload
    assert "tenant-secret-token" not in payload
    assert "session=private" not in payload
    assert "X-Auth-Token_fingerprint" in payload
    assert "Cookie_fingerprint" in payload


def test_catalog_is_secret_free_and_limits_preferred_models() -> None:
    catalog = build_catalog(config=_config(), version=7)
    payload = json.dumps(catalog, sort_keys=True)

    assert catalog["version"] == 7
    assert catalog["default_model"] == {"provider": "openai_org", "model": "gpt-4.1"}
    assert is_allowed_preferred_model(catalog, "openai_org", "gpt-4.1")
    assert is_allowed_preferred_model(catalog, "anthropic_team", "claude-3-5-sonnet")
    assert not is_allowed_preferred_model(catalog, "openai_org", "not-configured")
    assert "sk-openai-team-secret" not in payload
    assert "api_key" not in payload
    assert catalog["kb"]["active"]["embed"] == "embed_small"


def test_provider_configs_preserve_adapter_extras_without_secret_leak() -> None:
    configs = provider_configs_from_v2(_config())
    by_name = {cfg.name: cfg for cfg in configs}

    assert by_name["openai_org"].type == "openai_compatible"
    assert by_name["openai_org"].timeout_sec == 45
    assert by_name["anthropic_team"].extra["messages_path"] == "/v1/messages"
    assert by_name["openai_org"].extra["ssrf_guard"] is True


def test_azure_provider_round_trip_redaction_and_registry() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"] = {
        "type": "azure_openai",
        "endpoint": "https://93.184.216.34",
        "api_version": "2024-10-21",
        "auth": {"type": "bearer", "secret": "uptimize-secret"},
        "models": [
            {
                "id": "gpt-51",
                "deployment": "gpt-51",
                "capabilities": ["chat", "tool"],
            },
            {
                "id": "text-embedding-3-small",
                "capabilities": ["embed"],
            },
        ],
    }
    for agent_type, spec in cfg["agents"].items():
        targets = [spec["default"], *spec.get("profiles", {}).values()] if agent_type == "analyst" else [spec]
        for target in targets:
            target["primary"] = {
                "provider": "openai_org",
                "model": "gpt-51",
            }
            target["fallback"] = []
    cfg["kb"]["models"]["embed_small"]["real_id"] = "text-embedding-3-small"
    cfg["kb"]["models"] = {
        "embed_small": cfg["kb"]["models"]["embed_small"],
    }
    cfg["kb"]["active"] = {"embed": "embed_small"}

    validate_v2_config(cfg)
    providers = provider_configs_from_v2(cfg)
    azure = next(provider for provider in providers if provider.name == "openai_org")
    redacted = json.dumps(redact_config(cfg), sort_keys=True)

    assert azure.type == "azure_openai"
    assert azure.extra["endpoint"] == "https://93.184.216.34"
    assert azure.extra["api_version"] == "2024-10-21"
    assert azure.models[1]["capabilities"] == ["embed"]
    assert "uptimize-secret" not in redacted
    assert '"secret"' not in redacted
    assert "fingerprint" in redacted


def test_embedding_only_model_cannot_be_selected_for_chat() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [
        {
            "id": "text-embedding-3-small",
            "capabilities": ["embed"],
        }
    ]
    cfg["agents"]["lead"]["primary"] = {
        "provider": "openai_org",
        "model": "text-embedding-3-small",
    }

    with pytest.raises(TeamLlmConfigError, match="not chat-capable"):
        validate_v2_config(cfg)


def test_azure_rejects_deployment_level_endpoint() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"] = {
        "type": "azure_openai",
        "endpoint": ("https://93.184.216.34/openai/deployments/gpt-51/chat/completions"),
        "api_version": "2024-10-21",
        "api_key": "secret",
        "models": [{"id": "gpt-51", "capabilities": ["chat"]}],
    }
    cfg["agents"]["lead"]["primary"] = {
        "provider": "openai_org",
        "model": "gpt-51",
    }

    with pytest.raises(TeamLlmConfigError, match="root endpoint"):
        validate_v2_config(cfg)


def test_custom_http_validates_the_rendered_target_not_only_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Outbound address validation applies only in SaaS mode (22014fa9e); local mode permits internal targets as required.
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    cfg = _config()
    cfg["providers"]["openai_org"] = {
        "type": "custom_http",
        "base_url": "https://93.184.216.34",
        "auth": {"type": "none"},
        "transports": {
            "chat": {
                "contract": "openai_chat_completions",
                "request": {
                    "method": "POST",
                    "url_template": "http://169.254.169.254/latest",
                    "body_template": {
                        "messages": {"$bowhead": "messages"},
                    },
                },
            }
        },
        "models": [
            {
                "id": "internal-chat",
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
            }
        ],
    }
    cfg["agents"]["lead"]["primary"] = {
        "provider": "openai_org",
        "model": "internal-chat",
    }
    # Replacing openai_org removes the old KB embedding model (kb.models references are validated since 4460e3646);
    # clear KB like the preceding related case so only the rendered chat target determines the result.
    cfg["kb"] = {}

    # 22014fa9e standardized rejection copy from "SSRF guard" to "blocked by deployment mode".
    # Enforce at chat transport (rendered url_template); base_url itself is public HTTPS and allowed.
    with pytest.raises(TeamLlmConfigError, match=r"models\[internal-chat\]\.chat: .*blocked by deployment mode"):
        validate_v2_config(cfg)


def test_custom_http_accepts_a_full_url_without_base_url() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"] = {
        "type": "custom_http",
        "auth": {"type": "none"},
        "transports": {
            "chat": {
                "contract": "openai_chat_completions",
                "request": {
                    "method": "POST",
                    "url_template": "https://93.184.216.34/company/chat",
                    "body_template": {
                        "messages": {"$bowhead": "messages"},
                    },
                },
            }
        },
        "models": [
            {
                "id": "internal-chat",
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
            }
        ],
    }
    cfg["agents"]["lead"]["primary"] = {
        "provider": "openai_org",
        "model": "internal-chat",
    }
    cfg["kb"] = {}

    validate_v2_config(cfg)


@pytest.mark.asyncio
async def test_team_provider_adapters_enable_runtime_ssrf_guard() -> None:
    registry = ProviderRegistry()
    registry.load(provider_configs_from_v2(_config()))
    try:
        assert vars(registry.get("openai_org"))["ssrf_guard"] is True
        assert vars(registry.get("anthropic_team"))["ssrf_guard"] is True
    finally:
        await registry.close_all()


def test_build_router_from_v2_config_preserves_bindings_and_user_pref() -> None:
    router = build_router_from_v2_config(_config())
    bindings = router.list_bindings()

    assert ("lead", None, ("openai_org", "gpt-4.1"), None) in bindings
    assert ("analyst", "financial", ("openai_org", "gpt-4.1"), None) in bindings
    assert router.resolve(
        "lead",
        user_pref=UserModelPref(provider="anthropic_team", model="claude-3-5-sonnet"),
    ).primary == ("anthropic_team", "claude-3-5-sonnet")
    # Decision C (model tiers): analyst belongs to primary, now also overridden by user_pref (the old assertion that analyst ignores
    # user_pref was obsolete {lead,composer}-era behavior). Non-primary (echo/fast) ignoring user_pref
    # overrides is covered by test_user_pref_routing::test_user_pref_does_NOT_apply_to_echo.
    assert router.resolve(
        "analyst",
        user_pref=UserModelPref(provider="openai_org", model="gpt-4o-mini"),
    ).primary == ("openai_org", "gpt-4o-mini")


def test_embedding_model_requires_positive_dim() -> None:
    cfg = copy.deepcopy(_config())
    cfg["kb"]["models"]["embed_small"]["dim"] = 0

    with pytest.raises(TeamLlmConfigError, match="dim"):
        validate_v2_config(cfg)


def test_validate_rejects_provider_base_url_to_cloud_metadata_in_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    cfg = copy.deepcopy(_config())
    cfg["providers"]["openai_org"]["base_url"] = "http://169.254.169.254/latest/meta-data"

    with pytest.raises(TeamLlmConfigError, match="blocked by deployment mode"):
        validate_v2_config(cfg)


def test_validate_accepts_private_http_provider_base_url_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    cfg = copy.deepcopy(_config())
    cfg["providers"]["openai_org"]["base_url"] = "http://10.0.0.5:8080/v1"

    validate_v2_config(cfg)


def test_migrate_legacy_full_config_to_v2_router() -> None:
    legacy = {
        "providers": [
            {
                "name": "legacy_openai",
                "type": "openai_compatible",
                "base_url": "https://93.184.216.36/v1",
                "api_key": "sk-legacy-secret-1234567890",
                "models": ["legacy-large", "legacy-small"],
            }
        ],
        "agent_routing": {
            "lead": "legacy_openai/legacy-large",
            "composer": "legacy_openai/legacy-small",
            "critic": "legacy_openai/legacy-small",
        },
    }

    migrated = migrate_legacy_config_to_v2(legacy)

    validate_v2_config(migrated)
    assert migrated["schema_version"] == 2
    assert migrated["agents"]["narrative"]["primary"] == {
        "provider": "legacy_openai",
        "model": "legacy-small",
    }
    assert migrated["agents"]["judge"]["primary"] == {
        "provider": "legacy_openai",
        "model": "legacy-small",
    }
    router = build_router_from_v2_config(legacy)
    assert router.resolve("lead").primary == ("legacy_openai", "legacy-large")


def test_migrate_legacy_key_only_provider_uses_vendor_defaults_without_secret_leak() -> None:
    legacy = {
        "providers": [
            {
                "provider_name": "deepseek",
                "api_key": "sk-deepseek-legacy-secret-1234567890",
                "role": "lead",
            }
        ]
    }

    migrated = migrate_legacy_config_to_v2(legacy)
    redacted = redact_config(migrated)
    catalog = build_catalog(config=legacy, version=4)
    payload = json.dumps(redacted, sort_keys=True)

    validate_v2_config(migrated)
    assert migrated["providers"]["deepseek"]["type"] == "anthropic_compatible"
    assert migrated["agents"]["lead"]["primary"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
    }
    assert catalog["version"] == 4
    assert catalog["default_model"] == {"provider": "deepseek", "model": "deepseek-v4-pro"}
    assert is_allowed_preferred_model(catalog, "deepseek", "deepseek-v4-pro")
    assert "sk-deepseek-legacy" not in payload
    assert "api_key" not in payload


def test_catalog_never_falls_back_to_deployment_routing_config() -> None:
    deployment = LoadedRoutingConfig(
        providers=[
            ProviderConfig(
                name="deployment_provider",
                type="openai_compatible",
                base_url="https://deployment.example/v1",
                supports={"tools": True},
            )
        ],
        agent_bindings=[
            AgentBinding(
                agent_type="lead",
                primary=("deployment_provider", "deployment_model"),
            )
        ],
    )

    catalog = build_catalog(
        config=None,
        version=17,
        loaded_config=deployment,
    )

    assert catalog == {
        "version": 17,
        "providers": [],
        "default_model": None,
        "kb": {},
    }


def test_invalid_team_catalog_never_falls_back_to_deployment_routing_config() -> None:
    deployment = LoadedRoutingConfig(
        providers=[ProviderConfig(name="deployment_provider", type="mock")],
        agent_bindings=[
            AgentBinding(
                agent_type="lead",
                primary=("deployment_provider", "deployment_model"),
            )
        ],
    )

    catalog = build_catalog(
        config={"schema_version": 2, "providers": {}},
        version=23,
        loaded_config=deployment,
    )

    assert catalog == {
        "version": 23,
        "providers": [],
        "default_model": None,
        "kb": {},
    }


def test_legacy_team_catalog_never_inherits_deployment_provider_template() -> None:
    deployment = LoadedRoutingConfig(
        providers=[
            ProviderConfig(
                name="deployment_provider",
                type="openai_compatible",
                base_url="https://deployment.example/v1",
            )
        ],
        agent_bindings=[
            AgentBinding(
                agent_type="lead",
                primary=("deployment_provider", "deployment_model"),
            )
        ],
    )

    catalog = build_catalog(
        config={
            "providers": [
                {
                    "provider_name": "deployment_provider",
                    "api_key": "team-owned-secret",
                    "role": "lead",
                }
            ]
        },
        version=29,
        loaded_config=deployment,
    )

    assert catalog == {
        "version": 29,
        "providers": [],
        "default_model": None,
        "kb": {},
    }


def test_legacy_migration_never_inherits_deployment_provider_template() -> None:
    deployment = LoadedRoutingConfig(
        providers=[
            ProviderConfig(
                name="deployment_provider",
                type="openai_compatible",
                base_url="https://deployment.example/v1",
            )
        ],
        agent_bindings=[
            AgentBinding(
                agent_type="lead",
                primary=("deployment_provider", "deployment-model"),
            )
        ],
    )

    migrated = migrate_legacy_config_to_v2(
        {
            "providers": [
                {
                    "provider_name": "deployment_provider",
                    "api_key": "team-owned-secret",
                    "role": "lead",
                }
            ]
        },
        loaded_config=deployment,
    )

    provider = migrated["providers"]["deployment_provider"]
    assert provider["type"] == "openai_compatible"
    assert provider["models"] == []
    assert "base_url" not in provider


@pytest.mark.asyncio
async def test_team_kb_runtime_uses_team_router_for_ingest_llm(tmp_path) -> None:
    team_router = object()

    class _TeamRouterRegistry:
        async def get(self, team_id: str, llm_config: dict, version: int) -> object:
            assert team_id == "team_1"
            assert version == 3
            assert llm_config["schema_version"] == 2
            return team_router

    registry = TeamKbRuntimeRegistry(
        persist_dir=str(tmp_path / "chroma"),
        files_store=object(),
        artifact_store=object(),
        router=object(),
        team_router_registry=_TeamRouterRegistry(),
        doc_type_store=object(),
        extracted_record_store=object(),
        extracted_rows_store=object(),
        per_row_jobs_store=object(),
        row_extractor=None,
        active_per_row_tasks_ref={},
    )

    runtime = await registry.get("team_1", _config(), 3)

    assert runtime is not None
    assert runtime.worker is not None
    assert runtime.worker._structured_extractor._router is team_router
    assert runtime.worker._doc_type_classifier._router is team_router


# ── SECURITY: team api_key_ref = arbitrary env exfil (Fernet key etc.) ──────────


def test_team_config_rejects_api_key_ref():
    """team provider carrying api_key_ref (env-exfil vector) must be rejected."""
    cfg = _config()
    cfg["providers"]["openai_org"]["api_key_ref"] = "env://BOWHEAD_FERNET_KEY"
    with pytest.raises(TeamLlmConfigError, match="api_key_ref is not allowed"):
        validate_v2_config(cfg)
    # build path validates internally → also refuses
    with pytest.raises(TeamLlmConfigError):
        provider_configs_from_v2(cfg)


def test_plaintext_team_config_still_valid():
    """Regression: normal team config (plaintext api_key, no ref) still works, ref never populated."""
    cfg = _config()
    validate_v2_config(cfg)
    pcs = provider_configs_from_v2(cfg)
    assert all(pc.api_key_ref is None for pc in pcs)
    assert any(pc.api_key for pc in pcs)


def test_migrate_drops_api_key_ref():
    """Legacy migration must not carry team-supplied api_key_ref into v2 config."""
    legacy = {
        "providers": [
            {
                "name": "p1",
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key_ref": "env://BOWHEAD_FERNET_KEY",
                "models": ["m1"],
                "role": "lead",
            }
        ],
        "agent_routing": {"lead": "p1:m1"},
    }
    v2 = migrate_legacy_config_to_v2(legacy)
    for spec in (v2.get("providers") or {}).values():
        assert "api_key_ref" not in spec


def test_resolve_secret_refuses_sensitive_env(monkeypatch):
    """Defense-in-depth: _resolve_secret refuses crown-jewel env vars, resolves normal LLM keys."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "super-secret-master")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-legit-llm-key")
    fernet_cfg = ProviderConfig(
        name="x",
        type="openai_compatible",
        base_url="https://h/v1",
        api_key_ref="env://BOWHEAD_FERNET_KEY",
    )
    assert ProviderRegistry._resolve_secret(fernet_cfg) is None
    legit_cfg = ProviderConfig(
        name="y",
        type="openai_compatible",
        base_url="https://h/v1",
        api_key_ref="env://DEEPSEEK_API_KEY",
    )
    assert ProviderRegistry._resolve_secret(legit_cfg) == "sk-legit-llm-key"


# ── WS1: per-model capability/cost catalog (additive model_details) ────────────


def test_catalog_emits_model_details_backcompat_models():
    """Catalog gains additive model_details; models[] stays chat-capable ids (back-compat)."""
    cat = build_catalog(config=_config(), version=1)
    prov = {p["id"]: p for p in cat["providers"]}
    oi = prov["openai_org"]
    assert oi["model_details"], "model_details must be present"
    assert set(oi["models"]) >= {"gpt-4.1", "gpt-4o-mini"}  # legacy list[str] → all chat
    details = {d["id"]: d for d in oi["model_details"]}
    assert "chat" in details["gpt-4.1"]["capabilities"]
    assert "vision" in details["gpt-4.1"]["capabilities"]  # from provider supports.vision
    assert details["gpt-4.1"]["verified"] is False  # provider-level inference is unverified


def test_catalog_obj_shape_embed_only_excluded_from_chat_models():
    """Obj-shape models validate; embed-only model surfaces in details but not chat models[]."""
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [
        {"id": "gpt-4o", "capabilities": ["chat", "tool", "vision"], "cost": "premium", "source": "manual", "verified": True},
        {"id": "text-embedding-3", "capabilities": ["embed"], "cost": "cheap", "source": "manual"},
    ]
    validate_v2_config(cfg)  # obj-shape accepted, no raise
    cat = build_catalog(config=cfg, version=1)
    oi = next(p for p in cat["providers"] if p["id"] == "openai_org")
    details = {d["id"]: d for d in oi["model_details"]}
    assert details["text-embedding-3"]["capabilities"] == ["embed"]
    assert details["gpt-4o"]["verified"] is True
    assert "text-embedding-3" not in oi["models"]  # embed-only never a chat pick (§9.5)
    assert "gpt-4o" in oi["models"]


def test_catalog_rich_model_without_capabilities_does_not_inherit_provider() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [{"id": "manual-model"}]
    cfg["agents"]["lead"]["primary"]["model"] = "manual-model"

    catalog = build_catalog(config=cfg, version=1)

    provider = next(p for p in catalog["providers"] if p["id"] == "openai_org")
    detail = next(d for d in provider["model_details"] if d["id"] == "manual-model")
    assert detail["capabilities"] == []
    assert "manual-model" not in provider["models"]


def test_validate_rejects_kb_model_without_real_id() -> None:
    cfg = _config()
    del cfg["kb"]["models"]["embed_small"]["real_id"]

    with pytest.raises(TeamLlmConfigError, match="real_id"):
        validate_v2_config(cfg)


def test_validate_rejects_unsupported_kb_protocol() -> None:
    cfg = _config()
    cfg["kb"]["models"]["embed_small"]["protocol"] = "custom"

    with pytest.raises(TeamLlmConfigError, match="protocol"):
        validate_v2_config(cfg)


def test_validate_rejects_kb_active_capability_mismatch() -> None:
    cfg = _config()
    cfg["kb"]["active"]["embed"] = "vision_fast"

    with pytest.raises(TeamLlmConfigError, match="capability"):
        validate_v2_config(cfg)


def test_validate_rejects_incomplete_pending_embedding() -> None:
    cfg = _config()
    cfg["kb"]["pending_embed"] = {
        "model": "embed_small",
        "target_index_version": "kbidx_next",
    }

    with pytest.raises(TeamLlmConfigError, match="task_id"):
        validate_v2_config(cfg)


def test_validate_rejects_obj_model_without_id():
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [{"capabilities": ["chat"]}]
    with pytest.raises(TeamLlmConfigError, match="id is required"):
        validate_v2_config(cfg)


def test_validate_rejects_obj_model_bad_capability():
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [{"id": "m", "capabilities": ["chat", "nope"]}]
    with pytest.raises(TeamLlmConfigError, match="capabilities invalid"):
        validate_v2_config(cfg)


def test_validate_rejects_duplicate_provider_model_ids() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [
        {"id": "gpt-4.1", "capabilities": ["chat"]},
        {"id": " gpt-4.1 ", "capabilities": ["embed"]},
    ]

    with pytest.raises(TeamLlmConfigError, match="duplicate model id: gpt-4.1"):
        validate_v2_config(cfg)


def test_validate_accepts_rerank_provider_model_protocol() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"]["models"].append(
        {
            "id": "gte-rerank-v2",
            "capabilities": ["rerank"],
            "protocol": "dashscope_native",
        }
    )

    validate_v2_config(cfg)


def test_validate_rejects_invalid_provider_model_protocol() -> None:
    cfg = _config()
    cfg["providers"]["openai_org"]["models"].append(
        {
            "id": "gte-rerank-v2",
            "capabilities": ["rerank"],
            "protocol": "custom",
        }
    )

    with pytest.raises(TeamLlmConfigError, match="protocol invalid"):
        validate_v2_config(cfg)


@pytest.mark.parametrize(
    "provider",
    [
        {
            "type": "azure_openai",
            "endpoint": "https://azure.example.com",
            "api_version": "2024-10-21",
            "api_key": "stored-secret",
        },
        {
            "type": "custom_http",
            "base_url": "https://gateway.example.com",
            "auth": {"type": "none"},
            "transports": {
                "embed": {
                    "contract": "openai_embeddings",
                    "request": {
                        "method": "POST",
                        "url_template": "{base_url}/embeddings",
                        "body_template": {
                            "input": {"$bowhead": "input"},
                        },
                    },
                },
            },
            "models": [
                {
                    "id": "text-embedding-3-small",
                    "capabilities": ["embed"],
                    "capability_transports": {"embed": "embed"},
                },
            ],
        },
    ],
)
def test_kb_diagnostics_accepts_azure_endpoint_and_keyless_custom_http(
    provider: dict,
) -> None:
    config = {
        "providers": {"enterprise": provider},
        "kb": {
            "models": {
                "enterprise_embed": {
                    "provider": "enterprise",
                    "real_id": "text-embedding-3-small",
                    "capability": "embed",
                    "dim": 1536,
                }
            },
            "active": {"embed": "enterprise_embed"},
        },
    }

    diagnostics = diagnose_kb_runtime_config(config)

    assert diagnostics.capabilities["embed"].valid is True
    assert diagnostics.has_active_embedding is True


def test_kb_diagnostics_rejects_custom_http_without_embed_transport() -> None:
    config = {
        "providers": {
            "enterprise": {
                "type": "custom_http",
                "base_url": "https://gateway.example.com",
                "auth": {"type": "none"},
            },
        },
        "kb": {
            "models": {
                "enterprise_embed": {
                    "provider": "enterprise",
                    "real_id": "text-embedding-3-small",
                    "capability": "embed",
                    "dim": 1536,
                },
            },
            "active": {"embed": "enterprise_embed"},
        },
    }

    diagnostics = diagnose_kb_runtime_config(config)

    assert diagnostics.capabilities["embed"].valid is False
    assert diagnostics.has_active_embedding is False


def test_model_api_surface_valid_value_passes_and_reaches_runtime() -> None:
    """Responses 批 0：legacy PUT 路对未知模型键透传（review 实测），api_surface 的值
    校验必须在这里做；合法值要能一路活到 ProviderConfig.models（runtime 面判定的来源）。"""
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [
        {"id": "gpt-51", "capabilities": ["chat"], "api_surface": "responses"},
    ]
    for agent_type, spec in cfg["agents"].items():
        targets = [spec["default"], *spec.get("profiles", {}).values()] if agent_type == "analyst" else [spec]
        for target in targets:
            target["primary"] = {"provider": "openai_org", "model": "gpt-51"}
            target["fallback"] = []
    cfg["kb"]["active"] = {}
    cfg["kb"]["models"] = {}

    validate_v2_config(cfg)
    providers = provider_configs_from_v2(cfg)
    provider = next(p for p in providers if p.name == "openai_org")
    assert provider.models[0]["api_surface"] == "responses"


def test_model_api_surface_rejected_for_non_openai_compatible_provider() -> None:
    """review R1-P2-8：azure 等类型收下该键会「声明了 responses、运行静默走 chat」。"""
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [
        {"id": "gpt-51", "capabilities": ["chat"], "api_surface": "responses"},
    ]
    cfg["providers"]["openai_org"]["type"] = "azure_openai"
    cfg["providers"]["openai_org"]["endpoint"] = "https://93.184.216.34"
    cfg["providers"]["openai_org"]["api_version"] = "2024-10-21"

    with pytest.raises(TeamLlmConfigError, match="only supported for openai_compatible"):
        validate_v2_config(cfg)


def test_model_api_surface_invalid_value_rejected() -> None:
    """拼写错静默跑 chat 面是雷（tech-design §8.1 / C-4）——非法值必须拒绝保存。"""
    cfg = _config()
    cfg["providers"]["openai_org"]["models"] = [
        {"id": "gpt-51", "capabilities": ["chat"], "api_surface": "response"},
    ]

    with pytest.raises(TeamLlmConfigError, match="api_surface invalid"):
        validate_v2_config(cfg)
