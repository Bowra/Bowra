"""Adapter 基类与 MockAdapter（M1 扩展：支持 scripted tool-use loop）.

M1 会新增：
    - OpenAICompatibleAdapter
    - AnthropicCompatibleAdapter
    - CustomHttpAdapter（用户通过 URL + header + Jinja 模板接任意 LLM，TD §4.3.4）
    - AzureOpenAIAdapter / BedrockAdapter / VertexAdapter / OllamaAdapter

Sprint 0 的 MockAdapter 只做 echo。M1 扩展支持 scripted responses，让 Orchestrator
的 tool-use loop（LLM → tool_call → LLM → content）可以在无真 LLM 环境下测试。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bowhead_router.types import (
    LLMAdapter,
    LLMChunk,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class MockAdapter:
    """开发 / 测试用 mock 适配器.

    三种使用方式：

    1. **Echo 模式**（默认）：返回 "echoing: <last user msg>"
    2. **Preset 模式**：``preset_response="canned"`` 固定返回
    3. **Scripted 模式**（M1 新增）：``scripted_responses=[...]`` 按调用顺序返回预设响应。
       常用来模拟 tool-use loop：

       >>> adapter = MockAdapter(scripted_responses=[
       ...     LLMResponse(tool_calls=[ToolCall("c1", "bi_ask_question",
       ...                                       {"text": "今年 GMV"})]),
       ...     LLMResponse(content="根据 BI，今年 GMV 是 29.3 亿。"),
       ... ])
       >>> # 第一次调用返回 tool_call；第二次返回最终答案

    Scripted 模式下耗尽后自动回落到 echo 模式（或可配置 ``raise_on_exhaust=True``）。
    """

    provider_name: str = "mock"
    model_name: str = "mock-model"
    preset_response: str | None = None
    preset_tool_calls: list[ToolCall] = field(default_factory=list)
    preset_finish: str = "stop"
    latency_ms: int = 0
    scripted_responses: list[LLMResponse] = field(default_factory=list)
    raise_on_exhaust: bool = False
    _script_index: int = 0

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)

        # Scripted mode takes precedence.
        if self.scripted_responses and self._script_index < len(self.scripted_responses):
            resp = self.scripted_responses[self._script_index]
            self._script_index += 1
            # Fill in provider/model so Router attribution is correct.
            return LLMResponse(
                content=resp.content,
                tool_calls=list(resp.tool_calls),
                finish_reason=resp.finish_reason or ("tool_use" if resp.tool_calls else "stop"),
                usage=resp.usage or TokenUsage(input_tokens=10, output_tokens=10),
                raw=resp.raw or {"mock": True, "script_index": self._script_index - 1},
                provider=self.provider_name,
                model=req.model or self.model_name,
            )

        if self.scripted_responses and self.raise_on_exhaust:
            raise RuntimeError(f"MockAdapter scripted responses exhausted (used {self._script_index} / {len(self.scripted_responses)})")

        # Preset / echo fallback
        if self.preset_response is not None:
            content = self.preset_response
        else:
            last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
            content = f"[mock] echoing: {last_user.content if last_user else '<empty>'}"

        usage = TokenUsage(
            input_tokens=sum(len(m.content) for m in req.messages) // 4,
            output_tokens=len(content) // 4,
        )
        return LLMResponse(
            content=content,
            tool_calls=list(self.preset_tool_calls),
            finish_reason=self.preset_finish,  # type: ignore[arg-type]
            usage=usage,
            raw={"mock": True},
            provider=self.provider_name,
            model=req.model or self.model_name,
        )

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        resp = await self.invoke(req)
        for word in resp.content.split():
            yield LLMChunk(delta=word + " ")
            if self.latency_ms:
                await asyncio.sleep(self.latency_ms / 1000 / max(1, len(resp.content.split())))
        yield LLMChunk(finish_reason=resp.finish_reason, usage=resp.usage)

    async def close(self) -> None:
        pass

    def reset_script(self) -> None:
        """测试辅助：复位 scripted 游标."""
        self._script_index = 0


class TeamModelUnconfiguredError(RuntimeError):
    """进程级装配 Router 在非 dev 环境被真正调用 = 系统缺陷。

    credential-readiness D1：`529efa9c` 之后平台不持有部署级模型密钥，进程级
    Router 仅供缺少团队上下文的内部装配；任何真实调用都必须携带团队 router
    （TurnRequest.team_router / per-route 解析）。走到这里说明某条旁路漏接——
    fail loudly 并经 record_refusal 归因，绝不静默 echo。

    ``retryable = False``：router._invoke_with_retry 读此属性，不重试立刻穿透；
    装配 binding 无 fallback 链，一轮即抛（tech-design §3.2 三条安全性事实）。
    """

    retryable = False


@dataclass
class RefusalAdapter:
    """非 dev 环境的装配 Router 适配器——被调用即拒绝（credential-readiness D1）。

    与 MockAdapter 平级、互不沾染：测试替身不携带 prod 策略（PRD §14 F3），
    环境判断只发生在装配点（gateway `_build_assembly_router`）。拒绝发生在
    usage seam 之下：record_usage 在 adapter 成功返回后才调用，抛错 = 零用量
    记录（验收 A5b 的判据）。
    """

    provider_name: str = "llm_refusal"

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        raise self._refuse(req)

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        raise self._refuse(req)
        yield  # pragma: no cover — Make this an async generator to match the protocol signature.

    async def close(self) -> None:
        pass

    def _refuse(self, req: LLMRequest) -> TeamModelUnconfiguredError:
        from bowhead_router.usage import current_usage_context, record_refusal

        ctx = current_usage_context()
        logger.error(
            "llm_refusal.invoked model=%s reason=%s turn=%s",
            req.model or "<unbound>",
            ctx.reason,
            ctx.turn_id or "-",
        )
        record_refusal()
        return TeamModelUnconfiguredError(f"process-level assembly router invoked (reason={ctx.reason}); this path must carry a team router")


_: type[LLMAdapter] = MockAdapter  # type: ignore[assignment,type-abstract]
_refusal_conforms: type[LLMAdapter] = RefusalAdapter  # type: ignore[assignment,type-abstract]
