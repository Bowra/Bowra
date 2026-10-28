"""Published-source execution through real registered tools and SQLite stores.

Only the process/sandbox boundary is captured; resolver, package capture,
projection, authorization and file reads are production implementations.
"""

from __future__ import annotations

import asyncio
import copy
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.subagent import _materialize_team_profile, _team_revision_projection
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_skill.tools import build_skill_tools
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

from bowhead_skill import SkillCatalog, SkillRuntime

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.fixture
async def assigned_case(receiver_case, monkeypatch):
    case = receiver_case
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setattr("bowhead_core.assigned_skill_execution.builtin_catalog_dir", lambda: case.root)
    root = case.root.parent / "uploaded"
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: method\ndescription: exact method\nsandbox_requirements: [numpy==2.0.0]\n---\nUPLOADED $ARGUMENTS\n")
    (root / "scripts/run.py").write_text("print('ORIGINAL')\n")
    (root / "asset.bin").write_bytes(b"\x00\x01asset")
    record = await case.sources.instances.upsert(
        user_id=case.scope.user_id, name="method", source_type="user_upload", source_path=str(root), enabled=True
    )
    grant = ExactSkillGrant("user", record.id, "method", ("invoke", "read"), "sha256:" + bundle_sha256(root), "reference")
    # The installed same-name method must not shadow this explicit personal ID.
    sandbox = case.root / "python_sandbox"
    (sandbox / "scripts").mkdir(parents=True)
    (sandbox / "SKILL.md").write_text("---\nname: python_sandbox\ndescription: sandbox transport\n---\nTRANSPORT\n")
    (sandbox / "scripts/runner.py").write_text("print('transport')\n")
    catalog = SkillCatalog(roots=[case.root])
    runtime = SkillRuntime()
    calls = []

    async def run(spec, *, entry_script, input_data, extra_args):
        copied_root = Path(input_data["_skill_dir"]) if "_skill_dir" in input_data else spec.source_dir
        calls.append(
            {
                "spec": spec.name,
                "script": entry_script,
                "root": copied_root,
                "input": copy.deepcopy(input_data),
                "files": {p.relative_to(copied_root).as_posix(): p.read_bytes() for p in copied_root.rglob("*") if p.is_file()},
            }
        )
        return SimpleNamespace(
            ok=True, exit_code=0, stdout="done", stderr="", result_json={"ok": True}, outputs=[], workdir=None, duration_ms=1, error=None
        )

    monkeypatch.setattr(runtime, "run", run)
    registry = ToolRegistry()
    register_skill_tools(registry, catalog, runtime)
    ctx = ToolExecutionContext(
        workspace_id=case.scope.workspace_id,
        user_id=case.scope.user_id,
        team_id=case.scope.team_id,
        session_id=case.scope.session_id,
        turn_id="t_assigned",
        member_uid="m",
        datarc=None,
        access_control_store=case.sources.access,
        skill_instance_store=case.sources.instances,
        skill_catalog=catalog,
    )
    ctx.role = "analyst"
    ctx.access_context = await case.sources.access_factory()

    def configure(new_grant):
        profile = _materialize_team_profile(
            **_team_revision_projection(
                {"skill_grants": [new_grant.to_canonical_dict()], "tool_refs": [], "system_prompt": "Use exact skill.", "max_iterations": 3},
                {"name": "assigned", "description": "Analyze with the declared method."},
                expected_name="assigned",
            )
        )
        assert profile is not None
        ctx.skill_runtime_grants, ctx.skill_runtime_scope = profile.skill_runtime_grants, profile.skill_runtime_scope
        return profile

    configure(grant)
    return SimpleNamespace(
        base=case, root=root, grant=grant, ctx=ctx, registry=registry, catalog=catalog, runtime=runtime, calls=calls, configure=configure
    )


async def invoke(case, *, args=None, tool="skill_invoke", raw=False):
    args = args or {"name": "method", "script": "run.py", "input": {"inputs": {}}}
    if raw:
        return await next(item.handler for item in build_skill_tools(case.catalog, case.runtime) if item.name == tool)(args, case.ctx)
    return await case.registry.get(tool).handler(args, case.ctx)


async def test_exact_user_source_reads_and_runs_pinned_copy_despite_builtin_collision(assigned_case):
    case = assigned_case
    case.ctx._ξ_skill_cache["method|||'year'"] = {"body": "FOREIGN_CACHE"}
    read = await invoke(case, tool="skill_read", args={"name": "method", "args": "year"})
    assert read["body"].strip() == "UPLOADED year"
    assert read["script_execution"] == "sandbox"
    args = {"name": "method", "script": "run.py", "input": {"inputs": {}, "_skill_dir": "/wrong", "_sandbox_requirements": ["evil"]}}
    original = copy.deepcopy(args)
    result = await invoke(case, args=args)
    assert result["ok"] is True
    assert args == original
    call = case.calls[0]
    assert (call["spec"], call["script"]) == ("python_sandbox", "runner.py")
    assert call["root"] != case.root and not call["root"].exists()
    assert call["files"]["scripts/run.py"] == b"print('ORIGINAL')\n"
    assert call["files"]["asset.bin"] == b"\x00\x01asset"
    assert call["input"]["_sandbox_requirements"] == ["numpy==2.0.0"]
    assert case.ctx.assigned_skill_material is None


@pytest.mark.parametrize("change", ["id", "permission", "distribution", "pending", "disabled", "scope", "raw", "flag"])
async def test_unavailable_exact_source_never_reaches_process(assigned_case, monkeypatch, change):
    case = assigned_case
    if change == "id":
        case.configure(replace(case.grant, id="other_instance"))
    elif change == "permission":
        case.configure(replace(case.grant, permissions=("read",)))
    elif change == "distribution":
        case.configure(replace(case.grant, distribution="embedded_agent_scope"))
    elif change == "pending":
        await case.base.sources.instances.set_import_status(user_id=case.ctx.user_id, name="method", import_status="pending", import_report_json="{}")
    elif change == "disabled":
        await case.base.sources.instances.upsert(
            user_id=case.ctx.user_id, name="method", source_type="user_upload", source_path=str(case.root), enabled=False
        )
    elif change == "scope":
        case.ctx.team_id = "another_team"
    elif change == "flag":
        monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "0")
    result = await invoke(case, raw=change == "raw")
    assert result.get("ok") is not True and "error" in result
    assert case.calls == []


async def test_current_file_change_after_capture_cannot_replace_executed_bytes(assigned_case, monkeypatch):
    case = assigned_case

    async def hydrate(input_data, ctx):
        (case.root / "scripts/run.py").write_text("print('CHANGED_AFTER_CAPTURE')\n")

    monkeypatch.setattr("bowhead_skill.tools._fill_input_filenames", hydrate)
    assert (await invoke(case))["ok"] is True
    assert case.calls[0]["files"]["scripts/run.py"] == b"print('ORIGINAL')\n"
    # A subsequent invocation resolves again and captures the owner's new bytes.
    assert (await invoke(case))["ok"] is True
    assert case.calls[1]["files"]["scripts/run.py"] == b"print('CHANGED_AFTER_CAPTURE')\n"


async def test_revocation_during_input_hydration_blocks_process_and_removes_copy(assigned_case, monkeypatch):
    case = assigned_case
    copies = []

    async def hydrate(input_data, ctx):
        copies.append(ctx.assigned_skill_material.spec.source_dir)
        await case.base.sources.instances.upsert(
            user_id=case.ctx.user_id, name="method", source_type="user_upload", source_path=str(case.root), enabled=False
        )

    monkeypatch.setattr("bowhead_skill.tools._fill_input_filenames", hydrate)
    result = await invoke(case)
    assert (result["error_kind"], result["skill"], result["reason"]) == ("skill_unavailable", "method", "not_visible")
    assert "turned off in your Skills" in result["error"]
    assert not case.calls and len(copies) == 1 and not copies[0].exists()


@pytest.mark.parametrize(
    ("entry", "change"),
    [
        (entry, change)
        for entry in ("uploaded-script", "uploaded-command", "sandbox-invoke", "python-exec")
        for change in ("allowed", "disabled", "permission")
    ]
    + [("uploaded-script", "pending"), ("uploaded-command", "pending"), ("uploaded-script", "runtime"), ("uploaded-command", "command")],
)
async def test_ordinary_execution_rechecks_original_skill_after_hydration(assigned_case, monkeypatch, entry, change):
    case = assigned_case
    # Ordinary agents use the same registered handlers, without a published exact grant.
    case.ctx.skill_runtime_grants = None
    case.ctx.skill_runtime_scope = None
    catalog_root = case.root.parent / "ordinary-catalog"
    shutil.copytree(case.base.root / "python_sandbox", catalog_root / "python_sandbox")
    catalog = SkillCatalog(roots=[catalog_root])
    case.registry = ToolRegistry()
    register_skill_tools(case.registry, catalog, case.runtime)
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", "1")
    name = "method" if entry.startswith("uploaded") else "python_sandbox"
    hydrated = []

    async def hydrate(input_data, ctx):
        hydrated.append(name)
        if change == "disabled":
            await case.base.sources.instances.upsert(
                user_id=ctx.user_id,
                name=name,
                source_type="user_upload" if name == "method" else "builtin",
                source_path=str(case.root) if name == "method" else "",
                enabled=False,
            )
        elif change == "permission":
            await case.base.sources.access.create_or_update_resource_grant(
                team_id=ctx.team_id,
                resource_kind="skill",
                resource_id=name,
                principal_type="user",
                principal_id=ctx.user_id,
                permission="invoke",
                effect="deny",
            )
        elif change == "pending":
            await case.base.sources.instances.set_import_status(user_id=ctx.user_id, name=name, import_status="pending", import_report_json="{}")
        elif change in {"runtime", "command"}:
            monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME" if change == "runtime" else "BOWHEAD_SKILL_INVOKE_COMMAND", "0")

    monkeypatch.setattr("bowhead_skill.tools._fill_input_filenames", hydrate)
    args = {"name": name, "script": "run.py" if name == "method" else "runner.py", "input": {"code": "print(42)", "inputs": {}}}
    if entry == "uploaded-command":
        args["command"] = ["python3", "scripts/run.py"]
    if entry == "python-exec":
        args = {"code": "print(42)", "inputs": {}}
    result = await invoke(case, tool="python_exec" if entry == "python-exec" else "skill_invoke", args=args)
    assert hydrated == [name]
    if change == "allowed":
        assert result.get("ok") is True, result
        assert len(case.calls) == 1
        assert case.calls[0]["spec"] == "python_sandbox"
    else:
        expected = {
            "permission": "skill_scope_denied",
            "pending": "skill_import_pending",
            "runtime": "skill_unavailable",
            "command": "skill_unavailable",
        }
        assert result.get("error_kind") == expected.get(change, "resource_revoked"), result
        if change in {"runtime", "command"}:
            # Ordinary use reports the same switch decision as an Agent's Skill call.
            assert result["reason"] == ("script_runtime_disabled" if change == "runtime" else "command_disabled")
            assert result["skill"] == "method"
        assert case.calls == []


async def test_cancelled_handler_removes_copy_and_propagates_cancellation(assigned_case, monkeypatch):
    case = assigned_case
    copies = []

    async def hydrate(input_data, ctx):
        copies.append(ctx.assigned_skill_material.spec.source_dir)
        raise asyncio.CancelledError()

    monkeypatch.setattr("bowhead_skill.tools._fill_input_filenames", hydrate)
    with pytest.raises(asyncio.CancelledError):
        await invoke(case)
    assert not case.calls and len(copies) == 1 and not copies[0].exists()


async def test_builtin_grant_executes_actual_pinned_subprocess(assigned_case, monkeypatch):
    case = assigned_case
    root = case.base.root / "method"
    (root / "scripts").mkdir()
    (root / "scripts/run.py").write_text("import json\nprint(json.dumps({'answer': 42}))\n")
    case.catalog.refresh()
    case.configure(ExactSkillGrant("builtin", "builtin:method", "method", ("invoke", "read"), "sha256:" + bundle_sha256(root), "reference"))
    runtime = SkillRuntime(keep_workdir=False)
    monkeypatch.setattr(case.runtime, "run", runtime.run)
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "0")
    result = await invoke(case)
    assert result["ok"] is True and '"answer": 42' in result["stdout"]


async def test_team_grant_uses_exact_id_in_registry_and_source_gate(assigned_case):
    from bowhead_core.resource_access import resolve_skill_grant_target
    from bowhead_core.user_store import TeamSkill

    case = assigned_case
    team_skill = TeamSkill(
        id="oskill_assigned",
        team_id=case.ctx.team_id,
        name="method",
        source_path=str(case.root),
        created_by=case.ctx.user_id,
        approved_sha256=case.grant.revision,
    )
    await case.base.sources.users.create_team_skill(team_skill)
    case.configure(replace(case.grant, kind="team", id=team_skill.id))
    target, owner = await resolve_skill_grant_target(case.ctx, "method")
    assert target == ("team_skill", team_skill.id, "use") and owner == case.ctx.user_id
    # Both catalog and personal records have this same name; neither substitutes.
    invocation = await case.registry.execute("skill_invoke", {"name": "method", "script": "run.py", "input": {"inputs": {}}}, case.ctx)
    assert invocation.result is not None, invocation.error
    assert invocation.result["ok"] is True
    assert case.calls[0]["files"]["scripts/run.py"] == b"print('ORIGINAL')\n"


async def _team_method_skill(case):
    from bowhead_core.user_store import TeamSkill

    (case.root / "SKILL.md").write_text("---\nname: method\ndescription: exact method\n---\nAPPROVED_EDIT\n")
    (case.root / "scripts/run.py").write_text("print('APPROVED_EDIT')\n")
    team_skill = TeamSkill(
        id="oskill_assigned",
        team_id=case.ctx.team_id,
        name="method",
        source_path=str(case.root),
        created_by=case.ctx.user_id,
        approved_sha256=bundle_sha256(case.root),
    )
    await case.base.sources.users.create_team_skill(team_skill)
    # The Agent was published against the earlier approved revision of this Skill.
    case.configure(replace(case.grant, kind="team", id=team_skill.id))
    return team_skill


async def test_team_skill_edited_after_publish_follows_latest_approved_revision(assigned_case):
    case = assigned_case
    await _team_method_skill(case)
    read = await invoke(case, tool="skill_read", args={"name": "method"})
    assert read["body"].strip() == "APPROVED_EDIT"
    assert (await invoke(case))["ok"] is True
    assert case.calls[0]["files"]["scripts/run.py"] == b"print('APPROVED_EDIT')\n"


async def test_team_skill_edited_during_load_reports_the_skill(assigned_case, monkeypatch):
    case = assigned_case
    await _team_method_skill(case)

    async def stale(*args):
        return case.grant.revision

    monkeypatch.setattr("bowhead_core.assigned_skill_execution._current_revision", stale)
    result = await invoke(case, tool="skill_read", args={"name": "method"})
    assert result["error_kind"] == "assigned_skill_version_changed"
    assert "'method'" in result["error"] and "Retry" in result["error"]
    assert case.calls == []


async def test_personal_skill_edited_after_publish_follows_current_bytes(assigned_case):
    case = assigned_case
    (case.root / "scripts/run.py").write_text("print('OWNER_EDIT')\n")
    assert (await invoke(case))["ok"] is True
    assert case.calls[0]["files"]["scripts/run.py"] == b"print('OWNER_EDIT')\n"


async def test_read_revoked_during_description_await_does_not_return_body(assigned_case, monkeypatch):
    case = assigned_case

    async def describe(ctx, spec, **kwargs):
        await case.base.sources.instances.upsert(
            user_id=case.ctx.user_id, name="method", source_type="user_upload", source_path=str(case.root), enabled=False
        )
        return "description"

    monkeypatch.setattr("bowhead_skill.tools.description_for", describe)
    result = await invoke(case, tool="skill_read", args={"name": "method"})
    assert (result["error_kind"], result["operation"], result["reason"]) == ("skill_unavailable", "read", "not_visible") and "body" not in result


async def test_source_capture_detaches_arguments_and_context_before_await(assigned_case, monkeypatch):
    import bowhead_core.assigned_skill_execution as module

    case = assigned_case
    args = {"name": "method", "script": "run.py", "input": {"inputs": {}, "title": "ORIGINAL"}}
    original = module.SkillReadSourceResolver.prepare_runtime_package

    async def interleave(self, *pos, **kw):
        args["input"]["title"] = "CHANGED"
        case.ctx.team_id = "changed_team"
        return await original(self, *pos, **kw)

    monkeypatch.setattr(module.SkillReadSourceResolver, "prepare_runtime_package", interleave)
    assert (await invoke(case, args=args))["ok"] is True
    assert case.calls[0]["input"]["title"] == "ORIGINAL"


async def test_completed_material_cannot_be_reused(assigned_case, monkeypatch):
    from bowhead_skill.runtime_scope import assigned_skill_operation_denial

    case = assigned_case
    contexts = []

    async def hydrate(input_data, ctx):
        contexts.append(ctx)

    monkeypatch.setattr("bowhead_skill.tools._fill_input_filenames", hydrate)
    assert (await invoke(case))["ok"] is True
    result = await assigned_skill_operation_denial(contexts[0], "skill_invoke", {"name": "method"})
    assert (result["error_kind"], result["reason"]) == ("skill_unavailable", "source_changed")


@pytest.mark.parametrize("flag", ["BOWHEAD_SKILL_MULTIRUNTIME", "BOWHEAD_SKILL_INVOKE_COMMAND"])
async def test_flag_closed_during_hydration_blocks_new_dispatch(assigned_case, monkeypatch, flag):
    case = assigned_case
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", "1")

    async def hydrate(input_data, ctx):
        monkeypatch.setenv(flag, "0")

    monkeypatch.setattr("bowhead_skill.tools._fill_input_filenames", hydrate)
    args = {"name": "method", "command": ["python3", "scripts/run.py"], "input": {"inputs": {}}}
    result = await invoke(case, args=args)
    assert result["error_kind"] == "skill_unavailable"
    assert result["reason"] == ("script_runtime_disabled" if flag == "BOWHEAD_SKILL_MULTIRUNTIME" else "command_disabled")
    assert flag in result["error"] and "'method'" in result["error"]
    assert not case.calls


async def test_raw_dispatch_and_list_cannot_discard_the_published_scope(assigned_case):
    case = assigned_case
    case.ctx.skill_runtime_scope = None
    for tool, args in [("skill_invoke", {"name": "method"}), ("skill_read", {"name": "method"}), ("skill_list", {})]:
        result = await invoke(case, tool=tool, args=args, raw=True)
        assert result["error_kind"] == "skill_unavailable"
        assert result["reason"] == ("not_granted" if tool == "skill_list" else "source_changed")
    assert not case.calls


async def test_cancellation_reaps_actual_child_before_removing_source_copy(assigned_case, monkeypatch):
    import bowhead_skill.runtime as module

    case = assigned_case
    root = case.base.root / "method"
    (root / "scripts").mkdir()
    (root / "scripts/run.py").write_text("import time\ntime.sleep(60)\n")
    case.catalog.refresh()
    case.configure(ExactSkillGrant("builtin", "builtin:method", "method", ("invoke", "read"), "sha256:" + bundle_sha256(root), "reference"))
    monkeypatch.setattr(case.runtime, "run", SkillRuntime(keep_workdir=False).run)
    original = module.asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append((child, Path(args[1]).parent.parent))
        started.set()
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(invoke(case))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        child, copied_root = children[0]
        assert child.returncode is not None and not copied_root.exists()
    finally:
        # Also prevent a deliberate cancellation-guard mutation leaking a child.
        task.cancel()
        for child, _ in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


async def test_handler_failure_keeps_original_error_contract_and_cleans_copy(assigned_case, monkeypatch):
    case = assigned_case
    roots = []

    async def fail(spec, *, entry_script, input_data, extra_args):
        roots.append(Path(input_data["_skill_dir"]))
        raise RuntimeError("process outcome unavailable")

    monkeypatch.setattr(case.runtime, "run", fail)
    with pytest.raises(RuntimeError, match="process outcome unavailable"):
        await invoke(case)
    assert len(roots) == 1 and not roots[0].exists()


async def test_large_normal_package_does_not_expand_reference_storage_budget(assigned_case):
    from bowhead_core.chat_references import SkillResourceRef
    from bowhead_core.skill_package_material import SkillPackageMaterialError

    case = assigned_case
    (case.root / "asset.bin").write_bytes(b"asset" * 250000)
    case.configure(replace(case.grant, revision="sha256:" + bundle_sha256(case.root)))
    assert (await invoke(case))["ok"] is True
    assert len(case.calls[0]["files"]["asset.bin"]) == 1250000
    # The persisted reference format still has its independent encoded limit.
    resource = SkillResourceRef(kind="skill", source_kind="user", id=case.grant.id, version=case.ctx.skill_runtime_grants[0].revision)
    with pytest.raises(SkillPackageMaterialError):
        await case.base.sources.prepare_package(resource, scope=case.base.scope)


@pytest.mark.parametrize("tool", ["skill_invoke", "python_exec"])
async def test_exact_builtin_sandbox_uses_same_scope_for_both_entrypoints(assigned_case, tool):
    case = assigned_case
    root = case.base.root / "python_sandbox"
    case.configure(
        ExactSkillGrant("builtin", "builtin:python_sandbox", "python_sandbox", ("invoke", "read"), "sha256:" + bundle_sha256(root), "reference")
    )
    code = {"code": "print(42)", "inputs": {}}
    args = code if tool == "python_exec" else {"name": "python_sandbox", "script": "runner.py", "input": code}
    assert (await invoke(case, tool=tool, args=args))["ok"] is True
    assert case.calls[0]["spec"] == "python_sandbox"
    assert case.calls[0]["input"]["code"] == "print(42)"
    assert "_skill_dir" not in case.calls[0]["input"]
    assert case.calls[0]["root"] != root and not case.calls[0]["root"].exists()


@pytest.mark.parametrize("tool", ["skill_read", "python_exec"])
async def test_builtin_grant_follows_installed_revision_after_upgrade(assigned_case, tool):
    case = assigned_case
    root = case.base.root / "python_sandbox"
    published = "sha256:" + bundle_sha256(root)
    case.configure(ExactSkillGrant("builtin", "builtin:python_sandbox", "python_sandbox", ("invoke", "read"), published, "reference"))
    # An application release changes the installed builtin package.
    (root / "scripts/runner.py").write_text("print('transport v2')\n")
    case.catalog.refresh()
    assert "sha256:" + bundle_sha256(root) != published
    args = {"code": "print(42)", "inputs": {}} if tool == "python_exec" else {"name": "python_sandbox"}
    result = await invoke(case, tool=tool, args=args)
    assert result.get("ok") is not False, result
    if tool == "python_exec":
        assert case.calls[0]["files"]["scripts/runner.py"] == b"print('transport v2')\n"
    else:
        assert "TRANSPORT" in result["body"]


async def test_bridge_captures_receiver_and_arguments_before_context_refresh(assigned_case, monkeypatch):
    case = assigned_case
    args = {"name": "method", "script": "run.py", "input": {"inputs": {}, "title": "ORIGINAL"}}
    refresh = ToolExecutionContext.ensure_reference_skill_context

    async def interleave(ctx):
        args["input"]["title"] = "CHANGED"
        case.ctx.team_id = "changed_team"
        await refresh(ctx)

    monkeypatch.setattr(ToolExecutionContext, "ensure_reference_skill_context", interleave)
    assert (await invoke(case, args=args))["ok"] is True
    assert case.calls[0]["input"]["title"] == "ORIGINAL"


async def _team_skill_with(case, **fields):
    """A team Skill whose state the Agent's team grant meets at run time."""
    from bowhead_core.user_store import TeamSkill

    values = {"created_by": case.ctx.user_id, "approved_sha256": bundle_sha256(case.root), **fields}
    team_skill = TeamSkill(id="oskill_state", team_id=case.ctx.team_id, name="method", source_path=str(case.root), **values)
    await case.base.sources.users.create_team_skill(team_skill)
    case.configure(replace(case.grant, kind="team", id=team_skill.id, revision="sha256:" + bundle_sha256(case.root)))
    return team_skill


@pytest.mark.parametrize(
    ("fields", "reason", "fix"),
    [
        ({"status": "disabled"}, "disabled_by_team", "Ask a team admin to turn it back on."),
        ({"approval": "pending", "approved_sha256": None}, "not_approved", "Ask a team admin to approve it."),
        ({"approval": "pending"}, "pending_reapproval", "Ask a team admin to approve the new version."),
        ({"visibility": "creator_only", "created_by": "u_other_owner"}, "not_visible", "Ask its owner or a team admin to share it with you."),
    ],
)
async def test_team_skill_state_is_reported_with_its_specific_reason(assigned_case, fields, reason, fix):
    case = assigned_case
    await _team_skill_with(case, **fields)
    for tool, args, operation in [
        ("skill_read", {"name": "method"}, "read"),
        ("skill_invoke", {"name": "method", "script": "run.py", "input": {"inputs": {}}}, "invoke"),
    ]:
        result = await invoke(case, tool=tool, args=args)
        assert {key: result.get(key) for key in ("ok", "error_kind", "skill", "operation", "reason")} == {
            "ok": False,
            "error_kind": "skill_unavailable",
            "skill": "method",
            "operation": operation,
            "reason": reason,
        }, result
        assert result["error"].startswith("Skill 'method'") and result["error"].endswith(fix)
        assert str(case.root) not in result["error"]
    assert case.calls == []


async def test_admin_use_deny_is_reported_as_use_denied(assigned_case):
    case = assigned_case
    team_skill = await _team_skill_with(case)
    await case.base.sources.access.create_or_update_resource_grant(
        team_id=case.ctx.team_id,
        resource_kind="team_skill",
        resource_id=team_skill.id,
        principal_type="user",
        principal_id=case.ctx.user_id,
        permission="use",
        effect="deny",
    )
    case.ctx.access_context = await case.base.sources.access_factory()
    result = await case.registry.get("skill_invoke").handler({"name": "method", "script": "run.py", "input": {"inputs": {}}}, case.ctx)
    assert (result["error_kind"], result["skill"], result["reason"]) == ("skill_unavailable", "method", "use_denied"), result
    assert "a team admin restricted it for you" in result["error"]
    assert case.calls == []


async def test_member_preference_does_not_turn_off_an_agents_team_skill(assigned_case):
    """The member's own "turn off for me" choice affects only their ordinary Skill use."""
    from bowhead_core.chat_references import SkillResourceRef
    from bowhead_core.skill_read_resolution import SkillReadResolutionError

    case = assigned_case
    team_skill = await _team_skill_with(case)
    await case.base.sources.instances.upsert(
        user_id=case.ctx.user_id, name=f"team:{team_skill.id}", source_type="team_shared", source_path=str(case.root), enabled=False
    )
    read = await invoke(case, tool="skill_read", args={"name": "method"})
    assert read.get("error_kind") is None, read
    assert (await invoke(case))["ok"] is True
    assert case.calls[0]["files"]["scripts/run.py"] == b"print('ORIGINAL')\n"

    # Ordinary (non-Agent) resolution of the same Skill still honours the preference.
    resource = SkillResourceRef(kind="skill", source_kind="team", id=team_skill.id, version="sha256:" + bundle_sha256(case.root))
    with pytest.raises(SkillReadResolutionError) as raised:
        await case.base.sources.prepare(resource, scope=case.base.scope)
    assert (raised.value.reason, raised.value.cause) == ("not_visible", "personal_disabled")


async def test_agent_script_call_without_the_deployment_switch_names_the_switch(assigned_case, monkeypatch):
    case = assigned_case
    monkeypatch.delenv("BOWHEAD_SKILL_MULTIRUNTIME")
    result = await invoke(case)
    assert result == {
        "ok": False,
        "error_kind": "skill_unavailable",
        "skill": "method",
        "operation": "invoke",
        "reason": "script_runtime_disabled",
        "error": (
            "Skill 'method' can't run scripts here: this deployment hasn't enabled Skill scripts (BOWHEAD_SKILL_MULTIRUNTIME). "
            "Ask your administrator to enable it, or continue without running the Skill."
        ),
        "retryable": False,
    }
    # Reading the Skill doesn't need the switch.
    assert "UPLOADED" in (await invoke(case, tool="skill_read", args={"name": "method"}))["body"]
    assert case.calls == []


async def test_agent_command_call_without_the_command_switch_is_command_disabled(assigned_case, monkeypatch):
    case = assigned_case
    monkeypatch.delenv("BOWHEAD_SKILL_INVOKE_COMMAND", raising=False)
    result = await invoke(case, args={"name": "method", "command": ["python3", "scripts/run.py"], "input": {"inputs": {}}})
    assert (result["error_kind"], result["reason"]) == ("skill_unavailable", "command_disabled")
    assert "BOWHEAD_SKILL_INVOKE_COMMAND" in result["error"] and case.calls == []


async def test_skill_outside_the_agent_and_read_only_grant_are_not_granted(assigned_case):
    case = assigned_case
    other = await invoke(case, tool="skill_read", args={"name": "other-skill"})
    assert (other["error_kind"], other["skill"], other["operation"], other["reason"]) == ("skill_unavailable", "other-skill", "read", "not_granted")
    assert "isn't included in this Agent" in other["error"]
    case.configure(replace(case.grant, permissions=("read",)))
    read_only = await invoke(case)
    assert (read_only["skill"], read_only["operation"], read_only["reason"]) == ("method", "invoke", "not_granted")
    assert "reading only" in read_only["error"]
    assert case.calls == []


async def test_agent_skill_list_shows_only_the_agents_skills(assigned_case):
    case = assigned_case
    listed = await invoke(case, tool="skill_list", args={})
    assert isinstance(listed, list), listed
    assert [entry["name"] for entry in listed] == ["method"]
    # Without an Agent scope the ordinary list is unchanged (the installed sandbox appears).
    case.ctx.skill_runtime_grants = None
    case.ctx.skill_runtime_scope = None
    assert "python_sandbox" in {entry["name"] for entry in await invoke(case, tool="skill_list", args={})}
