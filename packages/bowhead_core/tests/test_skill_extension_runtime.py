from legacy_agent_release_fixture import seed_legacy_publication

"""Actual admission/claim/native Agent loop; provider/process responses are controlled.

Publication rows use the existing exact release fixture and production policy
proof writer. This does not claim public HTTP readiness or real business Eval.
"""

import json
import sqlite3

import httpx2
import pytest
import respx
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.skill_extension_material import prepare_skill_extension, restore_skill_extension
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import SubagentRunner
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case
from test_skill_reference_invocation import tools_for
from test_skill_reference_runtime import rows
from test_skill_reference_runtime import runtime_case as _runtime_case

start_case, receiver_case, claimed_case, runtime_case = _start_case, _receiver_case, _claimed_case, _runtime_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_extension_unused_invoke"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["extension"], indirect=True),
]


async def test_actual_claim_retains_extension_separately_and_restores_original_baseline(claimed_case):
    case = claimed_case
    claim = await claim_reference_skill_primary_for_worker(**case.args)
    assert claim.outcome == "claimed"
    composition, prepared, rendered = await restore(case)
    assert composition.runtime_policy == "initial-primary-skill-extension.v1"
    assert composition.content.content_kind == "skill_extension"
    assert {item.resource.id: item.provenance for item in composition.members} == {
        "builtin:optional_method": "readonly_extension",
        "builtin:support": "baseline",
    }
    assert {item.proof.name for item in prepared.baseline.packages} == {"method", "support", "unused"}
    assert "optional_method" not in {grant.name for grant in prepared.receiver.grants}
    assert b"BODY_optional_method" in rendered.content and b"fixture script must never run" not in rendered.content
    assert prepared.extension.policy.digest == composition.extension_policy_digest
    snapshot = prepare_skill_extension(prepared)
    (case.fixture.root / "optional_method/method.md").unlink()
    _, restored, again = await restore(case)
    assert restored == prepared and again == rendered
    changed = json.loads(snapshot.content)
    changed["extension"]["policy"]["skill_read"]["resource_allowlist"][0]["id"] = "builtin:other"
    from bowhead_core.skill_read_material import _hash, _json

    raw = _json(changed)
    with pytest.raises(ValueError, match="extension_material_unavailable"):
        restore_skill_extension(raw, receiver=prepared.receiver, content_hash=_hash(raw))


def tool_response(name, args, *, stream=False):
    if stream:
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call-" + name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n",
        )
    return httpx2.Response(
        200,
        json={
            "id": "controlled-tool",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-" + name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


async def test_native_loop_reads_optional_method_and_only_invokes_original_package(runtime_case, monkeypatch):
    case = runtime_case
    registry, calls, _ = tools_for(case.base.fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
    sent, observed = [], []

    def respond(request):
        sent.append(json.loads(request.content))
        observed.append((rows(case, "chat_reference_extension_checks"), rows(case, "chat_reference_model_invocations")))
        if len(sent) == 1:
            return tool_response("skill_read", {"name": "optional_method", "file": "method.md"}, stream=sent[-1].get("stream", False))
        if len(sent) == 2:
            return tool_response(
                "skill_invoke", {"name": "optional_method", "script": "runner.py", "input": {}}, stream=sent[-1].get("stream", False)
            )
        if len(sent) == 3:
            return tool_response("skill_invoke", {"name": "unused", "script": "runner.py", "input": {}}, stream=sent[-1].get("stream", False))
        return success("openai", "stream" if sent[-1].get("stream") else "invoke")

    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        assert result.ok and result.final_answer == "OK", result.error
        assert len(sent) == 4
        texts = [json.dumps(body) for body in sent]
        assert all("BODY_optional_method" in text and "BODY_support" in text for text in texts)
        tool_results = [[m for m in body["messages"] if m["role"] == "tool"] for body in sent]
        assert "BODY_optional_method" in tool_results[1][-1]["content"]
        assert "Skill 'optional_method' is included in this Agent for reading only" in tool_results[2][-1]["content"]
        assert "CALCULATED 42" in tool_results[3][-1]["content"]
        assert [call[0] for call in calls] == ["unused"]
        assert not calls[0][1].exists()
        assert case.ctx.reference_skill_access is None and case.ctx.skill_runtime_scope is None
        invocations, checks = rows(case, "chat_reference_model_invocations"), rows(case, "chat_reference_extension_checks")
        assert len(checks) == len(invocations) == 4 and all(row["status"] == "completed" for row in invocations)
        for check in checks:
            invocation = next(row for row in invocations if row["invocation_id"] == check["invocation_id"])
            assert (check["context_id"], check["execution_id"], check["created_generation"]) == (
                invocation["context_id"],
                invocation["execution_id"],
                1,
            )
            proof = json.loads(check["policy_check_json"])
            assert proof["current_revision_ref"] == case.primary.binding.revision_ref
            assert "resource_allowlist" not in check["policy_check_json"]
        assert all(len(check_rows) == len(invocation_rows) == i for i, (check_rows, invocation_rows) in enumerate(observed, 1))
        with sqlite3.connect(case.path) as db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute("UPDATE chat_reference_extension_checks SET policy_check_digest=composition_digest")
    finally:
        await adapter.close()


@pytest.mark.parametrize("point", ["before_dispatch", "after_first_send"])
async def test_current_published_disable_stops_next_use_and_preserves_confirmed_receipt(runtime_case, monkeypatch, point):

    case = runtime_case
    registry, calls, _ = tools_for(case.base.fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
    sent = []

    async def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        await publish_policy(case)
        return tool_response("skill_read", {"name": "optional_method"}, stream=body.get("stream", False))

    try:
        if point == "before_dispatch":
            await publish_policy(case)
        with respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            with pytest.raises(TerminalDurabilityAbort):
                await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        expected = int(point == "after_first_send")
        assert len(sent) == expected and calls == []
        invocations, checks = rows(case, "chat_reference_model_invocations"), rows(case, "chat_reference_extension_checks")
        assert len(checks) == len(invocations) == expected
        assert all(row["status"] == "completed" for row in invocations)
        assert not any(row["kind"] == "output_received" for row in rows(case, "chat_reference_evidence"))
        assert case.primary.binding.definition["system_prompt"] == "Use the exact assigned financial methods."
    finally:
        await adapter.close()


async def test_physical_send_requires_durable_current_policy_proof(runtime_case, monkeypatch):
    from bowhead_core import skill_read_extensions

    case = runtime_case
    registry, calls, _ = tools_for(case.base.fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)

    async def missing(*args, **kwargs):
        pass

    monkeypatch.setattr(skill_read_extensions, "record_extension_invocation_check", missing)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert not result.ok and not result.final_answer
            assert route.call_count == 0
        assert calls == [] and rows(case, "chat_reference_model_invocations") == [] and rows(case, "chat_reference_extension_checks") == []
    finally:
        await adapter.close()


async def publish_policy(case, *, enabled=False, expand=False):
    from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionStore
    from test_agent_revision_store import _candidate_hash_of, _listing, _Validator
    from test_skill_read_extensions import policy_for

    fixture = case.base.fixture.case.fixture
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?", (fixture.team_id, fixture.owner_id))
    store = AgentRevisionStore(str(case.path), validator=_Validator())
    definition = {
        **case.primary.binding.definition,
        "system_prompt": "A new role for future executions.",
        "runtime_extensions": policy_for(
            case.base.fixture.optional, *([case.base.fixture.selected] if expand else []), enabled=enabled
        ).canonical_value(),
    }
    working = await store.save_working_copy(
        team_id=fixture.team_id,
        agent_id="osa_exact",
        actor_user_id=fixture.owner_id,
        definition=definition,
        listing=_listing(),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    result = await seed_legacy_publication(
        store, team_id=fixture.team_id, agent_id="osa_exact", actor_user_id=fixture.owner_id, working_copy_id=working.id
    )
    assert result.state == "pending"
    with sqlite3.connect(case.path) as db:
        version = db.execute("SELECT row_version FROM team_subagent_release_candidate WHERE id=?", (result.candidate_id,)).fetchone()[0]
    result = await store.approve_candidate(
        team_id=fixture.team_id,
        candidate_id=result.candidate_id,
        actor_user_id=fixture.owner_id,
        expected_row_version=version,
        expected_candidate_hash=_candidate_hash_of(case.path, result.candidate_id),
    )
    assert result.state == "activated"
    return result
