"""Real canonical source selection, immutable admission and exact recovery."""

import json
from dataclasses import replace

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.request import (
    DocumentRequestError,
    DocumentTaskGenerationArguments,
    UserQuote,
    document_tool_schema,
    parse_document_tool_arguments,
)
from bowhead_core.documents.task_sources import resolve_task_source_in_tx, task_instruction_in_tx
from bowhead_core.ordinary_lead_continuation import content_hash
from jsonschema import validate
from test_document_revision import finish_chat_turn
from test_document_store import SCOPE, _assert_empty, _sql
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
AMOUNT = "318.25 (million); put only 318.25 in Amount."
OUTSIDE = "A distinct earlier private task."


@pytest.fixture(autouse=True)
def task_source_admission(monkeypatch):
    monkeypatch.setenv("FEATURE_DOCUMENT_TASK_SOURCES", "1")


async def follow_up(ctx, *, text=AMOUNT, turn_id="t_supplement"):
    await finish_chat_turn(ctx, "s_doc")
    source = replace(ctx.source, turn_id=turn_id, user_text=text, attachment_ids=(), attachment_refs=())
    await ctx.sessions.record_turn(turn_id=turn_id, session_id="s_doc", user_text=text, user_source=source)
    return source


def proposal_for(ctx, current, *, quote=None):
    return ctx.proposal.model_copy(
        update={"evidence": UserQuote(text=current.user_text), "task_source": UserQuote(text=quote or ctx.source.user_text)}
    )


async def admit(ctx, current, proposal):
    prepared = await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal)
    receipt = await ctx.store.submit_from_turn(
        scope=SCOPE, turn_id=current.turn_id, command_id="task-source", proposal=prepared.proposal, _task_source=prepared.task_source
    )
    return prepared, receipt


async def test_original_task_and_current_supplement_reach_reader_and_exact_recovery(ctx, monkeypatch):
    current = await follow_up(ctx)
    proposal = proposal_for(ctx, current)
    _, receipt = await admit(ctx, current, proposal)
    job = await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)
    assert job.user_source_sha256 == content_hash(chat_turn_user_source_projection(current))
    assert job.proposal.inputs[0].current_message_position is None
    request = json.loads((await _sql(ctx, "SELECT request_json FROM document_jobs WHERE id=?", (receipt.job_id,)))[0][0])
    assert request["task_source"] == {
        "version": 1,
        "turn_id": ctx.source.turn_id,
        "source_hash": content_hash(chat_turn_user_source_projection(ctx.source)),
    }
    async with open_shared_ctx(ctx.path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM document_jobs WHERE id=?", (receipt.job_id,))).fetchone()
        instruction = await task_instruction_in_tx(db, job=row, fallback=current.user_text)
    assert instruction.endswith("Original user request:\n" + ctx.source.user_text + "\n\nCurrent user supplement:\n" + AMOUNT)
    view = await DocumentDeliveryStore(ctx.artifacts).get(scope=SCOPE, job_id=receipt.job_id)
    assert view["request_text"] == ctx.source.user_text + "\n\n" + AMOUNT
    await follow_up(ctx, text=ctx.source.user_text, turn_id="t_later_duplicate")
    monkeypatch.setenv("FEATURE_DOCUMENT_TASK_SOURCES", "0")
    assert await ctx.store.recover_from_turn(scope=SCOPE, turn_id=current.turn_id, command_id="task-source", proposal=proposal) == receipt
    assert await ctx.store.get(scope=SCOPE, job_id=receipt.job_id) == job
    assert (await _sql(ctx, "SELECT count(*) FROM document_jobs"))[0][0] == 1
    assert (await _sql(ctx, "SELECT count(*) FROM document_confirmations"))[0][0] == 0


async def test_ambiguous_source_requires_a_unique_user_quotation_before_admission(ctx):
    await follow_up(ctx, text=ctx.source.user_text, turn_id="t_duplicate")
    current = await follow_up(ctx)
    with pytest.raises(DocumentRequestError, match="document_task_source_ambiguous"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current))
    await _assert_empty(ctx)


@pytest.mark.parametrize("boundary", ["session", "space", "user", "future", "assistant"])
async def test_task_source_cannot_read_another_scope_future_or_assistant_message(ctx, boundary):
    if boundary == "assistant":
        await _sql(ctx, "UPDATE turns SET final_answer=? WHERE id=?", (OUTSIDE, ctx.source.turn_id))
    elif boundary != "future":
        session_id = "s_outside"
        user_id = "u_other" if boundary == "user" else SCOPE.user_id
        workspace_id = "ws_other" if boundary == "space" else SCOPE.workspace_id
        await ctx.sessions.ensure_session(session_id=session_id, workspace_id=workspace_id, user_id=user_id)
        source = replace(
            ctx.source,
            turn_id="t_outside",
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
            user_text=OUTSIDE,
            attachment_ids=(),
            attachment_refs=(),
        )
        await ctx.sessions.record_turn(turn_id=source.turn_id, session_id=session_id, user_text=OUTSIDE, user_source=source)
    current = await follow_up(ctx)
    if boundary == "future":
        await follow_up(ctx, text=OUTSIDE, turn_id="t_future")
        async with open_shared_ctx(ctx.path) as db:
            with pytest.raises(DocumentRequestError, match="document_task_source_unavailable"):
                await resolve_task_source_in_tx(db, current=current, quote=UserQuote(text=OUTSIDE))
        await _assert_empty(ctx)
        return
    with pytest.raises(DocumentRequestError, match="document_task_source_unavailable"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current, quote=OUTSIDE))
    await _assert_empty(ctx)


async def test_source_reference_never_grants_an_unpinned_file(ctx):
    current = await follow_up(ctx)
    await _sql(ctx, "UPDATE session_attachments SET detached_at='2026-11-02'")
    with pytest.raises(DocumentRequestError, match="document_input_unavailable"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current))
    await _assert_empty(ctx)


async def test_source_hash_is_rechecked_at_commit_and_job_read(ctx):
    current = await follow_up(ctx)
    proposal = proposal_for(ctx, current)
    prepared = await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal)
    changed = prepared.task_source.model_copy(update={"source_hash": "0" * 64})
    with pytest.raises(DocumentRequestError, match="document_task_source_changed"):
        await ctx.store.submit_from_turn(scope=SCOPE, turn_id=current.turn_id, command_id="changed", proposal=prepared.proposal, _task_source=changed)
    await _assert_empty(ctx)
    _, receipt = await admit(ctx, current, proposal)
    # Emulate a restored/inconsistent source projection, not an authorized user edit.
    await _sql(ctx, "UPDATE turns SET user_text='Source no longer matches' WHERE id=?", (ctx.source.turn_id,))
    for reader in (ctx.store, DocumentDeliveryStore(ctx.artifacts)):
        with pytest.raises(DocumentRequestError, match="document_task_source_unavailable"):
            await reader.get(scope=SCOPE, job_id=receipt.job_id)


async def test_instruction_limit_is_checked_before_allocating_a_job(ctx):
    current = await follow_up(ctx, text="x" * 7999)
    with pytest.raises(DocumentRequestError, match="document_task_source_limit"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current))
    await _assert_empty(ctx)


async def test_new_source_admission_off_preserves_legacy_requests_and_hashes(ctx, monkeypatch):
    monkeypatch.setenv("FEATURE_DOCUMENT_TASK_SOURCES", "0")
    assert "task_source" not in document_tool_schema()["properties"]
    receipt = await ctx.submit()
    request = json.loads((await _sql(ctx, "SELECT request_json FROM document_jobs WHERE id=?", (receipt.job_id,)))[0][0])
    assert "task_source" not in request and "task_source" not in request["proposal"]
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).request_hash == content_hash(request)
    current = await follow_up(ctx)
    with pytest.raises(DocumentRequestError, match="document_task_source_disabled"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current))
    assert (await _sql(ctx, "SELECT count(*) FROM document_jobs"))[0][0] == 1


@pytest.mark.parametrize("reference", ["none", "current-full", "current-fragment", "earlier"])
async def test_current_arguments_use_canonical_message_and_recover_after_admission_off(ctx, monkeypatch, reference):
    current = await follow_up(ctx) if reference == "earlier" else ctx.source
    quote = None if reference == "none" else "保持格式替换本季度文字" if reference == "current-fragment" else ctx.source.user_text
    arguments = {
        "action": "generate",
        "inputs": [{"artifact_id": ctx.artifact.id, "role": "template"}],
        "original_request_quote": quote,
        "requested_outputs": ["source"],
    }
    validate(arguments, document_tool_schema())
    parsed = parse_document_tool_arguments(arguments)
    assert isinstance(parsed, DocumentTaskGenerationArguments)
    prepared = await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=parsed)
    assert prepared.proposal.evidence.text == current.user_text
    assert prepared.proposal.inputs[0].evidence.text == current.user_text
    assert (prepared.task_source is not None) == (reference == "earlier")
    receipt = await ctx.store.submit_from_turn(
        scope=SCOPE, turn_id=current.turn_id, command_id="current-arguments", proposal=prepared.proposal, _task_source=prepared.task_source
    )
    monkeypatch.setenv("FEATURE_DOCUMENT_TASK_SOURCES", "0")
    replay = parse_document_tool_arguments(arguments)
    assert await ctx.store.recover_from_turn(scope=SCOPE, turn_id=current.turn_id, command_id="current-arguments", proposal=replay) == receipt
    assert (await _sql(ctx, "SELECT count(*) FROM document_jobs"))[0][0] == 1


async def test_recovery_keeps_an_existing_prior_snapshot_when_quote_also_matches_current(ctx, monkeypatch):
    current = await follow_up(ctx, text=ctx.source.user_text + "\nAmount is 318.25.")
    evidence = UserQuote(text=current.user_text)
    legacy = ctx.proposal.model_copy(
        update={
            "evidence": evidence,
            "task_source": UserQuote(text=ctx.source.user_text),
            "inputs": tuple(item.model_copy(update={"evidence": evidence, "current_message_position": None}) for item in ctx.proposal.inputs),
        }
    )
    prepared, receipt = await admit(ctx, current, legacy)
    assert prepared.task_source.turn_id == ctx.source.turn_id
    before = await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)
    arguments = {
        "action": "generate",
        "inputs": [{"artifact_id": ctx.artifact.id, "role": "template"}],
        "original_request_quote": ctx.source.user_text,
        "requested_outputs": list(ctx.proposal.requested_outputs),
    }
    monkeypatch.setenv("FEATURE_DOCUMENT_TASK_SOURCES", "0")
    assert (
        await ctx.store.recover_from_turn(
            scope=SCOPE, turn_id=current.turn_id, command_id="task-source", proposal=parse_document_tool_arguments(arguments)
        )
        == receipt
    )
    assert await ctx.store.get(scope=SCOPE, job_id=receipt.job_id) == before
