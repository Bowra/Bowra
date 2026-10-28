"""Prompt task material keeps the same published Skill operation baseline."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import ReferenceNode
from bowhead_core.events import TurnEventBus
from bowhead_core.mcp_prompt_expansion import prepare_mcp_prompt_expansion
from bowhead_core.reference_mcp_metadata import build_reference_mcp_metadata
from bowhead_core.reference_mcp_prompt_execution import claim_reference_mcp_prompt_primary_for_worker
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from cryptography.fernet import Fernet
from test_reference_mcp_metadata import _insert_source
from test_reference_mcp_preparation import _document
from test_reference_mcp_prompt_materialization import _remote_result
from test_reference_start_preparation import activate, prepare, raw_document
from test_skill_extension_runtime import tool_response
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

from bowhead_skill import SkillCatalog, SkillRuntime

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.fixture
async def prompt_baseline(receiver_case):
    fixture = receiver_case
    activate(fixture)
    case = fixture.case
    path = case.fixture.db_path
    _insert_source(
        path,
        team_id=fixture.scope.team_id,
        source_id="ots_prompt_baseline",
        kind="mcp",
        tools=[],
        prompts=[{"name": "profitability", "title": "Profitability", "description": "Analyze profitability", "arguments": []}],
        created_by=case.fixture.owner_id,
    )
    index = await build_reference_mcp_metadata(path)
    prompt = index.for_team(fixture.scope.team_id)[0].prompts[0].resource
    agent = next(node.resource for node in raw_document(fixture, skills=False).nodes if isinstance(node, ReferenceNode))
    document = _document(agent, (prompt, "Profitability", {}))
    access = AccessControlStore(path)
    admission = await prepare(fixture, document=document, mcp_index=index, mcp_access=access)
    case.args.update(reference_start=admission, user_text=document.display_text())
    receipt = await case.store.finalize_started_chat_submission(**case.args)
    assert receipt.outcome == "started"
    source = await case.store.get_chat_turn_user_source(turn_id="t_reference", user_id=fixture.scope.user_id, workspace_id=fixture.scope.workspace_id)
    scope = fixture.scope.model_copy(update={"session_id": source.session_id})
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    args = {
        "index": index,
        "access": access,
        "scope": scope,
        "turn_id": source.turn_id,
        "owner_id": "prompt-baseline-worker",
        "expected_generation": 1,
        "sources": fixture.sources,
        "cipher": cipher,
    }
    calls = []

    async def fetch(resource, parameters, source_revision):
        calls.append(resource.prompt_name)
        return prepare_mcp_prompt_expansion(resource=resource, parameters=parameters, result=_remote_result("PROMPT_MATERIAL"))

    runtime = ReferencePrimaryRuntime(
        scope=ReferenceScope(**scope.model_dump(), policy_epoch=admission.policy_epoch),
        turn_id=source.turn_id,
        owner_id=args["owner_id"],
        bindings=case.bindings,
        cipher=cipher,
        skill_sources=fixture.sources,
        mcp_index=index,
        mcp_access=access,
        mcp_prompt_fetch=fetch,
    )
    ctx = ToolExecutionContext(**runtime.scope.identity(), turn_id=source.turn_id, member_uid=scope.user_id, datarc=None, session_store=case.store)
    return SimpleNamespace(fixture=fixture, path=path, args=args, runtime=runtime, ctx=ctx, calls=calls)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_prompt_claim_requires_baseline_before_owning_worker(prompt_baseline):
    case = prompt_baseline
    with pytest.raises(ValueError, match="reference_mcp_prompt_skill_baseline_unavailable"):
        await claim_reference_mcp_prompt_primary_for_worker(**{**case.args, "sources": None})
    claim = await claim_reference_mcp_prompt_primary_for_worker(**case.args)
    assert claim.outcome == "claimed"
    assert case.calls == []


@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
async def test_prompt_native_runner_reads_original_baseline_package(prompt_baseline, monkeypatch):
    case = prompt_baseline
    await claim_reference_mcp_prompt_primary_for_worker(**case.args)
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[case.fixture.root]), SkillRuntime())
    # Reading must consume the exact retained bytes, including after the
    # original filesystem package is removed and a stale name cache exists.
    (case.fixture.root / "unused/references/guide.md").unlink()
    assert (await claim_reference_mcp_prompt_primary_for_worker(**case.args)).outcome == "already_running"

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy name lookup")

    monkeypatch.setattr("bowhead_skill.tools.resolve_skill", forbidden)
    adapter = adapter_for("openai")
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "PROMPT_MATERIAL" in json.dumps(body)
        if len(calls) == 1:
            return tool_response("skill_read", {"name": "unused", "ref": "guide"})
        tool = next(message for message in body["messages"] if message["role"] == "tool")
        assert "BASELINE_GUIDE_unused" in tool["content"] and "error_kind" not in tool["content"]
        return success("openai", "stream" if body.get("stream") else "invoke")

    try:
        runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.fixture.case.fixture.users)
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        assert result.ok and len(calls) == 2 and case.calls == ["profitability"]
        assert case.ctx.reference_skill_access is None
    finally:
        await adapter.close()


@pytest.mark.parametrize("receiver_case", ["admission", "admission_selected_invoke"], indirect=True)
async def test_prompt_native_invocation_preserves_exact_original_operation_grant(prompt_baseline, monkeypatch):
    from test_skill_reference_invocation import tools_for

    case = prompt_baseline
    await claim_reference_mcp_prompt_primary_for_worker(**case.args)
    registry, executions, _ = tools_for(case.fixture.root, monkeypatch)
    permitted = case.fixture.mode.endswith("selected_invoke")
    if permitted:
        (case.fixture.root / "method/scripts/runner.py").write_text("print('CHANGED_PACKAGE')\n")
    adapter = adapter_for("openai")
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return tool_response("skill_invoke", {"name": "method", "script": "runner.py", "input": {}}, stream=bool(body.get("stream")))
        tool = next(message for message in body["messages"] if message["role"] == "tool")
        assert ("CALCULATED 42" in tool["content"]) == permitted
        if not permitted:
            assert "not in subagent 'exact-agent' whitelist" in tool["content"]
        return success("openai", "stream" if body.get("stream") else "invoke")

    try:
        runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.fixture.case.fixture.users)
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        assert result.ok and len(sent) == 2 and len(executions) == int(permitted)
        if permitted:
            assert b"fixture script must never run" in executions[0][2]
            assert not executions[0][1].exists()
    finally:
        await adapter.close()


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["revoke", "purge", "wrong_store"])
async def test_unavailable_baseline_blocks_prompt_expansion_and_model(prompt_baseline, change, tmp_path):
    import sqlite3
    from dataclasses import replace

    from bowhead_core.reference_mcp_prompt_execution import MCP_PROMPT_SKILL_BASELINE_KEY

    case = prompt_baseline
    await claim_reference_mcp_prompt_primary_for_worker(**case.args)
    runtime = case.runtime
    if change == "revoke":
        await case.args["access"].create_or_update_resource_grant(
            team_id=runtime.scope.team_id,
            resource_kind="skill",
            resource_id="support",
            principal_type="user",
            principal_id=runtime.scope.user_id,
            permission="use",
            effect="deny",
        )
    elif change == "purge":
        with sqlite3.connect(case.path) as db:
            db.execute(
                "UPDATE chat_reference_content_snapshots SET retention_state='purged',ciphertext=NULL WHERE content_key=?",
                (MCP_PROMPT_SKILL_BASELINE_KEY,),
            )
    else:
        from bowhead_core.skill_read_resolution import SkillReadSourceResolver

        runtime = replace(runtime, skill_sources=SkillReadSourceResolver(tmp_path / "wrong.sqlite", access_factory=lambda: None))
    with pytest.raises(ValueError):
        await runtime.run(runner=None, parent_ctx=case.ctx, parent_bus=TurnEventBus())
    assert case.calls == []
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT count(*) FROM chat_reference_model_invocations WHERE turn_id='t_reference'").fetchone() == (0,)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_baseline_write_failure_rolls_back_entire_worker_claim(prompt_baseline, monkeypatch):
    import sqlite3

    from bowhead_core.chat_reference_storage import ChatReferenceStore

    case = prompt_baseline
    original = ChatReferenceStore.put_content_in_connection

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("controlled snapshot failure")

    monkeypatch.setattr(ChatReferenceStore, "put_content_in_connection", fail_after_write)
    with pytest.raises(OSError, match="controlled snapshot failure"):
        await claim_reference_mcp_prompt_primary_for_worker(**case.args)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == ("admitted",)
        assert db.execute("SELECT owner_id FROM chat_turn_spawn_dispatch WHERE turn_id='t_reference'").fetchone() == (None,)
        assert db.execute("SELECT count(*) FROM chat_reference_content_snapshots WHERE turn_id='t_reference'").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM chat_reference_resolutions WHERE turn_id='t_reference'").fetchone() == (0,)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_prompt_accessor_cannot_promote_read_grant_to_invoke(prompt_baseline):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.reference_mcp_prompt_execution import restore_reference_mcp_prompt_composition_in_connection
    from bowhead_core.reference_mcp_prompt_model import ReferenceMcpPromptModelAccess
    from bowhead_core.skill_read_receiver import exact_reference_skill_grants
    from test_skill_model_access import receiver

    case = prompt_baseline
    claim = await claim_reference_mcp_prompt_primary_for_worker(**case.args)
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        composition = await restore_reference_mcp_prompt_composition_in_connection(db, primary=claim.primary, scope=case.runtime.scope, generation=1)
    access = ReferenceMcpPromptModelAccess(
        index=case.args["index"],
        access=case.args["access"],
        cipher=case.args["cipher"],
        scope=case.runtime.scope,
        turn_id=case.runtime.turn_id,
        owner_id=case.runtime.owner_id,
        generation=1,
        execution_id=composition.execution_id,
        composition_digest=composition.digest,
        sources=case.fixture.sources,
    )
    case.ctx.reference_skill_required = True
    case.ctx.reference_skill_access = access
    case.ctx.skill_runtime_grants = exact_reference_skill_grants(claim.primary.binding.definition)
    with receiver(access.subagent_id), pytest.raises(ValueError, match="reference_skill_operation_unavailable"):
        await access.authorize_tool("skill_invoke", {"name": "method", "script": "runner.py", "input": {}}, case.ctx)
