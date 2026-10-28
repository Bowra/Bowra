"""workspace-datarc-decouple P1 (R1.2) + S1: datarc-project resolution chokepoint.

Keystone seam: `ToolExecutionContext.active_datarc_project` + `resolve_datarc_project()`
+ the bi tool `_ws(ctx)` routing through it. Pinned behavior (批1/c3, workspace-space-split):
- a seeded/selected ``active_datarc_project`` is always used;
- with none, resolve == ``""`` — ``ctx.workspace_id`` (the Bowhead space id) NEVER stands in;
  the pre-canonical numeric escape hatch is retired (2026-07-07 116 scan: zero non-ws_* ids);
- a bi_* tool with a client but no datarc workspace refuses gracefully (steers to KB)
  instead of querying a bogus id.
"""

from __future__ import annotations

from typing import Any

from bowhead_core.tool_registry import (
    ToolExecutionContext,
    _tool_requires_datarc,
    build_default_registry,
)
from bowhead_core.user_memory import ProjectMemoryStore


def _ctx(*, workspace_id: str, active: str | None, datarc: Any = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id=workspace_id,
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=datarc,
        active_datarc_project=active,
    )


def test_resolve_non_canonical_workspace_never_stands_in() -> None:
    """批1/c3: a pre-canonical numeric workspace_id no longer stands in as its own datarc
    workspace — resolve == "" (the bi gate reports no-datarc-workspace)."""
    ctx = _ctx(workspace_id="1", active=None)
    assert ctx.resolve_datarc_project() == ""


def test_has_datarc_reflects_client_presence() -> None:
    """P3 capability signal: has_datarc() is True iff a datarc client is bound. Today the
    global env client is always bound → True (behavior-preserving); None (a P4 no-connection
    team) → False → BI tools/prompt gate off."""
    assert _ctx(workspace_id="1", active=None, datarc=object()).has_datarc() is True
    assert _ctx(workspace_id="1", active=None, datarc=None).has_datarc() is False


def test_resolve_prefers_active_datarc_project() -> None:
    """Decoupled: workspace_id is a Bowhead ws_*, the datarc project comes from
    active_datarc_project."""
    ctx = _ctx(workspace_id="ws_abc123", active="7")
    assert ctx.resolve_datarc_project() == "7"


def test_empty_active_project_resolves_empty() -> None:
    """Empty string (not just None) also means no datarc workspace — `or` semantics."""
    ctx = _ctx(workspace_id="1", active="")
    assert ctx.resolve_datarc_project() == ""


def test_ws_star_without_active_project_resolves_empty() -> None:
    """S1: a canonical Bowhead ``ws_*`` id is NEVER a datarc project — with no seeded project it
    resolves to "" (NOT the ws_*), so bi_* never queries a bogus id."""
    assert _ctx(workspace_id="ws_abc123", active=None).resolve_datarc_project() == ""
    assert _ctx(workspace_id="ws_abc123", active="").resolve_datarc_project() == ""


class _RecordingDatarc:
    """Minimal datarc stub recording the workspace_id passed to list_measures."""

    def __init__(self) -> None:
        self.measures_ws: str | None = None

    async def list_measures(self, workspace_id: str) -> list[Any]:
        self.measures_ws = workspace_id
        return []


async def test_bi_handler_uses_active_project_via_ws_chokepoint() -> None:
    """`_ws(ctx)` must route through resolve_datarc_project → a bi tool hits the
    active datarc project, NOT the (post-decouple ws_*) workspace_id."""
    dc = _RecordingDatarc()
    spec = build_default_registry().get("bi_list_measures")
    ctx = _ctx(workspace_id="ws_abc123", active="7", datarc=dc)
    await spec.handler({}, ctx)
    assert dc.measures_ws == "7"


async def test_bi_tool_refused_for_non_canonical_workspace() -> None:
    """批1/c3: numeric workspace_id no longer stands in — with no seeded datarc workspace the
    bi_* gate refuses and datarc is never queried with a bogus id."""
    dc = _RecordingDatarc()
    reg = build_default_registry()
    ctx = _ctx(workspace_id="1", active=None, datarc=dc)
    inv = await reg.execute("bi_list_measures", {}, ctx)
    assert inv.error is not None
    assert inv.error.startswith("DatarcUnavailableError")
    assert dc.measures_ws is None  # handler never reached


async def test_bi_tool_refused_via_execute_when_no_datarc() -> None:
    """P3 hard backstop: execute() refuses a bi_* tool when the turn has no datarc client — the
    handler is never reached (would otherwise crash on datarc=None). role=None so the role gate
    is skipped, exercising the datarc-capability gate specifically."""
    reg = build_default_registry()
    ctx = _ctx(workspace_id="ws_x", active=None, datarc=None)
    inv = await reg.execute("bi_list_measures", {}, ctx)
    assert inv.error is not None
    assert inv.error.startswith("DatarcUnavailableError")
    assert "kb_search" in inv.error  # steers to KB
    assert inv.result is None


async def test_bi_tool_runs_via_execute_when_datarc_present() -> None:
    """Behavior-preserving: with a datarc client bound, the gate is a no-op — bi_* reaches its
    handler and routes to the active project exactly as today."""
    dc = _RecordingDatarc()
    reg = build_default_registry()
    ctx = _ctx(workspace_id="ws_x", active="7", datarc=dc)
    inv = await reg.execute("bi_list_measures", {}, ctx)
    assert inv.error is None
    assert dc.measures_ws == "7"  # handler reached + routed through resolve_datarc_project


async def test_bi_tool_refused_when_client_present_but_no_project() -> None:
    """S1: a datarc client is bound but the workspace maps to no datarc project (canonical ws_*,
    nothing seeded) → execute() refuses bi_* gracefully (steers to KB) and never hands "" to
    datarc. Distinct from the no-client refusal (test_bi_tool_refused_via_execute_when_no_datarc)."""
    dc = _RecordingDatarc()
    reg = build_default_registry()
    ctx = _ctx(workspace_id="ws_x", active=None, datarc=dc)
    inv = await reg.execute("bi_list_measures", {}, ctx)
    assert inv.error is not None
    assert inv.error.startswith("DatarcUnavailableError")
    assert "kb_search" in inv.error  # steers to KB
    assert inv.result is None
    assert dc.measures_ws is None  # handler never reached → datarc not queried with a bogus id


def test_tool_requires_datarc_targets_only_bi_namespace() -> None:
    assert _tool_requires_datarc("bi_query") is True
    assert _tool_requires_datarc("bi_list_measures") is True
    assert _tool_requires_datarc("kb_search") is False
    assert _tool_requires_datarc("subagent_spawn") is False


async def test_project_memory_get_datarc_default_project(tmp_path: Any) -> None:
    """R8 read path (1b): get_datarc_default_project round-trips the seeded default
    project and isolates by workspace; unset → None (callers fall back to workspace_id)."""
    store = ProjectMemoryStore(str(tmp_path / "mem.db"))
    await store.initialize()
    assert await store.get_datarc_default_project("ws_x") is None
    await store.upsert(workspace_id="ws_x", kind="datarc_pref", key="default_project", value="7")
    assert await store.get_datarc_default_project("ws_x") == "7"
    assert await store.get_datarc_default_project("ws_y") is None


async def test_datarc_last_used_round_trip_and_isolation(tmp_path: Any) -> None:
    """R8 writer/reader (P3 d): set_datarc_last_used_project round-trips + isolates by workspace;
    unset / empty inputs → no-op None."""
    store = ProjectMemoryStore(str(tmp_path / "mem.db"))
    await store.initialize()
    assert await store.get_datarc_last_used_project("ws_x") is None
    await store.set_datarc_last_used_project("ws_x", "42")
    assert await store.get_datarc_last_used_project("ws_x") == "42"
    assert await store.get_datarc_last_used_project("ws_y") is None
    # idempotent overwrite (last write wins)
    await store.set_datarc_last_used_project("ws_x", "99")
    assert await store.get_datarc_last_used_project("ws_x") == "99"
    # empty inputs are no-ops (no row written)
    await store.set_datarc_last_used_project("", "7")
    await store.set_datarc_last_used_project("ws_z", "")
    assert await store.get_datarc_last_used_project("ws_z") is None


async def test_last_used_preferred_over_default_project(tmp_path: Any) -> None:
    """R8 resolution order (gateway): last_used wins over the seeded default; with only a
    default, the default is used; with neither, None (→ falls back to workspace_id)."""
    store = ProjectMemoryStore(str(tmp_path / "mem.db"))
    await store.initialize()

    async def _resolve(ws: str) -> str | None:  # mirrors chat.py's preference order
        return await store.get_datarc_last_used_project(ws) or await store.get_datarc_default_project(ws)

    await store.upsert(workspace_id="ws_x", kind="datarc_pref", key="default_project", value="7")
    assert await _resolve("ws_x") == "7"  # only default
    await store.set_datarc_last_used_project("ws_x", "42")
    assert await _resolve("ws_x") == "42"  # last_used overrides default
    assert await _resolve("ws_none") is None
