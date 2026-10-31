"""Exact callers see scoped pin identities without retaining file metadata as history."""

from copy import deepcopy
from pathlib import Path

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.agent_caller import DocumentAgentOutcome
from bowhead_core.documents.input_metadata import document_input_metadata
from bowhead_core.documents.retention import INTERNAL_PRODUCER
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from test_document_agent_caller import caller as caller
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_turn_runner import native as native
from test_document_turn_runner import observe
from test_subagent import _router

from bowhead_router import LLMResponse, ToolCall

pytestmark = pytest.mark.integration


@pytest.fixture
async def pinned(ctx, native, caller):
    artifact = await ctx.artifacts.put_bytes(
        content=Path(ctx.artifact.path).read_bytes(),
        filename="prior-template.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        created_by=caller.context.user_id,
    )
    await ctx.sessions.pin_session_attachments(session_id="s_doc", artifact_ids=[artifact.id])
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE session_attachments SET pinned_at='2026-01-01' WHERE artifact_id=?", (artifact.id,))
        await db.commit()
    caller.context.artifact_store = ctx.artifacts
    return artifact


async def test_metadata_uses_real_existing_pin_without_reading_file_body(ctx, native, caller, pinned):
    text = await document_input_metadata(caller.context)
    assert pinned.id in text and pinned.filename in text
    assert str(Path(pinned.path).parent) not in text
    assert "REGIONAL" not in text
    assert "untrusted user data" in text
    assert await ctx.sessions.list_pinned_attachments(session_id="s_doc")
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 0


@pytest.mark.parametrize(
    "change", ["detached", "late_pin", "deleted", "foreign_owner", "not_ready", "internal", "session", "membership", "assignment", "tool"]
)
async def test_metadata_rechecks_current_authority_and_original_pin_boundary(ctx, native, caller, pinned, change):
    async with open_shared_ctx(ctx.path) as db:
        if change == "detached":
            await db.execute("UPDATE session_attachments SET detached_at='2026-10-30' WHERE artifact_id=?", (pinned.id,))
        elif change == "late_pin":
            await db.execute("UPDATE session_attachments SET pinned_at='2099-01-01' WHERE artifact_id=?", (pinned.id,))
        elif change == "deleted":
            await db.execute("UPDATE artifacts SET deleted_at='2026-10-30' WHERE id=?", (pinned.id,))
        elif change == "foreign_owner":
            await db.execute("UPDATE artifacts SET created_by='u_other' WHERE id=?", (pinned.id,))
        elif change == "not_ready":
            await db.execute("UPDATE artifacts SET status='pending' WHERE id=?", (pinned.id,))
        elif change == "internal":
            await db.execute("UPDATE artifacts SET producer_kind=? WHERE id=?", (INTERNAL_PRODUCER, pinned.id))
        elif change == "session":
            caller.context.session_id = "another-session"
        elif change == "membership":
            await db.execute("UPDATE workspace_members SET status='removed' WHERE user_id='u_doc' AND workspace_id='ws_doc'")
        elif change == "assignment":
            await db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        else:
            await db.execute(
                "INSERT INTO team_settings(team_id,key,value,updated_at) VALUES ('team_doc','builtin_tool_states','{\"template_document_submit\":{\"enabled\":false}}','now')"
            )
        await db.commit()
    assert pinned.id not in await document_input_metadata(caller.context)


@pytest.mark.parametrize("overflow", [False, True])
async def test_actual_custom_request_projects_pin_and_admits_that_file(ctx, native, caller, pinned, monkeypatch, overflow):
    from bowhead_router.errors import LLMTargetFailure, ProviderFailureKind, StreamingTargetsExhaustedError

    from bowhead_core import context_assembly, token_estimator

    arguments = deepcopy(native.arguments)
    arguments["inputs"][0]["artifact_id"] = pinned.id
    router = _router(
        LLMResponse(
            content="", tool_calls=[ToolCall(id="document", name="template_document_submit", arguments=arguments)], finish_reason="tool_calls"
        )
    )
    requests = []
    budgeted = []
    original_invoke = router.invoke
    original_estimate = token_estimator.estimate_tokens

    def estimate(text):
        if "pinned_document_inputs" in text:
            budgeted.append(text)
        return original_estimate(text)

    monkeypatch.setattr(token_estimator, "estimate_tokens", estimate)
    if overflow:
        monkeypatch.setattr(context_assembly, "guard_window", lambda *args, **kwargs: 123)

    async def capture(agent_type, request, **kwargs):
        requests.append(deepcopy(request))
        if overflow and len(requests) == 1:
            raise StreamingTargetsExhaustedError(
                [
                    LLMTargetFailure(
                        provider="p1",
                        model="mock",
                        provider_family="mock",
                        mode="stream",
                        target_index=1,
                        attempt=1,
                        kind=ProviderFailureKind.CONTEXT_OVERFLOW,
                        retryable=False,
                        status_code=400,
                    )
                ]
            )
        return await original_invoke(agent_type, request, **kwargs)

    monkeypatch.setattr(router, "invoke", capture)
    with observe(ctx, native):
        result = await SubagentRunner(router=router, tool_registry=native.registry, team_subagent_store=caller.users).run(
            profile_name="exact-agent",
            task=ctx.source.user_text,
            parent_turn_id="t_doc",
            parent_bus=TurnEventBus(),
            parent_ctx=caller.context,
            exact_binding=caller.binding,
        )
    assert result.ok and isinstance(result.document_outcome, DocumentAgentOutcome), result.error
    assert len(requests) == (2 if overflow else 1)
    projection = [message for message in requests[-1].messages if "pinned_document_inputs" in (message.content or "")]
    assert len(projection) == 1 and projection[0].role == "user" and pinned.id in projection[0].content
    assert any(tool.name == "template_document_submit" for tool in requests[0].tools)
    assert budgeted == [projection[0].content] * (3 if overflow else 1)
    async with open_shared_ctx(ctx.path) as db:
        row = await (await db.execute("SELECT request_json FROM document_jobs WHERE id=?", (result.document_outcome.outcome.job_id,))).fetchone()
        assert pinned.id in row[0]
        copies = await (await db.execute("SELECT source_artifact_id FROM document_input_copies")).fetchall()
        assert [row[0] for row in copies] == [pinned.id]
