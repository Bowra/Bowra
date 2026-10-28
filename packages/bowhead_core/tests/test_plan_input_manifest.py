"""批 3 S2: input_manifest 写点/分桶/verifier 消费/caveats (batch3-replan-design D-E).

UT 纪律(review R2-F10): manifest 断言穿真 run_plan 产生, 禁 fixture 预置——
「测试替调用方补齐了它不做的事」前车。verify hook 收到的 step 对象必须已带
manifest(内存回填铁断言)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents.task_spec import TaskFailureError, TaskResult
from bowhead_core.plan_driver import PlanCaps, PlanDeps, PlanHooks, run_plan
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStore, StepStatus, new_step
from bowhead_core.plan_verify import VERDICT_PASS, VerifyOutcome, build_verify_digest

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeSctx:
    user_id: str = "u"
    workspace_id: str = "w"
    turn_id: str = "t_run"
    session_id: str = "s"


class Runner:
    def __init__(self, behavior: dict[str, tuple[str, Any]] | None = None) -> None:
        self.behavior = behavior or {}

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        out = []
        for t in tasks:
            beh = self.behavior.get(t.agent_name, ("ok", {"exec_summary": f"{t.agent_name} ok"}))
            if beh[0] == "ok":
                out.append(TaskResult.succeeded(t, beh[1], tokens_input=1, tokens_output=2))
            else:
                out.append(TaskResult.failed(t, TaskFailureError(beh[1])))
        return out


def _deps(runner) -> PlanDeps:
    return PlanDeps(
        sctx=FakeSctx(),
        runtime_factory=lambda c: c,
        agent_runner_fn=None,
        manifest_lookup=lambda name: object(),
        runner=runner,
    )


async def _store(db_path: str) -> PlanStore:
    s = PlanStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


async def _setup(store: PlanStore, specs: list[tuple[str, list[int]]], *, exclude: set[int] = frozenset()):
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="M")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}", included=(i not in exclude)) for i, (a, _d) in enumerate(specs)]
    for st, (_a, dep_idx) in zip(steps, specs, strict=True):
        st.depends_on = tuple(steps[d].id for d in dep_idx)
    levels = assign_levels(steps)
    for st in steps:
        st.level = levels[st.id]
    await store.add_steps(steps)
    return plan, steps


def _mani(step) -> dict | None:
    raw = step.input_manifest_json
    return json.loads(raw) if raw else None


@pytest.mark.asyncio
async def test_manifest_full_receive_and_root_none(db_path: str) -> None:
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    out = await run_plan(plan.id, store=store, deps=_deps(Runner()), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert _mani(by_id[steps[0].id]) is None  # No dependencies: no noise.
    m = _mani(by_id[steps[1].id])
    assert m == {
        "expected": [steps[0].id],
        "received": [steps[0].id],
        "degraded_inputs": [],
        "excluded_inputs": [],
    }


@pytest.mark.asyncio
async def test_manifest_degraded_bucket_on_failed_upstream(db_path: str) -> None:
    """partial-ok: C(dep A,B), A 失败 B 成功 → C 照跑, manifest 记 A 为 degraded."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("kb-analyst", []), ("financial-analyst", [0, 1])])
    runner = Runner({"compute": ("fail", "boom")})
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user"  # A fails → blocked plan, but C ran.
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[steps[2].id].status == StepStatus.DONE
    m = _mani(by_id[steps[2].id])
    assert m is not None
    assert m["received"] == [steps[1].id]
    assert m["degraded_inputs"] == [steps[0].id]
    assert m["excluded_inputs"] == []


@pytest.mark.asyncio
async def test_manifest_excluded_bucket_on_user_excluded_upstream(db_path: str) -> None:
    """用户勾掉上游 A → C 的 manifest 把 A 记 excluded(不是 degraded, review R2-F9)."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("kb-analyst", []), ("financial-analyst", [0, 1])], exclude={0})
    out = await run_plan(plan.id, store=store, deps=_deps(Runner()), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    m = _mani(by_id[steps[2].id])
    assert m is not None
    assert m["excluded_inputs"] == [steps[0].id]
    assert m["degraded_inputs"] == []


@pytest.mark.asyncio
async def test_verify_hook_sees_manifest_in_memory_iron(db_path: str) -> None:
    """铁断言(R1-F3/R2-F10 穿闸): verify hook 拿到的 step 对象已带本轮 manifest."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("kb-analyst", []), ("financial-analyst", [0, 1])])
    seen: dict[str, str | None] = {}

    async def _vhook(step, payload):
        seen[step.agent_name] = step.input_manifest_json
        return VerifyOutcome(VERDICT_PASS)

    runner = Runner({"compute": ("fail", "boom")})
    await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(verify=_vhook), caps=PlanCaps(), parent_token_budget=1000)
    raw = seen.get("financial-analyst")
    assert raw, "verify hook 必须在内存对象上读到 manifest(只写 DB=闸外)"
    m = json.loads(raw)
    assert m["degraded_inputs"] == [steps[0].id]


@pytest.mark.asyncio
async def test_manifest_not_written_when_flag_off(db_path: str, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_REPLAN", "0")
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    await run_plan(plan.id, store=store, deps=_deps(Runner()), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[steps[1].id].input_manifest_json is None


def test_digest_includes_manifest_and_minimal_core_keeps_it() -> None:
    payload = {"exec_summary": "x" * 50, "kpis": [{"label": "a", "value": 1}]}
    mani = {"expected": ["ps_a", "ps_b"], "received": ["ps_b"], "degraded_inputs": ["ps_a"], "excluded_inputs": []}
    d = json.loads(build_verify_digest(payload, input_manifest=mani))
    assert d["input_manifest"]["expected_upstream"] == 2
    assert d["input_manifest"]["received_upstream"] == 1
    assert d["input_manifest"]["missing_failed_upstream"] == ["ps_a"]
    # Minimal core always retains manifest (R1-F10), even when tiny budget forces core fallback.
    big = {
        "exec_summary": "长" * 700,
        "insights": [{"title": "t" * 80, "thesis": "s" * 160, "narrative": "n" * 200}] * 10,
        "kpis": [{"label": "k" * 30, "value": "v" * 30}] * 16,
        "ground_facts": ["g" * 160] * 16,
        "fallback_markdown": "m" * 3000,
    }
    core = json.loads(build_verify_digest(big, input_manifest=mani, budget_chars=600))
    assert core.get("_truncated") is True
    assert core["input_manifest"]["missing_failed_upstream"] == ["ps_a"]


def test_digest_excluded_only_manifest_has_no_missing_failed() -> None:
    """excluded-only 缺口不出「missing_failed」——用户意图不进 verifier 指控面."""
    mani = {"expected": ["ps_a", "ps_b"], "received": ["ps_b"], "degraded_inputs": [], "excluded_inputs": ["ps_a"]}
    d = json.loads(build_verify_digest({"exec_summary": "x"}, input_manifest=mani))
    assert "missing_failed_upstream" not in d["input_manifest"]


def test_gap_caveats_degraded_and_excluded_buckets() -> None:
    from bowhead_core.agents.lead_orchestrator import _plan_report_gap_caveats

    @dataclass
    class S:
        id: str
        agent_name: str
        status: str
        verify_json: str | None = None
        input_manifest_json: str | None = None
        error_kind: str | None = None

    up_fail = S("ps_up1", "compute", "failed")
    up_skip = S("ps_up2", "kb-analyst", "skipped")
    done_degraded = S(
        "ps_d1",
        "financial-analyst",
        "done",
        input_manifest_json=json.dumps({"expected": ["ps_up1"], "received": [], "degraded_inputs": ["ps_up1"], "excluded_inputs": []}),
    )
    done_excluded_only = S(
        "ps_d2",
        "consultant",
        "done",
        input_manifest_json=json.dumps({"expected": ["ps_up2"], "received": [], "degraded_inputs": [], "excluded_inputs": ["ps_up2"]}),
    )
    dead = S("ps_dead", "compute", "superseded")
    out = _plan_report_gap_caveats([up_fail, up_skip, done_degraded, done_excluded_only, dead])
    joined = "\n".join(out)
    assert "缺少上游「compute」的输入" in joined  # degraded → caveat
    assert "consultant」这步执行时缺少" not in joined  # excluded-only → no caveat.
    assert "superseded" not in joined and "ps_dead" not in joined  # Dead rows excluded from caveats.
    assert any("compute」这步执行失败" in x for x in out)  # Failed step itself still counted.
