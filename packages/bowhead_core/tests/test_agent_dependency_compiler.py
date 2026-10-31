from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.agent_dependency_compiler import (
    AgentDependencyCompiler,
    AgentDependencyContext,
    ExactSkillRuntimeScope,
    compare_agent_dependencies,
)
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.subagent_authoring_catalog import (
    AuthoringTool,
    SubagentAuthoringCatalogSnapshot,
)


def _tool(
    name: str,
    *,
    risk: str = "read",
    source_kind: str = "native",
    selectable: bool = True,
    approval_required: bool = False,
    operation_id: str | None = None,
    method: str | None = None,
) -> AuthoringTool:
    return AuthoringTool(
        stable_ref=name,
        display_name=name,
        description=name,
        source_kind=source_kind,  # type: ignore[arg-type]
        source_display_name="Bowhead" if source_kind == "native" else "CRM",
        capability_classes=frozenset(
            {"data_query"} if name.startswith("bi_") else (),
        ),
        risk=risk,  # type: ignore[arg-type]
        discoverable=True,
        selectable=selectable,
        locked_reason=None if selectable else "risk_metadata_unknown",
        risk_source=("native_registry" if source_kind == "native" else "openapi_method"),
        approval_required=approval_required,
        confirmation_summary=None,
        dependency_revision="native-v1" if source_kind == "native" else "2026-09-03T00:00:00+00:00",
        source_id="src_crm" if source_kind == "team_mcp" else None,
        operation_id=operation_id,
        method=method,
    )


def _catalog(*tools: AuthoringTool) -> SubagentAuthoringCatalogSnapshot:
    native = tuple(tool for tool in tools if tool.source_kind == "native")
    team = tuple(tool for tool in tools if tool.source_kind == "team_mcp")
    return SubagentAuthoringCatalogSnapshot(
        team_id="team_a",
        actor_user_id="user_owner",
        native_tools=native,
        team_tools=team,
        fingerprint="sha256:" + "1" * 64,
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )


def _context(
    *,
    agent_id: str = "tsa_agent",
    audience: str = "private",
    execution: str = "interactive",
    coverage: frozenset[str] = frozenset(),
) -> AgentDependencyContext:
    return AgentDependencyContext(
        agent_id=agent_id,
        team_id="team_a",
        owner_user_id="user_owner",
        audience_mode=audience,  # type: ignore[arg-type]
        execution_mode=execution,  # type: ignore[arg-type]
        reference_coverage=coverage,
    )


def _write_skill(
    root: Path,
    *,
    name: str,
    kind: str = "tool",
    with_script: bool = True,
) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill\nkind: {kind}\n---\n\n# Test\n",
        encoding="utf-8",
    )
    if with_script:
        scripts = path / "scripts"
        scripts.mkdir()
        (scripts / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return path


def _revision(path: Path) -> str:
    return f"sha256:{bundle_sha256(path)}"


def _grant(
    *,
    kind: str,
    skill_id: str,
    name: str,
    revision: str,
    permissions: list[str] | None = None,
    distribution: str = "reference",
) -> dict[str, object]:
    return {
        "skill_ref": {"kind": kind, "id": skill_id, "name": name},
        "permissions": permissions or ["read", "invoke"],
        "revision": revision,
        "distribution": distribution,
    }


class _PersonalStore:
    def __init__(self, records: dict[str, object]) -> None:
        self.records = records

    async def get_by_id(self, *, user_id: str, skill_id: str) -> object | None:
        assert user_id == "user_owner"
        return self.records.get(skill_id)


class _TeamStore:
    def __init__(self, records: dict[str, object]) -> None:
        self.records = records

    async def get_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        only_enabled: bool = False,
    ) -> object | None:
        assert team_id == "team_a"
        assert only_enabled is False
        return self.records.get(skill_id)

    async def get_team_skill_by_name(
        self,
        team_id: str,
        name: str,
        *,
        only_enabled: bool = True,
        only_approved: bool = True,
    ) -> object | None:
        assert team_id == "team_a"
        return next((record for record in self.records.values() if record.name == name), None)


def _team_record(path: Path, *, skill_id: str = "oskill_1", approved: str | None = None, report: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=skill_id,
        name=path.name,
        status="enabled",
        approval="approved",
        source_path=str(path),
        approved_sha256=(approved or _revision(path)).removeprefix("sha256:"),
        visibility="team_wide",
        created_by="user_owner",
        risk_report_json=report,
    )


_EMPTY_REPORT = json.dumps({"langs": {".py": 1}, "keywords": [], "shebangs": []})


@pytest.mark.anyio
async def test_explicit_python_selection_compiles_to_exact_executable_skill() -> None:
    compiler = AgentDependencyCompiler()
    grants = await compiler.exact_grants_for_legacy_names((), team_id="team_a", tool_refs=("python_sandbox",))
    assert len(grants) == 1
    assert grants[0].id == "builtin:python_sandbox"
    assert set(grants[0].permissions) == {"read", "invoke"}
    assert grants[0].revision.startswith("sha256:")
    result = await compiler.compile(
        {"tool_refs": ["python_sandbox"], "skill_grants": [grant.to_canonical_dict() for grant in grants]},
        context=_context(),
        tool_catalog=_catalog(_tool("python_sandbox", risk="compute"), _tool("skill_read"), _tool("skill_invoke", risk="compute")),
    )
    assert result.status == "ready", result.reason_codes
    assert result.skill_scope.invoke_names == ("python_sandbox",)
    assert await compiler.exact_grants_for_legacy_names((), team_id="team_a", tool_refs=("skill_invoke",)) == ()


@pytest.mark.anyio
async def test_personal_skill_authoring_grant_is_exact_and_owner_bound(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-private-method", with_script=False)
    record = SimpleNamespace(
        id="sk_private_1",
        name="my-private-method",
        enabled=True,
        source_type="user_generated",
        source_path=str(path),
    )
    compiler = AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    )

    grant = await compiler.exact_grant_for_personal_skill(
        owner_user_id="user_owner",
        skill_id=record.id,
    )

    assert grant.kind == "user"
    assert grant.id == record.id
    assert grant.name == record.name
    assert grant.permissions == ("read",)
    assert grant.revision == _revision(path)
    assert "source_path" not in grant.to_canonical_dict()


@pytest.mark.anyio
async def test_converters_grant_run_permission_to_runnable_skills(tmp_path: Path) -> None:
    runnable = _write_skill(tmp_path / "team", name="team-runner")
    knowledge = _write_skill(tmp_path / "team", name="team-notes", kind="knowledge")
    plain = _write_skill(tmp_path / "team", name="team-plain", with_script=False)
    js_only = _write_skill(tmp_path / "team", name="team-js", with_script=False)
    (js_only / "scripts").mkdir()
    (js_only / "scripts" / "run.js").write_text("console.log('ok')\n", encoding="utf-8")
    records = {f"oskill_{index}": _team_record(path, skill_id=f"oskill_{index}") for index, path in enumerate((runnable, knowledge, plain, js_only))}
    personal_path = _write_skill(tmp_path / "user", name="my-runner")
    personal = SimpleNamespace(id="sk_1", name="my-runner", enabled=True, source_type="user_upload", source_path=str(personal_path))
    compiler = AgentDependencyCompiler(
        team_skill_store=_TeamStore(records),
        personal_skill_store=_PersonalStore({personal.id: personal}),
    )

    grants = await compiler.exact_grants_for_legacy_names(
        ("team-runner", "team-notes", "team-plain", "team-js", "pptx", "doc-coauthoring"), team_id="team_a"
    )
    personal_grant = await compiler.exact_grant_for_personal_skill(owner_user_id="user_owner", skill_id=personal.id)

    permissions = {grant.name: grant.permissions for grant in grants}
    assert permissions == {
        "team-runner": ("invoke", "read"),
        "team-notes": ("read",),
        "team-plain": ("read",),
        # Run permission does not depend on the deployment's multi-runtime switch.
        "team-js": ("invoke", "read"),
        "pptx": ("invoke", "read"),
        "doc-coauthoring": ("read",),
    }
    assert personal_grant.permissions == ("invoke", "read")
    # python_sandbox keeps its own rule: invoke only with the Python sandbox Tool.
    (sandbox_read,) = await compiler.exact_grants_for_legacy_names(("python_sandbox",), team_id="team_a")
    assert sandbox_read.permissions == ("read",)


@pytest.mark.anyio
async def test_blocked_tool_is_listed_with_its_reason() -> None:
    compiled = await AgentDependencyCompiler().compile(
        {"tool_refs": ["bi_query", "teammcp.src_crm.gone"]},
        context=_context(),
        tool_catalog=_catalog(_tool("bi_query")),
    )

    assert compiled.status == "blocked"
    assert compiled.blocked_items == (
        {"kind": "tool", "name": "teammcp.src_crm.gone", "identity": "teammcp.src_crm.gone", "reason_codes": ["tool_unavailable"]},
    )


@pytest.mark.anyio
async def test_personal_skill_authoring_grant_rejects_disabled_or_non_personal_record(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-private-method", with_script=False)
    for source_type, enabled in (("user_generated", False), ("builtin", True)):
        record = SimpleNamespace(
            id="sk_private_1",
            name="my-private-method",
            enabled=enabled,
            source_type=source_type,
            source_path=str(path),
        )
        compiler = AgentDependencyCompiler(
            personal_skill_store=_PersonalStore({record.id: record}),
        )

        with pytest.raises(ValueError, match="unavailable"):
            await compiler.exact_grant_for_personal_skill(
                owner_user_id="user_owner",
                skill_id=record.id,
            )


@pytest.mark.anyio
async def test_known_native_dependencies_compile_deterministically() -> None:
    compiler = AgentDependencyCompiler()
    catalog = _catalog(_tool("bi_query"))
    definition_a = {"tool_refs": ["bi_query"], "model": "analyst"}
    definition_b = {"model": "analyst", "tool_refs": ["bi_query"]}

    first = await compiler.compile(
        definition_a,
        context=_context(),
        tool_catalog=catalog,
    )
    second = await compiler.compile(
        definition_b,
        context=_context(),
        tool_catalog=catalog,
    )

    assert first.status == "ready"
    assert first.risk.level == "low"
    assert first.risk.data == "internal_authorized"
    assert first.dependency_fingerprint == second.dependency_fingerprint
    assert first.capability_fingerprint == second.capability_fingerprint
    assert first.dependency_json()["tools"][0]["stable_ref"] == "bi_query"


@pytest.mark.anyio
async def test_tool_catalog_version_mismatch_is_rejected() -> None:
    catalog = replace(_catalog(_tool("bi_query")), risk_registry_version="future-v2")

    with pytest.raises(ValueError, match="catalog version"):
        await AgentDependencyCompiler().compile(
            {"tool_refs": ["bi_query"]},
            context=_context(),
            tool_catalog=catalog,
        )


@pytest.mark.anyio
async def test_known_external_write_is_reviewable_not_unknown() -> None:
    tool = _tool(
        "teammcp.src_crm.updateDeal",
        risk="external_write",
        source_kind="team_mcp",
        approval_required=True,
        operation_id="updateDeal",
        method="PATCH",
    )
    compiled = await AgentDependencyCompiler().compile(
        {"tool_refs": [tool.stable_ref]},
        context=_context(audience="teamwide"),
        tool_catalog=_catalog(tool),
    )

    assert compiled.status == "ready"
    assert compiled.publishable is True
    assert compiled.risk.level == "high"
    assert compiled.risk.approval_required is True
    assert compiled.risk.runtime_confirmation_required is True
    assert compiled.tools[0].connection_ref == "team_tool_source:src_crm"


@pytest.mark.anyio
async def test_private_external_write_skips_publish_approval_not_runtime_confirmation() -> None:
    tool = _tool(
        "teammcp.src_crm.updateDeal",
        risk="external_write",
        source_kind="team_mcp",
        approval_required=True,
        operation_id="updateDeal",
        method="PATCH",
    )
    compiled = await AgentDependencyCompiler().compile(
        {"tool_refs": [tool.stable_ref]},
        context=_context(),
        tool_catalog=_catalog(tool),
    )

    assert compiled.status == "ready"
    assert compiled.risk.level == "high"
    assert compiled.risk.approval_required is False
    assert compiled.risk.runtime_confirmation_required is True


@pytest.mark.anyio
async def test_stored_skill_wrapper_tool_refs_are_ignored_with_empty_grants() -> None:
    compiled = await AgentDependencyCompiler().compile(
        {"tool_refs": ["bi_query", "skill_invoke", "skill_list", "skill_read"], "skill_grants": []},
        context=_context(audience="teamwide"),
        tool_catalog=_catalog(_tool("bi_query"), _tool("skill_invoke"), _tool("skill_list"), _tool("skill_read")),
    )

    assert compiled.status == "ready", compiled.reason_codes
    assert "precise_skill_scope_required" not in compiled.reason_codes
    assert [tool.stable_ref for tool in compiled.tools] == ["bi_query"]
    assert compiled.blocked_items == ()


@pytest.mark.anyio
@pytest.mark.parametrize("definition", [{}, {"skills": []}], ids=["no-skill-keys", "empty-legacy-skills"])
async def test_stored_skill_wrapper_tool_refs_without_skill_grants_key_are_blocked(definition) -> None:
    # Without a skill_grants key the runtime has no exact Skill scope, so the
    # stored wrappers would run unscoped.
    compiled = await AgentDependencyCompiler().compile(
        {**definition, "tool_refs": ["bi_query", "skill_invoke", "skill_list"]},
        context=_context(audience="teamwide"),
        tool_catalog=_catalog(_tool("bi_query"), _tool("skill_invoke"), _tool("skill_list"), _tool("skill_read")),
    )

    assert compiled.status == "blocked"
    assert "precise_skill_scope_required" in compiled.reason_codes
    assert [tool.stable_ref for tool in compiled.tools] == ["bi_query"]
    assert compiled.blocked_items == (
        {
            "kind": "definition",
            "name": "skill_grants",
            "identity": "skill_grants",
            "reason_codes": ["precise_skill_scope_required"],
        },
    )


@pytest.mark.anyio
async def test_stored_skill_list_is_ignored_and_grants_decide_the_wrappers() -> None:
    from bowhead_skill.catalog import builtin_catalog_dir

    path = builtin_catalog_dir() / "python_sandbox"
    compiled = await AgentDependencyCompiler().compile(
        {
            "tool_refs": ["skill_list"],
            "skill_grants": [
                _grant(
                    kind="builtin",
                    skill_id="builtin:python_sandbox",
                    name="python_sandbox",
                    revision=_revision(path),
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(
            _tool("skill_list"),
            _tool("skill_read"),
            _tool("skill_invoke"),
        ),
    )

    assert compiled.status == "ready", compiled.reason_codes
    assert {tool.stable_ref for tool in compiled.tools} == {"skill_invoke", "skill_read"}


@pytest.mark.anyio
async def test_exact_builtin_skill_emits_same_publish_and_runtime_scope() -> None:
    from bowhead_skill.catalog import builtin_catalog_dir

    path = builtin_catalog_dir() / "python_sandbox"
    grant = _grant(
        kind="builtin",
        skill_id="builtin:python_sandbox",
        name="python_sandbox",
        revision=_revision(path),
    )
    compiled = await AgentDependencyCompiler().compile(
        {"tool_refs": [], "skill_grants": [grant]},
        context=_context(audience="teamwide"),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "ready"
    assert compiled.risk.effect == "compute"
    assert compiled.skill_scope.read_names == ("python_sandbox",)
    assert compiled.skill_scope.invoke_names == ("python_sandbox",)
    assert {tool.stable_ref for tool in compiled.tools} == {
        "skill_invoke",
        "skill_read",
    }


@pytest.mark.anyio
async def test_builtin_skill_revision_drift_is_rebased_to_the_current_bundle() -> None:
    from bowhead_core.agent_dependency_compiler import builtin_skill_bundle_revision

    from bowhead_skill.catalog import builtin_catalog_dir

    current = builtin_skill_bundle_revision(str(builtin_catalog_dir() / "python_sandbox"))
    assert current is not None
    stale = _grant(kind="builtin", skill_id="builtin:python_sandbox", name="python_sandbox", revision="sha256:" + "0" * 64)
    catalog = _catalog(_tool("skill_read"), _tool("skill_invoke"))
    compiled = await AgentDependencyCompiler().compile({"skill_grants": [stale]}, context=_context(), tool_catalog=catalog)
    exact = await AgentDependencyCompiler().compile({"skill_grants": [{**stale, "revision": current}]}, context=_context(), tool_catalog=catalog)

    assert compiled.status == "ready", compiled.reason_codes
    assert compiled.skills[0].grant.revision == current
    assert compiled.skill_scope.invoke_names == ("python_sandbox",)
    assert compiled.dependency_fingerprint == exact.dependency_fingerprint
    assert compiled.rebased_skill_grants({"skill_grants": [stale]}) == [{**stale, "revision": current}]
    assert exact.rebased_skill_grants({"skill_grants": [{**stale, "revision": current}]}) is None


@pytest.mark.anyio
async def test_team_skill_follows_the_latest_approved_revision(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, name="team-method")
    released = _revision(path)
    (path / "scripts" / "run.py").write_text("print('v2')\n", encoding="utf-8")
    record = _team_record(path, report=_EMPTY_REPORT)
    compiled = await AgentDependencyCompiler(team_skill_store=_TeamStore({record.id: record})).compile(
        {"skill_grants": [_grant(kind="team", skill_id=record.id, name=record.name, revision=released)]},
        context=_context(audience="teamwide"),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "ready", compiled.reason_codes
    assert compiled.skills[0].grant.revision == _revision(path) != released


@pytest.mark.anyio
async def test_edited_team_skill_waiting_for_reapproval_is_blocked_and_named(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, name="team-method")
    approved = _revision(path)
    (path / "scripts" / "run.py").write_text("print('edited')\n", encoding="utf-8")
    record = _team_record(path, approved=approved, report=_EMPTY_REPORT)
    compiled = await AgentDependencyCompiler(team_skill_store=_TeamStore({record.id: record})).compile(
        {"skill_grants": [_grant(kind="team", skill_id=record.id, name=record.name, revision=approved)]},
        context=_context(audience="teamwide"),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "blocked"
    assert "skill_pending_reapproval" in compiled.reason_codes
    assert "skill_revision_changed" not in compiled.reason_codes
    assert compiled.blocked_items == (
        {"kind": "skill", "name": "team-method", "identity": "team:oskill_1", "reason_codes": ["skill_pending_reapproval"]},
    )


@pytest.mark.anyio
async def test_skill_revision_is_not_part_of_the_dependency_fingerprint(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, name="my-method")
    record = SimpleNamespace(id="sk_1", name="my-method", enabled=True, source_path=str(path), risk_report_json=_EMPTY_REPORT)
    compiler = AgentDependencyCompiler(personal_skill_store=_PersonalStore({record.id: record}))
    catalog = _catalog(_tool("skill_read"), _tool("skill_invoke"))
    definition = {"skill_grants": [_grant(kind="user", skill_id=record.id, name=record.name, revision=_revision(path))]}
    before = await compiler.compile(definition, context=_context(), tool_catalog=catalog)
    (path / "references.md").write_text("More detail\n", encoding="utf-8")
    after = await compiler.compile(definition, context=_context(), tool_catalog=catalog)
    read_only = await compiler.compile(
        {"skill_grants": [{**definition["skill_grants"][0], "permissions": ["read"]}]}, context=_context(), tool_catalog=catalog
    )

    assert before.status == after.status == "ready"
    assert before.skills[0].grant.revision != after.skills[0].grant.revision
    assert before.dependency_fingerprint == after.dependency_fingerprint
    # Permissions stay in the fingerprint.
    assert read_only.dependency_fingerprint != before.dependency_fingerprint
    assert after.dependency_json()["skills"][0]["revision"] == _revision(path)


@pytest.mark.anyio
async def test_team_skill_requires_approved_exact_bundle_and_audience_coverage(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="team-method")
    revision = _revision(path)
    record = SimpleNamespace(
        id="oskill_1",
        name="team-method",
        status="enabled",
        approval="approved",
        source_path=str(path),
        approved_sha256=revision.removeprefix("sha256:"),
        visibility="creator_only",
        created_by="user_owner",
        risk_report_json=json.dumps(
            {"langs": {".py": 1}, "keywords": [], "shebangs": []},
        ),
    )
    compiler = AgentDependencyCompiler(team_skill_store=_TeamStore({record.id: record}))
    grant = _grant(
        kind="team",
        skill_id=record.id,
        name=record.name,
        revision=revision,
    )
    without_coverage = await compiler.compile(
        {"skill_grants": [grant]},
        context=_context(audience="custom"),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )
    with_coverage = await compiler.compile(
        {"skill_grants": [grant]},
        context=_context(
            audience="custom",
            coverage=frozenset({f"team:{record.id}@{revision}"}),
        ),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert without_coverage.status == "blocked"
    assert "skill_reference_audience_not_covered" in without_coverage.reason_codes
    assert with_coverage.status == "ready"
    assert with_coverage.risk.level == "medium"


@pytest.mark.anyio
async def test_personal_skill_cannot_be_silently_carried_to_shared_audience(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=json.dumps(
            {"langs": {".py": 1}, "keywords": [], "shebangs": []},
        ),
    )
    compiler = AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    )
    grant = _grant(
        kind="user",
        skill_id=record.id,
        name=record.name,
        revision=_revision(path),
    )
    private = await compiler.compile(
        {"skill_grants": [grant]},
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )
    shared = await compiler.compile(
        {"skill_grants": [grant]},
        context=_context(
            audience="teamwide",
            coverage=frozenset({f"user:{record.id}@{_revision(path)}"}),
        ),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert private.status == "ready"
    assert shared.status == "blocked"
    assert "skill_reference_audience_not_covered" in shared.reason_codes


@pytest.mark.anyio
async def test_distinct_skill_ids_with_one_runtime_name_return_blocked_not_error(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    revision = _revision(path)
    risk_report = json.dumps(
        {"langs": {".py": 1}, "keywords": [], "shebangs": []},
    )
    personal = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=risk_report,
    )
    team = SimpleNamespace(
        id="oskill_1",
        name="my-method",
        status="enabled",
        approval="approved",
        source_path=str(path),
        approved_sha256=None,
        visibility="creator_only",
        created_by="user_owner",
        risk_report_json=risk_report,
    )
    compiled = await AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({personal.id: personal}),
        team_skill_store=_TeamStore({team.id: team}),
    ).compile(
        {
            "skill_grants": [
                _grant(
                    kind="user",
                    skill_id=personal.id,
                    name=personal.name,
                    revision=revision,
                ),
                _grant(
                    kind="team",
                    skill_id=team.id,
                    name=team.name,
                    revision=revision,
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "blocked"
    assert "skill_runtime_name_conflict" in compiled.reason_codes
    assert compiled.skill_scope.read_names == ("my-method",)
    assert compiled.skill_scope.invoke_names == ("my-method",)


@pytest.mark.anyio
async def test_invoke_permission_requires_read_permission() -> None:
    from bowhead_skill.catalog import builtin_catalog_dir

    path = builtin_catalog_dir() / "python_sandbox"
    compiled = await AgentDependencyCompiler().compile(
        {
            "skill_grants": [
                _grant(
                    kind="builtin",
                    skill_id="builtin:python_sandbox",
                    name="python_sandbox",
                    revision=_revision(path),
                    permissions=["invoke"],
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_invoke")),
    )

    assert compiled.status == "blocked"
    assert "skill_invoke_requires_read_permission" in compiled.reason_codes


@pytest.mark.anyio
async def test_missing_risk_report_is_computed_from_the_bundle(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    (path / "SKILL.md").write_text(
        "---\nname: my-method\ndescription: Test skill\nkind: tool\n---\n\nRun `curl https://example.com` first.\n",
        encoding="utf-8",
    )
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=None,
    )
    compiler = AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    )
    compiled = await compiler.compile(
        {
            "skill_grants": [
                _grant(
                    kind="user",
                    skill_id=record.id,
                    name=record.name,
                    revision=_revision(path),
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "ready", compiled.reason_codes
    assert "skill_risk_report_missing" not in compiled.reason_codes
    assert compiled.skills[0].risk_evidence_digest is not None
    assert "skill_static_risk_signal" in compiled.risk.reasons
    assert compiled.risk.level == "medium"


@pytest.mark.anyio
async def test_computed_risk_report_counts_every_script_language_with_the_switch_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.skill_upload import build_bundle_risk_report

    monkeypatch.delenv("BOWHEAD_SKILL_MULTIRUNTIME", raising=False)
    path = _write_skill(tmp_path, name="my-method")
    (path / "scripts" / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")

    report = build_bundle_risk_report(path)

    assert report["langs"] == {".py": 1, ".sh": 1}


@pytest.mark.anyio
async def test_private_custom_skill_static_risk_signal_is_a_notice_not_a_review(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=json.dumps(
            {"langs": {".py": 1}, "keywords": ["curl "], "shebangs": []},
        ),
    )
    compiler = AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    )
    compiled = await compiler.compile(
        {
            "skill_grants": [
                _grant(
                    kind="user",
                    skill_id=record.id,
                    name=record.name,
                    revision=_revision(path),
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "ready"
    assert compiled.risk.level == "medium"
    assert compiled.risk.approval_required is False
    assert compiled.risk.runtime_confirmation_required is False
    assert "skill_static_risk_signal" in compiled.risk.reasons
    assert compiled.skills[0].review_required is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("report", "status", "approval_required"),
    [
        ({"langs": {".py": 1}, "keywords": ["curl "], "shebangs": []}, "ready", False),
        (
            {
                "langs": {".py": 1},
                "keywords": [],
                "shebangs": [],
                "vendored_node_modules": {"present": True, "file_count": 3, "shadows": ["xlsx"]},
            },
            "review_required",
            True,
        ),
    ],
)
async def test_shared_custom_skill_only_vendored_dependency_requires_publish_approval(
    tmp_path: Path,
    report: dict[str, object],
    status: str,
    approval_required: bool,
) -> None:
    path = _write_skill(tmp_path, name="team-method")
    revision = _revision(path)
    record = SimpleNamespace(
        id="oskill_1",
        name="team-method",
        status="enabled",
        approval="approved",
        source_path=str(path),
        approved_sha256=None,
        visibility="creator_only",
        created_by="user_owner",
        risk_report_json=json.dumps(report),
    )
    grant = _grant(
        kind="team",
        skill_id=record.id,
        name=record.name,
        revision=revision,
    )
    compiled = await AgentDependencyCompiler(
        team_skill_store=_TeamStore({record.id: record}),
    ).compile(
        {"skill_grants": [grant]},
        context=_context(
            audience="custom",
            coverage=frozenset({f"team:{record.id}@{revision}"}),
        ),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == status
    assert compiled.risk.level == ("high" if approval_required else "medium")
    assert compiled.risk.approval_required is approval_required
    assert compiled.risk.runtime_confirmation_required is False


@pytest.mark.anyio
async def test_custom_skill_risk_evidence_is_part_of_dependency_fingerprint(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=json.dumps(
            {"langs": {".py": 1}, "keywords": [], "shebangs": []},
        ),
    )
    compiler = AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    )
    definition = {
        "skill_grants": [
            _grant(
                kind="user",
                skill_id=record.id,
                name=record.name,
                revision=_revision(path),
            ),
        ],
    }
    first = await compiler.compile(
        definition,
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )
    record.risk_report_json = json.dumps(
        {
            "langs": {".py": 1},
            "keywords": [],
            "shebangs": ["run.py: #!/usr/bin/env python3"],
        },
    )
    second = await compiler.compile(
        definition,
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert first.status == second.status == "ready"
    assert first.dependency_fingerprint != second.dependency_fingerprint
    assert first.skills[0].risk_evidence_digest != second.skills[0].risk_evidence_digest


@pytest.mark.anyio
async def test_unknown_future_skill_risk_field_fails_closed(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=json.dumps(
            {
                "langs": {".py": 1},
                "keywords": [],
                "shebangs": [],
                "network_access": True,
            },
        ),
    )
    compiled = await AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    ).compile(
        {
            "skill_grants": [
                _grant(
                    kind="user",
                    skill_id=record.id,
                    name=record.name,
                    revision=_revision(path),
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "blocked"
    assert "skill_risk_report_invalid" in compiled.reason_codes


@pytest.mark.anyio
async def test_skill_bundle_symlink_is_not_hashed_or_published(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (path / "references").mkdir()
    (path / "references" / "outside.txt").symlink_to(outside)
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=json.dumps(
            {"langs": {".py": 1}, "keywords": [], "shebangs": []},
        ),
    )
    compiled = await AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    ).compile(
        {
            "skill_grants": [
                _grant(
                    kind="user",
                    skill_id=record.id,
                    name=record.name,
                    revision="sha256:" + "0" * 64,
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "blocked"
    assert "skill_bundle_unavailable" in compiled.reason_codes


@pytest.mark.anyio
async def test_malformed_legacy_tool_json_is_not_treated_as_empty_definition() -> None:
    compiled = await AgentDependencyCompiler().compile(
        {"tool_refs_json": "not-json"},
        context=_context(),
        tool_catalog=_catalog(),
    )

    assert compiled.status == "blocked"
    assert compiled.risk.level == "unknown"
    assert "dependency_definition_malformed" in compiled.reason_codes
    assert "not-json" not in json.dumps(compiled.dependency_json())


@pytest.mark.anyio
async def test_embedded_scope_stays_blocked_until_snapshot_safety_exists() -> None:
    from bowhead_skill.catalog import builtin_catalog_dir

    path = builtin_catalog_dir() / "python_sandbox"
    compiled = await AgentDependencyCompiler().compile(
        {
            "skill_grants": [
                _grant(
                    kind="builtin",
                    skill_id="builtin:python_sandbox",
                    name="python_sandbox",
                    revision=_revision(path),
                    distribution="embedded_agent_scope",
                ),
            ],
        },
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )

    assert compiled.status == "blocked"
    assert "embedded_skill_scope_not_supported" in compiled.reason_codes


@pytest.mark.anyio
async def test_legacy_skill_names_are_not_publishable_as_new_revision() -> None:
    compiled = await AgentDependencyCompiler().compile(
        {"skills_json": '["python_sandbox"]'},
        context=_context(),
        tool_catalog=_catalog(),
    )

    assert compiled.status == "blocked"
    assert "legacy_skill_scope_not_publishable" in compiled.reason_codes
    assert "precise_skill_scope_required" in compiled.reason_codes
    assert "python_sandbox" not in json.dumps(compiled.dependency_json())


def test_exact_skill_scope_checks_read_and_invoke_without_echoing_names() -> None:
    scope = ExactSkillRuntimeScope(
        read_names=("method-a", "runner-a"),
        invoke_names=("runner-a",),
    )

    assert (
        scope.error_for_call(
            tool_name="skill_read",
            arguments={"name": "method-a"},
        )
        is None
    )
    assert (
        scope.error_for_call(
            tool_name="skill_invoke",
            arguments={"raw_arguments": '{"name":"runner-a"}'},
        )
        is None
    )
    assert (
        scope.error_for_call(
            tool_name="skill_invoke",
            arguments={"name": "method-a"},
        )
        == "skill_scope_denied"
    )
    assert (
        scope.error_for_call(
            tool_name="skill_invoke",
            arguments={"name": "secret-user-name"},
        )
        == "skill_scope_denied"
    )


@pytest.mark.anyio
async def test_dependency_diff_marks_added_external_write_as_high_risk() -> None:
    compiler = AgentDependencyCompiler()
    read = _tool("bi_query")
    write = _tool(
        "teammcp.src_crm.updateDeal",
        risk="external_write",
        source_kind="team_mcp",
        approval_required=True,
    )
    catalog = _catalog(read, write)
    base_context = _context()
    candidate_context = _context(audience="teamwide", execution="scheduled")
    base = await compiler.compile(
        {"tool_refs": [read.stable_ref]},
        context=base_context,
        tool_catalog=catalog,
    )
    candidate = await compiler.compile(
        {"tool_refs": [read.stable_ref, write.stable_ref]},
        context=candidate_context,
        tool_catalog=catalog,
    )

    diff = compare_agent_dependencies(
        base,
        candidate,
        base_context=base_context,
        candidate_context=candidate_context,
    )

    assert diff.added_tools == (write.stable_ref,)
    assert diff.audience_expanded is True
    assert diff.automation_expanded is True
    assert diff.risk_increased is True
    assert diff.high_risk_change is True
    assert diff.review_required is True


@pytest.mark.anyio
async def test_dependency_diff_does_not_treat_low_risk_audience_expansion_as_high() -> None:
    compiler = AgentDependencyCompiler()
    catalog = _catalog(_tool("bi_query"))
    base_context = _context()
    candidate_context = _context(audience="custom")
    base = await compiler.compile(
        {"tool_refs": ["bi_query"]},
        context=base_context,
        tool_catalog=catalog,
    )
    candidate = await compiler.compile(
        {"tool_refs": ["bi_query"]},
        context=candidate_context,
        tool_catalog=catalog,
    )

    diff = compare_agent_dependencies(
        base,
        candidate,
        base_context=base_context,
        candidate_context=candidate_context,
    )

    assert diff.audience_expanded is True
    assert diff.risk_increased is False
    assert diff.high_risk_change is False
    assert diff.review_required is False


@pytest.mark.anyio
async def test_dependency_diff_rejects_cross_agent_comparison() -> None:
    compiler = AgentDependencyCompiler()
    catalog = _catalog(_tool("bi_query"))
    first_context = _context(agent_id="tsa_first")
    second_context = _context(agent_id="tsa_second")
    first = await compiler.compile(
        {"tool_refs": ["bi_query"]},
        context=first_context,
        tool_catalog=catalog,
    )
    second = await compiler.compile(
        {"tool_refs": ["bi_query"]},
        context=second_context,
        tool_catalog=catalog,
    )

    with pytest.raises(ValueError, match="one Agent"):
        compare_agent_dependencies(
            first,
            second,
            base_context=first_context,
            candidate_context=second_context,
        )


@pytest.mark.anyio
async def test_dependency_diff_keeps_new_risky_skill_high_when_base_is_already_high(
    tmp_path: Path,
) -> None:
    path = _write_skill(tmp_path, name="my-method")
    record = SimpleNamespace(
        id="sk_1",
        name="my-method",
        enabled=True,
        source_path=str(path),
        risk_report_json=json.dumps(
            {
                "langs": {".py": 1},
                "keywords": [],
                "shebangs": [],
                "vendored_node_modules": {"present": True, "file_count": 3, "shadows": []},
            },
        ),
    )
    compiler = AgentDependencyCompiler(
        personal_skill_store=_PersonalStore({record.id: record}),
    )
    external_write = _tool(
        "teammcp.src_crm.updateDeal",
        risk="external_write",
        source_kind="team_mcp",
        approval_required=True,
    )
    catalog = _catalog(
        external_write,
        _tool("skill_read"),
        _tool("skill_invoke"),
    )
    context = _context()
    base = await compiler.compile(
        {"tool_refs": [external_write.stable_ref]},
        context=context,
        tool_catalog=catalog,
    )
    candidate = await compiler.compile(
        {
            "tool_refs": [external_write.stable_ref],
            "skill_grants": [
                _grant(
                    kind="user",
                    skill_id=record.id,
                    name=record.name,
                    revision=_revision(path),
                ),
            ],
        },
        context=context,
        tool_catalog=catalog,
    )

    diff = compare_agent_dependencies(
        base,
        candidate,
        base_context=context,
        candidate_context=context,
    )

    assert base.risk.level == candidate.risk.level == "high"
    assert diff.risk_increased is False
    assert diff.high_risk_change is True
    assert diff.review_required is True


@pytest.mark.anyio
async def test_pending_import_personal_skill_is_blocked_and_cannot_be_granted(
    tmp_path: Path,
) -> None:
    """skill-import 批1（tech-design r3 §3.4）：待处理导入是生命周期状态——已绑定的编译为不可用，
    作者面也拿不到 exact grant；状态清掉后同一记录恢复 ready。"""
    path = _write_skill(tmp_path, name="my-import")
    record = SimpleNamespace(
        id="sk_imp",
        name="my-import",
        enabled=True,
        source_type="user_upload",
        source_path=str(path),
        risk_report_json=json.dumps({"langs": {".py": 1}, "keywords": [], "shebangs": []}),
        import_status="pending",
    )
    compiler = AgentDependencyCompiler(personal_skill_store=_PersonalStore({record.id: record}))
    grant = _grant(kind="user", skill_id=record.id, name=record.name, revision=_revision(path))
    blocked = await compiler.compile(
        {"skill_grants": [grant]},
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )
    assert blocked.status == "blocked"
    assert "skill_import_pending" in blocked.reason_codes
    with pytest.raises(ValueError, match="unavailable"):
        await compiler.exact_grant_for_personal_skill(owner_user_id="user_owner", skill_id=record.id)

    record.import_status = "cleared"
    ready = await compiler.compile(
        {"skill_grants": [grant]},
        context=_context(),
        tool_catalog=_catalog(_tool("skill_read"), _tool("skill_invoke")),
    )
    assert ready.status == "ready"
