"""Real exact access through registry, registered cache wrapper and raw tools.

The context is explicitly bound here. No production runtime/approval producer is
opened by these tests; ordinary baseline behavior has its existing regressions.
"""

from __future__ import annotations

import copy
import json

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMChunk, LLMResponse, ToolCall
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from bowhead_skill.tools import build_skill_tools
from test_skill_read_access import access_case as _access_case
from test_skill_read_access import claimed_case as _claimed_case
from test_skill_read_access import receiver
from test_skill_read_access import receiver_case as _receiver_case
from test_skill_read_access import start_case as _start_case
from test_skill_read_model_input import Registry
from test_skill_reference_claim import legacy_skill_composition as _legacy_skill_composition

from bowhead_skill import SkillCatalog, SkillRuntime

access_case = _access_case
claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
legacy_skill_composition = _legacy_skill_composition
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.fixture
async def tool_case(access_case, monkeypatch):
    case = access_case
    catalog = SkillCatalog(roots=[case.case.fixture.root])
    registry = ToolRegistry()
    register_skill_tools(registry, catalog, SkillRuntime())
    raw = {tool.name: tool.handler for tool in build_skill_tools(catalog, SkillRuntime())}
    case.ctx.reference_skill_required = True
    case.ctx.reference_skill_access = case.access
    case.ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("method", "support"), invoke_names=())
    case.registry, case.raw = registry, raw
    case.ctx._ξ_skill_cache["method|||''"] = {"body": "FOREIGN_NAME_CACHE"}

    def forbidden(*args, **kwargs):
        raise AssertionError("exact read must not fall back to a name resolver")

    monkeypatch.setattr("bowhead_skill.tools.resolve_skill", forbidden)
    monkeypatch.setattr("bowhead_core.resource_execution_gate.check_skill_execution", forbidden)
    return case


async def call(case, path, name="skill_read", args=None, *, ctx=None):
    context = ctx or case.ctx
    arguments = {"name": "method"} if args is None else args
    with receiver(case.handle.subagent_id):
        if path == "registry":
            invocation = await case.registry.execute(name, arguments, context)
            return invocation.result
        handler = case.raw[name] if path == "raw" else case.registry.get(name).handler
        return await handler(arguments, context)


@pytest.mark.parametrize("path", ["registry", "registered", "raw"])
async def test_real_tools_read_exact_saved_bytes_and_never_reuse_name_cache(tool_case, path):
    case = tool_case
    cache = copy.deepcopy(case.ctx._ξ_skill_cache)
    (case.case.fixture.root / "method" / "method.md").write_text("CHANGED_LIVE_PACKAGE")
    first = await call(case, path)
    assert first["body"] == "NOT_SELECTED_MANIFEST"
    assert first["selected_method"]["files"][0]["text"] == "BODY_method"
    assert first["selected_method"]["parameters"] == {"year": 2026}
    assert first["resource"] == case.handle.resource.model_dump(mode="json")
    assert "FOREIGN_NAME_CACHE" not in json.dumps(first) and not first.get("_cached")
    first["selected_method"]["files"][0]["text"] = "MUTATED_RETURN"
    assert (await call(case, path))["selected_method"]["files"][0]["text"] == "BODY_method"
    await case.case.fixture.sources.instances.upsert(
        user_id=case.case.scope.user_id, name="support", source_type="builtin", source_path=str(case.case.fixture.root / "support"), enabled=False
    )
    denied = await call(case, path)
    assert denied["error_kind"] == "reference_skill_unavailable"
    assert "BODY_method" not in json.dumps(denied)
    assert case.ctx._ξ_skill_cache == cache


@pytest.mark.usefixtures("legacy_skill_composition")
async def test_legacy_v1_tool_contract_remains_method_only(tool_case):
    result = await call(tool_case, "raw")
    assert result["files"][0]["text"] == "BODY_method" and result["parameters"] == {"year": 2026}
    assert "body" not in result and "selected_method" not in result
    for arguments in ({"name": "method", "file": "SKILL.md"}, {"name": "method", "args": "2029"}):
        assert (await call(tool_case, "raw", args=arguments))["error_kind"] == "reference_skill_unavailable"


@pytest.mark.parametrize("path", ["registry", "registered", "raw"])
@pytest.mark.parametrize("state", ["missing", "malformed", "flag_false", "flag_malformed"])
async def test_missing_or_inconsistent_required_context_cannot_fall_back(tool_case, path, state):
    case = tool_case
    if state == "missing":
        case.ctx.reference_skill_access = None
    elif state == "malformed":
        case.ctx.reference_skill_access = {"name": "method"}
    elif state == "flag_false":
        case.ctx.reference_skill_required = False
    else:
        case.ctx.reference_skill_required = 1
    assert (await call(case, path))["error_kind"] == "reference_skill_unavailable"


@pytest.mark.parametrize("path", ["registry", "registered", "raw"])
@pytest.mark.parametrize(
    "name,args", [("skill_list", {}), ("skill_invoke", {"name": "method", "script": "run.py"}), ("python_exec", {"code": "print(1)"})]
)
async def test_read_context_never_becomes_a_script_or_discovery_permission(tool_case, path, name, args):
    case = tool_case
    # Exercise the exact gate even when the earlier name scope is absent.
    case.ctx.skill_runtime_scope = None
    assert (await call(case, path, name, args))["error_kind"] == "reference_skill_unavailable"


@pytest.mark.parametrize(
    "args",
    [
        {"name": "other"},
        {"name": "method", "args": {"year": 2030}},
        {"name": "method", "ref": "unselected.md"},
        {"name": "method", "file": "../SKILL.md"},
        {"name": "method", "file": True},
        {"name": "method", "new_parameter": 1},
    ],
)
async def test_model_arguments_cannot_select_another_package_or_rebind_parameters(tool_case, args):
    tool_case.ctx.skill_runtime_scope = None
    assert (await call(tool_case, "raw", args=args))["error_kind"] == "reference_skill_unavailable"


async def test_registry_unwraps_arguments_before_exact_lookup_and_keeps_role_checks(tool_case):
    case = tool_case
    result = await call(case, "registry", args={"raw_arguments": '{"name":"method","file":"method.md"}'})
    assert result["files"][0]["text"] == "BODY_method"
    case.ctx.role = "not-allowed"
    with receiver(case.handle.subagent_id):
        invocation = await case.registry.execute("skill_read", {"name": "method"}, case.ctx)
    assert invocation.error and invocation.result is None


async def test_existing_runner_reaches_the_real_exact_handler(tool_case):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection

    case = tool_case
    async with open_shared_ctx(case.case.path) as db:
        await db.execute("BEGIN")
        primary = await read_reference_skill_primary_in_connection(
            db, scope=case.access.scope, turn_id=case.access.turn_id, bindings=case.case.fixture.case.bindings
        )
        await db.rollback()
    seen = []

    class Adapter:
        async def invoke(self, request):
            seen.append(request)
            if len(seen) == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="exact-read", name="skill_read", arguments={"name": "method"})])
            return LLMResponse(content="已按原方法完成分析。")

        async def stream(self, request):
            response = await self.invoke(request)
            yield LLMChunk(delta=response.content, finish_reason="stop")

    router = Router(Registry(primary=Adapter()))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "test-model")))
    runner = SubagentRunner(router=router, tool_registry=case.registry, team_subagent_store=case.case.fixture.case.fixture.users)
    result = await runner.run(
        profile_name=primary.binding.runtime_profile_name,
        task=primary.source.input_document.text_content(),
        parent_turn_id=case.access.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=case.ctx,
        exact_binding=primary.binding,
        pre_minted_subagent_id=case.handle.subagent_id,
    )
    assert result.ok and len(seen) == 2
    tool_messages = [message for message in seen[1].messages if message.role == "tool"]
    assert len(tool_messages) == 1
    body = json.loads(tool_messages[0].content)
    assert body["body"] == "NOT_SELECTED_MANIFEST"
    assert body["selected_method"]["files"][0]["text"] == "BODY_method"
    assert body["selected_method"]["parameters"] == {"year": 2026}
    assert "FOREIGN_NAME_CACHE" not in tool_messages[0].content


@pytest.mark.parametrize("unsupported", ["invoke", "outside_read"])
async def test_read_tool_adapter_refuses_to_downgrade_the_skill_baseline(tool_case, monkeypatch, unsupported):
    from dataclasses import replace

    import bowhead_core.skill_read_access as module

    original = module._load_current

    async def invocation_baseline(**kwargs):
        composition, rendered, prepared = await original(**kwargs)
        # Fault at the adapter's input boundary, after the real stored read
        # baseline has been verified. This is not an invocation release fixture.
        if unsupported == "invoke":
            first = replace(prepared.receiver.grants[0], permissions=("invoke", "read"))
            grants = (first, *prepared.receiver.grants[1:])
        else:
            additional = replace(prepared.receiver.grants[0], name="outside", id="builtin:outside")
            grants = (*prepared.receiver.grants, additional)
        prepared = replace(prepared, receiver=prepared.receiver.model_copy(update={"grants": grants}))
        return composition, rendered, prepared

    monkeypatch.setattr(module, "_load_current", invocation_baseline)
    assert (await call(tool_case, "raw")).get("error_kind") == "reference_skill_unavailable"


@pytest.mark.parametrize("propagate", [False, True])
async def test_revocation_after_registry_check_is_a_failed_read_not_unknown_effect(tool_case, propagate):
    case = tool_case
    spec = case.registry.get("skill_read")
    handler = spec.handler

    async def revoke(arguments, ctx):
        await case.case.fixture.sources.instances.upsert(
            user_id=case.case.scope.user_id, name="support", source_type="builtin", source_path=str(case.case.fixture.root / "support"), enabled=False
        )
        return await handler(arguments, ctx)

    spec.handler = revoke
    with receiver(case.handle.subagent_id):
        invocation = await case.registry.execute("skill_read", {"name": "method"}, case.ctx, propagate_ambiguous_transport=propagate)
    assert invocation.error == "ToolPermissionError: reference_skill_unavailable"
    assert invocation.result["error_kind"] == "reference_skill_unavailable"
    assert "BODY_method" not in json.dumps(invocation.result)


async def test_registry_rejects_missing_access_before_entering_an_alternate_handler(tool_case):
    case = tool_case
    calls = []

    async def handler(arguments, ctx):
        calls.append(arguments)
        return {"files": []}

    case.registry.get("skill_read").handler = handler
    case.ctx.reference_skill_access = None
    with receiver(case.handle.subagent_id):
        invocation = await case.registry.execute("skill_read", {"name": "method"}, case.ctx)
    assert invocation.error == "ToolPermissionError: reference_skill_unavailable" and calls == []


async def test_malformed_reference_arguments_do_not_log_private_input(tool_case, caplog):
    case = tool_case
    case.ctx.skill_runtime_scope = None
    private = "<PRIVATE_METHOD_PARAMETER_NOT_JSON>"
    with receiver(case.handle.subagent_id):
        invocation = await case.registry.execute("skill_read", {"raw_arguments": private}, case.ctx)
    assert invocation.error == "ToolPermissionError: reference_skill_unavailable"
    assert "PRIVATE_METHOD_PARAMETER" not in caplog.text
    assert "PRIVATE_METHOD_PARAMETER" not in json.dumps(invocation.arguments)
