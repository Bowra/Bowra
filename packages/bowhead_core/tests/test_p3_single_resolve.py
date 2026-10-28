"""CCA P3 · S3.5 — 单次 resolve + legacy start 事件的身份字段。

本文件原本的主题是「single-resolve-before-backend」：`run()` 只 resolve 一次
SubagentProfile，把**对象**同时喂给 legacy 与 langgraph 两个 backend（此前 langgraph
那支自己回查 `PROFILES[name]`，团队子代理不在 PROFILES 里 → KeyError → 团队子代理在
`BOWHEAD_SUBAGENT_BACKEND=langgraph` 下不可达）。

LangGraph backend 已于 2026-08 下线（Agent SDK 调研 A2），"两个 backend 要一致" 这个
命题随之消失，原先针对 `_build_nodes` / `run_subagent_graph` / graph start 的用例测的是
**被删掉的能力**，一并移除（能力下线 ≠ 回归，见 feedback_moved_capability_vs_dropped_capability）。

留下的是仍然活着的那一半：legacy start 事件的 display_name 归一化。
"""

from __future__ import annotations

import asyncio
from typing import Any

from bowhead_core.subagent import SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry


def _run(coro):
    return asyncio.run(coro)


class _StubRouter:
    async def invoke(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    def stream(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class _StubBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _team_profile() -> SubagentProfile:
    return SubagentProfile(
        name="sales-expert",
        llm_agent_type="analyst",
        tool_whitelist=("bi_query",),
        system_prompt="be helpful",
        max_iterations=7,
        description="sales",
        team_tool_bundle=("teammcp.crm.getDeal",),
        display_name="  销售专家  ",
    )


def _ctx(team_id: str = "team_a") -> ToolExecutionContext:
    return ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None, team_id=team_id)  # type: ignore[arg-type]


def test_legacy_start_emits_trimmed_display_name_snapshot() -> None:
    """start 事件带归一化后的 display_name（两侧空白裁掉）。"""
    bus = _StubBus()
    runner = SubagentRunner(
        router=_StubRouter(),
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
    )
    _run(
        runner._legacy_run(
            profile_name="sales-expert",
            resolved_profile=_team_profile(),
            task="核对销售口径",
            parent_turn_id="pt",
            parent_bus=bus,  # type: ignore[arg-type]
            parent_ctx=_ctx(),
        )
    )
    spawn = next(e for e in bus.events if e.event_type == "agent_spawned" and "task" in e.payload)
    assert spawn.payload["display_name"] == "销售专家"


def test_legacy_start_omits_blank_display_name() -> None:
    """全空白的 display_name 不该占一个空字段位。"""
    profile = SubagentProfile(
        name="blank-name",
        llm_agent_type="analyst",
        tool_whitelist=(),
        system_prompt="x",
        display_name=" \t ",
    )
    bus = _StubBus()
    runner = SubagentRunner(
        router=_StubRouter(),
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
    )
    _run(
        runner._legacy_run(
            profile_name=profile.name,
            resolved_profile=profile,
            task="task",
            parent_turn_id="pt",
            parent_bus=bus,  # type: ignore[arg-type]
            parent_ctx=_ctx(),
        )
    )
    assert "display_name" not in bus.events[0].payload
