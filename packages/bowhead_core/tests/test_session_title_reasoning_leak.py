"""会话标题不得是模型的推理文本（116 1.3.37 实测「<think> 用户输入的问题是…」）。

走真 Router：清理发生在 Router.invoke 出口，用假 router 直接喂 generate_title 会绕开它。
"""

from __future__ import annotations

import pytest
from bowhead_core.session_title import generate_title
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMRequest, LLMResponse


class _Registry:
    def __init__(self, adapter: object) -> None:
        self.adapter = adapter

    def has(self, name: str) -> bool:
        return name == "provider"

    def get(self, name: str) -> object:
        return self.adapter


class _Adapter:
    def __init__(self, content: str) -> None:
        self.content = content

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        return LLMResponse(content=self.content)


def _router(content: str) -> Router:
    router = Router(_Registry(_Adapter(content)))  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="echo", primary=("provider", "model")))
    return router


@pytest.mark.asyncio
async def test_reasoning_cut_off_by_the_token_limit_never_becomes_the_title() -> None:
    # Title call max_output_tokens=40: reasoning was cut off before even one title character was written.
    title = await generate_title(
        _router('<think> 用户输入的问题是一个简单的数学计算："只回答数字'),
        user_text="只回答数字：1+1 等于几？",
        final_answer="2",
    )

    assert title is None  # The caller keeps the default title (first line of the user question), rather than storing reasoning text.


@pytest.mark.asyncio
async def test_title_after_a_complete_reasoning_block_is_kept() -> None:
    title = await generate_title(
        _router("<think>用户问的是订单趋势</think>\n近7天订单趋势"),
        user_text="近7天每日订单量",
        final_answer="……",
    )

    assert title == "近7天订单趋势"
