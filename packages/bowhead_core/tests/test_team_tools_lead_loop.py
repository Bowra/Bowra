"""Sprint team-tool-lead-visibility review P1: `_lead_loop` integration coverage.

The unit tests (test_peer_block.py) stop at `build_lead_system_prompt` /
`_gather_team_tool_entries` in isolation — nothing proved the LIVE injection
point: "a turn whose `parent_ctx.tool_view` carries a team overlay produces a
Lead prompt containing `<team_tools>`". Without this, a refactor that drops the
`team_tools=` kwarg (or reads the wrong runtime attr, or an except that
swallows everything) stays green while reproducing the exact silent-dark bug
this sprint fixed (116 E2E: Lead self-answers "no such tool"). Harness style
borrowed from test_b72_lead_bail_contract.py: fake bus + fake SubagentRunner
capturing ``extra_instructions`` (= the Lead system prompt).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult
from bowhead_core.tool_registry import ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _CapturingRunner:
    """Fake SubagentRunner that records the Lead system prompt it was handed."""

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


class _PoisonedView:
    """A tool_view whose overlay_names property raises — drives the derive
    try/except degradation branch in `_lead_loop`."""

    @property
    def overlay_names(self) -> frozenset[str]:
        raise RuntimeError("poisoned view")


async def _handler(args: Any, ctx: Any) -> dict[str, Any]:
    return {}


def _overlay_view() -> ToolRegistry:
    spec = ToolSpec(
        schema=ToolSchema(
            name="teammcp.ots_x.get_repo_info",
            description="查询 GitHub 仓库信息",
            input_schema={"type": "object"},
        ),
        handler=_handler,
    )
    return ToolRegistry().clone_with_overlay([spec])


def _mk_runtime(runner: _CapturingRunner, tool_view: Any) -> LeadRuntime:
    ctx = SimpleNamespace(
        clarification_store=None,
        _m137_subagent_payloads=[],
        tool_view=tool_view,
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
    # Keep the turn on the plain _lead_loop path (planning hook1 would divert
    # before the prompt build we're asserting on).
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")


@pytest.mark.asyncio
async def test_lead_loop_injects_team_tools_block_from_overlay_view() -> None:
    """LIVE injection point: overlay on parent_ctx.tool_view → the Lead system
    prompt handed to the runner contains <team_tools> with the tool name."""
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner, tool_view=_overlay_view())
    result = await _lead_loop(_mk_sctx("t_teamtools_1"), "查一下这个工具能查什么", runtime)
    assert result is not None
    assert runner.captured, "runner.run never reached — harness broke upstream of the prompt build"
    prompt = runner.captured[0] or ""
    assert "<team_tools" in prompt
    assert "teammcp.ots_x.get_repo_info" in prompt
    assert "查询 GitHub 仓库信息" in prompt


@pytest.mark.asyncio
async def test_lead_loop_derive_failure_degrades_without_block_or_crash() -> None:
    """Degradation branch: a poisoned tool_view (overlay_names raises) must NOT
    break the turn — the Lead prompt simply omits <team_tools> (pre-sprint
    prompt), and the runner still runs."""
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner, tool_view=_PoisonedView())
    result = await _lead_loop(_mk_sctx("t_teamtools_2"), "随便问点什么", runtime)
    assert result is not None
    assert runner.captured, "runner.run never reached — degradation must not abort the turn"
    prompt = runner.captured[0] or ""
    assert "<team_tools" not in prompt


@pytest.mark.asyncio
async def test_lead_loop_no_overlay_omits_block() -> None:
    """tool_view=None (every non-team turn) → no <team_tools> in the live path."""
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner, tool_view=None)
    result = await _lead_loop(_mk_sctx("t_teamtools_3"), "普通问题", runtime)
    assert result is not None
    assert runner.captured
    assert "<team_tools" not in (runner.captured[0] or "")
