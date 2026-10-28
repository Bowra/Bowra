"""plan_driver unit tests (Planning Mode M1 block 3).

真 PlanStore (tmp sqlite) + fake runner/lookup/corpus/emit + fake sctx.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents.task_spec import TaskFailureError, TaskResult
from bowhead_core.plan_driver import (
    PlanCaps,
    PlanDeps,
    PlanHooks,
    run_plan,
)
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStatus, PlanStore, StepStatus, new_step

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeSctx:
    user_id: str = "u"
    workspace_id: str = "w"
    turn_id: str = "t_run"
    session_id: str = "s"


class FakeRunner:
    """behavior: {agent_name: ("ok", payload) | ("fail", reason)}; default ok."""

    def __init__(self, behavior: dict[str, tuple[str, Any]] | None = None) -> None:
        self.behavior = behavior or {}
        self.dispatched: list[list[Any]] = []

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        self.dispatched.append(list(tasks))
        out = []
        for t in tasks:
            beh = self.behavior.get(t.agent_name, ("ok", {"exec_summary": f"{t.agent_name} ok"}))
            if beh[0] == "ok":
                out.append(TaskResult.succeeded(t, beh[1], tokens_input=10, tokens_output=20))
            else:
                out.append(TaskResult.failed(t, TaskFailureError(beh[1])))
        return out

    @property
    def dispatched_agents(self) -> list[str]:
        return [t.agent_name for batch in self.dispatched for t in batch]


def _lookup(known: set[str]):
    def lk(name: str) -> object:
        if name in known:
            return object()
        raise KeyError(name)

    return lk


def _deps(runner: FakeRunner, known: set[str]) -> PlanDeps:
    return PlanDeps(
        sctx=FakeSctx(),
        runtime_factory=lambda c: c,
        agent_runner_fn=None,
        manifest_lookup=_lookup(known),
        runner=runner,
    )


async def _setup(store: PlanStore, specs: list[tuple[str, list[int], bool]]):
    """specs: [(agent, [dep_indices], needs_corpus)]. 建 plan + steps(算 level 持久)."""
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}", needs_corpus=nc) for i, (a, _deps_idx, nc) in enumerate(specs)]
    for st, (_a, dep_idx, _nc) in zip(steps, specs, strict=True):
        st.depends_on = tuple(steps[d].id for d in dep_idx)
    levels = assign_levels(steps)
    for st in steps:
        st.level = levels[st.id]
    await store.add_steps(steps)
    return plan, steps


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


async def _store(db_path: str) -> PlanStore:
    s = PlanStore(db_path)
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_single_level_completes_and_persists_payload(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, [("kb-analyst", [], False)])
    runner = FakeRunner({"kb-analyst": ("ok", {"exec_summary": "3 risks", "n": 3})})
    out = await run_plan(plan.id, store=store, deps=_deps(runner, {"kb-analyst"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)
    assert out.status == "completed" and out.done == 1
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.DONE
    assert '"n": 3' in (got.payload_json or "")  # Full payload persisted (report fidelity).
    assert got.tokens_input == 10 and got.tokens_output == 20
    p = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert p.status == PlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_diamond_upstream_payload_flows_to_summary(db_path: str) -> None:
    # a → {b, c} → d; d must receive full b,c payloads.
    store = await _store(db_path)
    plan, steps = await _setup(
        store,
        [
            ("a", [], False),
            ("b", [0], False),
            ("c", [0], False),
            ("d", [1, 2], False),
        ],
    )
    runner = FakeRunner(
        {
            "b": ("ok", {"exec_summary": "B-finding"}),
            "c": ("ok", {"exec_summary": "C-finding"}),
        }
    )
    out = await run_plan(
        plan.id, store=store, deps=_deps(runner, {"a", "b", "c", "d"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000
    )
    assert out.status == "completed" and out.done == 4
    # d dispatch task must include b,c upstream payloads.
    d_id = steps[3].id
    d_task = next(t for batch in runner.dispatched for t in batch if t.task_id == d_id)
    up_agents = {u["agent"] for u in d_task.upstream_payloads}
    assert {"b", "c"} <= up_agents
    payloads = {u["payload"]["exec_summary"] for u in d_task.upstream_payloads if u["agent"] in ("b", "c")}
    assert payloads == {"B-finding", "C-finding"}


@pytest.mark.asyncio
async def test_excluded_step_skipped_downstream_still_runs(db_path: str) -> None:
    # A (editable): a + b (unchecked included=False) → c (depends on a,b).
    # b SKIPPED, never dispatched; c runs with a upstream (partial-ok); plan completed.
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    a = new_step(plan_id=plan.id, seq=0, agent_name="a", objective="a")
    b = new_step(plan_id=plan.id, seq=1, agent_name="b", objective="b", included=False)
    c = new_step(plan_id=plan.id, seq=2, agent_name="c", objective="c")
    c.depends_on = (a.id, b.id)
    levels = assign_levels([a, b, c])
    for st in (a, b, c):
        st.level = levels[st.id]
    await store.add_steps([a, b, c])
    runner = FakeRunner()
    out = await run_plan(plan.id, store=store, deps=_deps(runner, {"a", "b", "c"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)
    assert out.status == "completed"
    steps = {s.id: s for s in await store.list_steps(plan.id)}
    assert steps[b.id].status == StepStatus.SKIPPED  # excluded → skipped, never ran.
    assert steps[a.id].status == StepStatus.DONE
    assert steps[c.id].status == StepStatus.DONE  # Runs with a upstream.
    assert "b" not in runner.dispatched_agents  # Excluded never dispatched.
    # included column round trip.
    assert steps[b.id].included is False
    assert steps[a.id].included is True


@pytest.mark.asyncio
async def test_gate_unknown_agent_and_needs_corpus_block(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(
        store,
        [
            ("kb-analyst", [], True),  # needs corpus, corpus_check False → blocked
            ("ghost-agent", [], False),  # not in registry → blocked
        ],
    )
    runner = FakeRunner()

    async def no_corpus(_s):
        return False

    out = await run_plan(
        plan.id,
        store=store,
        deps=_deps(runner, {"kb-analyst"}),
        hooks=PlanHooks(corpus_check=no_corpus),
        caps=PlanCaps(),
        parent_token_budget=20000,
    )
    assert out.status == "blocked_needs_user"
    st = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert st["kb-analyst"].status == StepStatus.BLOCKED
    assert st["kb-analyst"].error_kind == "needs_corpus"
    assert st["ghost-agent"].status == StepStatus.BLOCKED
    assert st["ghost-agent"].error_kind == "unknown_agent"
    assert runner.dispatched == []  # No dispatch (all gated).


@pytest.mark.asyncio
async def test_partial_upstream_runs_all_failed_blocks(db_path: str) -> None:
    # a,b → c (both deps): a ok,b fail → c runs (partial-ok). e→f: e fail → f BLOCKED (all failed).
    store = await _store(db_path)
    plan, _ = await _setup(
        store,
        [
            ("a", [], False),
            ("b", [], False),
            ("c", [0, 1], False),  # 0,1,2
            ("e", [], False),
            ("f", [3], False),  # 3,4
        ],
    )
    runner = FakeRunner({"b": ("fail", "boom"), "e": ("fail", "boom")})
    out = await run_plan(
        plan.id, store=store, deps=_deps(runner, {"a", "b", "c", "e", "f"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000
    )
    st = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert st["c"].status == StepStatus.DONE  # Some upstream (a) ok → runs.
    # Sole upstream (e) failed → BLOCKED (nonterminal; retry after upstream repair), not SKIPPED.
    assert st["f"].status == StepStatus.BLOCKED
    assert st["f"].error_kind == "upstream_failed"
    assert out.status == "blocked_needs_user"  # b,e failed, not completed.


@pytest.mark.asyncio
async def test_per_step_retry_keeps_done_steps(db_path: str) -> None:
    # a → b: first a fails → blocked. Repair/retry reruns a then b; done steps never rerun.
    store = await _store(db_path)
    plan, steps = await _setup(store, [("a", [], False), ("b", [0], False)])
    runner = FakeRunner({"a": ("fail", "boom")})
    out1 = await run_plan(plan.id, store=store, deps=_deps(runner, {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)
    assert out1.status == "blocked_needs_user"
    assert (await store.get_plan(plan.id, user_id="u", workspace_id="w")).status == PlanStatus.BLOCKED

    # User repairs a → reset pending and retry.
    await store.update_step_status(steps[0].id, status=StepStatus.PENDING)
    runner.behavior = {}  # a now succeeds.
    runner.dispatched.clear()
    out2 = await run_plan(plan.id, store=store, deps=_deps(runner, {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)
    assert out2.status == "completed"
    # Retry dispatches only a(level0),b(level1); never redispatches done steps.
    assert sorted(runner.dispatched_agents) == ["a", "b"]
    # b receives full a payload from this successful result.
    b_task = next(t for batch in runner.dispatched for t in batch if t.agent_name == "b")
    assert any(u["agent"] == "a" for u in b_task.upstream_payloads)


@pytest.mark.asyncio
async def test_retry_loads_prior_done_payload_from_db(db_path: str) -> None:
    # a done (payload persisted) → only b runs; reconstruct a upstream from payload_json (full fidelity).
    store = await _store(db_path)
    plan, steps = await _setup(store, [("a", [], False), ("b", [0], False)])
    # Preset a done + payload (simulates previous completion).
    await store.update_step_status(steps[0].id, status=StepStatus.DONE, payload_json='{"exec_summary":"A-prior","k":7}')
    runner = FakeRunner()
    out = await run_plan(plan.id, store=store, deps=_deps(runner, {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)
    assert out.status == "completed"
    assert runner.dispatched_agents == ["b"]  # a not rerun.
    b_task = next(t for batch in runner.dispatched for t in batch if t.agent_name == "b")
    a_up = next(u for u in b_task.upstream_payloads if u["agent"] == "a")
    assert a_up["payload"] == {"exec_summary": "A-prior", "k": 7}  # Full fidelity, no summary.


@pytest.mark.asyncio
async def test_caps_max_steps_and_tokens(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, [("a", [], False), ("b", [], False)])
    runner = FakeRunner()
    out = await run_plan(
        plan.id, store=store, deps=_deps(runner, {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(max_steps=1), parent_token_budget=20000
    )
    assert out.status == "failed" and out.reason == "max_steps_exceeded"

    store2 = await _store(db_path + "2")
    plan2, _ = await _setup(store2, [("a", [], False), ("b", [], False)])
    out2 = await run_plan(
        plan2.id, store=store2, deps=_deps(FakeRunner(), {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(max_total_tokens=5), parent_token_budget=20000
    )
    assert out2.status == "failed" and out2.reason == "max_total_tokens"


@pytest.mark.asyncio
async def test_cancel_stops_execution(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, [("a", [], False)])
    await store.update_plan_status(plan.id, PlanStatus.CANCELLED)  # Sticky terminal state.
    events: list[str] = []

    async def rec(name, payload):
        events.append(name)

    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner(), {"a"}), hooks=PlanHooks(emit=rec), caps=PlanCaps(), parent_token_budget=20000)
    # review-fix: a durable cancel returns a `cancelled` outcome (not "failed") so
    # hook2 surfaces an aborted (not error) turn; no plan_failed emitted; plan stays
    # CANCELLED (sticky).
    assert out.status == "cancelled" and out.reason == "cancelled"
    assert "plan_failed" not in events
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None and got.status == PlanStatus.CANCELLED


@pytest.mark.asyncio
async def test_emit_event_sequence(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, [("a", [], False)])
    events: list[str] = []

    async def rec(name, payload):
        events.append(name)

    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner(), {"a"}), hooks=PlanHooks(emit=rec), caps=PlanCaps(), parent_token_budget=20000)
    assert out.status == "completed"
    # 2026-07: RUNNING now actually emits (previously DB only; frontend could only set optimistically).
    assert events == ["plan_running", "step_started", "step_done", "plan_completed"]


# ── 2026-07 planning-ux-reliability: step timeout / wallclock anchor ──
# 116 production review: expert legs all hit chat-mode 500s fallback Timeout (8min completed work
# lost), canvas-writer 150s → no report. Plan-step timeout uses remaining wallclock budget.


def test_step_timeout_policy_pure() -> None:
    from bowhead_core.plan_driver import _step_timeout_s

    assert _step_timeout_s(None, is_last_level=False) == 1500  # No-cap fallback.
    assert _step_timeout_s(1680, is_last_level=False) == 1320  # Reserve writer time.
    assert _step_timeout_s(1680, is_last_level=True) == 1650  # Last level uses remainder.
    assert _step_timeout_s(100, is_last_level=False) == 120  # floor


def test_step_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.plan_driver import _step_timeout_s

    monkeypatch.setenv("BOWHEAD_PLANNING_STEP_TIMEOUT_S", "777")
    assert _step_timeout_s(1680, is_last_level=False) == 777
    assert _step_timeout_s(None, is_last_level=True) == 777


@pytest.mark.asyncio
async def test_dispatched_tasks_carry_budget_aware_timeout(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, [("a", [], False), ("writer", [0], False)])
    runner = FakeRunner()
    out = await run_plan(
        plan.id, store=store, deps=_deps(runner, {"a", "writer"}), hooks=PlanHooks(), caps=PlanCaps(max_wallclock_s=1680), parent_token_budget=20000
    )
    assert out.status == "completed"
    lvl0, lvl1 = runner.dispatched[0], runner.dispatched[1]
    # Nonfinal: remaining(≈1680) -360 reserve; final: remaining -30 (tiny elapsed).
    assert 1310 <= lvl0[0].timeout_s <= 1320
    assert 1640 <= lvl1[0].timeout_s <= 1650


@pytest.mark.asyncio
async def test_no_wallclock_cap_uses_fallback_timeout(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, [("a", [], False)])
    runner = FakeRunner()
    await run_plan(plan.id, store=store, deps=_deps(runner, {"a"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)
    assert runner.dispatched[0][0].timeout_s == 1500


@pytest.mark.asyncio
async def test_rerun_resets_wallclock_anchor(db_path: str) -> None:
    """隔天重试不再因老 execution_started_at 秒杀成 FAILED(终态,不可恢复)."""
    import sqlite3

    store = await _store(db_path)
    plan, _ = await _setup(store, [("a", [], False), ("b", [0], False)])
    out1 = await run_plan(
        plan.id,
        store=store,
        deps=_deps(FakeRunner({"a": ("fail", "Timeout")}), {"a", "b"}),
        hooks=PlanHooks(),
        caps=PlanCaps(max_wallclock_s=600),
        parent_token_budget=20000,
    )
    assert out1.status == "blocked_needs_user"
    # Simulate retry one day later: move wallclock anchor far into past.
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE plans SET execution_started_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (plan.id,))
        db.commit()
    out2 = await run_plan(
        plan.id, store=store, deps=_deps(FakeRunner(), {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(max_wallclock_s=600), parent_token_budget=20000
    )
    # Reusing old anchor triggers _caps_breach after level0 → failed(max_wallclock).
    assert out2.status == "completed"


@pytest.mark.asyncio
async def test_midflight_cancel_stops_at_next_level_boundary(db_path: str) -> None:
    # Feishu07.06 #7: cancel midflight (click Stop midway), real run_plan + real orchestrator cascade.
    # test_cancel_stops_execution is CANCELLED before execution; this checks cancellation while running →
    # stop at next level boundary, never run downstream, persist CANCELLED (F5 cannot revive).
    from bowhead_core.orchestrator import Orchestrator

    store = await _store(db_path)
    plan, _steps = await _setup(store, [("a", [], False), ("b", [0], False)])
    await store.update_plan_status(plan.id, PlanStatus.RUNNING)

    # Reproduce turn_runner turn→plan registration and gateway cancel_turn get_executing_plan + stamping.
    orch = Orchestrator.__new__(Orchestrator)
    orch._executing_plans = {"t_run": plan.id}
    assert orch.get_executing_plan("t_run") == plan.id

    dispatched: list[str] = []

    class MidFlightCancel(FakeRunner):
        async def dispatch(self, tasks, sctx, rf, arf, ml):
            for t in tasks:
                dispatched.append(t.agent_name)
                if t.agent_name == "a":  # User clicks Stop now → real cascade.
                    pid = orch.get_executing_plan("t_run")
                    await store.update_plan_status(pid, PlanStatus.CANCELLED)
            return await super().dispatch(tasks, sctx, rf, arf, ml)

    out = await run_plan(
        plan.id, store=store, deps=_deps(MidFlightCancel(), {"a", "b"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000
    )

    assert out.status == "cancelled" and out.reason == "cancelled"
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None and got.status == PlanStatus.CANCELLED
    assert dispatched == ["a"], f"level-1 的 b 不该执行, got {dispatched}"


# ── step_failed must carry reason (116 dogfood 2026-08-15) ───────
# Actual run: `plan_steps.error` always held readable sentence, but `step_failed` sent **only
# error_kind, no reason** (step_blocked always did) → live plan card displayed
# blank after "Failed:", **full text appearing only after F5**.
# Also pin same string in storage and event; otherwise live / persisted / replay diverge.


@pytest.mark.asyncio
async def test_step_failed_emits_human_reason_matching_the_db(db_path: str) -> None:
    store = await _store(db_path)
    plan, _steps = await _setup(store, [("a", [], False)])
    payloads: list[tuple[str, dict]] = []

    async def rec(name, payload):
        payloads.append((name, payload))

    runner = FakeRunner({"a": ("fail", "domain_mismatch")})
    # TaskFailureError with original: production bail uses this path (reason lives on original).
    orig_dispatch = runner.dispatch

    async def dispatch_with_original(tasks, sctx, rf, arf, ml):
        runner.dispatched.append(list(tasks))
        return [TaskResult.failed(t, TaskFailureError("domain_mismatch", Exception("知识库中无《供应商合同》文档"))) for t in tasks]

    runner.dispatch = dispatch_with_original  # type: ignore[method-assign]
    assert orig_dispatch is not None
    await run_plan(plan.id, store=store, deps=_deps(runner, {"a"}), hooks=PlanHooks(emit=rec), caps=PlanCaps(), parent_token_budget=20000)
    failed = [p for (n, p) in payloads if n == "step_failed"]
    assert failed, "没有 step_failed 事件，这条测试什么也没证明"
    # Mutation: remove "reason" from emit → this assertion fails.
    assert failed[0].get("reason") == "知识库中无《供应商合同》文档"
    assert failed[0].get("error_kind") == "domain_mismatch"
    # ★live == persisted: event string must exactly match stored error.
    row = (await store.list_steps(plan.id))[0]
    assert row.error == failed[0]["reason"]
    # Stop repeating enum value (previously "domain_mismatch: domain_mismatch").
    assert not (row.error or "").startswith("domain_mismatch:")


@pytest.mark.asyncio
async def test_step_failed_reason_falls_back_when_no_original(db_path: str) -> None:
    """负对照：拿不到人话时回落旧形式，不许出现空串（空比 kind 更糟）。"""
    store = await _store(db_path)
    plan, _steps = await _setup(store, [("a", [], False)])
    payloads: list[tuple[str, dict]] = []

    async def rec(name, payload):
        payloads.append((name, payload))

    await run_plan(
        plan.id,
        store=store,
        deps=_deps(FakeRunner({"a": ("fail", "Timeout")}), {"a"}),
        hooks=PlanHooks(emit=rec),
        caps=PlanCaps(),
        parent_token_budget=20000,
    )
    failed = [p for (n, p) in payloads if n == "step_failed"]
    assert failed and failed[0].get("reason"), "回落也必须有内容"
    row = (await store.list_steps(plan.id))[0]
    assert row.error == failed[0]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", [StepStatus.PENDING, StepStatus.BLOCKED])
async def test_retired_writer_step_in_an_old_plan_is_skipped_and_the_plan_completes(db_path: str, prior_status: StepStatus) -> None:
    """老 plan 的 canvas-writer 终端步（未跑，或此前被判 unknown_agent 阻塞）按 skipped 收口，plan 能 completed。"""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("kb-analyst", [], False), ("canvas-writer", [0], False)])
    if prior_status == StepStatus.BLOCKED:
        await store.update_step_status(
            steps[1].id, status=StepStatus.BLOCKED, error="agent 'canvas-writer' 不在 registry", error_kind="unknown_agent"
        )
    runner = FakeRunner()
    out = await run_plan(plan.id, store=store, deps=_deps(runner, {"kb-analyst"}), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=20000)

    assert out.status == "completed", out
    assert runner.dispatched_agents == ["kb-analyst"]
    writer = next(s for s in await store.list_steps(plan.id) if s.agent_name == "canvas-writer")
    assert writer.status == StepStatus.SKIPPED and writer.error_kind == "agent_retired"
    p = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert p.status == PlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_prepare_batch_sees_only_runnable_frontier_and_retry_uses_saved_output(db_path: str) -> None:
    from dataclasses import replace

    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", [], False), ("policy", [], True), ("summary", [0], False)])
    runner = FakeRunner()
    prepared = []

    async def prepare(tasks):
        prepared.append([task.task_id for task in tasks])
        if tasks[0].agent_name == "summary":
            assert tasks[0].upstream_payloads[0]["payload"]["exec_summary"] == "compute ok"

    deps = replace(_deps(runner, {"compute", "policy", "summary"}), prepare_batch=prepare)

    async def corpus_missing(step):
        return False

    first = await run_plan(plan.id, store=store, deps=deps, hooks=PlanHooks(corpus_check=corpus_missing), caps=PlanCaps(), parent_token_budget=1000)
    assert first.status == "blocked_needs_user"
    assert prepared == [[steps[0].id], [steps[2].id]]
    prepared.clear()

    async def corpus_available(step):
        return True

    second = await run_plan(
        plan.id, store=store, deps=deps, hooks=PlanHooks(corpus_check=corpus_available), caps=PlanCaps(), parent_token_budget=1000
    )
    assert second.status == "completed"
    assert prepared == [[steps[1].id]]
    assert runner.dispatched_agents == ["compute", "summary", "policy"]
