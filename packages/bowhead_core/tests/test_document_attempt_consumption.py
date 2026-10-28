"""Domain consume uses the real card, reservation and caller transaction.

The shared lifecycle owns generation orchestration. These fixtures create its
actual persisted boundary under the production SQL guards, not a success mock.
"""

import json

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.documents.continuation import consume_document_attempt_in_tx, load_document_checkpoint_in_tx
from bowhead_core.documents.delivery import _fence
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.usage import DocumentModelHook, settle_document_usage
from bowhead_core.rate_card import RATE_CARD_VERSION, ModelTokenUsage, credits_for_tokens_at_version
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.types import TokenUsage
from test_document_planning_continuation import QUESTION, checkpoint, link
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_usage import SPEC

pytestmark = pytest.mark.integration
ANSWER = "Compare Q2 with Q1"


async def ready(ctx):
    case = await checkpoint(ctx)
    store = ClarificationStore(ctx.path)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        material = await load_document_checkpoint_in_tx(db, checkpoint=case.ref, creation_worker=case.worker)
        card = await store.put(
            **case.worker.model_dump(include={"turn_id", "user_id", "workspace_id", "session_id"}),
            token="",
            rationale=QUESTION,
            candidates=[],
            kind="ambiguous_turn",
            connection=db,
            resume_context={
                "__schema_version": 3,
                **case.worker.model_dump(exclude={"generation", "owner_id"}),
                "user_text": ctx.source.user_text,
                "continuation": {
                    "kind": "document_planning",
                    "version": 1,
                    "checkpoint": case.ref.model_dump(),
                    "runtime_snapshot_digest": material.runtime_snapshot_digest,
                    "creation_generation": case.worker.generation,
                    "creation_owner_id": case.worker.owner_id,
                },
            },
        )
        await db.execute(
            """INSERT INTO clarification_document_continuations
          (card_id,job_id,attempt_no,turn_id,creation_generation,creation_owner_id,checkpoint_digest,source_hash,runtime_snapshot_digest,created_at)
          VALUES (?,?,1,?,?,?,?,?,?,?)""",
            (
                card.request_id,
                case.job_id,
                case.worker.turn_id,
                case.worker.generation,
                case.worker.owner_id,
                case.ref.checkpoint_digest,
                case.ref.source_hash,
                material.runtime_snapshot_digest,
                card.created_at,
            ),
        )
        await db.commit()
    assert await ctx.sessions.mark_clarification_pending(turn_id=case.worker.turn_id, intent="ambiguous", error_kind="needs_clarification")
    await TurnLogStore(str(ctx.path)).record(
        turn_id=case.worker.turn_id, event_type="done", payload={"turn_id": case.worker.turn_id, "status": "clarification_pending"}
    )
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id=case.worker.turn_id, owner_id=case.worker.owner_id)
    _, won = await store.resolve_once(
        card.request_id,
        chosen_value=ANSWER,
        answers=[{"question": QUESTION, "source": "custom", "answer": ANSWER, "skipped": False, "option_index": None}],
    )
    assert won
    # Materialize the shared reservation/new spawned worker boundary. This
    # does not claim to test shared HTTP, event or generation orchestration.
    worker = case.worker.model_copy(update={"generation": 2, "owner_id": "resumed-worker"})
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE clarification_document_continuations SET reserved_generation=2 WHERE card_id=?", (card.request_id,))
        await db.execute("UPDATE turns SET status='running',execution_generation=2 WHERE id=?", (worker.turn_id,))
        await db.execute("UPDATE sessions SET active_turn_id=? WHERE id=?", (worker.turn_id, worker.session_id))
        await db.execute(
            """UPDATE chat_turn_spawn_dispatch SET state='spawned',owner_id=?,lease_kind='resume',
          lease_execution_generation=2,lease_expires_at=datetime('now','+10 minutes') WHERE turn_id=?""",
            (worker.owner_id, worker.turn_id),
        )
        await db.commit()
    case.resumed_worker, case.card = worker, card
    return case


async def consume(db, case):
    return await consume_document_attempt_in_tx(db, worker=case.resumed_worker, checkpoint=case.ref, card_id=case.card.request_id, answer_text=ANSWER)


async def associate(db, case):
    await db.execute(
        "UPDATE clarification_document_continuations SET consumed_generation=?,consumed_owner_id=? WHERE card_id=?",
        (case.resumed_worker.generation, case.resumed_worker.owner_id, case.card.request_id),
    )


async def rows(ctx, sql, params=()):
    async with open_shared_ctx(ctx.path) as db, db.execute(sql, params) as cursor:
        return await cursor.fetchall()


async def test_consumption_keeps_attempt_owner_context_and_association_atomic(ctx):
    case = await ready(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        attempt = await consume(db, case)
        assert attempt.attempt_no == 2 and attempt.worker == case.resumed_worker
        with pytest.raises(DocumentRequestError, match="document_execution_fenced"):
            await _fence(db, SCOPE, case.job_id, case.resumed_worker.owner_id)
        await associate(db, case)
        assert await _fence(db, SCOPE, case.job_id, case.resumed_worker.owner_id)
        assert db.in_transaction
        await db.rollback()
    assert await rows(ctx, "SELECT attempt_no FROM document_planning_attempts") == [(1,)]
    assert await rows(ctx, "SELECT state,owner FROM document_executions") == [("question", case.worker.owner_id)]
    assert await rows(ctx, "SELECT reserved_generation,consumed_generation FROM clarification_document_continuations") == [(2, None)]
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await consume(db, case)
        await associate(db, case)
        await db.commit()
    context = json.loads((await rows(ctx, "SELECT context_json FROM document_planning_attempts WHERE attempt_no=2"))[0][0])
    payload = json.loads(context["payload"])
    assert any(fact["path"] == "/clarification/answer" and fact["value"] == ANSWER for fact in payload["facts"])
    assert payload["slots"] == json.loads(case.context["payload"])["slots"]
    assert await rows(ctx, "SELECT count(*) FROM document_model_calls") == [(1,)]
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(DocumentRequestError):
            await consume(db, case)


async def test_shared_consumption_failure_rolls_back_domain_attempt(ctx):
    case = await ready(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "CREATE TRIGGER fail_consume BEFORE UPDATE OF consumed_generation ON clarification_document_continuations BEGIN SELECT RAISE(ABORT,'injected consume failure'); END"
        )
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="injected consume failure"):
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await consume(db, case)
            await associate(db, case)
            await db.commit()
    assert await rows(ctx, "SELECT attempt_no FROM document_planning_attempts") == [(1,)]
    assert await rows(ctx, "SELECT state,owner FROM document_executions") == [("question", case.worker.owner_id)]


@pytest.mark.parametrize("change", ["answer", "card", "scope", "generation", "owner", "model", "revoked", "expired", "cancel"])
async def test_consume_refuses_stale_or_substituted_answer_and_authority(ctx, change):
    case = await ready(ctx)
    worker, card, answer = case.resumed_worker, case.card.request_id, ANSWER
    if change == "answer":
        answer = "A different answer"
    elif change == "card":
        card = "cq_other"
    elif change in {"scope", "generation", "owner"}:
        key = {"scope": "workspace_id", "generation": "generation", "owner": "owner_id"}[change]
        worker = worker.model_copy(update={key: 3 if change == "generation" else "another"})
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if change == "model":
            await db.execute("UPDATE teams SET llm_config_version=2 WHERE id=?", (worker.team_id,))
        elif change == "revoked":
            await db.execute("UPDATE workspace_members SET role='viewer' WHERE user_id=?", (worker.user_id,))
        elif change == "expired":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01'")
        elif change == "cancel":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='2026-10-04'")
        with pytest.raises(DocumentRequestError):
            await consume_document_attempt_in_tx(db, worker=worker, checkpoint=case.ref, card_id=card, answer_text=answer)
        async with db.execute("SELECT count(*) FROM document_planning_attempts") as cursor:
            assert (await cursor.fetchone())[0] == 1


async def test_resumed_first_call_has_distinct_observation_and_exact_cumulative_charge(ctx):
    case = await ready(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await consume(db, case)
        await associate(db, case)
        await db.commit()
    await link(ctx, case, receipt_id="lu_resumed_document", worker=case.resumed_worker, attempt_no=2)
    hook = DocumentModelHook(str(ctx.path), SCOPE, case.job_id, case.resumed_worker.owner_id, 1, attempt_no=2)
    await hook.begin(SPEC)
    with pytest.raises(InvocationOutcomeUnknownError):
        await hook.begin(SPEC)
    await hook.complete(usage=TokenUsage(700, 300), response_digest="c" * 64)
    await hook.complete(usage=TokenUsage(700, 300), response_digest="c" * 64)
    await settle_document_usage(str(ctx.path), case.job_id)
    assert await rows(ctx, "SELECT attempt_no,call_no,state FROM document_model_calls ORDER BY attempt_no") == [
        (1, 1, "completed"),
        (2, 1, "completed"),
    ]
    assert {row[0] for row in await rows(ctx, "SELECT id FROM credit_ledger WHERE reason='template_document'")} == {
        f"document:{case.job_id}:1",
        f"document:{case.job_id}:attempt:2:call:1",
    }
    # 5bca471f7: v2 settlement sums classified model usage (default rates for this spec) before rounding once.
    expected = credits_for_tokens_at_version(RATE_CARD_VERSION, 1300, 500, model_usage=[ModelTokenUsage(1300, 0, 500)])
    assert await rows(ctx, "SELECT settled_credits FROM document_executions") == [(expected,)]
    assert await rows(ctx, "SELECT count(DISTINCT receipt_id) FROM document_physical_receipt_links") == [(2,)]
