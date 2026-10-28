from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import canvas
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def lifecycle_ctx(tmp_path):
    user_db = tmp_path / "users.db"
    users = UserStore(user_db)
    await users.initialize()
    team = await users.create_team(name="Canvas Lifecycle", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="canvas-lifecycle@example.com",
        password="Passw0rd!",
    )
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Archived Canvas",
        creator_user_id=user.id,
    )

    canvases = CanvasStore(db_path=tmp_path / "canvas.db")
    await canvases.initialize()
    report = await canvases.ensure_canvas(
        session_id="s_archived_canvas",
        workspace_id=workspace_id,
        user_id=user.id,
    )

    async with aiosqlite.connect(user_db) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    app = FastAPI()
    app.include_router(canvas.router, prefix="/api/v1/canvas")
    app.state.canvas_store = canvases
    app.state.user_store = users
    app.state.settings = SimpleNamespace(
        dev_default_user=user.id,
        auth_mode="dev",
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, canvases, workspace_id, user.id, report.id, report.session_id


async def test_archived_canvas_history_remains_readable(lifecycle_ctx) -> None:
    client, _canvases, workspace_id, user_id, canvas_id, session_id = lifecycle_ctx
    headers = {
        "X-Bowhead-User-Id": user_id,
        "X-Space-Id": workspace_id,
    }

    by_session = client.get(
        f"/api/v1/canvas/by-session/{session_id}",
        headers=headers,
    )
    detail = client.get(
        f"/api/v1/canvas/{canvas_id}",
        headers=headers,
    )
    revisions = client.get(
        f"/api/v1/canvas/{canvas_id}/revisions",
        headers=headers,
    )
    status = client.get(
        f"/api/v1/canvas/{canvas_id}/status",
        headers=headers,
    )
    listed = client.get(
        "/api/v1/canvas",
        params={"workspace_id": workspace_id},
        headers=headers,
    )

    for response in (by_session, detail, revisions, status, listed):
        assert response.status_code == 200, response.text
    assert by_session.json()["canvas"]["id"] == canvas_id
    assert detail.json()["canvas"]["id"] == canvas_id
    assert listed.json()["items"][0]["id"] == canvas_id


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "post",
            "/api/v1/canvas/{canvas_id}/title",
            {"title": "Archived rename"},
        ),
        (
            "post",
            "/api/v1/canvas/{canvas_id}/status",
            {"status": "verified"},
        ),
        (
            "post",
            "/api/v1/canvas/{canvas_id}/clear",
            None,
        ),
        (
            "delete",
            "/api/v1/canvas/{canvas_id}",
            None,
        ),
        (
            "post",
            "/api/v1/canvas/{canvas_id}/sections/section_missing/rewrite",
            {"selection_text": "rewrite me", "preset": "concise"},
        ),
        (
            "post",
            "/api/v1/canvas/{canvas_id}/export",
            {"fmt": "pdf"},
        ),
        (
            "post",
            "/api/v1/canvas/{canvas_id}/exports",
            {},
        ),
        (
            "post",
            "/api/v1/canvas/{canvas_id}/exports/job_missing/cancel",
            None,
        ),
    ],
)
async def test_archived_canvas_mutations_and_execution_are_rejected(
    lifecycle_ctx,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    client, canvases, workspace_id, user_id, canvas_id, _session_id = lifecycle_ctx

    response = client.request(
        method.upper(),
        path.format(canvas_id=canvas_id),
        json=payload,
        headers={
            "X-Bowhead-User-Id": user_id,
            "X-Space-Id": workspace_id,
        },
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    code = detail.get("error", {}).get("code") if isinstance(detail, dict) else None
    assert code == "workspace_archived"
    persisted = await canvases.get_canvas(canvas_id)
    assert persisted is not None
    assert persisted.title is None
    assert persisted.status == "active"
