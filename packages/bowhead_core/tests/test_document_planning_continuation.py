"""Exact checkpoint storage and worker fences on the migrated domain Stores."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.continuation import (
    DocumentCallRef,
    DocumentRuntimeSnapshot,
    begin_document_planning_in_tx,
    current_document_call,
    document_call_scope,
    link_physical_receipt_in_tx,
    load_document_checkpoint_in_tx,
    register_document_turn_in_tx,
    retain_document_context_in_tx,
    retain_document_question_in_tx,
    validate_document_pause_in_tx,
    validate_document_resume_in_tx,
)
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, prepare_planning_context
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.store import _json
from bowhead_core.documents.usage import DocumentModelHook
from bowhead_core.language_context import LanguageContext
from bowhead_router.types import TokenUsage
from pydantic import ValidationError
from test_document_capability import runtime
from test_document_pptx_text import _deck
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_usage import SPEC

pytestmark = pytest.mark.integration
QUESTION = "Which comparison period should the presentation use?"


async def planning(ctx):
    # Supply real bounded template bytes before admission freezes this fixture.
    source_bytes = _deck()
    Path(ctx.artifact.path).write_bytes(source_bytes)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE artifacts SET size=? WHERE id=?", (len(source_bytes), ctx.artifact.id))
        await db.commit()
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=1, owner_id="document-worker")
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id=worker.turn_id, owner_id=worker.owner_id, lease_seconds=600) == "claimed"
    admitted = await runtime(ctx)
    receipt = await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="turn-planning", proposal=ctx.proposal, runtime=admitted)
    snapshot = DocumentRuntimeSnapshot(
        schema_version=1,
        image_id=admitted.image_id,
        model_config_version=admitted.model_config_version,
        planner_profile="pptx-grounded-plan-v4",
        language=LanguageContext(output_language="fr-FR").to_dict(),
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await register_document_turn_in_tx(db, worker=worker, job_id=receipt.job_id, runtime=snapshot)
        await db.execute("UPDATE teams SET llm_config_version=1,team_credit_balance=100 WHERE id=?", (SCOPE.team_id,))
        await db.execute("UPDATE users SET credit_balance=100 WHERE id=?", (SCOPE.user_id,))
        await db.commit()
    # The scanner must refuse even before an execution row exists.
    assert await DocumentDeliveryStore(ctx.artifacts).claim(scope=SCOPE, job_id=receipt.job_id) is None
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    # Native inventory/rendering is an external boundary in this Store fixture.
    context = asdict(
        prepare_planning_context(
            source_bytes,
            ctx.source.user_text,
            (JsonEvidenceSource("User request", json.dumps({"request": ctx.source.user_text}, ensure_ascii=False).encode()),),
        )
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await begin_document_planning_in_tx(db, worker=worker, job_id=receipt.job_id, instruction=ctx.source.user_text)
        await retain_document_context_in_tx(db, worker=worker, job_id=receipt.job_id, attempt_no=1, context=context)
        await db.commit()
    delivery = DocumentDeliveryStore(ctx.artifacts)
    await delivery.transition(
        scope=SCOPE, job_id=receipt.job_id, owner=worker.owner_id, state="planning", image_id=admitted.image_id, context_json=_json(context)
    )
    return SimpleNamespace(worker=worker, job_id=receipt.job_id, delivery=delivery, snapshot=snapshot, context=context)


async def test_inventory_cannot_replace_a_frozen_runtime_planner(ctx):
    from bowhead_core.documents.pptx_plan import CURRENT_PROFILE

    case = await planning(ctx)
    source = Path(ctx.artifact.path).read_bytes()
    context = asdict(
        prepare_planning_context(
            source,
            ctx.source.user_text,
            (JsonEvidenceSource("User request", json.dumps({"request": ctx.source.user_text}, ensure_ascii=False).encode()),),
            profile=CURRENT_PROFILE,
        )
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(DocumentRequestError, match="document_checkpoint_unavailable"):
            await retain_document_context_in_tx(db, worker=case.worker, job_id=case.job_id, attempt_no=1, context=context)
        async with db.execute("SELECT context_json FROM document_planning_attempts WHERE job_id=?", (case.job_id,)) as cursor:
            assert json.loads((await cursor.fetchone())[0]) == case.context


async def link(ctx, case, *, receipt_id="lu_document_fixture", spec=SPEC, worker=None, attempt_no=1):
    worker = worker or case.worker
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        # Observer-owned input for the domain transaction adapter. The shared
        # observer integration separately exercises its real transport seam.
        await db.execute(
            """INSERT INTO llm_usage_events(id,team_id,user_id,workspace_id,turn_id,reason,agent_type,provider,model,streamed,created_at,receipt_json,receipt_state)
          VALUES (?,?,?,?,?,'template_document','lead',?,?,0,'2026-10-03',?,'pending')""",
            (
                receipt_id,
                SCOPE.team_id,
                SCOPE.user_id,
                SCOPE.workspace_id,
                worker.turn_id,
                spec.provider,
                spec.model,
                _json({"generation": worker.generation, "request_digest": spec.request_digest}),
            ),
        )
        await link_physical_receipt_in_tx(db, call=DocumentCallRef(case.job_id, attempt_no, 1, worker), spec=spec, receipt_id=receipt_id)
        await db.commit()


async def checkpoint(ctx, *, physical_state="completed"):
    case = await planning(ctx)
    await link(ctx, case)
    hook = DocumentModelHook(str(ctx.path), SCOPE, case.job_id, case.worker.owner_id, 1)
    await hook.begin(SPEC)
    await hook.complete(usage=TokenUsage(600, 200), response_digest="b" * 64)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "UPDATE llm_usage_events SET receipt_state=?,response_digest=?,tokens_input=600,tokens_output=200,tokens_cached=0 WHERE id='lu_document_fixture'",
            (physical_state, "b" * 64),
        )
        await db.commit()
    await case.delivery.transition(
        scope=SCOPE,
        job_id=case.job_id,
        owner=case.worker.owner_id,
        state="rendering",
        response=_json({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": QUESTION}),
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        case.ref = await retain_document_question_in_tx(db, worker=case.worker, job_id=case.job_id, question=QUESTION, last_call_no=1)
        await db.commit()
    return case


@pytest.mark.parametrize("physical_state", ["pending", "unknown", "usage_missing", "not_applied"])
async def test_checkpoint_cannot_pause_on_uncertain_observation_even_with_known_charge(ctx, physical_state):
    with pytest.raises(DocumentRequestError, match="document_usage_unresolved"):
        await checkpoint(ctx, physical_state=physical_state)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT checkpoint_digest FROM document_planning_attempts") as cursor:
        assert (await cursor.fetchone())[0] is None


async def test_exact_checkpoint_retains_language_usage_and_caller_transaction(ctx):
    case = await checkpoint(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        material = await validate_document_pause_in_tx(db, worker=case.worker, checkpoint=case.ref)
        assert material.question == QUESTION and material.runtime_snapshot.language.output_language == "fr-FR"
        assert material.provider == SPEC.provider and material.model == SPEC.model
        assert db.in_transaction
        await db.execute("UPDATE sessions SET title='Uncommitted title' WHERE id='s_doc'")
        await db.rollback()
        async with db.execute("SELECT title FROM sessions WHERE id='s_doc'") as cursor:
            assert (await cursor.fetchone())[0] != "Uncommitted title"
        async with db.execute("SELECT id FROM credit_ledger WHERE reason='template_document'") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [f"document:{case.job_id}:1"]


@pytest.mark.parametrize("change", ["owner", "generation", "session", "digest", "source", "revoked", "cancel"])
async def test_checkpoint_reader_refuses_wrong_or_unavailable_material(ctx, change):
    case = await checkpoint(ctx)
    worker, ref = case.worker, case.ref
    if change in {"owner", "generation", "session"}:
        key = {"owner": "owner_id", "generation": "generation", "session": "session_id"}[change]
        worker = worker.model_copy(update={key: 2 if key == "generation" else "another"})
    elif change in {"digest", "source"}:
        ref = ref.model_copy(update={"checkpoint_digest" if change == "digest" else "source_hash": "0" * 64})
    if change == "cancel":
        await ctx.store.cancel(scope=SCOPE, job_id=case.job_id, command_id="cancel", expected_proposal_revision=1)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if change == "revoked":
            await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        with pytest.raises(DocumentRequestError):
            await load_document_checkpoint_in_tx(db, checkpoint=ref, creation_worker=worker)


async def test_pending_checkpoint_keeps_private_inputs_protected_from_cleanup(ctx):
    import aiosqlite

    case = await checkpoint(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(aiosqlite.IntegrityError, match="artifact_in_use"):
            await db.execute(
                "UPDATE artifacts SET status='missing' WHERE id IN (SELECT artifact_id FROM document_input_copies WHERE job_id=?)", (case.job_id,)
            )
        assert await load_document_checkpoint_in_tx(db, checkpoint=case.ref, creation_worker=case.worker)


async def test_scanner_claim_and_recovery_cannot_take_turn_owned_job(ctx):
    case = await planning(ctx)
    assert await case.delivery.claim(scope=SCOPE, job_id=case.job_id) is None
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE document_executions SET lease_until=0 WHERE job_id=?", (case.job_id,))
        await db.commit()
    await case.delivery.recover_expired()
    assert (await case.delivery.get(scope=SCOPE, job_id=case.job_id))["state"] == "planning"


@pytest.mark.parametrize("change", ["owner", "generation", "expired", "cancel"])
async def test_turn_owner_loss_fences_model_before_dispatch(ctx, change):
    case = await planning(ctx)
    await link(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        if change == "owner":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='another' WHERE turn_id='t_doc'")
        elif change == "generation":
            await db.execute("UPDATE turns SET execution_generation=2 WHERE id='t_doc'")
        elif change == "expired":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01' WHERE turn_id='t_doc'")
        else:
            await db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='2026-10-03' WHERE turn_id='t_doc'")
        await db.commit()
    hook = DocumentModelHook(str(ctx.path), SCOPE, case.job_id, case.worker.owner_id, 1)
    with pytest.raises(DocumentRequestError, match="document_execution_fenced"):
        await hook.begin(SPEC)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_model_calls") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_resume_accepts_settled_original_owner_but_not_live_or_changed_policy(ctx):
    case = await checkpoint(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(DocumentRequestError):
            await validate_document_resume_in_tx(db, checkpoint=case.ref, creation_worker=case.worker)
        await db.execute("UPDATE turns SET status='clarification_pending' WHERE id='t_doc'")
        await db.execute("UPDATE chat_turn_spawn_dispatch SET state='terminal' WHERE turn_id='t_doc'")
        material = await validate_document_resume_in_tx(db, checkpoint=case.ref, creation_worker=case.worker)
        assert material.creation_worker == case.worker
        await db.execute("UPDATE teams SET llm_config_version=2 WHERE id=?", (SCOPE.team_id,))
        with pytest.raises(DocumentRequestError, match="document_model_configuration_changed"):
            await validate_document_resume_in_tx(db, checkpoint=case.ref, creation_worker=case.worker)


async def test_missing_observation_link_does_not_authorize_turn_model(ctx):
    import aiosqlite

    case = await planning(ctx)
    hook = DocumentModelHook(str(ctx.path), SCOPE, case.job_id, case.worker.owner_id, 1)
    with pytest.raises(aiosqlite.IntegrityError, match="document model dispatch unavailable"):
        await hook.begin(SPEC)


@pytest.mark.parametrize("change", ["missing_language", "extra_language", "invalid_language", "wrong_image"])
def test_runtime_snapshot_never_fills_missing_or_invalid_language(change):
    value = {
        "schema_version": 1,
        "image_id": "sha256:" + "b" * 64,
        "model_config_version": 1,
        "planner_profile": "pptx-grounded-plan-v4",
        "language": LanguageContext().to_dict(),
    }
    if change == "missing_language":
        value["language"].pop("data_language")
    elif change == "extra_language":
        value["language"]["unknown"] = "en-US"
    elif change == "invalid_language":
        value["language"]["output_language"] = "auto"
    else:
        value["image_id"] = "latest"
    with pytest.raises(ValidationError):
        DocumentRuntimeSnapshot.model_validate(value)


def test_document_call_scope_restores_original_identity():
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=1, owner_id="worker")
    call = DocumentCallRef("djob_test", 1, 1, worker)
    assert current_document_call() is None
    with document_call_scope(call):
        assert current_document_call() is call
    assert current_document_call() is None
