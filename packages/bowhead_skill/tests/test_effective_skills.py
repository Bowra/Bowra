"""skill-audit 2026-07 统一视图 —— effective.list_effective_skills 语义.

契约 = resolve_skill 的列表版：列表里出现的名字就是执行时真能 resolve 的名字，
再叠加 per-user 停用过滤。重名优先级 catalog > user > team（低优先级不列）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_skill.effective import list_effective_skills


class _FakeCatalog:
    def __init__(self, specs):
        self._specs = {s.name: s for s in specs}

    def list_names(self):
        return sorted(self._specs)

    def list_specs(self):
        return [self._specs[n] for n in sorted(self._specs)]

    def get(self, name):
        return self._specs.get(name)


class _FakeUserStore:
    def __init__(self, insts, *, boom=False):
        self._insts = list(insts)
        self._boom = boom

    async def list_for_user(self, *, user_id):
        if self._boom:
            raise RuntimeError("db down")
        return self._insts

    async def get(self, *, user_id, name):
        for i in self._insts:
            if i.name == name:
                return i
        return None


class _FakeTeamStore:
    def __init__(self, recs):
        self._recs = list(recs)

    async def list(self, team_id, only_enabled=True):
        return self._recs


def _catalog_spec(name: str, desc: str = "builtin desc"):
    return SimpleNamespace(name=name, description=desc, version="1.0", scripts=[])


def _write_user_skill(tmp_path, uid: str, name: str, desc: str = "user skill desc"):
    d = tmp_path / ".data" / "user_skills" / uid / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nversion: 0.1.0\n---\nbody\n",
        encoding="utf-8",
    )
    return SimpleNamespace(name=name, source_type="user_upload", source_path=str(d), enabled=True)


def _team_rec(name: str, *, visibility: str = "team_wide", created_by: str = "creator", desc: str = "team skill desc"):
    return SimpleNamespace(id=f"oskill_{name}", name=name, description=desc, visibility=visibility, created_by=created_by)


def _ctx(**kw):
    base = {
        "user_id": "u1",
        "team_id": "t1",
        "skill_instance_store": None,
        "team_skill_store": None,
        "granted_team_skill_ids": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_user_and_team_skills_listed_with_origin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inst = _write_user_skill(tmp_path, "u1", "my-skill")
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([inst]),
        team_skill_store=_FakeTeamStore([_team_rec("team-skill")]),
    )
    cat = _FakeCatalog([_catalog_spec("builtin-a")])
    entries = await list_effective_skills(ctx, cat)
    by_name = {e.name: e for e in entries}
    assert by_name["builtin-a"].origin == "catalog"
    assert by_name["my-skill"].origin == "user"
    assert by_name["team-skill"].origin == "team"


@pytest.mark.asyncio
async def test_disabled_user_skill_hidden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inst = _write_user_skill(tmp_path, "u1", "my-skill")
    inst.enabled = False
    ctx = _ctx(skill_instance_store=_FakeUserStore([inst]))
    entries = await list_effective_skills(ctx, _FakeCatalog([]))
    assert entries == []


@pytest.mark.asyncio
async def test_creator_only_visibility(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recs = [_team_rec("secret-skill", visibility="creator_only", created_by="creator")]
    cat = _FakeCatalog([])
    # Not the creator: invisible.
    entries = await list_effective_skills(_ctx(team_skill_store=_FakeTeamStore(recs)), cat)
    assert entries == []
    # The creator: visible.
    entries = await list_effective_skills(
        _ctx(user_id="creator", team_skill_store=_FakeTeamStore(recs)),
        cat,
    )
    assert [e.name for e in entries] == ["secret-skill"]
    # Shared with a non-creator (matching a prefetched ID): visible.
    entries = await list_effective_skills(
        _ctx(
            team_skill_store=_FakeTeamStore(recs),
            granted_team_skill_ids=frozenset({"oskill_secret-skill"}),
        ),
        cat,
    )
    assert [e.name for e in entries] == ["secret-skill"]


@pytest.mark.asyncio
async def test_team_deduped_when_user_has_same_name(tmp_path, monkeypatch):
    """resolve 优先级 user > team——同名 team 条目不列（列了也 resolve 不到它）."""
    monkeypatch.chdir(tmp_path)
    inst = _write_user_skill(tmp_path, "u1", "dup-name")
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([inst]),
        team_skill_store=_FakeTeamStore([_team_rec("dup-name")]),
    )
    entries = await list_effective_skills(ctx, _FakeCatalog([]))
    assert [(e.name, e.origin) for e in entries] == [("dup-name", "user")]


@pytest.mark.asyncio
async def test_team_skill_per_user_disable_filtered(tmp_path, monkeypatch):
    """Legacy name-key preferences remain effective during the stable-ID rollout."""
    monkeypatch.chdir(tmp_path)
    disable_rec = SimpleNamespace(name="team-skill", source_type="team_shared", enabled=False, source_path="")
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([disable_rec]),
        team_skill_store=_FakeTeamStore([_team_rec("team-skill")]),
    )
    entries = await list_effective_skills(ctx, _FakeCatalog([]))
    assert entries == []


@pytest.mark.asyncio
async def test_team_skill_stable_id_preference_is_filtered(tmp_path, monkeypatch):
    """The Team preference key cannot collide with a personal Skill slug."""
    monkeypatch.chdir(tmp_path)
    disable_rec = SimpleNamespace(
        name="team:oskill_team-skill",
        source_type="team_shared",
        enabled=False,
        source_path="",
    )
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([disable_rec]),
        team_skill_store=_FakeTeamStore([_team_rec("team-skill")]),
    )

    entries = await list_effective_skills(ctx, _FakeCatalog([]))

    assert entries == []


@pytest.mark.asyncio
async def test_team_preference_cannot_disable_same_name_catalog_skill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    disable_rec = SimpleNamespace(
        name="team:oskill_builtin-a",
        source_type="team_shared",
        enabled=False,
        source_path="",
    )
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([disable_rec]),
        team_skill_store=_FakeTeamStore([_team_rec("builtin-a")]),
    )

    entries = await list_effective_skills(ctx, _FakeCatalog([_catalog_spec("builtin-a")]))

    assert [(entry.name, entry.origin) for entry in entries] == [("builtin-a", "catalog")]


@pytest.mark.asyncio
async def test_include_catalog_false_keeps_shadow_guard(tmp_path, monkeypatch):
    """Lead 动态段路：不返 catalog 条目，但 shadow builtin 名的 user skill 仍被去重."""
    monkeypatch.chdir(tmp_path)
    shadow = _write_user_skill(tmp_path, "u1", "builtin-a")
    real = _write_user_skill(tmp_path, "u1", "my-skill")
    ctx = _ctx(skill_instance_store=_FakeUserStore([shadow, real]))
    entries = await list_effective_skills(ctx, _FakeCatalog([_catalog_spec("builtin-a")]), include_catalog=False)
    assert [(e.name, e.origin) for e in entries] == [("my-skill", "user")]


@pytest.mark.asyncio
async def test_broken_user_skill_shadows_team_same_name(tmp_path, monkeypatch):
    """review 契约边角：坏 user 记录（目录丢失）占名后，team 同名不列——resolve 的
    user 腿命中记录后不落穿 team，列出 team 条目就是「列得到调不动」。"""
    monkeypatch.chdir(tmp_path)
    broken = SimpleNamespace(
        name="dup-name",
        source_type="user_upload",
        source_path=str(tmp_path / ".data" / "user_skills" / "u1" / "gone"),
        enabled=True,
    )
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([broken]),
        team_skill_store=_FakeTeamStore([_team_rec("dup-name")]),
    )
    entries = await list_effective_skills(ctx, _FakeCatalog([]))
    assert entries == []


@pytest.mark.asyncio
async def test_team_desc_source_truncated(tmp_path, monkeypatch):
    """security review P1 读侧兜底：历史脏数据的超长 team description 源头截断."""
    monkeypatch.chdir(tmp_path)
    rec = _team_rec("big-desc", desc="长" * 10_000)
    ctx = _ctx(team_skill_store=_FakeTeamStore([rec]))
    entries = await list_effective_skills(ctx, _FakeCatalog([]))
    assert len(entries) == 1
    assert len(entries[0].description) == 2048


@pytest.mark.asyncio
async def test_user_store_failure_degrades_to_other_origins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = _ctx(
        skill_instance_store=_FakeUserStore([], boom=True),
        team_skill_store=_FakeTeamStore([_team_rec("team-skill")]),
    )
    entries = await list_effective_skills(ctx, _FakeCatalog([_catalog_spec("builtin-a")]))
    assert [e.name for e in entries] == ["builtin-a", "team-skill"]
