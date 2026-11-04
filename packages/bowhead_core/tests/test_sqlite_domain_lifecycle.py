"""Linked Canvas deletion resumes across independently committed domain files."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator, SessionLifecycleError
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect, connect_sync
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, domain_database_path
from test_sqlite_domain_stores import domain_directory as domain_directory
from test_terminal_runtime import _NOW, _runtime


def _clone(source: Path, destination: Path) -> Path:
    destination.mkdir()
    for filename in DOMAIN_TARGET_FILES.values():
        original = sqlite3.connect((source / filename).as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(destination / filename)
        try:
            original.backup(target)
        finally:
            original.close()
            target.close()
    shutil.copy2(source / "sqlite-layout.json", destination / "sqlite-layout.json")
    return destination / "bowhead.db"


async def _stores(core: Path):
    execution = domain_database_path(core, "execution")
    async with connect(execution) as db:
        await db.execute(
            """INSERT INTO terminal_protocol_writer_lease(
              lease_name,owner_instance_id,epoch,admission_state,admission_epoch,rollout_config_generation,lease_expires_at,heartbeat_at
            ) VALUES('v1_writer','gateway_1',7,'open',3,'rollout_1',?,?)""",
            ((_NOW + timedelta(minutes=5)).isoformat(), _NOW.isoformat()),
        )
        await db.commit()
    runtime = await _runtime(str(core))
    canvases = CanvasStore(core)
    await canvases.initialize()
    canvas = await canvases.ensure_canvas(session_id="s_1", workspace_id="ws_1", user_id="u_1")
    lifecycle = SessionLifecycleCoordinator(SessionStore(core), runtime.store, canvases)
    return lifecycle, canvases, canvas


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_content", "after_content"])
async def test_linked_canvas_deletion_has_durable_pending_phase_and_resumes(
    domain_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    core = _clone(domain_directory, tmp_path / "instance")
    lifecycle, canvases, canvas = await _stores(core)
    original_delete = canvases.delete_canvas_in_transaction
    original_finish = lifecycle._finish_pending_canvas_delete

    async def failing_delete(connection, **kwargs):
        # There must be no execution writer while content is being changed.
        probe = connect_sync(domain_database_path(core, "execution"), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE").close()
            probe.rollback()
        finally:
            probe.close()
        raise RuntimeError("simulated content cleanup interruption")

    async def committed_content_then_crash(_operation_id):
        async with connect(canvases.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await original_delete(db, canvas_id=canvas.id, expected_session_id="s_1", expected_user_id="u_1")
            await db.commit()
        raise RuntimeError("simulated content commit interruption")

    if phase == "before_content":
        monkeypatch.setattr(canvases, "delete_canvas_in_transaction", failing_delete)
    else:
        monkeypatch.setattr(lifecycle, "_finish_pending_canvas_delete", committed_content_then_crash)
    arguments = {
        "session_id": "s_1",
        "actor_audit_ref": "u_1",
        "request_id": f"canvas-delete:{canvas.id}",
        "expected_user_id": "u_1",
        "linked_canvas_id": canvas.id,
    }
    with pytest.raises(RuntimeError, match="simulated content"):
        await lifecycle.soft_delete(**arguments)
    async with connect(domain_database_path(core, "execution")) as db:
        async with db.execute("SELECT state,committed_at FROM session_lifecycle_operations WHERE request_id=?", (arguments["request_id"],)) as cursor:
            assert await cursor.fetchone() == ("protocol_settled", None)
        async with db.execute("SELECT deleted_at FROM sessions WHERE id='s_1'") as cursor:
            assert (await cursor.fetchone())[0] is not None
    monkeypatch.setattr(canvases, "delete_canvas_in_transaction", original_delete)
    monkeypatch.setattr(lifecycle, "_finish_pending_canvas_delete", original_finish)
    if phase == "before_content":
        assert await lifecycle.resume_pending_canvas_deletions() == 1
    else:
        # HTTP replay must repair even after the content parent row is gone.
        assert await lifecycle.canvas_delete_replay(canvas_id=canvas.id, actor_audit_ref="u_1") is not None
    replay = await lifecycle.soft_delete(**arguments)
    assert replay.result_code == "deleted" and replay.changed
    assert await canvases.get_canvas(canvas.id) is None
    assert await lifecycle.resume_pending_canvas_deletions() == 0


@pytest.mark.asyncio
async def test_lifecycle_rejects_foreign_installation_and_foreign_canvas_owner(domain_directory: Path, tmp_path: Path) -> None:
    core = _clone(domain_directory, tmp_path / "instance")
    lifecycle, canvases, _canvas = await _stores(core)
    with pytest.raises(ValueError, match="installation"):
        SessionLifecycleCoordinator(SessionStore(core), lifecycle._terminal, CanvasStore(tmp_path / "other" / "bowhead.db"))
    async with connect(core) as db:
        await db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES('u_other','team_1','other@example.test','t')")
        await db.commit()
    sessions = SessionStore(core)
    await sessions.ensure_session(session_id="s_other", workspace_id="ws_1", user_id="u_other")
    foreign = await canvases.ensure_canvas(session_id="s_other", workspace_id="ws_1", user_id="u_other")
    with pytest.raises(SessionLifecycleError, match="session_owner_mismatch"):
        await lifecycle.soft_delete(
            session_id="s_1", actor_audit_ref="u_1", request_id=f"canvas-delete:{foreign.id}", expected_user_id="u_1", linked_canvas_id=foreign.id
        )
    assert await canvases.get_canvas(foreign.id) is not None
    assert (await sessions.get_session("s_1")).deleted_at is None
