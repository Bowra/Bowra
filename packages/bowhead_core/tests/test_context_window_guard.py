"""上下文工程 sprint 批 4 —— 窗口守卫（core 侧）：纯函数、配置校验、穿过 SubagentRunner 的接线。"""

from __future__ import annotations

import pytest
from bowhead_core.context_assembly import (
    WINDOW_GUARD_HYSTERESIS,
    cleared_marker,
    drop_history_pairs,
    estimate_messages_tokens,
    guard_window,
    is_cleared,
    tool_schemas_tokens,
)
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.team_llm_config import TeamLlmConfigError, _validate_provider, _validate_provider_models, context_windows_for_catalog
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.errors import LLMTargetFailure, ProviderFailureKind, StreamingTargetsExhaustedError
from bowhead_router.types import Message, ToolCall, ToolSchema

from bowhead_core import context_assembly as ca
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

# One-character-one-token estimator makes test numbers directly readable.
_ONE = len


@pytest.mark.unit
def test_preflight_capacity_failure_uses_the_existing_actionable_overflow_projection() -> None:
    from bowhead_core.llm_failure import project_llm_failure
    from bowhead_router.output_budget import ContextCapacityError, RenderedRequestBudget

    error = ContextCapacityError(
        "Private request content must not enter diagnostics",
        model="private-model-name",
        budget=RenderedRequestBudget(40000, "text", 32000, "model", 4096),
    )
    projection = project_llm_failure(error)
    assert projection is not None and projection.reason_code == "llm_context_overflow"
    assert projection.attempts == 0 and not projection.retryable
    assert "Private request" not in projection.safe_detail and "private-model-name" not in projection.safe_detail


def _tool(content: str, i: int) -> Message:
    return Message(role="tool", content=content, tool_call_id=f"c{i}", name="t")


def _asst(i: int) -> Message:
    return Message(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name="t", arguments={})])


_T = 200  # Characters per tool result.


def _convo() -> list[Message]:
    # user(5) tool(200) ×3 = 605 (empty assistant content/args excluded); tools after the last assistant are this iteration's new results.
    # cleared_marker(200) is 55 characters; clearing one saves 145. Once triggered, clear to 0.8×threshold (hysteresis).
    return [
        Message(role="user", content="q" * 5),
        _asst(1),
        _tool("a" * _T, 1),
        _asst(2),
        _tool("b" * _T, 2),
        _asst(3),
        _tool("c" * _T, 3),
    ]


# ── Pure functions ──


@pytest.mark.unit
def test_under_budget_touches_nothing() -> None:
    msgs = _convo()
    before = [m.content for m in msgs]
    assert guard_window(msgs, budget_tokens=10_000, estimate=_ONE) == 0
    assert [m.content for m in msgs] == before


@pytest.mark.unit
def test_over_budget_clears_oldest_tool_result_first() -> None:
    msgs = _convo()
    # Total 605; threshold 600 ⇒ target 480; clearing the oldest (605−145=460) suffices.
    assert WINDOW_GUARD_HYSTERESIS == 0.8
    cleared = guard_window(msgs, budget_tokens=600, estimate=_ONE)
    assert cleared == _T
    assert msgs[2].content == cleared_marker(_T)
    assert msgs[4].content == "b" * _T, "不该多清"


@pytest.mark.unit
def test_keeps_clearing_until_under_the_line() -> None:
    msgs = _convo()
    # Threshold 500 ⇒ target 400: clearing one leaves 460, two leave 315 (review fix: clear to hysteresis once, not one near-threshold item per loop).
    cleared = guard_window(msgs, budget_tokens=500, estimate=_ONE)
    assert cleared == 2 * _T
    assert is_cleared(msgs[2].content) and is_cleared(msgs[4].content)


@pytest.mark.unit
def test_never_clears_results_after_the_last_assistant_turn() -> None:
    """本圈刚拿到的结果模型还没消化；清了它模型只会再调一次同一个工具。清完仍超线就停。"""
    msgs = _convo()
    cleared = guard_window(msgs, budget_tokens=100, estimate=_ONE)
    assert cleared == 2 * _T
    assert msgs[6].content == "c" * _T


@pytest.mark.unit
def test_user_and_assistant_messages_are_never_touched() -> None:
    msgs = [Message(role="user", content="u" * 500), Message(role="assistant", content="a" * 500)]
    assert guard_window(msgs, budget_tokens=10, estimate=_ONE) == 0
    assert msgs[0].content == "u" * 500 and msgs[1].content == "a" * 500


@pytest.mark.unit
def test_already_cleared_is_not_counted_again() -> None:
    msgs = _convo()
    guard_window(msgs, budget_tokens=600, estimate=_ONE)
    again = guard_window(msgs, budget_tokens=600, estimate=_ONE)
    assert again == 0


@pytest.mark.unit
def test_unknown_window_is_a_noop() -> None:
    msgs = _convo()
    assert guard_window(msgs, budget_tokens=0, estimate=_ONE) == 0
    assert guard_window(msgs, budget_tokens=-1, estimate=_ONE) == 0
    assert msgs[2].content == "a" * _T


@pytest.mark.unit
def test_extra_tokens_count_toward_the_line() -> None:
    msgs = _convo()
    # Content 605 is below threshold; +100 system/etc. =705 exceeds 700 ⇒ target 560; one clear reaches exactly 560.
    assert guard_window(msgs, budget_tokens=700, extra_tokens=100, estimate=_ONE) == _T


@pytest.mark.unit
def test_estimate_messages_tokens_is_the_same_ruler() -> None:
    msgs = _convo()
    assert estimate_messages_tokens(msgs, estimate=_ONE) == 605


@pytest.mark.unit
def test_fast_path_counts_tool_call_arguments() -> None:
    """review 修：便宜的前置上界必须把 tool_calls 参数算进去，否则大参数能让守卫整个跳过。"""
    big_args = Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="t", arguments={"code": "x" * 400})])
    msgs = [Message(role="user", content="q"), big_args, _tool("r" * 100, 1), _asst(2), _tool("s" * 10, 2)]
    # Content totals 111 chars; args ~410. Threshold 450: a content-only precheck (111×4=444 ≤450) wrongly returns 0.
    assert guard_window(msgs, budget_tokens=450, estimate=_ONE) == 100


@pytest.mark.unit
def test_drop_history_pairs_drops_oldest_whole_pairs_to_the_hysteresis_line() -> None:
    hist = [Message(role="user", content="u" * 50), Message(role="assistant", content="a" * 50)] * 3  # 3 pairs, 100 each.
    msgs = [*hist, Message(role="user", content="task")]
    # Total 304; threshold 250 ⇒ target 200: drop one pair →204, still over; two →104, enough.
    dropped, tokens = drop_history_pairs(msgs, history_pairs=3, budget_tokens=250, estimate=_ONE)
    assert (dropped, tokens) == (2, 200)
    assert len(msgs) == 3 and msgs[-1].content == "task"


@pytest.mark.unit
def test_drop_history_pairs_never_touches_more_than_declared_pairs() -> None:
    hist = [Message(role="user", content="u" * 50), Message(role="assistant", content="a" * 50)]
    msgs = [*hist, Message(role="user", content="q" * 50), Message(role="assistant", content="x" * 50), Message(role="user", content="task")]
    dropped, _ = drop_history_pairs(msgs, history_pairs=1, budget_tokens=10, estimate=_ONE)
    assert dropped == 1 and [m.content[:1] for m in msgs] == ["q", "x", "t"]
    assert drop_history_pairs(msgs, history_pairs=0, budget_tokens=10, estimate=_ONE) == (0, 0)


@pytest.mark.unit
def test_tool_schema_dataclasses_are_counted_not_swallowed() -> None:
    """★ 第一版 `json.dumps(filtered_schemas)` 对 dataclass 抛 TypeError、兜成 0——「写着会算其实没算」。"""
    schemas = [ToolSchema(name="bi_query", description="查数", input_schema={"type": "object", "properties": {"q": {"type": "string"}}})]
    n = tool_schemas_tokens(schemas, estimate=_ONE)
    assert n > len("bi_query") + len("查数"), n
    assert tool_schemas_tokens([], estimate=_ONE) == 0
    assert tool_schemas_tokens([{"name": "x", "input_schema": {}}], estimate=_ONE) > 0


# ── Configuration validation ──


@pytest.mark.unit
def test_context_window_must_be_a_positive_int() -> None:
    _validate_provider_models("p", [{"id": "m", "context_window": 128_000}])
    for bad in (0, -1, True, "128k", 1.5):
        with pytest.raises(TeamLlmConfigError, match="context_window"):
            _validate_provider_models("p", [{"id": "m", "context_window": bad}])


@pytest.mark.unit
def test_provider_level_context_window_is_validated_too() -> None:
    ok = {"type": "openai_compatible", "base_url": "https://example.com/v1", "api_key": "k", "models": ["m"], "context_window": 128_000}
    _validate_provider("p", ok, ssrf_resolver=None)
    with pytest.raises(TeamLlmConfigError, match="providers.p.context_window"):
        _validate_provider("p", {**ok, "context_window": "big"}, ssrf_resolver=None)


@pytest.mark.unit
def test_context_windows_for_catalog_lists_every_declared_model_not_only_bound_targets() -> None:
    """review 修：user_pref 允许选目录里任何 chat 模型，只收绑定目标会让用户选的模型查不到窗口。"""
    config = {
        "providers": {
            "a": {"context_window": 128_000, "models": ["x", {"id": "y", "context_window": 32_000}, {"id": "unbound"}]},
            "b": {"models": ["z", {"id": "small", "context_window": 8_000}]},
        }
    }
    # "z" declares nothing: it gets the 131,072 default, so every catalog model has a window.
    assert context_windows_for_catalog(config) == {
        ("a", "x"): 128_000,
        ("a", "y"): 32_000,
        ("a", "unbound"): 128_000,
        ("b", "z"): 131_072,
        ("b", "small"): 8_000,
    }
    assert context_windows_for_catalog({"providers": None}) == {}


# ── Wiring through SubagentRunner ──


class _CaptureRouter(Router):
    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.calls = 0
        self.fail_first_with: ProviderFailureKind | None = None
        self.fail_first_chain: list[ProviderFailureKind] | None = None  # Multiple-target chain: target_index 1..n.

    def _first_call_failure(self) -> StreamingTargetsExhaustedError | None:
        self.calls += 1
        kinds = self.fail_first_chain or ([self.fail_first_with] if self.fail_first_with is not None else [])
        if self.calls != 1 or not kinds:
            return None
        # Peers now stream too; window overflow is outside the streaming-fallback allowlist, so StreamingTargetsExhausted reaches the loop directly.
        return StreamingTargetsExhaustedError(
            [
                LLMTargetFailure(
                    provider="p1",
                    model="mock",
                    provider_family="mock",
                    mode="stream",
                    target_index=i,
                    attempt=1,
                    kind=kind,
                    retryable=False,
                    status_code=400,
                )
                for i, kind in enumerate(kinds, start=1)
            ]
        )

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        exc = self._first_call_failure()
        if exc is not None:
            raise exc
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        exc = self._first_call_failure()
        if exc is not None:
            raise exc
        async for chunk in super().stream(agent_type, req, **kw):
            yield chunk


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    windows: dict | None,
    fail_first_with: ProviderFailureKind | None = None,
    fail_first_chain: list[ProviderFailureKind] | None = None,
    step_limit: int | None = None,
):
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))] * 3
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    router.fail_first_with = fail_first_with
    router.fail_first_chain = fail_first_chain
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    if windows:
        router.set_context_windows(windows)

    seen: list[int] = []

    def _spy(msgs, *, budget_tokens, extra_tokens=0, estimate=None, hysteresis=None, protect_first=0, cleared_ids=None):  # noqa: ANN001
        seen.append(budget_tokens)
        return 0 if (fail_first_with is None and fail_first_chain is None) else 123

    monkeypatch.setattr(ca, "guard_window", _spy)

    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    if step_limit is not None:
        from bowhead_core.agent_run_limits import AgentLoopBudget

        ctx.agent_runtime_budget = AgentLoopBudget(contract="agent-runtime-budget.v1", step_limit=step_limit)
    result = await runner.run(profile_name="compute", task="接线闸", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx)
    return router, seen, result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_budget_is_ninety_percent_of_the_declared_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automatic output reserves minimum progress, preserving the ordinary input window."""
    router, seen, _ = await _run(monkeypatch, windows={("p1", "mock"): 32_000})
    assert router.calls == 1
    expected = min(28_800, 32_000 - 1024 - 1024)
    assert seen and seen[0] == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_no_declared_window_uses_the_default_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner decision 2026-10-20: an undeclared window is bounded by the 131,072 default, not unlimited."""
    _, seen, _ = await _run(monkeypatch, windows=None)
    assert seen and seen[0] == min(int(131_072 * 0.9), 131_072 - 1024 - 1024)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_context_overflow_retries_exactly_once_after_shrinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游报 CONTEXT_OVERFLOW ⇒ 收 25% 再发一次；第二次成功。"""
    router, seen, result = await _run(monkeypatch, windows=None, fail_first_with=ProviderFailureKind.CONTEXT_OVERFLOW)
    assert router.calls == 2, "应重试一次"
    assert result.ok, result.error
    # The guard runs on each call against the default window, plus once to shrink before the retry.
    assert len(seen) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_other_400s_do_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _, result = await _run(monkeypatch, windows=None, fail_first_with=ProviderFailureKind.REQUEST_REJECTED)
    assert router.calls == 1
    assert not result.ok


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_overflow_on_the_last_iteration_does_not_retry_into_an_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the current decision controller for the last admissible model call."""
    router, _, result = await _run(monkeypatch, windows=None, fail_first_with=ProviderFailureKind.CONTEXT_OVERFLOW, step_limit=1)
    assert router.calls == 1, "An exhausted decision budget must not admit another request"
    assert not result.ok and not (result.final_answer or "").strip()


@pytest.mark.unit
def test_context_overflow_projects_to_its_own_reason_code() -> None:
    """正向验证：CONTEXT_OVERFLOW → reason_code llm_context_overflow（白名单 + _reason_for 两处都要在）。"""
    from bowhead_core.llm_failure import LLM_REASON_CODES, project_llm_failure

    exc = StreamingTargetsExhaustedError(
        [
            LLMTargetFailure(
                provider="p1",
                model="m",
                provider_family="mock",
                mode="stream",
                target_index=1,
                attempt=1,
                kind=ProviderFailureKind.CONTEXT_OVERFLOW,
                retryable=False,
                status_code=400,
            )
        ]
    )
    proj = project_llm_failure(exc)
    assert proj is not None and proj.reason_code == "llm_context_overflow"
    assert "llm_context_overflow" in LLM_REASON_CODES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_real_guard_drops_history_pairs_on_a_small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """不打桩的接线：窗口 64k（线 = 64000−8000−1024 = 54976，滞回目标 43980）、历史 3 对各 ≈24k token + 基线 ≈8k
    ⇒ 第二层掉最老的两对、留最新一对，发出去的请求里历史变少。"""
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    seen: list[list[str]] = []

    class _Cap(Router):
        async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
            seen.append([m.content or "" for m in req.messages])
            return await super().invoke(agent_type, req, **kw)

        async def stream(self, agent_type, req, **kw):  # type: ignore[override]
            seen.append([m.content or "" for m in req.messages])
            async for chunk in super().stream(agent_type, req, **kw):
                yield chunk

    router = _Cap(reg)
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    router.set_context_windows({("p1", "mock"): 64_000})
    big = "x " * 12_000  # ≈12k tokens; ≈24k per pair.
    hist = [m for i in range(3) for m in (Message(role="user", content=f"H{i} " + big), Message(role="assistant", content=f"A{i} " + big))]
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    await runner.run(profile_name="compute", task="TASK", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx, history=hist)
    assert seen, "没记到任何请求"
    sent = seen[0]
    kept_pairs = sum(1 for c in sent if c.startswith("H"))
    assert kept_pairs < 3, "历史一对没掉——第二层没接上"
    assert kept_pairs == 1 and any(c.startswith("H2") for c in sent), "该掉最老的整对、留最新的"
    assert "TASK" in sent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_fallback_overflow_after_primary_outage_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修二：主目标 5xx、备胎超窗 ⇒ 不能按备胎的窗口砍上下文重试——主模型装得下。"""
    router, _, result = await _run(monkeypatch, windows=None, fail_first_chain=[ProviderFailureKind.UPSTREAM, ProviderFailureKind.CONTEXT_OVERFLOW])
    assert router.calls == 1
    assert not result.ok


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_all_targets_overflow_still_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _, result = await _run(
        monkeypatch, windows=None, fail_first_chain=[ProviderFailureKind.CONTEXT_OVERFLOW, ProviderFailureKind.CONTEXT_OVERFLOW]
    )
    assert router.calls == 2 and result.ok


@pytest.mark.unit
def test_terminal_failure_prefers_the_primary_reason_over_a_fallback_overflow() -> None:
    """用户面：主模型不可用 + 备胎超窗 ⇒ 说「模型服务不可用」，不说「新开会话」（错误分类不能说谎）。"""
    from bowhead_core.llm_failure import project_llm_failure

    def _f(i: int, kind: ProviderFailureKind) -> LLMTargetFailure:
        return LLMTargetFailure(
            provider="p", model="m", provider_family="mock", mode="stream", target_index=i, attempt=1, kind=kind, retryable=False, status_code=400
        )

    mixed = project_llm_failure(StreamingTargetsExhaustedError([_f(1, ProviderFailureKind.UPSTREAM), _f(2, ProviderFailureKind.CONTEXT_OVERFLOW)]))
    assert mixed is not None and mixed.reason_code == "llm_provider_unavailable"
    both = project_llm_failure(
        StreamingTargetsExhaustedError([_f(1, ProviderFailureKind.CONTEXT_OVERFLOW), _f(2, ProviderFailureKind.CONTEXT_OVERFLOW)])
    )
    assert both is not None and both.reason_code == "llm_context_overflow"


@pytest.mark.unit
def test_guard_skips_results_shorter_than_the_marker() -> None:
    """review 修二：比占位还短的结果清了反而变大；守卫必须跳过它。"""
    msgs = [Message(role="user", content="q"), _asst(1), _tool("ok", 1), _asst(2), _tool("z" * 300, 2), _asst(3), _tool("new", 3)]
    cleared = guard_window(msgs, budget_tokens=200, estimate=_ONE)
    assert msgs[2].content == "ok", "短结果不该被换成更长的占位"
    assert cleared == 300 and is_cleared(msgs[4].content)


@pytest.mark.unit
def test_shrink_after_overflow_uses_the_exact_overage_when_numbers_are_known() -> None:
    """review 修二：模板给了 (maximum, requested) 时按准确超出量收，不再套 0.75 也不滞回——2k 的超出不该砍掉 52k。"""
    from bowhead_core.context_assembly import drop_history_pairs

    # 6 history pairs, 20 chars each =120; upstream maximum=110 / requested=120 ⇒10 over ⇒ threshold=120−10×1.05−110×0.05≈104.
    hist = [m for i in range(6) for m in (Message(role="user", content="u" * 10), Message(role="assistant", content="a" * 10))]
    msgs = [*hist]
    budget = 120 - int(10 * 1.05) - int(110 * 0.05)
    dropped, _ = drop_history_pairs(msgs, history_pairs=6, budget_tokens=budget, hysteresis=1.0, estimate=_ONE)
    assert dropped == 1, "准确超出量 10 ⇒ 掉一对（20）就够，不该掉到 75% 或 80%"
