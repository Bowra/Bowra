"""批 3 store 面：supersede_step/replan_steps 三重 CAS + SUPERSEDED 终态语义.

设计=batch3-replan-design §1 D-B/D-C（review R1-F1/R1-F5/R2-F3 的回归钉）：
- CAS-1 plans 行（blocked ∧ execution_turn_id IS NULL ∧ rev OCC）
- CAS-2 旧步行（failed|blocked 才可 supersede；双击第二发拒）
- SUPERSEDED 不可转出（update_step_status SQL 级守卫）
- resume_level / 下游复位谓词
"""

from __future__ import annotations

import pytest
from bowhead_core.plan_store import (
    PlanStore,
    StepOrigin,
    StepStatus,
    new_step,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _mk_plan(store: PlanStore, *, statuses: list[str]) -> tuple[str, list[str]]:
    plan = await store.create_plan(
        session_id="s_t",
        workspace_id="ws_t",
        user_id="u_t",
        title="t",
    )
    steps = []
    for i in range(len(statuses)):
        s = new_step(plan_id=plan.id, seq=i, agent_name=f"agent{i}", objective=f"o{i}", level=i)
        steps.append(s)
    await store.add_steps(steps)
    for s, st in zip(steps, statuses, strict=True):
        if st != StepStatus.PENDING:
            await store.update_step_status(s.id, status=st)
    return plan.id, [s.id for s in steps]


async def _to_blocked(store: PlanStore, plan_id: str) -> None:
    await store.update_plan_status(plan_id, "running")
    await store.update_plan_status(plan_id, "blocked")


def _revised(plan_id: str, *, seq: int = 99, level: int = 0) -> object:
    return new_step(
        plan_id=plan_id,
        seq=seq,
        agent_name="agent-rev",
        objective="修订后的目标",
        success_criteria=("含具体数值",),
        level=level,
        origin=StepOrigin.REVISION,
    )


async def test_supersede_happy_path(tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["done", "failed", "blocked"])
    # Downstream blocked(upstream_failed) row.
    await store.update_step_status(ids[2], status="blocked", error="所有上游步未成功", error_kind="upstream_failed")
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    rev = plan.current_rev

    new = _revised(plan_id, level=1)
    ok = await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[1],
        new_step=new,
        downstream_reset_ids=[ids[2]],
        expected_rev=rev,
    )
    assert ok

    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[ids[1]].status == StepStatus.SUPERSEDED
    assert by_id[ids[1]].superseded_by == new.id
    assert by_id[new.id].status == StepStatus.PENDING
    assert by_id[new.id].origin == StepOrigin.REVISION
    assert by_id[new.id].success_criteria == ("含具体数值",)
    # Reset downstream.
    assert by_id[ids[2]].status == StepStatus.PENDING
    assert by_id[ids[2]].error is None and by_id[ids[2]].error_kind is None
    after = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    assert after.current_rev == rev + 1
    assert after.replan_count == 1


async def test_supersede_cas_rejects_wrong_rev_and_double_confirm(tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["failed"])
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")

    # Wrong rev (OCC): reject, zero changes.
    assert not await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[0],
        new_step=_revised(plan_id),
        downstream_reset_ids=[],
        expected_rev=plan.current_rev + 7,
    )
    assert len(await store.list_steps(plan_id)) == 1

    # First succeeds.
    n1 = _revised(plan_id)
    assert await store.supersede_step(plan_id=plan_id, old_step_id=ids[0], new_step=n1, downstream_reset_ids=[], expected_rev=plan.current_rev)
    # Double click: old already superseded →CAS-2 rejects, even with changed rev.
    assert not await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[0],
        new_step=_revised(plan_id, seq=100),
        downstream_reset_ids=[],
        expected_rev=plan.current_rev + 1,
    )
    steps = await store.list_steps(plan_id)
    assert len(steps) == 2  # No orphan new row.


async def test_supersede_cas_rejects_running_plan_and_claim_fence(tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["failed"])
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")

    # Concurrent approve wins claim (status=running + nonempty execution_turn_id).
    assert await store.try_claim_running(plan_id, "t_claim")
    assert not await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[0],
        new_step=_revised(plan_id),
        downstream_reset_ids=[],
        expected_rev=plan.current_rev,
    )
    # Back to blocked but occupied execution_turn_id (worker tail window) →still reject.
    await store.update_plan_status(plan_id, "blocked")
    assert not await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[0],
        new_step=_revised(plan_id),
        downstream_reset_ids=[],
        expected_rev=plan.current_rev,
    )
    # Permit after tail release.
    assert await store.release_execution_turn(plan_id, "t_claim")
    assert await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[0],
        new_step=_revised(plan_id),
        downstream_reset_ids=[],
        expected_rev=plan.current_rev,
    )


async def test_superseded_is_sticky_and_resume_level_skips(tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["failed", "pending"])
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    new = _revised(plan_id, level=0)
    assert await store.supersede_step(plan_id=plan_id, old_step_id=ids[0], new_step=new, downstream_reset_ids=[], expected_rev=plan.current_rev)
    # Terminal guard: any write, including DONE/preflight BLOCKED, must never leave superseded.
    assert not await store.update_step_status(ids[0], status="done", clear_error=True)
    assert not await store.update_step_status(ids[0], status="blocked", error="x", error_kind="needs_user_input")
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[ids[0]].status == StepStatus.SUPERSEDED
    # resume_level never anchors dead row: live pending level0 replacement →0; after done,
    # only level1 pending remains →1 (superseded level0 no longer anchors).
    assert await store.resume_level(plan_id) == 0
    await store.update_step_status(new.id, status="done")
    assert await store.resume_level(plan_id) == 1


async def test_downstream_reset_predicate_only_upstream_failed(tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["failed", "blocked", "blocked"])
    # ids[1]=upstream_failed resets; ids[2]=needs_user_input must not reset.
    await store.update_step_status(ids[1], status="blocked", error="上游", error_kind="upstream_failed")
    await store.update_step_status(ids[2], status="blocked", error="等选择", error_kind="needs_user_input")
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    assert await store.supersede_step(
        plan_id=plan_id,
        old_step_id=ids[0],
        new_step=_revised(plan_id),
        downstream_reset_ids=[ids[1], ids[2]],
        expected_rev=plan.current_rev,
    )
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[ids[1]].status == StepStatus.PENDING
    assert by_id[ids[2]].status == StepStatus.BLOCKED  # Predicate: non-upstream_failed unchanged.
    assert by_id[ids[2]].error_kind == "needs_user_input"


async def test_replan_steps_generation_sentinel_and_level_updates(tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["done", "failed", "blocked"])
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    rev = plan.current_rev

    n1 = new_step(plan_id=plan_id, seq=10, agent_name="agent-n1", objective="新步", level=1, depends_on=(ids[0],), origin=StepOrigin.REPLAN)
    ok = await store.replan_steps(
        plan_id=plan_id,
        superseded_ids=[ids[1], ids[2]],
        new_steps=[n1],
        level_updates={ids[0]: 0, n1.id: 1},
        expected_rev=rev,
    )
    assert ok
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    sentinel = f"replan:rev{rev + 1}"
    assert by_id[ids[1]].status == StepStatus.SUPERSEDED
    assert by_id[ids[1]].superseded_by == sentinel
    assert by_id[ids[2]].superseded_by == sentinel
    assert by_id[ids[0]].status == StepStatus.DONE  # done unchanged.
    assert by_id[n1.id].origin == StepOrigin.REPLAN
    after = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    assert after.replan_count == 1

    # Cannot supersede done: targeted replan →CAS rejection + full rollback.
    plan2 = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    n2 = new_step(plan_id=plan_id, seq=11, agent_name="agent-n2", objective="x", level=0)
    assert not await store.replan_steps(
        plan_id=plan_id,
        superseded_ids=[ids[0]],
        new_steps=[n2],
        level_updates={},
        expected_rev=plan2.current_rev,
    )
    assert len(await store.list_steps(plan_id)) == 4  # n2 not inserted.


async def test_supersede_mid_transaction_exception_zero_change(tmp_path, monkeypatch) -> None:
    """设计 §3: 事务中途抛错 = 零变更(实现 review P2-4 缺席钉)."""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["failed"])
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")

    # Fail new-row insertion (third stage): same ID as old →PK IntegrityError.
    bad = _revised(plan_id)
    bad.id = ids[0]
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):
        await store.supersede_step(plan_id=plan_id, old_step_id=ids[0], new_step=bad, downstream_reset_ids=[], expected_rev=plan.current_rev)
    # Zero changes: all CAS-1/CAS-2 writes roll back.
    after = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    assert after.current_rev == plan.current_rev
    assert after.replan_count == 0
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[ids[0]].status == StepStatus.FAILED
    assert by_id[ids[0]].superseded_by is None


async def test_replan_rejects_skipped_rows(tmp_path) -> None:
    """实现 review P2-1: skipped=用户意图行, replan CAS-2 必拒(整体回滚)."""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_id, ids = await _mk_plan(store, statuses=["failed", "skipped"])
    await _to_blocked(store, plan_id)
    plan = await store.get_plan(plan_id, user_id="u_t", workspace_id="ws_t")
    n1 = _revised(plan_id)
    assert not await store.replan_steps(
        plan_id=plan_id,
        superseded_ids=[ids[0], ids[1]],  # ids[1]=skipped, race-window shape.
        new_steps=[n1],
        level_updates={},
        expected_rev=plan.current_rev,
    )
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[ids[0]].status == StepStatus.FAILED  # Full rollback.
    assert by_id[ids[1]].status == StepStatus.SKIPPED
    assert len(by_id) == 2
