"""批2 mid-run HITL 测试 (batch2-hitl-design §2 S2-S5, core 侧).

覆盖: 拾取判据 lookup 表驱动 / picker 卡 helper 双形状 / 收敛两态 / 终局定向过期
/ clarification_store.expire_pending_for_turn。gateway 分派 helper 见
test_plan_midrun_resolve.py(gateway 侧)。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import (
    LeadRuntime,
    _put_datarc_picker_card,
    _run_plan_execution_turn,
)
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStatus, PlanStore, new_step
from bowhead_core.subagent import SubagentResult

_WS = ({"id": "101", "name": "华东"}, {"id": "102", "name": "华南"})


# ── Two picker-card helper shapes (D1) ──────────────────────────


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)

    def find_all(self, event_type: str) -> list[Any]:
        return [e for e in self.events if e.event_type == event_type]


class _FakeClarStore:
    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.resume_ctx: dict[str, dict] = {}
        self.pending: list[Any] = []
        self.expired_turns: list[str] = []
        self.expired_plans: list[str] = []

    async def put(self, **kw: Any) -> Any:
        self.puts.append(kw)
        request_id = f"req_{len(self.puts)}"
        self.resume_ctx[request_id] = kw.get("resume_context") or {}
        return SimpleNamespace(request_id=request_id)

    async def set_resume_context(self, rid: str, ctx: dict) -> bool:
        self.resume_ctx[rid] = ctx
        return True

    async def get_pending_for_turn(self, turn_id: str) -> list[Any]:
        return self.pending

    async def expire_pending_for_turn(
        self,
        turn_id: str,
        *,
        plan_id: str,
    ) -> list[str]:
        self.expired_turns.append(turn_id)
        self.expired_plans.append(plan_id)
        return [SimpleNamespace(request_id="req_exp").request_id] if self.pending else []


def _sctx(plan_id: str, turn_id: str = "t_mr") -> SerializableTurnContext:
    return SerializableTurnContext(user_id="u", workspace_id="w", session_id="s", turn_id=turn_id, role="user", plan_execution_id=plan_id)


@pytest.mark.asyncio
async def test_picker_card_midrun_shape() -> None:
    bus, clar = _FakeBus(), _FakeClarStore()
    cid = await _put_datarc_picker_card(
        bus=bus,
        sctx=_sctx("plan_x"),
        clarification_store=clar,
        accessible=_WS,
        prompt_prefix="计划执行中发现需要选择:你在北极九章有",
        resume_context={"plan_midrun_plan_id": "plan_x", "session_id": "s"},
        log_tag="test",
    )
    assert cid == "req_1"
    assert clar.resume_ctx[cid] == {"plan_midrun_plan_id": "plan_x", "session_id": "s"}
    evs = bus.find_all("clarification_required")
    assert len(evs) == 1 and evs[0].payload["clarification_id"] == cid
    assert evs[0].payload["kind"] == "tool_context_select"
    assert [o["value"] for o in evs[0].payload["options"]] == ["101", "102"]


@pytest.mark.asyncio
async def test_picker_card_no_store_returns_empty() -> None:
    cid = await _put_datarc_picker_card(
        bus=_FakeBus(),
        sctx=_sctx("plan_x"),
        clarification_store=None,
        accessible=_WS,
        prompt_prefix="x",
        resume_context={},
        log_tag="test",
    )
    assert cid == ""


# ── Two convergence states + terminal expiry (D5), through _run_plan_execution_turn ──


@dataclass
class _ProfileStubRunner:
    by_profile: dict[str, SubagentResult] = field(default_factory=dict)

    async def run(self, *, profile_name: str, **_kw: Any) -> SubagentResult:
        r = self.by_profile.get(profile_name)
        assert r is not None, f"no stub for {profile_name}"
        return r


def _empty_sub(profile: str) -> SubagentResult:
    # Empty output →three-check empty_output →FAILED →plan blocked_needs_user.
    return SubagentResult(
        profile=profile, subagent_id="sub_x", ok=True, final_answer="", payload=None, tool_invocations=[], tokens_input=1, tokens_output=1
    )


async def _setup_plan(db_path: str) -> tuple[PlanStore, Any]:
    store = PlanStore(db_path)
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="T")
    st = new_step(plan_id=plan.id, seq=0, agent_name="financial-analyst", objective="o")
    st.level = assign_levels([st])[st.id]
    await store.add_steps([st])
    await store.update_plan_status(plan.id, PlanStatus.RUNNING)
    return store, plan


def _runtime(store: PlanStore, clar: Any) -> LeadRuntime:
    ctx = SimpleNamespace(
        canvas_store=None,
        artifact_store=None,
        kb_files_store=None,
        clarification_store=clar,
        member_uid="",
        language_context=None,
        resolve_datarc_project=lambda: "1",  # Bound already ⇒ no preflight.
        accessible_datarc_workspaces=_WS,
        agent_registry=None,
        user_memory_store=None,
    )
    bus = _FakeBus()
    bus._d6_canvas_store = None  # type: ignore[attr-defined]
    bus._d6_is_plan_execution = True  # type: ignore[attr-defined]
    return LeadRuntime(
        router=None,  # type: ignore[arg-type]
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),  # type: ignore[arg-type]
        parent_bus=bus,  # type: ignore[arg-type]
        parent_ctx=ctx,  # type: ignore[arg-type]
        plan_store=store,
        subagent_runner=_ProfileStubRunner({"financial-analyst": _empty_sub("financial-analyst")}),  # type: ignore[arg-type]
        multi_agent_registry=AgentRegistry.bootstrap(),
    )


@pytest.mark.asyncio
async def test_convergence_pending_card_ends_clarification_pending(tmp_path) -> None:
    """D5 两态之一: 有未答 **mid-run picker** 卡 → 预检同形 done(clarification_pending).

    实现 review P1-1: 判据必须按 resume_context.plan_midrun_plan_id 过滤——fake
    必须带真实行形状(resume_context), 无形状 fake 曾把错误行为钉成预期。"""
    store, plan = await _setup_plan(str(tmp_path / "p.db"))
    clar = _FakeClarStore()
    clar.pending = [SimpleNamespace(request_id="req_open", resume_context={"plan_midrun_plan_id": plan.id, "session_id": "s"})]
    rt = _runtime(store, clar)
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    done = rt.parent_bus.find_all("done")
    assert done and done[-1].payload["status"] == "clarification_pending"
    assert done[-1].payload["error_kind"] == "plan_needs_context"
    from bowhead_core.types import TurnStatus

    assert res.status == TurnStatus.CLARIFICATION_PENDING


@pytest.mark.asyncio
async def test_convergence_ignores_non_midrun_pending_cards(tmp_path) -> None:
    """实现 review P1-1 负例: 同 turn 的其它 pending 澄清行(如 BI token 假设卡)
    不得把 blocked 收敛翻成 clarification_pending——现状 error/plan_blocked 不变."""
    store, plan = await _setup_plan(str(tmp_path / "p.db"))
    clar = _FakeClarStore()
    clar.pending = [
        SimpleNamespace(request_id="req_token", resume_context=None),  # Token hypothesis-card shape.
        SimpleNamespace(request_id="req_other_plan", resume_context={"plan_midrun_plan_id": "plan_other"}),
    ]
    rt = _runtime(store, clar)
    await _run_plan_execution_turn(_sctx(plan.id), rt)
    done = rt.parent_bus.find_all("done")
    assert done and done[-1].payload["status"] == "error"
    assert done[-1].payload["error_kind"] == "plan_blocked"


@pytest.mark.asyncio
async def test_convergence_no_card_keeps_current_error_shape(tmp_path) -> None:
    """D5 两态之二: 无卡 → 现状 done(error, plan_blocked) 字节不变."""
    store, plan = await _setup_plan(str(tmp_path / "p.db"))
    clar = _FakeClarStore()  # pending empty.
    rt = _runtime(store, clar)
    await _run_plan_execution_turn(_sctx(plan.id), rt)
    done = rt.parent_bus.find_all("done")
    assert done and done[-1].payload["status"] == "error"
    assert done[-1].payload["error_kind"] == "plan_blocked"


@pytest.mark.asyncio
async def test_terminal_cancel_expires_pending_cards(tmp_path) -> None:
    """D5 终局: cancelled 收敛定向过期未答卡 + clarification.resolved(skipped=true)."""
    store, plan = await _setup_plan(str(tmp_path / "p.db"))
    await store.update_plan_status(plan.id, PlanStatus.CANCELLED)
    clar = _FakeClarStore()
    clar.pending = [SimpleNamespace(request_id="req_exp")]
    rt = _runtime(store, clar)
    await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert clar.expired_turns == ["t_mr"]
    assert clar.expired_plans == [plan.id]
    resolved = rt.parent_bus.find_all("clarification.resolved")
    assert resolved and resolved[0].payload["skipped"] is True


# ── clarification_store.expire_pending_for_turn (real store) ────


@pytest.mark.asyncio
async def test_store_expire_pending_for_turn(tmp_path) -> None:
    store = ClarificationStore(str(tmp_path / "clar.db"))
    await store.initialize()
    req = await store.put(
        turn_id="t_1",
        user_id="u",
        workspace_id="w",
        token="",
        rationale="r",
        candidates=[ClarificationCandidate(value="101", label="华东", hint="", confidence="", kind="tool_context")],
        kind="tool_context_select",
        multi_select=False,
        session_id="s",
        tool="datarc",
    )
    assert await store.set_resume_context(
        req.request_id,
        {"plan_midrun_plan_id": "plan_1", "session_id": "s"},
    )
    same_turn_other_plan = await store.put(
        turn_id="t_1",
        user_id="u",
        workspace_id="w",
        token="",
        rationale="r",
        candidates=[],
        kind="tool_context_select",
        multi_select=False,
        session_id="s",
        tool="datarc",
    )
    assert await store.set_resume_context(
        same_turn_other_plan.request_id,
        {"plan_midrun_plan_id": "plan_other", "session_id": "s"},
    )
    same_turn_regular = await store.put(
        turn_id="t_1",
        user_id="u",
        workspace_id="w",
        token="",
        rationale="r",
        candidates=[],
        kind="tool_context_select",
        multi_select=False,
        session_id="s",
        tool="datarc",
    )
    other = await store.put(
        turn_id="t_other",
        user_id="u",
        workspace_id="w",
        token="",
        rationale="r",
        candidates=[ClarificationCandidate(value="101", label="华东", hint="", confidence="", kind="tool_context")],
        kind="tool_context_select",
        multi_select=False,
        session_id="s",
        tool="datarc",
    )
    expired = await store.expire_pending_for_turn("t_1", plan_id="plan_1")
    assert expired == [req.request_id]
    got = await store.get(req.request_id)
    assert got is not None and got.status == "expired"
    # Other-turn cards unaffected.
    got_other = await store.get(other.request_id)
    assert got_other is not None and got_other.status == "pending"
    got_other_plan = await store.get(same_turn_other_plan.request_id)
    assert got_other_plan is not None and got_other_plan.status == "pending"
    got_regular = await store.get(same_turn_regular.request_id)
    assert got_regular is not None and got_regular.status == "pending"
    # Idempotent: reexpiry returns empty.
    assert await store.expire_pending_for_turn("t_1", plan_id="plan_1") == []


# ── S4 critical assertion (implementation review P1-1): revived redispatch ctx resolves project ──


class _SentinelThenOkRunner:
    """第一派返回 needs_user_input 信封(adapter ②-a 路);第二派记录派发时
    ctx 可解出的 project 并成功。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.calls = 0
        self.projects_at_dispatch: list[str] = []

    async def run(self, *, profile_name: str, parent_ctx: Any = None, **_kw: Any) -> SubagentResult:
        self.calls += 1
        probe = parent_ctx if parent_ctx is not None else self._ctx
        self.projects_at_dispatch.append(str(probe.resolve_datarc_project()))
        if self.calls == 1:
            return SubagentResult(
                profile=profile_name,
                subagent_id="sub_s",
                ok=True,
                final_answer="",
                payload={"error": {"kind": "needs_user_input", "clarification_payload": {"kind": "tool_context_select", "tool": "datarc"}}},
                tool_invocations=[],
                tokens_input=5,
                tokens_output=2,
            )
        return SubagentResult(
            profile=profile_name,
            subagent_id="sub_s2",
            ok=True,
            final_answer="",
            payload={"exec_summary": "ok", "insights": [], "kpis": []},
            tool_invocations=[],
            tokens_input=10,
            tokens_output=20,
        )


@pytest.mark.asyncio
async def test_revive_backfills_ctx_iron_assertion(tmp_path) -> None:
    """全链集成: sentinel 步 block → 出卡 → binding 已在 store(用户已答) →
    revive 复活 + **回填 ctx** → 重派步派发时 resolve_datarc_project() 非空 → 完成."""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="T")
    # KB is knowledge retrieval; incidental BI tools do not make it BI-primary for step preflight.
    st = new_step(plan_id=plan.id, seq=0, agent_name="kb-analyst", objective="o")
    st.level = assign_levels([st])[st.id]
    await store.add_steps([st])
    await store.update_plan_status(plan.id, PlanStatus.RUNNING)

    from bowhead_core.tool_registry import ToolExecutionContext

    class PreparedSelection:
        async def resolve_saved_selection(self):
            return "101", "华东"

        async def prepare_selection(self, identifier, name, *, current):
            return replace(current, datarc_workspace_id=identifier, name=name, member_uid="member_101")

    clar = _FakeClarStore()
    ctx = ToolExecutionContext(
        workspace_id="w",
        user_id="u",
        member_uid="",
        turn_id="t_iron",
        datarc=object(),
        session_id="s",
        accessible_datarc_workspaces=_WS,
        clarification_store=clar,
        datarc_context_switcher=PreparedSelection(),
    )
    runner = _SentinelThenOkRunner(ctx)
    bus = _FakeBus()
    bus._d6_canvas_store = None  # type: ignore[attr-defined]
    bus._d6_is_plan_execution = True  # type: ignore[attr-defined]
    rt = LeadRuntime(
        router=None,  # type: ignore[arg-type]
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),  # type: ignore[arg-type]
        parent_bus=bus,  # type: ignore[arg-type]
        parent_ctx=ctx,  # type: ignore[arg-type]
        plan_store=store,
        subagent_runner=runner,  # type: ignore[arg-type]
        multi_agent_registry=AgentRegistry.bootstrap(),
    )
    res = await _run_plan_execution_turn(_sctx(plan.id, turn_id="t_iron"), rt)

    # Critical assertion: first dispatch unbound (""), revival backfills ctx →"101".
    assert runner.projects_at_dispatch == ["", "101"]
    assert ctx.active_datarc_project == "101"
    assert ctx.active_datarc_workspace_name == "华东"
    assert ctx.member_uid == "member_101"
    # Midrun card issued with plan_midrun resume_context shape.
    assert clar.resume_ctx and next(iter(clar.resume_ctx.values()))["plan_midrun_plan_id"] == plan.id
    # Step ends done; normal terminal turn, not clarification_pending (successful pickup removes card block).
    got = (await store.list_steps(plan.id))[0]
    from bowhead_core.plan_store import StepStatus as _SS

    assert got.status == _SS.DONE
    # Second-dispatch tokens recorded (first uses adapter typed-raise; TaskResult.failed lacks
    # child tokens, preexisting shape recorded in ledger; V6 accumulation of token-bearing failed
    # results separately tested by test_same_turn_double_dispatch_tokens_accumulate).
    assert (got.tokens_input, got.tokens_output) == (10, 20)
    from bowhead_core.types import TurnStatus as _TS

    assert res.status != _TS.CLARIFICATION_PENDING
