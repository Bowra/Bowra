"""M158 paradigm v2 · ToolRegistry role-based enforce 单测.

设计文档 docs/M158-paradigm-shift-lead-as-manager.md §7.
现 prod 行为 (ctx.role=None) 不变, paradigm v2 ctx.role="lead" 时 BI 工具被拒.
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_registry import (
    _DEFAULT_ALLOWED_ROLES,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)
from bowhead_router.types import ToolSchema


def _ctx(role: str | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m_test",
        user_id="u_test",
        turn_id="t_test",
        datarc=None,  # type: ignore[arg-type]  # The tool handler does not actually call it.
        role=role,
    )


async def _noop_handler(args: dict, ctx: ToolExecutionContext) -> dict:
    return {"ok": True, "echo": args}


@pytest.mark.asyncio
async def test_default_role_None_skips_enforce():
    """ctx.role=None (现 prod 默认) → 任何工具都能调, 行为不变."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="bi.test", description="t", input_schema={"type": "object"}),
            handler=_noop_handler,
            allowed_for_roles=frozenset({"analyst"}),  # paradigm v2: BI for analyst only.
        )
    )
    inv = await reg.execute("bi.test", {"x": 1}, _ctx(role=None))
    assert inv.error is None  # role=None skips enforcement.
    assert inv.result == {"ok": True, "echo": {"x": 1}}


@pytest.mark.asyncio
async def test_lead_role_blocked_from_bi_tool():
    """paradigm v2: ctx.role='lead' 调 BI 工具 → ToolPermissionError 写入 invocation.error."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="bi.test", description="t", input_schema={"type": "object"}),
            handler=_noop_handler,
            allowed_for_roles=frozenset({"analyst"}),
        )
    )
    inv = await reg.execute("bi.test", {"x": 1}, _ctx(role="lead"))
    assert inv.error is not None
    assert "ToolPermissionError" in inv.error
    assert "role='lead'" in inv.error
    assert "subagent_spawn" in inv.error  # Hint for the LLM.
    # The handler must not be called.
    assert inv.result is None


@pytest.mark.asyncio
async def test_analyst_role_can_call_bi_tool():
    """paradigm v2: ctx.role='analyst' 调 BI 工具 → 通过."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="bi.test", description="t", input_schema={"type": "object"}),
            handler=_noop_handler,
            allowed_for_roles=frozenset({"analyst"}),
        )
    )
    inv = await reg.execute("bi.test", {"x": 1}, _ctx(role="analyst"))
    assert inv.error is None
    assert inv.result == {"ok": True, "echo": {"x": 1}}


@pytest.mark.asyncio
async def test_lead_role_can_call_spawn():
    """paradigm v2: ctx.role='lead' 调 subagent.spawn → 通过 (lead 独占)."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="subagent_spawn", description="t", input_schema={"type": "object"}),
            handler=_noop_handler,
            allowed_for_roles=frozenset({"lead"}),
        )
    )
    inv = await reg.execute("subagent_spawn", {"task": "x"}, _ctx(role="lead"))
    assert inv.error is None


@pytest.mark.asyncio
async def test_default_allowed_roles_share_all():
    """ToolSpec 默认 allowed_for_roles 含 lead/analyst/composer 全部 — 向后兼容."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="shared.tool", description="t", input_schema={"type": "object"}),
            handler=_noop_handler,
            # Omit allowed_for_roles → use the default.
        )
    )
    spec = reg.get("shared.tool")
    assert spec.allowed_for_roles == _DEFAULT_ALLOWED_ROLES
    # Any role can call (lead / analyst / composer are all in default).
    for role in ("lead", "analyst", "composer", None):
        inv = await reg.execute("shared.tool", {}, _ctx(role=role))
        assert inv.error is None, f"role={role} should pass default"
