"""Retained material through native Skill handlers and real source stores."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadRunState
from bowhead_core.ordinary_lead_material import (
    artifact_evidence,
    canvas_evidence,
    retained_tool_materials,
    revalidate_skills,
    revalidate_tool_materials,
)
from bowhead_core.types import ToolInvocation
from test_assigned_skill_execution import assigned_case as assigned_case
from test_assigned_skill_execution import invoke
from test_assigned_skill_execution import receiver_case as receiver_case
from test_assigned_skill_execution import start_case as start_case

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


async def collect(case, monkeypatch):
    ctx = case.ctx
    ctx.session_store = case.base.case.store
    ctx.session_id = "s_ordinary_material"
    await ctx.session_store.ensure_session(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    await ctx.session_store.record_turn(turn_id=ctx.turn_id, session_id=ctx.session_id, user_text="Inspect synthetic material.")
    ctx.ordinary_lead_state = OrdinaryLeadRunState()
    ctx.ordinary_call_identity = ("sub_material", "read-original")
    monkeypatch.setattr("bowhead_core.ordinary_lead_material.builtin_catalog_dir", lambda: case.base.root)
    return ctx


async def test_observation_cannot_bypass_last_dispatch_authorization(assigned_case, monkeypatch):
    from bowhead_core.ordinary_lead_material import OrdinarySkillObserver

    case = assigned_case
    await collect(case, monkeypatch)
    original = OrdinarySkillObserver.invoking

    async def revoke_after_observation(observer, spec, script, inputs, ctx):
        await original(observer, spec, script, inputs, ctx)
        await case.base.sources.instances.upsert(
            user_id=ctx.user_id, name="method", source_type="user_upload", source_path=str(case.root), enabled=False
        )

    monkeypatch.setattr(OrdinarySkillObserver, "invoking", revoke_after_observation)
    assert (await invoke(case))["error_kind"] == "skill_unavailable"
    assert not case.calls


@pytest.mark.parametrize("tool", ["skill_read", "skill_invoke"])
async def test_registered_skill_gate_retains_its_actual_denial(assigned_case, monkeypatch, tool):
    case = assigned_case
    ctx = await collect(case, monkeypatch)
    ctx.skill_runtime_scope = ctx.skill_runtime_grants = None
    ctx.ordinary_call_identity = ("sub_material", "denied-native")
    await case.base.sources.instances.upsert(user_id=ctx.user_id, name="method", source_type="user_upload", source_path=str(case.root), enabled=False)
    arguments = {"name": "method"} if tool == "skill_read" else {"name": "method", "script": "run.py", "input": {}}
    child = await case.registry.execute(tool, arguments, ctx)
    child.call_id = "denied-native"
    assert child.result["error_kind"] == "resource_revoked"
    assert not case.calls
    helper = ToolInvocation("subagent_spawn", {}, result={"subagent_id": "sub_material"}, nested=[child])
    captured = retained_tool_materials(helper, ctx.ordinary_lead_state)
    assert len(captured) == 1 and captured[0]["kind"] == "rejected"
    child.result = {**child.result, "error_kind": "unclassified_failure"}
    with pytest.raises(ValueError):
        retained_tool_materials(helper, ctx.ordinary_lead_state)


async def test_unclassified_read_failure_cannot_be_retained_as_known_rejection(assigned_case, monkeypatch):
    case = assigned_case
    ctx = await collect(case, monkeypatch)

    def interrupted_read(*args):
        raise OSError("Synthetic unclassified storage failure")

    monkeypatch.setattr("bowhead_skill.tools.read_bundle_text", interrupted_read)
    child = await case.registry.execute("skill_read", {"name": "method", "file": "scripts/run.py"}, ctx)
    child.call_id = ctx.ordinary_call_identity[1]
    assert child.error
    helper = ToolInvocation("subagent_spawn", {}, result={"subagent_id": "sub_material"}, nested=[child])
    with pytest.raises(ValueError):
        retained_tool_materials(helper, ctx.ordinary_lead_state)
    assert not case.calls


@pytest.mark.parametrize("file", ["profit.json", "references/../profit.json", "references/missing.json", "scripts/run.py"])
async def test_native_bundle_rejection_retains_exact_call_without_executing(assigned_case, monkeypatch, file):
    case = assigned_case
    ctx = await collect(case, monkeypatch)
    arguments = {"name": "method", "file": file}
    child = await case.registry.execute("skill_read", arguments, ctx)
    child.call_id = ctx.ordinary_call_identity[1]
    helper = ToolInvocation("subagent_spawn", {}, result={"subagent_id": "sub_material"}, nested=[child])
    captured = retained_tool_materials(helper, ctx.ordinary_lead_state)
    assert len(captured) == 1
    assert captured[0]["kind"] == ("skill_read" if file == "scripts/run.py" else "rejected")
    assert not case.calls
    if file != "scripts/run.py":
        assert child.result["error_kind"] == "invalid_args"
        assert "body" not in child.result
    child.arguments = {**arguments, "file": "references/other.json"}
    with pytest.raises(ValueError):
        retained_tool_materials(helper, ctx.ordinary_lead_state)


@pytest.mark.parametrize("change", ["none", "bytes", "disable", "scope", "result", "read_only"])
async def test_native_read_retains_exact_personal_source_and_rechecks_it(assigned_case, monkeypatch, change):
    case = assigned_case
    ctx = await collect(case, monkeypatch)
    arguments = {"name": "method", "args": "audience"}
    ctx._ξ_skill_cache["method|||'audience'"] = {"body": "FOREIGN CACHE"}
    result = await invoke(case, tool="skill_read", args=arguments)
    assert "UPLOADED audience" in result["body"]
    record = ctx.ordinary_lead_state.tool_materials[ctx.ordinary_call_identity]
    assert record["skills"][0]["resource"]["id"] == case.grant.id
    child = ToolInvocation("skill_read", arguments, call_id="read-original", result=result, ended_at="settled")
    helper = ToolInvocation("subagent_spawn", {}, result={"subagent_id": "sub_material"}, nested=[child])
    assert retained_tool_materials(helper, ctx.ordinary_lead_state)[0]["call_id"] == child.call_id
    if change == "bytes":
        (case.root / "asset.bin").write_bytes(b"changed")
    elif change == "disable":
        await case.base.sources.instances.upsert(
            user_id=ctx.user_id, name="method", source_type="user_upload", source_path=str(case.root), enabled=False
        )
    elif change == "scope":
        ctx.workspace_id = "ws_other"
    elif change == "result":
        child.result = {**result, "body": "changed"}
        with pytest.raises(ValueError):
            retained_tool_materials(helper, ctx.ordinary_lead_state)
        return
    elif change == "read_only":
        case.configure(replace(case.grant, permissions=("read",)))
        denied = await invoke(case)
        assert denied.get("ok") is False
        return
    if change == "none":
        await revalidate_skills(ctx, record["skills"])
    else:
        with pytest.raises(ValueError):
            await revalidate_skills(ctx, record["skills"])


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "script_failure",
        "wrapper_failure",
        "unknown_executor",
        "cleanup",
        "input_bytes",
        "deleted_output",
        "output_bytes",
        "subprocess",
        "disabled",
    ],
)
async def test_sandbox_requires_actual_input_and_settled_executor_output(assigned_case, monkeypatch, change):
    case = assigned_case
    ctx = await collect(case, monkeypatch)
    ctx.skill_runtime_scope = ctx.skill_runtime_grants = None
    ctx.ordinary_call_identity = ("sub_material", "sandbox-original")
    store = ArtifactStore(ctx.session_store.db_path, LocalBackend(case.base.root.parent / "artifacts"))
    await store.initialize()
    ctx.artifact_store = store
    source = await store.put_bytes(
        content=b"synthetic input",
        filename="input.txt",
        mime="text/plain",
        source="python_sandbox",
        created_by=ctx.user_id,
        created_by_turn=ctx.turn_id,
        workspace_id=ctx.workspace_id,
    )
    original = await artifact_evidence(ctx, source.id, output=False)
    output_ids = []

    async def run(spec, *, entry_script, input_data, extra_args):
        assert spec.name == "python_sandbox" and entry_script == "runner.py"
        assert input_data["inputs"]["source"]["artifact_id"] == source.id
        output = await store.put_bytes(
            content=b"42\n",
            filename="stdout.txt",
            mime="text/plain",
            source="python_sandbox",
            created_by=ctx.user_id,
            created_by_turn=ctx.turn_id,
            workspace_id=ctx.workspace_id,
        )
        output_ids.append(output.id)
        envelope = {
            "ok": True,
            "execution": {
                "contract": "sandbox-execution.v1",
                "id": "bowhead-sandbox-" + "a" * 24,
                "image_id": "sha256:" + "b" * 64,
                "runtime": "runsc",
                "network": "none",
                "settled": True,
            },
            "input_artifacts": [{"key": "source", **{field: original[field] for field in ("artifact_id", "size", "sha256")}}],
            "outputs": [{"artifact_id": output.id, "name": output.filename, "mime": output.mime, "size": output.size, "data_role": "stdout"}],
        }
        if change in {"unknown_executor", "disabled"}:
            envelope.pop("execution")
        elif change == "cleanup":
            envelope["execution"]["settled"] = False
        elif change == "input_bytes":
            envelope["input_artifacts"][0]["sha256"] = "c" * 64
        elif change == "script_failure":
            envelope.update(ok=False, exit_code=1, error="KeyError: missing column", stderr="KeyError: missing column")
        return SimpleNamespace(
            ok=change != "wrapper_failure",
            exit_code=1 if change == "wrapper_failure" else 0,
            stdout="42",
            stderr="Runner failed" if change == "wrapper_failure" else "",
            result_json=envelope,
            outputs=[],
            workdir=None,
            duration_ms=1,
            error="Runner failed" if change == "wrapper_failure" else None,
        )

    monkeypatch.setattr(case.runtime, "run", run)
    if change == "disabled":
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess" if change == "subprocess" else "executor")
    result = await invoke(case, tool="python_exec", args={"code": "print(42)", "inputs": {"source": {"artifact_id": source.id}}})
    assert result["ok"] is (change not in {"script_failure", "wrapper_failure"})
    assert len(output_ids) == 1
    record = ctx.ordinary_lead_state.tool_materials.get(ctx.ordinary_call_identity)
    if change in {"subprocess", "disabled", "script_failure", "wrapper_failure", "unknown_executor", "cleanup", "input_bytes"}:
        assert record is None
        if change == "script_failure":
            assert result["result_json"]["error"] == "KeyError: missing column"
        return
    assert record is not None and len(record["artifacts"]) == 2
    output = await store.get(output_ids[0])
    assert output.producer_kind == "python_sandbox_stdout_v1"
    if change == "deleted_output":
        await store.soft_delete(output.id, user_id=ctx.user_id)
    elif change == "output_bytes":
        from pathlib import Path

        Path(output.path).write_bytes(b"43\n")
    if change == "none":
        await revalidate_tool_materials(ctx, [record])
    else:
        with pytest.raises(ValueError):
            await revalidate_tool_materials(ctx, [record])


@pytest.mark.parametrize("degraded", [False, True])
async def test_native_list_only_retains_complete_source_observations(assigned_case, monkeypatch, degraded):
    case = assigned_case
    ctx = await collect(case, monkeypatch)
    ctx.skill_runtime_scope = ctx.skill_runtime_grants = None
    ctx.ordinary_call_identity = ("sub_material", "list-original")
    if degraded:

        async def unavailable(**kwargs):
            raise RuntimeError("Synthetic unavailable inventory")

        monkeypatch.setattr(case.base.sources.instances, "list_for_user", unavailable)
    result = await invoke(case, tool="skill_list", args={})
    assert isinstance(result, list) and result
    record = ctx.ordinary_lead_state.tool_materials.get(ctx.ordinary_call_identity)
    if degraded:
        assert record is None
    else:
        assert len(record["skills"]) == len(result)
        await revalidate_skills(ctx, record["skills"])


@pytest.mark.parametrize("change", ["none", "reorder", "delete", "delete_restore", "owner", "other_canvas", "oversized_unicode"])
async def test_canvas_freezes_full_ledger_and_owned_projection(assigned_case, monkeypatch, change):
    ctx = await collect(assigned_case, monkeypatch)
    store = CanvasStore(ctx.session_store.db_path)
    await store.initialize()
    ctx.canvas_store = store
    canvas = await store.ensure_canvas(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    first = await store.append_section(canvas_id=canvas.id, kind="markdown", payload={"text": "First synthetic result"}, by_turn_id=ctx.turn_id)
    second = await store.append_section(canvas_id=canvas.id, kind="markdown", payload={"text": "Second synthetic result"}, by_turn_id=ctx.turn_id)
    saved = await canvas_evidence(ctx)
    if change == "reorder":
        await store.reorder_sections(canvas_id=canvas.id, ordered_ids=[second.id, first.id], by_turn_id="t_competing")
    elif change in {"delete", "delete_restore"}:
        await store.delete_section(canvas_id=canvas.id, section_id=second.id, by_turn_id="t_competing")
        if change == "delete_restore":
            await store.append_section(canvas_id=canvas.id, kind="markdown", payload=second.payload, by_turn_id="t_competing")
    elif change == "owner":
        ctx.user_id = "u_other"
    elif change == "other_canvas":
        other = await store.ensure_canvas(session_id="s_other", workspace_id=ctx.workspace_id, user_id=ctx.user_id)
        await store.append_section(canvas_id=other.id, kind="markdown", payload={"text": "Elsewhere"}, by_turn_id=ctx.turn_id)
    elif change == "oversized_unicode":
        await store.append_section(
            canvas_id=canvas.id, kind="markdown", payload={"text": "\N{CJK UNIFIED IDEOGRAPH-6570}" * 800_000}, by_turn_id=ctx.turn_id
        )
    if change in {"owner", "other_canvas", "oversized_unicode"}:
        with pytest.raises(ValueError):
            await canvas_evidence(ctx)
    else:
        assert (await canvas_evidence(ctx) == saved) is (change == "none")
