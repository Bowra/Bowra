"""workflow-solidify S4 — team_skill data layer（clone team_subagent，tech-design §4.1）.

team-scoped + IDOR-safe（每 read/write 都 AND team_id）+ BEGIN IMMEDIATE (team_id,name) dup-check
+ name immutable post-create + verdict 落库。镜像 test_p3_team_subagent_store.py。
"""

from __future__ import annotations

import pytest
from bowhead_core.user_store import TeamSkill, UserStore, _id, _now


def _sk(team_id="team_a", name="channel-gm", **kw) -> TeamSkill:
    return TeamSkill(
        id=_id("oskill"),
        team_id=team_id,
        name=name,
        created_by="u1",
        created_at=_now(),
        updated_at=_now(),
        **kw,
    )


async def _store(tmp_path) -> UserStore:
    s = UserStore(str(tmp_path / "u.db"))
    await s.initialize()
    return s


async def test_create_get_round_trip(tmp_path) -> None:
    s = await _store(tmp_path)
    sk = _sk(
        display_name="渠道毛利", description="渠道毛利核对 触发：渠道毛利/口径", source_path=".data/team_skills/team_a/channel-gm/", size_bytes=842
    )
    await s.create_team_skill(sk)
    got = await s.get_team_skill("team_a", sk.id)
    assert got is not None
    assert got.name == "channel-gm" and got.display_name == "渠道毛利"
    assert got.source_path == ".data/team_skills/team_a/channel-gm/"
    assert got.size_bytes == 842 and got.visibility == "team_wide" and got.status == "enabled"
    # By-name resolution (used by skill_resolver's team branch).
    by_name = await s.get_team_skill_by_name("team_a", "channel-gm")
    assert by_name is not None and by_name.id == sk.id


async def test_dup_name_same_team_rejected_cross_team_ok(tmp_path) -> None:
    s = await _store(tmp_path)
    await s.create_team_skill(_sk(name="dup"))
    with pytest.raises(ValueError):
        await s.create_team_skill(_sk(name="dup"))  # Same name within the same team → reject.
    await s.create_team_skill(_sk(team_id="team_b", name="dup"))  # Same name across teams → OK (isolated).
    assert (await s.get_team_skill_by_name("team_b", "dup")) is not None


async def test_cross_team_read_returns_none_idor(tmp_path) -> None:
    s = await _store(tmp_path)
    sk = _sk(team_id="team_a", name="secret-recipe")
    await s.create_team_skill(sk)
    # team_b reads team_a's skill by known ID / name → None (IDOR-safe).
    assert (await s.get_team_skill("team_b", sk.id)) is None
    assert (await s.get_team_skill_by_name("team_b", "secret-recipe")) is None


async def test_list_only_enabled(tmp_path) -> None:
    s = await _store(tmp_path)
    await s.create_team_skill(_sk(name="a"))
    await s.create_team_skill(_sk(name="b", status="disabled"))
    assert {x.name for x in await s.list_team_skills("team_a")} == {"a", "b"}
    assert {x.name for x in await s.list_team_skills("team_a", only_enabled=True)} == {"a"}
    assert await s.list_team_skills("team_b") == []  # Empty across teams.


async def test_update_meta_name_immutable(tmp_path) -> None:
    s = await _store(tmp_path)
    sk = _sk(name="fixed-name", description="old")
    await s.create_team_skill(sk)
    # P1b: update_team_skill_meta no longer accepts visibility; visibility changes go only through the approval state machine.
    await s.update_team_skill_meta("team_a", sk.id, description="new", status="disabled")
    got = await s.get_team_skill("team_a", sk.id)
    assert got.name == "fixed-name"  # name is immutable (no name parameter).
    assert got.description == "new" and got.status == "disabled"
    # Cross-team update does not match.
    await s.update_team_skill_meta("team_b", sk.id, description="hacked")
    assert (await s.get_team_skill("team_a", sk.id)).description == "new"


async def test_set_verdict_and_delete(tmp_path) -> None:
    s = await _store(tmp_path)
    sk = _sk(name="r1")
    await s.create_team_skill(sk)
    assert await s.set_team_skill_verdict("team_a", sk.id, verdict="pass", verdict_json='{"checkpoints":[]}') is True
    got = await s.get_team_skill("team_a", sk.id)
    assert got.verdict == "pass" and got.verdict_json == '{"checkpoints":[]}'
    # Cross-team set_verdict does not match.
    assert await s.set_team_skill_verdict("team_b", sk.id, verdict="fail") is False
    # delete team-bound.
    assert await s.delete_team_skill("team_b", sk.id) is False  # No cross-team deletion.
    assert await s.delete_team_skill("team_a", sk.id) is True
    assert (await s.get_team_skill("team_a", sk.id)) is None
