"""Canonical admitted-primary reader + source stores + exact file materialization.

Release/Safety fixtures seed matching immutable rows; this is not a business
Eval result or a claim that the HTTP writer/model injection is enabled.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml
from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_behavior_materializer import default_agent_behavior_materializer
from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler, ExactSkillGrant
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferencePlan, reference_source_hash
from bowhead_core.chat_references import AgentResourceRef, InputDocument, McpToolResourceRef, ReferenceNode, SkillResourceRef
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.session_store import _insert_chat_turn_user_source_in_tx
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_read_receiver import SkillReadReceiverError, prepare_primary_baseline_skill_reads
from bowhead_core.skill_read_render import SkillReadRenderError, prepare_primary_skill_read_candidate, restore_rendered_skill_reads
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.subagent_authoring_catalog import native_authoring_tools
from bowhead_gateway.access_context import build_access_context
from test_agent_dependency_compiler import _catalog, _context, _tool
from test_agent_execution_binding import _insert_release
from test_reference_primary_reader import _prepare_case, _read
from test_reference_start_admission import start_case as _start_case

from bowhead_skill.catalog import SkillCatalog

start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


def package(root, name, children=(), *, parameter_schema=None, executable=False, ordinary=False):
    path = root / name
    path.mkdir(parents=True)
    declaration = {
        "schema_version": 1,
        "entry_file": "method.md",
        "required_files": [],
        "parameter_schema": parameter_schema or {"type": "object", "properties": {}, "additionalProperties": False},
        "required_skill_refs": [child.model_dump(mode="json") for child in children],
        "requires_script": False,
        "requires_new_tools": False,
        "requires_new_connections": False,
    }
    (path / "SKILL.md").write_text(
        "---\n"
        + yaml.safe_dump({"name": name, "description": "Financial method", **({} if ordinary else {"read_only_entry": declaration})})
        + "---\n"
        + ("BASELINE_SOP_" + name + " $ARGUMENTS" if ordinary else "NOT_SELECTED_MANIFEST")
    )
    (path / "method.md").write_text("BODY_" + name)
    if executable:
        (path / "scripts").mkdir()
        (path / "scripts" / "runner.py").write_text("raise RuntimeError('fixture script must never run')\n")
    if ordinary:
        (path / "references").mkdir()
        (path / "references/guide.md").write_text("BASELINE_GUIDE_" + name)
    return SkillResourceRef(kind="skill", source_kind="builtin", id="builtin:" + name, version="sha256:" + bundle_sha256(path))


@pytest.fixture
async def receiver_case(start_case, tmp_path, monkeypatch, request):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    case = start_case
    root = tmp_path / "installed_methods"
    parameter_schema = None
    source_mode = str(getattr(request, "param", "")).startswith("source")
    admission_mode = str(getattr(request, "param", "")).startswith("admission")
    if (
        getattr(request, "param", None) == "parameters"
        or source_mode
        or (admission_mode and "optional" not in str(request.param) and request.param != "admission_capability_skill")
    ):
        parameter_schema = {
            "type": "object",
            "properties": {"year": {"type": "integer", "minimum": 2000, "default": 2026}},
            "required": ["year"],
            "additionalProperties": False,
        }
    child = package(root, "support", parameter_schema=parameter_schema)
    mode = str(getattr(request, "param", ""))
    selected = package(
        root, "method", (child,), parameter_schema=parameter_schema, executable=mode.endswith("selected_invoke") or "package_assets" in mode
    )
    if "package_assets" in mode:
        (root / "method" / "asset.bin").write_bytes(bytes(range(256)) * 1280)
        selected = selected.model_copy(update={"version": "sha256:" + bundle_sha256(root / "method")})
    # Only the installed-root location is redirected; compiler/catalog/hash and
    # source reads use the actual package bytes in this isolated installation.
    monkeypatch.setattr("bowhead_core.agent_dependency_compiler.builtin_catalog_dir", lambda: root)
    grants = [
        ExactSkillGrant(
            "builtin",
            resource.id,
            name,
            ("invoke", "read") if name == "method" and mode.endswith("selected_invoke") else ("read",),
            resource.version,
            "reference",
        ).to_canonical_dict()
        for name, resource in (("method", selected), ("support", child))
    ]
    unused = None
    if "unused_" in mode:
        executable = mode.endswith("unused_invoke")
        unused_name = "python_sandbox" if "sandbox_unused_invoke" in mode else "unused"
        unused = package(root, unused_name, executable=executable, ordinary=mode.endswith("unused_sop"))
        if mode.endswith("unused_large"):
            (root / "unused" / "asset.bin").write_bytes(b"x" * (320 * 1024))
            unused = unused.model_copy(update={"version": "sha256:" + bundle_sha256(root / "unused")})
        unused_kind = "builtin"
        if "team_unused_invoke" in mode:
            from bowhead_core.user_store import TeamSkill

            record = TeamSkill(
                id="oskill_runtime_unused",
                team_id=case.fixture.team_id,
                name="unused",
                source_path=str(root / "unused"),
                created_by=case.fixture.owner_id,
                approved_sha256=unused.version,
                risk_report_json=json.dumps({"langs": {".py": 1}, "keywords": [], "shebangs": []}),
            )
            await case.fixture.users.create_team_skill(record)
            unused_kind = "team"
            unused = unused.model_copy(update={"source_kind": "team", "id": record.id})
        grants.append(
            ExactSkillGrant(
                unused_kind, unused.id, unused_name, ("invoke", "read") if executable else ("read",), unused.version, "reference"
            ).to_canonical_dict()
        )
    if str(getattr(request, "param", "")).endswith("root_only"):
        grants = grants[:1]
    if mode == "admission_empty":
        grants = []
    if mode == "admission_capability":
        grants = []
    definition = {
        "max_iterations": 8,
        "model": "analyst",
        "output_schema": None,
        "skill_grants": grants,
        "system_prompt": "Use the exact assigned financial methods.",
        "tool_refs": ["bi_query"],
    }
    if mode in {"admission_capability", "admission_capability_skill"}:
        definition.update(
            primary_capability_ref="cap_visualize@1",
            tool_refs=["image_generate"],
        )
    if "bi_metadata" in mode:
        definition["tool_refs"] = ["bi_query", "bi_schema", "bi_list_measures", "bi_list_dimensions"]
    if mode == "admission_concept_resolve":
        definition["tool_refs"] = ["bi_query", "bi_concept_resolve"]
    if "office" in mode:
        definition["tool_refs"] = ["attachment_read"]
    optional = None
    if "extension" in mode:
        from test_skill_read_extensions import policy_for

        optional = package(root, "optional_method", (child,), parameter_schema=parameter_schema, executable=True)
        definition["runtime_extensions"] = policy_for(optional).canonical_value()
    dependency = await AgentDependencyCompiler(builtin_skills=SkillCatalog(roots=[root]), team_skill_store=case.fixture.users).compile(
        definition,
        context=replace(_context(audience="teamwide"), agent_id="osa_exact", team_id=case.fixture.team_id, owner_user_id=case.fixture.owner_id),
        tool_catalog=replace(
            _catalog(
                _tool("bi_query"),
                *([_tool("bi_concept_resolve")] if mode == "admission_concept_resolve" else []),
                *([_tool(name) for name in ("bi_schema", "bi_list_measures", "bi_list_dimensions")] if "bi_metadata" in mode else []),
                _tool("image_generate", risk="artifact_write"),
                _tool("skill_read"),
                _tool("skill_invoke"),
                next(tool for tool in native_authoring_tools() if tool.stable_ref == "attachment_read"),
            ),
            team_id=case.fixture.team_id,
            actor_user_id=case.fixture.owner_id,
        ),
    )
    assert dependency.status == "ready", dependency.reason_codes
    revision_id, release_id = await _insert_release(
        case.fixture,
        sequence=2,
        prompt=definition["system_prompt"],
        skill_grants=grants,
        runtime_extensions=definition.get("runtime_extensions"),
        dependency_fingerprint=dependency.dependency_fingerprint,
        capability_fingerprint=dependency.capability_fingerprint,
        definition_overrides={
            "primary_capability_ref": "cap_visualize@1",
            "tool_refs": ["image_generate"],
        }
        if mode in {"admission_capability", "admission_capability_skill"}
        else {"tool_refs": definition["tool_refs"]}
        if "bi_metadata" in mode or "office" in mode or mode == "admission_concept_resolve"
        else None,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute("SELECT * FROM team_subagent_revision WHERE id=?", (revision_id,)).fetchone()
    materializer = default_agent_behavior_materializer()
    assert json.loads(revision["definition_json"]) == definition
    descriptor = materializer.compile_team_revision(
        revision_id=revision_id,
        definition=json.loads(revision["definition_json"]),
        source_definition_hash=revision["definition_hash"],
        dependency=dependency,
    )
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        await materializer.persist_team_revision_conn(
            db,
            revision_id=revision_id,
            team_id=case.fixture.team_id,
            subagent_id="osa_exact",
            descriptor=descriptor,
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
    admission = case.args["reference_start"]
    node = admission.input_document.nodes[0]
    admission.input_document.nodes[0] = node.model_copy(
        update={"resource": node.resource.model_copy(update={"version": revision_id, "release_id": release_id})}
    )
    expected = replace(admission.expected, revision_ref=revision_id, release_id=release_id, behavior_fingerprint=descriptor.behavior_fingerprint)
    case.args["reference_start"] = replace(admission, expected=expected)
    case = await _prepare_case(case)
    case.bindings = AgentExecutionBindingStore(case.fixture.db_path)
    if source_mode:
        await _seed_skill_source(case, selected, child, mode=request.param)
    elif not admission_mode:
        await case.store.finalize_started_chat_submission(**case.args)
    await SkillInstanceStore(case.fixture.db_path).initialize()
    access = AccessControlStore(case.fixture.db_path)
    scope = ReferenceRequestScope(**case.scope.model_dump(exclude={"policy_epoch"}))
    if admission_mode:
        scope = scope.model_copy(update={"session_id": None})

    async def context():
        return await build_access_context(
            user_store=case.fixture.users,
            access_store=access,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            expected_team_id=scope.team_id,
            operation=SpaceOperation.EXECUTE,
        )

    index = build_builtin_skill_metadata(builtin_root=root)
    sources = SkillReadSourceResolver(case.fixture.db_path, access_factory=context, builtin_index=lambda: index, builtin_root=root)

    async def reader():
        return await _read(case)

    async def prepare(resources=None):
        return await prepare_primary_baseline_skill_reads(
            sources, resources or [selected], scope=scope, turn_id="t_reference", primary_factory=reader
        )

    return SimpleNamespace(
        case=case,
        root=root,
        selected=selected,
        child=child,
        unused=unused,
        optional=optional,
        sources=sources,
        prepare=prepare,
        scope=scope,
        reader=reader,
        mode=mode,
    )


async def _seed_skill_source(case, selected, child, *, mode):
    """Seed a stored mixed source with actual storage/binding producers.

    There is deliberately no HTTP admission/receipt/worker. Skill plan digests
    are historical fixture data, never current permission evidence for the reader.
    """
    admission = case.args["reference_start"]
    nodes = list(admission.input_document.nodes)
    for index, resource in enumerate((selected, child), start=1):
        nodes.insert(
            index - 1,
            ReferenceNode(
                type="reference",
                node_id=f"00000000-0000-4000-8000-{index:012d}",
                resource=resource,
                label="SAVED_METHOD_LABEL",
                parameters={"year": 2027 - index},
            ),
        )
    if mode == "source_agent_parameters":
        nodes[2] = nodes[2].model_copy(update={"parameters": {"unconfirmed": True}})
    elif mode == "source_agent_binding":
        nodes[2] = nodes[2].model_copy(update={"resource": nodes[2].resource.model_copy(update={"release_id": "arl_other"})})
    elif mode == "source_no_skills":
        nodes = nodes[2:]
    elif mode == "source_no_agent":
        nodes.pop(2)
    elif mode == "source_agent_first":
        nodes.insert(0, nodes.pop(2))
    elif mode == "source_other":
        nodes.insert(
            0,
            ReferenceNode.model_validate(
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000003",
                    "resource": {"kind": "capability", "id": "financial-analysis", "version": 1},
                    "label": "Capability",
                    "parameters": {},
                }
            ),
        )
    elif mode == "source_mcp":
        nodes = [
            ReferenceNode(
                type="reference",
                node_id="00000000-0000-4000-8000-000000000004",
                resource=McpToolResourceRef(
                    kind="mcp_tool",
                    source_id="ots_finance",
                    local_contract_revision="sha256:" + "1" * 64,
                    connection_binding_id="ots_finance",
                    connection_subject_fingerprint="sha256:" + "2" * 64,
                    operation_name="teammcp.ots_finance.getMargin",
                    operation_contract_revision="sha256:" + "3" * 64,
                ),
                label="Read margin",
                parameters={"year": 2026},
            ),
            *nodes[2:],
        ]
    document = InputDocument.model_validate({"schema_version": 1, "nodes": [node.model_dump(mode="python") for node in nodes]})
    source = ChatTurnUserSource(
        "t_reference", "s_reference", case.scope.user_id, case.scope.workspace_id, document.display_text(), (), (), None, document
    )
    now = datetime.now(UTC).isoformat()
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at,active_turn_id) VALUES(?,?,?,?,?,'t_reference')",
            (source.session_id, source.workspace_id, source.user_id, now, now),
        )
        await db.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_reference',?,1,?,?)", (source.session_id, source.user_text, now)
        )
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now)
        binding = await case.bindings.admit_team_execution_in_connection(
            db,
            team_id=case.scope.team_id,
            user_id=case.scope.user_id,
            turn_id=source.turn_id,
            agent_id=admission.expected.agent_id,
            profile_name=admission.profile_name,
            dispatch_source="explicit",
            dispatch_key="reference:primary",
            expected=admission.expected,
        )
        plan = ReferencePlan.model_validate(
            {
                "schema_version": 1,
                "source_turn_id": source.turn_id,
                "logical_source_turn_id": source.turn_id,
                "snapshot_owner_turn_id": source.turn_id,
                "source_hash": reference_source_hash(source),
                "admissions": [
                    {
                        "node_id": node.node_id,
                        "resource": node.resource.model_dump(exclude_none=True),
                        "operation": ("invoke" if mode == "source_invoke" else "read")
                        if isinstance(node.resource, SkillResourceRef)
                        else {"agent": "delegate", "capability": "execute", "mcp_tool": "invoke"}[node.resource.kind],
                        "policy_digest": "sha256:" + "0" * 64
                        if mode == "source_agent_policy" and isinstance(node.resource, AgentResourceRef)
                        else binding.policy_fingerprint,
                    }
                    for node in nodes
                    if isinstance(node, ReferenceNode)
                ],
            }
        )
        await ChatReferenceStore.put_plan_in_connection(
            db, scope=case.scope, turn_id=source.turn_id, plan=plan, expires_at=(datetime.now(UTC) + timedelta(days=90)).isoformat()
        )
        await case.bindings.insert_root_binding_in_connection(
            db,
            binding=binding,
            user_id=case.scope.user_id,
            assignment_id=admission.expected.assignment_id,
            binding_source="mention",
            bind_primary_execution=True,
        )
        await db.commit()


async def test_actual_primary_baseline_matches_closure_and_runtime_read_scope(receiver_case):
    state = receiver_case
    primary = await _read(state.case)
    assert primary.binding.behavior_descriptor.verification_state != "blocked"
    result = await state.prepare()
    assert result.receiver.revision_ref == "arv_exact_2" and result.receiver.release_id == "arl_exact_2"
    assert [item.proof.resource for item in result.closure.items] == [state.child, state.selected]
    assert [item.material.document()["files"][0]["text"] for item in result.closure.items] == ["BODY_support", "BODY_method"]
    primary = await _read(state.case)
    profile = await state.case.runner.resolve_by_agent_revision(primary.binding, state.case.ctx)
    assert profile.skill_runtime_scope.read_names == ("method", "support")
    assert profile.skill_runtime_scope.invoke_names == ()
    with sqlite3.connect(state.case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_content_snapshots").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM turn_events WHERE turn_id='t_reference'").fetchone() == (0,)


@pytest.mark.parametrize("receiver_case", ["parameters"], indirect=True)
async def test_actual_bound_baseline_renders_each_explicit_parameter_without_changing_source(receiver_case):
    state = receiver_case
    before = await state.reader()
    selections = [
        ReferenceNode(
            type="reference",
            node_id=f"00000000-0000-4000-8000-{index:012d}",
            resource=resource,
            label="DISPLAY_NOT_AUTHORITY",
            parameters={"year": year},
        )
        for index, (resource, year) in enumerate(((state.selected, 2026), (state.child, 2025)), start=1)
    ]
    with pytest.raises(SkillReadRenderError, match="^reference_skill_dependency_parameters_required$"):
        await prepare_primary_skill_read_candidate(
            state.sources, selections[:1], scope=state.scope, turn_id="t_reference", primary_factory=state.reader
        )
    prepared, rendered = await prepare_primary_skill_read_candidate(
        state.sources, selections, scope=state.scope, turn_id="t_reference", primary_factory=state.reader
    )
    assert [item["parameters"] for item in rendered.document()["skills"]] == [{"year": 2025}, {"year": 2026}]
    assert [item["files"][0]["text"] for item in rendered.document()["skills"]] == ["BODY_support", "BODY_method"]
    assert prepared.receiver.execution_id == before.binding.execution_id
    assert prepared.receiver.source_hash == before.plan.source_hash
    after = await state.reader()
    assert after.source == before.source and after.plan == before.plan
    (state.root / "method" / "method.md").write_text("A_LATER_VERSION")
    assert restore_rendered_skill_reads(rendered.content, prepared=prepared, selections=selections) == rendered
    # Caller candidates have not been written into single-@ source or submitted.
    with sqlite3.connect(state.case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_content_snapshots").fetchone() == (0,)


async def test_candidate_renderer_cannot_bypass_current_primary_assignment(receiver_case):
    state = receiver_case
    with sqlite3.connect(state.case.fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET personal_state='removed' WHERE user_id=? AND agent_id='osa_exact'", (state.case.fixture.member_id,))
    selection = ReferenceNode(
        type="reference", node_id="00000000-0000-4000-8000-000000000001", resource=state.selected, label="Method", parameters={}
    )
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_unavailable$"):
        await prepare_primary_skill_read_candidate(state.sources, [selection], scope=state.scope, turn_id="t_reference", primary_factory=state.reader)


async def test_available_method_cannot_supply_a_new_unassigned_baseline(receiver_case, monkeypatch):
    state = receiver_case
    other = package(state.root, "unassigned")
    refreshed = build_builtin_skill_metadata(builtin_root=state.root)
    state.sources.builtin_index = lambda: refreshed
    await state.sources.prepare(other, scope=ReferenceRequestScope(**state.case.scope.model_dump(exclude={"policy_epoch"})))
    calls = []

    async def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("unassigned method must not reach source IO")

    monkeypatch.setattr(state.sources, "prepare", unexpected)
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_baseline_required$"):
        await state.prepare([other])
    assert not calls


async def test_current_agent_removal_during_capture_invalidates_preparation(receiver_case, monkeypatch):
    state = receiver_case
    original = state.sources.prepare
    calls = []

    async def prepare_then_remove(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result.proof.resource)
        with sqlite3.connect(state.case.fixture.db_path) as db:
            db.execute(
                "UPDATE agent_assignment SET personal_state='removed' WHERE user_id=? AND agent_id='osa_exact'", (state.case.fixture.member_id,)
            )
        return result

    monkeypatch.setattr(state.sources, "prepare", prepare_then_remove)
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_unavailable$"):
        await state.prepare()
    assert calls == [state.selected, state.child]


@pytest.mark.parametrize("receiver_case", ["root_only"], indirect=True)
async def test_actual_declared_child_needs_its_own_receiver_grant(receiver_case, monkeypatch):
    state = receiver_case
    original = state.sources.prepare
    calls = []

    async def record(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result.proof.resource)
        return result

    monkeypatch.setattr(state.sources, "prepare", record)
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_baseline_required$"):
        await state.prepare()
    assert calls == [state.selected]


async def test_new_live_release_cannot_replace_the_reserved_baseline(receiver_case):
    state = receiver_case
    await _insert_release(state.case.fixture, sequence=3, prompt="New release without these methods")
    result = await state.prepare()
    assert result.receiver.revision_ref == "arv_exact_2"
    assert [item.proof.resource for item in result.closure.items] == [state.child, state.selected]
