"""Knowledge base access (phase 1 slice 2) through the real gateway paths.

A parity harness on a 116-shaped dataset migrated by revision 0239 compares every person,
Space context and file with today's rules (a frozen copy of the permission functions at
5b7379b1be). Members-only Space and Team knowledge bases are then checked across reads,
writes, lists, search, citations, the Files page, the knowledge base check and the
fail-closed rule without levels (C3).
"""

from __future__ import annotations

import itertools
import sqlite3
from contextlib import closing
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.access_control import AccessControlStore
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.knowledge_base_access import KNOWLEDGE_EDIT, KNOWLEDGE_MANAGE, KNOWLEDGE_OWNER, KNOWLEDGE_USE
from bowhead_core.kb.knowledge_base_store import default_for_scope, default_knowledge_base_id
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.store import KbStore
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import User
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.kb_authorization import kb_file_authorizer, kb_file_decisions
from bowhead_gateway.kb_citation_access import kb_file_access_for_user
from bowhead_gateway.routes import files as files_routes
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.kb import _knowledge_base_check, _make_scope_context_checked
from bowhead_gateway.source_access_projection import _kb_presentations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

# The revision that adds knowledge bases; the dataset is seeded just before it (never a copied revision literal).
KNOWLEDGE_BASES_MIGRATION = import_module("bowhead_core.migrations.versions.0239_knowledge_bases")

from bowhead_core import migrations

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

TEAM, OTHER = "team_a", "team_b"
NOW = "2026-11-27T00:00:00+00:00"
SPACES = ("ws_1", "ws_2", "ws_3", "ws_4")
SPACE_TEAM = {**dict.fromkeys(SPACES, TEAM), "ws_b": OTHER}
# Person → (Space roles, Team role, Team). 116 has no viewers; the dataset adds them on purpose.
PEOPLE = {
    "u_owner": ({"ws_1": "admin", "ws_2": "member", "ws_3": "viewer"}, "owner", TEAM),
    "u_tadmin": ({"ws_1": "member", "ws_2": "viewer"}, "team_admin", TEAM),
    "u_sadmin": ({"ws_1": "admin"}, "member", TEAM),
    "u_mem": ({"ws_1": "member", "ws_2": "member"}, "member", TEAM),
    "u_mem2": ({"ws_1": "member"}, "member", TEAM),
    "u_view": ({"ws_1": "viewer", "ws_4": "member"}, "member", TEAM),
    "u_out": ({}, "member", TEAM),
    "u_b": ({"ws_b": "member"}, "member", OTHER),
}


def _contexts():
    for person, (spaces, _, team) in PEOPLE.items():
        yield person, team, ""
        for space in spaces:
            yield person, team, space


# ─── Today's rules: a frozen copy of KbPermission and kb_file_decisions at 5b7379b1be ────


def _today_role(person: str, space: str) -> str:
    spaces, team_role, _ = PEOPLE[person]
    if team_role in ("owner", "team_admin"):
        return "team_admin"
    return "ws_admin" if spaces.get(space) == "admin" else "user"


def _today_read(file, person: str, team: str, space: str) -> bool:
    if file.soft_deleted_at or file.team_id != team:
        return False
    if file.scope == "personal":
        return file.scope_id == person
    if file.scope in ("space", "workspace"):
        return file.scope_id == space
    if file.scope == "team":
        return file.scope_id == team
    return False


def _today_read_only(person: str, space: str, *, viewer_rule: bool) -> bool:
    return viewer_rule and PEOPLE[person][0].get(space) == "viewer" and _today_role(person, space) != "team_admin"


def _today_delete(file, person: str, team: str, space: str, *, viewer_rule: bool = True) -> bool:
    if not _today_read(file, person, team, space):
        return False
    role = _today_role(person, space)
    if file.scope == "personal":
        return file.scope_id == person
    if file.scope in ("space", "workspace"):
        if _today_read_only(person, space, viewer_rule=viewer_rule):
            return False
        return file.uploaded_by == person or role in ("ws_admin", "team_admin")
    return role == "team_admin"


def _today_add(home: str, home_id: str, person: str, team: str, space: str, *, viewer_rule: bool = True) -> bool:
    if home == "personal":
        return home_id == person
    if home == "space":
        return home_id == space and not _today_read_only(person, space, viewer_rule=viewer_rule)
    return home_id == team and _today_role(person, space) == "team_admin"


# ─── Dataset ────────────────────────────────────────────────────────────────


def _files_config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "files"
    return config


def _insert(db, file_id, scope, scope_id, workspace_id, uploaded_by, *, team=TEAM, deleted=False):
    db.execute(
        "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,"
        "uploaded_at,parse_status,soft_deleted_at) VALUES (?,?,?,'application/pdf',10,?,?,?,?,?,?,'done',?)",
        (file_id, f"art_{file_id}", f"{file_id}.pdf", scope, scope_id, workspace_id, team, uploaded_by, NOW, NOW if deleted else None),
    )


def _seed_116_shape(db) -> None:
    """Personal files over several Spaces (one the legacy empty Space), Space and Team files, soft-deleted rows, a second Team."""
    for owner, spaces in (("u_mem", ("ws_1", "ws_2", "")), ("u_view", ("ws_1", "ws_4")), ("u_owner", ("ws_1", "ws_2", "ws_3"))):
        for space, index in itertools.product(spaces, range(2)):
            _insert(db, f"kbf_p_{owner}_{space or 'legacy'}_{index}", "personal", owner, space, owner)
    _insert(db, "kbf_p_u_view_gone", "personal", "u_view", "ws_1", "u_view", deleted=True)
    # An administrator moved someone else's file into their own personal scope: the owner is scope_id.
    _insert(db, "kbf_p_moved", "personal", "u_tadmin", "ws_1", "u_mem")
    for space, uploader in itertools.product(SPACES, ("u_owner", "u_mem", "u_sadmin", "u_view", "u_tadmin")):
        _insert(db, f"kbf_s_{space}_{uploader}", "space", space, space, uploader)
    _insert(db, "kbf_s_legacy", "workspace", "ws_1", "ws_1", "u_mem2")
    _insert(db, "kbf_s_gone", "space", "ws_2", "ws_2", "u_mem", deleted=True)
    for index in range(2):
        _insert(db, f"kbf_t_{index}", "team", TEAM, "", "u_owner")
    _insert(db, "kbf_t_mem", "team", TEAM, "", "u_mem")
    _insert(db, "kbf_t_mem2", "team", TEAM, "", "u_mem2")
    _insert(db, "kbf_t_gone", "team", TEAM, "", "u_owner", deleted=True)
    _insert(db, "kbf_b_team", "team", OTHER, "", "u_b", team=OTHER)
    _insert(db, "kbf_b_personal", "personal", "u_b", "ws_b", "u_b", team=OTHER)
    _insert(db, "kbf_b_space", "space", "ws_b", "ws_b", "u_b", team=OTHER)


@pytest.fixture(scope="module")
def migrated(tmp_path_factory) -> Path:
    """The dataset seeded at 0238 and upgraded to head, so 0239 backfills every default."""
    path = tmp_path_factory.mktemp("kb-access") / "kb_files.db"
    config = _files_config(path)
    command.upgrade(config, KNOWLEDGE_BASES_MIGRATION.down_revision)
    with closing(sqlite3.connect(path)) as db:
        # Stand-in for the runtime's 0238 retention guard (nothing is trashed).
        db.create_function("bowhead_file_retention_unavailable_v1", 1, lambda _artifact_id: 0)
        _seed_116_shape(db)
        db.commit()
    command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT scope,scope_id,team_id,knowledge_base_id FROM kb_files").fetchall()
    assert rows and all(kb_id == default_for_scope(scope, scope_id, team) for scope, scope_id, team, kb_id in rows)
    return path


class _Users:
    """Space and Team roles from PEOPLE; every Space is active."""

    async def get_user_team_id(self, user_id, **_):
        return PEOPLE[user_id][2] if user_id in PEOPLE else None

    async def get_user(self, user_id, **_):
        return SimpleNamespace(id=user_id, status="active", team_id=PEOPLE[user_id][2]) if user_id in PEOPLE else None

    async def get_team_role(self, user_id, team_id, **_):
        return PEOPLE[user_id][1] if user_id in PEOPLE and PEOPLE[user_id][2] == team_id else None

    async def get_workspace_team_id(self, workspace_id, **_):
        return SPACE_TEAM.get(workspace_id)

    async def get_member_role(self, workspace_id, user_id, **_):
        return PEOPLE[user_id][0].get(workspace_id) if user_id in PEOPLE else None

    async def get_workspace(self, workspace_id, **_):
        return {"id": workspace_id, "status": "active"} if workspace_id in SPACE_TEAM else None

    async def list_workspaces_for_user(self, user_id, *, team_id=None):
        return [{"workspace_id": space} for space in PEOPLE[user_id][0] if team_id is None or SPACE_TEAM[space] == team_id]

    async def get_team_member_labels(self, team_id, user_ids):
        return {}


def _group(db, group_id, workspace_id, members, *, status="active", removed=()):
    db.execute(
        "INSERT INTO groups(id,team_id,workspace_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (group_id, TEAM, workspace_id, group_id, status, NOW, NOW),
    )
    for person in (*members, *removed):
        db.execute(
            "INSERT INTO group_memberships(team_id,group_id,principal_type,principal_id,status,created_at) VALUES (?,?,'user',?,?,?)",
            (TEAM, group_id, person, "removed" if person in removed else "active", NOW),
        )


@pytest.fixture
async def env(tmp_path, migrated, monkeypatch):
    # Named knowledge bases need the switch (slice 3); the parity test also runs with it off.
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "1")
    path = tmp_path / "kb_files.db"
    with closing(sqlite3.connect(migrated)) as source, closing(sqlite3.connect(path)) as destination:
        source.backup(destination)
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    with closing(sqlite3.connect(tmp_path / "access.db")) as db:
        _group(db, "grp_team", "", ["u_mem2"], removed=["u_out"])
        _group(db, "grp_ws1", "ws_1", ["u_view"])
        _group(db, "grp_gone", "", ["u_mem"], status="removed")
        db.commit()
    files = KbFilesStore(path)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(kb.router)
    app.state.kb_files_store = files
    app.state.access_control_store = access
    app.state.user_store = _Users()
    current = {"user": "u_mem"}
    app.dependency_overrides[_current_user] = lambda: _user(current["user"])
    return SimpleNamespace(
        path=path,
        files=files,
        access=access,
        app=app,
        client=TestClient(app, raise_server_exceptions=False),
        current=current,
        authorize=kb_file_authorizer(app.state.user_store, access, files_store=files),
    )


def _user(person: str) -> User:
    return User(id=person, email=f"{person}@example.test", team_id=PEOPLE[person][2], tier="ultra", email_verified=True)


def _request(app, space: str) -> Request:
    headers = [(b"x-space-id", space.encode())] if space else []
    return Request({"type": "http", "app": app, "headers": headers, "state": {}, "method": "GET", "path": "/", "query_string": b""})


async def _scope(env, person: str, space: str) -> ScopeContext:
    return await _make_scope_context_checked(
        _user(person), _request(env.app, space), allow_missing_workspace=True, operation=SpaceOperation.READ_HISTORY
    )


async def _all_files(env):
    with closing(sqlite3.connect(env.path)) as db:
        ids = [row[0] for row in db.execute("SELECT file_id FROM kb_files ORDER BY file_id")]
    return await env.files.get_many(ids)


def _named_kb(db, kb_id, home, home_id, owner, *, audience="members", level="use", team=TEAM, deleted=False):
    db.execute(
        "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,audience,audience_level,owner_user_id,created_at,updated_at,deleted_at) "
        "VALUES (?,?,?,?,0,?,?,?,?,?,?,?)",
        (kb_id, team, home, home_id, kb_id, audience, level, owner, NOW, NOW, NOW if deleted else None),
    )


def _member(db, kb_id, principal_type, principal_id, level):
    db.execute("INSERT INTO kb_knowledge_base_members VALUES (?,?,?,?,'u_owner',?)", (kb_id, principal_type, principal_id, level, NOW))


def _add_named_knowledge_bases(path: Path, *, move_files: bool) -> None:
    """A members-only Space knowledge base in ws_1 and a members-only Team knowledge base."""
    with closing(sqlite3.connect(path)) as db:
        _named_kb(db, "kbase_finance", "space", "ws_1", "u_mem")
        _member(db, "kbase_finance", "user", "u_mem2", "use")
        _member(db, "kbase_finance", "group", "grp_ws1", "use")  # u_view, through a group bound to ws_1
        _member(db, "kbase_finance", "group", "grp_gone", "edit")  # a removed group: inert
        _named_kb(db, "kbase_legal", "team", TEAM, "u_owner")
        _member(db, "kbase_legal", "space", "ws_4", "use")  # C9: u_view, a member of ws_4, wherever they work
        _member(db, "kbase_legal", "group", "grp_team", "edit")  # u_mem2; u_out's membership was removed
        _member(db, "kbase_legal", "group", "grp_ws1", "manage")  # Space-bound: never applies to a Team knowledge base
        if move_files:
            db.executemany(
                "UPDATE kb_files SET knowledge_base_id=? WHERE file_id=?",
                [
                    ("kbase_finance", "kbf_s_ws_1_u_mem"),
                    ("kbase_finance", "kbf_s_ws_1_u_view"),
                    ("kbase_legal", "kbf_t_0"),
                    ("kbase_legal", "kbf_t_mem2"),
                ],
            )
        db.commit()


# ─── Parity ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("switch", ["1", "0"], ids=["switch_on", "switch_off"])
@pytest.mark.parametrize("named_knowledge_bases", [False, True], ids=["defaults_only", "named_but_unused"])
async def test_parity_with_todays_rules_for_every_person_space_and_file(env, named_knowledge_bases, switch, monkeypatch):
    """Acceptance 1: with only defaults in use, read and write decisions equal today's for everyone."""
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", switch)
    if named_knowledge_bases:
        _add_named_knowledge_bases(env.path, move_files=False)
    files = await _all_files(env)
    diffs: dict[str, list] = {"read": [], "write": [], "add": [], "route_read": [], "levels": []}
    p9: list = []
    for person, team, space in _contexts():
        reads = await env.authorize.batch(files, person, team, space)
        writes = await env.authorize.batch(files, person, team, space, write=True)
        assert reads is not None and writes is not None, (person, space)
        ctx = await _scope(env, person, space)
        assert ctx.knowledge_levels is not None
        for file in files:
            expected_read, expected_write = _today_read(file, person, team, space), _today_delete(file, person, team, space)
            if reads[file.file_id] != expected_read:
                diffs["read"].append((person, space, file.file_id))
            if writes[file.file_id] != expected_write:
                diffs["write"].append((person, space, file.file_id))
            active = not file.soft_deleted_at and file.team_id == ctx.team_id
            if (active and KbPermission.can_read(file, ctx)) != expected_read:
                diffs["route_read"].append((person, space, file.file_id))
            # The levels alone (what slice 3 shows and checks) reproduce today's rule on defaults, C2 included.
            level = ctx.knowledge_levels.get(file.knowledge_base_id or "", 0) if active else 0
            owner = KbPermission.owner_of(file) == person
            level_write = level >= KNOWLEDGE_MANAGE or (level >= KNOWLEDGE_EDIT and owner)
            if (level >= KNOWLEDGE_USE) != expected_read or level_write != expected_write:
                diffs["levels"].append((person, space, file.file_id, level))
            if level_write != _today_delete(file, person, team, space, viewer_rule=False):
                p9.append((person, space, "delete", file.file_id))
        for home, home_id in (("personal", person), ("space", space), ("team", team)):
            if not home_id:
                continue
            expected_add = _today_add(home, home_id, person, team, space)
            default = default_knowledge_base_id(team, home, home_id)
            if KbPermission.can_write_to_scope(home, home_id, ctx, ctx.role) != expected_add:
                diffs["add"].append((person, space, home))
            if KbPermission.can_write_to_scope(home, home_id, ctx, ctx.role, knowledge_base_id=default) != expected_add:
                diffs["add"].append((person, space, home, "by id"))
            level_add = ctx.knowledge_levels.get(default, 0) >= KNOWLEDGE_EDIT
            if level_add != expected_add:
                diffs["levels"].append((person, space, home))
            if level_add != _today_add(home, home_id, person, team, space, viewer_rule=False):
                p9.append((person, space, "add", home))
    assert diffs == {"read": [], "write": [], "add": [], "route_read": [], "levels": []}
    # Against the rules before the shipped viewer fix (05e04ffa47), the only differences are
    # ordinary Space viewers adding or deleting their own Space files (P9).
    assert p9 == [
        ("u_view", "ws_1", "delete", "kbf_s_ws_1_u_view"),
        ("u_view", "ws_1", "add", "space"),
    ]


# ─── Members-only knowledge bases ───────────────────────────────────────────

FINANCE = ("kbf_s_ws_1_u_mem", "kbf_s_ws_1_u_view")
LEGAL = ("kbf_t_0", "kbf_t_mem2")
FINANCE_READERS = {"u_mem", "u_mem2", "u_sadmin", "u_owner", "u_view"}
LEGAL_READERS = {"u_owner", "u_tadmin", "u_view", "u_mem2"}


@pytest.fixture
async def named(env):
    _add_named_knowledge_bases(env.path, move_files=True)
    return env


async def test_members_only_knowledge_bases_decide_reads_and_writes(named):
    files = await _all_files(named)
    by_id = {file.file_id: file for file in files}
    for person, team, space in _contexts():
        reads = await named.authorize.batch(files, person, team, space)
        writes = await named.authorize.batch(files, person, team, space, write=True)
        for file_id in FINANCE:
            assert reads[file_id] == (space == "ws_1" and person in FINANCE_READERS), (person, space, file_id)
        for file_id in LEGAL:
            assert reads[file_id] == (team == TEAM and person in LEGAL_READERS), (person, space, file_id)
        # Every other file keeps today's decision.
        for file in files:
            if file.file_id not in FINANCE + LEGAL:
                assert reads[file.file_id] == _today_read(file, person, team, space)
                assert writes[file.file_id] == _today_delete(file, person, team, space)
        managers = {"u_mem", "u_sadmin", "u_owner"} if space == "ws_1" else set()
        for file_id in FINANCE:
            # Listed use and the viewer cap never delete, not even the viewer's own file.
            assert writes[file_id] == (person in managers), (person, space, file_id)
        # Team owners/admins manage; u_mem2 edits through the group and removes only their own file.
        assert writes["kbf_t_0"] == (person in {"u_owner", "u_tadmin"}), (person, space)
        assert writes["kbf_t_mem2"] == (person in {"u_owner", "u_tadmin", "u_mem2"}), (person, space)
    assert by_id["kbf_t_mem2"].uploaded_by == "u_mem2"


async def test_named_files_are_listed_and_opened_only_for_people_who_can_use_them(named):
    for person, team, space in _contexts():
        if team != TEAM:
            continue
        named.current["user"] = person
        headers = {"X-Space-Id": space} if space else {}
        listed = named.client.get("/api/v1/kb/files", params={"scope": "all", "limit": 1000}, headers=headers)
        assert listed.status_code == 200, (person, space, listed.text)
        got = {item["file_id"] for item in listed.json()["files"]}
        for file_id in (*FINANCE, *LEGAL):
            visible = (space == "ws_1" and person in FINANCE_READERS) if file_id in FINANCE else person in LEGAL_READERS
            assert (file_id in got) == visible, (person, space, file_id)
            if not space:
                continue  # File details need a Space context.
            opened = named.client.get(f"/api/v1/kb/files/{file_id}", headers=headers)
            assert opened.status_code == (200 if visible else 404), (person, space, file_id)
            if not visible:
                assert "kb_file_not_found" in opened.text and file_id not in opened.text


async def test_search_pre_filters_named_knowledge_bases_and_the_read_rule_post_filters(named, tmp_path):
    store = KbStore(str(tmp_path / "chroma"))
    files = {file.file_id: file for file in await _all_files(named)}

    async def index(file_id: str, seed: int) -> None:
        file = files[file_id]
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
        embedding = [((seed * 7919 + i) % 97) / 97 for i in range(16)]
        await store.add_chunks(
            file, [KbChunk(chunk_id=make_chunk_id(file_id, 1, file_id), file_id=file_id, content=file_id, embedding=embedding, metadata=metadata)]
        )

    for seed, file_id in enumerate((*FINANCE, *LEGAL, "kbf_s_ws_1_u_sadmin", "kbf_t_1")):
        await index(file_id, seed)
    query = [0.5] * 16
    for person, space in (("u_mem2", "ws_1"), ("u_tadmin", "ws_1"), ("u_view", "ws_1"), ("u_mem", "ws_1"), ("u_out", "")):
        ctx = await _scope(named, person, space)
        found = {hit["metadata"]["file_id"] for hit in await store.search(ctx, query, top_k=20)}
        expected = {"kbf_t_1"} | ({"kbf_s_ws_1_u_sadmin"} if space == "ws_1" else set())
        expected |= set(FINANCE) if space == "ws_1" and person in FINANCE_READERS else set()
        expected |= set(LEGAL) if person in LEGAL_READERS else set()
        assert found == expected, (person, space)
        # What the vector store returns, the SQL read rule admits.
        assert all(KbPermission.can_read(files[file_id], ctx) for file_id in found)


async def test_citations_and_shared_report_readers_follow_the_knowledge_base(named):
    targets = {"kb:kbf_s_ws_1_u_mem:chunk_1": ("kbf_s_ws_1_u_mem", "chunk_1"), "kb:kbf_t_0:chunk_2": ("kbf_t_0", "chunk_2")}
    for person in ("u_mem2", "u_tadmin", "u_view", "u_out"):
        check = kb_file_access_for_user(named.app, _user(person))
        finance, legal = await check("kbf_s_ws_1_u_mem"), await check("kbf_t_0")
        assert finance.state == ("readable" if person in FINANCE_READERS else "forbidden"), person
        assert legal.state == ("readable" if person in LEGAL_READERS else "forbidden"), person
        assert finance.knowledge_base_id == ("kbase_finance" if finance.state == "readable" else None)
        assert legal.knowledge_base_id == ("kbase_legal" if legal.state == "readable" else None)
        rows = await _kb_presentations(targets, check)
        assert rows["kb:kbf_s_ws_1_u_mem:chunk_1"]["access_state"] == ("linked_accessible" if person in FINANCE_READERS else "linked_restricted")
        assert rows["kb:kbf_t_0:chunk_2"]["access_state"] == ("linked_accessible" if person in LEGAL_READERS else "linked_restricted")
        # The viewer projection's fields are unchanged; a restricted row names nothing.
        assert all(set(row) <= {"access_state", "kb_file_id", "kb_chunk_id", "safe_label", "kb_scope", "kb_space_id"} for row in rows.values())
    # Default knowledge base files keep today's decision and name their default.
    default = await kb_file_access_for_user(named.app, _user("u_mem2"))("kbf_t_1")
    assert default.state == "readable" and default.knowledge_base_id == default_knowledge_base_id(TEAM, "team", TEAM)


async def test_files_page_shows_knowledge_status_only_for_usable_files(named, monkeypatch):
    rows = await named.files.get_many([*FINANCE, *LEGAL, "kbf_s_ws_1_u_sadmin"])
    for person in ("u_mem2", "u_tadmin", "u_view", "u_out"):
        monkeypatch.setattr(files_routes, "_user_id", lambda _request, person=person: person)
        readable = {row.file_id for row in await files_routes._kb_rows_readable(_request(named.app, ""), rows)}
        expected = set(FINANCE) if person in FINANCE_READERS else set()
        expected |= set(LEGAL) if person in LEGAL_READERS else set()
        expected |= {"kbf_s_ws_1_u_sadmin"} if "ws_1" in PEOPLE[person][0] else set()
        assert readable == expected, person


async def test_without_levels_named_files_are_refused_and_defaults_keep_todays_rule(named):
    """C3: a call site that does not resolve levels (or the switch off) hides named files instead of widening them."""
    files = await _all_files(named)
    no_levels = kb_file_authorizer(named.app.state.user_store, named.access)
    reads = await no_levels.batch(files, "u_mem", TEAM, "ws_1")
    assert not any(reads[file_id] for file_id in (*FINANCE, *LEGAL))
    assert reads["kbf_s_ws_1_u_sadmin"] and reads["kbf_t_1"]
    ctx = await _scope(named, "u_mem", "ws_1")
    bare = ScopeContext(user_id="u_mem", workspace_id="ws_1", team_id=TEAM, role=ctx.role)
    by_id = {file.file_id: file for file in files}
    assert KbPermission.can_read(by_id["kbf_s_ws_1_u_mem"], ctx) and not KbPermission.can_read(by_id["kbf_s_ws_1_u_mem"], bare)
    access_ctx = await named.authorize.knowledge_levels("u_mem", TEAM, "ws_1")
    assert access_ctx is not None and access_ctx["kbase_finance"] == KNOWLEDGE_OWNER

    class _Broken:
        def __getattr__(self, name):
            return getattr(named.files, name)

        async def resolve_knowledge_levels(self, *_args, **_kwargs):
            raise RuntimeError("files database unavailable")

    named.app.state.kb_files_store = _Broken()
    broken = await _scope(named, "u_mem", "ws_1")
    assert broken.knowledge_levels is None
    assert not KbPermission.can_read(by_id["kbf_s_ws_1_u_mem"], broken)
    assert KbPermission.can_read(by_id["kbf_s_ws_1_u_sadmin"], broken)
    decisions = await kb_file_decisions(files, await _access_context(named, "u_mem", "ws_1"), named.access, files_store=_Broken())
    assert not decisions["kbf_s_ws_1_u_mem"] and decisions["kbf_s_ws_1_u_sadmin"]


async def test_switching_off_hides_named_knowledge_bases_and_never_widens(named, monkeypatch):
    """C3: off, named files are refused for everyone, their chunks are pre-filtered and defaults keep today's rule."""
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    files = await _all_files(named)
    for person, team, space in _contexts():
        reads = await named.authorize.batch(files, person, team, space)
        writes = await named.authorize.batch(files, person, team, space, write=True)
        assert not any(reads[file_id] or writes[file_id] for file_id in (*FINANCE, *LEGAL)), (person, space)
        for file in files:
            if file.file_id not in FINANCE + LEGAL:
                assert reads[file.file_id] == _today_read(file, person, team, space)
                assert writes[file.file_id] == _today_delete(file, person, team, space)
    ctx = await _scope(named, "u_mem", "ws_1")
    assert "kbase_finance" not in ctx.knowledge_levels and "kbase_legal" not in ctx.knowledge_levels
    # Every named knowledge base of the context is pre-filtered from search, usable or not.
    assert ctx.knowledge_levels.excluded_in("space", "ws_1") == ("kbase_finance",)
    assert ctx.knowledge_levels.excluded_in("team", TEAM) == ("kbase_legal",)


async def test_entries_on_a_default_raise_write_rights_only_while_the_switch_is_on(env, monkeypatch):
    """C6: an edit entry on the Team default lets a member add and remove their own Team files; off, today's rule."""
    team_default = default_knowledge_base_id(TEAM, "team", TEAM)
    with closing(sqlite3.connect(env.path)) as db:
        _member(db, team_default, "user", "u_mem", "edit")
        db.commit()
    files = {file.file_id: file for file in await _all_files(env)}
    on = await _scope(env, "u_mem", "ws_1")
    assert KbPermission.can_write_to_scope("team", TEAM, on, on.role)
    assert KbPermission.can_delete(files["kbf_t_mem"], on, on.role)
    assert not KbPermission.can_delete(files["kbf_t_0"], on, on.role)  # someone else's file needs manage
    writes = await env.authorize.batch(list(files.values()), "u_mem", TEAM, "ws_1", write=True)
    assert writes["kbf_t_mem"] and not writes["kbf_t_0"]
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    off = await _scope(env, "u_mem", "ws_1")
    assert not KbPermission.can_write_to_scope("team", TEAM, off, off.role)
    assert not KbPermission.can_delete(files["kbf_t_mem"], off, off.role)
    assert not (await env.authorize.batch(list(files.values()), "u_mem", TEAM, "ws_1", write=True))["kbf_t_mem"]


async def _access_context(env, person, space):
    from bowhead_gateway.access_context import build_access_context

    return await build_access_context(
        user_store=env.app.state.user_store, access_store=env.access, user_id=person, workspace_id=space, allow_missing_workspace=True
    )


# ─── _knowledge_base_check ──────────────────────────────────────────────────


async def test_knowledge_base_check_answers_one_not_found_for_every_refusal(named):
    with closing(sqlite3.connect(named.path)) as db:
        _named_kb(db, "kbase_deleted", "space", "ws_1", "u_mem", audience="space", deleted=True)
        _named_kb(db, "kbase_b", "team", OTHER, "u_b", audience="team", team=OTHER)
        db.commit()

    async def check(person, space, knowledge_base_id, level=KNOWLEDGE_USE):
        return await _knowledge_base_check(knowledge_base_id, await _scope(named, person, space), named.files, level=level)

    refusals = [
        ("u_mem", "ws_1", "kbase_unknown"),
        ("u_mem", "ws_1", "kbase_b"),  # another Team's
        ("u_mem", "ws_1", "kbase_deleted"),
        ("u_mem", "ws_2", "kbase_finance"),  # a Space knowledge base outside the current Space
        ("u_tadmin", "ws_1", "kbase_finance"),  # no level (Q6)
        ("u_mem", "ws_1", "kbase_legal"),  # no level
        ("u_mem", "ws_1", default_knowledge_base_id(TEAM, "personal", "u_mem2")),  # someone's My knowledge
    ]
    for person, space, knowledge_base_id in refusals:
        with pytest.raises(HTTPException) as caught:
            await check(person, space, knowledge_base_id)
        assert (caught.value.status_code, caught.value.detail) == (404, {"error": {"code": "knowledge_base_not_found"}}), knowledge_base_id

    row, level = await check("u_mem2", "ws_1", "kbase_finance")
    assert (row.id, row.home, row.home_id, row.is_default, level) == ("kbase_finance", "space", "ws_1", False, KNOWLEDGE_USE)
    with pytest.raises(HTTPException) as caught:
        await check("u_mem2", "ws_1", "kbase_finance", KNOWLEDGE_EDIT)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"]["code"] == "knowledge_base_level_required"
    assert (caught.value.detail["error"]["required_level"], caught.value.detail["error"]["level"]) == ("edit", "use")
    # Transfer and delete need the owner or an automatic manager, not a listed manager.
    with pytest.raises(HTTPException) as caught:
        await check("u_mem2", "", "kbase_legal", KNOWLEDGE_OWNER)
    assert caught.value.detail["error"]["required_level"] == "owner"
    assert (await check("u_mem", "ws_1", "kbase_finance", KNOWLEDGE_OWNER))[1] == KNOWLEDGE_OWNER
    assert (await check("u_view", "", "kbase_legal"))[1] == KNOWLEDGE_USE
    # A home's default is answered before its row exists (ws_3 has no files).
    row, level = await check("u_owner", "ws_3", default_knowledge_base_id(TEAM, "space", "ws_3"), KNOWLEDGE_MANAGE)
    assert (row.home, row.home_id, row.is_default, row.audience, level) == ("space", "ws_3", True, "space", KNOWLEDGE_OWNER)
    with pytest.raises(HTTPException) as caught:
        await _knowledge_base_check("kbase_finance", ScopeContext(user_id="u_mem", workspace_id="ws_1", team_id=TEAM), named.files)
    assert caught.value.status_code == 503
