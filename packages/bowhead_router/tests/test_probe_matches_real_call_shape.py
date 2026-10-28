"""Keep probe request modes, model overrides, and response decoding aligned with runtime.

Streaming and non-streaming failures exercise distinct request shapes. The
probe contract version is part of config_signature and must change when its
shape changes. Execution-level assertions catch SSE decoding mismatches that
body-shape constant comparisons alone cannot detect."""

from __future__ import annotations

import bowhead_router.probe as probe_module
import httpx2
import pytest
from bowhead_router.probe import CompatibleProbeRegistry, ProbeRequestPlan

SSE_PROVIDER = {
    "type": "openai_compatible",
    "base_url": "https://api.example.com/v1",
    "api_key": "secret",
    "models": [{"id": "m1", "capabilities": ["chat", "tool", "embed"]}],
}
PROVIDER_ID = "llmp_00000000000000000000000000000000"


def _plan(capability: str, provider: dict | None = None) -> ProbeRequestPlan:
    plan = CompatibleProbeRegistry().plan(
        provider or SSE_PROVIDER,
        provider_id=PROVIDER_ID,
        provider_display_name="P",
        model_id="m1",
        capability=capability,  # type: ignore[arg-type]
    )
    assert isinstance(plan, ProbeRequestPlan), f"{capability} 应当可探测"
    return plan


def _sse(*frames: str) -> str:
    return "".join(f"data: {frame}\n\n" for frame in frames)


def _serve(monkeypatch, body: str, *, status: int = 200) -> None:
    """把探针的出站客户端换成回放固定响应的替身。"""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    def factory(**kwargs):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(probe_module, "build_guarded_async_client", factory)


def test_chat_probe_streams_like_the_real_call() -> None:
    """An SSE chat contract must verify the streaming request shape."""
    assert _plan("chat").body_shape["stream"] is True


def test_tool_probe_also_streams() -> None:
    """tool 同样要与真实调用同模式。

    ⚠ 别按「tool 本来就流式」理解：`openai_compatible` 的 adapter_contract_version 是
    `openai-compatible-v1` 而非 SSE，所以它的 tool 探针此前**也没有**流式过。我最初
    据此推断「tool 通过 ⇒ 网关支持流式」是错的，实测才纠正过来。
    """
    assert _plan("tool").body_shape["stream"] is True


def test_embed_probe_never_streams() -> None:
    """负对照：embed 没有流式形态，必须保持不发 stream。"""
    assert "stream" not in _plan("embed").body_shape


def test_chat_contract_version_moved_with_the_shape() -> None:
    """形状与契约版本必须同进同出。

    只改形状不换版本 = 历史证据谎称自己是在新形状下验的（`probe_contract_version`
    进 config_signature）。这条钉住二者绑定。
    """
    assert _plan("chat").probe_contract_version == "chat-v2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_probe_decodes_the_stream_it_asked_for(monkeypatch) -> None:
    """P0 回归钉：探针请求了流式，就必须能解流式正文。

    2026-08-17 事故：chat-v2 让 `openai_compatible` 发 `stream: true`，provider 如约回
    200 + SSE，解码却按契约版本（`openai-compatible-v1` ≠ SSE）走 `json.loads`，成功的
    探针被判 `invalid_response` —— 全部 openai 兼容网关重验即全红。

    消融：把 `_decode_success_payload` 的判据换回 `adapter_contract_version` 即红。
    """
    _serve(
        monkeypatch,
        _sse(
            '{"id":"1","model":"m1","choices":[{"index":0,"delta":{"content":"OK"}}]}',
            '{"id":"1","model":"m1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "[DONE]",
        ),
    )
    result = await CompatibleProbeRegistry().execute(_plan("chat"), timeout=5)

    assert result.terminal_state == "verified"
    assert result.code == "ok"
    assert result.actual_model_id == "m1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_probe_decodes_tool_calls_split_across_frames(monkeypatch) -> None:
    """tool 档同理：工具调用在 SSE 里是跨帧累加的，解不出来就是 `tool_call_missing`。

    分帧断在参数中间，正是真实 provider 的发法。
    """
    _serve(
        monkeypatch,
        _sse(
            '{"id":"1","model":"m1","choices":[{"index":0,"delta":{"tool_calls":'
            '[{"index":0,"id":"call_1","function":{"name":"bowhead_connection_probe",'
            '"arguments":"{\\"a\\":"}}]}}]}',
            '{"id":"1","model":"m1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}',
            '{"id":"1","model":"m1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
            "[DONE]",
        ),
    )
    result = await CompatibleProbeRegistry().execute(_plan("tool"), timeout=5)

    assert result.terminal_state == "verified"
    assert result.code == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_truncated_stream_is_still_a_failure(monkeypatch) -> None:
    """负对照：判据放宽到「正文像 SSE 就按 SSE 解」之后，坏流仍须判失败。

    没有终止帧（既无 `finish_reason` 也无 `[DONE]`）＝ 连接半路断了，不是一次成功的
    验证。没有这条，上面两条可以靠「什么都收就算过」作弊通过。
    """
    _serve(
        monkeypatch,
        _sse('{"id":"1","model":"m1","choices":[{"index":0,"delta":{"content":"OK"}}]}'),
    )
    result = await CompatibleProbeRegistry().execute(_plan("chat"), timeout=5)

    assert result.terminal_state == "failed"
    assert result.code == "invalid_response"


def test_custom_http_non_sse_contract_does_not_stream() -> None:
    """负对照：判据是「真实调用是否流式」，不是「一律加 stream」。

    custom_http 是**声明式**契约：provider 自己声明响应形态。声明成非 SSE 时真实调用
    不流式，探针也不得流式 —— 否则就从「探针测得太少」翻到「探针测了我们根本不发的形状」。
    """
    provider = {
        "type": "custom_http",
        "base_url": "https://gateway.example.com",
        "auth": {"type": "none"},
        "transports": {
            "chat": {
                "contract": "openai_chat_completions",
                "request": {
                    "method": "POST",
                    "url_template": "{base_url}/chat",
                    "body_template": {
                        "model": {"$bowhead": "model"},
                        "messages": {"$bowhead": "messages"},
                    },
                },
            },
        },
        "models": [
            {
                "id": "m1",
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
            }
        ],
    }
    plan = CompatibleProbeRegistry().plan(
        provider,
        provider_id=PROVIDER_ID,
        provider_display_name="P",
        model_id="m1",
        capability="chat",
    )
    assert isinstance(plan, ProbeRequestPlan)
    assert plan.transport.adapter_contract_version != "openai_chat_completions_sse"
    assert plan.body_shape.get("stream") is not True


def test_probe_consumes_the_model_body_overrides_the_runtime_applies() -> None:
    """A probe must honor the same model-level body overrides as runtime calls."""
    provider = {
        "type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "models": [
            {
                "id": "m1",
                "capabilities": ["chat", "tool"],
                "request_body_overrides": {"chat": {"temperature": 1}, "tool": {"temperature": 1}},
            }
        ],
    }

    assert _plan("chat", provider).body_shape["temperature"] == 1
    assert _plan("tool", provider).body_shape["temperature"] == 1


def test_overrides_cannot_rewrite_runtime_inputs_in_the_probe_either() -> None:
    """负对照：保护字段与运行时同一套 —— 覆盖不得改 messages/tools/model/stream。"""
    provider = {
        "type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "models": [
            {
                "id": "m1",
                "capabilities": ["chat"],
                "request_body_overrides": {"chat": {"messages": [{"role": "user", "content": "hijacked"}], "stream": False}},
            }
        ],
    }

    body = _plan("chat", provider).body_shape

    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["stream"] is True
