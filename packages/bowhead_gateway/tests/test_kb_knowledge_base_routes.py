"""Knowledge base API (phase 1 slice 3) against a migrated split layout and the real stores.

Routes under ``/api/v1/kb/knowledge-bases`` behind ``FEATURE_KNOWLEDGE_BASES``: the switch,
creation rights, the directory, uniform not-found, members (Q2, groups, Spaces) and their
summary, settings revisions, deletion, transfer, per-knowledge-base files, moves with their
access preview, upload targets with the retention guard, and the chat tool targets.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import zipfile
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import run_sqlite_authority_write
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.knowledge_base_store import default_knowledge_base_id
from bowhead_core.kb.management_tools import register_management_tools
from bowhead_core.kb.store import KbStore
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.tool_registry import ToolRegistry
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.knowledge_management import KnowledgeManagementAccess
from bowhead_gateway.routes import kb, kb_presign
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

BASE = "/api/v1/kb/knowledge-bases"
PEOPLE = ("owner", "tadmin", "sadmin", "mem", "mem2", "view", "out")


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    directory = tmp_path_factory.mktemp("kb-bases") / "sqlite"
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    return directory


@pytest.fixture
async def env(tmp_path, layout, monkeypatch):
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    directory = tmp_path / "sqlite"
    shutil.copytree(layout, directory, ignore=shutil.ignore_patterns("*.lock"))
    core = str(directory / "bowhead.db")
    users = UserStore(core)
    await users.initialize()
    team = await users.create_team(name="Knowledge", max_seats=20)
    ids = {
        name: (await users.create_user(team_id=team.id, email=f"{name}@example.test", password="Passw0rd!", display_name=name.title())).id
        for name in PEOPLE
    }

    async def roles(db):
        await db.execute("UPDATE teams SET created_by_user_id=? WHERE id=?", (ids["owner"], team.id))
        await db.execute("UPDATE team_members SET role='team_admin' WHERE user_id=? AND team_id=?", (ids["tadmin"], team.id))

    await run_sqlite_authority_write(core, roles)
    ws1 = await users.create_local_workspace(team_id=team.id, name="Finance", creator_user_id=ids["sadmin"])
    ws2 = await users.create_local_workspace(team_id=team.id, name="Legal", creator_user_id=ids["out"])
    for name, role in (("mem", "member"), ("mem2", "member"), ("view", "viewer"), ("tadmin", "member"), ("owner", "member")):
        await users.add_member(workspace_id=ws1, user_id=ids[name], role=role)
    other = await users.create_team(name="Other", max_seats=5)
    stranger = (await users.create_user(team_id=other.id, email="stranger@example.test", password="Passw0rd!")).id
    access = AccessControlStore(core)
    await access.initialize()
    files = KbFilesStore(directory / "kb_files.db")
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "objects"), db_path=core)
    await artifacts.initialize()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(kb.router)
    app.include_router(kb_presign.router)
    app.state.user_store = users
    app.state.access_control_store = access
    app.state.kb_files_store = files
    app.state.artifact_store = artifacts
    app.state.kb_store = KbStore(str(tmp_path / "chroma"))
    app.state.kb_worker = SimpleNamespace(parse_and_index=AsyncMock(), parse_and_index_many=AsyncMock())
    current = {"user": ids["mem"], "team": team.id}
    app.dependency_overrides[_current_user] = lambda: User(
        id=current["user"], email=f"{current['user']}@example.test", team_id=current["team"], tier="ultra", email_verified=True
    )
    return SimpleNamespace(
        directory=directory,
        core=core,
        users=users,
        access=access,
        files=files,
        artifacts=artifacts,
        app=app,
        client=TestClient(app, raise_server_exceptions=False),
        current=current,
        ids=ids,
        team=team.id,
        other_team=other.id,
        stranger=stranger,
        ws1=ws1,
        ws2=ws2,
    )


def call(env, method, path, *, user="mem", space=None, team=None, **kwargs):
    env.current["user"] = env.ids.get(user, user)
    env.current["team"] = team or env.team
    headers = {"X-Space-Id": space} if space else {}
    return env.client.request(method, path if path.startswith("/") else f"{BASE}/{path}", headers=headers, **kwargs)


def error(response):
    return response.json()["detail"]["error"]


def create(env, *, user="mem", space=None, home="space", audience="members", level="use", name="Contracts"):
    response = call(env, "POST", BASE, user=user, space=space, json={"name": name, "home": home, "audience": audience, "audience_level": level})
    assert response.status_code == 201, response.text
    return response.json()


def upload(env, *, user="mem", space=None, scope="space", scope_id=None, knowledge_base_id=None, name="a.md", body=b"# Plan\nnumbers"):
    data = {"knowledge_intent": "add_to_knowledge", "scope": scope, "scope_id": scope_id or space or ""}
    if knowledge_base_id:
        data["knowledge_base_id"] = knowledge_base_id
    return call(env, "POST", "/api/v1/kb/files", user=user, space=space, data=data, files={"file": (name, body, "text/markdown")})


def members(env, knowledge_base_id, changes, *, user="mem", space=None):
    return call(env, "PUT", f"{knowledge_base_id}/members", user=user, space=space, json={"changes": changes})


def _rows(env, sql, params=()):
    with closing(sqlite3.connect(env.directory / "kb_files.db")) as db:
        return db.execute(sql, params).fetchall()


# ─── Switch ──────────────────────────────────────────────────────────────


async def test_switch_off_answers_404_everywhere_and_ignores_the_new_fields(env, monkeypatch):
    finance = create(env, space=env.ws1)
    named = upload(env, space=env.ws1, knowledge_base_id=finance["id"])
    assert named.status_code == 200, named.text
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    probes = [
        ("GET", BASE, {}),
        ("POST", BASE, {"json": {"name": "x", "home": "personal", "audience": "owner"}}),
        ("POST", f"{BASE}/files/move", {"json": {"file_ids": ["kbf_x"], "target_knowledge_base_id": finance["id"]}}),
        ("GET", f"{BASE}/{finance['id']}", {}),
        ("PATCH", f"{BASE}/{finance['id']}", {"json": {"revision": 1, "name": "y"}}),
        ("DELETE", f"{BASE}/{finance['id']}?file_count=0", {}),
        ("GET", f"{BASE}/{finance['id']}/members", {}),
        ("PUT", f"{BASE}/{finance['id']}/members", {"json": {"changes": []}}),
        ("POST", f"{BASE}/{finance['id']}/transfer", {"json": {"user_id": "u"}}),
        ("GET", f"{BASE}/{finance['id']}/files", {}),
    ]
    for method, path, kwargs in probes:
        response = call(env, method, path, space=env.ws1, **kwargs)
        assert (response.status_code, error(response)["code"]) == (404, "knowledge_base_not_found"), (method, path, response.text)
    # The target field is ignored as before: the file goes to the Space's default, and responses keep today's fields.
    ignored = upload(env, space=env.ws1, knowledge_base_id=finance["id"], name="b.md")
    assert ignored.status_code == 200, ignored.text
    assert "knowledge_base_id" not in ignored.json()
    assert (await env.files.get(ignored.json()["file_id"])).knowledge_base_id == default_knowledge_base_id(env.team, "space", env.ws1)
    # C3: the named knowledge base's file is hidden, even from its owner, never widened to the Space.
    listed = call(env, "GET", "/api/v1/kb/files", space=env.ws1, params={"scope": "all"})
    assert {item["file_id"] for item in listed.json()["files"]} == {ignored.json()["file_id"]}
    assert call(env, "GET", f"/api/v1/kb/files/{named.json()['file_id']}", space=env.ws1).status_code == 404


# ─── Creation, directory and non-disclosure ──────────────────────────────


async def test_creation_rights_follow_the_home(env):
    assert create(env, home="personal", audience="owner")["home"] == "personal"
    assert create(env, space=env.ws1)["home_id"] == env.ws1
    viewer = call(env, "POST", BASE, user="view", space=env.ws1, json={"name": "V", "home": "space", "audience": "space"})
    assert (viewer.status_code, error(viewer)["code"]) == (403, "knowledge_base_create_forbidden")
    no_space = call(env, "POST", BASE, json={"name": "S", "home": "space", "audience": "space"})
    assert (no_space.status_code, error(no_space)["code"]) == (400, "missing_workspace_context")
    member_team = call(env, "POST", BASE, json={"name": "T", "home": "team", "audience": "team"})
    assert (member_team.status_code, error(member_team)["code"]) == (403, "knowledge_base_create_forbidden")
    assert create(env, user="tadmin", home="team", audience="team")["audience"] == "team"
    assert create(env, user="owner", home="team", audience="members")["owner"]["user_id"] == env.ids["owner"]
    for body in (
        {"name": "P", "home": "personal", "audience": "members"},
        {"name": "T", "home": "team", "audience": "team", "audience_level": "edit"},
        {"name": "S", "home": "space", "audience": "team"},
    ):
        bad = call(env, "POST", BASE, user="tadmin", space=env.ws1, json=body)
        assert (bad.status_code, error(bad)["code"]) == (422, "knowledge_base_audience_invalid"), body
    blank = call(env, "POST", BASE, json={"name": "   ", "home": "personal", "audience": "owner"})
    assert (blank.status_code, error(blank)["code"]) == (422, "knowledge_base_name_required")
    # C4: names need not be unique.
    twins = {create(env, space=env.ws1, name="Twin")["id"], create(env, user="mem2", space=env.ws1, name="Twin")["id"]}
    assert len(twins) == 2


async def test_directory_lists_usable_knowledge_bases_with_counts(env):
    finance = create(env, space=env.ws1, name="Finance plans")
    first = upload(env, space=env.ws1, knowledge_base_id=finance["id"]).json()["file_id"]
    upload(env, space=env.ws1, knowledge_base_id=finance["id"], name="b.md")
    with closing(sqlite3.connect(env.directory / "kb_files.db")) as db:
        db.execute("UPDATE kb_files SET parse_status='failed' WHERE file_id=?", (first,))
        db.commit()
    upload(env, space=env.ws1, name="default.md")
    mine = call(env, "GET", BASE, space=env.ws1).json()
    space = mine["sections"]["space"]
    assert [item["is_default"] for item in space] == [True, False]
    default, named = space
    assert (default["name"], default["level"], default["file_count"], default["audience"]) == (None, "edit", 1, "space")
    assert (named["name"], named["level"], named["file_count"], named["failed_count"]) == ("Finance plans", "manage", 2, 1)
    assert named["owner"] == {"user_id": env.ids["mem"], "name": "Mem"} and named["can"]["delete"] and not default["can"]["delete"]
    assert [item["is_default"] for item in mine["sections"]["personal"]] == [True]
    assert mine["sections"]["team"][0]["level"] == "use" and mine["can_create"] == {"personal": True, "space": True, "team": False}
    # Members-only: another Space member does not see it; a viewer can't create.
    theirs = call(env, "GET", BASE, user="mem2", space=env.ws1).json()
    assert [item["id"] for item in theirs["sections"]["space"]] == [default["id"]]
    assert call(env, "GET", BASE, user="view", space=env.ws1).json()["can_create"]["space"] is False
    # Without a current Space there is no Space section.
    assert call(env, "GET", BASE).json()["sections"]["space"] == []


async def test_one_not_found_for_every_refusal_and_403_only_with_use(env):
    finance = create(env, space=env.ws1)
    unknown = call(env, "GET", "kbase_000000000000000000000000", space=env.ws1)
    refusals = [
        call(env, "GET", finance["id"], user="mem2", space=env.ws1),  # no level
        call(env, "GET", finance["id"], user="tadmin", space=env.ws1),  # Team admins don't read members-only Space knowledge bases (Q6)
        call(env, "GET", finance["id"], user="out", space=env.ws2),  # outside the current Space
        call(env, "GET", finance["id"]),  # no Space context
        call(env, "GET", f"{finance['id']}/files", user="mem2", space=env.ws1),
        call(
            env,
            "PUT",
            f"{finance['id']}/members",
            user="mem2",
            space=env.ws1,
            json={"changes": [{"principal_type": "user", "principal_id": "x", "level": "use"}]},
        ),
    ]
    for refused in refusals:
        assert (refused.status_code, refused.content) == (unknown.status_code, unknown.content)
    assert unknown.status_code == 404 and finance["id"] not in unknown.text
    assert (
        members(env, finance["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "use"}], space=env.ws1).status_code == 200
    )
    assert call(env, "GET", finance["id"], user="mem2", space=env.ws1).json()["level"] == "use"
    for method, path, kwargs in (
        ("PATCH", finance["id"], {"json": {"revision": 1, "name": "Mine now"}}),
        ("PUT", f"{finance['id']}/members", {"json": {"changes": [{"principal_type": "user", "principal_id": env.ids["view"], "level": "use"}]}}),
    ):
        refused = call(env, method, path, user="mem2", space=env.ws1, **kwargs)
        assert (refused.status_code, error(refused)["code"], error(refused)["required_level"]) == (403, "knowledge_base_level_required", "manage")
    # Listed managers cannot delete or transfer: those need the owner or an automatic manager.
    members(env, finance["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "manage"}], space=env.ws1)
    refused = call(env, "DELETE", f"{finance['id']}?file_count=0", user="mem2", space=env.ws1)
    assert (refused.status_code, error(refused)["required_level"]) == (403, "owner")
    assert call(env, "GET", finance["id"], user="sadmin", space=env.ws1).json()["can"]["delete"]  # Space admins manage automatically


# ─── Members ─────────────────────────────────────────────────────────────


async def test_member_entries_follow_the_picker_rules(env):
    finance = create(env, space=env.ws1)
    team_group = await env.access.create_group(team_id=env.team, name="Analysts")
    await env.access.add_group_member(team_id=env.team, group_id=team_group.id, user_id=env.ids["mem2"])
    await env.access.add_group_member(team_id=env.team, group_id=team_group.id, user_id=env.ids["out"])
    other_space_group = await env.access.create_group(team_id=env.team, name="Legal team", workspace_id=env.ws2)
    gone = await env.access.create_group(team_id=env.team, name="Gone")
    with closing(connect_sync(env.core)) as db:
        db.execute("UPDATE groups SET status='removed' WHERE id=?", (gone.id,))
        db.commit()

    def refused(principal_type, principal_id, *, target=finance["id"], user="mem", space=env.ws1):
        response = members(env, target, [{"principal_type": principal_type, "principal_id": principal_id, "level": "use"}], user=user, space=space)
        assert (response.status_code, error(response)["code"]) == (422, "knowledge_base_member_invalid"), response.text
        return error(response)["reason"]

    assert refused("user", env.ids["out"]) == "not_in_space"  # Q2
    assert refused("user", env.stranger) == "not_in_team"
    assert refused("user", env.ids["mem"]) == "owner"
    assert refused("group", other_space_group.id) == "group_not_applicable"
    assert refused("group", gone.id) == "group_inactive"
    assert refused("space", env.ws2) == "space_entries_team_only"
    added = members(
        env,
        finance["id"],
        [
            {"principal_type": "user", "principal_id": env.ids["view"], "level": "edit"},
            {"principal_type": "group", "principal_id": team_group.id, "level": "use"},
        ],
        space=env.ws1,
    ).json()
    # Owner and Space admin manage; mem2 uses through the group (out is not a Space member); the viewer is capped at use.
    assert added["summary"] == {"use": 2, "edit": 0, "manage": 2, "people": 4}
    assert {item["user_id"] for item in added["automatic_managers"]} == {env.ids["sadmin"]}
    assert {(item["principal_type"], item["state"]) for item in added["entries"]} == {("user", "active"), ("group", "active")}
    read_only = call(env, "GET", f"{finance['id']}/members", user="view", space=env.ws1).json()
    assert read_only["can_manage"] is False and read_only["summary"] == added["summary"]
    removed = members(env, finance["id"], [{"principal_type": "group", "principal_id": team_group.id, "level": None}], space=env.ws1).json()
    assert removed["summary"]["people"] == 3

    legal = create(env, user="tadmin", home="team", audience="members", name="Legal")
    assert refused("group", other_space_group.id, target=legal["id"], user="tadmin", space=None) == "group_not_applicable"
    assert refused("space", "ws_missing", target=legal["id"], user="tadmin", space=None) == "space_not_found"
    shared = members(
        env,
        legal["id"],
        [
            {"principal_type": "space", "principal_id": env.ws2, "level": "edit"},
            {"principal_type": "group", "principal_id": team_group.id, "level": "use"},
        ],
        user="tadmin",
    ).json()
    # C9: ws2's members (out) edit; mem2 uses through the group; the Team owner and admin manage.
    assert shared["summary"] == {"use": 1, "edit": 1, "manage": 2, "people": 4}
    assert {item["user_id"] for item in shared["automatic_managers"]} == {env.ids["owner"]}
    mine = create(env, home="personal", audience="owner")
    shared_mine = members(env, mine["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "use"}])
    assert (shared_mine.status_code, error(shared_mine)["code"]) == (422, "knowledge_base_not_shareable")


async def test_settings_need_the_current_revision_and_defaults_stay_fixed(env):
    finance = create(env, space=env.ws1)
    stale = call(env, "PATCH", finance["id"], space=env.ws1, json={"revision": 7, "name": "Renamed"})
    assert (stale.status_code, error(stale)["code"]) == (409, "knowledge_base_revision_conflict")
    renamed = call(env, "PATCH", finance["id"], space=env.ws1, json={"revision": 1, "name": "Renamed", "audience": "space", "audience_level": "edit"})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["audience"], renamed.json()["revision"]) == ("Renamed", "space", 2)
    # The audience now lets every Space member edit; viewers stay at use.
    assert call(env, "GET", finance["id"], user="mem2", space=env.ws1).json()["level"] == "edit"
    assert call(env, "GET", finance["id"], user="view", space=env.ws1).json()["level"] == "use"
    default = default_knowledge_base_id(env.team, "space", env.ws1)
    for body in ({"revision": 1, "name": "Space"}, {"revision": 1, "audience_level": "use"}, {"revision": 1, "audience": "members"}):
        fixed = call(env, "PATCH", default, user="sadmin", space=env.ws1, json=body)
        assert (fixed.status_code, error(fixed)["code"]) == (422, "knowledge_base_default_fixed"), body
    same = call(env, "PATCH", default, user="sadmin", space=env.ws1, json={"revision": 1, "audience_level": "edit"})
    assert same.status_code == 200 and same.json()["is_default"]


async def test_delete_takes_files_out_of_knowledge_and_keeps_the_originals(env):
    finance = create(env, space=env.ws1)
    uploaded = [upload(env, space=env.ws1, knowledge_base_id=finance["id"], name=f"{index}.md").json() for index in range(2)]
    wrong = call(env, "DELETE", f"{finance['id']}?file_count=1", space=env.ws1)
    assert (wrong.status_code, error(wrong)["code"], error(wrong)["file_count"]) == (409, "knowledge_base_files_changed", 2)
    deleted = call(env, "DELETE", f"{finance['id']}?file_count=2", space=env.ws1)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "knowledge_base_id": finance["id"], "files_removed": 2}
    for item in uploaded:
        assert (await env.files.get(item["file_id"])).soft_deleted_at
        assert await env.artifacts.get(item["artifact_id"]) is not None  # Q5: originals stay in Files
    assert call(env, "GET", finance["id"], space=env.ws1).status_code == 404
    assert _rows(env, "SELECT deleted_at IS NOT NULL FROM kb_knowledge_bases WHERE id=?", (finance["id"],)) == [(1,)]
    default = call(env, "DELETE", f"{default_knowledge_base_id(env.team, 'space', env.ws1)}?file_count=0", user="sadmin", space=env.ws1)
    assert (default.status_code, error(default)["code"]) == (409, "knowledge_base_default_not_deletable")
    # A deleted knowledge base takes no new files.
    late = upload(env, space=env.ws1, knowledge_base_id=finance["id"], name="late.md")
    assert late.status_code == 404


async def test_transfer_goes_to_another_manager(env):
    finance = create(env, space=env.ws1)
    members(env, finance["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "use"}], space=env.ws1)
    refused = call(env, "POST", f"{finance['id']}/transfer", space=env.ws1, json={"user_id": env.ids["mem2"]})
    assert (refused.status_code, error(refused)["code"]) == (422, "knowledge_base_transfer_target_invalid")
    members(env, finance["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "manage"}], space=env.ws1)
    moved = call(env, "POST", f"{finance['id']}/transfer", space=env.ws1, json={"user_id": env.ids["mem2"], "revision": 1})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["owner"]["user_id"], moved.json()["level"], moved.json()["can"]["delete"]) == (env.ids["mem2"], "manage", False)
    entries = call(env, "GET", f"{finance['id']}/members", space=env.ws1).json()["entries"]
    assert [(item["principal_id"], item["level"]) for item in entries] == [(env.ids["mem"], "manage")]
    again = call(env, "POST", f"{finance['id']}/transfer", space=env.ws1, json={"user_id": env.ids["mem"]})
    assert (again.status_code, error(again)["required_level"]) == (403, "owner")
    mine = create(env, home="personal", audience="owner")
    personal = call(env, "POST", f"{mine['id']}/transfer", json={"user_id": env.ids["mem2"]})
    assert (personal.status_code, error(personal)["code"]) == (422, "knowledge_base_not_transferable")


async def test_one_knowledge_base_lists_only_its_files_with_todays_filters(env):
    finance = create(env, space=env.ws1)
    inside = {upload(env, space=env.ws1, knowledge_base_id=finance["id"], name=f"plan-{index}.md").json()["file_id"] for index in range(3)}
    outside = upload(env, space=env.ws1, name="plan-default.md").json()["file_id"]
    listed = call(env, "GET", f"{finance['id']}/files", space=env.ws1, params={"limit": 2, "order_by": "filename", "order_dir": "asc"}).json()
    assert listed["total"] == 3 and listed["has_more"] and len(listed["files"]) == 2
    rest = call(
        env,
        "GET",
        f"{finance['id']}/files",
        space=env.ws1,
        params={"limit": 2, "order_by": "filename", "order_dir": "asc", "cursor": listed["next_cursor"]},
    ).json()
    assert {item["file_id"] for item in listed["files"] + rest["files"]} == inside
    assert all(item["can_delete"] for item in listed["files"])
    searched = call(env, "GET", f"{finance['id']}/files", space=env.ws1, params={"q": "plan-1"}).json()
    assert [item["filename"] for item in searched["files"]] == ["plan-1.md"]
    default = call(env, "GET", f"{default_knowledge_base_id(env.team, 'space', env.ws1)}/files", space=env.ws1).json()
    assert {item["file_id"] for item in default["files"]} == {outside}


# ─── Moving files ────────────────────────────────────────────────────────


async def _index(env, file_id, seed):
    file = await env.files.get(file_id)
    metadata = ChunkMetadata(
        file_id=file_id,
        file_name=file.filename,
        scope=file.scope,
        scope_owner=file.scope_id,
        workspace_id=file.workspace_id,
        team_id=file.team_id,
        chunk_index=1,
        knowledge_base_id=file.knowledge_base_id or "",
    )
    embedding = [((seed * 31 + index) % 17) / 17 for index in range(16)]
    await env.app.state.kb_store.add_chunks(
        file, [KbChunk(chunk_id=make_chunk_id(file_id, 1, file_id), file_id=file_id, content=file_id, embedding=embedding, metadata=metadata)]
    )
    return embedding


def _chunks(env, collection, file_id):
    store = env.app.state.kb_store
    opened = store._open_collection(collection, create=False)
    if opened is None:
        return [], [], []
    got = opened.get(where={"file_id": file_id}, include=["embeddings", "metadatas"])
    return got["ids"], [list(map(float, vector)) for vector in got["embeddings"]], got["metadatas"]


async def test_moves_within_a_home_and_across_collections_keep_embeddings(env):
    store = env.app.state.kb_store
    finance = create(env, space=env.ws1)
    legal = create(env, user="tadmin", home="team", audience="members", name="Legal")
    members(env, legal["id"], [{"principal_type": "user", "principal_id": env.ids["mem"], "level": "edit"}], user="tadmin")
    file_id = upload(env, space=env.ws1).json()["file_id"]
    embedding = await _index(env, file_id, 3)
    space_collection, team_collection = store._ws_collection_name(env.ws1), store._team_collection_name(env.team)
    chunk_ids, _, _ = _chunks(env, space_collection, file_id)

    within = call(env, "POST", f"{BASE}/files/move", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": finance["id"]})
    assert within.status_code == 200 and within.json()["succeeded"] == 1, within.text
    assert (await env.files.get(file_id)).knowledge_base_id == finance["id"]
    ids, vectors, metadatas = _chunks(env, space_collection, file_id)
    assert ids == chunk_ids and metadatas[0]["knowledge_base_id"] == finance["id"]
    assert vectors[0] == pytest.approx(embedding)  # metadata only: never re-embedded

    across = call(env, "POST", f"{BASE}/files/move", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": legal["id"]})
    assert across.status_code == 200 and across.json()["succeeded"] == 1, across.text
    moved = await env.files.get(file_id)
    assert (moved.scope, moved.scope_id, moved.knowledge_base_id) == ("team", env.team, legal["id"])
    assert _chunks(env, space_collection, file_id)[0] == []
    ids, vectors, metadatas = _chunks(env, team_collection, file_id)
    assert ids == chunk_ids and (metadatas[0]["knowledge_base_id"], metadatas[0]["scope"]) == (legal["id"], "team")
    assert vectors[0] == pytest.approx(embedding)  # copied with its embedding
    # Moving into the knowledge base it is in already changes nothing.
    again = call(env, "POST", f"{BASE}/files/move", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": legal["id"]}).json()
    assert again["unchanged"] == 1 and again["results"][0] == {"file_id": file_id, "ok": True, "unchanged": True}


async def test_move_preview_counts_gains_and_losses_and_writes_nothing(env):
    finance = create(env, space=env.ws1)
    members(env, finance["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "use"}], space=env.ws1)
    file_id = upload(env, space=env.ws1).json()["file_id"]
    preview = call(
        env, "POST", f"{BASE}/files/move", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": finance["id"], "dry_run": True}
    )
    assert preview.status_code == 200, preview.text
    # The Space default reaches all six ws1 members; Finance reaches its owner, the Space admin and mem2.
    assert {key: preview.json()[key] for key in ("dry_run", "movable", "gains", "losses")} == {"dry_run": True, "movable": 1, "gains": 0, "losses": 3}
    assert (await env.files.get(file_id)).knowledge_base_id == default_knowledge_base_id(env.team, "space", env.ws1)
    mine = create(env, home="personal", audience="owner")
    to_me = call(
        env, "POST", f"{BASE}/files/move", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": mine["id"], "dry_run": True}
    ).json()
    assert (to_me["gains"], to_me["losses"]) == (0, 5)
    # mem2 can only use Finance: 403; a knowledge base they cannot see: 404; someone else's file without manage: per item 403.
    use_only = call(
        env, "POST", f"{BASE}/files/move", user="mem2", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": finance["id"]}
    )
    assert (use_only.status_code, error(use_only)["code"]) == (403, "knowledge_base_level_required")
    hidden = call(env, "POST", f"{BASE}/files/move", user="mem2", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": mine["id"]})
    assert hidden.status_code == 404
    theirs = create(env, user="mem2", space=env.ws1, name="Theirs")
    others = call(
        env, "POST", f"{BASE}/files/move", user="mem2", space=env.ws1, json={"file_ids": [file_id], "target_knowledge_base_id": theirs["id"]}
    ).json()
    assert others["results"] == [{"file_id": file_id, "ok": False, "error_code": "kb_file_access_denied"}]


# ─── Upload targets ──────────────────────────────────────────────────────


async def test_upload_targets_need_edit_on_a_visible_knowledge_base_in_the_scope_home(env):
    finance = create(env, space=env.ws1)
    assert upload(env, space=env.ws1, knowledge_base_id=finance["id"]).json()["knowledge_base_id"] == finance["id"]
    hidden = upload(env, user="mem2", space=env.ws1, knowledge_base_id=finance["id"])
    assert (hidden.status_code, error(hidden)["code"]) == (404, "knowledge_base_not_found")
    members(env, finance["id"], [{"principal_type": "user", "principal_id": env.ids["mem2"], "level": "use"}], space=env.ws1)
    use_only = upload(env, user="mem2", space=env.ws1, knowledge_base_id=finance["id"])
    assert (use_only.status_code, error(use_only)["code"]) == (403, "knowledge_base_level_required")
    mismatch = upload(env, space=env.ws1, scope="personal", scope_id=env.ids["mem"], knowledge_base_id=finance["id"])
    assert (mismatch.status_code, error(mismatch)["code"]) == (422, "knowledge_base_scope_mismatch")
    # A ZIP's files all join the target.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one.md", "# One")
        bundle.writestr("two.md", "# Two")
    zipped = call(
        env,
        "POST",
        "/api/v1/kb/files/bundle",
        space=env.ws1,
        data={"knowledge_intent": "add_to_knowledge", "scope": "space", "scope_id": env.ws1, "knowledge_base_id": finance["id"]},
        files={"file": ("both.zip", archive.getvalue(), "application/zip")},
    )
    assert zipped.status_code == 200, zipped.text
    assert {item["knowledge_base_id"] for item in zipped.json()["uploaded"]} == {finance["id"]}
    # The presign entry checks the target before issuing anything.
    presign = call(
        env,
        "POST",
        "/api/v1/kb/presign",
        user="out",
        space=env.ws2,
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "a.md",
            "mime": "text/markdown",
            "size_bytes": 5,
            "scope": "space",
            "scope_id": env.ws2,
            "knowledge_base_id": finance["id"],
        },
    )
    assert (presign.status_code, error(presign)["code"]) == (404, "knowledge_base_not_found")


async def test_add_from_files_targets_a_knowledge_base_and_maps_the_retention_guard(env, monkeypatch):
    finance = create(env, space=env.ws1)

    async def artifact(name):
        return await env.artifacts.put_bytes(
            content=b"# Notes",
            filename=name,
            mime="text/markdown",
            source="upload",
            kind="user_upload",
            created_by=env.ids["mem"],
            workspace_id=env.ws1,
        )

    kept = await artifact("kept.md")
    body = {
        "artifact_id": kept.id,
        "scope": "space",
        "knowledge_intent": "add_to_knowledge",
        "action_id": "act_kept",
        "knowledge_base_id": finance["id"],
    }
    added = call(env, "POST", "/api/v1/kb/files/from-artifact", space=env.ws1, json=body)
    assert added.status_code == 201, added.text
    assert added.json()["knowledge_base_id"] == finance["id"]
    receipt = _rows(env, "SELECT destination_json FROM kb_membership_operations WHERE file_id=?", (added.json()["file_id"],))
    assert json.loads(receipt[0][0]) == {"scope": "space", "scope_id": env.ws1, "team_id": env.team, "knowledge_base_id": finance["id"]}
    # Cleanup trashed and claimed this original after the route read it: the 0238 guard refuses
    # the Knowledge row in its own transaction, and the answer says what to do.
    claimed = await artifact("claimed.md")
    assert await env.artifacts.soft_delete(claimed.id, user_id=env.ids["mem"])
    with closing(connect_sync(env.directory / "bowhead_content.db")) as db:
        db.execute(
            "INSERT INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) VALUES (?,?,?,?)",
            (claimed.id, claimed.path, env.ids["mem"], "2026-11-27T00:00:00+00:00"),
        )
        db.commit()
    read_before = env.artifacts.get

    async def stale_read(artifact_id, *args, **kwargs):
        return claimed if artifact_id == claimed.id else await read_before(artifact_id, *args, **kwargs)

    monkeypatch.setattr(env.artifacts, "get", stale_read)
    refused = call(env, "POST", "/api/v1/kb/files/from-artifact", space=env.ws1, json={**body, "artifact_id": claimed.id, "action_id": "act_claimed"})
    assert (refused.status_code, error(refused)["code"]) == (409, "knowledge_source_in_trash")
    assert "Restore" in error(refused)["hint"]
    assert await env.files.find_active_by_artifact(claimed.id) is None


async def test_maintenance_scan_observes_named_files_without_granting_access(env):
    from bowhead_core.kb.permission import KbPermission, ScopeContext
    from bowhead_gateway.kb_maintenance import _maintenance_levels

    finance = create(env, space=env.ws1)
    named = await env.files.get(upload(env, space=env.ws1, knowledge_base_id=finance["id"]).json()["file_id"])
    default = await env.files.get(upload(env, space=env.ws1, name="d.md").json()["file_id"])
    scan = _maintenance_levels(named)
    # The scan's own context reads the physical file; it was refused before (no levels).
    assert KbPermission.can_read(named, ScopeContext(user_id=named.uploaded_by, workspace_id=env.ws1, team_id=env.team, knowledge_levels=scan))
    assert not KbPermission.can_read(named, ScopeContext(user_id=named.uploaded_by, workspace_id=env.ws1, team_id=env.team))
    assert _maintenance_levels(default) is None and list(scan) == [finance["id"]]


# ─── Chat management tools ───────────────────────────────────────────────


async def _tool_context(env, text, *, scope="space"):
    sessions = SessionStore(env.core)
    await sessions.initialize()
    session = await sessions.ensure_session(session_id=None, workspace_id=env.ws1, user_id=env.ids["mem"])
    artifact = await env.artifacts.put_bytes(
        content=b"# Policy",
        filename="Policy.md",
        mime="text/markdown",
        source="upload",
        kind="user_upload",
        created_by=env.ids["mem"],
        workspace_id=env.ws1,
    )
    turn_id = "t_kbtarget0001"
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session.id,
        user_id=env.ids["mem"],
        workspace_id=env.ws1,
        user_text=text,
        attachment_ids=(artifact.id,),
        attachment_refs=(),
        parts=None,
    )
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text=text, user_source=source)
    decision = {"authorized": True, "intent": "add", "scope": scope, "target": "one_attachment", "evidence": text}
    router = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(decision))))
    registry = ToolRegistry()
    register_management_tools(registry)
    ctx = SimpleNamespace(
        user_id=env.ids["mem"],
        team_id=env.team,
        workspace_id=env.ws1,
        session_id=session.id,
        turn_id=turn_id,
        session_store=sessions,
        router=router,
        knowledge_user_request=True,
        knowledge_management_access=KnowledgeManagementAccess(env.app),
    )
    return registry, ctx, artifact


async def test_chat_tools_ask_before_writing_when_the_target_is_unclear_or_not_editable(env, monkeypatch):
    env.current["user"] = env.ids["mem"]
    finance = create(env, space=env.ws1, name="Finance")
    registry, ctx, artifact = await _tool_context(env, "Add this file to the Finance knowledge base")
    add = registry.get("kb_add_file").handler
    # The model sees the knowledge base field only while the switch is on.
    schema = next(item for item in registry.to_schemas(["kb_add_file"]) if item.name == "kb_add_file")
    assert "knowledge_base_id" in schema.input_schema["properties"]

    unspecified = (await add({"artifact_id": artifact.id, "scope": "space"}, ctx)).result
    assert (unspecified["ok"], unspecified["error"], unspecified["reason"]) == (False, "knowledge_base_needs_clarification", "unspecified")
    assert {item["knowledge_base_id"] for item in unspecified["knowledge_bases"]} == {
        finance["id"],
        default_knowledge_base_id(env.team, "space", env.ws1),
    }
    theirs = create(env, user="mem2", space=env.ws1, name="Theirs")
    hidden = (await add({"artifact_id": artifact.id, "scope": "space", "knowledge_base_id": theirs["id"]}, ctx)).result
    assert (hidden["error"], hidden["reason"]) == ("knowledge_base_needs_clarification", "not_found")
    assert ctx.router.invoke.await_count == 0  # asked before any intent check or write
    assert await env.files.find_active_by_artifact(artifact.id) is None

    added = (await add({"artifact_id": artifact.id, "scope": "space", "knowledge_base_id": finance["id"]}, ctx)).result
    assert added["ok"], added
    assert (await env.files.get(added["file_id"])).knowledge_base_id == finance["id"]
    packet = json.loads(ctx.router.invoke.call_args.args[1].messages[0].content)
    assert packet["proposal"]["knowledge_base"] == {"name": "Finance", "home": "space"}

    remove = registry.get("kb_remove_file").handler
    elsewhere = (await remove({"file_id": added["file_id"], "knowledge_base_id": theirs["id"]}, ctx)).result
    assert (elsewhere["error"], elsewhere["reason"]) == ("knowledge_base_needs_clarification", "not_in_knowledge_base")
    assert [item["knowledge_base_id"] for item in elsewhere["knowledge_bases"]] == [finance["id"]]
    assert not (await env.files.get(added["file_id"])).soft_deleted_at

    # Off, the field is refused exactly as an unknown field was.
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    off = (await add({"artifact_id": artifact.id, "scope": "space", "knowledge_base_id": finance["id"]}, ctx)).result
    assert off["error"] == "knowledge_arguments_invalid"
    assert "knowledge_base_id" not in next(item for item in registry.to_schemas(["kb_add_file"])).input_schema["properties"]
