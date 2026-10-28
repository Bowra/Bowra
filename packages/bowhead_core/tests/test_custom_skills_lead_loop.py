"""skill-audit A3 — `_lead_loop` 集成：<user_team_skills> 真注入.

镜像 test_team_tools_lead_loop.py（同款教训：单测只测 build_lead_system_prompt /
gather 孤立面，没人证明 LIVE 注入点——kwarg 被重构掉/except 吞掉时全绿复现
「Lead 零感知」原 bug）。fake runner 截 extra_instructions（= Lead system prompt）。
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


class _PoisonedCatalog:
    def list_names(self):
        raise RuntimeError("poisoned catalog")

    def list_specs(self):
        return []

    def get(self, name):
        return None


class _FakeTeamStore:
    def __init__(self, recs):
        self._recs = list(recs)

    async def list(self, team_id, only_enabled=True):
        return self._recs


def _mk_runtime(runner: _CapturingRunner, ctx_extra: dict[str, Any]) -> LeadRuntime:
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
    for k, v in ctx_extra.items():
        setattr(ctx, k, v)
    return LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        subagent_runner=runner,
    )


def _mk_sctx(turn_id: str) -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id=turn_id,
        role="user",
    )


@pytest.fixture(autouse=True)
def _ensure_lead_profile():
    pre = "lead" in PROFILES
    if not pre:
        PROFILES["lead"] = SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=("subagent_return", "clarification_ask"),
            system_prompt="(stub)",
            max_iterations=4,
        )
    try:
        yield
    finally:
        if not pre:
            PROFILES.pop("lead", None)


@pytest.fixture(autouse=True)
def _no_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")


@pytest.mark.asyncio
async def test_lead_loop_injects_custom_skills_block_from_ctx_stores() -> None:
    """LIVE 注入点：parent_ctx 的 team_skill_store 有 team_wide skill → runner 拿到的
    Lead prompt 含 <user_team_skills> + skill 名（视图现算，无缓存参与）。"""
    runner = _CapturingRunner()
    rec = SimpleNamespace(
        id="oskill_x",
        name="channel-margin-audit",
        description="渠道毛利核对流程",
        visibility="team_wide",
        created_by="creator",
    )
    runtime = _mk_runtime(runner, {"team_skill_store": _FakeTeamStore([rec])})
    result = await _lead_loop(_mk_sctx("t_cskills_1"), "核对一下渠道毛利", runtime)
    assert result is not None
    assert runner.captured, "runner.run never reached — harness broke upstream of the prompt build"
    prompt = runner.captured[0] or ""
    assert "<user_team_skills" in prompt
    assert "channel-margin-audit" in prompt
    assert "渠道毛利核对流程" in prompt


@pytest.mark.asyncio
async def test_lead_loop_gather_failure_degrades_without_block_or_crash() -> None:
    """降级分支：poisoned catalog（list_names raises）→ 无块、turn 照跑。"""
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner, {"skill_catalog": _PoisonedCatalog()})
    result = await _lead_loop(_mk_sctx("t_cskills_2"), "随便问点什么", runtime)
    assert result is not None
    assert runner.captured
    assert "<user_team_skills" not in (runner.captured[0] or "")


@pytest.mark.asyncio
async def test_lead_loop_no_custom_skills_omits_block() -> None:
    """无自建 skill（store 空/未接）→ 无块（逐字节零变化路径）。"""
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner, {})
    result = await _lead_loop(_mk_sctx("t_cskills_3"), "普通问题", runtime)
    assert result is not None
    assert runner.captured
    assert "<user_team_skills" not in (runner.captured[0] or "")
