"""多语言批2：语言段的**覆盖面**与**唯一性**。

tech-design §4.3。两条判据，都必须穿过真的 `_legacy_run`（组装 system 的唯一现场）：

1. **覆盖面**：自定义 subagent 也要拿到语言段。它在 `_resolve_profile` 里手工造
   `SubagentProfile`、绕开 `manifest_to_profile` ⇒ `_LANGUAGE_RULE` 此前对它整条不存在。
2. **唯一性**：有 `language_context` 时，方向相反的两条规则不许同时在场
   （「跟随提问语言」vs 末尾「用设置语言回答」——116 G2 混排的提示词侧根因）。

⚠ 判据取 `router.systems[0]`（真发出去的 `req.system`），不是我们自己拼一遍再断言
——重建的输入不是证据。
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any

import pytest
from bowhead_core.agents.agent_runner_adapter import _LANGUAGE_RULE
from bowhead_core.custom_subagent_store import CustomSubagent
from bowhead_core.events import TurnEventBus
from bowhead_core.language_context import LanguageContext
from bowhead_core.subagent import PROFILES, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_core import subagent as subagent_mod
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)


@contextmanager
def _builtin_profile(name: str) -> Any:
    """把一个**按当前 flag 现建**的内置 profile 塞进 PROFILES。

    没有 boot 时 `PROFILES` 只有 `compute`，`runner.run("analyst")` 直接解析不到、
    连 router 都到不了（第一版就是这么假绿的）。这里现调 `manifest_to_profile` ——
    它正是批2 要验的那个决策点，必须在 flag 已设之后调用。
    """
    from bowhead_core.agents import AgentRegistry
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile

    registry = AgentRegistry.bootstrap()
    prev = PROFILES.get(name)
    PROFILES[name] = manifest_to_profile(registry.get(name, workspace_id="ws_lang_cov"))
    try:
        yield
    finally:
        if prev is not None:
            PROFILES[name] = prev
        else:
            PROFILES.pop(name, None)


class _RecordingRouter(Router):
    """记下每次请求**模型真正看得见的全部文本**（system + messages）。

    ⚠ 上下文工程 sprint 批 2-a 之后，语言段不再拼进 `system`，而是与 refs 清单一样
    挂在 **messages 末尾**（只进请求拷贝）。不变量是「模型看得见语言段」，
    不是「它在 system 字符串里」—— 只记 `req.system` 会把这条闸变成假红。
    """

    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.systems: list[str] = []

    def _record(self, req: Any) -> None:
        parts = [req.system or ""]
        parts.extend((getattr(m, "content", "") or "") for m in (req.messages or []))
        self.systems.append("\n\n".join(p for p in parts if p))

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self._record(req)
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self._record(req)
        return await super().stream(agent_type, req, **kw)


def _router() -> _RecordingRouter:
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [
        LLMResponse(content="收尾直答", usage=TokenUsage(input_tokens=10, output_tokens=5, cached_tokens=0)),
    ]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _RecordingRouter(reg)
    for tier in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=tier, primary=("p1", "mock")))
    return router


class _FakeCustomStore:
    """只实现 `_resolve_profile` 真正调的那一个方法。"""

    def __init__(self, agent: CustomSubagent) -> None:
        self._agent = agent

    async def list_for_user(self, user_id: str) -> list[CustomSubagent]:
        return [self._agent] if user_id else []


def _custom_agent() -> CustomSubagent:
    return CustomSubagent(
        id="csa_1",
        user_id="u",
        name="my-analyst",
        display_name="我的分析师",
        description="d",
        system_prompt="你是一个自定义分析师。",
        allowed_tools=["kb_search"],
    )


def _ctx(*, language_context: LanguageContext | None) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t_lang_cov",
        datarc=None,  # type: ignore[arg-type]
        session_id="s_lang_cov",
    )
    ctx.language_context = language_context  # type: ignore[attr-defined]
    return ctx


async def _run_and_capture(
    monkeypatch: Any,
    *,
    flag_on: bool,
    profile_name: str,
    language_context: LanguageContext | None,
    custom: bool = False,
) -> str:
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1" if flag_on else "0")
    router = _router()
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    if custom:
        runner.custom_store = _FakeCustomStore(_custom_agent())  # type: ignore[attr-defined]

    async def _stub_exec(tc: Any, *args: Any, **kwargs: Any) -> Any:
        return subagent_mod.ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result={"ok": True}, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    stack = nullcontext() if custom else _builtin_profile(profile_name)
    with stack:
        await runner.run(
            profile_name=profile_name,
            task="任务",
            parent_turn_id="t_lang_cov",
            parent_bus=TurnEventBus(),
            parent_ctx=_ctx(language_context=language_context),
        )
    assert router.systems, "没有捕获到任何请求——判据是死的"
    return router.systems[0]


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_for_builtin(monkeypatch: Any) -> None:
    """回滚绳：flag 关时内置 peer 仍从 manifest 那条路拿到规则，一字不动。"""
    system = await _run_and_capture(monkeypatch, flag_on=False, profile_name="business-analyst", language_context=None)
    assert _LANGUAGE_RULE in system


@pytest.mark.asyncio
async def test_flag_off_custom_subagent_still_has_no_rule(monkeypatch: Any) -> None:
    """★ 存量缺口的**正对照**：flag 关时自定义 subagent 拿不到任何语言规则。

    这条今天是绿的（= 缺口存在）。它存在的意义是：下一条测试证明 flag 开之后缺口补上了，
    而不是"两条都绿说明本来就没问题"。
    """
    system = await _run_and_capture(
        monkeypatch,
        flag_on=False,
        profile_name="my-analyst",
        language_context=None,
        custom=True,
    )
    assert _LANGUAGE_RULE not in system


@pytest.mark.asyncio
async def test_flag_on_covers_custom_subagent(monkeypatch: Any) -> None:
    """覆盖面闸：flag 开 + 无 language_context ⇒ 自定义 subagent 也拿到兜底规则。"""
    system = await _run_and_capture(
        monkeypatch,
        flag_on=True,
        profile_name="my-analyst",
        language_context=None,
        custom=True,
    )
    assert _LANGUAGE_RULE in system


@pytest.mark.asyncio
async def test_flag_on_with_context_keeps_exactly_one_language_voice(monkeypatch: Any) -> None:
    """唯一性闸：有 language_context 时只留末尾 directive，不许再挂方向相反的那条。"""
    ctx_lang = LanguageContext(ui_language="en-US", output_language="en-US")
    system = await _run_and_capture(
        monkeypatch,
        flag_on=True,
        profile_name="business-analyst",
        language_context=ctx_lang,
        custom=False,
    )
    assert "output_language=en-US" in system, "末尾语言指令段没出来"
    assert _LANGUAGE_RULE not in system, "「跟随提问语言」与「用设置语言」同时在场——正是 G2 混排的提示词侧根因"


@pytest.mark.asyncio
async def test_flag_off_with_context_still_has_both(monkeypatch: Any) -> None:
    """把「今天就是两条同时在场」钉成事实——否则上一条闸可能只是在测一个从不发生的状态。"""
    ctx_lang = LanguageContext(ui_language="en-US", output_language="en-US")
    system = await _run_and_capture(
        monkeypatch,
        flag_on=False,
        profile_name="business-analyst",
        language_context=ctx_lang,
        custom=False,
    )
    assert "output_language=en-US" in system
    assert _LANGUAGE_RULE in system
