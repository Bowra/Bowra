"""Knowledge base grant report (organization phase 1, slice 6) against a migrated layout and the real stores.

``GET /api/v1/resource-grants/knowledge-base-report`` behind ``FEATURE_KNOWLEDGE_BASES``: the switch,
the grant management guard, names instead of raw IDs (within the caller's read rights), deleted and
unresolvable targets, what each grant restricts with enforcement on or off, the member mapping for
scope and file grants on default and named knowledge bases, keyset pages, and that nothing changes.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import run_sqlite_authority_write
from bowhead_core.access_control import AccessControlStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.knowledge_base_store import KnowledgeBaseStore, default_knowledge_base_id
from bowhead_core.user_store import UserStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

SECRET = "test-secret-long-enough-for-hs256-32-bytes"
REPORT = "/api/v1/resource-grants/knowledge-base-report"
PEOPLE = ("owner", "tadmin", "mem", "audit", "gone", "out")


def _jwt(user_id: str) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + 3600, "pwd_v": 0}, SECRET, algorithm="HS256")


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    directory = tmp_path_factory.mktemp("kb-grant-report") / "sqlite"
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    return directory


@pytest.fixture
async def env(tmp_path, layout, monkeypatch):
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "1")
    monkeypatch.delenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", raising=False)
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
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
        await db.execute("UPDATE team_members SET role='audit_admin' WHERE user_id=? AND team_id=?", (ids["audit"], team.id))
        await db.execute("UPDATE team_members SET status='removed' WHERE user_id=? AND team_id=?", (ids["gone"], team.id))

    await run_sqlite_authority_write(core, roles)
    finance = await users.create_local_workspace(team_id=team.id, name="Finance", creator_user_id=ids["owner"])
    legal = await users.create_local_workspace(team_id=team.id, name="Legal", creator_user_id=ids["out"])
    closed = await users.create_local_workspace(team_id=team.id, name="Closed", creator_user_id=ids["owner"])
    for name in ("tadmin", "mem"):
        await users.add_member(workspace_id=finance, user_id=ids[name], role="member")

    async def archive(db):
        await db.execute("UPDATE workspaces SET status='archived' WHERE id=?", (closed,))

    await run_sqlite_authority_write(core, archive)
    other = await users.create_team(name="Other", max_seats=5)
    stranger = await users.create_user(team_id=other.id, email="stranger@example.test", password="Passw0rd!")
    foreign_space = await users.create_local_workspace(team_id=other.id, name="Foreign", creator_user_id=stranger.id)
    access = AccessControlStore(core)
    await access.initialize()
    files = KbFilesStore(directory / "kb_files.db")

    from bowhead_gateway.routes import access_control as access_routes
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    app = FastAPI()
    app.state.user_store = users
    app.state.access_control_store = access
    app.state.kb_files_store = files
    app.state.settings = SimpleNamespace(dev_default_user="dev_user", auth_mode="prod")
    app.include_router(access_routes.router, prefix="/api/v1")
    return SimpleNamespace(
        directory=directory,
        users=users,
        access=access,
        files=files,
        bases=KnowledgeBaseStore(directory / "kb_files.db"),
        client=TestClient(app),
        ids=ids,
        team=team.id,
        finance=finance,
        legal=legal,
        closed=closed,
        foreign_space=foreign_space,
    )


def get(env, path=REPORT, *, user="tadmin", **params):
    headers = {"Cookie": f"bowhead_jwt={_jwt(env.ids[user])}"} if user else {}
    return env.client.get(path, params=params, headers=headers)


def report(env, *, user="tadmin", **params):
    response = get(env, user=user, **params)
    assert response.status_code == 200, response.text
    return response.json()


async def grant(env, kind, resource_id, principal_type, principal_id, permission="read", effect="allow", expires_at=None):
    return await env.access.create_or_update_resource_grant(
        team_id=env.team,
        resource_kind=kind,
        resource_id=resource_id,
        principal_type=principal_type,
        principal_id=principal_id,
        permission=permission,
        effect=effect,
        created_by=env.ids["owner"],
        expires_at=expires_at,
    )


async def kb_file(env, name, *, scope, scope_id, by="mem", workspace=""):
    return await env.files.create(
        artifact_id=f"art_{name}",
        filename=name,
        mime_type="text/markdown",
        size_bytes=10,
        scope=scope,
        scope_id=scope_id,
        workspace_id=workspace,
        team_id=env.team,
        uploaded_by=env.ids[by],
    )


async def named(env, name, *, home, home_id, audience="members", owner="owner", members=()):
    row = await env.bases.create(
        team_id=env.team,
        home=home,
        home_id=home_id,
        name=name,
        description=None,
        audience=audience,
        audience_level="use",
        owner_user_id=env.ids[owner],
    )
    if members:
        await env.bases.apply_member_changes(row, [("user", env.ids[user], level) for user, level in members], added_by=env.ids[owner])
    return row


async def move(env, file_id, knowledge_base):
    file = await env.files.get(file_id)
    assert await env.files.set_knowledge_base(file_id, knowledge_base.id, expected=file.knowledge_base_id)


def by_grant(body):
    return {item["grant_id"]: item for item in body["items"]}


# ─── Switch and guard ────────────────────────────────────────────────────


async def test_switch_off_answers_like_an_unknown_route(env, monkeypatch):
    await grant(env, "kb_scope", f"workspace:{env.finance}", "user", env.ids["mem"])
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    for user in ("tadmin", "mem", None):
        response = get(env, user=user)
        assert (response.status_code, response.json()) == (404, {"detail": "Not Found"}), (user, response.text)
    # Grant management itself is unchanged.
    assert get(env, "/api/v1/resource-grants").status_code == 200


async def test_guard_is_the_grant_management_guard(env):
    await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids["mem"])
    for user in ("owner", "tadmin", "mem", "audit", None):
        manage = get(env, "/api/v1/resource-grants", user=user)
        view = get(env, user=user)
        assert view.status_code == manage.status_code, (user, view.text, manage.text)
    assert get(env, user="owner").status_code == 200
    assert get(env, user="mem").status_code == 403
    assert get(env, user="audit").status_code == 403
    assert get(env, user=None).status_code == 401


# ─── Targets, names and mapping ──────────────────────────────────────────


async def test_scope_grants_name_the_home_and_map_to_its_knowledge_bases(env):
    contracts = await named(env, "Contracts", home="space", home_id=env.finance, members=[("tadmin", "use")])
    await named(env, "Litigation", home="space", home_id=env.legal, owner="out")
    policies = await named(env, "Policies", home="team", home_id=env.team, audience="team")
    space_allow = await grant(env, "kb_scope", f"workspace:{env.finance}", "user", env.ids["mem"], "read")
    legal_deny = await grant(env, "kb_scope", f"workspace:{env.legal}", "user", env.ids["mem"], "write", "deny")
    team_allow = await grant(env, "kb_scope", f"team:{env.team}", "workspace", env.finance, "write")
    archived = await grant(env, "kb_scope", f"workspace:{env.closed}", "user", env.ids["mem"])
    foreign = await grant(env, "kb_scope", f"workspace:{env.foreign_space}", "user", env.ids["mem"])
    legacy = await grant(env, "kb_scope", f"space:{env.finance}", "user", env.ids["mem"])

    items = by_grant(report(env))

    item = items[space_allow.id]
    assert item["target"] == {
        "kind": "kb_scope",
        "home": "space",
        "space_name": "Finance",
        "file_name": None,
        "state": "active",
        "knowledge_base": None,
    }
    assert item["applies_to"] == {"type": "user", "id": env.ids["mem"], "label": "Mem", "state": "active", "space_name": None, "member_count": None}
    assert item["restriction"] == {"kind": "allow_list", "actions": ["read"], "reason": None}
    mapping = item["mapping"]
    assert (mapping["action"], mapping["level"], mapping["whole_home"], mapping["open_audience"]) == ("needs_level", "use", True, True)
    assert [(kb["id"], kb["is_default"], kb["name"], kb["home_name"]) for kb in mapping["knowledge_bases"]] == [
        (default_knowledge_base_id(env.team, "space", env.finance), True, None, "Finance"),
        (contracts.id, False, "Contracts", "Finance"),
    ]

    # Legal: the caller is not a member, so only its default is named; its members-only knowledge base stays undisclosed.
    item = items[legal_deny.id]
    assert item["target"]["space_name"] == "Legal"
    assert item["restriction"] == {"kind": "deny", "actions": ["read", "write"], "reason": None}
    assert item["mapping"]["action"] == "keep_out"
    assert [kb["id"] for kb in item["mapping"]["knowledge_bases"]] == [default_knowledge_base_id(env.team, "space", env.legal)]
    assert "Litigation" not in str(items)

    # Team: Team admins manage every Team knowledge base, so all are named; a Space principal maps to a Space entry.
    item = items[team_allow.id]
    assert item["applies_to"]["label"] == "Finance" and item["applies_to"]["type"] == "workspace"
    assert (item["mapping"]["action"], item["mapping"]["level"]) == ("needs_level", "edit")
    assert [kb["id"] for kb in item["mapping"]["knowledge_bases"]] == [default_knowledge_base_id(env.team, "team", env.team), policies.id]

    assert items[archived.id]["target"]["state"] == "space_archived"
    assert items[archived.id]["mapping"]["action"] == "needs_level"
    for unavailable, state in ((foreign, "not_found"), (legacy, "unrecognized")):
        assert items[unavailable.id]["target"]["state"] == state
        assert items[unavailable.id]["mapping"] == {
            "action": "none",
            "level": None,
            "knowledge_bases": [],
            "more": 0,
            "whole_home": False,
            "open_audience": False,
            "reason": "target_unavailable",
        }
    # Names, never the stored IDs, label every target the caller can see.
    assert env.finance not in str([items[space_allow.id]["target"], items[legal_deny.id]["target"]])


async def test_file_grants_follow_the_callers_read_rights(env):
    contracts = await named(env, "Contracts", home="space", home_id=env.finance, members=[("tadmin", "use"), ("mem", "edit")])
    policies = await named(env, "Policies", home="team", home_id=env.team)
    budget = await kb_file(env, "budget.md", scope="space", scope_id=env.finance, workspace=env.finance)
    nda = await kb_file(env, "nda.md", scope="space", scope_id=env.finance, workspace=env.finance)
    await move(env, nda, contracts)
    handbook = await kb_file(env, "handbook.md", scope="team", scope_id=env.team, by="owner")
    await move(env, handbook, policies)
    brief = await kb_file(env, "brief.md", scope="space", scope_id=env.legal, by="out", workspace=env.legal)
    diary = await kb_file(env, "diary.md", scope="personal", scope_id=env.ids["mem"])
    removed = await kb_file(env, "removed.md", scope="space", scope_id=env.finance, workspace=env.finance)
    await env.files.soft_delete(removed)
    grants = {
        "budget": await grant(env, "kb_file", budget, "team", env.team, "read", "deny"),
        "nda": await grant(env, "kb_file", nda, "user", env.ids["mem"], "read"),
        "handbook": await grant(env, "kb_file", handbook, "user", env.ids["mem"], "write"),
        "brief": await grant(env, "kb_file", brief, "user", env.ids["mem"], "read"),
        "diary": await grant(env, "kb_file", diary, "user", env.ids["mem"], "read", "deny"),
        "removed": await grant(env, "kb_file", removed, "user", env.ids["mem"]),
        "missing": await grant(env, "kb_file", "kbf_missing0000", "user", env.ids["mem"]),
    }

    items = {name: by_grant(report(env))[record.id] for name, record in grants.items()}

    # Default knowledge base of a Space the caller is in.
    target = items["budget"]["target"]
    assert (target["file_name"], target["space_name"], target["state"]) == ("budget.md", "Finance", "active")
    assert target["knowledge_base"]["id"] == default_knowledge_base_id(env.team, "space", env.finance)
    assert target["knowledge_base"]["is_default"] is True and target["knowledge_base"]["home_name"] == "Finance"
    assert items["budget"]["applies_to"] == {
        "type": "team",
        "id": env.team,
        "label": None,
        "state": "active",
        "space_name": None,
        "member_count": None,
    }
    mapping = items["budget"]["mapping"]
    assert (mapping["action"], mapping["whole_home"], mapping["open_audience"]) == ("keep_out", False, True)

    # Named knowledge bases the caller can use are named, with the level the grant keeps.
    assert items["nda"]["target"]["knowledge_base"]["name"] == "Contracts"
    mapping = items["nda"]["mapping"]
    assert (mapping["action"], mapping["level"], mapping["open_audience"]) == ("needs_level", "use", False)
    assert [kb["id"] for kb in mapping["knowledge_bases"]] == [contracts.id]
    assert items["handbook"]["target"]["knowledge_base"]["id"] == policies.id
    assert (items["handbook"]["mapping"]["level"], items["handbook"]["mapping"]["knowledge_bases"][0]["name"]) == ("edit", "Policies")

    # A Space file the caller cannot open: its Space is named, the file and its knowledge base are not.
    target = items["brief"]["target"]
    assert (target["file_name"], target["space_name"], target["state"], target["knowledge_base"]) == (None, "Legal", "hidden", None)
    assert (items["brief"]["mapping"]["action"], items["brief"]["mapping"]["knowledge_bases"]) == ("needs_level", [])
    assert "brief.md" not in str(items)

    # Someone else's My knowledge: not named, and My knowledge has no member list to map to.
    target = items["diary"]["target"]
    assert (target["home"], target["file_name"], target["state"]) == ("personal", None, "hidden")
    assert (items["diary"]["mapping"]["action"], items["diary"]["mapping"]["reason"]) == ("none", "personal")
    assert "diary.md" not in str(items)

    assert items["removed"]["target"]["state"] == "deleted" and items["removed"]["target"]["file_name"] is None
    assert items["missing"]["target"] == {
        "kind": "kb_file",
        "home": None,
        "space_name": None,
        "file_name": None,
        "state": "not_found",
        "knowledge_base": None,
    }
    for name in ("removed", "missing"):
        assert (items[name]["mapping"]["action"], items[name]["mapping"]["reason"]) == ("none", "target_unavailable")


async def test_own_personal_file_is_named_for_its_owner(env):
    diary = await kb_file(env, "own-diary.md", scope="personal", scope_id=env.ids["tadmin"], by="tadmin")
    record = await grant(env, "kb_file", diary, "user", env.ids["mem"], "read")
    item = by_grant(report(env))[record.id]
    assert (item["target"]["file_name"], item["target"]["home"]) == ("own-diary.md", "personal")
    assert item["target"]["knowledge_base"]["id"] == default_knowledge_base_id(env.team, "personal", env.ids["tadmin"])
    assert (item["mapping"]["action"], item["mapping"]["reason"]) == ("none", "personal")
    # Another administrator does not see the name.
    assert by_grant(report(env, user="owner"))[record.id]["target"]["file_name"] is None


async def test_principals_are_named_and_unresolvable_ones_labelled(env):
    analysts = await env.access.create_group(team_id=env.team, name="Analysts", created_by=env.ids["owner"])
    await env.access.add_group_member(team_id=env.team, group_id=analysts.id, user_id=env.ids["mem"], created_by=env.ids["owner"])
    reviewers = await env.access.create_group(team_id=env.team, name="Reviewers", workspace_id=env.finance, created_by=env.ids["owner"])
    retired = await env.access.create_group(team_id=env.team, name="Retired", created_by=env.ids["owner"])

    async def retire(db):
        await db.execute("UPDATE groups SET status='archived' WHERE id=?", (retired.id,))

    await run_sqlite_authority_write(str(env.directory / "bowhead.db"), retire)
    scope = f"team:{env.team}"
    grants = {
        "group": await grant(env, "kb_scope", scope, "group", analysts.id),
        "space_group": await grant(env, "kb_scope", scope, "group", reviewers.id),
        "retired": await grant(env, "kb_scope", scope, "group", retired.id),
        "unknown_group": await grant(env, "kb_scope", scope, "group", "grp_unknown"),
        "gone": await grant(env, "kb_scope", scope, "user", env.ids["gone"]),
        "stranger": await grant(env, "kb_scope", scope, "user", "usr_stranger"),
        "foreign_space": await grant(env, "kb_scope", scope, "workspace", env.foreign_space),
        "archived_space": await grant(env, "kb_scope", scope, "workspace", env.closed),
        "other_team": await grant(env, "kb_scope", scope, "team", "team_other"),
    }
    items = {name: by_grant(report(env))[record.id] for name, record in grants.items()}
    principal = {name: item["applies_to"] for name, item in items.items()}

    assert (principal["group"]["label"], principal["group"]["state"], principal["group"]["member_count"]) == ("Analysts", "active", 1)
    assert (principal["space_group"]["label"], principal["space_group"]["space_name"]) == ("Reviewers", "Finance")
    assert (principal["retired"]["label"], principal["retired"]["state"]) == ("Retired", "group_removed")
    assert (principal["unknown_group"]["label"], principal["unknown_group"]["state"]) == (None, "not_found")
    assert (principal["gone"]["label"], principal["gone"]["state"]) == ("Gone", "not_in_team")
    assert (principal["stranger"]["label"], principal["stranger"]["state"]) == (None, "not_found")
    assert (principal["foreign_space"]["label"], principal["foreign_space"]["state"]) == (None, "not_found")
    assert (principal["archived_space"]["label"], principal["archived_space"]["state"]) == ("Closed", "space_archived")
    assert principal["other_team"]["state"] == "not_found"
    for name in ("retired", "unknown_group", "gone", "stranger", "foreign_space", "other_team"):
        assert (items[name]["mapping"]["action"], items[name]["mapping"]["reason"]) == ("none", "principal_unavailable"), name
    for name in ("group", "space_group", "archived_space"):
        assert items[name]["mapping"]["action"] == "needs_level", name


# ─── Restrictions and enforcement ────────────────────────────────────────


async def test_restriction_and_enforcement_labels(env, monkeypatch):
    scope = f"workspace:{env.finance}"
    grants = {
        "allow_read": await grant(env, "kb_scope", scope, "user", env.ids["mem"], "read"),
        "allow_admin": await grant(env, "kb_scope", scope, "user", env.ids["tadmin"], "admin"),
        "allow_use": await grant(env, "kb_scope", scope, "user", env.ids["owner"], "use"),
        "deny_read": await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids["mem"], "read", "deny"),
        "deny_use": await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids["tadmin"], "use", "deny"),
        "expired": await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids["owner"], "write", "deny", "2020-01-01T00:00:00+00:00"),
        "future": await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids["out"], "read", "allow", "2999-01-01T00:00:00+00:00"),
    }
    body = report(env)
    assert body["enforced"] is False
    items = {name: by_grant(body)[record.id] for name, record in grants.items()}
    restriction = {name: item["restriction"] for name, item in items.items()}
    assert restriction["allow_read"] == {"kind": "allow_list", "actions": ["read"], "reason": None}
    assert restriction["allow_admin"] == {"kind": "allow_list", "actions": ["read", "write"], "reason": None}
    # An allow grant with a permission KB checks never use still makes the target an allow list.
    assert restriction["allow_use"] == {"kind": "allow_list", "actions": [], "reason": None}
    assert restriction["deny_read"] == {"kind": "deny", "actions": ["read"], "reason": None}
    assert restriction["deny_use"] == {"kind": "none", "actions": [], "reason": "permission_not_checked"}
    assert restriction["expired"] == {"kind": "none", "actions": [], "reason": "expired"}
    assert (items["expired"]["expired"], items["future"]["expired"]) == (True, False)
    assert restriction["future"]["kind"] == "allow_list"
    assert (items["allow_admin"]["mapping"]["level"], items["allow_read"]["mapping"]["level"]) == ("edit", "use")
    assert (items["allow_use"]["mapping"]["action"], items["allow_use"]["mapping"]["reason"]) == ("none", "permission_not_checked")
    assert (items["deny_use"]["mapping"]["action"], items["deny_use"]["mapping"]["reason"]) == ("none", "permission_not_checked")
    assert (items["expired"]["mapping"]["action"], items["expired"]["mapping"]["reason"]) == ("none", "expired")

    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    enforced = report(env)
    assert enforced["enforced"] is True
    # Enforcement changes only the label, not what each grant would restrict.
    assert [item["restriction"] for item in enforced["items"]] == [item["restriction"] for item in body["items"]]


async def test_enforced_grants_hide_the_files_they_deny_the_caller(env, monkeypatch):
    budget = await kb_file(env, "budget.md", scope="space", scope_id=env.finance, workspace=env.finance)
    record = await grant(env, "kb_file", budget, "user", env.ids["tadmin"], "read", "deny")
    assert by_grant(report(env))[record.id]["target"]["file_name"] == "budget.md"
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    assert by_grant(report(env))[record.id]["target"]["state"] == "hidden"


# ─── Pages and read-only ─────────────────────────────────────────────────


async def test_pages_cover_every_grant_once_newest_first(env):
    created = [await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids[name]) for name in ("owner", "tadmin", "mem", "audit", "out")]
    created.append(await grant(env, "kb_file", "kbf_missing0000", "user", env.ids["mem"]))
    await grant(env, "skill", "forecast", "user", env.ids["mem"], "use")  # Not a knowledge grant.
    revoked = await grant(env, "kb_scope", f"workspace:{env.finance}", "user", env.ids["mem"])
    await env.access.revoke_resource_grant(env.team, revoked.id)

    seen, cursor, pages = [], None, 0
    while True:
        body = report(env, limit=4, **({"cursor": cursor} if cursor else {}))
        assert body["total"] == 6 + pages
        seen += [item["grant_id"] for item in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        # A grant added between pages does not shift or repeat the next page.
        if pages == 1:
            late = await grant(env, "kb_scope", f"team:{env.team}", "group", "grp_late")
    assert pages == 2
    assert sorted(seen) == sorted(record.id for record in created)
    assert len(seen) == len(set(seen))
    stored = {record.id: record.created_at for record in await env.access.list_resource_grants(env.team)}
    assert [stored[grant_id] for grant_id in seen] == sorted((stored[grant_id] for grant_id in seen), reverse=True)
    assert report(env)["total"] == 7 and late.id in {item["grant_id"] for item in report(env)["items"]}

    for bad in ("not-a-cursor", "WyJ4Il0"):
        response = get(env, cursor=bad)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"]["code"] == "grant_report_cursor_invalid"
    assert get(env, limit=0).status_code == 422
    assert get(env, limit=201).status_code == 422


async def test_empty_team_reports_no_items(env):
    await grant(env, "skill", "forecast", "user", env.ids["mem"], "use")
    assert report(env) == {"enforced": False, "items": [], "total": 0, "next_cursor": None}


def _snapshot(env):
    with closing(sqlite3.connect(env.directory / "kb_files.db")) as db:
        files = list(db.iterdump())
    with closing(sqlite3.connect(env.directory / "bowhead.db")) as db:
        core = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608 -- Fixed table names.
            for table in ("resource_grants", "groups", "group_memberships", "access_policy_state")
        }
    return files, core


async def test_the_report_changes_nothing(env):
    contracts = await named(env, "Contracts", home="space", home_id=env.finance, members=[("tadmin", "use")])
    nda = await kb_file(env, "nda.md", scope="space", scope_id=env.finance, workspace=env.finance)
    await move(env, nda, contracts)
    await grant(env, "kb_file", nda, "user", env.ids["mem"])
    # The Legal and Team defaults have no rows yet; reading the report must not create them.
    await grant(env, "kb_scope", f"workspace:{env.legal}", "user", env.ids["mem"], "write", "deny")
    await grant(env, "kb_scope", f"team:{env.team}", "user", env.ids["mem"])
    epoch = await env.access.get_policy_epoch(env.team)
    before = _snapshot(env)
    for user in ("tadmin", "owner"):
        report(env, user=user)
    assert _snapshot(env) == before
    assert await env.access.get_policy_epoch(env.team) == epoch
    legal_default = default_knowledge_base_id(env.team, "space", env.legal)
    assert await env.bases.get(legal_default) is None
