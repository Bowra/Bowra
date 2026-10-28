"""plan_driver × verify hook 测试 (graph-mode 批 1, tech-design §1.4/§1.7).

复用 test_plan_driver 的 harness 风格: 真 PlanStore + FakeRunner + fake emit。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents.task_spec import TaskResult
from bowhead_core.plan_driver import PlanCaps, PlanDeps, PlanHooks, run_plan
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStore, StepStatus, new_step
from bowhead_core.plan_verify import (
    ITEM_MET,
    ITEM_UNMET,
    VERDICT_FAIL,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_SKIPPED,
    VerifyItem,
    VerifyOutcome,
    build_verify_digest,
    verify_step_output,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeSctx:
    user_id: str = "u"
    workspace_id: str = "w"
    turn_id: str = "t_run"
    session_id: str = "s"


class FakeRunner:
    async def dispatch(self, tasks, sctx, rf, arf, ml):
        return [TaskResult.succeeded(t, {"exec_summary": f"{t.agent_name} ok"}, tokens_input=10, tokens_output=20) for t in tasks]


def _deps(runner) -> PlanDeps:
    return PlanDeps(
        sctx=FakeSctx(),
        runtime_factory=lambda c: c,
        agent_runner_fn=None,
        manifest_lookup=lambda name: object(),
        runner=runner,
    )


class _Emit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def named(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]


def _fail_outcome() -> VerifyOutcome:
    return VerifyOutcome(VERDICT_FAIL, items=(VerifyItem("含季度数字", ITEM_UNMET, note="缺"),))


def _pass_outcome() -> VerifyOutcome:
    return VerifyOutcome(VERDICT_PASS, items=(VerifyItem("含季度数字", ITEM_MET),))


def _hook(outcome_by_agent: dict[str, VerifyOutcome], calls: list[str] | None = None):
    async def verify(step, payload):
        if calls is not None:
            calls.append(step.agent_name)
        return outcome_by_agent.get(step.agent_name, _pass_outcome())

    return verify


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


async def _store(db_path: str) -> PlanStore:
    s = PlanStore(db_path)
    await s.initialize()
    return s


async def _setup(store: PlanStore, agents: list[str], criteria: tuple[str, ...] = ("含季度数字",)):
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}", success_criteria=criteria) for i, a in enumerate(agents)]
    levels = assign_levels(steps)
    for st in steps:
        st.level = levels[st.id]
    await store.add_steps(steps)
    return plan, steps


@pytest.mark.asyncio
async def test_enforce_needs_no_flag(db_path: str) -> None:
    """★正对照: **只挂 verify hook、不传任何开关**, fail 就必须打 FAILED。

    原 `test_shadow_fail_stays_done_and_records` 钉的是 shadow 阶段(fail 只记账)。
    2026-08-16 标定闸达成后 shadow/enforce 两阶段收尾, `verify_enforce` 开关删除,
    这条改钉相反的不变量: **enforce 不需要任何 flag 才生效**——将来谁再给它加一道
    默认关的闸(env/字段/配置皆同), 这条立刻变红。
    """
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute"])
    emit = _Emit()
    hooks = PlanHooks(emit=emit, verify=_hook({"compute": _fail_outcome()}))  # ← No flag.
    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user" and out.failed == 1 and out.done == 0
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.FAILED and got.error_kind == "criteria_unmet"
    assert json.loads(got.verify_json)["verdict"] == VERDICT_FAIL
    # Negative control: no verify hook still DONE (absent verifier must not kill output).
    plan2, _ = await _setup(store, ["compute"])
    out2 = await run_plan(plan2.id, store=store, deps=_deps(FakeRunner()), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed" and out2.done == 1


async def test_required_exchange_gap_stays_partial_in_live_and_persisted_plan(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="Revenue verification")
    objective = "Calculate net revenue and check the refund definition with the policy expert."
    step = new_step(
        plan_id=plan.id,
        seq=0,
        agent_name="compute",
        objective=objective,
        success_criteria=("Includes net revenue of 1080 USD",),
    )
    await store.add_steps([step])
    payload = {"exec_summary": "Net revenue is 1080 USD; the required peer check remains pending."}

    class _Runner:
        async def dispatch(self, tasks, sctx, rf, arf, ml):
            return [TaskResult.succeeded(task, payload, tokens_input=10, tokens_output=20) for task in tasks]

    class _VerifierRouter:
        async def invoke(self, agent_type, request):
            assert objective in request.messages[0].content
            return SimpleNamespace(content=json.dumps({"items": [{"status": "met"}, {"status": "unmet", "note": "The peer check remains pending."}]}))

    async def verify(step, payload):
        return await verify_step_output(
            _VerifierRouter(),
            objective=step.objective,
            deliverable=step.deliverable,
            criteria=step.success_criteria,
            digest=build_verify_digest(payload),
        )

    emit = _Emit()
    outcome = await run_plan(
        plan.id,
        store=store,
        deps=_deps(_Runner()),
        hooks=PlanHooks(emit=emit, verify=verify),
        caps=PlanCaps(),
        parent_token_budget=1000,
    )
    assert outcome.status == "completed"
    persisted = (await PlanStore(db_path).list_steps(plan.id))[0]
    assert persisted.status == StepStatus.DONE
    assert json.loads(persisted.payload_json) == payload
    assert json.loads(persisted.verify_json)["verdict"] == VERDICT_PARTIAL
    assert json.loads(persisted.verify_json)["items"][-1]["text"] == objective
    assert emit.named("step_done")[0]["verify"]["verdict"] == VERDICT_PARTIAL
    assert emit.named("step_done")[0]["verify"]["items"] == json.loads(persisted.verify_json)["items"]
    counts = emit.named("plan_completed")[0]["verify"]
    assert counts["partial"] == 1
    assert counts.get("pass", 0) == 0


@pytest.mark.asyncio
async def test_enforce_fail_marks_failed_with_full_ledger(db_path: str) -> None:
    """阶段 B(enforce): fail → FAILED(criteria_unmet), token/payload 全留 (review B3/C7)."""
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute"])
    emit = _Emit()
    hooks = PlanHooks(emit=emit, verify=_hook({"compute": _fail_outcome()}))
    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user" and out.failed == 1
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.FAILED
    assert got.error_kind == "criteria_unmet"
    assert "验收未达" in (got.error or "")
    assert got.tokens_input == 10 and got.tokens_output == 20  # Spent tokens must be recorded.
    assert "exec_summary" in (got.payload_json or "")  # Retain real output evidence.
    assert emit.named("step_failed")[0]["verify"]["unmet"] == ["含季度数字"]


@pytest.mark.asyncio
async def test_enforce_second_fail_degrades_to_done(db_path: str) -> None:
    """无进展降级 (review A5): 上一轮 criteria_unmet 的步再 fail → DONE+caveat, 且清旧错."""
    store = await _store(db_path)
    plan, steps = await _setup(store, ["compute"])
    hooks = PlanHooks(verify=_hook({"compute": _fail_outcome()}))
    # First round: FAILED(criteria_unmet).
    await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert (await store.list_steps(plan.id))[0].status == StepStatus.FAILED
    # Second round (re-approve retry): same verdict → degrade to DONE, clear error (review B4).
    out2 = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed" and out2.done == 1
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.DONE
    assert got.error is None and got.error_kind is None
    assert "验收两次未过" in json.loads(got.verify_json)["note"]


@pytest.mark.asyncio
async def test_no_hook_is_current_behavior(db_path: str) -> None:
    """hook=None: 现状回归——无 verify 字段/verify_json 不写 (flag-off 路径)."""
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute"])
    emit = _Emit()
    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=PlanHooks(emit=emit), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    got = (await store.list_steps(plan.id))[0]
    assert got.verify_json is None
    assert "verify" not in emit.named("step_done")[0]
    assert "verify" not in emit.named("plan_completed")[0]


@pytest.mark.asyncio
async def test_verify_runs_once_per_ok_step(db_path: str) -> None:
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute", "kb-analyst"])
    calls: list[str] = []
    hooks = PlanHooks(verify=_hook({}, calls))
    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    assert sorted(calls) == ["compute", "kb-analyst"]


@pytest.mark.asyncio
async def test_throwing_hook_degrades_to_skipped_not_crash(db_path: str) -> None:
    """review C8: gather 里 hook 抛错不得炸掉整批写回——兜成 skipped."""
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute", "kb-analyst"])

    async def boom(step, payload):
        if step.agent_name == "compute":
            raise RuntimeError("closure bug")
        return _pass_outcome()

    emit = _Emit()
    hooks = PlanHooks(emit=emit, verify=boom)
    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed" and out.done == 2  # Both steps written back.
    by_agent = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert json.loads(by_agent["compute"].verify_json)["verdict"] == VERDICT_SKIPPED
    assert json.loads(by_agent["kb-analyst"].verify_json)["verdict"] == VERDICT_PASS
    verify_counts = emit.named("plan_completed")[0]["verify"]
    assert verify_counts["skipped"] == 1 and verify_counts["pass"] == 1


@pytest.mark.asyncio
async def test_enforce_fail_isolates_downstream_same_run(db_path: str) -> None:
    """实现 review B1(P1): enforce fail 的被拒产出不得喂给同 run 下游——
    results[sid] 覆盖为 failed → 下游 _precondition BLOCKED(upstream_failed)."""
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    up = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="上游", success_criteria=("含数字",))
    down = new_step(plan_id=plan.id, seq=1, agent_name="kb-analyst", objective="下游", depends_on=(up.id,))
    levels = assign_levels([up, down])
    for st in (up, down):
        st.level = levels[st.id]
    await store.add_steps([up, down])

    dispatched: list[list] = []

    class _Runner(FakeRunner):
        async def dispatch(self, tasks, sctx, rf, arf, ml):
            dispatched.append(list(tasks))
            return await super().dispatch(tasks, sctx, rf, arf, ml)

    hooks = PlanHooks(verify=_hook({"compute": _fail_outcome()}))
    out = await run_plan(plan.id, store=store, deps=_deps(_Runner()), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user"
    by_agent = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert by_agent["compute"].status == StepStatus.FAILED
    # Downstream gated (upstream_failed), never consuming rejected output to become DONE.
    assert by_agent["kb-analyst"].status == StepStatus.BLOCKED
    assert by_agent["kb-analyst"].error_kind == "upstream_failed"
    # Never dispatched either.
    assert all(t.agent_name != "kb-analyst" for batch in dispatched for t in batch)


@pytest.mark.asyncio
async def test_stale_verify_cleared_when_hook_absent(db_path: str) -> None:
    """实现 review B4-A: enforce fail 后**不带 verify hook** 重跑 → 新 DONE 不得挂旧 fail 结论."""
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute"])
    hooks_on = PlanHooks(verify=_hook({"compute": _fail_outcome()}))
    await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=hooks_on, caps=PlanCaps(), parent_token_budget=1000)
    assert (await store.list_steps(plan.id))[0].verify_json is not None
    # Rerun without verifier (hook=None).
    out = await run_plan(plan.id, store=store, deps=_deps(FakeRunner()), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.DONE
    assert got.verify_json is None  # Stale fail cannot accuse new output.


class TestVerdictMetric:
    """标定埋点：shadow 阶段的 verdict 分布要能自动累计。

    PRD R1.3a 的 enforce 前置是「fail 假阴=0 @ N≥20」；此前 verdict 只落
    `plan_steps.verify_json`，没有聚合出口，「攒到多少了」得人翻库（S6 那次是手数的），
    而真实运行不可回放、漏统计补不回来。计数器只负责 N 与分布，假阴判定仍靠人审 fail。
    """

    def test_verdict_counter_accumulates_by_label(self, monkeypatch) -> None:
        monkeypatch.setenv("BOWHEAD_METRICS", "1")
        from bowhead_core.plan_driver import _tally_verify
        from bowhead_core.plan_verify import (
            VERDICT_FAIL,
            VERDICT_PASS,
            VERDICT_SKIPPED,
            VerifyOutcome,
        )

        from bowhead_core import metrics as m

        name = "bowhead_plan_verify_verdict_total"
        before = {tuple(sorted(lbl.items())): v for lbl, v in m.counter_items(name)}
        counts: dict[str, int] = {}
        for verdict in (VERDICT_PASS, VERDICT_PASS, VERDICT_FAIL, VERDICT_SKIPPED):
            _tally_verify(counts, VerifyOutcome(verdict))
        after = {tuple(sorted(lbl.items())): v for lbl, v in m.counter_items(name)}

        def delta(verdict: str) -> float:
            key = (("verdict", verdict),)
            return after.get(key, 0.0) - before.get(key, 0.0)

        assert delta("pass") == 2.0
        assert delta("fail") == 1.0
        assert delta("skipped") == 1.0
        # Instrumentation must not change existing counts accounting.
        assert counts["verify_pass"] == 2
        assert counts["verify_fail"] == 1

    def test_metric_failure_never_breaks_tally(self, monkeypatch) -> None:
        """metrics 出问题不许阻断业务（先例同款 except: pass）。"""
        import bowhead_core.metrics as m
        from bowhead_core.plan_driver import _tally_verify
        from bowhead_core.plan_verify import VERDICT_PASS, VerifyOutcome

        def boom(*a, **kw):
            raise RuntimeError("metrics down")

        monkeypatch.setattr(m, "inc", boom)
        counts: dict[str, int] = {}
        _tally_verify(counts, VerifyOutcome(VERDICT_PASS))  # No raise.
        assert counts["verify_pass"] == 1


class _FlakyRunner:
    """第一次派活按 `kinds` 失败,重派一律成功。记录每一轮派了谁。"""

    def __init__(self, kinds: dict[str, str]) -> None:
        self.kinds = kinds  # agent_name → bail kind
        self.rounds: list[list[str]] = []

    async def dispatch(self, tasks, sctx, rf, arf, ml):  # noqa: ANN001, ANN202
        from bowhead_core.agents.task_spec import TaskFailureError

        self.rounds.append([t.agent_name for t in tasks])
        first = len(self.rounds) == 1
        out = []
        for t in tasks:
            kind = self.kinds.get(t.agent_name)
            if first and kind:
                out.append(TaskResult.failed(t, TaskFailureError(kind, RuntimeError("BI 查询下载超时 60s"))))
            else:
                out.append(TaskResult.succeeded(t, {"exec_summary": f"{t.agent_name} ok"}, tokens_input=10, tokens_output=20))
        return out


@pytest.mark.asyncio
async def test_transient_is_retried_once_and_downstream_survives(db_path: str) -> None:
    """★transient 自动重试:一次数据库慢不该废掉整张图。

    真机 plan_ca30aa484f68(2026-08-19):BI 行级查询 60s 超时 → 2 步 transient,
    另 2 步被 upstream_failed 连坐 —— **5 步废 4 步**,而 `_VALID_BAIL_KINDS` 的
    契约原文写的就是「transient: tool 失败但 ws 真有数据 → **retry** 或显错」。

    钉三件:①真的重派了 ②重派成功后步是 DONE 而非 FAILED
    ③**下游没有被 upstream_failed 连坐**(这是 5 废 4 的放大器)。
    """
    store = await _store(db_path)
    # ⚠ Pass depends_on at new_step(): later assignment is not persisted, so both dispatch same round,
    # making downstream unaffected assertion vacuous (initial test did this; only mutation①
    # `rounds: [['compute','business-analyst']]` exposed it).
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    up = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="上游")
    down = new_step(plan_id=plan.id, seq=1, agent_name="business-analyst", objective="下游", depends_on=(up.id,))
    levels = assign_levels([up, down])
    for st in (up, down):
        st.level = levels[st.id]
    await store.add_steps([up, down])
    assert levels[down.id] == 1, "下游必须在 level 1, 否则连坐路径根本没被走到"
    runner = _FlakyRunner({"compute": "transient"})
    emit = _Emit()
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(emit=emit), caps=PlanCaps(), parent_token_budget=1000)

    assert len(runner.rounds) >= 2, f"transient 没有被重派: {runner.rounds}"
    assert runner.rounds[1] == ["compute"], f"重派的应当只有失败那步: {runner.rounds}"
    rows = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert rows["compute"].status == StepStatus.DONE
    assert rows["compute"].error_kind is None
    # ③ Downstream unaffected.
    assert rows["business-analyst"].status == StepStatus.DONE
    assert out.status == "completed" and out.failed == 0
    # First failure must not leak as step_failed (emits at writeback; retry occurs before writeback).
    assert not emit.named("step_failed")


@pytest.mark.asyncio
async def test_non_transient_kinds_are_not_retried(db_path: str) -> None:
    """★负对照:「做不到」类失败不许拿免死金牌 —— 重试一百次也一样。

    没有这条,`_RETRYABLE_KINDS` 被改成「什么都重试」时测试仍会全绿。
    """
    store = await _store(db_path)
    plan, _ = await _setup(store, ["kb-analyst"])
    runner = _FlakyRunner({"kb-analyst": "domain_mismatch"})
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert len(runner.rounds) == 1, f"domain_mismatch 不该被重派: {runner.rounds}"
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.FAILED and got.error_kind == "domain_mismatch"
    assert out.failed == 1


class _AlwaysTransientRunner:
    def __init__(self) -> None:
        self.rounds = 0

    async def dispatch(self, tasks, sctx, rf, arf, ml):  # noqa: ANN001, ANN202
        from bowhead_core.agents.task_spec import TaskFailureError

        self.rounds += 1
        return [TaskResult.failed(t, TaskFailureError("transient", RuntimeError("还是超时"))) for t in tasks]


@pytest.mark.asyncio
async def test_transient_retries_exactly_once_then_gives_up(db_path: str) -> None:
    """★只重试一次:再大也救不了「BI 真的挂了」那档,只会把一次失败变两次昂贵失败。"""
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute"])
    runner = _AlwaysTransientRunner()
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert runner.rounds == 2, f"应当恰好派两次(首派+一次重试),实际 {runner.rounds}"
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.FAILED and got.error_kind == "transient"
    assert out.failed == 1


@pytest.mark.asyncio
async def test_transient_retry_skipped_when_wallclock_exhausted(db_path: str) -> None:
    """★预算护栏:墙钟见底时不许重试。

    `_caps_breach` 是**每层之后**才查的(run_plan 主循环),重试发生在它上游 ——
    没有这道自查,一次重试能把 `max_wallclock_s` 冲破一整步,深图上尤其明显。
    """
    store = await _store(db_path)
    plan, _ = await _setup(store, ["compute"])
    # Move execution start far into past → negative remaining → guard must block retry.
    await store.mark_execution_started(plan.id, turn_id="t_x") if hasattr(store, "mark_execution_started") else None
    runner = _AlwaysTransientRunner()
    out = await run_plan(
        plan.id,
        store=store,
        deps=_deps(runner),
        hooks=PlanHooks(),
        caps=PlanCaps(max_wallclock_s=1),  # 1s budget exhausted after first dispatch.
        parent_token_budget=1000,
    )
    assert runner.rounds == 1, f"墙钟见底时不该重试,实际派了 {runner.rounds} 次"
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.FAILED and got.error_kind == "transient"
    assert out.failed == 1
