"""workflow-solidify S4c — team_skill resolve 接线（adapter + flag-gate + ctx 字段）.

`bowhead_skill.skill_resolver` duck-type 期望 `team_skill_store.get_by_name(...)`，但 UserStore
方法名是 `get_team_skill_by_name` → TeamSkillStoreAdapter 桥接。turn_runner 按 workflow_creator
flag 把 adapter 注入 ctx.team_skill_store（与 skill_instance_store 同一 build_tool_exec_ctx 点，
peer 经 copy.copy 继承）。resolver 完整解析 + load_skill 在 S0 test_skill_resolver + task#8 e2e。
"""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

from bowhead_core.user_store import TeamSkill, TeamSkillStoreAdapter, UserStore, _id, _now


async def test_adapter_delegates_and_isolates(tmp_path):
    s = UserStore(str(tmp_path / "u.db"))
    await s.initialize()
    sk = TeamSkill(
        id=_id("os"),
        team_id="team_a",
        name="r1",
        source_path="/data/team_skills/team_a/r1/",
        visibility="team_wide",
        created_by="u1",
        created_at=_now(),
        updated_at=_now(),
    )
    await s.create_team_skill(sk)
    ad = TeamSkillStoreAdapter(s)
    # get_by_name bridges get_team_skill_by_name, returning .source_path/.visibility/.created_by for the resolver.
    got = await ad.get_by_name("team_a", "r1")
    assert got is not None
    assert got.source_path == "/data/team_skills/team_a/r1/"
    assert got.visibility == "team_wide" and got.created_by == "u1"
    # Cross-team isolation survives the adapter → None.
    assert await ad.get_by_name("team_b", "r1") is None
    # disabled + default only_enabled=True → None (dispatch ignores disabled); only_enabled=False exposes it.
    await s.update_team_skill_meta("team_a", got.id, status="disabled")
    assert await ad.get_by_name("team_a", "r1") is None
    assert (await ad.get_by_name("team_a", "r1", only_enabled=False)) is not None


def test_team_skill_store_helper_flag_gated(monkeypatch):
    from bowhead_core.turn_runner import _team_skill_store

    orch = SimpleNamespace(user_store="US")
    # Flag OFF → None (team skills inert, one-switch rollback); default is already ON, so explicitly set 0 for the OFF path.
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "0")
    assert _team_skill_store(orch) is None
    # Flag ON → adapter wrapping orch.user_store.
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")
    ad = _team_skill_store(orch)
    assert isinstance(ad, TeamSkillStoreAdapter)
    assert ad._us == "US"
    # No user_store → None (no crash).
    assert _team_skill_store(SimpleNamespace(user_store=None)) is None


def test_ctx_declares_team_skill_store_field():
    """ToolExecutionContext 必有 team_skill_store 字段（turn_runner 注入 + tools.py 读它）."""
    from bowhead_core.tool_registry import ToolExecutionContext

    names = {f.name for f in fields(ToolExecutionContext)}
    assert "team_skill_store" in names
    assert "skill_instance_store" in names  # Invariant: inject team and user stores at the same point.
