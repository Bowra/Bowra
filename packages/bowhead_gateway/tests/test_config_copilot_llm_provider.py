"""批 1-d WP-1：llm_provider 表单（tech-design-batch1d-llm-provider.md §2/§4/§5）.

最重的一条闸是 **materializer 零网络**：它在 `move_on_after` 保护圈之外且在 repair
循环内，任何网络/DNS 调用的失败形态是挂住而不是降级（破铁律 3）。设计 v1 曾要在这里
自动跑 precheck，被 review 拦下——本文件把「不许碰网络」钉成守卫。
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from bowhead_core.config_copilot import FormContext, get_form, reset_registry_for_tests
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from fastapi import FastAPI


class _ConfigStore:
    def __init__(self, providers: dict[str, Any] | None = None) -> None:
        self.providers = providers if providers is not None else {}

    async def get_team_llm_config_with_version(self, team_id: str):
        return {"providers": self.providers}, 3


@pytest.fixture
def reg_and_ctx():
    def _build(providers: dict[str, Any] | None = None):
        reset_registry_for_tests()
        app = FastAPI()
        store = _ConfigStore(providers)
        app.state.user_store = store
        register_gateway_forms(app)
        registration = get_form("llm_provider")
        assert registration is not None
        ctx = FormContext(user_id="u1", user_email="a@t.local", team_id="team_t", locale="zh-CN", current_values={})
        return registration, ctx

    yield _build
    reset_registry_for_tests()


def _draft(**overrides: Any) -> dict[str, Any]:
    body = {
        "protocol": "openai_compatible",
        "name": "dashscope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_version": "",
        "messages_path": "",
        "auth_type": "bearer",
        "auth_header_name": "",
        "auth_prefix": "",
        "models": [{"id": "qwen-plus", "caps": ["chat"], "deployment": "", "request_body_overrides": {}}],
        "api_key": "",
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_materializer_makes_no_network_call(reg_and_ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """整个 materialize 过程不得触碰 socket——设计 v1 的自动 precheck 会走阻塞 DNS。"""
    calls: list[Any] = []

    def _boom(*args: Any, **kwargs: Any):
        calls.append(args)
        raise AssertionError("materializer 不得发起网络调用")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom, raising=False)
    registration, ctx = reg_and_ctx()
    result = await registration.materializer(ctx, _draft())
    assert not result.issues
    assert not calls


@pytest.mark.asyncio
async def test_runtime_key_normalized_and_noted(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx()
    result = await registration.materializer(ctx, _draft(name="  阿里 DashScope 接入  "))
    assert result.normalized["name"] == "dashscope"  # Strip Chinese/spaces, lowercase, trim hyphens.
    assert result.notes["name"] == "已规范化为 dashscope"
    assert not [issue for issue in result.issues if issue.field == "name"]


@pytest.mark.asyncio
async def test_runtime_key_collision_is_repairable_issue(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx({"dashscope": {"type": "openai_compatible"}})
    result = await registration.materializer(ctx, _draft(name="dashscope"))
    taken = [issue for issue in result.issues if issue.code == "runtime_key_taken"]
    assert taken and taken[0].repairable  # Ask model to rename, never modify user's provider.


@pytest.mark.asyncio
async def test_base_url_tail_stripped(reg_and_ctx) -> None:
    """base_url 只到 /v1——模型很容易把 curl 里的完整路径整条抄进来。"""
    registration, ctx = reg_and_ctx()
    result = await registration.materializer(ctx, _draft(base_url="https://api.x.com/v1/chat/completions"))
    assert result.normalized["base_url"] == "https://api.x.com/v1"
    assert "base_url" in result.notes


@pytest.mark.asyncio
async def test_caps_filtered_by_protocol(reg_and_ctx) -> None:
    """词表按 protocol 分档：anthropic 兼容没有 rerank/embed（review P2-6）。"""
    registration, ctx = reg_and_ctx()
    result = await registration.materializer(
        ctx,
        _draft(
            protocol="anthropic_compatible", models=[{"id": "claude", "caps": ["chat", "rerank"], "deployment": "", "request_body_overrides": {}}]
        ),
    )
    assert [issue.code for issue in result.issues] == ["cap_unsupported"]
    assert result.normalized["models"][0]["caps"] == ["chat"]


@pytest.mark.asyncio
async def test_protocol_specific_required_fields(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx()
    azure = await registration.materializer(ctx, _draft(protocol="azure_openai", api_version=""))
    assert any(issue.code == "api_version_required" for issue in azure.issues)
    custom = await registration.materializer(ctx, _draft(protocol="custom_http", models=[]))
    assert any(issue.code == "model_required" for issue in custom.issues)


@pytest.mark.asyncio
async def test_auth_header_required_for_custom_header_auth(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx()
    result = await registration.materializer(ctx, _draft(auth_type="api_key", auth_header_name=""))
    assert any(issue.code == "auth_header_required" for issue in result.issues)


@pytest.mark.asyncio
async def test_override_keys_must_be_capabilities(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx()
    bad = [{"id": "qwen-plus", "caps": ["chat"], "deployment": "", "request_body_overrides": {"enable_thinking": {"x": 1}}}]
    result = await registration.materializer(ctx, _draft(models=bad))
    assert any(issue.code == "override_key_invalid" for issue in result.issues)
    assert result.normalized["models"][0]["request_body_overrides"] == {}


@pytest.mark.asyncio
async def test_context_lists_existing_keys_and_caps(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx({"alpha": {}, "beta": {}})
    text = await registration.context_provider(ctx)
    assert "alpha" in text and "beta" in text
    assert "anthropic_compatible" in text
    assert "image_gen" in text  # UI vocabulary, not backend's five values.


@pytest.mark.asyncio
async def test_read_accessor_never_exposes_api_key(reg_and_ctx) -> None:
    registration, ctx = reg_and_ctx(
        {"alpha": {"type": "openai_compatible", "base_url": "https://x/v1", "api_key": "sk-REAL", "models": [{"id": "m1"}]}}
    )
    data = await registration.read_accessor(ctx)
    assert "sk-REAL" not in str(data)
    assert data["providers"][0]["models"] == ["m1"]


def test_api_key_is_secret_policy(reg_and_ctx) -> None:
    from bowhead_core.config_copilot import POLICY_SECRET, generation_schema

    registration, _ = reg_and_ctx()
    assert registration.field_policy["api_key"] == POLICY_SECRET
    assert "api_key" not in generation_schema(registration)["properties"]  # Model cannot even see the field.


@pytest.mark.asyncio
async def test_azure_caps_match_host_tier(reg_and_ctx) -> None:
    """azure 的能力档要与宿主 connectCaps 一致（DECLARATIVE_HTTP_CAPS 四词）。

    实现层 review：后端给 azure 用了 7 词 UI_CAPS，模型会产出宿主渲染不出的 rerank/
    image_gen/web_search——正是设计层 P2-6 点名要防的那条。
    """
    registration, ctx = reg_and_ctx()
    for protocol in ("azure_openai", "custom_http"):
        result = await registration.materializer(
            ctx,
            _draft(
                protocol=protocol,
                api_version="2024-05-01",
                models=[{"id": "m", "caps": ["chat", "rerank", "image_gen"], "deployment": "", "request_body_overrides": {}}],
            ),
        )
        assert any(issue.code == "cap_unsupported" for issue in result.issues), protocol
        assert result.normalized["models"][0]["caps"] == ["chat"], protocol


def test_generation_schema_field_names(reg_and_ctx) -> None:
    """DTO 字段集是 FE↔BE 的契约——改名会让前端 admit 静默失效（实现层 review P1-2）。

    对侧由 models-connect.test.ts::PROVIDER_DRAFT_BACKEND_KEYS 钉住同一份名单。
    """
    from bowhead_core.config_copilot import generation_schema

    registration, _ = reg_and_ctx()
    assert sorted(generation_schema(registration)["properties"]) == [
        "api_version",
        "auth_header_name",
        "auth_prefix",
        "auth_type",
        "base_url",
        "messages_path",
        "models",
        "name",
        "protocol",
    ]
