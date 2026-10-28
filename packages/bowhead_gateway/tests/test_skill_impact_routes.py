"""WP5-B1 protected personal and Team Skill mutation routes."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.resource_usage_projection import ResourceUsageProjectionService, ResourceUsageUnavailableError
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import skills as skills_route
from bowhead_gateway.routes import team_skills as team_skills_route
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


class _Catalog:
    def list_specs(self):
        return []

    def list_names(self):
        return []

    def get(self, _name):
        return None

    def refresh(self):
        return None


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: Guarded recipe 触发：guard\n---\n# Steps\n"


def _stores(tmp_path: Path):
    requested = tmp_path / "requested.db"
    assert upgrade(f"sqlite+aiosqlite:///{requested}", target="core") == ("core",)
    db_path = tmp_path / "bowhead.db"
    user_store = UserStore(str(db_path))
    skill_store = SkillInstanceStore(str(db_path))
    _run(user_store.initialize())
    _run(skill_store.initialize())
    team = _run(user_store.create_team(name="Skill guard", max_seats=3))
    user = _run(user_store.create_user(team_id=team.id, email="guard@example.com"))
    return db_path, user_store, skill_store, user


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_RESOURCE_IMPACT_GUARD", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")


def test_personal_skill_disable_requires_preview_and_replays(tmp_path, monkeypatch):
    db_path, user_store, skill_store, user = _stores(tmp_path)
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / ".data" / "user_skills" / user.id / "guarded"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("guarded"), encoding="utf-8")
    inst = _run(
        skill_store.upsert(
            user_id=user.id,
            name="guarded",
            source_type="user_generated",
            source_path=str(skill_dir),
            description="Guarded recipe",
        )
    )
    app = FastAPI()
    app.include_router(skills_route.router, prefix="/api/v1/skills")
    app.state.user_store = user_store
    app.state.skill_instance_store = skill_store
    app.state.skill_catalog = _Catalog()
    app.dependency_overrides[skills_route._current_user] = lambda: user
    client = TestClient(app)

    with monkeypatch.context() as patch:

        async def unavailable(*args, **kwargs):
            raise ResourceUsageUnavailableError("private projection or database detail")

        patch.setattr(ResourceUsageProjectionService, "usage", unavailable)
        unavailable_response = client.get("/api/v1/skills/guarded/usage")
        assert unavailable_response.status_code == 503
        assert unavailable_response.json()["detail"]["code"] == "resource_usage_unavailable"
        assert unavailable_response.json()["detail"]["diagnostics"]["retryable"] is True
        assert "private projection" not in unavailable_response.text
        patch.setattr(ResourceUsageProjectionService, "usage_many_in_connection", unavailable)
        preview_failure = client.post(
            "/api/v1/skills/guarded/impact-preview?scope=personal",
            json={"action": "patch", "proposed_change": {"enabled": False}},
        )
        assert preview_failure.status_code == 503, preview_failure.text
        assert preview_failure.json()["detail"]["code"] == "resource_impact_projection_incomplete"
        assert preview_failure.json()["detail"]["diagnostics"]["retryable"] is True
        assert "private projection" not in preview_failure.text

    invalid = client.post(
        "/api/v1/skills/guarded/impact-preview?scope=personal",
        json={"action": "patch", "proposed_change": {"unknown": "private input"}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "resource_command_invalid"
    assert invalid.json()["detail"]["diagnostics"]["retryable"] is False
    assert "private input" not in invalid.text

    missing = client.patch("/api/v1/skills/update?scope=personal", json={"name": "guarded", "enabled": False})
    assert missing.status_code == 412
    preview = client.post(
        "/api/v1/skills/guarded/impact-preview?scope=personal",
        json={"action": "patch", "proposed_change": {"enabled": False}},
    )
    assert preview.status_code == 200, preview.text
    headers = {
        "X-Bowhead-Impact-Token": preview.json()["impact_token"],
        "Idempotency-Key": "disable-personal-skill-0001",
    }
    original_grant_check = skills_route._require_skill_grant_for_request

    async def deny_write(*_args, **_kwargs):
        raise skills_route.skill_workflow_http_error(403, "skill_write_forbidden")

    monkeypatch.setattr(skills_route, "_require_skill_grant_for_request", deny_write)
    denied = client.patch(
        "/api/v1/skills/update?scope=personal",
        json={"name": "guarded", "enabled": False},
        headers=headers,
    )
    assert denied.status_code == 403
    assert _run(skill_store.get(user_id=user.id, name="guarded")).enabled is True
    monkeypatch.setattr(
        skills_route,
        "_require_skill_grant_for_request",
        original_grant_check,
    )
    first = client.patch(
        "/api/v1/skills/update?scope=personal",
        json={"name": "guarded", "enabled": False},
        headers=headers,
    )
    retry = client.patch(
        "/api/v1/skills/update?scope=personal",
        json={"name": "guarded", "enabled": False},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert retry.status_code == 200, retry.text
    assert _run(skill_store.get(user_id=user.id, name="guarded")).enabled is False
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT count(*) FROM resource_change_receipt WHERE resource_kind='skill' AND resource_id=?",
            (inst.id,),
        ).fetchone() == (1,)


def test_bulk_skill_disable_cannot_bypass_guard_with_fake_headers(tmp_path, monkeypatch):
    _db_path, user_store, skill_store, user = _stores(tmp_path)
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / ".data" / "user_skills" / user.id / "bulk-guarded"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("bulk-guarded"), encoding="utf-8")
    _run(
        skill_store.upsert(
            user_id=user.id,
            name="bulk-guarded",
            source_type="user_generated",
            source_path=str(skill_dir),
        )
    )
    app = FastAPI()
    app.include_router(skills_route.router, prefix="/api/v1/skills")
    app.state.user_store = user_store
    app.state.skill_instance_store = skill_store
    app.state.skill_catalog = _Catalog()
    app.dependency_overrides[skills_route._current_user] = lambda: user
    client = TestClient(app)

    result = client.patch(
        "/api/v1/skills/bulk?scope=personal",
        json={"items": [{"name": "bulk-guarded", "enabled": False}]},
        headers={
            "X-Bowhead-Impact-Token": "not-a-real-token",
            "Idempotency-Key": "fake-bulk-impact-0001",
        },
    )

    assert result.status_code == 200
    assert result.json()["items"] == []
    assert result.json()["errors"][0]["status"] == 412
    error = result.json()["errors"][0]["detail"]["error"]
    assert error["code"] == "resource_impact_required"
    assert error["diagnostics"]["retryable"] is False
    assert _run(skill_store.get(user_id=user.id, name="bulk-guarded")).enabled is True


async def _projection_with_live_dependency(original, *args, **kwargs):
    views = await original(*args, **kwargs)
    return {key: replace(view, summary=replace(view.summary, current_agent_count=1)) for key, view in views.items()}


def test_personal_skill_delete_quarantines_after_validation(tmp_path, monkeypatch):
    db_path, user_store, skill_store, user = _stores(tmp_path)
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / ".data" / "user_skills" / user.id / "deletable"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("deletable"), encoding="utf-8")
    inst = _run(
        skill_store.upsert(
            user_id=user.id,
            name="deletable",
            source_type="user_upload",
            source_path=str(skill_dir),
        )
    )
    app = FastAPI()
    app.include_router(skills_route.router, prefix="/api/v1/skills")
    app.state.user_store = user_store
    app.state.skill_instance_store = skill_store
    app.state.skill_catalog = _Catalog()
    app.dependency_overrides[skills_route._current_user] = lambda: user
    client = TestClient(app)
    preview = client.post(
        "/api/v1/skills/deletable/impact-preview?scope=personal",
        json={"action": "delete", "proposed_change": {}},
    ).json()
    headers = {
        "X-Bowhead-Impact-Token": preview["impact_token"],
        "Idempotency-Key": "delete-personal-skill-0001",
    }

    original_projection = ResourceUsageProjectionService.usage_many_in_connection
    with monkeypatch.context() as patch:

        async def live_projection(*args, **kwargs):
            return await _projection_with_live_dependency(original_projection, *args, **kwargs)

        patch.setattr(ResourceUsageProjectionService, "usage_many_in_connection", live_projection)
        blocked = client.delete(f"/api/v1/skills/{inst.id}?scope=personal", headers=headers)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["error"]["code"] == "resource_delete_has_live_dependencies"
        assert blocked.json()["detail"]["error"]["diagnostics"]["retryable"] is False
    assert _run(skill_store.get_by_id(user_id=user.id, skill_id=inst.id)) is not None
    assert skill_dir.exists()
    assert client.delete(f"/api/v1/skills/{inst.id}?scope=personal", headers=headers).status_code == 204
    assert client.delete(f"/api/v1/skills/{inst.id}?scope=personal", headers=headers).status_code == 204
    assert not skill_dir.exists()
    assert _run(skill_store.get_by_id(user_id=user.id, skill_id=inst.id)) is None
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT state FROM resource_file_cleanup_receipt WHERE resource_id=?",
            (inst.id,),
        ).fetchone() == ("completed",)


def test_team_skill_disable_and_delete_are_protected(tmp_path, monkeypatch):
    db_path, user_store, skill_store, user = _stores(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(team_skills_route, "_TEAM_SKILLS_ROOT", tmp_path / ".data" / "team_skills")
    app = FastAPI()
    app.include_router(team_skills_route.router, prefix="/api/v1/team-skills")
    app.include_router(skills_route.router, prefix="/api/v1/skills")
    app.state.user_store = user_store
    app.state.skill_instance_store = skill_store
    app.state.skill_catalog = _Catalog()
    app.dependency_overrides[team_skills_route._current_user] = lambda: user
    app.dependency_overrides[skills_route._current_user] = lambda: user
    client = TestClient(app)
    created = client.post(
        "/api/v1/team-skills",
        json={"body_md": _skill_md("team-guarded"), "visibility": "creator_only"},
    )
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]

    preference_preview = client.post(
        "/api/v1/skills/team-guarded/impact-preview?scope=personal",
        json={"action": "patch", "proposed_change": {"enabled": False}},
    )
    assert preference_preview.status_code == 200, preference_preview.text
    preference_disabled = client.patch(
        "/api/v1/skills/update?scope=personal",
        json={"name": "team-guarded", "enabled": False},
        headers={
            "X-Bowhead-Impact-Token": preference_preview.json()["impact_token"],
            "Idempotency-Key": "disable-team-skill-preference-0001",
        },
    )
    assert preference_disabled.status_code == 200, preference_disabled.text
    assert preference_disabled.json()["source_type"] == "team_shared"
    assert preference_disabled.json()["enabled"] is False

    assert client.patch(f"/api/v1/team-skills/{skill_id}", json={"status": "disabled"}).status_code == 412
    disable_preview = client.post(
        f"/api/v1/team-skills/{skill_id}/impact-preview",
        json={"action": "patch", "proposed_change": {"status": "disabled"}},
    ).json()
    disabled = client.patch(
        f"/api/v1/team-skills/{skill_id}",
        json={"status": "disabled"},
        headers={
            "X-Bowhead-Impact-Token": disable_preview["impact_token"],
            "Idempotency-Key": "disable-team-skill-0001",
        },
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    delete_preview = client.post(
        f"/api/v1/team-skills/{skill_id}/impact-preview",
        json={"action": "delete", "proposed_change": {}},
    ).json()
    headers = {
        "X-Bowhead-Impact-Token": delete_preview["impact_token"],
        "Idempotency-Key": "delete-team-skill-0001",
    }
    original_projection = ResourceUsageProjectionService.usage_many_in_connection
    with monkeypatch.context() as patch:

        async def live_projection(*args, **kwargs):
            return await _projection_with_live_dependency(original_projection, *args, **kwargs)

        patch.setattr(ResourceUsageProjectionService, "usage_many_in_connection", live_projection)
        blocked = client.delete(f"/api/v1/team-skills/{skill_id}", headers=headers)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["error"]["code"] == "resource_delete_has_live_dependencies"
        assert blocked.json()["detail"]["error"]["diagnostics"]["retryable"] is False
    assert _run(user_store.get_team_skill(user.team_id, skill_id)) is not None
    assert client.delete(f"/api/v1/team-skills/{skill_id}", headers=headers).status_code == 204
    assert client.delete(f"/api/v1/team-skills/{skill_id}", headers=headers).status_code == 204
    assert _run(user_store.get_team_skill(user.team_id, skill_id)) is None
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT state FROM resource_file_cleanup_receipt WHERE resource_id=?",
            (skill_id,),
        ).fetchone() == ("completed",)


def test_team_preference_uses_stable_id_when_personal_skill_has_same_name(
    tmp_path,
    monkeypatch,
):
    """A Team row must never mutate a same-name personal Skill."""
    _db_path, user_store, skill_store, user = _stores(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FEATURE_RESOURCE_IMPACT_GUARD", "0")
    monkeypatch.setattr(team_skills_route, "_TEAM_SKILLS_ROOT", tmp_path / ".data" / "team_skills")
    personal_dir = tmp_path / ".data" / "user_skills" / user.id / "same-name"
    personal_dir.mkdir(parents=True)
    (personal_dir / "SKILL.md").write_text(_skill_md("same-name"), encoding="utf-8")
    personal = _run(
        skill_store.upsert(
            user_id=user.id,
            name="same-name",
            source_type="user_upload",
            source_path=str(personal_dir),
        )
    )
    app = FastAPI()
    app.include_router(team_skills_route.router, prefix="/api/v1/team-skills")
    app.include_router(skills_route.router, prefix="/api/v1/skills")
    app.state.user_store = user_store
    app.state.skill_instance_store = skill_store
    app.state.skill_catalog = _Catalog()
    app.dependency_overrides[team_skills_route._current_user] = lambda: user
    app.dependency_overrides[skills_route._current_user] = lambda: user
    client = TestClient(app)
    created = client.post(
        "/api/v1/team-skills",
        json={"body_md": _skill_md("same-name"), "visibility": "creator_only"},
    )
    assert created.status_code == 200, created.text
    team_skill_id = created.json()["id"]

    disabled = client.patch(
        "/api/v1/skills/update?scope=personal",
        json={"name": "same-name", "team_skill_id": team_skill_id, "enabled": False},
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["source_type"] == "team_shared"
    assert _run(skill_store.get_by_id(user_id=user.id, skill_id=personal.id)).enabled is True
    preference = _run(
        skill_store.get(
            user_id=user.id,
            name=f"team:{team_skill_id}",
        )
    )
    assert preference is not None and preference.enabled is False
    listed = client.get("/api/v1/team-skills").json()["items"]
    assert listed[0]["my_enabled"] is False


def test_guarded_team_preference_preview_keeps_same_name_personal_skill(
    tmp_path,
    monkeypatch,
):
    _db_path, user_store, skill_store, user = _stores(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(team_skills_route, "_TEAM_SKILLS_ROOT", tmp_path / ".data" / "team_skills")
    personal_dir = tmp_path / ".data" / "user_skills" / user.id / "same-name-guarded"
    personal_dir.mkdir(parents=True)
    (personal_dir / "SKILL.md").write_text(_skill_md("same-name-guarded"), encoding="utf-8")
    personal = _run(
        skill_store.upsert(
            user_id=user.id,
            name="same-name-guarded",
            source_type="user_generated",
            source_path=str(personal_dir),
        )
    )
    app = FastAPI()
    app.include_router(team_skills_route.router, prefix="/api/v1/team-skills")
    app.include_router(skills_route.router, prefix="/api/v1/skills")
    app.state.user_store = user_store
    app.state.skill_instance_store = skill_store
    app.state.skill_catalog = _Catalog()
    app.dependency_overrides[team_skills_route._current_user] = lambda: user
    app.dependency_overrides[skills_route._current_user] = lambda: user
    client = TestClient(app)
    created = client.post(
        "/api/v1/team-skills",
        json={"body_md": _skill_md("same-name-guarded"), "visibility": "creator_only"},
    )
    assert created.status_code == 200, created.text
    team_skill_id = created.json()["id"]
    preview = client.post(
        "/api/v1/skills/same-name-guarded/impact-preview?scope=personal",
        json={
            "action": "patch",
            "proposed_change": {"enabled": False},
            "team_skill_id": team_skill_id,
        },
    )
    assert preview.status_code == 200, preview.text
    changed = client.patch(
        "/api/v1/skills/update?scope=personal",
        json={"name": "same-name-guarded", "team_skill_id": team_skill_id, "enabled": False},
        headers={
            "X-Bowhead-Impact-Token": preview.json()["impact_token"],
            "Idempotency-Key": "disable-same-name-team-pref-0001",
        },
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["source_type"] == "team_shared"
    assert _run(skill_store.get_by_id(user_id=user.id, skill_id=personal.id)).enabled is True


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("bowhead.db",):
        initialize_migrated_core_database(tmp_path / filename)
