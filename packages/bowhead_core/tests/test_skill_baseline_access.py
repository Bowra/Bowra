"""Unselected ordinary SOPs through real admission, exact handles and tools."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.skill_read_access import SkillReadAccessError
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolRegistry
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_reference_claim import restore
from test_skill_reference_runtime import rows
from test_skill_reference_runtime import runtime_case as _runtime_case
from test_skill_reference_tools import access_case as _access_case
from test_skill_reference_tools import call, receiver
from test_skill_reference_tools import claimed_case as _claimed_case
from test_skill_reference_tools import receiver_case as _receiver_case
from test_skill_reference_tools import start_case as _start_case
from test_skill_reference_tools import tool_case as _tool_case

from bowhead_skill import SkillCatalog, SkillRuntime

access_case = _access_case
claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
tool_case = _tool_case
runtime_case = _runtime_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True),
]


@pytest.fixture
async def baseline_case(tool_case):
    _, prepared, rendered = await restore(tool_case.case)
    tool_case.ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=tuple(grant.name for grant in prepared.receiver.grants), invoke_names=())
    tool_case.original_render = rendered
    return tool_case


@pytest.mark.parametrize("path", ["registry", "registered", "raw"])
async def test_sop_reference_and_manifest_use_retained_bytes_without_becoming_required_input(baseline_case, path):
    case = baseline_case
    assert "BASELINE_SOP_unused" not in case.original_render.content.decode()
    shutil.rmtree(case.case.fixture.root)
    result = await call(case, path, args={"name": "unused", "args": ["2026"]})
    assert result["body"] == "BASELINE_SOP_unused 2026"
    assert "selected_method" not in result and "parameters" not in result
    assert result["references"] == ["guide.md"] and result["script_execution"] == "none"
    assert (await call(case, path, args={"name": "unused", "ref": "guide"}))["body"] == "BASELINE_GUIDE_unused"
    assert "read_only_entry" not in (await call(case, path, args={"name": "unused", "file": "SKILL.md"}))["body"]
    with receiver(case.handle.subagent_id):
        assert await case.access.read_model_input(ctx=case.ctx) == case.original_render


async def test_selected_method_parameters_and_baseline_arguments_stay_separate(baseline_case):
    case = baseline_case
    result = await call(case, "registry", args={"name": "method", "args": "2029"})
    assert result["body"] == "NOT_SELECTED_MANIFEST"
    assert result["selected_method"]["parameters"] == {"year": 2026}
    exact = await call(case, "registry", args={"name": "method", "file": "method.md"})
    assert exact["parameters"] == {"year": 2026} and "selected_method" not in exact
    assert "NOT_SELECTED_MANIFEST" not in json.dumps(exact)


async def test_tool_argument_mutation_during_authorization_does_not_change_sop_input(baseline_case, monkeypatch):
    import bowhead_core.skill_read_access as access

    case = baseline_case
    arguments = {"name": "unused", "args": ["2026"]}
    original = access._load_current

    async def interleave(**kwargs):
        result = await original(**kwargs)
        arguments["args"][0] = "2039"
        arguments["name"] = "method"
        return result

    monkeypatch.setattr(access, "_load_current", interleave)
    with receiver(case.handle.subagent_id):
        result = await case.access.read_tool(arguments, case.ctx)
    assert result["name"] == "unused" and result["body"] == "BASELINE_SOP_unused 2026"


@pytest.mark.parametrize("change", ["subagent", "operation", "material", "parameter_source"])
async def test_baseline_handles_cannot_cross_receiver_or_change_operation(baseline_case, change):
    case = baseline_case
    handle = next(item for item in case.access.handles if item.resource.id == "builtin:unused")
    assert handle.parameter_source == "agent-baseline.v1"
    changes = {
        "subagent": {"subagent_id": "helper"},
        "operation": {"operation": "invoke"},
        "material": {"content_hash": "sha256:" + "f" * 64},
        "parameter_source": {"parameter_source": "initial-source.v1"},
    }
    with receiver(case.handle.subagent_id):
        with pytest.raises(SkillReadAccessError):
            await case.access.read(replace(handle, **changes[change]), ctx=case.ctx)
        result = await case.access.read(handle, ctx=case.ctx)
    assert result["body"].startswith("BASELINE_SOP_unused")


async def test_unselected_source_revocation_blocks_all_model_and_tool_uses(baseline_case):
    case = baseline_case
    await case.case.fixture.sources.instances.upsert(
        user_id=case.case.scope.user_id, name="unused", source_type="builtin", source_path=str(case.case.fixture.root / "unused"), enabled=False
    )
    for name in ("unused", "method"):
        result = await call(case, "registry", args={"name": name})
        assert result["error_kind"] == "reference_skill_unavailable" and "BASELINE_SOP" not in json.dumps(result)
    with receiver(case.handle.subagent_id), pytest.raises(SkillReadAccessError):
        await case.access.read_model_input(ctx=case.ctx)


async def test_baseline_read_does_not_grant_invocation(baseline_case):
    case = baseline_case
    assert (await call(case, "registry", args={"name": "unused"}))["body"].startswith("BASELINE_SOP_unused")
    for tool, args in (("skill_invoke", {"name": "unused", "script": "run.py"}), ("python_exec", {"code": "print(1)"})):
        case.ctx.skill_runtime_scope = None
        assert (await call(case, "raw", tool, args))["error_kind"] == "reference_skill_unavailable"


async def test_native_runner_reads_unselected_sop_and_completes_original_execution(runtime_case, monkeypatch):
    case = runtime_case
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[case.base.fixture.root]), SkillRuntime())
    shutil.rmtree(case.base.fixture.root)

    def forbidden(*args, **kwargs):
        raise AssertionError("baseline used current name loader")

    monkeypatch.setattr("bowhead_skill.tools.resolve_skill", forbidden)
    adapter = adapter_for("openai")
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            assert "BASELINE_SOP_unused" not in json.dumps(body)
            assert "BODY_method" in json.dumps(body)
            assert "unused" in "".join(str(item.get("content", "")) for item in body["messages"] if item["role"] == "system")
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "read-baseline",
                                        "type": "function",
                                        "function": {"name": "skill_read", "arguments": '{"name":"unused","args":"2026"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        tool = next(item for item in body["messages"] if item["role"] == "tool")
        assert json.loads(tool["content"])["body"] == "BASELINE_SOP_unused 2026"
        assert body["stream"] is True
        return success("openai", "stream")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert result.ok and route.call_count == 2, (result.error, [[m for m in body["messages"] if m["role"] == "tool"] for body in seen])
        calls = rows(case, "chat_reference_model_invocations")
        assert len(calls) == 2 and all(item["status"] == "completed" for item in calls)
        assert {item["execution_id"] for item in calls} == {case.primary.binding.execution_id}
    finally:
        await adapter.close()
