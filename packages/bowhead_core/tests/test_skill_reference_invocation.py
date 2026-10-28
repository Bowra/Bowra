"""Retained baseline invocation under an exact live reference worker.

Provider/process boundaries are controlled; source stores, admission, claim,
reference access, registry, runner and model receipts are real implementations.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolRegistry
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from bowhead_skill.tools import build_skill_tools
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_access import access_case as _access_case
from test_skill_read_access import receiver
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case
from test_skill_reference_runtime import expire, rows
from test_skill_reference_runtime import runtime_case as _runtime_case

from bowhead_skill import SkillCatalog, SkillRuntime

start_case = _start_case
receiver_case = _receiver_case
claimed_case = _claimed_case
access_case = _access_case
runtime_case = _runtime_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


def tools_for(root, monkeypatch):
    catalog, runtime, calls = SkillCatalog(roots=[root]), SkillRuntime(), []

    async def run(spec, *, entry_script, input_data, extra_args):
        source = Path(input_data["_skill_dir"]) if "_skill_dir" in input_data else spec.source_dir
        source_script = input_data.get("_skill_entry") or entry_script
        calls.append((spec.name, source, (source / "scripts" / source_script).read_bytes(), input_data.copy()))
        return SimpleNamespace(
            ok=True, exit_code=0, stdout="CALCULATED 42", stderr="", result_json={"answer": 42}, outputs=[], workdir=None, duration_ms=1, error=None
        )

    monkeypatch.setattr(runtime, "run", run)
    registry = ToolRegistry()
    register_skill_tools(registry, catalog, runtime)
    return registry, calls, {item.name: item.handler for item in build_skill_tools(catalog, runtime)}


@pytest.fixture
async def invoke_case(access_case, monkeypatch):
    case = access_case
    composition, prepared, _ = await restore(case.case)
    assert composition.runtime_policy == "initial-primary-skill-baseline.v1"
    case.ctx.reference_skill_required, case.ctx.reference_skill_access = True, case.access
    case.ctx.skill_runtime_grants = prepared.receiver.grants
    case.ctx.skill_runtime_scope = ExactSkillRuntimeScope(
        read_names=tuple(sorted(grant.name for grant in prepared.receiver.grants)),
        invoke_names=tuple(sorted(grant.name for grant in prepared.receiver.grants if "invoke" in grant.permissions)),
    )
    if case.ctx.skill_runtime_grants[-1].kind == "team":
        sandbox = case.case.fixture.root / "python_sandbox"
        (sandbox / "scripts").mkdir(parents=True)
        (sandbox / "SKILL.md").write_text("---\nname: python_sandbox\ndescription: transport\n---\ntransport\n")
        (sandbox / "scripts/runner.py").write_text("print('transport')\n")
    case.registry, case.calls, case.raw = tools_for(case.case.fixture.root, monkeypatch)
    return case


async def call(case, tool="skill_invoke", arguments=None, *, raw=False):
    arguments = arguments if arguments is not None else {"name": "unused", "script": "runner.py", "input": {}}
    with receiver(case.handle.subagent_id):
        return await (case.raw[tool] if raw else case.registry.get(tool).handler)(arguments, case.ctx)


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_only_original_invocable_baseline_package_can_execute(invoke_case):
    case = invoke_case
    original = (case.case.fixture.root / "unused/scripts/runner.py").read_bytes()
    (case.case.fixture.root / "unused/scripts/runner.py").unlink()
    read = await call(case, "skill_read", {"name": "unused"})
    assert read["script_execution"] == "builtin"
    result = await call(case)
    assert result["ok"] is True and result["result_json"] == {"answer": 42}
    assert case.calls[0][0] == "unused" and case.calls[0][2] == original
    assert not case.calls[0][1].exists()
    method = await call(case, "skill_read", {"name": "method"})
    assert method["selected_method"]["parameters"] == {"year": 2026}
    assert "CALCULATED" not in method["body"]
    denied = await call(case, arguments={"name": "method", "script": "runner.py", "input": {}})
    assert "error" in denied and len(case.calls) == 1


@pytest.mark.parametrize("change", ["receiver", "generation", "grants", "expired", "raw"])
@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_missing_current_binding_never_enters_the_process(invoke_case, change):
    case = invoke_case
    if change == "receiver":
        case.ctx.turn_id = "another_turn"
    elif change == "generation":
        from dataclasses import replace

        case.ctx.reference_skill_access = replace(case.access, generation=2)
    elif change == "grants":
        case.ctx.skill_runtime_grants = ()
    elif change == "expired":
        expire(case.case)
    assert "error" in await call(case, raw=change == "raw")
    assert not case.calls


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_revoke_during_handler_preparation_blocks_start(invoke_case, monkeypatch):
    case = invoke_case
    roots = []

    async def interleave(name, input_data, ctx):
        roots.append(ctx.assigned_skill_material.spec.source_dir)
        await case.case.fixture.sources.instances.upsert(
            user_id=case.ctx.user_id, name="unused", source_type="builtin", source_path=str(case.case.fixture.root / "unused"), enabled=False
        )

    monkeypatch.setattr("bowhead_skill.tools._inject_current_canvas_sections", interleave)
    result = await call(case)
    assert "error" in result and not case.calls
    assert len(roots) == 1 and not roots[0].exists()


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_actual_primary_loop_invokes_retained_baseline_and_settles_model_receipts(runtime_case, monkeypatch):
    case = runtime_case
    registry, calls, _ = tools_for(case.base.fixture.root, monkeypatch)
    original = (case.base.fixture.root / "unused/scripts/runner.py").read_bytes()
    (case.base.fixture.root / "unused/scripts/runner.py").unlink()
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return httpx2.Response(
                200,
                json={
                    "id": "invoke-baseline",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-baseline",
                                        "type": "function",
                                        "function": {
                                            "name": "skill_invoke",
                                            "arguments": json.dumps({"name": "unused", "script": "runner.py", "input": {}}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )
        assert "CALCULATED 42" in json.dumps(body)
        return success("openai", "after-baseline")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert result.ok and result.final_answer == "OK" and route.call_count == 2
        assert len(calls) == 1 and calls[0][2] == original and not calls[0][1].exists()
        assert len(rows(case, "chat_reference_model_contexts")) == 2
        assert [row["status"] for row in rows(case, "chat_reference_model_invocations")] == ["completed", "completed"]
        assert [row["kind"] for row in rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
        assert case.ctx.skill_runtime_grants is None and case.ctx.assigned_skill_material is None
    finally:
        await adapter.close()


@pytest.mark.parametrize("receiver_case", ["admission_team_unused_invoke"], indirect=True)
@pytest.mark.parametrize("flags", [("0", "0"), ("0", "1"), ("1", "0"), ("1", "1")])
async def test_uploaded_original_package_uses_sandbox_and_parent_flag(invoke_case, monkeypatch, flags):
    case = invoke_case
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", flags[0])
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", flags[1])
    original = (case.case.fixture.root / "unused/scripts/runner.py").read_bytes()
    (case.case.fixture.root / "unused/scripts/runner.py").unlink()
    result = await call(case)
    if flags[0] == "0":
        assert "error" in result and not case.calls
    else:
        assert result["ok"] is True
        assert case.calls[0][0] == "python_sandbox" and case.calls[0][2] == original
        assert case.calls[0][3]["_skill_entry"] == "runner.py" and not case.calls[0][1].exists()


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_worker_expires_during_preparation_before_process_start(invoke_case, monkeypatch):
    case = invoke_case
    roots = []

    async def interleave(name, input_data, ctx):
        roots.append(ctx.assigned_skill_material.spec.source_dir)
        expire(case.case)

    monkeypatch.setattr("bowhead_skill.tools._inject_current_canvas_sections", interleave)
    assert "error" in await call(case)
    assert not case.calls and len(roots) == 1 and not roots[0].exists()


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_existing_read_only_policy_never_acquires_invocation(access_case):
    case = access_case
    composition, prepared, _ = await restore(case.case)
    assert composition.runtime_policy == "initial-primary-skill-read.v2"
    case.ctx.reference_skill_required, case.ctx.reference_skill_access = True, case.access
    case.ctx.skill_runtime_grants = prepared.receiver.grants
    case.ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("method", "support"), invoke_names=("method",))
    calls = []

    async def handler(arguments, ctx):
        calls.append(arguments)

    with receiver(case.handle.subagent_id):
        result = await case.access.execute_tool("skill_invoke", handler, {"name": "method", "script": "runner.py"}, case.ctx)
    assert result["error_kind"] == "reference_skill_unavailable" and not calls


@pytest.mark.parametrize("receiver_case", ["admission_team_unused_invoke"], indirect=True)
async def test_parent_flag_closed_during_last_authority_read_blocks_process(invoke_case, monkeypatch):
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    case = invoke_case
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    prepared = False
    original = SkillReadSourceResolver.revalidate_in_connection

    async def interleave(name, input_data, ctx):
        nonlocal prepared
        prepared = True

    async def revalidate(self, db, proof):
        await original(self, db, proof)
        if prepared:
            monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "0")

    monkeypatch.setattr("bowhead_skill.tools._inject_current_canvas_sections", interleave)
    monkeypatch.setattr(SkillReadSourceResolver, "revalidate_in_connection", revalidate)
    assert (await call(case)).get("error") and prepared and not case.calls


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_handler_failure_preserves_unknown_outcome_and_releases_material(invoke_case):
    case = invoke_case
    contexts = []
    failure = RuntimeError("process transport interrupted")

    async def handler(arguments, ctx):
        contexts.append(ctx)
        raise failure

    with receiver(case.handle.subagent_id), pytest.raises(RuntimeError) as caught:
        await case.access.execute_tool("skill_invoke", handler, {"name": "unused", "script": "runner.py"}, case.ctx)
    assert caught.value is failure
    assert len(contexts) == 1 and not contexts[0].assigned_skill_material.active
    assert not contexts[0].assigned_skill_material.spec.source_dir.exists()


@pytest.mark.parametrize("receiver_case", ["admission_sandbox_unused_invoke"], indirect=True)
@pytest.mark.parametrize("tool", ["skill_invoke", "python_exec"])
async def test_exact_original_sandbox_uses_both_existing_entrypoints(invoke_case, tool):
    case = invoke_case
    original = (case.case.fixture.root / "python_sandbox/scripts/runner.py").read_bytes()
    (case.case.fixture.root / "python_sandbox/scripts/runner.py").unlink()
    arguments = {"code": "print(42)", "inputs": {}}
    if tool == "skill_invoke":
        arguments = {"name": "python_sandbox", "script": "runner.py", "input": arguments}
    result = await call(case, tool, arguments)
    assert result["ok"] is True and len(case.calls) == 1
    assert case.calls[0][2] == original and not case.calls[0][1].exists()


@pytest.mark.parametrize("receiver_case", ["admission_package_assets_unused_invoke"], indirect=True)
async def test_read_only_package_with_scripts_cannot_borrow_other_baseline_invoke(invoke_case):
    case = invoke_case
    assert (case.case.fixture.root / "method/scripts/runner.py").exists()
    # Even an overbroad generic name scope must not override the retained grant.
    case.ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("method", "support", "unused"), invoke_names=("method", "unused"))
    result = await call(case, arguments={"name": "method", "script": "runner.py", "input": {}})
    assert "error" in result and not case.calls


@pytest.mark.parametrize("receiver_case", ["admission_team_unused_invoke"], indirect=True)
@pytest.mark.parametrize("flags", [("0", "0"), ("0", "1"), ("1", "0"), ("1", "1")])
async def test_uploaded_command_requires_both_original_runtime_flags(invoke_case, monkeypatch, flags):
    case = invoke_case
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", flags[0])
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", flags[1])
    result = await call(case, arguments={"name": "unused", "command": ["python3", "scripts/runner.py"], "input": {}})
    if flags == ("1", "1"):
        assert result["ok"] is True and len(case.calls) == 1 and case.calls[0][0] == "python_sandbox"
        assert not case.calls[0][1].exists()
    else:
        assert "error" in result and not case.calls


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_worker_expiry_during_last_source_read_blocks_process(invoke_case, monkeypatch):
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    case = invoke_case
    prepared = False
    original = SkillReadSourceResolver.revalidate_in_connection

    async def interleave(name, input_data, ctx):
        nonlocal prepared
        prepared = True

    async def revalidate(self, db, proof):
        await original(self, db, proof)
        if prepared:
            await db.execute(
                "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )

    monkeypatch.setattr("bowhead_skill.tools._inject_current_canvas_sections", interleave)
    monkeypatch.setattr(SkillReadSourceResolver, "revalidate_in_connection", revalidate)
    assert (await call(case)).get("error") and prepared and not case.calls
