"""批5 phase2 — 收回 Lead 自调 `subagent_return` 的权（2026-08-21 转正，无 flag）。

取证（116 生产 30 天，2026-08-20）：Lead 自调该工具 **3 次全被 R7 拒**，零成功；
同期 225 个出了 final_answer 的 turn 里只有那 3 个调过它 ⇒ **Lead 收尾靠散文
（空 tool_calls），不依赖这个终端工具**，收权不会掐断收尾路径。

判据纪律（取证时踩过两个坑，写进测试防复发）：
- Lead 的 subagent_id **也是 `sub_` 前缀**，不能靠前缀区分 Lead / peer；
- 「本 turn 有无 spawn」也不能当判据——计划执行轮的专家腿走 plan_driver 不经
  `subagent_spawn`，会把 peer 的 return 误算成 Lead 的（初版 240 例假阳全是计划轮）。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import _LEAD_TOOL_DENY


def test_deny_set_is_exactly_the_two_names() -> None:
    """转正后 deny 集是常量，**写完整集合**而不是「包含某个名字」。

    子集断言在这里是危险的：交权与收权共用这一个常量，写成 `in` 的话删掉其中
    一个名字不会红。转正前这一族是 4 条参数化用例（两个 flag 的 2×2），flag 没了
    之后那 4 条就只剩这一个真实取值。
    """
    assert frozenset({"bi_query", "subagent_return"}) == _LEAD_TOOL_DENY


# ─────────────────────────────────────────────────────────────
# Through the gate: deny removes tools from Lead advertising + dispatch (paired positive controls)
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lead_loop_wires_deny_set_to_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """★review F-3：其他用例都**显式传 deny**，绕开了唯一的真接线
    （`_LEAD_TOOL_DENY` → `runner.run(tool_deny=)`）——把装配点改成别的字面量
    它们照样全绿。这条穿 `_lead_loop` 真实编排，钉住那条线。"""
    from dataclasses import dataclass, field
    from types import SimpleNamespace

    from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
    from bowhead_core.agents.turn_context import SerializableTurnContext
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult

    @dataclass
    class _Capt:
        kwargs: list[dict] = field(default_factory=list)

        async def run(self, *, profile_name: str, **kw: Any) -> SubagentResult:
            self.kwargs.append(dict(kw))
            return SubagentResult(profile=profile_name, subagent_id="s", ok=True, final_answer="ok")

    class _Bus:
        def __init__(self) -> None:
            self._pi_turn_state: dict = {}

        async def emit(self, ev: Any) -> None:
            return None

    def _rt(runner: Any) -> LeadRuntime:
        return LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=_Bus(),
            parent_ctx=SimpleNamespace(
                clarification_store=None, _m137_subagent_payloads=[], tool_view=None, execution_generation=1, execution_owner_id=None
            ),
            subagent_runner=runner,
        )

    def _sctx(tid: str) -> SerializableTurnContext:
        return SerializableTurnContext(user_id="u", workspace_id="ws", session_id="s", turn_id=tid, role="user")

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    prev = PROFILES.get("lead")
    if prev is None:
        PROFILES["lead"] = SubagentProfile(
            name="lead", llm_agent_type="lead", tool_whitelist=("subagent_return",), system_prompt="(stub)", max_iterations=3
        )
    try:
        # ⚠ History (review #2 F-2): assembly helper defaulted `return_revoked` to None,
        # falling back internally to env. Omitting the argument equaled passing it, so this test was blind
        # to argument removal; production once omitted it while all tests passed. Release made deny constant,
        # removing the omission shape, but wiring still needs a guard: changing the assembly set must fail.
        r_on = _Capt()
        assert await _lead_loop(_sctx("t_rv_on"), "问题", _rt(r_on)) is not None
        assert r_on.kwargs[0].get("tool_deny") == frozenset({"bi_query", "subagent_return"})
    finally:
        if prev is None:
            PROFILES.pop("lead", None)


@pytest.mark.asyncio
async def test_revoke_strips_return_from_profile_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """deny 作用在 profile 白名单层（广告面 / per-call 闸 / include_tool 三个消费点
    的共同上游）——直接观测 `_legacy_run` 用的那份 whitelist。

    ⚠ 不用「LLM 收到的 tools 列表」当判据：`subagent_return` 与 `subagent_spawn`
    在裸 ctx（无 role）下本来就会被**角色过滤**剔出广告面 —— 初版这么写时，
    正对照立刻证伪了它（OFF 路那条也看不到该工具）＝会得到一个「为错误理由绿」
    的测试。留此注释防复发。
    """
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    from bowhead_router import (
        AgentBinding,
        LLMResponse,
        MockAdapter,
        ProviderConfig,
        ProviderRegistry,
        Router,
    )

    def _router() -> Router:
        a = MockAdapter(provider_name="p1")
        a.scripted_responses = [LLMResponse(content="直接回答", tool_calls=[])]
        reg = ProviderRegistry()
        reg.load([ProviderConfig(name="p1", type="mock")])
        reg._adapters["p1"] = a  # type: ignore[attr-defined]
        r = Router(reg)
        r.bind(AgentBinding(agent_type="lead", primary=("p1", "mock")))
        return r

    seen_whitelists: list[tuple[str, ...]] = []
    import bowhead_core.subagent as sub_mod

    orig_effective = sub_mod.effective_tool_names

    def _spy(view: Any, whitelist: Any, **kw: Any) -> Any:
        seen_whitelists.append(tuple(whitelist))
        return orig_effective(view, whitelist, **kw)

    monkeypatch.setattr(sub_mod, "effective_tool_names", _spy)

    prev = PROFILES.get("lead")
    PROFILES["lead"] = SubagentProfile(
        name="lead",
        llm_agent_type="lead",
        tool_whitelist=("subagent_spawn", "subagent_return", "bi_schema"),
        system_prompt="(stub)",
        max_iterations=3,
    )
    try:
        ctx = ToolExecutionContext(workspace_id="1", member_uid="m", user_id="u", turn_id="t_rev", datarc=None)
        await SubagentRunner(router=_router(), tool_registry=build_default_registry()).run(
            profile_name="lead",
            task="q",
            parent_turn_id="t_rev",
            parent_bus=TurnEventBus(),
            parent_ctx=ctx,
            tool_deny=frozenset({"subagent_return"}),
        )
        assert seen_whitelists, "effective_tool_names 未被调用 — harness 断在更上游"
        on_wl = seen_whitelists[0]
        assert "subagent_return" not in on_wl
        assert "subagent_spawn" in on_wl and "bi_schema" in on_wl  # Others unaffected

        seen_whitelists.clear()
        await SubagentRunner(router=_router(), tool_registry=build_default_registry()).run(
            profile_name="lead",
            task="q",
            parent_turn_id="t_rev2",
            parent_bus=TurnEventBus(),
            parent_ctx=ctx,
        )
        assert seen_whitelists and "subagent_return" in seen_whitelists[0]  # Positive control
    finally:
        if prev is not None:
            PROFILES["lead"] = prev
        else:
            PROFILES.pop("lead", None)
