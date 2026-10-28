"""workflow-solidify S4 — team_skills CRUD route tests（clone test_p3_team_subagents_routes）.

member-open CRUD round-trip + team-scoping/IDOR（team_a 成员不可见/动 team_b）+ create 把 SKILL.md
写盘（reserved-floor + 格式 gate via _validate_skill_dir）+ name 从 frontmatter + dup 拒 +
delete 删行+删盘 + best-effort audit。bare app 直挂 router（gate 默认放行，无 entitlement 配置）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bowhead_core.user_store import TeamSkill, User, UserStore, _id, _now
from bowhead_gateway.routes import team_skills as oskr
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path) -> UserStore:
    s = UserStore(str(tmp_path / "u.db"))
    _run(s.initialize())
    return s


def _client(store: UserStore, user: User, monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(oskr, "_TEAM_SKILLS_ROOT", tmp_path / "team_skills_data")
    app = FastAPI()
    app.include_router(oskr.router, prefix="/api/v1/team-skills")
    app.state.user_store = store
    app.dependency_overrides[oskr._current_user] = lambda: user
    return TestClient(app)


def _skill_md(name="channel-gm-recipe", desc="渠道毛利核对 触发：渠道毛利/口径") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n## 执行步骤\n1. 取数\n2. 算毛利\n"


_BASE = "/api/v1/team-skills"


def test_create_writes_disk_and_row(tmp_path, monkeypatch):
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    r = client.post(_BASE, json={"body_md": _skill_md(), "display_name": "渠道毛利", "scripts": [{"name": "step_1.py", "content": "x = 1\n"}]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "channel-gm-recipe" and data["display_name"] == "渠道毛利"
    assert data["visibility"] == "team_wide" and data["size_bytes"] > 0
    assert "source_path" not in data  # Curated view hides disk paths.
    # SKILL.md + scripts written on disk.
    sk_dir = tmp_path / "team_skills_data" / "team_a" / "channel-gm-recipe"
    assert (sk_dir / "SKILL.md").is_file()
    assert (sk_dir / "scripts" / "step_1.py").is_file()
    # Row exists.
    row = _run(store.get_team_skill("team_a", data["id"]))
    assert row is not None and Path(row.source_path).name == "channel-gm-recipe"


def test_list_carries_audience_badge(tmp_path, monkeypatch):
    """content-permissions: team_skills list 带 audience/share_count(喂 workflows 页分享徽标)。
    无 access_control_store → roster 空,audience 由 visibility 派生(team_wide / private)。"""
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    client.post(_BASE, json={"body_md": _skill_md()})  # Default team_wide.
    rid = client.post(_BASE, json={"body_md": _skill_md(name="private-recipe", desc="私有食谱 触发：私有/口径")}).json()["id"]
    client.patch(f"{_BASE}/{rid}", json={"visibility": "creator_only"})

    items = {i["name"]: i for i in client.get(_BASE).json()["items"]}
    assert items["channel-gm-recipe"]["audience"] == "team_wide"
    assert items["channel-gm-recipe"]["share_count"] == 0
    assert items["private-recipe"]["audience"] == "private"
    assert items["private-recipe"]["share_count"] == 0


def test_crud_roundtrip_and_idor(tmp_path, monkeypatch):
    store = _store(tmp_path)
    # Seed team_b recipe; team_a members must never see/modify it.
    _run(
        store.create_team_skill(
            TeamSkill(
                id=_id("oskill"), team_id="team_b", name="other-recipe", source_path="/x", created_by="u_b", created_at=_now(), updated_at=_now()
            )
        )
    )
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    # Create team_a's.
    rid = client.post(_BASE, json={"body_md": _skill_md()}).json()["id"]
    # List own team only.
    items = client.get(_BASE).json()["items"]
    assert {i["name"] for i in items} == {"channel-gm-recipe"}
    # GET own → 200.
    assert client.get(f"{_BASE}/{rid}").status_code == 200
    # IDOR: team_a member reads team_b ID → 404.
    ob = _run(store.get_team_skill_by_name("team_b", "other-recipe"))
    assert client.get(f"{_BASE}/{ob.id}").status_code == 404
    assert client.delete(f"{_BASE}/{ob.id}").status_code == 404  # No cross-team deletion.


def test_patch_visibility_status(tmp_path, monkeypatch):
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    # Non-admin create defaults team_wide → pending (skill-team-governance gate 1); pending
    # freezes metadata (CAS does not overwrite description, prevents approval-card substitution); withdraw (→creator_only) before edits.
    rid = client.post(_BASE, json={"body_md": _skill_md()}).json()["id"]
    assert client.patch(f"{_BASE}/{rid}", json={"display_name": "改名"}).status_code == 403  # pending frozen.
    r = client.patch(f"{_BASE}/{rid}", json={"visibility": "creator_only"})  # L1 withdrawal semantics.
    assert r.status_code == 200, r.text
    r = client.patch(f"{_BASE}/{rid}", json={"display_name": "改名", "status": "disabled"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["display_name"] == "改名" and d["visibility"] == "creator_only" and d["status"] == "disabled"
    assert d["name"] == "channel-gm-recipe"  # name immutable.
    # Invalid visibility → 422.
    assert client.patch(f"{_BASE}/{rid}", json={"visibility": "public"}).status_code == 422


def test_delete_removes_row_and_disk(tmp_path, monkeypatch):
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    rid = client.post(_BASE, json={"body_md": _skill_md()}).json()["id"]
    sk_dir = tmp_path / "team_skills_data" / "team_a" / "channel-gm-recipe"
    assert sk_dir.is_dir()
    assert client.delete(f"{_BASE}/{rid}").status_code == 204
    assert _run(store.get_team_skill("team_a", rid)) is None  # Row deleted.
    assert not sk_dir.exists()  # Disk deleted.
    assert client.delete(f"{_BASE}/{rid}").status_code == 404  # Repeated delete → 404.


def test_dup_name_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    assert client.post(_BASE, json={"body_md": _skill_md()}).status_code == 200
    # Same-team same-name create again → 422 (file-layer final_dir exists).
    assert client.post(_BASE, json={"body_md": _skill_md()}).status_code == 422


def test_reserved_name_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    # python_sandbox is built-in skill name → reserved-floor rejects (_validate_skill_dir chokepoint).
    r = client.post(_BASE, json={"body_md": _skill_md(name="python_sandbox")})
    assert r.status_code == 422


def test_create_no_team_403(tmp_path, monkeypatch):
    store = _store(tmp_path)
    client = _client(store, User(id="u_a", team_id="", email="a@x.com"), monkeypatch, tmp_path)
    assert client.post(_BASE, json={"body_md": _skill_md()}).status_code == 403


def test_creator_only_hidden_from_other_members(tmp_path, monkeypatch):
    """P2（review）：creator_only team-skill 只 created_by 可见；同 team 其他成员 list 看不到、get 404
    （反枚举）。与 agent 路径 skill_resolver._visibility_ok 同语义，堵管理 HTTP 面的元数据枚举。"""
    store = _store(tmp_path)
    alice = User(id="u_alice", team_id="team_a", email="alice@x.com")
    bob = User(id="u_bob", team_id="team_a", email="bob@x.com")
    # alice creates a creator_only team skill.
    ac = _client(store, alice, monkeypatch, tmp_path)
    rid = ac.post(_BASE, json={"body_md": _skill_md(name="alice-private"), "visibility": "creator_only"}).json()["id"]
    # Creator alice sees list entry + GET 200.
    assert "alice-private" in {i["name"] for i in ac.get(_BASE).json()["items"]}
    assert ac.get(f"{_BASE}/{rid}").status_code == 200
    # Same-team noncreator bob sees no list entry, GET 404.
    bc = _client(store, bob, monkeypatch, tmp_path)
    assert "alice-private" not in {i["name"] for i in bc.get(_BASE).json()["items"]}
    assert bc.get(f"{_BASE}/{rid}").status_code == 404


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("u.db",):
        initialize_migrated_core_database(tmp_path / filename)


def test_list_names_creators_without_leaking_raw_user_ids(tmp_path, monkeypatch):
    """The workflows page shows ``created_by_name``: a display name, else the email local part, else null (never ``usr_*``)."""
    store = _store(tmp_path)
    team = _run(store.create_team(name="Team A", max_seats=5))
    named = _run(store.create_user(team_id=team.id, email="ann@x.com", display_name="Ann Lee"))
    email_only = _run(store.create_user(team_id=team.id, email="bob@x.com"))
    creators = {"named-recipe": named.id, "email-recipe": email_only.id, "ghost-recipe": "usr_24f0f61b37bb"}
    for name, creator in creators.items():
        _run(
            store.create_team_skill(
                TeamSkill(id=_id("oskill"), team_id=team.id, name=name, source_path="/x", created_by=creator, created_at=_now(), updated_at=_now())
            )
        )
    client = _client(store, User(id=named.id, team_id=team.id, email="ann@x.com"), monkeypatch, tmp_path)

    items = {i["name"]: i for i in client.get(_BASE).json()["items"]}

    assert {name: items[name]["created_by_name"] for name in creators} == {
        "named-recipe": "Ann Lee",
        "email-recipe": "bob",
        "ghost-recipe": None,
    }
