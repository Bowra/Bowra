"""One observer link and one charge owner, with separate inclusive display."""

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.document_chat_usage import document_usage_projection
from bowhead_core.documents.continuation import DocumentCallRef, document_call_scope
from bowhead_core.documents.usage import DocumentModelHook
from bowhead_core.physical_usage import PhysicalUsageObserver
from bowhead_router.types import TokenUsage
from bowhead_router.usage import usage_context
from cryptography.fernet import Fernet
from test_document_planning_continuation import planning
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_usage import SPEC

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def observe(ctx, case):
    observer = PhysicalUsageObserver(
        str(ctx.path), cipher=ReferenceCipher(Fernet.generate_key().decode()), turn_id=case.worker.turn_id, generation=case.worker.generation
    )
    with (
        usage_context(**SCOPE.model_dump(), turn_id=case.worker.turn_id, reason="template_document"),
        document_call_scope(DocumentCallRef(case.job_id, 1, 1, case.worker)),
    ):
        return await observer.begin(SPEC, request=None, body={}, family="openai")


async def test_actual_observer_links_before_authority_and_charges_once(ctx):
    case = await planning(ctx)
    attempt = await observe(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute(
            "SELECT l.receipt_id,u.receipt_state FROM document_physical_receipt_links l JOIN llm_usage_events u ON u.id=l.receipt_id"
        ) as cursor:
            assert await cursor.fetchall() == [(attempt.identity, "pending")]
        async with db.execute("SELECT count(*) FROM document_model_calls") as cursor:
            assert (await cursor.fetchone())[0] == 0
    hook = DocumentModelHook(str(ctx.path), SCOPE, case.job_id, case.worker.owner_id, 1)
    await hook.begin(SPEC)
    usage = TokenUsage(600, 200)
    assert await hook.complete(usage=usage, response_digest="b" * 64)
    await attempt.complete(usage=usage, response_digest="b" * 64)
    assert await hook.complete(usage=usage, response_digest="b" * 64)
    display = await document_usage_projection(ctx.path, turn_id=case.worker.turn_id, generation_input=17, generation_output=9)
    assert display == {"tokens_input": 617, "tokens_output": 209, "partial": False}
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT id FROM credit_ledger WHERE reason='template_document'") as cursor:
            assert await cursor.fetchall() == [(f"document:{case.job_id}:1",)]
        async with db.execute("SELECT tokens_input,tokens_output FROM turns WHERE id='t_doc'") as cursor:
            assert await cursor.fetchone() == (0, 0)


async def test_link_failure_rolls_back_observer_without_call_permission(ctx):
    case = await planning(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("CREATE TRIGGER fail_link BEFORE INSERT ON document_physical_receipt_links BEGIN SELECT RAISE(ABORT,'link fault'); END")
        await db.commit()
    with pytest.raises(Exception, match="link fault"):
        await observe(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        for table in ("llm_usage_events", "document_physical_receipt_links", "document_model_calls", "credit_ledger"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0


async def test_late_known_usage_updates_display_without_reopening_unknown_call(ctx):
    case = await planning(ctx)
    attempt = await observe(ctx, case)
    hook = DocumentModelHook(str(ctx.path), SCOPE, case.job_id, case.worker.owner_id, 1)
    await hook.begin(SPEC)
    await hook.unknown(reason_code="transport_disconnected")
    await attempt.unknown(reason_code="transport_disconnected")
    await ctx.sessions.persist_turn_summary(turn_id=case.worker.turn_id, total_ms=100, tools_count=1, llm_count=1, tokens_input=0, tokens_output=0)
    assert await document_usage_projection(ctx.path, turn_id=case.worker.turn_id) == {"tokens_input": 0, "tokens_output": 0, "partial": True}
    assert (await ctx.sessions.get_turn_summaries([case.worker.turn_id]))[case.worker.turn_id].usage_partial
    assert await hook.complete(usage=TokenUsage(600, 200), response_digest="b" * 64)
    assert await document_usage_projection(ctx.path, turn_id=case.worker.turn_id) == {"tokens_input": 600, "tokens_output": 200, "partial": False}
    summary = (await ctx.sessions.get_turn_summaries([case.worker.turn_id]))[case.worker.turn_id]
    assert (summary.tokens_input, summary.tokens_output, summary.usage_partial) == (600, 200, False)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT receipt_state FROM llm_usage_events") as cursor:
            assert await cursor.fetchall() == [("unknown",)]
        async with db.execute("SELECT count(*) FROM credit_ledger WHERE reason='template_document'") as cursor:
            assert (await cursor.fetchone())[0] == 1
