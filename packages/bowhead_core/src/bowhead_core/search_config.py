"""Search 模型注册表 —— 团队 ``search:`` 块 → SearchModelRegistry.

web_search 是可选能力：由团队在「模型」设置里绑定一个 provider + 模型才可用（整体
镜像 ``media/config_loader`` 的 image_gen 团队化）。配置异常或缺 active 模型时返回空
registry，注册层据此不注册工具，Lead/peer 看不到 web_search——不读取 YAML、环境变量
或部署级密钥（禁部署级模型密钥入口，388acfb5）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from bowhead_core.media.config_loader import build_capability_registry_section

logger = logging.getLogger(__name__)


# Capability vocabulary: v1 has only web_search (reserving extensions such as news / academic).
SearchCapability = Literal["web_search"]
SEARCH_CAPABILITIES: tuple[SearchCapability, ...] = ("web_search",)

# Protocol vocabulary: determines which DashScope endpoint the web_search handler uses:
# ``dashscope_compat`` = OpenAI-compatible chat/completions (enable_search, default);
# ``dashscope_native`` = native generation endpoint (search_options.enable_source → structured
# search_results). Previously selected by ``BOWHEAD_WEB_SEARCH_NATIVE``; team-scoped configuration now uses
# the model entry protocol instead (one fewer deployment-level env variable).
SearchProtocol = Literal["dashscope_compat", "dashscope_native", "tavily"]
SEARCH_PROTOCOLS: tuple[SearchProtocol, ...] = ("dashscope_compat", "dashscope_native", "tavily")


@dataclass(frozen=True)
class SearchModelSpec:
    """Resolved search model —— web_search handler 发 HTTP 调用所需的全部信息。

    ``api_key`` 可能为空字符串 = soft-disabled：注册层据此不注册工具，loader 不视为错误。
    """

    name: str  # catalog key (e.g. "qwen-plus-search")
    capability: SearchCapability
    provider: str  # Provider name in the providers: block
    real_id: str  # API model field value
    base_url: str
    api_key: str
    timeout_sec: int
    protocol: SearchProtocol
    ssrf_guard: bool = False  # Must be True for team BYO endpoints.


@dataclass(frozen=True)
class SearchModelRegistry:
    """团队 ``search:`` 块的全部模型 + active 绑定。"""

    by_name: dict[str, SearchModelSpec]
    active: dict[SearchCapability, str]

    def get_active(self, capability: SearchCapability) -> SearchModelSpec | None:
        """active 模型 spec；未配置 / 指向不存在的模型 → None（不 raise）。"""
        name = self.active.get(capability, "")
        if not name:
            return None
        spec = self.by_name.get(name)
        if spec is None:
            logger.warning(
                "search: active %s=%r not in catalog (known: %s) — treating as unconfigured",
                capability,
                name,
                sorted(self.by_name),
            )
            return None
        if spec.capability != capability:
            logger.warning(
                "search: model %r capability=%r can't bind to %r — treating as unconfigured",
                name,
                spec.capability,
                capability,
            )
            return None
        return spec


def build_team_search_registry(config: dict) -> SearchModelRegistry:
    """从 team v2 config 的 ``search:`` 段建 registry（镜像 build_team_media_registry）。

    ``ssrf_guard=True``——team BYO endpoint 必须过 SSRF guard。boot/调用安全：malformed
    条目 skip（写路径严格校验由 ``team_llm_config._validate_search`` 负责）。
    """
    by_name, active = build_capability_registry_section(
        config,
        section="search",
        capabilities=SEARCH_CAPABILITIES,
        protocols=SEARCH_PROTOCOLS,
        spec_factory=SearchModelSpec,
        ssrf_guard=True,
    )
    return SearchModelRegistry(by_name=by_name, active=active)
