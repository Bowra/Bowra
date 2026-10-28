"""KB model registry types — ModelSpec / KbModelRegistry / capability vocabulary.

Sprint kb-model-config-2026-05-26 introduced these to replace hardcoded model
constants in embedder.py / reranker.py / vision_llm.py. The registry is built
from the TEAM llm config（`team_kb_runtime._build_registry`，团队连接注册表是
唯一来源）；原 yaml 文件加载路径 `load_kb_registry` 已在 credential-readiness
R10 删除（部署级 routing yaml 不再喂 KB 模型，见 docs/design/概念术语.md 的
连接解析边界）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

# Capability vocabulary — used by env override + active block.
# Sprint 25 added "row_extractor" for per-row PDF → structured rows via LLM.
Capability = Literal["vision", "embed", "rerank", "row_extractor"]
CAPABILITIES: tuple[Capability, ...] = ("vision", "embed", "rerank", "row_extractor")
PROTOCOLS: tuple[str, ...] = ("openai_compat", "dashscope_native")


class KbModelConfigError(ValueError):
    """yaml `kb:` section is malformed or references missing provider."""


@dataclass(frozen=True)
class ModelSpec:
    """Resolved KB model — everything callers (Embedder / Reranker / VisionLLMClient)
    need to make an HTTP call. ``base_url`` / ``api_key`` come from the
    provider block (resolved from env://). ``real_id`` is what we send in
    the API ``model`` field (may include vendor prefix e.g. ``qwen/...``).
    """

    name: str  # catalog key (e.g. "qwen-text-embedding-v4")
    capability: Capability
    provider: str  # provider name (matches providers: block)
    real_id: str  # API-facing model id (e.g. "text-embedding-v4")
    base_url: str
    api_key: str
    timeout_sec: int
    dim: int | None = None  # embed only — chromadb vector dimension
    ssrf_guard: bool = False  # team BYO endpoints: validate+pin each outbound request
    # Request protocol (2026-06-05 de-edu): openai_compat = OpenAI-style {base}/rerank etc.;
    # dashscope_native = native DashScope paths + input/parameters wrapper (official rerank only supports this).
    protocol: str = "openai_compat"
    provider_type: str = "openai_compatible"
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KbModelRegistry:
    """All KB models defined in yaml, plus active bindings (with env override)."""

    by_name: dict[str, ModelSpec]
    active: dict[Capability, str]  # capability → model name

    def get_active(self, capability: Capability) -> ModelSpec:
        name = self._active_name(capability)
        spec = self.by_name.get(name)
        if spec is None:
            raise KbModelConfigError(f"active kb.{capability}={name!r} but no such model in catalog. Known: {sorted(self.by_name.keys())}")
        if spec.capability != capability:
            raise KbModelConfigError(f"model {name!r} has capability={spec.capability!r}, can't bind to {capability!r}")
        return spec

    def get_by_name(self, name: str) -> ModelSpec:
        spec = self.by_name.get(name)
        if spec is None:
            raise KbModelConfigError(f"kb model {name!r} not in catalog. Known: {sorted(self.by_name.keys())}")
        return spec

    def _active_name(self, capability: Capability) -> str:
        # Env override wins so an operator can A/B without editing yaml.
        env_var = f"BOWHEAD_KB_{capability.upper()}"
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            return env_val
        if capability not in self.active:
            raise KbModelConfigError(f"kb.active.{capability} missing and {env_var} not set")
        return self.active[capability]
