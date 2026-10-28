"""Required data integrity and conservative per-target budgets, not model quality."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from bowhead_core.skill_read_model_input import RequiredSkillModelInput
from bowhead_core.skill_read_render import RenderedSkillReads
from bowhead_router.errors import RetryPolicy, StreamingTargetsExhaustedError
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import ImageRef, LLMChunk, LLMRequest, LLMResponse, Message, ToolSchema

pytestmark = pytest.mark.unit


class Adapter:
    supports_input_admission = True

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def invoke(self, request):
        self.calls.append(request)
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return LLMResponse(content=value)

    async def stream(self, request):
        response = await self.invoke(request)
        yield LLMChunk(delta=response.content, finish_reason="stop")


class Registry:
    def __init__(self, **adapters):
        self.adapters = adapters

    def has(self, name):
        return name in self.adapters

    def get(self, name):
        return self.adapters[name]

    def definition_for(self, name):
        """Expose the exact synthetic adapter metadata used by output reservation."""
        from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter

        adapter = self.get(name)
        family = "anthropic_compatible" if isinstance(adapter, AnthropicCompatibleAdapter) else getattr(adapter, "adapter_type", "openai_compatible")
        return {**getattr(adapter, "provider_options", {}), "type": family}


class Failure(Exception):
    def __init__(self, retryable):
        self.retryable = retryable


def method(body="METHOD_DATA"):
    # Synthetic bytes isolate budgeting, never stand in for source authorization.
    content = body.encode()
    return RequiredSkillModelInput.from_rendered(
        RenderedSkillReads(content, "sha256:" + hashlib.sha256(content).hexdigest(), "preparation", "selection")
    )


def request(body="METHOD_DATA", **changes):
    base = LLMRequest(
        model="large", system="BASELINE", messages=[Message(role="user", content="TASK"), Message(role="user", content="FINALIZE IN CHINESE")]
    )
    base = replace(base, **changes)
    required = method(body)
    return required, required.attach(base)


def budget(required, req, window=100000):
    return required.budget(req, provider="primary", model=req.model, context_window=window)


def test_required_data_leaves_baseline_task_and_tail_order_unchanged():
    base = LLMRequest(
        model="large",
        system="BASELINE",
        messages=[
            Message(role="system", content="LEADING SYSTEM"),
            Message(role="user", content="TASK"),
            Message(role="user", content="FINALIZE IN CHINESE"),
        ],
    )
    required = method()
    attached = required.attach(base)
    assert attached.system == base.system
    assert [m.content for m in attached.messages] == ["LEADING SYSTEM", required.content, "TASK", "FINALIZE IN CHINESE"]
    assert len(base.messages) == 3 and base.input_admission is None
    assert "METHOD_DATA" not in repr(required)
    with pytest.raises(LLMInputAdmissionError, match="already_attached"):
        required.attach(attached)


@pytest.mark.parametrize("window", [None, True, 0, -1, "100000"])
def test_unknown_or_invalid_window_is_never_an_unlimited_budget(window):
    required, req = request()
    with pytest.raises(LLMInputAdmissionError, match="budget_unavailable"):
        budget(required, req, window)


def test_budget_precheck_logs_only_finite_failure_labels(caplog):
    required, req = request("PRIVATE_METHOD_DATA")
    with pytest.raises(LLMInputAdmissionError, match="^reference_model_budget_unavailable$"):
        required.budget(req, provider="primary", model=req.model, context_window=None)
    assert "failed=context_window" in caplog.text
    assert "PRIVATE_METHOD_DATA" not in caplog.text


@pytest.mark.parametrize("change", ["drop", "body", "position", "tool_role", "images", "hash", "admission"])
def test_missing_or_changed_required_input_is_rejected(change):
    required, req = request()
    if change == "drop":
        req.messages.pop(0)
    elif change == "body":
        req.messages[0].content = "TRUNCATED"
    elif change == "position":
        req.messages.reverse()
    elif change == "tool_role":
        req.messages[0].role = "tool"
    elif change == "images":
        req.messages[-1].images = [ImageRef(base64="AAAA", mime="image/gif")]
    elif change == "hash":
        required = replace(required, content_hash="sha256:" + "0" * 64)
        req.input_admission = required
    else:
        req.input_admission = None
    with pytest.raises(LLMInputAdmissionError):
        budget(required, req)


@pytest.mark.parametrize("body", ["A" * 8300, "中" * 3000, "\x01" * 1500], ids=["ascii", "cjk", "json-escaping"])
def test_encoded_required_data_has_a_hard_cap_without_truncation(body):
    required, req = request(body)
    original = list(req.messages)
    with pytest.raises(LLMInputAdmissionError, match="context_budget_exceeded"):
        budget(required, req, 1000000)
    assert req.messages == original and req.messages[0].content == required.content


def test_actual_system_tools_output_and_thinking_reduce_available_budget():
    required, req = request("X" * 1000)
    admitted = budget(required, req)
    assert admitted.estimate_contract == "utf8-bytes-conservative.v1"
    for changed in (
        replace(req, system="BASELINE" * 20000),
        replace(req, tools=[ToolSchema("long_tool", "description" * 20000, {})]),
        replace(req, max_output_tokens=99000),
        replace(req, thinking_budget_tokens=99000),
    ):
        with pytest.raises(LLMInputAdmissionError, match="context_budget_exceeded"):
            budget(required, changed)
    with pytest.raises(LLMInputAdmissionError, match="context_budget_exceeded"):
        budget(required, req, 8000)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_real_router_uses_each_target_budget_and_retains_full_data(mode, fallback):
    required, req = request("X" * 1000)
    primary = Adapter([Failure(False)] if fallback else ["done", "done"])
    secondary = Adapter(["must not execute"])
    router = Router(Registry(primary=primary, secondary=secondary))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large"), fallback=[("secondary", "small")]))
    router.set_context_windows({("primary", "large"): 100000, ("secondary", "small"): 8000})

    async def call():
        if mode == "invoke":
            return (await router.invoke("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=1))).content
        return "".join([chunk.delta async for chunk in router.stream("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=1))])

    if fallback:
        with pytest.raises(LLMInputAdmissionError, match="context_budget_exceeded"):
            await call()
    else:
        assert await call() == await call() == "done"
    assert secondary.calls == []
    assert len(req.messages) == 3
    assert all(r.messages[0].content == required.content and len(r.messages) == 3 for r in primary.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_undeclared_fallback_is_skipped_but_later_declared_target_can_run(mode, caplog):
    required, req = request("METHOD_DATA")
    primary = Adapter([Failure(False)])
    undeclared = Adapter(["must not execute"])
    declared = Adapter(["done"])
    router = Router(Registry(primary=primary, undeclared=undeclared, declared=declared))
    router.bind(
        AgentBinding(
            agent_type="analyst",
            primary=("primary", "large"),
            fallback=[("undeclared", "small"), ("declared", "large")],
        )
    )
    router.set_context_windows({("primary", "large"): 100000, ("declared", "large"): 100000})

    policy = RetryPolicy(max_attempts_per_target=1)
    if mode == "invoke":
        result = (await router.invoke("analyst", req, retry_policy=policy)).content
    else:
        result = "".join([chunk.delta async for chunk in router.stream("analyst", req, retry_policy=policy)])

    assert result == "done"
    assert len(primary.calls) == len(declared.calls) == 1
    assert undeclared.calls == []
    assert declared.calls[0].messages[0].content == required.content
    assert "router.input_admission_fallback_skipped" in caplog.text


@pytest.mark.asyncio
async def test_skipped_undeclared_fallback_preserves_safe_stream_to_invoke_recovery():
    _, req = request("METHOD_DATA")
    primary = Adapter([TimeoutError("provider timeout")])
    undeclared = Adapter(["must not execute"])
    router = Router(Registry(primary=primary, undeclared=undeclared))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large"), fallback=[("undeclared", "small")]))
    router.set_context_windows({("primary", "large"): 100000})

    with pytest.raises(StreamingTargetsExhaustedError) as error:
        async for _ in router.stream("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=1)):
            pass

    assert error.value.safe_to_fallback
    assert len(primary.calls) == 1
    assert undeclared.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_retry_observes_a_changed_target_window_before_reusing_the_request(mode):
    required, req = request("X" * 1000)

    class Shrinking(Adapter):
        async def invoke(self, request):
            self.calls.append(request)
            router.set_context_windows({("primary", "large"): 1000})
            raise Failure(True)

    adapter = Shrinking([])
    router = Router(Registry(primary=adapter))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large")))
    router.set_context_windows({("primary", "large"): 100000})
    with pytest.raises(LLMInputAdmissionError, match="context_budget_exceeded"):
        if mode == "invoke":
            await router.invoke("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=2, base_delay_s=0))
        else:
            async for _ in router.stream("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=2, base_delay_s=0)):
                pass
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("change", ["hash", "contract", "utf8"])
def test_invalid_render_is_a_finite_error_without_private_bytes(change):
    import traceback

    content = b"PRIVATE_METHOD" if change != "utf8" else b"PRIVATE_METHOD\xff"
    rendered = RenderedSkillReads(content, "sha256:" + hashlib.sha256(content).hexdigest(), "preparation", "selection")
    if change == "hash":
        rendered = replace(rendered, rendered_content_hash="sha256:" + "0" * 64)
    elif change == "contract":
        rendered = replace(rendered, render_contract="future")
    with pytest.raises(LLMInputAdmissionError, match="^reference_model_input_invalid$") as error:
        RequiredSkillModelInput.from_rendered(rendered)
    assert "PRIVATE_METHOD" not in "".join(traceback.format_exception(error.value))
