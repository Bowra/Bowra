"""`_lead_loop` 起跑前的分段计时 `lead.prespawn_ms`。

116 实测（2026-09-24）：`turn.prespawn_ms` 只量到四个预取（3.2s），之后到 Lead 起跑还有 ~100s
没有任何计时。这行日志是下一次归因的唯一入口——段名缺了、顺序乱了，grep 就对不上。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult

_SEGMENTS = [
    "reference",
    "plan_propose",
    # f7510ff73 retired the keyword task-plan preflight and its "task_plan" segment.
    "facts_memory",
    "roster",
    "skill_index",
    "custom_skills",
    "prompt_blocks",
    "turn_history",
    "digests",
    "runner_setup",
]


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _Runner:
    calls: list[str] = field(default_factory=list)

    async def run(self, *, profile_name: str, **_kwargs: Any) -> SubagentResult:
        self.calls.append(profile_name)
        return SubagentResult(profile=profile_name, subagent_id="sub_test", ok=True, final_answer="ok")


class _EmptyCatalog:
    def list_names(self):
        return []

    def list_specs(self):
        return []

    def get(self, name):
        return None


@pytest.fixture(autouse=True)
def _lead_profile(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
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


@pytest.mark.asyncio
async def test_lead_logs_every_prespawn_segment_in_order(caplog: pytest.LogCaptureFixture) -> None:
    runner = _Runner()
    ctx = SimpleNamespace(
        clarification_store=None,
        _m137_subagent_payloads=[],
        tool_view=None,
        user_id="u1",
        team_id="t1",
        skill_instance_store=None,
        team_skill_store=None,
        granted_team_skill_ids=None,
        skill_catalog=_EmptyCatalog(),
        execution_generation=1,
        execution_owner_id=None,
    )
    runtime = LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        subagent_runner=runner,
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_prespawn", role="user")

    with caplog.at_level(logging.INFO, logger="bowhead_core.agents.lead_orchestrator"):
        await _lead_loop(sctx, "你好", runtime)

    assert runner.calls == ["lead"]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("lead.prespawn_ms turn=t_prespawn ")]
    assert len(lines) == 1, lines
    marks = re.findall(r" (\w+)=(\d+)", lines[0])
    assert marks[0][0] == "total"
    assert [name for name, _ in marks[1:]] == _SEGMENTS
    values = [int(ms) for _, ms in marks[1:]]
    assert values == sorted(values), "各段是自起点的累计墙钟，必须单调不减"
    assert int(marks[0][1]) >= values[-1]
