"""A Skill selected with `/` stays the selected source for Lead and its helpers (skill-bundle-read tech-design §6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.selected_skill_runtime import history_selected_resources, resolve_selected_skill
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from bowhead_core.tool_registry import ToolRegistry
from bowhead_skill.loader import load_skill
from bowhead_skill.tools import build_skill_tools

from bowhead_core import ordinary_lead_material, selected_skill_runtime
from bowhead_skill import SkillCatalog, SkillRuntime

pytestmark = pytest.mark.asyncio

TEAM = SkillResourceRef(kind="skill", source_kind="team", id="tskill_1", version="sha256:" + "1" * 64)


def _skill(root: Path, owner: str, body: str) -> Path:
    folder = root / owner / "report"
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: report\ndescription: d\n---\n{body}\n")
    (folder / "references" / "guide.md").write_text(f"{owner} guide")
    return folder


class _Instances:
    """A disabled personal copy with the selected team Skill's name."""

    def __init__(self, folder: Path) -> None:
        self.record = SimpleNamespace(id="sk_personal", name="report", enabled=False, source_type="user_upload", source_path=str(folder))

    async def get(self, *, user_id, name, **_):
        return self.record if name == "report" else None


class _Teams:
    async def get_by_name(self, team_id, name, *, only_enabled=True):
        return None  # Description lookups by name find nothing; the selected source is by id.

    async def get_team_skill(self, team_id, skill_id, *, only_enabled=False):
        return SimpleNamespace(id=skill_id, name="report", created_by="u_author") if (team_id, skill_id) == ("team_1", "tskill_1") else None


def _ctx(personal: Path, **extra):
    return SimpleNamespace(
        user_id="u_1",
        team_id="team_1",
        workspace_id="ws_1",
        session_id="s_1",
        skill_instance_store=_Instances(personal),
        team_skill_store=_Teams(),
        selected_skill_resources={"report": TEAM},
        **extra,
    )


async def test_tools_read_the_selected_source_not_a_same_name_personal_copy(tmp_path):
    personal, team = _skill(tmp_path, "personal", "PERSONAL DRAFT"), _skill(tmp_path, "team", "TEAM METHOD")
    seen = []

    async def resolve_selected(ctx, name):
        seen.append(name)
        return load_skill(team)

    tools = {tool.name: tool.handler for tool in build_skill_tools(SkillCatalog(roots=[]), SkillRuntime(), resolve_selected=resolve_selected)}
    ctx = _ctx(personal)
    body = await tools["skill_read"]({"name": "report"}, ctx)
    guide = await tools["skill_read"]({"name": "report", "file": "references/guide.md"}, ctx)
    assert "TEAM METHOD" in body["body"] and guide["body"] == "team guide"
    # The disabled personal copy neither shadows nor disables the selected team Skill.
    assert seen == ["report", "report"]


async def test_a_selected_skill_that_changed_is_refused_with_a_correction(tmp_path):
    personal = _skill(tmp_path, "personal", "PERSONAL DRAFT")

    async def resolve_selected(ctx, name):
        return selected_skill_runtime.selected_skill_changed(name)

    tools = {tool.name: tool.handler for tool in build_skill_tools(SkillCatalog(roots=[]), SkillRuntime(), resolve_selected=resolve_selected)}
    for tool, args in (("skill_read", {"name": "report"}), ("skill_invoke", {"name": "report", "script": "run.py"})):
        result = await tools[tool](args, _ctx(personal))
        assert result["error_kind"] == "selected_skill_changed" and "select it again with /" in result["error"]
        assert "PERSONAL" not in str(result)


async def test_resolution_checks_the_exact_source_and_its_version(tmp_path, monkeypatch):
    team = _skill(tmp_path, "team", "TEAM METHOD")
    outcome = {}

    class Sources:
        async def prepare_runtime_package(self, resource, *, scope):
            if outcome.get("error"):
                raise outcome["error"]
            assert resource == TEAM and scope.user_id == "u_1"
            return SimpleNamespace(path=team, proof=SimpleNamespace(name="report"))

    monkeypatch.setattr(ordinary_lead_material, "_sources", lambda ctx, resource, name: Sources())
    ctx = _ctx(tmp_path)
    assert (await resolve_selected_skill(ctx, "report")).source_dir == team
    assert await resolve_selected_skill(ctx, "other") is None
    outcome["error"] = SkillReadResolutionError("reference_skill_version_changed")
    assert (await resolve_selected_skill(ctx, "report"))["error_kind"] == "selected_skill_changed"


@pytest.mark.parametrize("failures", [1, 2])
async def test_an_access_change_during_resolution_is_checked_again(tmp_path, monkeypatch, failures):
    """A group change by another session mid-check is checked once more, not reported as an edited Skill."""
    team = _skill(tmp_path, "team", "TEAM METHOD")
    calls = []

    class Sources:
        async def prepare_runtime_package(self, resource, *, scope):
            calls.append(resource.id)
            if len(calls) <= failures:
                raise SkillReadResolutionError("reference_skill_source_changed")
            return SimpleNamespace(path=team, proof=SimpleNamespace(name="report"))

    monkeypatch.setattr(ordinary_lead_material, "_sources", lambda ctx, resource, name: Sources())
    result = await resolve_selected_skill(_ctx(tmp_path), "report")
    assert len(calls) == 2
    if failures == 1:
        assert result.source_dir == team
    else:
        assert result["error_kind"] == "selected_skill_changed"


async def test_registered_tools_skip_the_name_cache_and_name_gate_for_a_selected_skill(tmp_path, monkeypatch):
    personal, team = _skill(tmp_path, "personal", "PERSONAL DRAFT"), _skill(tmp_path, "team", "TEAM METHOD")

    async def resolve(ctx, name):
        return load_skill(team) if name == "report" else None

    monkeypatch.setattr(selected_skill_runtime, "resolve_selected_skill", resolve)
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[]), SkillRuntime())
    ctx = _ctx(personal, _ξ_skill_cache={"peer|report|||''": {"body": "FOREIGN CACHED BODY"}})
    result = await registry.get("skill_read").handler({"name": "report"}, ctx)
    assert "TEAM METHOD" in result["body"] and "FOREIGN" not in result["body"]


async def test_grants_of_a_selected_skill_are_keyed_by_its_selected_source(tmp_path, monkeypatch):
    """A same-name personal copy never answers the resource grant check for a selected team Skill."""
    from bowhead_core.resource_access import resolve_skill_grant_target

    from bowhead_core import resource_execution_gate

    personal, team = _skill(tmp_path, "personal", "PERSONAL DRAFT"), _skill(tmp_path, "team", "TEAM METHOD")
    ctx = _ctx(personal, skill_catalog=SkillCatalog(roots=[]))
    # Registry layer: the selected team source and its author.
    assert await resolve_skill_grant_target(ctx, "report", permission="invoke") == (("team_skill", "tskill_1", "invoke"), "u_author")
    ctx.selected_skill_resources = {}
    personal_copy = SimpleNamespace(source_type="user_upload", source_path=str(personal), enabled=True, import_status=None)
    ctx.skill_instance_store.record = personal_copy
    assert await resolve_skill_grant_target(ctx, "report", permission="invoke") == (("skill", "report", "invoke"), None)

    # Dispatch layer: the grant recheck runs on the selected source and its refusal stops the call.
    ctx.selected_skill_resources = {"report": TEAM}
    checked = []

    async def refuse(ctx, skill_name, *, permission, team_skill=None):
        checked.append((skill_name, permission, getattr(team_skill, "id", None)))
        return {"ok": False, "error_kind": "skill_scope_denied", "error": "skill permission was revoked"}

    async def resolve(ctx, name):
        return load_skill(team) if name == "report" else None

    monkeypatch.setattr(resource_execution_gate, "_check_skill_grant", refuse)
    monkeypatch.setattr(selected_skill_runtime, "resolve_selected_skill", resolve)
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[]), SkillRuntime())
    result = await registry.get("skill_read").handler({"name": "report"}, ctx)
    assert result["error_kind"] == "skill_scope_denied" and "TEAM METHOD" not in str(result)
    assert checked == [("report", "read", "tskill_1")]


async def test_earlier_selections_resolve_by_source_id(monkeypatch):
    builtin = SkillResourceRef(kind="skill", source_kind="builtin", id="builtin:pdf", version="sha256:" + "2" * 64)
    user = SkillResourceRef(kind="skill", source_kind="user", id="sk_mine", version="sha256:" + "3" * 64)

    class Store:
        async def list_turns(self, *, session_id):
            return [SimpleNamespace(id="t_1"), SimpleNamespace(id="t_2"), SimpleNamespace(id="t_now")]

        async def get_chat_turn_user_source(self, *, turn_id, user_id, workspace_id):
            return SimpleNamespace(selected_skills={"t_1": (TEAM, builtin), "t_2": (user,)}.get(turn_id, ()))

    class Teams:
        async def list(self, team_id):
            return [SimpleNamespace(id="tskill_1", name="report")]

    class Instances:
        async def get_by_id(self, *, user_id, skill_id):
            return SimpleNamespace(name="mine") if skill_id == "sk_mine" else None

    ctx = SimpleNamespace(
        session_store=Store(), user_id="u_1", workspace_id="ws_1", team_id="team_1", team_skill_store=Teams(), skill_instance_store=Instances()
    )
    found = await history_selected_resources(ctx, session_id="s_1", turn_id="t_now", names=["report", "pdf", "mine", "gone"])
    assert found == {"report": TEAM, "pdf": builtin, "mine": user}
