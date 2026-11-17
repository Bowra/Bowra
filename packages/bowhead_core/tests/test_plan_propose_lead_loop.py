"""graph-mode — `_lead_loop` 集成：Auto turn 的 Lead prompt 真带「Step 0 要不要先出计划」。

镜像 test_custom_skills_lead_loop.py 的教训，而且这一条是**吃过亏才写的**：
`plan_propose` 早就在 lead.yaml 的 allow-list 里、registry 里注册着、role/visible 与
`clarification_ask` 逐字段相同，单测也全绿 —— 116 真机上 Lead 三轮一次都没调过，
因为它从没被写进 Lead 的 system prompt。**「段落函数会渲染」和「这个 turn 真收到它」
是两件事**，中间隔着 `_lead_loop` 这条缝；只测 build_lead_system_prompt 跨不过去
（kwarg 被重构掉 / 判据写成 `== "auto"` 时，孤立单测照样全绿）。

fake runner 截 extra_instructions（= Lead system prompt）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _CapturingRunner:
    captured: list[str | None] = field(default_factory=list)

    async def run(
        self,
        *,
        profile_name: str,
        task: str,
        parent_turn_id: str,
        parent_bus: Any,
        parent_ctx: Any,
        extra_instructions: str | None = None,
        **_kwargs: Any,
    ) -> SubagentResult:
        self.captured.append(extra_instructions)
        return SubagentResult(
            profile=profile_name,
            subagent_id="sub_test",
            ok=True,
            final_answer="ok",
        )


class _FakeCatalog:
    def list_names(self):
        return []

    def list_specs(self):
        return []

    def get(self, name):
        return None


def _mk_runtime(runner: _CapturingRunner) -> LeadRuntime:
    ctx = SimpleNamespace(
        clarification_store=None,
        _m137_subagent_payloads=[],
        tool_view=None,
        user_id="u1",
        team_id="t1",
        skill_instance_store=None,
        team_skill_store=None,
        granted_team_skill_ids=None,
        skill_catalog=_FakeCatalog(),
        execution_generation=1,
        execution_owner_id=None,
    )
    return LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        subagent_runner=runner,
    )


def _mk_sctx(turn_id: str, planning_mode: str | None) -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id=turn_id,
        role="user",
        planning_mode=planning_mode,
    )


@pytest.fixture(autouse=True)
def _ensure_lead_profile():
    pre = "lead" in PROFILES
    if not pre:
        PROFILES["lead"] = SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=("subagent_return", "plan_propose"),
            system_prompt="(stub)",
            max_iterations=4,
        )
    try:
        yield
    finally:
        if not pre:
            PROFILES.pop("lead", None)


@pytest.fixture(autouse=True)
def _planning_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable planning flag: exclude explicit planning's pre-loop direct path (it bypasses the prompt),
    # so all three modes reach the same prompt construction point, differing only in planning_auto.
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")


# ── Deterministic fallback: installing the gate ────────────────────────
# Dispatching ≥2 distinct peers in one round requires a plan first. Check `_lead_loop` installs the gate
# on parent_ctx with the correct mode; test_multi_peer_gate_loop.py exercises the hot-path lines.


async def _gate_after_turn(mode: str | None, turn_id: str):
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner)
    await _lead_loop(_mk_sctx(turn_id, mode), "做一份三方向尽调", runtime)
    return getattr(runtime.parent_ctx, "_round_tool_calls_gate", None)


@pytest.mark.asyncio
async def test_gate_installed_on_auto() -> None:
    """★Auto 的 wire 值是 None（FE 约定 auto 不上 wire）——主路必须装上。"""
    assert await _gate_after_turn(None, "t_gate_auto") is not None


@pytest.mark.asyncio
async def test_gate_installed_on_explicit_auto_string() -> None:
    assert await _gate_after_turn("auto", "t_gate_auto_str") is not None


_TWO_PEERS = [
    SimpleNamespace(name="subagent_spawn", arguments={"profile": "financial-analyst"}),
    SimpleNamespace(name="subagent_spawn", arguments={"profile": "business-analyst"}),
]
_PLAN_CALL = [SimpleNamespace(name="plan_propose", arguments={"reason": "three steps"})]


async def _gate_and_runtime(mode: str | None, turn_id: str):
    runtime = _mk_runtime(_CapturingRunner())
    await _lead_loop(_mk_sctx(turn_id, mode), "fill the template", runtime)
    return runtime.parent_ctx._round_tool_calls_gate, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "planning"])
async def test_outside_auto_a_batch_of_peers_is_not_asked_about_a_plan(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """off: the user said no plans. planning: the card path already ran before the loop."""
    from bowhead_core.agents import lead_orchestrator

    async def never(*_args: Any, **_kwargs: Any):
        raise AssertionError("the card path must not be asked")

    gate, _ = await _gate_and_runtime(mode, f"t_gate_{mode}")
    monkeypatch.setattr(lead_orchestrator, "_maybe_propose_plan", never)
    assert await gate(_TWO_PEERS, "lead") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "off", "planning"])
async def test_a_plan_call_that_yields_no_card_lets_the_run_continue_and_says_so(mode: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Lead's own plan_propose call is decided before it runs.

    The call used to end the run and ask the card path afterwards. When that path created no card the Lead
    had written nothing and the user got the empty-answer fallback (observed in a regression).
    Mutation: decide after the call (return the sentinel unconditionally) → this test fails.
    """
    from bowhead_core.agents import lead_orchestrator
    from bowhead_core.agents.plan_propose_tool import PLAN_NOT_CREATED, _plan_propose, is_plan_propose_sentinel

    asked: list[str] = []

    async def no_card(sctx: Any, *_args: Any, **_kwargs: Any):
        asked.append(sctx.turn_id)
        return

    gate, runtime = await _gate_and_runtime(mode, f"t_declined_{mode}")
    monkeypatch.setattr(lead_orchestrator, "_maybe_propose_plan", no_card)
    # A tool handler receives a shallow copy of the Turn context.
    import copy

    tool_ctx = copy.copy(runtime.parent_ctx)
    assert is_plan_propose_sentinel(await _plan_propose({"reason": "x"}, tool_ctx)), "undecided calls keep the old contract"
    assert await gate(_PLAN_CALL, "lead") is None
    assert await _plan_propose({"reason": "x"}, tool_ctx) == {"plan_created": False, "detail": PLAN_NOT_CREATED}
    # A second call in the same run repeats the fact without authoring a plan again.
    assert await gate(_PLAN_CALL, "lead") is None and len(asked) == 1
    assert await gate(_PLAN_CALL, "compute") is None and len(asked) == 1


@pytest.mark.asyncio
async def test_a_plan_call_that_yields_a_card_ends_the_run_with_that_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents import lead_orchestrator

    decided = SimpleNamespace(turn_id="t_card")

    async def card(*_args: Any, **_kwargs: Any):
        return decided

    gate, runtime = await _gate_and_runtime("off", "t_card")
    monkeypatch.setattr(lead_orchestrator, "_maybe_propose_plan", card)
    assert await gate(_PLAN_CALL, "lead") == {"_plan_gate_committed": True}
    assert runtime.parent_ctx._plan_gate_result is decided and runtime.parent_ctx._plan_propose_state == {"declined": False}


@pytest.mark.asyncio
async def test_installed_gate_declines_instead_of_aborting_when_not_a_project() -> None:
    """★**先决定后提交**：闸命中条件后先问出卡路径，它不出计划就**放行本轮**。

    这是 116/1.1.3 那个 P0 的回归钉。第一版是反的——命中就中止本轮、再去问
    「是不是项目」，答「不是」时模型选好的 tool_calls 已经被丢掉、turn 空着，
    用户拿到「本次没有产出可展示的内容」。
    本测的 runtime 没有 plan_store（`_maybe_propose_plan` 必返 None），
    所以「闸返回 None」= 本轮照常执行 —— 突变（命中就无条件返哨兵）→ 本条红。
    """
    gate = await _gate_after_turn(None, "t_gate_behaviour")
    assert gate is not None, "Auto 下必须装闸"
    two = [
        SimpleNamespace(name="subagent_spawn", arguments={"profile": "financial-analyst"}),
        SimpleNamespace(name="subagent_spawn", arguments={"profile": "business-analyst"}),
    ]
    assert await gate(two, "lead") is None, "出卡路径不出计划时必须放行，不能中止本轮"


@pytest.mark.asyncio
async def test_installed_gate_is_lead_only_and_needs_two_distinct_peers() -> None:
    """非 lead 与「不够两个不同 peer」都要在**问出卡路径之前**就短路掉。"""
    gate = await _gate_after_turn(None, "t_gate_shortcut")
    assert gate is not None
    two = [
        SimpleNamespace(name="subagent_spawn", arguments={"profile": "financial-analyst"}),
        SimpleNamespace(name="subagent_spawn", arguments={"profile": "business-analyst"}),
    ]
    assert await gate(two, "compute") is None, "非 lead 不许被拦"
    one = [SimpleNamespace(name="subagent_spawn", arguments={"profile": "compute"})]
    assert await gate(one, "lead") is None
