"""CCA sprint block ③ — per-turn team tool view (`ctx.tool_view`).

Test-first reachability for the seam that the cca PRD/tech-design reviews
(r1→r2→d1→d2) repeatedly caught me mis-grounding. The contract under test:

1. `ToolRegistry.clone_with_overlay(extra_specs)` returns a per-turn VIEW
   (native ∪ overlay) that NEVER mutates the shared base singleton — overlay
   tools must not leak into the global registry, and an overlay spec that
   shadows a native name must override only inside the view.
2. `SubagentRunner._execute_tool` resolves dispatch from the IN-SCOPE
   `ctx.tool_view` (falling back to `self.tool_registry`). This is the point
   d1/d2 missed: the subagent's real `teammcp.*` dispatch happens here, not
   only in the `_legacy_run` filter. With a view set, an `teammcp.*` tool is
   reachable; with no view, it falls back to native → "not registered".
3. `_effective_tool_whitelist` augments the static profile whitelist with the
   team-overlay tool names — computed by SET DIFFERENCE (view − base), NOT a
   `mcp.*`/`teammcp.*` prefix scan (review F2: a prefix scan would leak GLOBAL
   stdio `mcp.*` tools across tenants). tool_view=None → overlay empty → no
   behavior change for existing turns.

Per `feedback_ut_must_assert_emit_contract` + the sprint's iron rule
("那个反复踩坑的 seam test-first 写 reachability 测试先证").
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSchema,
    ToolSpec,
    _effective_tool_whitelist,  # canonical home (subagent only re-exports it)
)

from bowhead_router import ToolCall


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
def _spec(name: str, handler: Any) -> ToolSpec:
    return ToolSpec(
        schema=ToolSchema(name=name, description="", input_schema={}),
        handler=handler,
    )


async def _native_handler(args: dict, ctx: Any) -> Any:
    return {"src": "native"}


async def _team_handler(args: dict, ctx: Any) -> Any:
    return {"src": "team", "args": args}


def _base_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_spec("bi_query", _native_handler))
    return reg


def _stub_runner(base: ToolRegistry) -> SubagentRunner:
    class _StubRouter:
        async def invoke(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

        def stream(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

        async def close(self) -> None:
            pass

    return SubagentRunner(router=_StubRouter(), tool_registry=base)  # type: ignore[arg-type]


def _ctx(*, tool_view: ToolRegistry | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        active_datarc_project="w",  # Batch 1/c3: seed the datarc workspace explicitly, without workspace_id fallback.
        member_uid="m",
        user_id="u",
        turn_id="t",
        # ws-datarc-decouple P3: non-None sentinel so the execute() datarc-capability gate
        # passes for the bi_query-named native stub. Handlers here don't touch datarc.
        datarc=object(),  # type: ignore[arg-type]
        role=None,  # paradigm-v2 off → isolate tool_view routing from role ACL
        tool_view=tool_view,
    )


# ─────────────────────────────────────────────────────────────
# 1. clone_with_overlay isolation
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_clone_with_overlay_combines_native_and_overlay() -> None:
    base = _base_registry()
    view = base.clone_with_overlay([_spec("teammcp.datarc.search", _team_handler)])

    assert view.has("bi_query")  # native carried over
    assert view.has("teammcp.datarc.search")  # overlay present in view


@pytest.mark.unit
def test_clone_with_overlay_does_not_mutate_base() -> None:
    base = _base_registry()
    base.clone_with_overlay([_spec("teammcp.datarc.search", _team_handler)])

    # overlay tool MUST NOT leak into the shared global registry (cross-tenant).
    assert not base.has("teammcp.datarc.search")
    assert base.list_names() == ["bi_query"]


@pytest.mark.unit
def test_clone_with_overlay_view_mutation_isolated_from_base() -> None:
    base = _base_registry()
    view = base.clone_with_overlay([_spec("teammcp.a", _team_handler)])

    # mutating the view's dict (e.g. a later overlay add) must not touch base.
    view.register(_spec("teammcp.b", _team_handler))
    assert view.has("teammcp.b")
    assert not base.has("teammcp.b")
    # ...and mutating base later must not retroactively appear in an old view.
    base.register(_spec("native_2", _native_handler))
    assert not view.has("native_2")


@pytest.mark.unit
def test_clone_with_overlay_shadow_name_overrides_only_in_view() -> None:
    base = _base_registry()
    shadow = _spec("bi_query", _team_handler)  # same name as native, diff handler
    view = base.clone_with_overlay([shadow])

    assert view.get("bi_query").handler is _team_handler  # view sees override
    assert base.get("bi_query").handler is _native_handler  # base untouched


# ─────────────────────────────────────────────────────────────
# 2. _execute_tool reachability via ctx.tool_view (the d1/d2-missed seam)
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_tool_reaches_overlay_when_tool_view_set() -> None:
    base = _base_registry()  # base has NO teammcp tool
    view = base.clone_with_overlay([_spec("teammcp.datarc.search", _team_handler)])
    runner = _stub_runner(base)
    ctx = _ctx(tool_view=view)
    bus = TurnEventBus()

    inv = await runner._execute_tool(
        ToolCall(id="c1", name="teammcp.datarc.search", arguments={"q": "x"}),
        ctx,
        bus,
        parent_turn_id="t",
        subagent_id="sub",
        running_profile="analyst",
    )

    # dispatch actually went through the VIEW at the execute() seam (:2468)
    assert inv.error is None
    assert inv.result == {"src": "team", "args": {"q": "x"}}


@pytest.mark.asyncio
async def test_execute_tool_falls_back_to_native_when_no_view() -> None:
    base = _base_registry()  # base has NO teammcp tool
    runner = _stub_runner(base)
    ctx = _ctx(tool_view=None)  # no per-turn view
    bus = TurnEventBus()

    inv = await runner._execute_tool(
        ToolCall(id="c2", name="teammcp.datarc.search", arguments={}),
        ctx,
        bus,
        parent_turn_id="t",
        subagent_id="sub",
        running_profile="analyst",
    )

    # no view → has() check (:2405) falls back to base → not registered
    assert inv.error is not None
    assert "not registered" in inv.error


@pytest.mark.asyncio
async def test_execute_tool_native_still_works_with_view_set() -> None:
    """View carries native tools too — a native call must still dispatch."""
    base = _base_registry()
    view = base.clone_with_overlay([_spec("teammcp.x", _team_handler)])
    runner = _stub_runner(base)
    ctx = _ctx(tool_view=view)
    bus = TurnEventBus()

    inv = await runner._execute_tool(
        ToolCall(id="c3", name="bi_query", arguments={}),
        ctx,
        bus,
        parent_turn_id="t",
        subagent_id="sub",
        running_profile="analyst",
    )
    assert inv.error is None
    assert inv.result == {"src": "native"}


# ─────────────────────────────────────────────────────────────
# 3. _effective_tool_whitelist — set-diff augmentation, NOT prefix scan
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_clone_with_overlay_stamps_overlay_names() -> None:
    base = _base_registry()
    assert base.overlay_names == frozenset()  # base singleton: no overlay
    view = base.clone_with_overlay(
        [
            _spec("teammcp.a", _team_handler),
            _spec("teammcp.b", _team_handler),
        ]
    )
    assert view.overlay_names == frozenset({"teammcp.a", "teammcp.b"})
    assert base.overlay_names == frozenset()  # base still untouched
    # nested overlay accumulates (defensive — union with inherited)
    view2 = view.clone_with_overlay([_spec("teammcp.c", _team_handler)])
    assert view2.overlay_names == frozenset({"teammcp.a", "teammcp.b", "teammcp.c"})


@pytest.mark.unit
def test_effective_whitelist_no_view_is_profile_only() -> None:
    base = _base_registry()
    # base registry → overlay_names empty → whitelist unchanged
    result = _effective_tool_whitelist(("bi_query", "skill_read"), base)
    assert result == {"bi_query", "skill_read"}


@pytest.mark.unit
def test_effective_whitelist_augments_with_overlay_names() -> None:
    base = _base_registry()
    view = base.clone_with_overlay(
        [
            _spec("teammcp.datarc.search", _team_handler),
            _spec("teammcp.datarc.fetch", _team_handler),
        ]
    )
    result = _effective_tool_whitelist(("bi_query",), view)
    assert result == {"bi_query", "teammcp.datarc.search", "teammcp.datarc.fetch"}


@pytest.mark.unit
def test_effective_whitelist_does_not_leak_global_mcp_via_prefix() -> None:
    """F2 cross-tenant guard: a GLOBAL stdio `mcp.*` tool already in the base
    registry must NOT be auto-whitelisted just because it shares an mcp prefix.
    Only names this team's view explicitly overlaid (view.overlay_names) are
    added — the global mcp.* tool is in base, not in the overlay stamp."""
    base = _base_registry()
    base.register(_spec("mcp.global_stdio.tool", _native_handler))  # global, not team
    # this team's view adds only its own overlay; the global mcp.* stays in base
    view = base.clone_with_overlay([_spec("teammcp.datarc.search", _team_handler)])

    result = _effective_tool_whitelist(("bi_query",), view)
    assert "teammcp.datarc.search" in result  # team overlay → allowed
    assert "mcp.global_stdio.tool" not in result  # global mcp.* → NOT auto-allowed


# ─────────────────────────────────────────────────────────────
# 4. (Removed) The langgraph backend's two allowlist gates
#
# Two former cases required subagent_graph llm_step / exec_tools to use the team overlay's
# expanded allowlist (otherwise langgraph silently dropped teammcp.*). The LangGraph backend retired in
# 2026-08 (Agent SDK investigation A2), removing these gates with their module; sections 1–3 above
# cover the legacy path, now the only execution path.
# ─────────────────────────────────────────────────────────────
