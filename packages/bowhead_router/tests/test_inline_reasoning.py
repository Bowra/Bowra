"""正文开头内联的 <think> 推理块在 Router.invoke 出口被挪进 thinking。"""

from __future__ import annotations

import pytest
from bowhead_router.inline_reasoning import split_inline_reasoning
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, ToolCall

# Observed on 116 (1.3.37): max_output_tokens=40 cut off reasoning, which became the session title verbatim.
LEAKED_116_TITLE_OUTPUT = '<think> 用户输入的问题是一个简单的数学计算："只回答数字'


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<think>先想想</think>\n近7天订单趋势", ("近7天订单趋势", "先想想")),
        (LEAKED_116_TITLE_OUTPUT, ("", '用户输入的问题是一个简单的数学计算："只回答数字')),
        ("  \n<think>a</think>答案", ("答案", "a")),
        ("<THINK>大写</THINK>答案", ("答案", "大写")),
        ("<thinking>别名</thinking>答案", ("答案", "别名")),
        ("<think>一</think><think>二</think>答案", ("答案", "一\n二")),
        ("<think></think>答案", ("答案", "")),
    ],
)
def test_leading_reasoning_block_is_split_off(content: str, expected: tuple[str, str]) -> None:
    assert split_inline_reasoning(content) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "近7天订单趋势",
        "",
        "HTML 里的 <think> 标签只是示例文本",
        "答案</think>",
    ],
)
def test_content_without_a_leading_block_is_untouched(content: str) -> None:
    assert split_inline_reasoning(content) == (content, "")


class _Registry:
    def __init__(self, adapters: dict[str, object]) -> None:
        self.adapters = adapters

    def has(self, name: str) -> bool:
        return name in self.adapters

    def get(self, name: str) -> object:
        return self.adapters[name]


class _OneShotAdapter:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        return self.response


class _StreamingAdapter:
    def __init__(self, chunks: list[LLMChunk]) -> None:
        self.chunks = chunks

    async def stream(self, req: LLMRequest):
        for chunk in self.chunks:
            yield chunk


def _router(response: LLMResponse) -> Router:
    router = Router(_Registry({"provider": _OneShotAdapter(response)}))  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="echo", primary=("provider", "model")))
    return router


@pytest.mark.asyncio
async def test_router_invoke_moves_inline_reasoning_into_thinking() -> None:
    tool_call = ToolCall(id="c1", name="noop", arguments={})
    resp = await _router(
        LLMResponse(content="<think>推理</think>\n双11销售下滑归因", thinking="提供方单独给的", tool_calls=[tool_call]),
    ).invoke("echo", LLMRequest(model="", messages=[]))

    assert resp.content == "双11销售下滑归因"
    assert resp.thinking == "提供方单独给的\n推理"
    assert resp.tool_calls == [tool_call]


@pytest.mark.asyncio
async def test_router_invoke_leaves_ordinary_content_alone() -> None:
    resp = await _router(LLMResponse(content="近7天订单趋势")).invoke("echo", LLMRequest(model="", messages=[]))

    assert resp.content == "近7天订单趋势"
    assert resp.thinking == ""


@pytest.mark.asyncio
async def test_router_stream_keeps_chunked_inline_reasoning_out_of_visible_deltas() -> None:
    router = Router(
        _Registry(
            {
                "provider": _StreamingAdapter(
                    [
                        LLMChunk(delta="<thi"),
                        LLMChunk(delta="nk>Do not show this"),
                        LLMChunk(delta=" reasoning</think>答案"),
                        LLMChunk(delta="完成", finish_reason="stop"),
                    ]
                )
            }
        )
    )  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="echo", primary=("provider", "model")))

    chunks = [chunk async for chunk in router.stream("echo", LLMRequest(model="", messages=[]))]

    assert "".join(chunk.delta for chunk in chunks) == "答案完成"
    assert "Do not show this reasoning" in "".join(chunk.thinking_delta for chunk in chunks)
    assert all("think" not in chunk.delta for chunk in chunks)


@pytest.mark.asyncio
async def test_router_stream_with_truncated_reasoning_has_no_visible_text() -> None:
    router = Router(
        _Registry(
            {
                "provider": _StreamingAdapter(
                    [
                        LLMChunk(delta="<think>private"),
                        LLMChunk(delta=" analysis", finish_reason="length"),
                    ]
                )
            }
        )
    )  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="echo", primary=("provider", "model")))

    chunks = [chunk async for chunk in router.stream("echo", LLMRequest(model="", messages=[]))]

    assert "".join(chunk.delta for chunk in chunks) == ""
    assert "private analysis" in "".join(chunk.thinking_delta for chunk in chunks)


@pytest.mark.asyncio
async def test_router_stream_preserves_ordinary_text_and_mid_answer_tag_example() -> None:
    router = Router(
        _Registry(
            {
                "provider": _StreamingAdapter(
                    [
                        LLMChunk(delta="答"),
                        LLMChunk(delta="案中的 <think> 是标签示例", finish_reason="stop"),
                    ]
                )
            }
        )
    )  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="echo", primary=("provider", "model")))

    chunks = [chunk async for chunk in router.stream("echo", LLMRequest(model="", messages=[]))]

    assert [chunk.delta for chunk in chunks] == ["答", "案中的 <think> 是标签示例"]
    assert chunks[-1].finish_reason == "stop"
