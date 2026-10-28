"""Canonical revision intent, exact successor bytes and transaction races."""

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.document_revision_target import document_reuse_enabled
from bowhead_core.documents.inputs import DocumentInputStore, cleanup_document_inputs
from bowhead_core.documents.request import DocumentRequestError, InputRoleProposal, UserQuote
from bowhead_core.documents.turn_policy import DocumentTurnState
from test_document_admission import admission as admission
from test_document_admission import invoke
from test_document_capability import runtime
from test_document_delivery import _review
from test_document_pptx_plan import case as case
from test_document_revision_validation import INSTRUCTION, revision_source
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def enable_revision_admission(monkeypatch):
    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "1")


@pytest.mark.parametrize(("value", "expected"), [(None, True), ("1", True), ("0", False)])
def test_revision_admission_uses_enabled_default_and_explicit_override(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", raising=False)
    else:
        monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", value)
    assert document_reuse_enabled() is expected


async def admit_source(ctx, source):
    await finish_chat_turn(ctx, source.session_id)
    await ctx.sessions.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text, user_source=source)
    return {
        "scope": SCOPE,
        "turn_id": source.turn_id,
        "command_id": f"revision:{source.turn_id}",
        "evidence": UserQuote(text=source.user_text),
        "runtime": await runtime(ctx, turn_id=source.turn_id),
    }


async def finish_chat_turn(ctx, session_id):
    session = await ctx.sessions.get_session(session_id)
    if session.active_turn_id is not None:
        await ctx.sessions.mark_completed(
            turn_id=session.active_turn_id,
            intent=None,
            final_answer="Private document request accepted.",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0,
        )
        await ctx.sessions.finalize_turn(session_id=session_id, current_turn_id=session.active_turn_id, next_turn_id=None)


async def test_exact_revision_preserves_candidate_bytes_and_recovers_one_successor(ctx, monkeypatch):
    delivery, parent, draft, output = await _review(ctx)
    source = revision_source(ctx, parent, draft["draft_digest"])
    arguments = await admit_source(ctx, source)
    one, two = await asyncio.gather(*(ctx.store.submit_revision_from_turn(**arguments) for _ in range(2)))
    assert one == two
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "cancelled"
    successor = await delivery.get(scope=SCOPE, job_id=one.job_id)
    assert successor["request_text"] == INSTRUCTION
    inputs = DocumentInputStore(ctx.artifacts)
    frozen = await inputs.freeze(scope=SCOPE, job_id=one.job_id)
    assert frozen[0].source_artifact_id == draft["draft"]["files"]["candidate.pptx"]["artifact_id"]
    assert await inputs.read(scope=SCOPE, job_id=one.job_id, artifact_id=frozen[0].artifact_id) == output.files["candidate.pptx"]
    assert await ctx.artifacts.get(frozen[0].source_artifact_id) is None
    with pytest.raises(DocumentRequestError, match="document_revision_conflict"):
        await delivery.confirm(scope=SCOPE, job_id=parent, command_id="stale-confirm", draft_digest=draft["draft_digest"])
    await ctx.sessions.mark_cancelled(turn_id=source.turn_id)
    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "0")
    assert await ctx.store.submit_revision_from_turn(**arguments) == one
    with pytest.raises(DocumentRequestError, match="document_command_conflict"):
        await ctx.store.submit_revision_from_turn(**{**arguments, "evidence": UserQuote(text="Change the title")})
    async with open_shared_ctx(ctx.path) as db:
        for table in ("document_revisions", "document_dispatches"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 1


async def test_failed_successor_commit_rolls_back_link_receipt_and_supersession(ctx):
    delivery, parent, draft, _ = await _review(ctx)
    arguments = await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"]))
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("CREATE TRIGGER reject_revision BEFORE INSERT ON document_revision_inputs BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="injected failure"):
        await ctx.store.submit_revision_from_turn(**arguments)
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "review"
    async with open_shared_ctx(ctx.path) as db:
        for table, expected in (("document_jobs", 1), ("document_revisions", 0), ("document_dispatches", 0), ("document_command_receipts", 1)):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == expected


@pytest.mark.parametrize("concurrent", [False, True])
async def test_confirmation_and_revision_have_one_winner(ctx, concurrent):
    delivery, parent, draft, _ = await _review(ctx)
    arguments = await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"]))
    confirm = delivery.confirm(scope=SCOPE, job_id=parent, command_id="confirm-race", draft_digest=draft["draft_digest"])
    if concurrent:
        outcomes = await asyncio.gather(confirm, ctx.store.submit_revision_from_turn(**arguments), return_exceptions=True)
        assert sum(isinstance(value, DocumentRequestError) for value in outcomes) == 1
    else:
        await confirm
        with pytest.raises(DocumentRequestError, match="document_revision_conflict"):
            await ctx.store.submit_revision_from_turn(**arguments)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT count(*) FROM document_confirmations WHERE job_id=?", (parent,)) as cursor:
            published = (await cursor.fetchone())[0]
        async with db.execute("SELECT count(*) FROM document_revisions WHERE parent_job_id=?", (parent,)) as cursor:
            superseded = (await cursor.fetchone())[0]
    assert published + superseded == 1


async def test_superseded_source_is_retained_until_its_successor_is_cancelled(ctx):
    delivery, parent, draft, output = await _review(ctx)
    arguments = await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"]))
    successor = await ctx.store.submit_revision_from_turn(**arguments)
    source_id = draft["draft"]["files"]["candidate.pptx"]["artifact_id"]
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE document_executions SET updated_at='2000-01-01T00:00:00Z' WHERE job_id=?", (parent,))
        async with db.execute("SELECT path FROM artifacts WHERE id=?", (source_id,)) as cursor:
            path = Path((await cursor.fetchone())[0])
        with pytest.raises(aiosqlite.IntegrityError, match="artifact_in_use"):
            await db.execute("UPDATE artifacts SET deleted_at='2026-10-03' WHERE id=?", (source_id,))
        await db.commit()
    await delivery.cleanup()
    await cleanup_document_inputs(ctx.artifacts, now=time.time() + 172800)
    assert path.read_bytes() == output.files["candidate.pptx"]
    frozen = await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=successor.job_id)
    assert len(frozen) == 1
    await ctx.store.cancel(scope=SCOPE, job_id=successor.job_id, command_id="cancel-successor", expected_proposal_revision=1)
    await delivery.cleanup()
    assert not path.exists()


async def test_revision_question_answer_retains_exact_selected_template(ctx):
    from test_document_recovery import turn

    delivery, parent, draft, output = await _review(ctx)
    successor = await ctx.store.submit_revision_from_turn(**await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"])))
    inputs = DocumentInputStore(ctx.artifacts)
    await inputs.freeze(scope=SCOPE, job_id=successor.job_id)
    owner = await delivery.claim(scope=SCOPE, job_id=successor.job_id)
    await delivery.transition(
        scope=SCOPE,
        job_id=successor.job_id,
        owner=owner,
        state="planning",
        context_json=json.dumps({"payload": json.dumps({"instruction": INSTRUCTION})}),
    )
    await delivery.transition(scope=SCOPE, job_id=successor.job_id, owner=owner, state="rendering")
    await delivery.transition(scope=SCOPE, job_id=successor.job_id, owner=owner, state="question", question="Which title period?")
    await finish_chat_turn(ctx, "s_doc")
    answer_turn = await turn(ctx, "revision_answer", text="Use Q3 2026.", session_id="s_doc", attach=False)
    answered = await ctx.store.answer_from_turn(
        scope=SCOPE,
        turn_id=answer_turn,
        command_id="answer-revision",
        parent_job_id=successor.job_id,
        evidence=UserQuote(text="Use Q3 2026."),
        runtime=await runtime(ctx, turn_id=answer_turn),
    )
    frozen = await inputs.freeze(scope=SCOPE, job_id=answered.job_id)
    assert await inputs.read(scope=SCOPE, job_id=answered.job_id, artifact_id=frozen[0].artifact_id) == output.files["candidate.pptx"]


async def test_revision_reuses_frozen_data_not_mutable_original(ctx):
    data = await ctx.artifacts.put_bytes(
        content=b'{"period":"Q2","total":6000}', filename="data.json", mime="application/json", created_by=SCOPE.user_id
    )
    await ctx.sessions.pin_session_attachments(session_id="s_doc", artifact_ids=[data.id])
    ctx.proposal = ctx.proposal.model_copy(
        update={
            "inputs": (*ctx.proposal.inputs, InputRoleProposal(artifact_id=data.id, role="data", evidence=ctx.proposal.evidence)),
        }
    )
    delivery, parent, draft, _ = await _review(ctx)
    original_data = Path(data.path)
    original_data.write_bytes(b'{"period":"changed","total":0}')
    successor = await ctx.store.submit_revision_from_turn(**await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"])))
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE document_executions SET updated_at='2000-01-01T00:00:00Z' WHERE job_id=?", (parent,))
        await db.commit()
    await delivery.cleanup()
    await cleanup_document_inputs(ctx.artifacts, now=time.time() + 172800)
    inputs = DocumentInputStore(ctx.artifacts)
    frozen = await inputs.freeze(scope=SCOPE, job_id=successor.job_id)
    assert len(frozen) == 2 and frozen[1].source_artifact_id != data.id
    assert await inputs.read(scope=SCOPE, job_id=successor.job_id, artifact_id=frozen[1].artifact_id) == b'{"period":"Q2","total":6000}'


async def test_corrupted_retained_candidate_fails_before_copy_or_model(ctx):
    _, parent, draft, _ = await _review(ctx)
    successor = await ctx.store.submit_revision_from_turn(**await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"])))
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT path FROM document_revision_inputs WHERE job_id=?", (successor.job_id,)) as cursor:
        path = Path((await cursor.fetchone())[0])
    path.write_bytes(b"corrupted private source")
    with pytest.raises(DocumentRequestError, match="document_source_hash_mismatch"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=successor.job_id)
    async with open_shared_ctx(ctx.path) as db:
        for table in ("document_input_sets", "document_model_calls"):
            async with db.execute(f"SELECT count(*) FROM {table} WHERE job_id=?", (successor.job_id,)) as cursor:
                assert (await cursor.fetchone())[0] == 0


async def test_revision_target_cannot_be_used_as_new_generation(ctx):
    delivery, parent, draft, _ = await _review(ctx)
    source = revision_source(ctx, parent, draft["draft_digest"])
    arguments = await admit_source(ctx, source)
    proposal = ctx.proposal.model_copy(update={"evidence": UserQuote(text=source.user_text)})
    with pytest.raises(DocumentRequestError, match="document_revision_required"):
        await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=source.turn_id, proposal=proposal)
    with pytest.raises(DocumentRequestError, match="document_revision_required"):
        await ctx.store.submit_from_turn(
            scope=SCOPE, turn_id=source.turn_id, command_id="wrong-action", proposal=proposal, runtime=arguments["runtime"]
        )
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "review"


async def test_revocation_between_chat_and_successor_keeps_original_draft(ctx):
    delivery, parent, draft, _ = await _review(ctx)
    arguments = await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"]))
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        await db.commit()
    with pytest.raises(DocumentRequestError):
        await ctx.store.submit_revision_from_turn(**arguments)
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "review"


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("artifact_id", "art_unrelated_private"), ("path", "unrelated-private-path")])
async def test_tampered_prepared_private_input_cannot_acquire_revision_authority(ctx, field, value):
    delivery, parent, draft, _ = await _review(ctx)
    source = revision_source(ctx, parent, draft["draft_digest"])
    arguments = await admit_source(ctx, source)
    prepared = await ctx.store.prepare_from_turn(scope=SCOPE, turn_id=source.turn_id, proposal=arguments["evidence"])
    altered = replace(prepared.revision, inputs=(replace(prepared.revision.inputs[0], **{field: value}),))
    with pytest.raises(DocumentRequestError, match="document_revision_conflict"):
        await ctx.store.submit_from_turn(
            scope=SCOPE,
            turn_id=source.turn_id,
            command_id="tampered-input",
            proposal=prepared.proposal,
            runtime=arguments["runtime"],
            _revision=altered,
        )
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "review"


async def test_successor_retains_immutable_private_lineage(ctx):
    _, parent, draft, _ = await _review(ctx)
    successor = await ctx.store.submit_revision_from_turn(**await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"])))
    source_id = draft["draft"]["files"]["candidate.pptx"]["artifact_id"]
    async with open_shared_ctx(ctx.path) as db:
        for statement, args in (
            ("DELETE FROM document_revisions WHERE job_id=?", (successor.job_id,)),
            ("UPDATE document_revision_inputs SET sha256=? WHERE job_id=?", ("0" * 64, successor.job_id)),
            ("DELETE FROM document_revision_inputs WHERE job_id=?", (successor.job_id,)),
            ("DELETE FROM artifacts WHERE id=?", (source_id,)),
            ("UPDATE artifacts SET path='changed' WHERE id=?", (source_id,)),
        ):
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(statement, args)
            await db.rollback()


@pytest.mark.parametrize("change", ["none", "gate", "viewer", "published", "cancelled", "missing_input"])
async def test_revision_availability_is_real_read_authority_not_browser_target(ctx, monkeypatch, change):
    delivery, parent, draft, _ = await _review(ctx)
    assert draft["can_revise"] is True
    if change == "gate":
        monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "0")
    elif change == "published":
        await delivery.confirm(scope=SCOPE, job_id=parent, command_id="confirm-availability", draft_digest=draft["draft_digest"])
    elif change == "cancelled":
        await ctx.store.cancel(scope=SCOPE, job_id=parent, command_id="cancel-availability", expected_proposal_revision=1)
    elif change in {"viewer", "missing_input"}:
        async with open_shared_ctx(ctx.path) as db:
            if change == "viewer":
                await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            else:
                await db.execute("UPDATE artifacts SET status='missing' WHERE id=?", (draft["draft"]["files"]["candidate.pptx"]["artifact_id"],))
            await db.commit()
    assert (await delivery.get(scope=SCOPE, job_id=parent))["can_revise"] == (change == "none")


async def test_revision_worker_receives_current_candidate_and_original_frozen_data(ctx, case, tmp_path):
    from bowhead_core.documents.delivery import DocumentDeliveryStore
    from bowhead_core.documents.executor import WorkerResult
    from bowhead_core.documents.pptx_draft import compile_pptx_draft
    from bowhead_core.documents.runner import run_document
    from test_document_runner import IMAGE, HostWorker, setup_job, transport

    parent = await setup_job(ctx, case)
    compiled = compile_pptx_draft(case[0], case[1], case[2], json.dumps(case[4]), expected_context_digest=case[3].digest)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=parent)
    owner = await delivery.claim(scope=SCOPE, job_id=parent)
    await delivery.transition(scope=SCOPE, job_id=parent, owner=owner, state="planning", image_id=IMAGE)
    await delivery.transition(scope=SCOPE, job_id=parent, owner=owner, state="rendering")
    # The parent is a synthetic reviewed fixture, not native-render acceptance.
    await delivery.save_draft(
        scope=SCOPE,
        job_id=parent,
        owner=owner,
        result=WorkerResult(
            {"kind": "draft", "manifest": {"semantic_status": "unapproved"}, "preview": {}},
            {"candidate.pptx": compiled.candidate, "candidate.pdf": b"%PDF-parent-fixture", "page-1.png": b"preview-fixture"},
            IMAGE,
        ),
    )
    draft = await delivery.get(scope=SCOPE, job_id=parent)
    source = revision_source(ctx, parent, draft["draft_digest"], index="worker")
    source = replace(
        source, session_id="s_pipeline", document_revision_target=source.document_revision_target.model_copy(update={"session_id": "s_pipeline"})
    )
    successor = await ctx.store.submit_revision_from_turn(**await admit_source(ctx, source))
    worker, calls = HostWorker(tmp_path / "revision-worker"), []
    await run_document(
        delivery=delivery,
        executor=worker,
        scope=SCOPE,
        job_id=successor.job_id,
        invoke=transport([json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison period?"})], calls),
        expected_image_id=IMAGE,
    )
    saved = await delivery.get(scope=SCOPE, job_id=successor.job_id)
    assert saved["state"] == "question", saved
    payload = json.loads(calls[0])
    assert INSTRUCTION in payload["instruction"] and case[1] not in payload["instruction"]
    texts = {slot["text"] for slot in payload["slots"]}
    assert {"Quarter two", "53", "21"} <= texts and "Quarter one" not in texts
    assert len(calls) == 1 and worker.runs == ["inventory", "apply_verify"]


async def test_native_revision_service_repairs_quote_once_and_recovers_without_runtime(ctx, admission, monkeypatch):
    delivery, parent, draft, _ = await _review(ctx)
    source = revision_source(ctx, parent, draft["draft_digest"])
    await finish_chat_turn(ctx, source.session_id)
    await ctx.sessions.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text, user_source=source)
    admission.context.turn_id = source.turn_id
    admission.context.document_revision_target = source.document_revision_target
    admission.context.document_turn_state = DocumentTurnState(revision_required=True)
    invalid = await invoke(admission, {"action": "revise", "evidence": {"text": "Absent user quotation"}})
    assert invalid["error"] == "document_evidence_mismatch" and not invalid["document_terminal"]
    admission.service.authorize_model.assert_not_awaited()
    admission.context.document_admission_binding.assert_not_awaited()
    assert not admission.requests
    arguments = {"action": "revise", "evidence": {"text": source.user_text}}
    accepted = await invoke(admission, arguments)
    assert accepted["ok"] and accepted["state"] == "accepted"
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "cancelled"
    admission.service.authorize_model.assert_awaited_once()
    admission.context.document_admission_binding.assert_awaited_once()
    assert len(admission.requests) == 1
    await ctx.sessions.mark_cancelled(turn_id=source.turn_id)
    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "0")
    admission.context.document_turn_state = DocumentTurnState(revision_required=True)
    assert await invoke(admission, arguments) == accepted
    admission.service.authorize_model.assert_awaited_once()
    admission.context.document_admission_binding.assert_awaited_once()
    assert len(admission.requests) == 1


async def test_revision_receipt_names_the_revised_draft_and_counts_revisions_only(ctx):
    from types import SimpleNamespace

    from bowhead_core.documents.revision import draft_display_title
    from bowhead_core.turn_runner import _document_revision_receipt

    delivery, parent, draft, _ = await _review(ctx)
    source = revision_source(ctx, parent, draft["draft_digest"])
    target = source.document_revision_target
    title = draft_display_title(json.dumps(draft["draft"]))
    assert title and title.endswith(".pptx") and "djob_" not in title
    assert (await delivery.get(scope=SCOPE, job_id=parent))["draft_version"] == 1
    receipt = await ctx.sessions.get_document_revision_receipt(
        target=target, session_id=source.session_id, user_id=source.user_id, workspace_id=source.workspace_id
    )
    assert receipt == {"title": title, "version": 1}
    # The turn_started projection carries the same facts; it is persisted with turn_started, so replay agrees.
    request = SimpleNamespace(
        document_revision_target=target,
        session_id=source.session_id,
        user_id=source.user_id,
        workspace_id=source.workspace_id,
        turn_id=source.turn_id,
    )
    assert await _document_revision_receipt(request, ctx.sessions) == {"document_revision": {"title": title, "version": 1}}
    # Another draft, another user or another Space never reads this draft's name.
    stale = target.model_copy(update={"draft_digest": "0" * 64})
    assert (
        await ctx.sessions.get_document_revision_receipt(
            target=stale, session_id=source.session_id, user_id=source.user_id, workspace_id=source.workspace_id
        )
        is None
    )
    assert (
        await ctx.sessions.get_document_revision_receipt(
            target=target, session_id=source.session_id, user_id="u_other", workspace_id=source.workspace_id
        )
        is None
    )
    assert (
        await ctx.sessions.get_document_revision_receipt(target=target, session_id=source.session_id, user_id=source.user_id, workspace_id="ws_other")
        is None
    )
    # The requesting Turn's own session must be the draft's session.
    assert (
        await ctx.sessions.get_document_revision_receipt(
            target=target, session_id="s_other", user_id=source.user_id, workspace_id=source.workspace_id
        )
        is None
    )
    assert await _document_revision_receipt(SimpleNamespace(**{**vars(request), "document_revision_target": stale}), ctx.sessions) == {}
    # The successor of one revision is draft 2.
    successor = await ctx.store.submit_revision_from_turn(**await admit_source(ctx, source))
    assert (await delivery.get(scope=SCOPE, job_id=successor.job_id))["draft_version"] == 2


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        ({"output_stem": "Q2 Review - Updated", "files": {"candidate.pptx": {}, "preview.pdf": {}}}, "Q2 Review - Updated.pptx"),
        ({"output_stem": "Q2 Review.pptx", "files": {"candidate.pptx": {}}}, "Q2 Review.pptx"),
        ({"output_stem": "Q2 Review", "files": {"preview.pdf": {}}}, "Q2 Review"),
        ({"output_stem": "  ", "files": {"candidate.pptx": {}}}, None),
        ({"files": {"candidate.pptx": {}}}, None),
    ],
)
def test_draft_display_title_matches_the_canvas_title(draft, expected):
    from bowhead_core.documents.revision import draft_display_title

    assert draft_display_title(json.dumps(draft)) == expected
    assert draft_display_title(None) is None
    assert draft_display_title("not json") is None
