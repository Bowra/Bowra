from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite
import jwt
import pytest
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.user_store import UserStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
async def store(tmp_path) -> UserStore:
    value = UserStore(tmp_path / "admin-spaces.db")
    await value.initialize()
    return value


@pytest.fixture
def client(store: UserStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    from bowhead_gateway.routes import admin_spaces, auth

    app = FastAPI()
    app.state.user_store = store

    class Settings:
        auth_mode = "prod"
        dev_default_user = "dev_user"

    app.state.settings = Settings()
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(admin_spaces.router)
    return TestClient(app)


def _token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


async def _seed_identity(
    store: UserStore,
) -> tuple[str, str, str]:
    team = await store.create_team(name="Route Team", max_seats=20)
    owner = await store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="Passw0rd!",
    )
    member = await store.create_user(
        team_id=team.id,
        email="member@example.com",
        password="Passw0rd!",
    )
    await store.set_team_creator(team.id, owner.id)
    return team.id, owner.id, member.id


async def test_team_list_includes_spaces_owner_has_not_joined(
    client: TestClient,
    store: UserStore,
) -> None:
    team_id, owner_id, member_id = await _seed_identity(store)
    workspace_id = await store.create_local_workspace(
        team_id=team_id,
        name="Operations",
        creator_user_id=member_id,
    )

    response = client.get(
        "/api/v1/admin/spaces",
        cookies={"bowhead_jwt": _token(owner_id)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"] == [
        {
            "workspace_id": workspace_id,
            "name": "Operations",
            "description": None,
            "status": "active",
            "member_count": 1,
            "admin_count": 1,
            "caller_membership": None,
            "created_at": response.json()["items"][0]["created_at"],
            "updated_at": response.json()["items"][0]["updated_at"],
            "revision": 1,
        },
    ]


async def test_member_cannot_list_or_create_team_spaces(
    client: TestClient,
    store: UserStore,
) -> None:
    _, _, member_id = await _seed_identity(store)
    cookies = {"bowhead_jwt": _token(member_id)}

    listed = client.get("/api/v1/admin/spaces", cookies=cookies)
    created = client.post(
        "/api/v1/admin/spaces",
        json={"name": "Denied"},
        cookies=cookies,
    )

    assert listed.status_code == 403
    assert listed.json()["detail"]["code"] == "not_team_admin"
    assert created.status_code == 403
    assert created.json()["detail"]["code"] == "not_team_admin"


async def test_team_governance_can_assign_and_revoke_space_admin(
    client: TestClient,
    store: UserStore,
) -> None:
    team_id, owner_id, member_id = await _seed_identity(store)
    workspace_id = await store.create_local_workspace(
        team_id=team_id,
        name="Operations",
        creator_user_id=owner_id,
    )
    cookies = {"bowhead_jwt": _token(owner_id)}

    granted = client.put(
        f"/api/v1/admin/spaces/{workspace_id}/admins",
        json={"user_id": member_id},
        cookies=cookies,
    )
    assert granted.status_code == 200, granted.text
    assert await store.get_member_role(workspace_id, member_id) == "admin"

    revoked = client.delete(
        f"/api/v1/admin/spaces/{workspace_id}/admins/{member_id}",
        cookies=cookies,
    )
    assert revoked.status_code == 200, revoked.text
    assert await store.get_member_role(workspace_id, member_id) == "member"


async def test_archive_occ_and_restore_roundtrip(
    client: TestClient,
    store: UserStore,
) -> None:
    _, owner_id, _ = await _seed_identity(store)
    cookies = {"bowhead_jwt": _token(owner_id)}
    first = client.post(
        "/api/v1/admin/spaces",
        json={"name": "First"},
        cookies=cookies,
    ).json()
    client.post(
        "/api/v1/admin/spaces",
        json={"name": "Second"},
        cookies=cookies,
    )

    stale = client.post(
        f"/api/v1/admin/spaces/{first['workspace_id']}/archive",
        json={"expected_revision": 99},
        cookies=cookies,
    )
    archived = client.post(
        f"/api/v1/admin/spaces/{first['workspace_id']}/archive",
        json={"expected_revision": 1},
        cookies=cookies,
    )
    restored = client.post(
        f"/api/v1/admin/spaces/{first['workspace_id']}/restore",
        json={"expected_revision": 2},
        cookies=cookies,
    )

    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "stale_workspace_update",
        "details": {"current_revision": 1},
    }
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT action FROM admin_audit_log WHERE target_id=? ORDER BY ts",
            (first["workspace_id"],),
        ) as cur,
    ):
        actions = [row[0] async for row in cur]
    assert actions == [
        "workspace_created",
        "workspace_archived",
        "workspace_restored",
    ]


async def test_archive_pauses_only_target_space_active_jobs(
    client: TestClient,
    store: UserStore,
) -> None:
    _, owner_id, _ = await _seed_identity(store)
    cookies = {"bowhead_jwt": _token(owner_id)}
    target = client.post(
        "/api/v1/admin/spaces",
        json={"name": "Target"},
        cookies=cookies,
    ).json()
    other = client.post(
        "/api/v1/admin/spaces",
        json={"name": "Other"},
        cookies=cookies,
    ).json()
    scheduled = ScheduledJobStore(str(store.db_path))
    await scheduled.initialize()
    client.app.state.scheduled_store = scheduled
    target_job = await scheduled.create(
        user_id=owner_id,
        workspace_id=target["workspace_id"],
        name="Target job",
        cron="0 9 * * *",
        query="q",
    )
    other_job = await scheduled.create(
        user_id=owner_id,
        workspace_id=other["workspace_id"],
        name="Other job",
        cron="0 10 * * *",
        query="q",
    )

    archived = client.post(
        f"/api/v1/admin/spaces/{target['workspace_id']}/archive",
        json={"expected_revision": 1},
        cookies=cookies,
    )

    assert archived.status_code == 200, archived.text
    target_after = await scheduled.get(target_job.id)
    other_after = await scheduled.get(other_job.id)
    assert target_after is not None
    assert target_after.status == "paused"
    assert target_after.pause_reason == "workspace_archived"
    assert other_after is not None
    assert other_after.status == "active"


async def test_scheduled_pause_failure_does_not_roll_back_archive(
    client: TestClient,
    store: UserStore,
) -> None:
    _, owner_id, _ = await _seed_identity(store)
    cookies = {"bowhead_jwt": _token(owner_id)}
    target = client.post(
        "/api/v1/admin/spaces",
        json={"name": "Target"},
        cookies=cookies,
    ).json()
    client.post(
        "/api/v1/admin/spaces",
        json={"name": "Other"},
        cookies=cookies,
    )

    class FailingScheduledStore:
        async def pause_active_for_workspace(
            self,
            workspace_id: str,
            *,
            reason: str,
        ) -> int:
            raise RuntimeError(f"{workspace_id}:{reason}")

    client.app.state.scheduled_store = FailingScheduledStore()
    archived = client.post(
        f"/api/v1/admin/spaces/{target['workspace_id']}/archive",
        json={"expected_revision": 1},
        cookies=cookies,
    )

    assert archived.status_code == 200, archived.text
    persisted = await store.get_workspace(target["workspace_id"])
    assert persisted is not None
    assert persisted["status"] == "archived"


# ── Restore spaces and scheduled jobs paused by archiving ─────────


async def _space_with_jobs(
    client: TestClient,
    store: UserStore,
) -> tuple[str, str, str, ScheduledJobStore]:
    _, owner_id, member_id = await _seed_identity(store)
    cookies = {"bowhead_jwt": _token(owner_id)}
    target = client.post(
        "/api/v1/admin/spaces",
        json={"name": "Target"},
        cookies=cookies,
    ).json()
    client.post(
        "/api/v1/admin/spaces",
        json={"name": "Other"},
        cookies=cookies,
    )
    await store.add_member(workspace_id=target["workspace_id"], user_id=member_id)
    scheduled = ScheduledJobStore(str(store.db_path))
    await scheduled.initialize()
    client.app.state.scheduled_store = scheduled
    return target["workspace_id"], owner_id, member_id, scheduled


def _archive(client: TestClient, workspace_id: str, owner_id: str) -> int:
    current = client.get(
        f"/api/v1/admin/spaces/{workspace_id}",
        cookies={"bowhead_jwt": _token(owner_id)},
    ).json()
    response = client.post(
        f"/api/v1/admin/spaces/{workspace_id}/archive",
        json={"expected_revision": current["revision"]},
        cookies={"bowhead_jwt": _token(owner_id)},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["revision"])


def _restore(client: TestClient, workspace_id: str, user_id: str, revision: int) -> Any:
    return client.post(
        f"/api/v1/admin/spaces/{workspace_id}/restore",
        json={"expected_revision": revision},
        cookies={"bowhead_jwt": _token(user_id)},
    )


async def test_restore_resumes_only_jobs_the_archive_paused(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, member_id, scheduled = await _space_with_jobs(client, store)
    archived_job = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="a", cron="0 9 * * *", query="q")
    member_job = await scheduled.create(user_id=member_id, workspace_id=workspace_id, name="m", cron="0 9 * * *", query="q")
    manual_job = await scheduled.create(user_id=member_id, workspace_id=workspace_id, name="p", cron="0 9 * * *", query="q")
    system_job = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="s", cron="0 9 * * *", query="q")
    assert await scheduled.patch(manual_job.id, status="paused")
    assert await scheduled.patch(system_job.id, status="paused", pause_reason="scheduled_recipe_lost")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE scheduled_jobs SET failure_count=2,next_run_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (archived_job.id,),
        )
        await db.commit()

    revision = _archive(client, workspace_id, owner_id)
    restored = _restore(client, workspace_id, owner_id, revision)

    assert restored.status_code == 200, restored.text
    after = {job_id: await scheduled.get(job_id) for job_id in (archived_job.id, member_job.id, manual_job.id, system_job.id)}
    # Resume archive-paused jobs (including others'): clear reason/failure count; calculate next run forward from restoration.
    for job_id in (archived_job.id, member_job.id):
        job = after[job_id]
        assert job is not None
        assert (job.status, job.pause_reason, job.failure_count) == ("active", None, 0)
        assert job.next_run_at is not None and job.next_run_at > "2026-"
    # User-paused jobs and jobs system-paused for other reasons stay unchanged.
    assert (after[manual_job.id].status, after[manual_job.id].pause_reason) == ("paused", None)
    assert (after[system_job.id].status, after[system_job.id].pause_reason) == ("paused", "scheduled_recipe_lost")


async def test_repeated_archive_and_restore_keep_manual_pause(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    kept = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="k", cron="0 9 * * *", query="q")
    later_paused = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="l", cron="0 9 * * *", query="q")

    for cycle in range(3):
        revision = _archive(client, workspace_id, owner_id)
        assert (await scheduled.get(kept.id)).pause_reason == "workspace_archived"
        assert _restore(client, workspace_id, owner_id, revision).status_code == 200
        assert (await scheduled.get(kept.id)).status == "active"
        if cycle == 0:
            # User pauses one after restoration: future archive/restore cycles leave it untouched.
            assert await scheduled.patch(later_paused.id, status="paused")
        else:
            job = await scheduled.get(later_paused.id)
            assert (job.status, job.pause_reason) == ("paused", None)
    # Restoring an already active space again does not resume user-paused jobs.
    again = await scheduled.resume_archived_for_workspace(workspace_id)
    assert again.resumed == []
    assert (await scheduled.get(later_paused.id)).status == "paused"


async def test_member_cannot_restore_space_or_resume_its_jobs(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, member_id, scheduled = await _space_with_jobs(client, store)
    job = await scheduled.create(user_id=member_id, workspace_id=workspace_id, name="m", cron="0 9 * * *", query="q")
    revision = _archive(client, workspace_id, owner_id)

    denied = _restore(client, workspace_id, member_id, revision)

    assert denied.status_code == 403
    after = await scheduled.get(job.id)
    assert (after.status, after.pause_reason) == ("paused", "workspace_archived")


async def test_legacy_space_status_route_restore_also_resumes_jobs(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    job = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="a", cron="0 9 * * *", query="q")
    _archive(client, workspace_id, owner_id)

    restored = client.patch(
        f"/api/v1/auth/me/spaces/{workspace_id}",
        json={"status": "active"},
        cookies={"bowhead_jwt": _token(owner_id)},
    )

    assert restored.status_code == 200, restored.text
    assert (await scheduled.get(job.id)).status == "active"


async def test_scheduled_resume_failure_does_not_roll_back_restore(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, _, _ = await _space_with_jobs(client, store)
    revision = _archive(client, workspace_id, owner_id)

    class FailingScheduledStore:
        async def resume_archived_for_workspace(self, workspace_id: str) -> None:
            raise RuntimeError(workspace_id)

    client.app.state.scheduled_store = FailingScheduledStore()
    restored = _restore(client, workspace_id, owner_id, revision)

    assert restored.status_code == 200, restored.text
    assert (await store.get_workspace(workspace_id))["status"] == "active"


async def test_resume_counts_next_run_from_restore_time_without_catch_up(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    daily = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="d", cron="0 9 * * *", query="q", tz="UTC")
    shanghai = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="s", cron="0 9 * * *", query="q")
    manual = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="m", cron="@manual", query="q", tz="UTC")
    revision = _archive(client, workspace_id, owner_id)
    workspace = await store.get_workspace(workspace_id)
    await store.restore_governed_workspace(
        team_id=workspace["team_id"],
        actor_user_id=owner_id,
        workspace_id=workspace_id,
        expected_revision=revision,
    )

    # Missed 9/30 + 10/1 while archived; restore 10/1 10:00 UTC → next run 10/2 09:00, no catch-up.
    # Shanghai 09:00 = 01:00 UTC; likewise calculate forward from restoration.
    result = await scheduled.resume_archived_for_workspace(
        workspace_id,
        now=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
    )

    assert set(result.resumed) == {daily.id, shanghai.id, manual.id}
    assert (await scheduled.get(daily.id)).next_run_at == "2026-10-02T09:00:00+00:00"
    assert (await scheduled.get(shanghai.id)).next_run_at == "2026-10-02T01:00:00+00:00"
    assert (await scheduled.get(manual.id)).next_run_at is None


async def test_resume_does_nothing_while_space_is_archived(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    job = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="a", cron="0 9 * * *", query="q")
    _archive(client, workspace_id, owner_id)

    # Restore races rearchive, archive commits first: space remains archived; never release jobs.
    result = await scheduled.resume_archived_for_workspace(workspace_id)

    assert result.resumed == [] and result.over_limit == []
    assert (await scheduled.get(job.id)).status == "paused"


async def test_resume_respects_owner_active_job_limit(
    client: TestClient,
    store: UserStore,
) -> None:
    from bowhead_core.scheduled_store import MAX_ACTIVE_JOBS_PER_USER

    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    first = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="1", cron="0 9 * * *", query="q")
    second = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="2", cron="0 9 * * *", query="q")
    revision = _archive(client, workspace_id, owner_id)
    # While archived, users consume enabled-job capacity in other spaces, leaving one slot.
    for index in range(MAX_ACTIVE_JOBS_PER_USER - 1):
        await scheduled.create(user_id=owner_id, workspace_id="ws_elsewhere", name=f"x{index}", cron="0 9 * * *", query="q")

    assert _restore(client, workspace_id, owner_id, revision).status_code == 200

    assert (await scheduled.get(first.id)).status == "active"
    over = await scheduled.get(second.id)
    assert (over.status, over.pause_reason) == ("paused", "scheduled_job_limit_reached")
    assert await scheduled.count_active(owner_id) == MAX_ACTIVE_JOBS_PER_USER


async def test_legacy_space_status_route_archive_pauses_jobs_like_governance(
    client: TestClient,
    store: UserStore,
) -> None:
    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    job = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="a", cron="0 9 * * *", query="q")
    manual = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="p", cron="0 9 * * *", query="q")
    assert await scheduled.patch(manual.id, status="paused")

    archived = client.patch(
        f"/api/v1/auth/me/spaces/{workspace_id}",
        json={"status": "archived"},
        cookies={"bowhead_jwt": _token(owner_id)},
    )

    assert archived.status_code == 200, archived.text
    after = await scheduled.get(job.id)
    assert (after.status, after.pause_reason) == ("paused", "workspace_archived")
    assert (await scheduled.get(manual.id)).pause_reason is None
    # Same restore entry: resume only archive-paused jobs.
    client.patch(
        f"/api/v1/auth/me/spaces/{workspace_id}",
        json={"status": "active"},
        cookies={"bowhead_jwt": _token(owner_id)},
    )
    assert (await scheduled.get(job.id)).status == "active"
    assert (await scheduled.get(manual.id)).status == "paused"


async def test_late_archive_pause_after_restore_does_not_strand_jobs(
    client: TestClient,
    store: UserStore,
) -> None:
    """归档提交 → 恢复提交 → 恢复端 resume → 归档端 pause（两边各自在提交之后收敛任务）。

    归档端的暂停晚到时空间已恢复为启用：不得再把任务停在 `workspace_archived`，
    否则启用中的空间里留着「因归档暂停」的任务、再也没有恢复动作会带它回来。
    """
    from bowhead_gateway.routes.admin_spaces import pause_archived_space_jobs, resume_restored_space_jobs

    workspace_id, owner_id, _, scheduled = await _space_with_jobs(client, store)
    job = await scheduled.create(user_id=owner_id, workspace_id=workspace_id, name="a", cron="0 9 * * *", query="q")
    workspace = await store.get_workspace(workspace_id)
    archived = await store.archive_governed_workspace(
        team_id=workspace["team_id"],
        actor_user_id=owner_id,
        workspace_id=workspace_id,
        expected_revision=int(workspace["revision"]),
    )
    await store.restore_governed_workspace(
        team_id=workspace["team_id"],
        actor_user_id=owner_id,
        workspace_id=workspace_id,
        expected_revision=int(archived["revision"]),
    )

    await resume_restored_space_jobs(client.app, workspace_id=workspace_id)
    await pause_archived_space_jobs(client.app, workspace_id=workspace_id)

    assert (await store.get_workspace(workspace_id))["status"] == "active"
    after = await scheduled.get(job.id)
    assert (after.status, after.pause_reason) == ("active", None)
