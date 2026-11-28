"""Knowledge base levels (phase 1 slice 2): the resolver's rules and the fail-closed file rule."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.knowledge_base_access import (
    _RESOLVE_SQL,
    KNOWLEDGE_EDIT,
    KNOWLEDGE_MANAGE,
    KNOWLEDGE_OWNER,
    KNOWLEDGE_USE,
    KnowledgeCaller,
    KnowledgeLevels,
    TeamPrincipals,
    named_knowledge_base_id,
)
from bowhead_core.kb.knowledge_base_store import default_knowledge_base_id
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.schemas import KbFileDTO

from bowhead_core import migrations

TEAM = "team_a"
NOW = "2026-11-27T00:00:00+00:00"
SPACE_DEFAULT = default_knowledge_base_id(TEAM, "space", "ws_1")
TEAM_DEFAULT = default_knowledge_base_id(TEAM, "team", TEAM)


def _kb(db, kb_id, home, home_id, *, audience="members", level="use", owner="u_owner", default=False, deleted=False, team=TEAM):
    db.execute(
        "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,audience,audience_level,owner_user_id,created_at,updated_at,deleted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (kb_id, team, home, home_id, int(default), None if default else kb_id, audience, level, owner, NOW, NOW, NOW if deleted else None),
    )


def _member(db, kb_id, principal_type, principal_id, level):
    db.execute("INSERT INTO kb_knowledge_base_members VALUES (?,?,?,?,?,?)", (kb_id, principal_type, principal_id, level, "u_owner", NOW))


@pytest.fixture(scope="module")
def files_schema(tmp_path_factory) -> Path:
    """The files target at Alembic head (0239 and later), built once per module."""
    path = tmp_path_factory.mktemp("kb-files-head") / "kb_files.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "files"
    command.upgrade(config, "head")
    return path


@pytest.fixture
def files_db(tmp_path, files_schema):
    def copy() -> Path:
        path = tmp_path / "kb_files.db"
        with closing(sqlite3.connect(files_schema)) as source, closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
        return path

    return copy


@pytest.fixture
def store(files_db):
    path = files_db()
    with closing(sqlite3.connect(path)) as db:
        _kb(db, SPACE_DEFAULT, "space", "ws_1", audience="space", level="edit", owner=None, default=True)
        _kb(db, TEAM_DEFAULT, "team", TEAM, audience="team", owner=None, default=True)
        # Members-only Space knowledge base owned by u_mem, with people and groups.
        _kb(db, "kbase_space_finance", "space", "ws_1", owner="u_mem")
        _member(db, "kbase_space_finance", "user", "u_listed", "edit")
        _member(db, "kbase_space_finance", "user", "u_view", "manage")
        _member(db, "kbase_space_finance", "group", "grp_team", "use")
        _member(db, "kbase_space_finance", "group", "grp_ws1", "edit")
        # "All Space members can use" in ws_2.
        _kb(db, "kbase_space_open", "space", "ws_2", audience="space", level="use", owner="u_mem")
        # Members-only Team knowledge base with a person, groups and a Space entry.
        _kb(db, "kbase_team_legal", "team", TEAM, owner="u_owner")
        _member(db, "kbase_team_legal", "user", "u_listed", "use")
        _member(db, "kbase_team_legal", "space", "ws_3", "edit")
        _member(db, "kbase_team_legal", "group", "grp_team", "manage")
        _member(db, "kbase_team_legal", "group", "grp_ws1", "manage")
        # A deleted one, another Team's and another Space's are never resolved.
        _kb(db, "kbase_deleted", "space", "ws_1", audience="space", level="edit", owner="u_mem", deleted=True)
        _kb(db, "kbase_other_team", "team", "team_b", audience="team", owner="u_b", team="team_b")
        _kb(db, "kbase_other_space", "space", "ws_9", audience="space", owner="u_mem")
        # My knowledge bases: a named one of u_mem's and someone else's.
        _kb(db, "kbase_my_notes", "personal", "u_mem", audience="owner", owner="u_mem")
        _kb(db, "kbase_their_notes", "personal", "u_other", audience="owner", owner="u_other")
        db.commit()
    return KbFilesStore(path)


def _caller(user="u_mem", space="ws_1", role="member", *, team_admin=False, groups=(), team_groups=None, spaces=None) -> KnowledgeCaller:
    return KnowledgeCaller(
        user_id=user,
        team_id=TEAM,
        space_id=space,
        space_role=role if space else "",
        is_team_admin=team_admin,
        group_ids=frozenset(groups),
        team_group_ids=None if team_groups is None else frozenset(team_groups),
        space_ids=None if spaces is None else frozenset(spaces),
    )


async def _levels(store, **kwargs) -> KnowledgeLevels:
    return await store.resolve_knowledge_levels(_caller(**kwargs))


@pytest.mark.integration
async def test_my_home_is_owner_only_and_counts_every_my_knowledge_base(store):
    mine = await _levels(store)
    assert mine[default_knowledge_base_id(TEAM, "personal", "u_mem")] == KNOWLEDGE_OWNER
    assert mine["kbase_my_notes"] == KNOWLEDGE_OWNER
    assert "kbase_their_notes" not in mine
    assert "kbase_my_notes" not in await _levels(store, user="u_other")


@pytest.mark.integration
async def test_space_default_keeps_todays_rights_including_c2(store):
    assert (await _levels(store, user="u_x", role="member"))[SPACE_DEFAULT] == KNOWLEDGE_EDIT
    assert (await _levels(store, user="u_x", role="viewer"))[SPACE_DEFAULT] == KNOWLEDGE_USE
    assert (await _levels(store, user="u_x", role="ws_admin"))[SPACE_DEFAULT] == KNOWLEDGE_OWNER
    # C2: Team owners/admins who are Space members manage the default, as viewers too (today's add/delete rights).
    assert (await _levels(store, user="u_x", role="member", team_admin=True))[SPACE_DEFAULT] == KNOWLEDGE_OWNER
    assert (await _levels(store, user="u_x", role="viewer", team_admin=True))[SPACE_DEFAULT] == KNOWLEDGE_OWNER
    # Not a member of the Space (Q2): nothing, Team admin or not.
    assert SPACE_DEFAULT not in await _levels(store, user="u_x", role="", team_admin=True)


@pytest.mark.integration
async def test_members_only_space_knowledge_base(store):
    assert "kbase_space_finance" not in await _levels(store, user="u_x")
    assert (await _levels(store, user="u_listed"))["kbase_space_finance"] == KNOWLEDGE_EDIT
    assert (await _levels(store, user="u_mem"))["kbase_space_finance"] == KNOWLEDGE_OWNER  # the owner
    assert (await _levels(store, user="u_x", role="ws_admin"))["kbase_space_finance"] == KNOWLEDGE_OWNER  # automatic manager
    # Viewers are capped at use, also when listed higher.
    assert (await _levels(store, user="u_view", role="viewer"))["kbase_space_finance"] == KNOWLEDGE_USE
    # Q6: Team owners/admins get nothing automatically on named Space knowledge bases.
    assert "kbase_space_finance" not in await _levels(store, user="u_x", team_admin=True)
    # Space entries are not principals on Space knowledge bases; the knowledge base applies only in its Space.
    assert "kbase_space_finance" not in await _levels(store, user="u_listed", space="ws_2")
    assert "kbase_space_finance" not in await _levels(store, user="u_listed", space="")
    # A listed person who left the Space (no role there) has nothing until they rejoin.
    assert "kbase_space_finance" not in await _levels(store, user="u_listed", role="")
    # "All Space members can use".
    assert (await _levels(store, user="u_x", space="ws_2"))["kbase_space_open"] == KNOWLEDGE_USE


@pytest.mark.integration
async def test_groups_follow_their_binding_and_inactive_groups_are_inert(store):
    # Space knowledge bases accept Team-wide groups and groups bound to that Space (both arrive in group_ids).
    assert (await _levels(store, user="u_x", groups={"grp_team"}))["kbase_space_finance"] == KNOWLEDGE_USE
    assert (await _levels(store, user="u_x", groups={"grp_ws1"}))["kbase_space_finance"] == KNOWLEDGE_EDIT
    # A group missing from group_ids (inactive, deleted, or the person left it) gives nothing.
    assert "kbase_space_finance" not in await _levels(store, user="u_x", groups=set())
    # Team knowledge bases accept Team-wide groups only: grp_ws1 is in group_ids in ws_1 but is not Team-wide.
    in_space = await _levels(store, user="u_x", groups={"grp_ws1"}, team_groups=set(), spaces=set())
    assert "kbase_team_legal" not in in_space
    team_wide = await _levels(store, user="u_x", groups={"grp_team"}, team_groups={"grp_team"}, spaces=set())
    assert team_wide["kbase_team_legal"] == KNOWLEDGE_MANAGE
    # Without a current Space every group in the context is Team-wide.
    assert (await _levels(store, user="u_x", space="", groups={"grp_team"}, spaces=set()))["kbase_team_legal"] == KNOWLEDGE_MANAGE


@pytest.mark.integration
async def test_space_entries_on_team_knowledge_bases_mean_that_spaces_members_wherever_they_work(store):
    # C9: u_x is a member of ws_3 and works in ws_1.
    assert (await _levels(store, user="u_x", spaces={"ws_1", "ws_3"}, team_groups=set()))["kbase_team_legal"] == KNOWLEDGE_EDIT
    assert "kbase_team_legal" not in await _levels(store, user="u_x", spaces={"ws_1"}, team_groups=set())
    calls = []

    async def load():
        calls.append(1)
        return TeamPrincipals(space_ids=frozenset({"ws_3"}), team_group_ids=frozenset())

    loaded = await store.resolve_knowledge_levels(_caller(user="u_x"), load_team_principals=load)
    assert loaded["kbase_team_legal"] == KNOWLEDGE_EDIT and calls == [1]
    # Without a loader those entries do not apply: narrower, never wider.
    assert "kbase_team_legal" not in await _levels(store, user="u_x")


@pytest.mark.integration
async def test_team_principals_are_loaded_only_when_a_team_knowledge_base_lists_spaces_or_groups(files_db):
    path = files_db()
    with closing(sqlite3.connect(path)) as db:
        _kb(db, "kbase_team_people", "team", TEAM)
        _member(db, "kbase_team_people", "user", "u_x", "edit")
        db.commit()

    async def load():
        raise AssertionError("no Team knowledge base lists Spaces or groups")

    levels = await KbFilesStore(path).resolve_knowledge_levels(_caller(user="u_x"), load_team_principals=load)
    assert levels["kbase_team_people"] == KNOWLEDGE_EDIT


@pytest.mark.integration
async def test_team_knowledge_bases_and_highest_level_wins(store):
    assert (await _levels(store, user="u_x"))[TEAM_DEFAULT] == KNOWLEDGE_USE
    assert (await _levels(store, user="u_x", team_admin=True))[TEAM_DEFAULT] == KNOWLEDGE_OWNER
    assert (await _levels(store, user="u_x", team_admin=True, spaces=set(), team_groups=set()))["kbase_team_legal"] == KNOWLEDGE_OWNER
    assert (await _levels(store, user="u_owner", spaces=set(), team_groups=set()))["kbase_team_legal"] == KNOWLEDGE_OWNER
    # Listed use + Space entry edit + group manage: the highest applies.
    both = await _levels(store, user="u_listed", groups={"grp_team"}, team_groups={"grp_team"}, spaces={"ws_3"})
    assert both["kbase_team_legal"] == KNOWLEDGE_MANAGE
    # A viewer cap applies to Space knowledge bases only.
    assert (await _levels(store, user="u_listed", role="viewer", spaces={"ws_3"}, team_groups=set()))["kbase_team_legal"] == KNOWLEDGE_EDIT


@pytest.mark.integration
async def test_deleted_other_team_and_other_space_knowledge_bases_are_never_resolved(store):
    levels = await _levels(store, user="u_mem", role="ws_admin", team_admin=True, spaces=set(), team_groups=set())
    assert {"kbase_deleted", "kbase_other_team", "kbase_other_space"}.isdisjoint(levels)
    assert all(levels.home_of(identity) is None for identity in ("kbase_deleted", "kbase_other_team", "kbase_other_space"))


@pytest.mark.integration
async def test_excluded_names_the_named_knowledge_bases_of_each_home_the_caller_cannot_use(store):
    outsider = await _levels(store, user="u_x", spaces=set(), team_groups=set())
    assert outsider.excluded_in("space", "ws_1") == ("kbase_space_finance",)
    assert outsider.excluded_in("team", TEAM) == ("kbase_team_legal",)
    assert outsider.home_of("kbase_space_finance") == ("space", "ws_1")
    member = await _levels(store, user="u_listed", spaces={"ws_3"}, team_groups=set())
    assert member.excluded_in("space", "ws_1") == () and member.excluded_in("team", TEAM) == ()
    # Defaults are never excluded.
    assert SPACE_DEFAULT not in outsider.excluded_in("space", "ws_1")


@pytest.mark.integration
async def test_defaults_are_reported_before_their_rows_exist(files_db):
    path = files_db()
    levels = await KbFilesStore(path).resolve_knowledge_levels(_caller(user="u_x", space="ws_new", role="member"))
    assert dict(levels) == {
        default_knowledge_base_id(TEAM, "personal", "u_x"): KNOWLEDGE_OWNER,
        default_knowledge_base_id(TEAM, "space", "ws_new"): KNOWLEDGE_EDIT,
        default_knowledge_base_id(TEAM, "team", TEAM): KNOWLEDGE_USE,
    }
    assert levels.home_of(default_knowledge_base_id(TEAM, "space", "ws_new")) == ("space", "ws_new")


@pytest.mark.integration
async def test_a_files_database_before_0239_answers_defaults_only(tmp_path):
    store = KbFilesStore(tmp_path / "kb_files.db")
    await store.initialize()
    levels = await store.resolve_knowledge_levels(_caller(user="u_x"))
    assert set(levels) == {
        default_knowledge_base_id(TEAM, home, home_id) for home, home_id in (("personal", "u_x"), ("space", "ws_1"), ("team", TEAM))
    }
    assert await store.get_knowledge_base(SPACE_DEFAULT) is None


@pytest.mark.integration
async def test_the_query_uses_the_home_index_and_the_members_key(store):
    with closing(sqlite3.connect(store._db_path)) as db:
        plan = " ".join(
            row[-1]
            for row in db.execute(
                "EXPLAIN QUERY PLAN " + _RESOLVE_SQL,
                {"user": "u", "team": TEAM, "space": "ws_1", "groups": "[]", "team_groups": "[]", "spaces": "[]", "pending_check": 1},
            )
        )
    assert "idx_kb_knowledge_bases_home" in plan
    assert "sqlite_autoindex_kb_knowledge_base_members_1" in plan
    assert "SCAN k" not in plan


# ─── KbPermission: the fail-closed file rule ────────────────────────────────


def _file(scope="space", scope_id="ws_1", *, knowledge_base_id=None, uploaded_by="u_mem") -> KbFileDTO:
    return KbFileDTO(
        file_id="kbf_1",
        artifact_id="art_1",
        filename="a.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        scope=scope,
        scope_id=scope_id,
        workspace_id=scope_id if scope in ("space", "workspace") else "",
        team_id=TEAM,
        uploaded_by=uploaded_by,
        uploaded_at=NOW,
        knowledge_base_id=knowledge_base_id,
    )


def _ctx(user="u_mem", space="ws_1", role="user", *, viewer=False, levels=None) -> ScopeContext:
    return ScopeContext(user_id=user, workspace_id=space, team_id=TEAM, role=role, space_viewer=viewer, knowledge_levels=levels)


def _named(level: int, kb_id="kbase_space_finance", home=("space", "ws_1")) -> KnowledgeLevels:
    return KnowledgeLevels({kb_id: level}, {kb_id: home})


@pytest.mark.unit
def test_named_knowledge_base_is_recognized_without_a_read():
    assert named_knowledge_base_id(_file()) is None
    assert named_knowledge_base_id(_file(knowledge_base_id=SPACE_DEFAULT)) is None
    assert named_knowledge_base_id(_file(scope="workspace", knowledge_base_id=SPACE_DEFAULT)) is None
    assert named_knowledge_base_id(_file(knowledge_base_id="kbase_space_finance")) == "kbase_space_finance"
    # Personal files stay owner-only whatever their knowledge base.
    assert named_knowledge_base_id(_file(scope="personal", scope_id="u_mem", knowledge_base_id="kbase_my_notes")) is None


@pytest.mark.unit
@pytest.mark.parametrize("levels", [None, KnowledgeLevels({}, {})])
def test_default_knowledge_base_files_keep_todays_rule_with_or_without_levels(levels):
    for stored in (None, SPACE_DEFAULT):
        own, other = _file(knowledge_base_id=stored), _file(knowledge_base_id=stored, uploaded_by="u_other")
        assert KbPermission.can_read(own, _ctx(levels=levels))
        assert KbPermission.can_delete(own, _ctx(levels=levels), "user")
        assert not KbPermission.can_delete(other, _ctx(levels=levels), "user")
        assert KbPermission.can_delete(other, _ctx(role="ws_admin", levels=levels), "ws_admin")
        assert KbPermission.can_delete(other, _ctx(role="team_admin", viewer=True, levels=levels), "team_admin")
        assert not KbPermission.can_delete(own, _ctx(viewer=True, levels=levels), "user")
        assert not KbPermission.can_read(own, _ctx(space="ws_2", levels=levels))
    team = _file(scope="team", scope_id=TEAM, knowledge_base_id=TEAM_DEFAULT)
    assert KbPermission.can_read(team, _ctx(levels=levels))
    assert not KbPermission.can_delete(team, _ctx(levels=levels), "user")
    assert KbPermission.can_write_to_scope("space", "ws_1", _ctx(levels=levels), "user", knowledge_base_id=SPACE_DEFAULT)
    assert not KbPermission.can_write_to_scope("team", TEAM, _ctx(levels=levels), "user", knowledge_base_id=TEAM_DEFAULT)


@pytest.mark.unit
def test_named_knowledge_base_files_need_levels_and_are_refused_without_them():
    own = _file(knowledge_base_id="kbase_space_finance")
    other = _file(knowledge_base_id="kbase_space_finance", uploaded_by="u_other")
    # C3: no levels (a missed call site, or the switch off) refuses even what today's rule allows.
    assert not KbPermission.can_read(own, _ctx(role="team_admin"))
    assert not KbPermission.can_delete(own, _ctx(role="team_admin"), "team_admin")
    assert not KbPermission.can_read(own, _ctx(levels=KnowledgeLevels({}, {})))
    assert KbPermission.can_read(own, _ctx(levels=_named(KNOWLEDGE_USE)))
    assert not KbPermission.can_delete(own, _ctx(levels=_named(KNOWLEDGE_USE)), "user")
    # Edit removes and moves your own files; manage anyone's.
    assert KbPermission.can_delete(own, _ctx(levels=_named(KNOWLEDGE_EDIT)), "user")
    assert not KbPermission.can_delete(other, _ctx(levels=_named(KNOWLEDGE_EDIT)), "user")
    assert KbPermission.can_delete(other, _ctx(levels=_named(KNOWLEDGE_MANAGE)), "user")
    # The level counts only in the knowledge base's own home and Space.
    assert not KbPermission.can_read(own, _ctx(space="ws_2", levels=_named(KNOWLEDGE_MANAGE)))
    assert not KbPermission.can_read(own, _ctx(levels=_named(KNOWLEDGE_MANAGE, home=("team", TEAM))))
    team_file = _file(scope="team", scope_id=TEAM, knowledge_base_id="kbase_team_legal")
    assert KbPermission.can_read(team_file, _ctx(space="", levels=_named(KNOWLEDGE_USE, "kbase_team_legal", ("team", TEAM))))
    assert not KbPermission.can_read(team_file, _ctx(space="", levels=_named(KNOWLEDGE_USE, "kbase_team_legal", ("space", "ws_1"))))
    # Personal files stay owner-only.
    mine = _file(scope="personal", scope_id="u_mem", knowledge_base_id="kbase_my_notes")
    assert KbPermission.can_read(mine, _ctx())
    assert not KbPermission.can_read(mine, _ctx(user="u_other", levels=_named(KNOWLEDGE_OWNER, "kbase_my_notes", ("personal", "u_mem"))))


@pytest.mark.unit
def test_adding_and_moving_into_a_named_knowledge_base_needs_edit_there():
    target = "kbase_space_finance"
    assert not KbPermission.can_write_to_scope("space", "ws_1", _ctx(), "user", knowledge_base_id=target)
    assert not KbPermission.can_write_to_scope("space", "ws_1", _ctx(levels=_named(KNOWLEDGE_USE)), "user", knowledge_base_id=target)
    assert KbPermission.can_write_to_scope("space", "ws_1", _ctx(levels=_named(KNOWLEDGE_EDIT)), "user", knowledge_base_id=target)
    assert not KbPermission.can_write_to_scope("space", "ws_2", _ctx(levels=_named(KNOWLEDGE_EDIT)), "user", knowledge_base_id=target)
    # A named Team knowledge base: edit there is enough, Team admin or not.
    team = _named(KNOWLEDGE_EDIT, "kbase_team_legal", ("team", TEAM))
    assert KbPermission.can_write_to_scope("team", TEAM, _ctx(levels=team), "user", knowledge_base_id="kbase_team_legal")
    own_space_file = _file()
    assert KbPermission.can_change_scope(own_space_file, _ctx(levels=team), "team", TEAM, target_knowledge_base_id="kbase_team_legal")
    assert not KbPermission.can_change_scope(own_space_file, _ctx(), "team", TEAM, target_knowledge_base_id="kbase_team_legal")
    # The level is the target's: an edit level in another home does not count.
    assert not KbPermission.can_write_to_scope("space", "ws_1", _ctx(levels=team), "user", knowledge_base_id="kbase_team_legal")
    assert KbPermission.knowledge_base_level("kbase_team_legal", _ctx(levels=team)) == KNOWLEDGE_EDIT
    assert KbPermission.knowledge_base_level("kbase_team_legal", _ctx()) == 0
