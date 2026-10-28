"""Media 模型注册表 —— 团队 ``media:`` 块 → MediaModelRegistry.

媒体模型是可选能力。配置异常或缺少 active 模型时返回空 registry，由调用层
明确显示“未配置”；不读取 YAML、环境变量或部署级密钥。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Capability vocabulary: v1 only supports image_gen; video / tts are reserved extensions.
MediaCapability = Literal["image_gen"]
MEDIA_CAPABILITIES: tuple[MediaCapability, ...] = ("image_gen",)

# Protocol vocabulary: determines ImageGenClient request/polling shapes.
MediaProtocol = Literal["dashscope_async", "ark_sync"]
MEDIA_PROTOCOLS: tuple[MediaProtocol, ...] = ("dashscope_async", "ark_sync")


class MediaModelConfigError(ValueError):
    """media 配置硬错误（仅在显式 get_by_name 等强查询路径抛出）."""


@dataclass(frozen=True)
class MediaModelSpec:
    """Resolved media model —— ImageGenClient 发 HTTP 调用所需的全部信息。

    ``api_key`` 可能为空字符串（env 未设）= soft-disabled：注册层据此决定
    不注册工具，loader 不视为错误。
    """

    name: str  # catalog key (e.g. "qwen-image-plus")
    capability: MediaCapability
    provider: str  # Provider name in the providers: block.
    real_id: str  # API model field value.
    base_url: str
    api_key: str
    timeout_sec: int
    protocol: MediaProtocol
    ssrf_guard: bool = False  # Team BYO endpoints require True (always False in v1).


@dataclass(frozen=True)
class MediaModelRegistry:
    """团队 ``media:`` 块的全部模型 + active 绑定。"""

    by_name: dict[str, MediaModelSpec]
    active: dict[MediaCapability, str]

    def get_active(self, capability: MediaCapability) -> MediaModelSpec | None:
        """active 模型 spec；未配置 / 指向不存在的模型 → None（不 raise）。

        未配置 / 指向不存在的模型时返回 None。
        """
        name = self.active.get(capability, "")
        if not name:
            return None
        spec = self.by_name.get(name)
        if spec is None:
            logger.warning(
                "media: active %s=%r not in catalog (known: %s) — treating as unconfigured",
                capability,
                name,
                sorted(self.by_name),
            )
            return None
        if spec.capability != capability:
            logger.warning(
                "media: model %r capability=%r can't bind to %r — treating as unconfigured",
                name,
                spec.capability,
                capability,
            )
            return None
        return spec

    def get_by_name(self, name: str) -> MediaModelSpec:
        spec = self.by_name.get(name)
        if spec is None:
            raise MediaModelConfigError(f"media model {name!r} not in catalog. Known: {sorted(self.by_name)}")
        return spec


def empty_media_registry() -> MediaModelRegistry:
    return MediaModelRegistry(by_name={}, active={})


def build_capability_registry_section(
    config: dict,
    *,
    section: str,
    capabilities: tuple[str, ...],
    protocols: tuple[str, ...],
    spec_factory: Callable[..., Any],
    ssrf_guard: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """从 team v2 config 的某个能力段（``media:`` / ``search:``）建 ``(by_name, active)``。

    media 与 search 的模型段是同一形状（``{section}.models{name → {provider, real_id,
    capability, protocol}}`` + ``{section}.active{cap → name}``，provider 明文 api_key），
    此前 media 单独写了一份、kb 也有一份——search 恢复时抽这份共享循环，别写第 4 份
    （见 chat-pipeline 之外的通用 seam 约定）。boot/调用安全：malformed 条目 skip
    （写路径严格校验各自的 ``team_llm_config._validate_*``）。``spec_factory`` 用 kwargs
    构造对应的 ``*ModelSpec``，调用方决定具体类型。
    """
    section_cfg = config.get(section) or {}
    providers = config.get("providers") or {}
    models_section = section_cfg.get("models") if isinstance(section_cfg, dict) else None
    by_name: dict[str, Any] = {}
    if isinstance(models_section, dict):
        for name, spec in models_section.items():
            if not isinstance(spec, dict):
                continue
            prov_name = spec.get("provider")
            prov = providers.get(prov_name) if isinstance(prov_name, str) else None
            if not isinstance(prov, dict):
                continue
            real_id = spec.get("real_id") or spec.get("model")
            capability = spec.get("capability")
            protocol = spec.get("protocol")
            base_url = str(prov.get("base_url") or "").rstrip("/")
            if not (isinstance(real_id, str) and real_id and capability in capabilities and protocol in protocols and base_url):
                continue
            by_name[name] = spec_factory(
                name=name,
                capability=capability,
                provider=prov_name,
                real_id=real_id,
                base_url=base_url,
                api_key=str(prov.get("api_key") or ""),
                timeout_sec=int(prov.get("timeout_sec", 120) or 120),
                protocol=protocol,
                ssrf_guard=ssrf_guard,
            )
    active: dict[str, str] = {}
    active_section = section_cfg.get("active") if isinstance(section_cfg, dict) else None
    if isinstance(active_section, dict):
        for cap in capabilities:
            val = active_section.get(cap)
            if isinstance(val, str) and val:
                active[cap] = val
    return by_name, active


def build_team_media_registry(config: dict) -> MediaModelRegistry:
    """从 team v2 config 的 ``media:`` 段建 registry（模型档位化 P2）。

    与 ``load_media_registry``（读 yaml）区别：
    - 直接吃 team config dict（provider 是明文 ``api_key`` 的 dict，非 ProviderConfig）；
    - ``ssrf_guard=True``（team BYO endpoint 必须过 SSRF guard，MediaModelSpec 早预留此位）。
    """
    by_name, active = build_capability_registry_section(
        config,
        section="media",
        capabilities=MEDIA_CAPABILITIES,
        protocols=MEDIA_PROTOCOLS,
        spec_factory=MediaModelSpec,
        ssrf_guard=True,
    )
    return MediaModelRegistry(by_name=by_name, active=active)
