"""Skill updates since an Agent release (agent-skill-gates D2)."""

from __future__ import annotations

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.release_skill_versions import skill_updates_conn
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.user_store import TeamSkill, UserStore

from bowhead_skill.catalog import builtin_catalog_dir


def _grant(kind: str, skill_id: str, name: str, revision: str) -> dict[str, object]:
    return {
        "skill_ref": {"kind": kind, "id": skill_id, "name": name},
        "permissions": ["read"],
        "revision": revision,
        "distribution": "reference",
    }


@pytest.mark.asyncio
async def test_skill_updates_list_skills_whose_approved_revision_changed(tmp_path):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Team", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    bundle = tmp_path / "sales-report"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("---\nname: sales-report\ndescription: Report\n---\n\n# Report\n", encoding="utf-8")
    released = f"sha256:{bundle_sha256(bundle)}"
    await users.create_team_skill(
        TeamSkill(
            id="oskill_sales",
            team_id=team.id,
            name="sales-report",
            source_path=str(bundle),
            created_by=owner.id,
            approval="approved",
            approved_sha256=released.removeprefix("sha256:"),
            approved_at="2026-10-01T00:00:00+00:00",
            created_at="2026-10-01T00:00:00+00:00",
            updated_at="2026-10-01T00:00:00+00:00",
        )
    )
    builtin_current = f"sha256:{bundle_sha256(builtin_catalog_dir() / 'python_sandbox')}"
    definition = {
        "skill_grants": [
            _grant("team", "oskill_sales", "sales-report", released),
            _grant("builtin", "builtin:python_sandbox", "python_sandbox", builtin_current),
        ]
    }
    async with open_shared_ctx(str(path)) as db:
        assert await skill_updates_conn(db, team_id=team.id, owner_user_id=owner.id, definition=definition) == []

    (bundle / "SKILL.md").write_text("---\nname: sales-report\ndescription: Report v2\n---\n\n# Report\n", encoding="utf-8")
    approved = bundle_sha256(bundle)
    async with open_shared_ctx(str(path)) as db:
        await db.execute(
            "UPDATE team_skill SET approved_sha256=?,approved_at='2026-10-16T00:00:00+00:00' WHERE id='oskill_sales'",
            (approved,),
        )
        await db.commit()
    stale_builtin = {"skill_grants": [_grant("builtin", "builtin:python_sandbox", "python_sandbox", "sha256:" + "0" * 64)]}
    async with open_shared_ctx(str(path)) as db:
        updates = await skill_updates_conn(db, team_id=team.id, owner_user_id=owner.id, definition=definition)
        builtin = await skill_updates_conn(db, team_id=team.id, owner_user_id=owner.id, definition=stale_builtin)

    assert len(updates) == 1
    assert updates[0]["name"] == "sales-report"
    assert updates[0]["identity"] == "team:oskill_sales"
    assert updates[0]["release_revision"] == released
    assert updates[0]["current_revision"] == f"sha256:{approved}"
    assert updates[0]["changed_at"]
    assert builtin == [
        {
            "name": "python_sandbox",
            "identity": "builtin:builtin:python_sandbox",
            "release_revision": "sha256:" + "0" * 64,
            "current_revision": builtin_current,
            "changed_at": None,
        }
    ]
