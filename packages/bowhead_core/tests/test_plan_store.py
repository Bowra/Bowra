"""PlanStore unit tests (Planning Mode M1)."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest
from bowhead_core.plan_store import (
    DeliverableKind,
    PlanStatus,
    PlanStore,
    StepStatus,
    new_step,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


async def _store(db_path: str) -> PlanStore:
    store = PlanStore(db_path)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_create_and_get(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        title="X 公司尽调",
        objective="尽职调查",
    )
    assert plan.id.startswith("plan_")
    assert plan.status == PlanStatus.PROPOSED
    assert plan.deliverable_kind == DeliverableKind.REPORT
    assert plan.current_rev == 1

    got = await store.get_plan(plan.id, user_id="u1", workspace_id="w1")
    assert got is not None
    assert got.title == "X 公司尽调"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "duplicate_step", "cancel"])
async def test_proposal_is_visible_only_with_all_steps(db_path: str, monkeypatch, outcome: str) -> None:
    store = await _store(db_path)
    first = new_step(plan_id="", seq=0, agent_name="compute", objective="Read revenue")
    second = new_step(plan_id="", seq=1, agent_name="compute", objective="Compare profit", depends_on=(first.id,))
    if outcome == "duplicate_step":
        second.id = first.id
    inserted, release = asyncio.Event(), asyncio.Event()
    original = store._insert_steps

    async def insert_steps(db, steps, ts):
        await original(db, steps[:1], ts)
        inserted.set()
        await release.wait()
        await original(db, steps[1:], ts)

    monkeypatch.setattr(store, "_insert_steps", insert_steps)
    task = asyncio.create_task(
        store.create_plan(
            session_id="s", workspace_id="w", user_id="u", title="Profit review", spec_json='{"scope":"FY2026"}', initial_steps=[first, second]
        )
    )
    try:
        await asyncio.wait_for(inserted.wait(), timeout=5)
        assert await store.list_plans_for_user(user_id="u", workspace_id="w") == []
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM plan_steps").fetchone() == (0,)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == "duplicate_step":
                with pytest.raises(sqlite3.IntegrityError):
                    await task
            else:
                plan = await task
                saved, steps = await store.get_plan_with_steps(plan.id, user_id="u", workspace_id="w")
                assert saved.spec_json == '{"scope":"FY2026"}' and saved.current_rev == 1
                assert [step.id for step in steps] == [first.id, second.id]
                assert all(step.plan_id == plan.id for step in steps)
                assert steps[1].depends_on == (first.id,)
                assert first.plan_id == second.plan_id == ""
                assert await store.try_claim_running(plan.id, "t_execution", expected_rev=saved.current_rev)
        if outcome != "complete":
            assert await store.list_plans_for_user(user_id="u", workspace_id="w") == []
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM plan_steps").fetchone() == (0,)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_proposal_cannot_reassign_an_existing_plans_steps(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="Original")
    step = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="Read revenue")
    await store.add_steps([step])
    with pytest.raises(ValueError, match="unbound"):
        await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="Replacement", initial_steps=[step])
    assert [item.id for item in await store.list_plans_for_user(user_id="u", workspace_id="w")] == [plan.id]
    assert (await store.list_steps(plan.id))[0].id == step.id


@pytest.mark.asyncio
async def test_get_plan_tenant_isolation(db_path: str) -> None:
    """review P1-K: 别的 user / workspace 拿不到这个 plan."""
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        title="t",
    )
    assert await store.get_plan(plan.id, user_id="u2", workspace_id="w1") is None
    assert await store.get_plan(plan.id, user_id="u1", workspace_id="w2") is None
    assert await store.get_plan(plan.id, user_id="u1", workspace_id="w1") is not None


@pytest.mark.asyncio
async def test_list_for_user_scoped(db_path: str) -> None:
    store = await _store(db_path)
    await store.create_plan(session_id="s", workspace_id="w1", user_id="u1", title="a")
    await store.create_plan(session_id="s", workspace_id="w1", user_id="u1", title="b")
    await store.create_plan(session_id="s", workspace_id="w1", user_id="u2", title="c")
    mine = await store.list_plans_for_user(user_id="u1", workspace_id="w1")
    assert {p.title for p in mine} == {"a", "b"}


@pytest.mark.asyncio
async def test_invalid_deliverable_kind_rejected(db_path: str) -> None:
    store = await _store(db_path)
    with pytest.raises(ValueError, match="deliverable_kind"):
        await store.create_plan(
            session_id="s",
            workspace_id="w",
            user_id="u",
            title="t",
            deliverable_kind="bogus",
        )


@pytest.mark.asyncio
async def test_answer_deliverable_kind_accepted(db_path: str) -> None:
    """delivery-surface C3b: ANSWER（答案在聊天里）过校验并原样持久化。"""
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="查今年利润前三的品牌",
        objective="分两步：①用 BI 查今年利润前三的品牌 ②一句话总结。不要写报告。",
        deliverable_kind=DeliverableKind.ANSWER,
    )
    assert plan.deliverable_kind == DeliverableKind.ANSWER
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None
    assert got.deliverable_kind == "answer"


@pytest.mark.asyncio
async def test_update_plan_status(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    assert await store.update_plan_status(plan.id, PlanStatus.RUNNING)
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None and got.status == PlanStatus.RUNNING
    with pytest.raises(ValueError, match="plan status"):
        await store.update_plan_status(plan.id, "bogus")


@pytest.mark.asyncio
async def test_terminal_status_is_sticky(db_path: str) -> None:
    """CTO P2-10: completed/failed/cancelled 不可转出 (durable-cancel 依赖)."""
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    assert await store.update_plan_status(plan.id, PlanStatus.CANCELLED)
    # cancelled →running must be rejected.
    assert await store.update_plan_status(plan.id, PlanStatus.RUNNING) is False
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None and got.status == PlanStatus.CANCELLED


@pytest.mark.asyncio
async def test_add_and_list_steps_ordered(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    s_legal = new_step(plan_id=plan.id, seq=1, level=0, agent_name="compliance-analyst", objective="法务", needs_corpus=True)
    s_fin = new_step(plan_id=plan.id, seq=2, level=0, agent_name="financial-analyst", objective="审计", needs_corpus=True)
    s_sum = new_step(plan_id=plan.id, seq=3, level=1, agent_name="canvas-writer", objective="汇总", depends_on=(s_legal.id, s_fin.id))
    await store.add_steps([s_sum, s_legal, s_fin])  # Inserted out of order.

    steps = await store.list_steps(plan.id)
    # Sort by level,seq: level0(seq1,seq2), then level1.
    assert [s.objective for s in steps] == ["法务", "审计", "汇总"]
    assert steps[0].needs_corpus is True
    assert steps[2].depends_on == (s_legal.id, s_fin.id)
    assert steps[2].level == 1


@pytest.mark.asyncio
async def test_update_step_status_writeback(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    s = new_step(plan_id=plan.id, seq=1, agent_name="compute", objective="数据")
    await store.add_steps([s])
    ok = await store.update_step_status(
        s.id,
        status=StepStatus.DONE,
        turn_id="t_abc",
        output_section_ids=("sec_1", "sec_2"),
        result_digest="找到3个风险",
        payload_json='{"exec_summary":"3 risks","insights":[]}',
        tokens_input=1200,
        tokens_output=800,
    )
    assert ok
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.DONE
    assert got.turn_id == "t_abc"
    assert got.output_section_ids == ("sec_1", "sec_2")
    assert got.result_digest == "找到3个风险"
    # Persist full payload (reports/retry downstream need fidelity, not summary).
    assert got.payload_json == '{"exec_summary":"3 risks","insights":[]}'
    assert got.tokens_input == 1200 and got.tokens_output == 800
    with pytest.raises(ValueError, match="step status"):
        await store.update_step_status(s.id, status="bogus")


@pytest.mark.asyncio
async def test_resume_level(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    l0a = new_step(plan_id=plan.id, seq=1, level=0, agent_name="a")
    l0b = new_step(plan_id=plan.id, seq=2, level=0, agent_name="b")
    l1 = new_step(plan_id=plan.id, seq=3, level=1, agent_name="c")
    await store.add_steps([l0a, l0b, l1])

    # All pending →resume starts level 0.
    assert await store.resume_level(plan.id) == 0
    # level0 all done →resume starts level 1.
    await store.update_step_status(l0a.id, status=StepStatus.DONE)
    await store.update_step_status(l0b.id, status=StepStatus.SKIPPED)  # skipped also terminal.
    assert await store.resume_level(plan.id) == 1
    # All terminal →None (can summarize).
    await store.update_step_status(l1.id, status=StepStatus.DONE)
    assert await store.resume_level(plan.id) is None


@pytest.mark.asyncio
async def test_replace_steps(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    await store.add_steps([new_step(plan_id=plan.id, seq=1, agent_name="a")])
    assert await store.replace_steps(
        plan.id,
        [
            new_step(plan_id=plan.id, seq=1, agent_name="x"),
            new_step(plan_id=plan.id, seq=2, agent_name="y"),
        ],
        expected_rev=plan.current_rev,
    )
    steps = await store.list_steps(plan.id)
    assert [s.agent_name for s in steps] == ["x", "y"]
    saved = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert saved.current_rev == plan.current_rev + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["edit", "approve"])
async def test_edit_and_approve_cannot_use_different_plan_versions(db_path: str, winner: str) -> None:
    """Two request snapshots; force both database orderings without timing sleeps."""
    store = await _store(db_path)
    other = PlanStore(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    original = new_step(plan_id=plan.id, seq=0, agent_name="original")
    revised = new_step(plan_id=plan.id, seq=0, agent_name="revised")
    await store.add_steps([original])
    if winner == "edit":
        assert await store.replace_steps(plan.id, [revised], expected_rev=plan.current_rev)
        assert not await other.try_claim_running(plan.id, "t_stale", expected_rev=plan.current_rev)
        assert not await other.replace_steps(plan.id, [original], expected_rev=plan.current_rev)
        assert await other.try_claim_running(plan.id, "t_new", expected_rev=plan.current_rev + 1)
    else:
        assert await other.try_claim_running(plan.id, "t_original", expected_rev=plan.current_rev)
        assert not await store.replace_steps(plan.id, [revised], expected_rev=plan.current_rev)
    saved = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert saved.status == PlanStatus.RUNNING
    assert saved.current_rev == plan.current_rev + (winner == "edit")
    assert saved.execution_turn_id == ("t_new" if winner == "edit" else "t_original")
    assert [s.id for s in await store.list_steps(plan.id)] == [revised.id if winner == "edit" else original.id]


@pytest.mark.asyncio
async def test_failed_edit_keeps_original_steps_and_revision(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    original = new_step(plan_id=plan.id, seq=0, agent_name="original")
    revised = new_step(plan_id=plan.id, seq=0, agent_name="revised")
    await store.add_steps([original])
    with pytest.raises(sqlite3.IntegrityError):
        await store.replace_steps(plan.id, [revised, revised], expected_rev=plan.current_rev)
    saved = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert saved.current_rev == plan.current_rev
    assert [s.id for s in await store.list_steps(plan.id)] == [original.id]


@pytest.mark.asyncio
async def test_set_execution_started_at_resets_per_run(db_path: str) -> None:
    """2026-07 语义修正: 每次 run_plan 重置 wallclock 锚(cap = 本执行 turn 的预算)。

    原 `IS NULL` 只设一次的语义,让隔天补料重试的 plan 因老锚点瞬间 wallclock
    越界 → 终态 FAILED,重试永不可能成功(见 test_plan_driver
    test_rerun_resets_wallclock_anchor)。
    """
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    await store.set_execution_started_at(plan.id)
    first = (await store.get_plan(plan.id, user_id="u", workspace_id="w")).execution_started_at
    assert first is not None
    await store.set_execution_started_at(plan.id)  # Retry resets to new anchor.
    second = (await store.get_plan(plan.id, user_id="u", workspace_id="w")).execution_started_at
    assert second is not None and second >= first


@pytest.mark.asyncio
async def test_try_claim_running_prevents_double_run(db_path: str) -> None:
    """CTO P1-5: 原子认领, 防 approve 双击 / approve+retry 并发双跑."""
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    # proposed →first claim succeeds.
    assert await store.try_claim_running(plan.id, "t_claim_1") is True
    # Already running →second rejected.
    assert await store.try_claim_running(plan.id, "t_claim_2") is False
    # blocked →reclaimable (per-step retry).
    await store.update_plan_status(plan.id, PlanStatus.BLOCKED)
    # The old Turn can still be composing its terminal tail. BLOCKED alone is
    # not permission for a new worker to overwrite the exact execution token.
    assert await store.try_claim_running(plan.id, "t_claim_3") is False
    assert await store.release_execution_turn(plan.id, "t_claim_1") is True
    assert await store.try_claim_running(plan.id, "t_claim_3") is True
    # Terminal →reject.
    await store.update_plan_status(plan.id, PlanStatus.COMPLETED)
    assert await store.try_claim_running(plan.id, "t_claim_4") is False


@pytest.mark.asyncio
async def test_bind_execution_turn_only_revalidates_exact_token(
    db_path: str,
) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="exact owner",
    )
    assert await store.try_claim_running(plan.id, "t_owner")
    assert await store.bind_execution_turn(plan.id, "t_owner")
    assert not await store.bind_execution_turn(plan.id, "t_stale")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET execution_turn_id=NULL WHERE id=?",
            (plan.id,),
        )
        connection.commit()
    assert not await store.bind_execution_turn(plan.id, "t_owner")


@pytest.mark.asyncio
async def test_execution_turn_mapping_supports_cross_process_cancel(
    db_path: str,
) -> None:
    owner_store = await _store(db_path)
    remote_store = await _store(db_path)
    plan = await owner_store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="t",
    )
    assert await owner_store.try_claim_running(plan.id, "t_plan_exec")

    assert await remote_store.cancel_execution_for_turn("t_plan_exec")
    got = await owner_store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None
    assert got.status == PlanStatus.CANCELLED
    assert got.execution_turn_id == "t_plan_exec"


@pytest.mark.asyncio
async def test_execution_claim_status_requires_exact_turn_token(
    db_path: str,
) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="terminal tail owner",
    )
    assert await store.try_claim_running(plan.id, "t_terminal_owner")
    assert await store.update_plan_status(plan.id, PlanStatus.COMPLETED)

    assert await store.get_execution_claim_status(plan.id, "t_terminal_owner") == PlanStatus.COMPLETED
    assert await store.get_execution_claim_status(plan.id, "t_stale_owner") is None


@pytest.mark.asyncio
async def test_release_execution_claim_clears_failed_spawn_identity(
    db_path: str,
) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="t",
    )
    assert await store.try_claim_running(plan.id, "t_spawn_failed")

    assert await store.release_execution_claim(plan.id, "t_spawn_failed")
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None
    assert got.status == PlanStatus.BLOCKED
    assert got.execution_turn_id is None


@pytest.mark.asyncio
async def test_release_dynamic_plan_execution_by_exact_turn(db_path: str) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="auto plan",
    )
    assert await store.try_claim_running(plan.id, "t_auto")
    assert await store.update_plan_status(plan.id, PlanStatus.BLOCKED)

    assert await store.release_execution_turn_for_turn("t_auto")
    assert not await store.release_execution_turn_for_turn("t_stale")
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None
    assert got.execution_turn_id is None


@pytest.mark.asyncio
async def test_fresh_atomic_claim_is_not_recovered_and_old_request_is_fenced(
    db_path: str,
) -> None:
    owner_store = await _store(db_path)
    recovery_store = await _store(db_path)
    plan = await owner_store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="claim window",
    )

    assert await owner_store.try_claim_running(plan.id, "t_old_claim")
    assert await recovery_store.recover_orphans() == (0, 0)
    claimed = await owner_store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert claimed is not None
    assert claimed.status == PlanStatus.RUNNING
    assert claimed.execution_turn_id == "t_old_claim"

    # Once the publication grace is genuinely stale it is recoverable.  A
    # later approval gets a different Turn token, so the delayed old request
    # cannot release the new claim.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", plan.id),
        )
        connection.commit()
    assert await recovery_store.recover_orphans() == (1, 0)
    assert await owner_store.try_claim_running(plan.id, "t_new_claim")
    assert not await owner_store.release_execution_claim(plan.id, "t_old_claim")
    reclaimed = await owner_store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert reclaimed is not None
    assert reclaimed.status == PlanStatus.RUNNING
    assert reclaimed.execution_turn_id == "t_new_claim"


@pytest.mark.asyncio
async def test_recover_orphans_preserves_another_gateway_live_lease(
    db_path: str,
) -> None:
    owner_store = await _store(db_path)
    new_gateway_store = await _store(db_path)
    plan = await owner_store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="rolling deploy",
    )
    assert await owner_store.try_claim_running(plan.id, "t_live_owner")
    step = new_step(plan_id=plan.id, seq=1, agent_name="a")
    await owner_store.add_steps([step])
    await owner_store.update_step_status(step.id, status=StepStatus.RUNNING)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE chat_turn_spawn_dispatch(turn_id TEXT PRIMARY KEY,state TEXT NOT NULL,lease_expires_at TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,state,lease_expires_at) VALUES (?,'spawned',?)",
            ("t_live_owner", "2099-01-01T00:00:00+00:00"),
        )
        connection.commit()

    assert await new_gateway_store.recover_orphans() == (0, 0)
    got = await owner_store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None and got.status == PlanStatus.RUNNING
    [got_step] = await owner_store.list_steps(plan.id)
    assert got_step.status == StepStatus.RUNNING


@pytest.mark.asyncio
async def test_recover_orphans_rejects_partial_terminal_schema(
    db_path: str,
) -> None:
    from bowhead_core.session_store import SessionProtocolManagedError

    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="partial terminal schema",
    )
    assert await store.try_claim_running(plan.id, "t_partial_terminal")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE turns(id TEXT PRIMARY KEY,terminal_protocol_version INTEGER)",
        )
        connection.execute(
            "INSERT INTO turns(id,terminal_protocol_version) VALUES ('t_partial_terminal',1)",
        )
        connection.execute(
            "UPDATE plans SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (plan.id,),
        )
        connection.commit()

    with pytest.raises(
        SessionProtocolManagedError,
        match="terminal protocol schema is partial",
    ):
        await store.recover_orphans()
    current = await store.get_plan(
        plan.id,
        user_id="u",
        workspace_id="w",
    )
    assert current is not None
    assert current.status == PlanStatus.RUNNING
    assert current.execution_turn_id == "t_partial_terminal"


@pytest.mark.asyncio
async def test_recover_orphans_releases_stale_blocked_tail_after_lease_expires(
    db_path: str,
) -> None:
    owner_store = await _store(db_path)
    recovery_store = await _store(db_path)
    plan = await owner_store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="blocked tail",
    )
    assert await owner_store.try_claim_running(plan.id, "t_blocked_tail")
    assert await owner_store.update_plan_status(plan.id, PlanStatus.BLOCKED)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE chat_turn_spawn_dispatch(turn_id TEXT PRIMARY KEY,state TEXT NOT NULL,lease_expires_at TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,state,lease_expires_at) VALUES (?,'spawned',?)",
            ("t_blocked_tail", "2099-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "UPDATE plans SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", plan.id),
        )
        connection.commit()

    # A live old Gateway still owns the report/terminal tail: preserve its CAS.
    assert await recovery_store.recover_orphans() == (0, 0)
    claimed = await owner_store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert claimed is not None
    assert claimed.status == PlanStatus.BLOCKED
    assert claimed.execution_turn_id == "t_blocked_tail"

    # Crash/repeated release failure: once the dispatch lease expires, periodic
    # maintenance clears only this exact stale token and re-approval can proceed.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ("2000-01-01T00:00:00+00:00", "t_blocked_tail"),
        )
        connection.commit()
    assert await recovery_store.recover_orphans() == (1, 0)
    assert await owner_store.try_claim_running(plan.id, "t_blocked_retry")


@pytest.mark.asyncio
async def test_dev_tail_heartbeat_preserves_exact_blocked_owner_freshness(
    db_path: str,
) -> None:
    store = await _store(db_path)
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="long dev terminal tail",
    )
    assert await store.try_claim_running(plan.id, "t_dev_tail")
    assert await store.update_plan_status(plan.id, PlanStatus.BLOCKED)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", plan.id),
        )
        connection.commit()

    assert await store.heartbeat_execution_tail(plan.id, "t_dev_tail")
    assert not await store.heartbeat_execution_tail(plan.id, "t_stale_tail")
    assert await store.recover_orphans() == (0, 0)
    kept = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert kept is not None
    assert kept.execution_turn_id == "t_dev_tail"

    assert await store.cancel_execution_for_turn("t_dev_tail") is False
    assert await store.update_plan_status(plan.id, PlanStatus.CANCELLED)
    assert not await store.heartbeat_execution_tail(plan.id, "t_dev_tail")


@pytest.mark.asyncio
async def test_recover_orphans_reconciles_to_resumable(db_path: str) -> None:
    """track C: 崩溃孤儿 reconcile-don't-re-run —— RUNNING plan→BLOCKED(可续,非
    FAILED 终态), RUNNING step→PENDING(error 清,重跑), DONE step 保留(不重跑→
    resume 跳过 = 不重复扣费/写)。"""
    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    await store.update_plan_status(plan.id, PlanStatus.RUNNING)
    done = new_step(plan_id=plan.id, seq=1, agent_name="a")
    running = new_step(plan_id=plan.id, seq=2, agent_name="b")
    await store.add_steps([done, running])
    await store.update_step_status(done.id, status=StepStatus.DONE)
    await store.update_step_status(running.id, status=StepStatus.RUNNING)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", plan.id),
        )
        connection.commit()

    plans_n, steps_n = await store.recover_orphans()
    assert plans_n == 1 and steps_n == 1  # 1 plan + only the RUNNING step (DONE untouched)
    got = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert got is not None and got.status == PlanStatus.BLOCKED  # resumable, NOT FAILED
    steps = {s.id: s for s in await store.list_steps(plan.id)}
    assert steps[done.id].status == StepStatus.DONE  # completed work preserved
    assert steps[running.id].status == StepStatus.PENDING  # reset for re-run
    assert steps[running.id].error_kind is None  # error cleared (fresh re-run)
    # resumable: try_claim accepts BLOCKED → run_plan would resume from resume_level
    assert await store.try_claim_running(plan.id, "t_resume") is True


# ── C1: gc_stale_proposed (stale proposals) ──────────────────────
@pytest.mark.asyncio
async def test_gc_stale_proposed_only_removes_old_proposed(db_path: str) -> None:
    """超 N 天未批准的 PROPOSED 删;新 proposed / 老的非-proposed 一律留。"""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    store = await _store(db_path)
    old_p = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="old-proposed")
    # Review F4: real steps under old proposed plan verify cascade (previous no-step create_plan made assertion trivial).
    await store.add_steps(
        [
            new_step(plan_id=old_p.id, seq=1, agent_name="compute", objective="x"),
            new_step(plan_id=old_p.id, seq=2, agent_name="canvas-writer", objective="y"),
        ]
    )
    assert len(await store.list_steps(old_p.id)) == 2  # Actual steps before deletion.
    new_p = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="new-proposed")
    done_p = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="old-done")
    await store.update_plan_status(done_p.id, PlanStatus.RUNNING)
    await store.update_plan_status(done_p.id, PlanStatus.COMPLETED)
    # Set old_p + done_p created_at 15 days ago.
    old_ts = (datetime.now(tz=UTC) - timedelta(days=15)).isoformat()
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE plans SET created_at = ? WHERE id IN (?, ?)", (old_ts, old_p.id, done_p.id))
        db.commit()

    n = await store.gc_stale_proposed(older_than_days=14)
    assert n == 1  # Delete old proposed only.
    assert await store.get_plan(old_p.id, user_id="u", workspace_id="w") is None
    assert await store.get_plan(new_p.id, user_id="u", workspace_id="w") is not None  # Retain new proposed.
    assert await store.get_plan(done_p.id, user_id="u", workspace_id="w") is not None  # Retain old completed.
    # Steps cascade-deleted too.
    assert await store.list_steps(old_p.id) == []
