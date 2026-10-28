"""LLM 用量计量 seam —— 所有 LLM 调用的唯一必经记账点。

## 为什么在这一层

token 用量此前是**每个调用点手工上报**：peer 靠 `task_runner` 约定返回 `(payload, tokens_in,
tokens_out)` 三元组、Lead 手工 `sum(_compose_usage)`、turn 收尾再由 gateway 汇总扣费。这是把
cross-cutting concern 手工 wire 到 N 处，忘穿一处就静默漏计——已实锤漏的：dreaming 记忆候选
（`bowhead_memory/candidates.py` 的 fire-and-forget `router.invoke`，源码注释自认「S2 记账」欠账）、
workflow distill、会话标题生成。这些调用烧了 token，但 `turns` 与 `credit_ledger` 两张表都不进。

`Router.invoke` / `Router.stream` 是**所有** LLM 调用的必经点，且响应自带 `TokenUsage`。在这里记一次，
新增调用点就不可能漏。

## 分层与安全约束

- 本模块**只定义 seam**，不知道数据库、团队、计费规则。上层（gateway 启动时）用
  :func:`set_usage_sink` 注册真正的落库实现；没注册时整条路径是 no-op。
- 记账**绝不能影响 LLM 调用本身**：sink 抛异常一律吞掉并降级为一条 WARNING。宁可丢一条用量记录，
  不能让记账把用户的 turn 弄挂。
- 记账 ≠ 扣费。本 seam 只回答「谁在什么上下文里烧了多少 token」；是否对某类用量扣 credit 是上层的
  产品决策（例如系统自己的记忆蒸馏是否该由用户付费）。这也是它不带 flag 的原因——没有「想要旧的
  漏计行为」的场景。

## 用法

```python
with usage_context(team_id="t_1", user_id="u_1", reason="dreaming"):
    await router.invoke("fast", req)   # 这次调用的用量自动带上上述归属
```

`usage_context` 用 :mod:`contextvars`，所以 `asyncio.create_task` 派生的子任务会继承当时的上下文，
不需要把归属信息层层穿参。未设上下文时仍会记录（`reason="unattributed"`），因为「有调用但归属不明」
本身就是要暴露的信号，比丢掉这条记录强。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

UNATTRIBUTED = "unattributed"


@dataclass(frozen=True, slots=True)
class UsageContext:
    """一次 LLM 调用的计费/归属上下文。字段全可空 —— 后台调用未必有 turn 或 workspace。"""

    team_id: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    turn_id: str | None = None
    #: **Why** this call happened (`turn` / `dreaming` / `workflow_distill` / `session_title` …).
    #: Distinguishes user-request-driven usage from spontaneous background usage, a key dimension for deciding whether to bill.
    reason: str = UNATTRIBUTED


@dataclass(frozen=True, slots=True)
class LLMUsageRecord:
    """一次成功 LLM 调用的用量记录。"""

    agent_type: str
    provider: str | None
    model: str | None
    tokens_input: int
    tokens_output: int
    tokens_cached: int
    streamed: bool
    context: UsageContext
    physical_recorded: bool = False

    @property
    def tokens_total(self) -> int:
        return self.tokens_input + self.tokens_output


UsageSink = Callable[[LLMUsageRecord], None]

#: Empty attribution when no context is set. A singleton: `UsageContext` is frozen and safe to share.
_EMPTY = UsageContext()
# default=None rather than `_EMPTY`: ruff B039 rejects nontrivial ContextVar defaults (it cannot determine
# that a frozen dataclass is immutable); `current_usage_context()` consistently falls back to `_EMPTY`.
_CURRENT: ContextVar[UsageContext | None] = ContextVar("bowhead_usage_context", default=None)
_TURN_SINK: ContextVar[UsageSink | None] = ContextVar(
    "bowhead_turn_usage_sink",
    default=None,
)
_SINK: UsageSink | None = None


def current_usage_context() -> UsageContext:
    """当前生效的归属上下文（未设置时返回全空的 `UsageContext`）。"""
    return _CURRENT.get() or _EMPTY


@contextmanager
def usage_context(
    *,
    team_id: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    turn_id: str | None = None,
    reason: str | None = None,
    turn_usage_sink: UsageSink | None = None,
) -> Iterator[UsageContext]:
    """在块内为所有 LLM 调用附加归属信息。

    只覆盖显式传入的字段，其余继承外层上下文 —— 这样 turn 里的子流程可以只改 `reason`
    （例如 `reason="session_title"`）而不必重复 team/user/turn。
    """
    base = current_usage_context()
    merged = replace(
        base,
        team_id=team_id if team_id is not None else base.team_id,
        user_id=user_id if user_id is not None else base.user_id,
        workspace_id=workspace_id if workspace_id is not None else base.workspace_id,
        turn_id=turn_id if turn_id is not None else base.turn_id,
        reason=reason if reason is not None else base.reason,
    )
    token = _CURRENT.set(merged)
    sink_token = _TURN_SINK.set(
        turn_usage_sink if turn_usage_sink is not None else _TURN_SINK.get(),
    )
    try:
        yield merged
    finally:
        _TURN_SINK.reset(sink_token)
        _CURRENT.reset(token)


@contextmanager
def detached_usage_context(*, reason: str) -> Iterator[UsageContext]:
    """Keep attribution while excluding an auxiliary task from root billing."""

    if not isinstance(reason, str) or not reason:
        raise ValueError("detached usage reason must be non-empty")
    merged = replace(current_usage_context(), reason=reason)
    token = _CURRENT.set(merged)
    sink_token = _TURN_SINK.set(None)
    try:
        yield merged
    finally:
        _TURN_SINK.reset(sink_token)
        _CURRENT.reset(token)


def set_usage_sink(sink: UsageSink | None) -> UsageSink | None:
    """注册（或用 `None` 卸载）用量落库实现。返回上一个 sink，便于测试还原。"""
    global _SINK
    previous = _SINK
    _SINK = sink
    return previous


# ── refusal sink（credential-readiness D1）───────────────────────────
# When RefusalAdapter rejects a call, report attribution (reason/turn) for gateway metrics.
# Like the usage sink, the router layer does not know the metrics implementation (the layering constraint
# forbids router depending on core). An unregistered sink is a no-op; sink exceptions are swallowed without affecting callers.

RefusalSink = Callable[[UsageContext], None]
_REFUSAL_SINK: RefusalSink | None = None


def set_refusal_sink(sink: RefusalSink | None) -> RefusalSink | None:
    """注册（或用 `None` 卸载）refusal 上报实现。返回上一个 sink，便于测试还原。"""
    global _REFUSAL_SINK
    previous = _REFUSAL_SINK
    _REFUSAL_SINK = sink
    return previous


def record_refusal() -> None:
    """记一次装配 Router 拒绝。**永不抛异常**（镜像 record_usage 的契约）。"""
    if _REFUSAL_SINK is None:
        return
    try:
        _REFUSAL_SINK(current_usage_context())
    except Exception as e:  # noqa: BLE001 — Reporting must never affect the caller.
        logger.warning("llm_refusal.sink_failed err=%s", e)


def _caller_fingerprint(*, _max_len: int = 60) -> str:
    """回溯栈，取第一个不属于本包的调用帧 → `模块.函数`。

    只在 reason 仍是 `unattributed` 时调用（相对低频），且**永不抛**——记账路径的
    任何异常都不该波及 LLM 调用本身。取不到就返回 `unknown`，那也比一个笼统的
    `unattributed` 多一点信息（至少说明「连栈都读不到」是另一类情况）。
    """

    try:
        import sys

        frame = sys._getframe(1)
        here = __name__.rsplit(".", 1)[0]  # bowhead_router
        depth = 0
        while frame is not None and depth < 40:
            mod = frame.f_globals.get("__name__") or ""
            if mod and not mod.startswith(here):
                return f"{mod.rsplit('.', 1)[-1]}.{frame.f_code.co_name}"[:_max_len]
            frame = frame.f_back
            depth += 1
    except Exception:  # pragma: no cover — Usage accounting never raises.
        logger.debug("usage.caller_fingerprint_failed", exc_info=True)
    return "unknown"


def record_usage(
    *,
    agent_type: str,
    provider: str | None,
    model: str | None,
    tokens_input: int,
    tokens_output: int,
    tokens_cached: int = 0,
    streamed: bool = False,
    owner_team_id: str | None = None,
    physical_recorded: bool = False,
) -> None:
    """记一次 LLM 用量。**永不抛异常** —— 记账失败不该波及 LLM 调用。

    `tokens_total == 0` 时直接跳过：mock adapter 与部分 provider 不回 usage，记一堆空行没有信息量。

    `owner_team_id`（credit gap23 步1）= **本次调用所用 router 的所属团队**，由
    `Router` 自带（`Router.owner_team_id`，唯一构造点 `TeamRouterRegistry` 盖章）。
    合流规则 **contextvar 优先、router 兜底**：有 `usage_context` 的路径（chat turn /
    dreaming）行为一字不变；没有的路径（定时任务、canvas 改写、KB 摄取等 20+ 入口）
    不再落成无主行。「算谁的钱」由此变成**结构保证**，调用点无需申报任何东西。
    """
    turn_sink = _TURN_SINK.get()
    if _SINK is None and turn_sink is None:
        return
    if (tokens_input or 0) <= 0 and (tokens_output or 0) <= 0:
        return
    try:
        ctx = current_usage_context()
        if not ctx.team_id and owner_team_id:
            # If contextvar omits the team (common across 20+ entry points), fall back to the router construction identity.
            ctx = replace(ctx, team_id=owner_team_id)
        if ctx.reason == UNATTRIBUTED:
            # Apply the same fallback to reason: **identify the caller ourselves** when no reason is declared.
            #
            # Why this is necessary (116 observed on 2026-08-10): the database had 286 `unattributed` rows (9% of all
            # usage), **still increasing daily**, all without workspace_id / turn_id: a blind spot for purely background
            # usage. Fixing it requires knowing the source, but it is **impossible to establish afterward**: one generic
            # `unattributed` bucket mixes 14 candidate call sites, leaving only guesswork.
            # The previous fix (Copilot draft routing) could be located only because someone happened to report specific symptoms;
            # the remaining cases have no such evidence.
            #
            # Recording the call site on each row turns the blind spot into a list. Preserve the `unattributed` prefix
            # so existing prefix-based aggregation stays unchanged (exact equality matches must become prefix matches).
            ctx = replace(ctx, reason=f"{UNATTRIBUTED}:{_caller_fingerprint()}")
        record = LLMUsageRecord(
            agent_type=agent_type,
            provider=provider,
            model=model,
            tokens_input=int(tokens_input or 0),
            tokens_output=int(tokens_output or 0),
            tokens_cached=int(tokens_cached or 0),
            streamed=streamed,
            context=ctx,
            physical_recorded=physical_recorded,
        )
        if turn_sink is not None:
            turn_sink(record)
        if _SINK is not None:
            _SINK(record)
    except Exception as e:  # noqa: BLE001 — Usage accounting must never affect the caller.
        logger.warning("llm_usage.sink_failed agent=%s provider=%s err=%s", agent_type, provider, e)
