"""Physical dispatch receipts, no transport replay and exact cumulative billing."""

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.usage import DocumentModelHook
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import PhysicalInvocationSpec
from bowhead_router.types import TokenUsage
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
SPEC = PhysicalInvocationSpec("configured-provider", "configured-model", "test.v1", "invoke", "a" * 64)


async def _hook(ctx):
    receipt = await ctx.submit()
    delivery = DocumentDeliveryStore(ctx.artifacts)
    owner = await delivery.claim(scope=SCOPE, job_id=receipt.job_id)
    await delivery.transition(scope=SCOPE, job_id=receipt.job_id, owner=owner, state="planning")
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE users SET credit_balance=100 WHERE id=?", (SCOPE.user_id,))
        await db.execute("UPDATE teams SET team_credit_balance=100 WHERE id=?", (SCOPE.team_id,))
        await db.commit()
    return delivery, DocumentModelHook(str(ctx.path), SCOPE, receipt.job_id, owner, 1)


async def test_physical_retry_cannot_send_again_after_unknown(ctx):
    _, hook = await _hook(ctx)
    await hook.begin(SPEC)
    await hook.unknown(reason_code="transport_disconnected")
    with pytest.raises(InvocationOutcomeUnknownError):
        await hook.begin(SPEC)


async def test_delivered_usage_after_cancel_is_settled_once(ctx):
    _, hook = await _hook(ctx)
    await hook.begin(SPEC)
    await ctx.store.cancel(scope=SCOPE, job_id=hook.job_id, command_id="cancel", expected_proposal_revision=1)
    usage = TokenUsage(600, 200, 300)
    assert await hook.complete(usage=usage, response_digest="b" * 64)
    assert await hook.complete(usage=usage, response_digest="b" * 64)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT credit_balance FROM users WHERE id=?", (SCOPE.user_id,)) as cursor:
            assert (await cursor.fetchone())[0] == 99
        async with db.execute("SELECT turn_id,tokens_input,tokens_output FROM credit_ledger WHERE reason='template_document'") as cursor:
            assert list(await cursor.fetchall()) == [(None, 600, 200)]


async def test_document_v2_uses_frozen_model_rates_and_cached_input(ctx):
    _, hook = await _hook(ctx)
    spec = PhysicalInvocationSpec(
        "configured-provider",
        "configured-model",
        "test.v1",
        "invoke",
        "a" * 64,
        credit_rates={"input": "2", "cached_input": "0.25", "output": "4"},
        usage_family="openai",
    )
    await hook.begin(spec)
    assert await hook.complete(usage=TokenUsage(800, 100, 600), response_digest="b" * 64)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT rate_card_version,delta FROM credit_ledger WHERE reason='template_document'") as cursor:
            assert await cursor.fetchone() == ("v2", -1)
        async with db.execute(
            "SELECT model,tokens_input,tokens_cached,tokens_output,rate_input,rate_cached,rate_output FROM credit_model_usage"
        ) as cursor:
            assert await cursor.fetchone() == ("configured-model", 200, 600, 100, "2", "0.25", "4")


async def test_second_completed_call_charges_only_cumulative_difference(ctx):
    delivery, first = await _hook(ctx)
    await first.begin(SPEC)
    assert await first.complete(usage=TokenUsage(600, 100), response_digest="b" * 64)
    await delivery.transition(scope=SCOPE, job_id=first.job_id, owner=first.owner, state="rendering")
    await delivery.transition(scope=SCOPE, job_id=first.job_id, owner=first.owner, state="planning", error_code="planning_invalid_response")
    second = DocumentModelHook(first.path, first.scope, first.job_id, first.owner, 2)
    await second.begin(SPEC)
    assert await second.complete(usage=TokenUsage(100, 100), response_digest="c" * 64)
    async with (
        open_shared_ctx(ctx.path) as db,
        db.execute("SELECT SUM(delta),SUM(tokens_input),SUM(tokens_output) FROM credit_ledger WHERE reason='template_document'") as cursor,
    ):
        assert tuple(await cursor.fetchone()) == (-1, 700, 200)


async def test_settlement_failure_retains_usage_and_recovers_without_reinvocation(ctx, monkeypatch):
    import asyncio

    from bowhead_core.documents import usage as module
    from bowhead_router.invocation import complete_physical_invocation

    delivery, hook = await _hook(ctx)
    await hook.begin(SPEC)
    real_settle = module._deduct_credit_with_stable_id_in_tx

    async def unavailable(*args, **kwargs):
        raise OSError("Temporary settlement outage")

    monkeypatch.setattr(module, "_deduct_credit_with_stable_id_in_tx", unavailable)
    with pytest.raises(InvocationOutcomeUnknownError):
        await complete_physical_invocation(hook, usage=TokenUsage(600, 200), response={"content": "Known response"})
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT tokens_input,tokens_output,settled_at FROM document_usage_receipts") as cursor:
        assert tuple(await cursor.fetchone()) == (600, 200, None)
    assert (await delivery.get(scope=SCOPE, job_id=hook.job_id))["usage"]["settlement_pending"] is True
    await ctx.store.cancel(scope=SCOPE, job_id=hook.job_id, command_id="cancel", expected_proposal_revision=1)
    monkeypatch.setattr(module, "_deduct_credit_with_stable_id_in_tx", real_settle)
    await asyncio.gather(module.settle_pending_usage(str(ctx.path)), module.settle_document_usage(str(ctx.path), hook.job_id))
    await module.settle_document_usage(str(ctx.path), hook.job_id)
    status = await delivery.get(scope=SCOPE, job_id=hook.job_id)
    assert status["state"] == "cancelled" and status["usage"] == {"settled_credits": 1, "pending": False, "settlement_pending": False}
    async with (
        open_shared_ctx(ctx.path) as db,
        db.execute("SELECT count(*),SUM(delta) FROM credit_ledger WHERE reason='template_document'") as cursor,
    ):
        assert tuple(await cursor.fetchone()) == (1, -1)


async def test_late_known_usage_settles_unknown_call_without_restoring_execution(ctx):
    from bowhead_core.documents.request import DocumentRequestError

    delivery, hook = await _hook(ctx)
    await hook.begin(SPEC)
    await hook.unknown(reason_code="transport_disconnected")
    await delivery.fail(job_id=hook.job_id, owner=hook.owner, code="document_usage_unresolved")
    assert await hook.complete(usage=TokenUsage(600, 200), response_digest="b" * 64)
    assert not await hook.complete(usage=TokenUsage(601, 200), response_digest="b" * 64)
    status = await delivery.get(scope=SCOPE, job_id=hook.job_id)
    assert status["state"] == "failed" and status["usage"]["pending"] is False
    assert status["error_code"] == "document_execution_interrupted"
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT state FROM document_model_calls") as cursor:
        assert (await cursor.fetchone())[0] == "unknown"
    with pytest.raises(DocumentRequestError, match="document_execution_fenced"):
        await hook.begin(SPEC)
