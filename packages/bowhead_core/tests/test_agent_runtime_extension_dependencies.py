"""Optional policies use exact source/readonly truth without granting baseline tools."""

from types import SimpleNamespace

import pytest
import yaml
from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError
from bowhead_core.skill_upload import bundle_sha256
from test_agent_dependency_compiler import _catalog, _context, _PersonalStore, _TeamStore
from test_agent_runtime_extensions import policy
from test_skill_read_material import _skill

from bowhead_skill import SkillCatalog

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def case(tmp_path, monkeypatch, kind="team", *, pinned=False):
    root = _skill(tmp_path / "dupont")
    revision = "sha256:" + bundle_sha256(root)
    record = SimpleNamespace(
        id="sk_dupont",
        name="dupont",
        source_path=str(root),
        status="enabled",
        approval="approved",
        visibility="team_wide",
        created_by="user_owner",
        approved_sha256=revision,
        enabled=True,
        source_type="user_upload",
        import_status="active",
    )
    value = policy(kind=kind, version=revision if pinned else None)
    value["runtime_extensions"]["skill_read"]["resource_allowlist"][0]["id"] = "builtin:dupont" if kind == "builtin" else record.id
    monkeypatch.setattr("bowhead_core.agent_dependency_compiler.builtin_catalog_dir", lambda: tmp_path)
    compiler = AgentDependencyCompiler(
        builtin_skills=SkillCatalog(roots=[tmp_path]),
        team_skill_store=_TeamStore({record.id: record}),
        personal_skill_store=_PersonalStore({record.id: record}),
    )
    return SimpleNamespace(root=root, record=record, value={"tool_refs": [], "skill_grants": [], **value}, compiler=compiler)


async def compile_policy(case, *, audience="private"):
    return await case.compiler.compile(case.value, context=_context(audience=audience), tool_catalog=_catalog())


@pytest.mark.parametrize("kind", ["builtin", "team", "user"])
async def test_optional_method_changes_fingerprint_without_adding_any_baseline_grant(tmp_path, monkeypatch, kind):
    c = case(tmp_path, monkeypatch, kind)
    ordinary = await c.compiler.compile({"tool_refs": [], "skill_grants": []}, context=_context(), tool_catalog=_catalog())
    result = await compile_policy(c)
    assert result.status == "ready"
    assert result.skills == result.tools == ()
    assert result.skill_scope.read_names == result.skill_scope.invoke_names == ()
    assert result.risk.effect == ordinary.risk.effect
    assert result.dependency_fingerprint != ordinary.dependency_fingerprint
    assert result.capability_fingerprint != ordinary.capability_fingerprint


@pytest.mark.parametrize("pinned", [False, True])
async def test_unpinned_policy_tracks_eligible_current_version_without_changing_its_scope_hash(tmp_path, monkeypatch, pinned):
    c = case(tmp_path, monkeypatch, pinned=pinned)
    initial = await compile_policy(c)
    (c.root / "references/method.md").write_text("Updated profitability method.")
    c.record.approved_sha256 = "sha256:" + bundle_sha256(c.root)
    current = await compile_policy(c)
    if pinned:
        assert current.status == "blocked" and current.reason_codes == ("agent_runtime_extension_source_unavailable",)
    else:
        assert current.status == "ready" and current.dependency_fingerprint == initial.dependency_fingerprint


@pytest.mark.parametrize(
    "change", ["disabled", "pending", "unapproved", "unapproved_bytes", "private_other_owner", "wrong_id", "no_readonly", "missing_text", "new_tools"]
)
async def test_source_lifecycle_and_readonly_contract_are_required(tmp_path, monkeypatch, change):
    c = case(tmp_path, monkeypatch, "user" if change == "pending" else "team")
    if change == "disabled":
        c.record.status = "disabled"
    elif change == "pending":
        c.record.import_status = "pending"
    elif change == "unapproved":
        c.record.approval = "pending"
    elif change == "unapproved_bytes":
        (c.root / "private.txt").write_text("Bundle changed after approval.")
    elif change == "private_other_owner":
        c.record.visibility, c.record.created_by = "private", "another_owner"
    elif change == "wrong_id":
        c.value["runtime_extensions"]["skill_read"]["resource_allowlist"][0]["id"] = "hidden_identity"
    elif change == "missing_text":
        (c.root / "references/method.md").unlink()
        c.record.approved_sha256 = "sha256:" + bundle_sha256(c.root)
    else:
        source = c.root / "SKILL.md"
        front = yaml.safe_load(source.read_text().split("---", 2)[1])
        if change == "no_readonly":
            front.pop("read_only_entry")
        else:
            front["read_only_entry"]["requires_new_tools"] = True
        source.write_text("---\n" + yaml.safe_dump(front) + "---\nInstructions.")
        c.record.approved_sha256 = "sha256:" + bundle_sha256(c.root)
    result = await compile_policy(c)
    assert result.status == "blocked"
    assert "agent_runtime_extension_source_unavailable" in result.reason_codes
    assert result.skills == result.tools == ()
    assert all("hidden_identity" not in reason for reason in result.reason_codes)


async def test_personal_selector_cannot_be_published_to_a_shared_audience(tmp_path, monkeypatch):
    c = case(tmp_path, monkeypatch, "user")
    with pytest.raises(AgentRuntimeExtensionError, match="private_source"):
        await compile_policy(c, audience="teamwide")


async def test_disabled_optional_scope_can_be_saved_after_source_removal(tmp_path, monkeypatch):
    c = case(tmp_path, monkeypatch)
    c.value["runtime_extensions"]["skill_read"]["enabled"] = False
    c.record.status = "disabled"
    assert (await compile_policy(c)).status == "ready"


@pytest.mark.parametrize("pinned", [False, True])
async def test_optional_dependency_projection_is_distinct_from_required_scope(tmp_path, monkeypatch, pinned):
    from bowhead_core.resource_dependency_projection import edges_from_compilation, stable_edges_from_definition

    c = case(tmp_path, monkeypatch, pinned=pinned)
    result = await compile_policy(c)
    context = {
        "team_id": "team_a",
        "subject_kind": "agent_revision",
        "subject_ref": "arv_optional",
        "agent_id": "tsa_agent",
        "subject_fingerprint": "sha256:" + "c" * 64,
    }
    fresh, fresh_edges = edges_from_compilation(**context, compilation=result)
    restored, restored_edges = stable_edges_from_definition(**context, definition=c.value)
    assert fresh.status == restored.status == "complete"
    assert fresh_edges == restored_edges and len(fresh_edges) == 1
    edge = fresh_edges[0]
    assert edge.dependency_role == "optional" and edge.resource_ref == "team:sk_dupont"
    assert edge.resource_revision == (c.record.approved_sha256 if pinned else None)
    assert edge.evidence_level == ("exact" if pinned else "stable_only")
    assert "sk_dupont" not in str(result.dependency_json())
    c.value["runtime_extensions"]["skill_read"]["enabled"] = False
    disabled = await compile_policy(c)
    assert edges_from_compilation(**context, compilation=disabled)[1] == ()
    assert stable_edges_from_definition(**context, definition=c.value)[1] == ()
