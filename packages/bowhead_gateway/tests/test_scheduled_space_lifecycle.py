from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.user_store import UserStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def lifecycle_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    user_store = UserStore(tmp_path / "scheduled-lifecycle.db")
    await user_store.initialize()
    team = await user_store.create_team(name="Scheduled Lifecycle")
    owner = await user_store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="StrongPass123!",
    )
    await user_store.set_team_creator(team.id, owner.id)
    workspace_id = await user_store.create_local_workspace(
        team_id=team.id,
        name="Scheduled Space",
        creator_user_id=owner.id,
    )
    scheduled_store = ScheduledJobStore(str(user_store.db_path))
    await scheduled_store.initialize()
    job = await scheduled_store.create(
        user_id=owner.id,
        workspace_id=workspace_id,
        name="Daily",
        cron="0 9 * * *",
        query="q",
    )

    from bowhead_gateway.routes import scheduled

    app = FastAPI()
    app.state.user_store = user_store
    app.state.scheduled_store = scheduled_store

    class Settings:
        auth_mode = "dev"
        dev_default_user = owner.id

    app.state.settings = Settings()
    app.include_router(
        scheduled.router,
        prefix="/api/v1/scheduled",
    )
    return (
        TestClient(app),
        user_store,
        scheduled_store,
        owner.id,
        workspace_id,
        job.id,
    )


@pytest.mark.asyncio
async def test_archived_space_keeps_scheduled_history_readable(
    lifecycle_app,
) -> None:
    client, user_store, _scheduled_store, _owner_id, workspace_id, job_id = lifecycle_app
    async with aiosqlite.connect(user_store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    listed = client.get("/api/v1/scheduled/jobs")
    fetched = client.get(f"/api/v1/scheduled/jobs/{job_id}")
    runs = client.get(f"/api/v1/scheduled/jobs/{job_id}/runs")

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [job_id]
    assert fetched.status_code == 200, fetched.text
    assert runs.status_code == 200, runs.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/v1/scheduled/jobs/{job_id}", {"name": "Denied"}),
        ("delete", "/api/v1/scheduled/jobs/{job_id}", None),
        ("post", "/api/v1/scheduled/jobs/{job_id}/run-now", None),
    ],
)
async def test_archived_space_rejects_scheduled_mutation_and_execution(
    lifecycle_app,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    client, user_store, scheduled_store, _owner_id, workspace_id, job_id = lifecycle_app
    async with aiosqlite.connect(user_store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    response = client.request(
        method,
        path.format(job_id=job_id),
        json=body,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"
    assert await scheduled_store.get(job_id) is not None
    assert await scheduled_store.list_runs(job_id) == []
