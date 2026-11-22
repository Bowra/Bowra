"""A Skill selected beside an Agent's own is read as its whole exact package.

Real admission, claim and exact access; the tools run through the registry.
See docs/sprints/skill-bundle-read-2026-11-20/tech-design.md §10.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_core.resource_access import resolve_skill_grant_target
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from bowhead_core.tool_registry import ToolRegistry
from test_skill_read_access import access_case as _access_case
from test_skill_read_access import receiver
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case

from bowhead_skill import SkillCatalog, SkillRuntime

access_case, claimed_case, receiver_case, start_case = _access_case, _claimed_case, _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize(
        "receiver_case,claimed_case",
        [("admission_extension_unused_invoke", "extension"), ("admission_temporary_unused_invoke", "temporary")],
        indirect=True,
    ),
]


@pytest.fixture
async def extension_case(access_case):
    case = access_case
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[case.case.fixture.root]), SkillRuntime())
    case.registry = registry
    case.ctx.reference_skill_required = True
    case.ctx.reference_skill_access = case.access
    case.ctx.skill_runtime_scope = None
    return case


async def read(case, **arguments):
    with receiver(case.access.subagent_id):
        invocation = await case.registry.execute("skill_read", {"name": "optional_method", **arguments}, case.ctx)
        return invocation.result


async def test_any_package_file_is_read_by_path_and_the_name_read_lists_them(extension_case):
    case = extension_case
    listed = await read(case)
    assert listed["files"][0]["path"] == "method.md" and listed["parameters"] == {"year": 2026}
    assert {entry["path"] for entry in listed["package_files"]} == {"SKILL.md", "method.md", "scripts/runner.py"}

    manifest = await read(case, file="SKILL.md")
    assert manifest["file"] == "SKILL.md" and "NOT_SELECTED_MANIFEST" in manifest["body"]
    assert manifest["resource"] == case.case.fixture.optional.model_dump(mode="json")
    script = await read(case, ref="scripts/runner.py")
    assert script["file"] == "scripts/runner.py" and "fixture script must never run" in script["body"]

    # A retained entry file keeps the parameters the user selected.
    entry = await read(case, file="method.md")
    assert [item["path"] for item in entry["files"]] == ["method.md"] and entry["parameters"] == {"year": 2026}

    miss = await read(case, file="references/absent.md")
    assert miss["error_kind"] == "skill_file_not_found" and miss["requested"] == "references/absent.md"
    assert {item["path"] for item in miss["package_files"]} == {"SKILL.md", "method.md", "scripts/runner.py"}


async def test_an_edited_package_refuses_unretained_files_with_a_correction(extension_case):
    case = extension_case
    (case.case.fixture.root / "optional_method" / "SKILL.md").write_text("---\nname: optional_method\ndescription: x\n---\nEDITED")
    changed = await read(case, file="SKILL.md")
    assert changed["error_kind"] == "selected_skill_changed" and changed["retained_files"] == ["method.md"]
    assert "EDITED" not in json.dumps(changed)
    # What was retained at the selection stays readable, without a listing of the edited package.
    listed = await read(case)
    assert listed["files"][0]["text"] == "BODY_optional_method" and "package_files" not in listed
    assert (await read(case, file="method.md"))["files"][0]["text"] == "BODY_optional_method"


async def test_fixed_parameters_and_other_skills_stay_refused(extension_case):
    case = extension_case
    assert (await read(case, args="2030"))["error_kind"] == "reference_skill_unavailable"
    with receiver(case.access.subagent_id):
        other = await case.registry.execute("skill_read", {"name": "not_selected", "file": "SKILL.md"}, case.ctx)
    assert other.result["error_kind"] == "reference_skill_unavailable"


async def test_the_authorization_pass_captures_no_package(extension_case, monkeypatch):
    case = extension_case
    captures = []
    original = SkillReadSourceResolver.prepare_runtime_package

    async def counted(self, resource, *, scope):
        captures.append(resource)
        return await original(self, resource, scope=scope)

    monkeypatch.setattr(SkillReadSourceResolver, "prepare_runtime_package", counted)
    with receiver(case.access.subagent_id):
        await case.access.authorize_tool("skill_read", {"name": "optional_method", "file": "SKILL.md"}, case.ctx)
    assert captures == []
    assert (await read(case, file="SKILL.md"))["file"] == "SKILL.md"
    assert captures == [case.case.fixture.optional]


async def test_the_grant_check_keys_an_extension_read_by_its_exact_source(extension_case):
    case = extension_case
    ctx = SimpleNamespace(skill_runtime_grants=(), reference_skill_access=case.access)
    assert await resolve_skill_grant_target(ctx, "optional_method", permission="read") == (("skill", "optional_method", "read"), None)
    with pytest.raises(ValueError, match="assigned_skill_unavailable"):
        await resolve_skill_grant_target(ctx, "not_selected", permission="read")
