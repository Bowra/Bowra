"""Closure plus canonical scopes/stores and immutable file capture, without LLM IO."""

from __future__ import annotations

import shutil
import sqlite3

import pytest
import yaml
from bowhead_core.skill_read_closure import SkillReadClosureError, prepare_skill_read_closure
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from test_skill_read_resolution import app_ctx as _app_ctx
from test_skill_read_resolution import case as _case
from test_skill_read_resolution import ref

app_ctx = _app_ctx
case = _case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def declaration(path, **changes):
    manifest = path / "SKILL.md"
    _, raw, body = manifest.read_text().split("---\n", 2)
    data = yaml.safe_load(raw)
    data["read_only_entry"].update(changes)
    manifest.write_text("---\n" + yaml.safe_dump(data) + "---\n" + body)


def graph(case):
    team = ref("team", case.team.id, case.team_root)
    declaration(case.personal_root, required_skill_refs=[team.model_dump(mode="json")])
    user = ref("user", case.personal.id, case.personal_root)
    declaration(case.builtin, required_skill_refs=[user.model_dump(mode="json")])
    builtin = ref("builtin", "builtin:dupont", case.builtin)
    case.holder.index = build_builtin_skill_metadata(builtin_root=case.builtin_root)
    return builtin, user, team


async def test_mixed_origin_dependency_closure_uses_exact_authorized_bytes(case):
    builtin, user, team = graph(case)
    result = await prepare_skill_read_closure(case.resolver, [builtin, team], scope=case.scope)
    assert result.roots == (builtin, team)
    assert [item.proof.resource for item in result.items] == [team, user, builtin]
    assert [item.material.document()["files"][0]["text"] for item in result.items] == ["TEAM_METHOD", "PERSONAL_METHOD", "SELECTED_METHOD"]
    assert len({item.proof.policy_epoch for item in result.items}) == 1
    assert str(case.personal_root) not in repr(result)
    assert "PERSONAL_METHOD" not in repr(result)


async def test_explicit_parent_selection_does_not_grant_a_private_dependency(case):
    builtin, _, _ = graph(case)
    with sqlite3.connect(case.db_path) as db:
        db.execute("UPDATE team_skill SET visibility='creator_only',approved_sha256=NULL WHERE id=?", (case.team.id,))
    with pytest.raises(SkillReadResolutionError):
        await prepare_skill_read_closure(case.resolver, [builtin], scope=case.scope)


@pytest.mark.parametrize("change", ["pending", "path", "policy"])
async def test_later_child_preparation_cannot_hide_earlier_source_changes(case, monkeypatch, tmp_path, change):
    builtin, _, team = graph(case)
    original = case.resolver.prepare

    async def prepare_then_change(resource, *, scope):
        resolved = await original(resource, scope=scope)
        if resource == team:
            if change == "policy":
                await case.access.create_or_update_resource_grant(
                    team_id=scope.team_id,
                    resource_kind="skill",
                    resource_id="unrelated",
                    principal_type="user",
                    principal_id=scope.user_id,
                    permission="use",
                    effect="allow",
                )
            else:
                with sqlite3.connect(case.db_path) as db:
                    if change == "pending":
                        db.execute("UPDATE skill_instances SET import_status='pending' WHERE id=?", (case.personal.id,))
                    else:
                        relocated = tmp_path / "relocated"
                        shutil.copytree(case.personal_root, relocated)
                        db.execute("UPDATE skill_instances SET source_path=? WHERE id=?", (str(relocated), case.personal.id))
        return resolved

    monkeypatch.setattr(case.resolver, "prepare", prepare_then_change)
    with pytest.raises(SkillReadResolutionError):
        await prepare_skill_read_closure(case.resolver, [builtin], scope=case.scope)


async def test_real_declared_files_obey_aggregate_per_package_text_budget(case):
    (case.builtin / "method.md").write_text("a" * 131072)
    (case.builtin / "required.md").write_text("b" * 131073)
    declaration(case.builtin, required_files=["required.md"])
    case.holder.index = build_builtin_skill_metadata(builtin_root=case.builtin_root)
    resource = ref("builtin", "builtin:dupont", case.builtin)
    # Each file meets the material contract; the composed method exceeds the
    # stricter per-package budget and must not become a partial injection.
    await case.resolver.prepare(resource, scope=case.scope)
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_material_limit$"):
        await prepare_skill_read_closure(case.resolver, [resource], scope=case.scope)
