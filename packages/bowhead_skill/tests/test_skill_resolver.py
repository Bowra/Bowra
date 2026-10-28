"""resolve_skill per-request 解析 layer 的 UT（workflow-solidify S0 / tech-design §1.5）.

结构性多租户隔离的硬保证：shadow 不可能、跨 user/team 碰撞各自独立、fail-CLOSED。
async 用 asyncio.run 包装（bowhead_skill test 不配 pytest-asyncio）。
"""

import asyncio
from pathlib import Path

from bowhead_skill.skill_resolver import resolve_skill

from bowhead_skill.catalog import SkillCatalog


def _write(d: Path, name: str, desc: str = "x") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\nbody for {name}\n",
        encoding="utf-8",
    )
    return d


class _Rec:
    def __init__(self, source_type, source_path, visibility=None, created_by=None):
        self.source_type = source_type
        self.source_path = source_path
        self.visibility = visibility
        self.created_by = created_by


class _UserStore:
    def __init__(self):
        self._d = {}

    def put(self, uid, name, rec):
        self._d[(uid, name)] = rec

    async def get(self, *, user_id, name):
        return self._d.get((user_id, name))


class _TeamStore:
    def __init__(self):
        self._d = {}

    def put(self, team_id, name, rec):
        self._d[(team_id, name)] = rec

    async def get_by_name(self, team_id, name, only_enabled=True):
        return self._d.get((team_id, name))


def _run(coro):
    return asyncio.run(coro)


def test_trusted_root_visible_to_any_ctx(tmp_path):
    b = _write(tmp_path / "builtin" / "af", "analyst-framework")  # noqa: F841
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    s = _run(resolve_skill("analyst-framework", catalog=cat, user_id="A", user_skill_store=_UserStore()))
    assert s is not None and s.name == "analyst-framework"
    # builtin needs no user / store; the trust root permits every ctx.
    assert _run(resolve_skill("analyst-framework", catalog=cat)) is not None


def test_shadow_structurally_impossible(tmp_path):
    """§10-A 核心结构修：user A 存同名 python_sandbox 也 shadow 不了 builtin."""
    _write(tmp_path / "builtin" / "ps", "python_sandbox", "BUILTIN")
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    evil = _write(tmp_path / "ud" / "A" / "ps", "python_sandbox", "EVIL")
    st = _UserStore()
    st.put("A", "python_sandbox", _Rec("user_generated", str(evil)))
    # ctx=B invoking python_sandbox → builtin wins (catalog.get first); never query A's user_skill.
    assert _run(resolve_skill("python_sandbox", catalog=cat, user_id="B", user_skill_store=st)).description == "BUILTIN"
    # ctx=A also resolves to builtin, not its own EVIL.
    assert _run(resolve_skill("python_sandbox", catalog=cat, user_id="A", user_skill_store=st)).description == "BUILTIN"


def test_cross_user_collision_isolated(tmp_path):
    """跨 user 同名各自独立（pre-existing 碰撞 bug 修）."""
    (tmp_path / "builtin").mkdir()
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    a = _write(tmp_path / "ud" / "A" / "foo", "foo", "A-foo")
    bb = _write(tmp_path / "ud" / "B" / "foo", "foo", "B-foo")
    st = _UserStore()
    st.put("A", "foo", _Rec("user_generated", str(a)))
    st.put("B", "foo", _Rec("user_generated", str(bb)))
    assert _run(resolve_skill("foo", catalog=cat, user_id="A", user_skill_store=st)).description == "A-foo"
    assert _run(resolve_skill("foo", catalog=cat, user_id="B", user_skill_store=st)).description == "B-foo"


def test_builtin_disable_override_record_not_loaded(tmp_path):
    """skill_instance 的 source_type='builtin'（disable override 记录）不被 step2 load."""
    _write(tmp_path / "builtin" / "ps", "python_sandbox", "BUILTIN")
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    st = _UserStore()
    st.put("A", "python_sandbox", _Rec("builtin", "/nonexistent/override/path"))
    # Resolve to trust root BUILTIN; never load the builtin-override record's source_path.
    assert _run(resolve_skill("python_sandbox", catalog=cat, user_id="A", user_skill_store=st)).description == "BUILTIN"


def test_miss_returns_none(tmp_path):
    (tmp_path / "builtin").mkdir()
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    assert _run(resolve_skill("nonexistent", catalog=cat, user_id="A", user_skill_store=_UserStore())) is None


def test_team_store_none_skips(tmp_path):
    """S0：team_skill_store=None → team 分支跳过（不崩）."""
    (tmp_path / "builtin").mkdir()
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    assert _run(resolve_skill("x", catalog=cat, user_id="A", team_id="O1", user_skill_store=_UserStore(), team_skill_store=None)) is None


def test_team_visibility_and_cross_team(tmp_path):
    """team_wide 任意本 team 可见 / creator_only 仅 creator / 跨 team 不可见."""
    (tmp_path / "builtin").mkdir()
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    cat.refresh()
    shared = _write(tmp_path / "od" / "O1" / "shared", "shared", "SHARED")
    priv = _write(tmp_path / "od" / "O1" / "priv", "priv", "PRIV")
    ost = _TeamStore()
    ost.put("O1", "shared", _Rec("team", str(shared), visibility="team_wide"))
    ost.put("O1", "priv", _Rec("team", str(priv), visibility="creator_only", created_by="creator1"))
    us = _UserStore()
    # team_wide → any user in this team.
    assert _run(resolve_skill("shared", catalog=cat, user_id="U2", team_id="O1", user_skill_store=us, team_skill_store=ost)).description == "SHARED"
    # creator_only → invisible to non-creators.
    assert _run(resolve_skill("priv", catalog=cat, user_id="U2", team_id="O1", user_skill_store=us, team_skill_store=ost)) is None
    # creator_only → visible to the creator.
    assert _run(resolve_skill("priv", catalog=cat, user_id="creator1", team_id="O1", user_skill_store=us, team_skill_store=ost)).description == "PRIV"
    # Cross-team: O2 cannot see O1's shared skill (key mismatch provides structural isolation).
    assert _run(resolve_skill("shared", catalog=cat, user_id="U3", team_id="O2", user_skill_store=us, team_skill_store=ost)) is None
