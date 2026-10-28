"""M3.2 · OpenAI-compat 多家 provider 兼容矩阵 contract 测试.

设计文档: docs/M157-routing-user-pref-design.md §4.5
docs/OPENAI_COMPAT_PROVIDERS.md (列已验证 base_url + 已知 quirk)

只验证 adapter 能针对各家 base_url 正确构造 request, 不打真 API. 真 e2e 跑 vs 各家
需要相应 API key, 跟 test_openai_compat.test_live_qwen_* 同 pattern (env-gated 跳过).

覆盖 base_url:
  - Aliyun Qwen
  - Doubao 火山方舟
  - OpenAI 原生
  - Ollama 本地 (auth=none)
  - LM Studio 本地 (auth=none)
  - Custom OpenAI-compat (vLLM 自部署)
"""

from __future__ import annotations

import pytest
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.types import LLMRequest, Message

# (provider_id, base_url, has_api_key, expect_auth_header)
PROVIDER_MATRIX = [
    ("aliyun_qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", True, True),
    ("doubao_ark", "https://ark.cn-beijing.volces.com/api/v3", True, True),
    ("openai", "https://api.openai.com/v1", True, True),
    ("ollama_local", "http://localhost:11434/v1", False, False),
    ("lmstudio_local", "http://localhost:1234/v1", False, False),
    ("custom_vllm", "http://internal-vllm.company.local:8000/v1", True, True),
]


@pytest.mark.parametrize("provider_id,base_url,has_key,expect_auth", PROVIDER_MATRIX)
def test_adapter_constructs_with_provider_url(
    provider_id: str,
    base_url: str,
    has_key: bool,
    expect_auth: bool,
):
    """adapter 能构造指向各 provider base_url, 不抛."""
    api_key = "sk-test" if has_key else ""
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key=api_key,
        provider_name=provider_id,
    )
    assert adapter.base_url == base_url.rstrip("/")
    if expect_auth:
        assert adapter._http.headers.get("Authorization") == f"Bearer {api_key}"
    # ollama / lmstudio: with an empty api_key, Authorization is either absent or "Bearer ".
    # (The exact behavior is not required; the key requirement is to avoid crashing.)


@pytest.mark.parametrize("provider_id,base_url,has_key,_expect_auth", PROVIDER_MATRIX)
def test_build_body_uniform_across_providers(
    provider_id: str,
    base_url: str,
    has_key: bool,
    _expect_auth: bool,
):
    """所有 OpenAI-compat provider 的 request body 应统一; 不应因 base_url 不同而变."""
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="sk-test" if has_key else "",
        provider_name=provider_id,
    )
    req = LLMRequest(
        model="test-model",
        messages=[Message(role="user", content="hello")],
    )
    body = adapter._build_body(req, stream=False)
    # Standard OpenAI Chat Completions schema.
    assert body["model"] == "test-model"
    assert isinstance(body["messages"], list)
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "hello"


def test_local_provider_no_auth_does_not_break_adapter():
    """本地 Ollama/LM Studio 没 API key, 也能构造 + 发请求 (HTTPClient 不会因为 Bearer
    空字符串崩)."""
    adapter = OpenAICompatibleAdapter(
        base_url="http://localhost:11434/v1",
        api_key="",  # ollama has no authentication by default.
        provider_name="ollama_local",
    )
    req = LLMRequest(
        model="llama3:70b",
        messages=[Message(role="user", content="x")],
    )
    body = adapter._build_body(req, stream=False)
    assert body["model"] == "llama3:70b"


def test_doubao_endpoint_id_routes_via_model_field():
    """火山方舟用 endpoint_id (ep-xxxx) 作 model name, adapter 不做特殊处理 (透传).
    用户在 ProviderConfig.extra.endpoint_id 填; turn-start 时把它当 model 用.
    """
    adapter = OpenAICompatibleAdapter(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="sk-test",
        provider_name="doubao_ark",
    )
    req = LLMRequest(
        model="ep-20260101-xxx",  # Must be an endpoint_id, not a model name.
        messages=[Message(role="user", content="x")],
    )
    body = adapter._build_body(req, stream=False)
    # Pass through unchanged; the adapter needs no doubao-specific handling.
    assert body["model"] == "ep-20260101-xxx"
