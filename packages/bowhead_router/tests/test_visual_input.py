"""Visual self-check, router part: which targets read images, and withholding for the others.

Design: docs/sprints/visual-self-check-2026-11-16/tech-design.md §3 (FR1, FR2, FR15).
"""

from __future__ import annotations

import pytest
from bowhead_router.router import AgentBinding, Router, UserModelPref
from bowhead_router.types import ImageRef, LLMChunk, LLMRequest, LLMResponse, Message
from bowhead_router.visual_input import WITHHELD_IMAGE_NOTICE, image_input_targets, withhold_images

PROVIDERS = {
    "deepseek": {
        "type": "openai_compatible",
        "supports": {"vision": False, "tool_use": True},
        "models": [
            {"id": "deepseek-flash", "capabilities": ["chat", "tool", "vision"]},
            {"id": "deepseek-v4-pro", "capabilities": ["chat", "tool"]},
        ],
    },
    "qwen": {"type": "openai_compatible", "supports": {"vision": True}, "models": ["qwen3-vl-plus"]},
    "doubao": {"type": "anthropic_compatible", "supports": {"vision": False}, "models": ["Doubao-Seed-2.0-pro"]},
}


class _Registry:
    def __init__(self, adapters: dict[str, object]) -> None:
        self.adapters = adapters

    def has(self, name: str) -> bool:
        return name in self.adapters

    def get(self, name: str) -> object:
        return self.adapters[name]


class _Recorder:
    """Records each request; the first ``fail`` calls raise a non-retryable error."""

    def __init__(self, fail: int = 0) -> None:
        self.requests: list[LLMRequest] = []
        self.fail = fail

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if len(self.requests) <= self.fail:
            error = RuntimeError("http 400")
            error.retryable = False  # type: ignore[attr-defined]
            raise error
        return LLMResponse(content="ok")

    async def stream(self, req: LLMRequest):
        self.requests.append(req)
        yield LLMChunk(delta="ok")
        yield LLMChunk(finish_reason="stop")


def _request() -> LLMRequest:
    from bowhead_router.runtime_notes import VISUAL_VIEW_NOTE_NAME

    return LLMRequest(
        model="",
        messages=[
            Message(role="user", content="Make a deck like this.", images=[ImageRef(base64="BBBB", mime="image/png")]),
            Message(role="user", name=VISUAL_VIEW_NOTE_NAME, content="Rendered view.", images=[ImageRef(base64="AAAA", mime="image/png")]),
        ],
    )


def _router(primary: tuple[str, str], fallback: list[tuple[str, str]], adapters: dict[str, object], *, targets: bool = True) -> Router:
    router = Router(_Registry(adapters))  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="lead", primary=primary, fallback=fallback))
    router.max_retries = 0
    if targets:
        router.set_image_input_targets(image_input_targets(PROVIDERS))
    return router


def test_image_input_targets_read_rich_capabilities_and_legacy_supports() -> None:
    assert image_input_targets(PROVIDERS) == frozenset({("deepseek", "deepseek-flash"), ("qwen", "qwen3-vl-plus")})
    assert image_input_targets(None) == frozenset()


def test_accepts_images_follows_the_primary_and_user_pref() -> None:
    router = _router(("deepseek", "deepseek-flash"), [("deepseek", "deepseek-v4-pro")], {})
    assert router.accepts_images_for("lead") is True
    assert router.accepts_images_for("lead", user_pref=UserModelPref(provider="deepseek", model="deepseek-v4-pro")) is False
    assert router.accepts_images_for("helper") is False  # no binding


def test_accepts_images_is_false_without_catalog_capabilities() -> None:
    router = _router(("deepseek", "deepseek-flash"), [], {}, targets=False)
    assert router.accepts_images_for("lead") is False


def test_withhold_images_keeps_other_messages_identical() -> None:
    from bowhead_router.runtime_notes import VISUAL_VIEW_NOTE_NAME

    request = _request()
    messages, count = withhold_images(request.messages, names=frozenset({VISUAL_VIEW_NOTE_NAME}))
    assert count == 1
    assert messages[0] is request.messages[0]  # the user's attachment is not touched
    assert messages[1].images == []
    assert messages[1].content == f"Rendered view.\n{WITHHELD_IMAGE_NOTICE}"
    assert request.messages[1].images  # the caller's request is unchanged
    everything, total = withhold_images(request.messages)
    assert total == 2 and everything[0].images == []


@pytest.mark.asyncio
async def test_fallback_without_vision_gets_the_notice_for_renders_only(monkeypatch) -> None:
    monkeypatch.delenv("FEATURE_VISUAL_SELF_CHECK", raising=False)  # on by default (D13)
    flash, pro = _Recorder(fail=1), _Recorder()
    router = _router(("deepseek", "deepseek-flash"), [("deepseek_pro", "deepseek-v4-pro")], {"deepseek": flash, "deepseek_pro": pro})
    router.set_image_input_targets({("deepseek", "deepseek-flash")})
    response = await router.invoke("lead", _request())
    assert flash.requests[0].messages[1].images  # the vision primary received the image
    sent = pro.requests[0].messages[1]
    assert sent.images == [] and WITHHELD_IMAGE_NOTICE in sent.content
    assert pro.requests[0].messages[0].images  # user attachments are sent as before
    assert response.diagnostics is not None and response.diagnostics.images_withheld == 1
    assert response.diagnostics.trace_fields()["images_withheld"] == 1


@pytest.mark.asyncio
async def test_switch_off_sends_images_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_VISUAL_SELF_CHECK", "0")
    pro = _Recorder()
    router = _router(("deepseek_pro", "deepseek-v4-pro"), [], {"deepseek_pro": pro})
    response = await router.invoke("lead", _request())
    assert pro.requests[0].messages[1].images
    assert response.diagnostics is None or "images_withheld" not in response.diagnostics.trace_fields()


@pytest.mark.asyncio
async def test_stream_withholds_for_a_target_without_vision(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_VISUAL_SELF_CHECK", "1")
    pro = _Recorder()
    router = _router(("deepseek_pro", "deepseek-v4-pro"), [], {"deepseek_pro": pro})
    chunks = [chunk async for chunk in router.stream("lead", _request())]
    assert pro.requests[0].messages[1].images == []
    final = [chunk for chunk in chunks if chunk.finish_reason is not None][-1]
    assert final.diagnostics is not None and final.diagnostics.images_withheld == 1


# ------------------------------------------------- provider request shapes (§11)


def _render_request(model: str) -> LLMRequest:
    from bowhead_router.runtime_notes import VISUAL_VIEW_NOTE_NAME
    from bowhead_router.types import ToolCall, ToolSchema

    return LLMRequest(
        model=model,
        messages=[
            Message(role="user", content="Make a chart."),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="call_a", name="python_sandbox", arguments={})]),
            Message(role="tool", content='{"ok": true}', tool_call_id="call_a", name="python_sandbox"),
            Message(role="user", name=VISUAL_VIEW_NOTE_NAME, content="Rendered view.", images=[ImageRef(base64="AAAA", mime="image/png")]),
            Message(role="user", content="<turn tail>"),
        ],
        tools=[ToolSchema(name="python_sandbox", description="Run Python", input_schema={"type": "object"})],
    )


def _flatten(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def test_anthropic_compatible_merges_the_render_into_the_tool_result_turn() -> None:
    from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
    from bowhead_router.runtime_notes import RUNTIME_NOTE_PREFIX

    body = AnthropicCompatibleAdapter(base_url="https://api.example.com", api_key="sk-test")._build_body(
        _render_request("deepseek-flash"), stream=False
    )
    last = body["messages"][-1]
    assert last["role"] == "user"
    kinds = [block["type"] for block in last["content"]]
    assert kinds[0] == "tool_result" and "image" in kinds
    text = next(block["text"] for block in last["content"] if block["type"] == "text" and "Rendered view." in block["text"])
    assert text.startswith(RUNTIME_NOTE_PREFIX)
    image = next(block for block in last["content"] if block["type"] == "image")
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert "bowhead_visual_view_v1" not in _flatten(body)


@pytest.mark.parametrize("model", ["chat-model", "resp-model"])
def test_openai_compatible_sends_the_render_as_a_user_item_after_the_tool_output(model: str) -> None:
    from bowhead_router.runtime_notes import RUNTIME_NOTE_PREFIX

    from bowhead_router import OpenAICompatibleAdapter

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        provider_name="test_provider",
        provider_options={"models": [{"id": "resp-model", "api_surface": "responses"}, {"id": "chat-model"}]},
    )
    body = adapter._build_body(_render_request(model), stream=False)
    items = body.get("messages") or body["input"]
    output_index = next(index for index, item in enumerate(items) if item.get("role") == "tool" or item.get("type") == "function_call_output")
    render = items[output_index + 1]
    assert render["role"] == "user" and "name" not in render
    serialized = _flatten(render["content"])
    assert RUNTIME_NOTE_PREFIX.strip() in serialized and "data:image/png;base64,AAAA" in serialized
    assert "bowhead_visual_view_v1" not in _flatten(body)


@pytest.mark.parametrize(("value", "enabled"), [(None, True), ("1", True), ("on", True), ("0", False), ("false", False), (" OFF ", False)])
def test_switch_is_on_by_default_and_zero_turns_it_off(monkeypatch, value, enabled) -> None:
    from bowhead_router.visual_input import visual_self_check_enabled

    if value is None:
        monkeypatch.delenv("FEATURE_VISUAL_SELF_CHECK", raising=False)
    else:
        monkeypatch.setenv("FEATURE_VISUAL_SELF_CHECK", value)
    assert visual_self_check_enabled() is enabled
