"""Adapter 实现集合。

每个 adapter 实现 `LLMAdapter` Protocol（见 `bowhead_router.types`）。
注册在 `ProviderRegistry._build_adapter` 的 dispatch 里。

已实现：
    - :class:`OpenAICompatibleAdapter` — 覆盖 OpenAI / 阿里云百炼 / Kimi / DeepSeek / 智谱 /
      Azure OpenAI（简化模式） / vLLM gateway / 自研 OpenAI 兼容端点

M1 后续会补：
    - AnthropicCompatibleAdapter
    - AzureOpenAIAdapter（deployment 映射）
    - CustomHttpAdapter（TD §4.3.4 的 Jinja 模板 + JSONPath 解析）
    - BedrockAdapter / VertexAdapter / OllamaAdapter
"""

from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter

__all__ = ["AnthropicCompatibleAdapter", "OpenAICompatibleAdapter"]
