"""LLM Router 的核心数据结构。

所有 Adapter 对上层暴露 ``LLMRequest`` → ``LLMResponse`` 契约，上层 Agent
代码不感知具体 provider（TD §4.3.6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bowhead_router.input_admission import LLMInputAdmission
    from bowhead_router.output_budget import RenderedRequestBudget

MessageRole = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_use", "content_filter", "error"]


@dataclass
class ImageRef:
    """单张图片 —— base64 inline, 用于 vision 模型的 content block.
    mime 支持 image/png, image/jpeg, image/webp.
    """

    base64: str
    mime: str


@dataclass
class Message:
    """单条消息。"""

    role: MessageRole
    content: str
    name: str | None = None  # Tool name for messages with the tool role.
    tool_call_id: str | None = None  # ID of the corresponding tool call for tool messages.
    tool_calls: list[ToolCall] | None = None  # Calls initiated by messages with the assistant role.
    # Image attachments (M1 P5), for user messages only. Vision-capable adapters convert content
    # into multiple blocks (text + image_url / base64 images); other adapters ignore images,
    # keep content as plain text, and log a warning.
    images: list[ImageRef] = field(default_factory=list)


@dataclass
class ToolSchema:
    """工具定义（传给 LLM）。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    """LLM 触发的工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class TokenUsage:
    """Token 使用情况（Router 用来记成本）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMRequest:
    """标准化的 LLM 请求。"""

    model: str
    messages: list[Message]
    system: str | None = None
    tools: list[ToolSchema] = field(default_factory=list)
    temperature: float = 0.7
    top_p: float = 1.0
    max_output_tokens: int | None = None
    stop: list[str] = field(default_factory=list)
    extended_thinking: bool = False
    thinking_budget_tokens: int = 0
    stream: bool = False
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # OpenAI / DashScope `response_format`: enforce output format constraints.
    # Supported values: None (default free-form) / {"type": "json_object"} (require valid JSON).
    # subagent_return / structured payload paths use json_object to reduce Qwen literal contamination.
    response_format: dict[str, Any] | None = None
    # "none" keeps the declared tools (so tool history stays valid) but forbids calling them;
    # used for the wrap-up call when an agent run stops at a limit.
    tool_choice: Literal["auto", "none"] = "auto"
    # Server-only policy; adapters serialize explicit provider fields, never this object.
    input_admission: LLMInputAdmission | None = field(default=None, repr=False, compare=False)
    input_admission_window: int | None = field(default=None, repr=False, compare=False)
    # Server-only logical preparation identity; never a provider/billing ID.
    input_context_id: str | None = field(default=None, repr=False, compare=False)
    invocation_agent_type: str = field(default="unknown", repr=False, compare=False)
    physical_receipt_recorded: bool = field(default=False, repr=False, compare=False)
    credit_rates: dict[str, str] | None = field(default=None, repr=False, compare=False)
    invocation_execution_id: str | None = field(default=None, repr=False, compare=False)
    invocation_subagent_id: str | None = field(default=None, repr=False, compare=False)
    output_budget: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    context_budget: RenderedRequestBudget | None = field(default=None, repr=False, compare=False)
    summary_only: bool = field(default=False, repr=False, compare=False)
    output_reservation: int | None = field(default=None, repr=False, compare=False)
    configuration_version: int | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class LLMResponseDiagnostics:
    """Content-free adapter observations; absent counts mean unobserved, not zero."""

    transport_mode: Literal["stream", "invoke"]
    upstream_tool_call_count: int | None = None
    upstream_finish_reason: str | None = None
    legacy_function_call: bool | None = None
    incomplete_reason: str | None = None
    reasoning_tokens: int | None = None
    output_token_limit: int | None = None
    output_budget: dict[str, Any] | None = None

    def trace_fields(self) -> dict[str, Any]:
        """Project a fixed, bounded shape without model-controlled strings."""

        count = self.upstream_tool_call_count
        return {
            **({"output_budget": dict(self.output_budget)} if self.output_budget is not None else {}),
            "mode": self.transport_mode if isinstance(self.transport_mode, str) and self.transport_mode in {"stream", "invoke"} else "unknown",
            "upstream_tool_call_count": count if type(count) is int and count >= 0 else None,
            "upstream_finish_reason": (
                self.upstream_finish_reason
                if self.upstream_finish_reason is None
                or (
                    isinstance(self.upstream_finish_reason, str)
                    and self.upstream_finish_reason
                    in {"stop", "length", "tool_calls", "function_call", "content_filter", "completed", "incomplete", "failed"}
                )
                else "unknown"
            ),
            "legacy_function_call": self.legacy_function_call if type(self.legacy_function_call) is bool else None,
            "incomplete_reason": self.incomplete_reason
            if self.incomplete_reason is None
            or (isinstance(self.incomplete_reason, str) and self.incomplete_reason in {"max_output_tokens", "content_filter"})
            else "unknown",
            "reasoning_tokens": self.reasoning_tokens if type(self.reasoning_tokens) is int and self.reasoning_tokens >= 0 else None,
            "output_token_limit": self.output_token_limit if type(self.output_token_limit) is int and self.output_token_limit > 0 else None,
        }


@dataclass
class LLMResponse:
    """标准化的 LLM 响应。"""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    # Reasoning trace from thinking models (e.g. Doubao-Seed-2.0-pro / Claude extended thinking).
    # Anthropic Messages API: content block `{type:"thinking", thinking:"..."}`
    # An empty string indicates a non-thinking model or no reasoning output.
    thinking: str = ""
    diagnostics: LLMResponseDiagnostics | None = None


@dataclass
class LLMChunk:
    """流式响应中的一帧（Delta）。"""

    delta: str = ""
    # P4.D: streaming chain-of-thought deltas from thinking models (qwen3.6 / Doubao reasoning /
    # Claude extended_thinking). Content deltas and thinking deltas use separate streams;
    # usually only one is nonempty in each chunk.
    thinking_delta: str = ""
    tool_call_delta: ToolCall | None = None
    # Sprint chat-redesign-2026-05-21 Phase 1.1 (Spike 1) — partial tool-call
    # argument stream for `lead_thinking_writing` UX. Surfaces the JSON the LLM
    # is writing into tool args BEFORE the full block closes (which is what
    # `tool_call_delta` waits for). Consumers (subagent.py) gate on
    # tool_name ∈ {python_sandbox, bi_query} to avoid leaking every internal
    # tool's partial JSON to chat.
    # Shape: {"tool_name": str, "tool_call_id": str, "partial_args_json": str}
    tool_call_partial: dict[str, str] | None = None
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None
    diagnostics: LLMResponseDiagnostics | None = None


@runtime_checkable
class LLMAdapter(Protocol):
    """LLM 厂商适配器契约（见 TD §4.3.6）。"""

    provider_name: str

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        """一次非流式调用。"""
        ...

    def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        """流式调用。返回异步迭代器。"""
        ...

    async def close(self) -> None:
        """释放连接池 / 清理资源。"""
        ...
