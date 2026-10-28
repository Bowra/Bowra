"""CCA P3 · S1 binding — the opt-in team-tool-bundle subset intersection.

Tech-design d3 §2.1/§2.2: SubagentProfile carries an optional `team_tool_bundle`
(None = built-in/system → full overlay union, byte-identical; a concrete tuple =
the P3 team subagent's declared SUBSET, `()` = zero team tools). The single seam is
`_effective_tool_whitelist(profile_whitelist, view, team_tool_bundle=None)`:

    bundle is None → set(profile_whitelist) | set(view.overlay_names)        # union (unchanged)
    bundle set     → set(profile_whitelist) | (set(view.overlay_names) ∩ set(bundle))

These tests pin the sentinel contract + that native names always ride
profile_whitelist regardless of bundle (native names are not in overlay_names).
"""

from __future__ import annotations

from bowhead_core.subagent import SubagentProfile, _materialize_team_profile
from bowhead_core.tool_registry import (
    ToolRegistry,
    ToolSpec,
    _effective_tool_whitelist,
)
from bowhead_router.types import ToolSchema


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        schema=ToolSchema(name=name, description="", input_schema={"type": "object"}),
        handler=lambda args, ctx: None,  # type: ignore[arg-type,return-value]
    )


def _team_view(*team_names: str) -> ToolRegistry:
    """A per-turn view whose overlay = the given teammcp.* names (mirrors
    clone_with_overlay at the LOAD POINT)."""
    base = ToolRegistry()
    return base.clone_with_overlay([_spec(n) for n in team_names])


PROF = ("bi_query", "skill_invoke")  # native names — never in overlay_names


def test_no_overlay_none_bundle_is_just_profile() -> None:
    base = ToolRegistry()
    assert _effective_tool_whitelist(PROF, base) == set(PROF)
    assert _effective_tool_whitelist(PROF, base, None) == set(PROF)


def test_none_bundle_keeps_full_union_byte_identical() -> None:
    view = _team_view("teammcp.a.x", "teammcp.a.y", "teammcp.b.z")
    # built-in/system profile (bundle=None) still gets EVERY team tool (the
    # intentional union — compute/consultant rely on it).
    assert _effective_tool_whitelist(PROF, view) == set(PROF) | {
        "teammcp.a.x",
        "teammcp.a.y",
        "teammcp.b.z",
    }
    assert _effective_tool_whitelist(PROF, view, None) == _effective_tool_whitelist(PROF, view)


def test_bundle_subset_intersects_overlay() -> None:
    view = _team_view("teammcp.a.x", "teammcp.a.y", "teammcp.b.z")
    eff = _effective_tool_whitelist(PROF, view, ("teammcp.a.x", "teammcp.b.z"))
    # native profile names kept; team tools narrowed to the declared subset.
    assert eff == set(PROF) | {"teammcp.a.x", "teammcp.b.z"}
    assert "teammcp.a.y" not in eff  # NOT in the bundle → not exposed


def test_empty_bundle_yields_zero_team_tools_not_union() -> None:
    """sentinel: a native-only team subagent resolves to team_tool_bundle=() and
    must see ZERO team tools (NOT re-open the full union)."""
    view = _team_view("teammcp.a.x", "teammcp.a.y")
    eff = _effective_tool_whitelist(PROF, view, ())
    assert eff == set(PROF)
    assert not any(n.startswith("teammcp.") for n in eff)


def test_stale_bundle_ref_drops_via_intersection() -> None:
    """a bundle naming an team tool no longer in the overlay (deleted/disabled
    source) is silently dropped by the intersection."""
    view = _team_view("teammcp.a.x")
    eff = _effective_tool_whitelist(PROF, view, ("teammcp.a.x", "teammcp.gone.z"))
    assert eff == set(PROF) | {"teammcp.a.x"}
    assert "teammcp.gone.z" not in eff


def test_subagent_profile_carries_bundle_and_output_schema() -> None:
    p = SubagentProfile(
        name="sales-expert",
        llm_agent_type="analyst",
        tool_whitelist=("bi_query",),
        system_prompt="...",
        team_tool_bundle=("teammcp.crm.get",),
        output_schema={"type": "object", "additionalProperties": False},
    )
    assert p.team_tool_bundle == ("teammcp.crm.get",)
    assert p.output_schema == {"type": "object", "additionalProperties": False}
    # defaults: built-in profiles carry None (→ union sentinel)
    base = SubagentProfile(
        name="compute",
        llm_agent_type="analyst",
        tool_whitelist=("bi_query",),
        system_prompt="...",
    )
    assert base.team_tool_bundle is None
    assert base.output_schema is None


# ─────────────────────────────────────────────────────────────
# S1.2 — legacy-path reachability: bundle subset + role-aware Lead filter.
# Drives the prod-default `_legacy_run` schema-filter via a capturing router
# (mirrors test_cca_tool_view_overlay's _CapturingRouter). The router captures
# req.tools on the first invoke (== filtered_schemas) then ends the loop.
# ─────────────────────────────────────────────────────────────
import pytest  # noqa: E402
from bowhead_core.events import TurnEventBus  # noqa: E402
from bowhead_core.subagent import PROFILES, SubagentRunner  # noqa: E402
from bowhead_core.tool_registry import ToolExecutionContext  # noqa: E402

from bowhead_router import LLMResponse  # noqa: E402


def _spec_roled(name: str, roles: frozenset[str] | None = None) -> ToolSpec:
    s = ToolSpec(
        schema=ToolSchema(name=name, description="", input_schema={"type": "object"}),
        handler=lambda args, ctx: None,  # type: ignore[arg-type,return-value]
    )
    if roles is not None:
        object.__setattr__(s, "allowed_for_roles", roles)
    return s


class _CapturingRouter:
    def __init__(self) -> None:
        self.captured: list[str] = []

    async def invoke(self, agent_type: str, req, **_kw) -> LLMResponse:  # type: ignore[no-untyped-def]
        # **_kw tolerates ws-decouple's per-team routing kwargs (profile=, user_pref=)
        self.captured = [t.name for t in req.tools]
        return LLMResponse(content="done", tool_calls=[])  # no tool_calls → loop ends

    def stream(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _ctx_role(role: str, view: ToolRegistry) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        role=role,
        tool_view=view,
    )


async def _capture_tools(profile: SubagentProfile, ctx: ToolExecutionContext, monkeypatch) -> list[str]:
    monkeypatch.setitem(PROFILES, profile.name, profile)
    router = _CapturingRouter()
    runner = SubagentRunner(router=router, tool_registry=ToolRegistry())  # type: ignore[arg-type]
    await runner.run(
        profile_name=profile.name,
        task="t",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    return router.captured


def _team_view_roled() -> ToolRegistry:
    base = ToolRegistry()
    base.register(_spec_roled("bi_query"))  # native, default roles {lead,analyst,composer}
    return base.clone_with_overlay([_spec_roled("teammcp.x", frozenset({"analyst"}))])


@pytest.mark.asyncio
async def test_legacy_lead_filter_drops_analyst_only_team_tool(monkeypatch) -> None:
    """role-aware advertise filter: the LEAD (role='lead') is NOT shown an
    {analyst}-only team tool it can't call, but keeps a native tool it can."""
    view = _team_view_roled()
    lead = SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=("bi_query",), system_prompt="x", max_iterations=1)
    tools = await _capture_tools(lead, _ctx_role("lead", view), monkeypatch)
    assert "teammcp.x" not in tools  # dropped: lead ∉ {analyst}
    assert "bi_query" in tools  # kept: lead ∈ default roles


@pytest.mark.asyncio
async def test_legacy_nonlead_keeps_team_tool_pins_profilename_discriminator(monkeypatch) -> None:
    """a freshly-spawned non-Lead peer runs with ctx.role=='lead' at this site
    (F6 swaps only per-call later) — so a naive ctx.role filter would WRONGLY drop
    the team tool. The discriminator is profile.name=='lead', so a non-Lead profile
    KEEPS the team tool. (canvas-writer is 'narrative', not 'analyst' — proves the
    keep-condition is name!='lead', not an llm_agent_type test.)"""
    view = _team_view_roled()
    peer = SubagentProfile(name="canvas-writer", llm_agent_type="narrative", tool_whitelist=("bi_query",), system_prompt="x", max_iterations=1)
    tools = await _capture_tools(peer, _ctx_role("lead", view), monkeypatch)
    assert "teammcp.x" in tools  # kept — profile.name != 'lead'


@pytest.mark.asyncio
async def test_legacy_bundle_subset_on_prod_default_path(monkeypatch) -> None:
    """a P3 team subagent (team_tool_bundle set) sees ONLY its declared subset on
    the legacy (prod-default) schema-filter; a 3rd team tool is not advertised."""
    base = ToolRegistry()
    base.register(_spec_roled("bi_query", frozenset({"analyst", "lead"})))
    view = base.clone_with_overlay(
        [
            _spec_roled("teammcp.x", frozenset({"analyst"})),
            _spec_roled("teammcp.y", frozenset({"analyst"})),
        ]
    )
    prof = SubagentProfile(
        name="p3-sales", llm_agent_type="analyst", tool_whitelist=("bi_query",), system_prompt="x", team_tool_bundle=("teammcp.x",), max_iterations=1
    )
    tools = await _capture_tools(prof, _ctx_role("analyst", view), monkeypatch)
    assert "teammcp.x" in tools  # in the bundle → exposed
    assert "teammcp.y" not in tools  # NOT in the bundle → not exposed
    assert "bi_query" in tools  # native rides profile_whitelist


# (Removed) test_langgraph_bundle_subset_at_build_nodes checked that langgraph
# backend _build_nodes also respected the team_tool_bundle subset. That backend was retired in 2026-08
# (Agent SDK study A2); the legacy-route tests above cover the same guarantee.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("named_tool", "expect_dispatched", "text_only"),
    [("teammcp.y", False, False), ("teammcp.x", True, False), ("teammcp.y", False, True), ("bi_query", False, True)],
)
async def test_per_call_gate_rejects_out_of_bundle_team_tool(monkeypatch, named_tool: str, expect_dispatched: bool, text_only: bool) -> None:
    """S1-T1（HARD guarantee）：即使 LLM **点名**一个 schema-filter 藏起来的、不在
    bundle 里的团队工具（prompt 注入 / 背下来的名字），per-call gate 也必须**硬拒**。

    原先这条钉在 langgraph 的 exec_tools gate 上；该 backend 已于 2026-08 下线
    （Agent SDK 调研 A2）。**保证本身没有下线** —— legacy 路有同一道闸
    （subagent.py `if tc.name not in effective_whitelist`，读的是同一个 bundle 交集后
    的白名单），所以这条改成驱动 legacy 路，而不是随 backend 一起删掉。

    ★带正对照（teammcp.x 在 bundle 内 → 必须真被派发）：只断言"什么都没发生"是
    空断言 —— harness 一坏（比如 profile 没生效、循环压根没跑到 tool 派发）也照样
    绿。正对照让这条用例真的能红。
    """
    from bowhead_router import ToolCall

    dispatched: list[str] = []

    def _recording_spec(name: str) -> ToolSpec:
        async def _h(args, ctx):  # type: ignore[no-untyped-def]
            dispatched.append(name)
            return {"ok": True}

        return ToolSpec(
            schema=ToolSchema(name=name, description="", input_schema={"type": "object"}),
            handler=_h,  # type: ignore[arg-type]
        )

    base = ToolRegistry()
    base.register(_recording_spec("bi_query"))
    view = base.clone_with_overlay([_recording_spec("teammcp.x"), _recording_spec("teammcp.y")])
    prof = SubagentProfile(
        name="p3-gate", llm_agent_type="analyst", tool_whitelist=(), system_prompt="x", team_tool_bundle=("teammcp.x",)
    )  # bundle EXCLUDES teammcp.y
    if text_only:
        prof = _materialize_team_profile(
            name="p3-gate",
            display_name="Text editor",
            description="Edit supplied text",
            system_prompt="Edit only user-provided text.",
            tool_refs=[],
            skills=[],
            model=None,
            max_iterations=3,
            output_schema=None,
        )
        assert prof is not None
        assert prof.tool_whitelist == ("subagent_return",)
        assert prof.team_tool_bundle == ()
    monkeypatch.setitem(PROFILES, "p3-gate", prof)

    class _NamesTool:
        """第一轮点名目标工具，之后收工。"""

        def __init__(self) -> None:
            self.calls = 0

        async def invoke(self, agent_type: str, req, **_kw):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c", name=named_tool, arguments={})])
            return LLMResponse(content="done", tool_calls=[])

        def stream(self, *a, **kw):  # type: ignore[no-untyped-def]
            # With tool-role messages in the second iteration, legacy streams; an empty stream ends the loop.
            async def _empty():
                return
                yield  # pragma: no cover — Make this an async generator.

            return _empty()

        async def close(self) -> None:
            pass

    runner = SubagentRunner(router=_NamesTool(), tool_registry=ToolRegistry())  # type: ignore[arg-type]
    await runner.run(
        profile_name="p3-gate",
        task="t",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx_role("analyst", view),
    )

    # Both tools are **registered** in the overlay view; only bundle membership differs.
    assert (named_tool in dispatched) is expect_dispatched, (named_tool, dispatched)
