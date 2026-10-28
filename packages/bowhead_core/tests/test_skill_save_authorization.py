"""真实 ToolRegistry 保存不得绕过目标 Skill 的显式写授权。"""

from pathlib import Path

import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_save_tool import register_skill_save_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

from bowhead_skill import SkillCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["allow", "deny"])
async def test_save_obeys_personal_skill_write_grant(tmp_path, monkeypatch, effect):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    store = SkillInstanceStore(str(tmp_path / "skills.db"))
    await store.initialize()
    ctx = ToolExecutionContext(
        workspace_id="ws_sales",
        member_uid="u_alice",
        user_id="u_alice",
        turn_id="t_review",
        datarc=object(),
        access_context=AccessContext(
            user_id="u_alice",
            team_id="team_a",
            workspace_id="ws_sales",
            workspace_role="member",
            team_role=None,
            group_ids=(),
            policy_epoch=1,
            is_team_admin=False,
        ).__dict__,
        access_control_store=access,
    )
    ctx.team_id = "team_a"
    ctx.skill_instance_store = store
    ctx.skill_catalog = SkillCatalog([])
    registry = ToolRegistry()
    register_skill_save_tool(registry)
    body = "---\nname: review-recipe\ndescription: recipe\n---\nOriginal\n"
    first = await registry.execute("skill_save_generated", {"body_md": body}, ctx)
    assert first.error is None
    path = Path(".data/user_skills/u_alice/review-recipe/SKILL.md")
    before = path.read_text()
    await access.create_or_update_resource_grant(
        team_id="team_a",
        resource_kind="skill",
        resource_id="review-recipe",
        principal_type="user",
        principal_id="u_alice",
        permission="write",
        effect=effect,
        created_by="u_admin",
    )
    second = await registry.execute("skill_save_generated", {"body_md": body.replace("Original", "Changed"), "overwrite": True}, ctx)
    actual = path.read_text()
    if effect == "deny":
        assert actual == before
        assert second.error is not None
    else:
        assert second.error is None
        assert "Changed" in actual
