"""turn-latency A —— 每次 LLM 调用的 prompt 形状日志（**纯读**）。

用途：手上只有调用**之后**的聚合计数（`tokens_in`/`tokens_cached`），
看不见实际发出去的是什么，两个谜团因此卡在推测上（我已经猜错三次）：
① 第二次调用的 prompt 比第一次小约 3.4k，代码里找不到缩水机制；
② 首次调用有时 0% 命中、有时 45–49%，而 0% 的恰是最大的那些。

★★ 本文件最重要的一条是 `test_shape_log_does_not_change_the_request` ——
**观测器自己也要有正对照**。往 prompt 里塞 trace_id/时间戳来做关联，会每次都打断
前缀匹配、等于把 provider 缓存关掉；这条闸钉死「加了日志 = 请求一个字节都不变」。
"""

from __future__ import annotations

import logging

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

# Unique task string used to prove logs **do not** leak prompt content.
SECRET_MARKER = "ZZ_TASK_MARKER_9f3a_ZZ"


class _RecordingRouter(Router):
    """记下每次 invoke/stream 收到的 LLMRequest，供逐字对拍。"""

    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self.seen.append((req.system or "", tuple((m.content or "") for m in req.messages)))
        return await super().invoke(agent_type, req, **kw)


def _router() -> _RecordingRouter:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=100, output_tokens=50, cached_tokens=30))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = _RecordingRouter(reg)
    for t in ("lead", "analyst", "echo"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


async def _run(monkeypatch, *, enabled: bool) -> _RecordingRouter:
    monkeypatch.setenv("BOWHEAD_LLM_SHAPE_LOG", "1" if enabled else "0")
    router = _router()
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
    )
    await runner.run(
        profile_name="compute",
        task=f"形状日志契约测试 {SECRET_MARKER}",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    return router


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shape_log_does_not_change_the_request(monkeypatch) -> None:
    """★★ 观测器必须是被动的：开关开与关，发出去的 system + messages **逐字相同**。

    这是本次改动的核心风险闸——往 prompt 里加任何关联标记都会打断前缀缓存。
    突变：在 `_system_aug` 后面拼一个 trace 标记 ⇒ 本条红。
    """
    on = await _run(monkeypatch, enabled=True)
    off = await _run(monkeypatch, enabled=False)

    assert on.seen, "没记到任何请求，闸是死的"
    assert on.seen == off.seen, "加了形状日志之后请求变了——前缀缓存会被打断"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shape_log_is_emitted_with_expected_fields(monkeypatch, caplog) -> None:
    """开着的时候必须真的打出来，且带齐定位所需的字段。"""
    with caplog.at_level(logging.INFO, logger="bowhead_core.subagent"):
        await _run(monkeypatch, enabled=True)

    lines = [r.getMessage() for r in caplog.records if "llm_call.shape" in r.getMessage()]
    assert lines, "形状日志没打出来（功能是死的）"
    line = lines[0]
    for field in ("sys_len=", "sys_sha8=", "sys_head_sha8=", "n_msgs=", "msgs_len=", "msg0_sha8=", "tools_n="):
        assert field in line, f"缺字段 {field}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shape_log_leaks_no_prompt_content(monkeypatch, caplog) -> None:
    """★正对照：只记长度与哈希，**不许**出现 prompt 里的任何内容。

    没有它，把日志写成 `sys=%s` 打全文也能让上一条过。
    """
    with caplog.at_level(logging.INFO, logger="bowhead_core.subagent"):
        await _run(monkeypatch, enabled=True)

    lines = [r.getMessage() for r in caplog.records if "llm_call.shape" in r.getMessage()]
    assert lines
    for line in lines:
        assert SECRET_MARKER not in line, "形状日志泄漏了 messages 内容"
        # ★ Content-independent criterion: shape logs contain only lengths and hashes, so lines must be short.
        # The marker exists only in the task (messages), so it **cannot detect full system-prompt logging**.
        # That happened in the first version: replacing `_h(_system_aug)` with `_system_aug` logged the full text,
        # while all four tests passed. The criterion used the wrong proxy (another occurrence in this sprint).
        # The line-length limit is independent of specific paragraphs; logging any full paragraph exceeds it.
        assert len(line) < 400, f"形状日志过长（{len(line)} 字符）——像是把 prompt 打进去了"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shape_log_can_be_turned_off(monkeypatch, caplog) -> None:
    """回滚绳：`BOWHEAD_LLM_SHAPE_LOG=0` 必须完全静默。"""
    with caplog.at_level(logging.INFO, logger="bowhead_core.subagent"):
        await _run(monkeypatch, enabled=False)

    assert not [r for r in caplog.records if "llm_call.shape" in r.getMessage()], "关不掉"
