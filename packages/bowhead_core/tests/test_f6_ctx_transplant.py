"""F6 follow-up 回归 — role-swap 浅拷贝丢 subagent_return payload stash。

真 bug（multimodal-v1 真 LLM dogfood 2026-06-05 抓到，引入于 9a96d32 2026-06-02）：
`_execute_tool` 在 role-swap（ctx.role 非 None 且 running_profile != "lead"）时
dispatch 到 `copy.copy(ctx)`；`subagent_return` 把 payload `setattr` 在副本上，
runner 的 `consume_returned_payload(parent_ctx)` 永远拿 None → 每个 peer 的
payload 返回退化成 subagent_empty_answer。修复 = 把契约属性 transplant 回共享 ctx。
"""

from __future__ import annotations

from typing import Any

from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.subagent_return_tool import CTX_PAYLOAD_ATTR, consume_returned_payload
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)
from bowhead_router.types import ToolCall, ToolSchema


def _stashing_tool_spec() -> ToolSpec:
    """最小复刻 subagent_return 的 stash 行为：handler 往 ctx 上新建契约属性。"""

    async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        setattr(ctx, CTX_PAYLOAD_ATTR, {"title": "t", "insights": [{"title": "i"}]})
        return {"ok": True}

    return ToolSpec(
        schema=ToolSchema(
            name="stub_return",
            description="stash payload like subagent_return",
            input_schema={"type": "object", "properties": {}},
        ),
        handler=_handler,
    )


def _ctx(role: str | None) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t_x",
        datarc=None,  # type: ignore[arg-type]  # The stub tool does not access datarc.
    )
    ctx.role = role  # Dynamic attribute (same as paradigm v2 injection).
    return ctx


async def _run_one(role: str | None, running_profile: str) -> ToolExecutionContext:
    registry = ToolRegistry()
    registry.register(_stashing_tool_spec())
    runner = SubagentRunner(router=None, tool_registry=registry)  # type: ignore[arg-type]
    parent_ctx = _ctx(role)
    bus = TurnEventBus(None)
    await runner._execute_tool(
        ToolCall(id="call_1", name="stub_return", arguments={}),
        parent_ctx,
        bus,
        "t_x",
        "sub_x",
        running_profile=running_profile,
    )
    return parent_ctx


async def test_payload_stash_survives_role_swap() -> None:
    """role-swap 路径（peer + role 非 None）→ stash 必须出现在共享 parent_ctx 上。"""
    parent_ctx = await _run_one(role="analyst", running_profile="marketing-analyst")
    payload = consume_returned_payload(parent_ctx)
    assert payload is not None, "role-swap 丢 payload stash（F6 浅拷贝回归）"
    assert payload["title"] == "t"
    # Keep role itself unmodified (preserve the original F6 goal).
    assert parent_ctx.role == "analyst"


async def test_payload_stash_no_swap_path_unchanged() -> None:
    """无 swap 路径（role None）→ 原行为不变。"""
    parent_ctx = await _run_one(role=None, running_profile="compute")
    assert consume_returned_payload(parent_ctx) is not None


async def test_payload_stash_lead_profile_no_swap() -> None:
    """running_profile=lead 不 swap（θ R1+R2 行为保持）。"""
    parent_ctx = await _run_one(role="lead", running_profile="lead")
    assert consume_returned_payload(parent_ctx) is not None
