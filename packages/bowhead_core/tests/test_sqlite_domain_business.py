"""Mixed business operations keep their authoritative writes in the owning file."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.memory_privacy import MemoryDestructiveOperationStore
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.session_store import SessionStore
from bowhead_core.space_instructions import SpaceInstructionsStore
from bowhead_core.sqlite_domain_runtime import connect
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.subagent import _system_profile_snapshot
from test_sqlite_domain_stores import domain_directory as domain_directory


@pytest.mark.asyncio
async def test_system_revision_precedes_execution_and_space_snapshot_uses_execution(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    async with open_shared_ctx(core) as db:
        await db.execute("INSERT OR IGNORE INTO team_members(id,user_id,team_id,role,created_at) VALUES('tm_1','u_1','team_1','member','t')")
        await db.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES('ws_1','u_1','member','t')")
        await db.commit()
    sessions = SessionStore(core)
    turn = await sessions.record_turn(turn_id="t_domain_system", session_id="s_1", user_text="compute")
    instructions = SpaceInstructionsStore(core)
    await instructions.save(workspace_id="ws_1", content="Use regional sales totals.", created_by="u_1", expected_version=0)
    snapshot = await instructions.snapshot_for_turn(turn_id=turn.id, workspace_id="ws_1")
    assert snapshot is not None and snapshot.version == 1
    assert await instructions.version_for_turn(turn.id) == ("ws_1", 1)
    profile = manifest_to_profile(AgentRegistry.bootstrap().get("compute"))
    definition = _system_profile_snapshot(profile)
    binding = await AgentExecutionBindingStore(core).admit_system_execution(
        team_id="team_1",
        user_id="u_1",
        turn_id=turn.id,
        profile_name="compute",
        dispatch_key="domain:compute",
        definition=definition,
        manifest_fingerprint="sha256:" + hashlib.sha256(b"manifest").hexdigest(),
        override_version=None,
        system_required=True,
    )
    assert binding.agent_id == "system:compute"
    async with connect(core) as db, db.execute("SELECT id FROM main.system_agent_effective_revision WHERE id=?", (binding.revision_ref,)) as cursor:
        assert await cursor.fetchone() == (binding.revision_ref,)
    async with (
        connect(domain_database_path(core, "execution")) as db,
        db.execute("SELECT id FROM main.turn_agent_execution WHERE id=?", (binding.execution_id,)) as cursor,
    ):
        assert await cursor.fetchone() == (binding.execution_id,)


@pytest.mark.asyncio
async def test_schedule_followup_occurs_after_core_commit(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    schedules = ScheduledJobStore(core)
    job = await schedules.create(user_id="u_1", workspace_id="ws_1", name="Followup", cron="0 9 * * *", query="Calculate totals")
    assignments = AgentAssignmentStore(core)
    async with open_shared_ctx(core) as db:
        await db.execute("BEGIN IMMEDIATE")
        ids = await assignments._set_schedule_status_conn(
            db,
            job_ids=(job.id,),
            status="paused",
            pause_reason="agent_unhired:test",
            timestamp="2026-11-03",
        )
        assert ids == (job.id,)
        await db.commit()
    assert (await schedules.get(job.id)).status == "active"
    await assignments._apply_split_schedule_status(job_ids=ids, status="paused", pause_reason="agent_unhired:test", timestamp="2026-11-03")
    assert (await schedules.get(job.id)).status == "paused"


@pytest.mark.asyncio
async def test_memory_privacy_residue_phase_scrubs_notification_domain(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    async with connect(domain_database_path(core, "notifications")) as db:
        await db.execute(
            """INSERT INTO notification_items(id,team_id,category,type,title,body,dedupe_key,created_at,updated_at)
            VALUES('memory-note','team_1','system','memory.suggestion','Suggestion','private memory','memory-note','t','t')"""
        )
        await db.execute("INSERT INTO notification_recipients(notification_id,user_id,delivered_at) VALUES('memory-note','u_1','t')")
        await db.commit()
    privacy = MemoryDestructiveOperationStore(core, ledger=None, fingerprint_key="test-key")
    await privacy._assert_full_user_residue_zero(user_id="u_1")
    async with (
        connect(domain_database_path(core, "notifications")) as db,
        db.execute("SELECT body FROM notification_items WHERE id='memory-note'") as cursor,
    ):
        assert await cursor.fetchone() == ("Memory suggestions removed",)


@pytest.mark.asyncio
async def test_eval_scratch_resumes_after_execution_commit_before_core_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from functools import partial

    import anyio
    import bowhead_core.agent_eval_job_store as module
    from bowhead_cli.migrate import upgrade
    from bowhead_core._db import close_pooled_readers
    from bowhead_core.agent_eval_job_store import AgentEvalJobStore, AgentEvalJobStoreError
    from cryptography.fernet import Fernet
    from test_agent_eval_execution import _fixture

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    core, _users, _team, _owner, workspace_id, evals, _revision, run = await _fixture(tmp_path)
    await close_pooled_readers()
    await anyio.to_thread.run_sync(partial(upgrade, f"sqlite+aiosqlite:///{core}"))
    jobs = AgentEvalJobStore(core)
    await jobs.materialize_jobs(
        run_id=run.run_id, workspace_id=workspace_id, attempts=1, max_concurrency=1, max_duration_ms=120_000, max_cost_microusd=5_000_000
    )
    await evals.mark_run_running(run.run_id)
    claim = await jobs.claim_next_job(run_id=run.run_id, worker_id="split-worker", lease_seconds=120)
    assert claim is not None
    original = module._require_current_lease
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AgentEvalJobStoreError("simulated_core_link_interruption")
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "_require_current_lease", interrupted)
    arguments = {
        "job_id": claim.job.job_id,
        "lease_token": claim.lease_token,
        "session_id": "s_split_eval",
        "turn_id": "t_split_eval",
        "user_text": "test",
    }
    with pytest.raises(AgentEvalJobStoreError, match="simulated_core_link_interruption"):
        await jobs.start_scratch_turn(**arguments)
    monkeypatch.setattr(module, "_require_current_lease", original)
    scratch = await jobs.start_scratch_turn(**arguments)
    replay = await jobs.start_scratch_turn(**arguments)
    assert scratch == replay
    assert scratch.turn_id == "t_split_eval"
    assert (await SessionStore(core).get_session(scratch.session_id)).origin == "agent_eval"
