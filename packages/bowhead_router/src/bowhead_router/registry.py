"""Provider 注册表.

把 YAML 配置（TD §4.3.3）→ Adapter 实例池。
M1 Day 2：接入 `OpenAICompatibleAdapter`（真实 HTTP）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bowhead_router.adapter import MockAdapter, RefusalAdapter
from bowhead_router.adapters import AnthropicCompatibleAdapter, OpenAICompatibleAdapter

if TYPE_CHECKING:
    from bowhead_router.types import LLMAdapter


@dataclass
class ProviderConfig:
    """单个 provider 配置（来自 providers.yaml 或代码直写）.

    Attributes:
        name: 运行时别名（用在 Agent binding 里引用）
        type: adapter 类型 dispatch key
              `mock` | `openai_compatible` | `anthropic_compatible` | `custom_http` | ...
        base_url: API 基地址（原生 adapter 如 bedrock/vertex 可忽略）
        api_key_ref: secret manager 引用，如 `env://ALIYUN_DASHSCOPE_API_KEY`
        api_key: 直接传入的 key（测试用，生产不建议）
        default_headers: 附加 HTTP header
        timeout_sec: per-request transport timeout; retries come from the model configuration's `retry` setting
        supports: 能力声明
        extra: 各 type 专属字段（例如 custom_http 的 template）
    """

    name: str
    type: str
    base_url: str | None = None
    api_key_ref: str | None = None
    api_key: str | None = None
    default_headers: dict[str, str] = field(default_factory=dict)
    timeout_sec: int = 60
    supports: dict[str, bool] = field(default_factory=dict)
    models: list[Any] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderRegistry:
    """运行时 provider 实例池."""

    def __init__(self) -> None:
        self._adapters: dict[str, LLMAdapter] = {}
        self._configs: dict[str, ProviderConfig] = {}

    def load(self, configs: list[ProviderConfig]) -> None:
        for cfg in configs:
            self._adapters[cfg.name] = self._build_adapter(cfg)
            self._configs[cfg.name] = cfg

    def get(self, name: str) -> LLMAdapter:
        if name not in self._adapters:
            raise KeyError(f"Provider '{name}' not registered")
        return self._adapters[name]

    def list_names(self) -> list[str]:
        return list(self._adapters.keys())

    def definition_for(self, name: str) -> dict[str, Any]:
        """Return the configuration used to construct this exact adapter."""
        config = self._configs[name]
        return {**config.extra, "models": config.models, "type": config.type}

    def has(self, name: str) -> bool:
        return name in self._adapters

    async def reload(self, configs: list[ProviderConfig]) -> None:
        new_adapters: dict[str, LLMAdapter] = {}
        for cfg in configs:
            new_adapters[cfg.name] = self._build_adapter(cfg)
        to_close = set(self._adapters) - set(new_adapters)
        old_adapters = self._adapters
        self._adapters = new_adapters
        self._configs = {cfg.name: cfg for cfg in configs}
        for name in to_close:
            await old_adapters[name].close()

    async def close_all(self) -> None:
        for adapter in self._adapters.values():
            await adapter.close()
        self._adapters.clear()
        self._configs.clear()

    # ─────────────────────────────────────────────────────────
    # Adapter factory
    # ─────────────────────────────────────────────────────────
    def _build_adapter(self, cfg: ProviderConfig) -> LLMAdapter:
        if cfg.type == "mock":
            return MockAdapter(provider_name=cfg.name)

        if cfg.type == "refusal":
            # credential-readiness D1: the assembly Router in non-dev environments refuses every invocation.
            return RefusalAdapter(provider_name=cfg.name)

        if cfg.type == "openai_compatible":
            api_key = self._resolve_secret(cfg)
            if not cfg.base_url:
                raise ValueError(f"provider {cfg.name!r}: openai_compatible requires base_url")
            return OpenAICompatibleAdapter(
                base_url=cfg.base_url,
                api_key=api_key or "",
                provider_name=cfg.name,
                timeout_sec=cfg.timeout_sec,
                default_headers=cfg.default_headers,
                ssrf_guard=bool((cfg.extra or {}).get("ssrf_guard")),
                sanitize_tool_dots=bool((cfg.extra or {}).get("sanitize_tool_dots", True)),
                # Per-model request_body_overrides require model declarations (previously passed only to azure/custom_http).
                provider_options={**cfg.extra, "models": cfg.models},
            )

        if cfg.type in {"azure_openai", "custom_http"}:
            api_key = self._resolve_secret(cfg)
            base_url = cfg.base_url or str((cfg.extra or {}).get("endpoint") or "")
            if cfg.type == "azure_openai" and not base_url:
                raise ValueError(f"provider {cfg.name!r}: {cfg.type} requires endpoint/base_url")
            return OpenAICompatibleAdapter(
                base_url=base_url,
                api_key=api_key or "",
                provider_name=cfg.name,
                timeout_sec=cfg.timeout_sec,
                default_headers=cfg.default_headers,
                ssrf_guard=bool((cfg.extra or {}).get("ssrf_guard")),
                sanitize_tool_dots=bool((cfg.extra or {}).get("sanitize_tool_dots", True)),
                adapter_type=cfg.type,
                provider_options={
                    **(cfg.extra or {}),
                    "models": cfg.models,
                },
            )

        if cfg.type == "anthropic_compatible":
            api_key = self._resolve_secret(cfg)
            if not cfg.base_url:
                raise ValueError(f"provider {cfg.name!r}: anthropic_compatible requires base_url")
            # extra supports a custom messages_path (e.g. DeepSeek uses /messages rather than /v1/messages).
            extra = cfg.extra or {}
            kwargs: dict[str, Any] = {
                "base_url": cfg.base_url,
                "api_key": api_key or "",
                "provider_name": cfg.name,
                "timeout_sec": cfg.timeout_sec,
                "default_headers": cfg.default_headers,
                "ssrf_guard": bool((cfg.extra or {}).get("ssrf_guard")),
            }
            if "messages_path" in extra:
                kwargs["messages_path"] = extra["messages_path"]
            if "anthropic_version" in extra:
                kwargs["anthropic_version"] = extra["anthropic_version"]
            # Tool names are made provider-safe by default (bowhead_router.tool_names);
            # extra.sanitize_tool_dots=false is the explicit opt-out.
            if "sanitize_tool_dots" in extra:
                kwargs["sanitize_tool_dots"] = bool(extra["sanitize_tool_dots"])
            # Pass model declarations through: outbound parameters are per-model (one gateway can host models that accept
            # a parameter and models that reject it). Previously only the OpenAI family received provider_options, leaving
            # the Anthropic surface without even a configuration escape hatch.
            kwargs["provider_options"] = {**extra, "models": cfg.models}
            return AnthropicCompatibleAdapter(**kwargs)

        # After M1: custom_http / bedrock / vertex / ollama ...
        raise ValueError(f"provider {cfg.name!r}: adapter type {cfg.type!r} not yet implemented")

    # ─────────────────────────────────────────────────────────
    # Secret resolution
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_secret(cfg: ProviderConfig) -> str | None:
        """按 `secret://` 引用 resolve。支持 env backend（MVP）."""
        if cfg.api_key:
            return cfg.api_key
        auth = (cfg.extra or {}).get("auth")
        if isinstance(auth, dict) and isinstance(auth.get("secret"), str):
            return auth["secret"]
        ref = cfg.api_key_ref
        if not ref:
            return None
        if ref.startswith("env://"):
            env_name = ref[len("env://") :]
            # Support the default-value syntax env://VAR:default.
            var = env_name.split(":", 1)[0] if ":" in env_name else env_name
            # SECURITY (defense-in-depth): refuse crown-jewel env vars regardless
            # of source. The team write path already strips api_key_ref
            # (team_llm_config), so this normally only guards trusted platform
            # refs, whose names (*_API_KEY / *_AUTH_TOKEN) never match these.
            if any(marker in var.upper() for marker in ("FERNET", "SECRET", "PASSWORD", "PRIVATE_KEY", "SIGNING")):
                return None
            if ":" in env_name:
                _, default = env_name.split(":", 1)
                return os.environ.get(var, default)
            return os.environ.get(var)
        # Other backends (vault:// / k8s:// / aws-sm:// / gcp-sm://) will be integrated in M2.
        raise ValueError(f"unsupported secret backend in ref {ref!r}; supported: env:// (M1); future: vault:// / k8s:// / aws-sm:// / gcp-sm://")
