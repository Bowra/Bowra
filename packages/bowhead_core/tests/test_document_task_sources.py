"""Real canonical source selection, immutable admission and exact recovery."""

import json
from dataclasses import replace

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_export_projection import build_canvas_content_projection
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.documents.canvas_inputs import CanvasInputAdmission, CanvasInputManifest, CanvasSectionIdentity, continuation_canvas_sections_in_tx
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


async def test_repeated_supplements_keep_only_their_latest_position(ctx):
    retry = "Use the confirmed scope and show me the draft."
    for turn_id, text in [("t_a", "Use demo stores."), ("t_b", retry), ("t_c", "Rank the top 20."), ("t_d", "Use demo stores."), ("t_e", retry)]:
        await follow_up(ctx, text=text, turn_id=turn_id)
    current = await follow_up(ctx, text=retry, turn_id="t_apply")
    _, receipt = await admit(ctx, current, proposal_for(ctx, current))
    async with open_shared_ctx(ctx.path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM document_jobs WHERE id=?", (receipt.job_id,))).fetchone()
        collapsed = await task_instruction_in_tx(db, job=row, fallback="")
        expanded = await task_instruction_in_tx(db, job=row, fallback="", collapse_repeats=False)
    # A retry repeats no new requirement; the latest copy keeps later changes ordered after earlier ones.
    assert collapsed.count("Earlier user supplement:") == 2 and collapsed.count(retry) == 1
    assert collapsed.endswith("Current user supplement:\n" + retry)
    assert collapsed.index("Original user request:") < collapsed.index("Rank the top 20.") < collapsed.index("Use demo stores.")
    assert expanded.count("Earlier user supplement:") == 5 and expanded.count(retry) == 3


async def test_ambiguous_source_requires_a_unique_user_quotation_before_admission(ctx):
    await follow_up(ctx, text=ctx.source.user_text, turn_id="t_duplicate")
    current = await follow_up(ctx)
    with pytest.raises(DocumentRequestError, match="document_task_source_ambiguous"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current))
    await _assert_empty(ctx)


@pytest.mark.parametrize("quote_in_current", [False, True])
async def test_quoted_continuation_retains_confirmed_answers_supplement_and_canvas(ctx, quote_in_current):
    card_store = ClarificationStore(ctx.path)
    question = "What period should the report cover?"
    card = await card_store.put(
        turn_id=ctx.source.turn_id,
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        session_id=ctx.source.session_id,
        token="period",
        rationale=question,
        candidates=[],
        kind="options",
    )
    _, resolved = await card_store.resolve_once(
        card.request_id,
        chosen_value="FY2026 Q1",
        answers=[
            {"question": question, "source": "custom", "answer": "FY2026 Q1", "skipped": False},
            {"question": "Unanswered?", "source": "skip", "skipped": True},
        ],
    )
    assert resolved
    supplement = await follow_up(ctx, text="Use demo stores and rank the top 20.", turn_id="t_scope")
    await _sql(ctx, "UPDATE turns SET final_answer='Use all medical organizations' WHERE id=?", (supplement.turn_id,))
    canvas_store = CanvasStore(ctx.path)
    canvas = await canvas_store.ensure_canvas(session_id=ctx.source.session_id, workspace_id=SCOPE.workspace_id, user_id=SCOPE.user_id)
    await canvas_store.append_section(canvas_id=canvas.id, kind="text", payload={"text": "Unlinked data"})
    for index in range(65):
        await canvas_store.append_section(canvas_id=canvas.id, kind="code_block", payload={"code": f"value = {index}"}, by_turn_id=supplement.turn_id)
    section = await canvas_store.append_section(
        canvas_id=canvas.id, kind="text", payload={"text": "Demo store revenue"}, by_turn_id=supplement.turn_id
    )
    text = "Please apply that to the same template now."
    if quote_in_current:
        text += " Original request: " + ctx.source.user_text
    current = await follow_up(ctx, text=text, turn_id="t_apply")
    proposal = parse_document_tool_arguments(
        {
            "action": "generate",
            "inputs": [{"artifact_id": ctx.artifact.id, "role": "template"}],
            "original_request_quote": ctx.source.user_text,
            "requested_outputs": list(ctx.proposal.requested_outputs),
        }
    )
    prepared = await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal)
    assert prepared.task_source.continuation.through_turn_id == current.turn_id
    async with open_shared_ctx(ctx.path) as db:
        assert await continuation_canvas_sections_in_tx(
            db, scope=SCOPE, session_id=current.session_id, original_turn_id=ctx.source.turn_id, current_turn_id=current.turn_id
        ) == (section.id,)
    projection = build_canvas_content_projection(title=canvas.title, sections=await canvas_store.list_sections(canvas.id))
    manifest = CanvasInputManifest(
        canvas_id=canvas.id,
        session_id=current.session_id,
        projection_token=projection.projection_token,
        sections=(CanvasSectionIdentity(section_id=section.id, rev=section.rev),),
        source_refs=(),
        artifact_closure=(),
        accessible_sources=(),
        sha256="a" * 64,
        size_bytes=1,
    )
    receipt = await ctx.store.submit_from_turn(
        scope=SCOPE,
        turn_id=current.turn_id,
        command_id="task-source",
        proposal=prepared.proposal,
        _task_source=prepared.task_source,
        _canvas=CanvasInputAdmission(manifest, "art_frozen_canvas", "canvas-data.json"),
    )
    await follow_up(ctx, text="Future request text", turn_id="t_future")
    async with open_shared_ctx(ctx.path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM document_jobs WHERE id=?", (receipt.job_id,))).fetchone()
        instruction = await task_instruction_in_tx(db, job=row, fallback="")
    assert instruction.index("FY2026 Q1") < instruction.index(supplement.user_text) < instruction.index(current.user_text)
    assert all(text not in instruction for text in ("Unanswered?", "medical organizations", "Future request text"))
    assert json.loads(row["request_json"])["canvas_input"] == manifest.model_dump(mode="json")
    assert prepared.proposal.inputs[0].artifact_id == ctx.artifact.id
    assert await ctx.store.recover_from_turn(scope=SCOPE, turn_id=current.turn_id, command_id="task-source", proposal=proposal) == receipt


async def test_complete_original_message_wins_over_later_quoted_mentions(ctx):
    await follow_up(ctx, text="Continue this task: " + ctx.source.user_text, turn_id="t_mention")
    current = await follow_up(ctx)
    prepared = await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=proposal_for(ctx, current))
    assert prepared.task_source.turn_id == ctx.source.turn_id


async def test_ambiguous_earlier_quote_does_not_fall_back_to_current_message(ctx):
    await follow_up(ctx, text=ctx.source.user_text, turn_id="t_duplicate")
    current = await follow_up(ctx, text="Continue: " + ctx.source.user_text)
    parsed = parse_document_tool_arguments(
        {
            "action": "generate",
            "inputs": [{"artifact_id": ctx.artifact.id, "role": "template"}],
            "original_request_quote": ctx.source.user_text,
        }
    )
    with pytest.raises(DocumentRequestError, match="document_task_source_ambiguous"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=current.turn_id, proposal=parsed)
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
