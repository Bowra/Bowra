"""Canonical ordinary answers reach the original document worker and compiler."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiosqlite import Connection
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationCandidate
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.continuation import begin_document_planning_in_tx
from bowhead_core.documents.executor import WorkerInput
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.planning_instruction import DocumentInstruction, clarification_evidence_sources_in_tx, document_instruction_in_tx
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, prepare_planning_context, review_planning_response
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.store import _json
from bowhead_core.turn_log_store import TurnLogStore
from test_document_store import SCOPE, Context
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_turn_runner import ASK, claim_resume, observe, start
from test_document_turn_runner import native as native

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
QUESTION = "Which audience should the title address?"
ANSWER = "Board members"
LEGACY_SUFFIX = "\n\nUse the canonical earlier answers in Document clarification history.json to resolve the prior questions."


async def legacy_instruction(db: Connection, *, worker: DocumentTurnOwner, job_id: str) -> DocumentInstruction:
    """Historical producer bytes, retained through real worker/Store checkpoint writes."""
    from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=worker.turn_id, expected_user_id=worker.user_id, expected_workspace_id=worker.workspace_id
    )
    answers = await prior_document_answers_in_tx(db, worker=worker)
    return DocumentInstruction(
        source.user_text + LEGACY_SUFFIX,
        WorkerInput("Document clarification history.json", "data", _json({"request": source.user_text, "prior_answers": answers}).encode()),
    )


def owner(native: SimpleNamespace) -> DocumentTurnOwner:
    return DocumentTurnOwner(
        **SCOPE.model_dump(),
        session_id="s_doc",
        turn_id="t_doc",
        generation=native.context.execution_generation,
        owner_id=native.context.execution_owner_id,
    )


async def ordinary_answer(ctx: Context, native: SimpleNamespace, source: str = "candidate", answer: str = ANSWER) -> dict:
    context = native.context
    store = context.clarification_store
    typed = {
        "question": QUESTION,
        "source": source,
        "answer": None if source == "skip" else answer,
        "skipped": source == "skip",
        "option_index": 1 if source == "candidate" else None,
    }
    card = await store.put(
        turn_id="t_doc",
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        session_id="s_doc",
        token="",
        rationale=QUESTION,
        candidates=[ClarificationCandidate(value="All employees", label="All employees"), ClarificationCandidate(value=ANSWER, label=ANSWER)],
        kind="ambiguous_turn",
        resume_context={"user_text": ctx.source.user_text},
        bounded_question={"generation": 1, "owner_id": context.execution_owner_id, "questions": [{"question": QUESTION}]},
    )
    assert (await store.resolve_once(card.request_id, chosen_value=answer, answers=[typed]))[1]
    assert await ctx.sessions.mark_clarification_pending(turn_id="t_doc", intent="ambiguous", error_kind="needs_clarification")
    await TurnLogStore(str(ctx.path)).record(turn_id="t_doc", event_type="done", payload={"turn_id": "t_doc", "status": "clarification_pending"})
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id="t_doc", owner_id=context.execution_owner_id)
    context.execution_generation = await ctx.sessions.advance_turn_execution_generation(turn_id="t_doc", clarification_id=card.request_id)
    assert context.execution_generation == 2
    context.execution_owner_id = "document-after-ordinary-answer"
    assert (
        await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id=context.execution_owner_id, lease_seconds=600) == "claimed"
    )
    await TurnLogStore(str(ctx.path)).record(turn_id="t_doc", event_type="turn.resumed", payload={"turn_id": "t_doc", "execution_generation": 2})
    return typed


@pytest.mark.parametrize("source", ["candidate", "custom", "skip"])
async def test_canonical_answer_contract_reaches_the_actual_planner(ctx: Context, native: SimpleNamespace, source: str) -> None:
    typed = await ordinary_answer(ctx, native, source)
    outcome = await start(ctx, native)
    payload = json.loads(native.calls[0]["messages"][1]["content"])
    instruction = payload["instruction"]
    assert "Confirmed user decisions" in instruction
    assert "Do not ask an answered question again" in instruction
    assert "null or uncertain answer resolves nothing" in instruction
    decisions = json.loads(instruction.rsplit("\n", 1)[1])
    assert decisions == {"decisions": [{"question": QUESTION, "answer": typed["answer"]}]}
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        material = await document_instruction_in_tx(db, worker=owner(native), job_id=outcome.job_id)
        async with db.execute("SELECT * FROM document_jobs WHERE id=?", (outcome.job_id,)) as cursor:
            job = await cursor.fetchone()
        assert await clarification_evidence_sources_in_tx(db, job=job) == [
            {
                "source_name": material.history.name,
                "source_sha256": hashlib.sha256(material.history.content).hexdigest(),
            }
        ]
    assert material.text == instruction
    assert json.loads(material.history.content) == {"answers": [{"question": QUESTION, "answer": typed["answer"]}]}
    history_index = len(payload["sources"]) - 1
    history_facts = [fact for fact in payload["facts"] if fact["source"] == history_index]
    assert {fact["value"] for fact in history_facts} == {QUESTION, typed["answer"]}
    # A genuinely different question still pauses and consumes the original checkpoint.
    attempt = await claim_resume(ctx, native, outcome)
    native.responses = [ASK]
    with observe(ctx, native):
        result = await native.service.resume_turn(native.context, attempt)
    assert result.kind == "incomplete"
    assert len(native.calls) == 2 and native.worker.runs == ["inventory", "apply_verify", "apply_verify"]


@pytest.mark.parametrize("retained", ["legacy", "current", "changed"])
async def test_retained_instruction_is_verified_without_rewriting(ctx: Context, native: SimpleNamespace, retained: str, monkeypatch) -> None:
    if retained == "legacy":
        monkeypatch.setattr("bowhead_gateway.document_service.CURRENT_PROFILE", "pptx-grounded-plan-v4")
    await ordinary_answer(ctx, native)
    receipt = await native.service.submit(native.context, ctx.proposal)
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        current = await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
        text = ctx.source.user_text + LEGACY_SUFFIX if retained == "legacy" else current.text
        if retained == "changed":
            text += " Changed instruction"
        await begin_document_planning_in_tx(db, worker=owner(native), job_id=receipt.job_id, instruction=text)
        await db.commit()
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        async with db.execute("SELECT * FROM document_planning_attempts WHERE job_id=?", (receipt.job_id,)) as cursor:
            before = [tuple(row) for row in await cursor.fetchall()]
        if retained == "changed":
            with pytest.raises(DocumentRequestError, match="document_checkpoint_unavailable"):
                await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
        else:
            restored = await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
            assert restored.text == text
            expected = await legacy_instruction(db, worker=owner(native), job_id=receipt.job_id) if retained == "legacy" else current
            assert restored.history == expected.history
        async with db.execute("SELECT * FROM document_planning_attempts WHERE job_id=?", (receipt.job_id,)) as cursor:
            assert [tuple(row) for row in await cursor.fetchall()] == before
    assert not native.calls and not native.worker.runs


async def test_no_history_preserves_the_original_instruction(ctx: Context, native: SimpleNamespace) -> None:
    receipt = await native.service.submit(native.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        material = await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
    assert material.text == ctx.source.user_text and material.history is None


async def test_display_origin_requires_exact_canonical_answer_bytes(ctx: Context, native: SimpleNamespace, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    await ordinary_answer(ctx, native)
    outcome = await start(ctx, native)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        await document_instruction_in_tx(db, worker=owner(native), job_id=outcome.job_id)
        async with db.execute("SELECT * FROM document_jobs WHERE id=?", (outcome.job_id,)) as cursor:
            job = await cursor.fetchone()
        async with db.execute("SELECT context_json FROM document_planning_attempts WHERE job_id=?", (outcome.job_id,)) as cursor:
            original_context = (await cursor.fetchone())[0]
        assert len(await clarification_evidence_sources_in_tx(db, job=job)) == 1
        monkeypatch.setattr(
            "bowhead_core.document_clarification_lifecycle.prior_document_answers_in_tx",
            AsyncMock(
                return_value=[{"question": QUESTION, "answer": "Different authenticated answer"}],
            ),
        )
        assert await clarification_evidence_sources_in_tx(db, job=job) == []
        async with db.execute("SELECT context_json FROM document_planning_attempts WHERE job_id=?", (outcome.job_id,)) as cursor:
            assert (await cursor.fetchone())[0] == original_context


async def test_pre_upgrade_checkpoint_resumes_with_the_retained_instruction(
    ctx: Context, native: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bowhead_core.documents import turn_runner

    await ordinary_answer(ctx, native)
    with monkeypatch.context() as previous:
        previous.setattr("bowhead_gateway.document_service.CURRENT_PROFILE", "pptx-grounded-plan-v4")
        previous.setattr(turn_runner, "document_instruction_in_tx", legacy_instruction)
        outcome = await start(ctx, native)
    before = native.calls[0]["messages"][1]["content"]
    attempt = await claim_resume(ctx, native, outcome)
    native.responses = [ASK]
    with observe(ctx, native):
        result = await native.service.resume_turn(native.context, attempt)
    assert result.kind == "incomplete"
    after = json.loads(native.calls[1]["messages"][1]["content"])
    assert after["instruction"].startswith(json.loads(before)["instruction"] + "\n\nClarification question:")
    assert native.worker.runs == ["inventory", "apply_verify", "apply_verify"]


async def test_legacy_checkpoint_rejects_changed_canonical_history_before_resume(
    ctx: Context, native: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bowhead_core.documents import turn_runner

    await ordinary_answer(ctx, native)
    with monkeypatch.context() as previous:
        previous.setattr("bowhead_gateway.document_service.CURRENT_PROFILE", "pptx-grounded-plan-v4")
        previous.setattr(turn_runner, "document_instruction_in_tx", legacy_instruction)
        outcome = await start(ctx, native)
    # Exercise the legacy instruction's missing inline values, using the original stored source digest.
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "UPDATE clarification_requests SET answers_json=replace(answers_json,?,?) WHERE request_id IN "
            "(SELECT request_id FROM clarification_requests WHERE kind='ambiguous_turn' AND status='resolved')",
            (ANSWER, "Changed audience"),
        )
        await db.commit()
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        with pytest.raises(DocumentRequestError, match="document_checkpoint_unavailable"):
            await document_instruction_in_tx(db, worker=owner(native), job_id=outcome.job_id)
    assert len(native.calls) == 1


async def test_literal_answers_remain_verifiable_by_the_original_compiler(ctx: Context, native: SimpleNamespace) -> None:
    await ordinary_answer(ctx, native, "custom")
    receipt = await native.service.submit(native.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        material = await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
    source = Path(ctx.artifact.path).read_bytes()
    data = (JsonEvidenceSource(material.history.name, material.history.content),)
    context = prepare_planning_context(source, material.text, data)
    slots = json.loads(context.payload)["slots"]
    answer_ref = next(f["id"] for f in json.loads(context.payload)["facts"] if f["path"] == "/answers/0/answer")
    response = _json(
        {"action": "propose", "updates": [{"slot": slots[0]["id"], "parts": [{"ref": answer_ref}]}], "keep": [slot["id"] for slot in slots[1:]]}
    )
    review = review_planning_response(source, material.text, data, response, expected_context_digest=context.digest)
    assert review.changes[0].after == ANSWER and review.semantic_status == "unapproved"


async def test_combined_instruction_limit_rejects_without_truncating(ctx: Context, native: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from bowhead_core.documents import planning_instruction

    await ordinary_answer(ctx, native)
    receipt = await native.service.submit(native.context, ctx.proposal)
    # Only the canonical source read boundary is controlled; typed history and the builder are real.
    long_source = replace(ctx.source, user_text="x" * 7600)
    monkeypatch.setattr(planning_instruction, "_read_chat_turn_user_source_in_tx", AsyncMock(return_value=long_source))
    assert len(long_source.user_text + LEGACY_SUFFIX) < 8000
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        with pytest.raises(DocumentRequestError, match="planning_input_limit"):
            await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
    assert not native.calls and not native.worker.runs


@pytest.mark.skipif(os.environ.get("BOWHEAD_DOCUMENT_NATIVE_SMOKE") != "1", reason="Requires explicit native LibreOffice/Poppler smoke environment")
async def test_ordinary_answer_reaches_review_and_exact_publication_in_one_model_call(ctx: Context, native: SimpleNamespace) -> None:
    await ordinary_answer(ctx, native)
    receipt = await native.service.submit(native.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        instruction = await document_instruction_in_tx(db, worker=owner(native), job_id=receipt.job_id)
    source = Path(ctx.artifact.path).read_bytes()
    context = prepare_planning_context(source, instruction.text, (JsonEvidenceSource(instruction.history.name, instruction.history.content),))
    payload = json.loads(context.payload)
    answer_ref = next(f["id"] for f in payload["facts"] if f["path"] == "/answers/0/answer")
    native.responses = [
        _json(
            {
                "action": "propose",
                "updates": [{"slot": "s0", "parts": [{"ref": answer_ref}]}],
                "keep": [slot["id"] for slot in payload["slots"] if slot["id"] != "s0"],
            }
        )
    ]
    with observe(ctx, native):
        outcome = await native.service.run_turn(native.context, receipt)
    assert outcome.kind == "review"
    delivery = native.service.delivery
    draft = await delivery.get(scope=SCOPE, job_id=receipt.job_id)
    assert draft["clarification_evidence_sources"] == [
        {
            "source_name": instruction.history.name,
            "source_sha256": hashlib.sha256(instruction.history.content).hexdigest(),
        }
    ]
    assert draft["draft"]["manifest"]["changes"][0]["evidence"][0]["source_sha256"] == draft["clarification_evidence_sources"][0]["source_sha256"]
    assert hashlib.sha256(_json(draft["draft"]).encode()).hexdigest() == draft["draft_digest"]
    assert draft["draft"]["preview"]["text_and_geometry"] == "passed"
    published = await delivery.confirm(scope=SCOPE, job_id=receipt.job_id, command_id="confirm-answered-draft", draft_digest=outcome.draft_digest)
    assert published["state"] == "published"
    assert (
        await delivery.confirm(scope=SCOPE, job_id=receipt.job_id, command_id="confirm-answered-draft", draft_digest=outcome.draft_digest)
        == published
    )
    for name in ("candidate.pptx", "candidate.pdf"):
        content, _ = await delivery.read_file(scope=SCOPE, job_id=receipt.job_id, name=name, preview=False)
        assert hashlib.sha256(content).hexdigest() == draft["draft"]["files"][name]["sha256"]
    assert len(native.calls) == 1 and native.worker.runs == ["inventory", "apply_verify"]
