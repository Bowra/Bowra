"""Canonical admission and exact Capability receipts on a migrated database."""

import json

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.store import DocumentRuntimeAdmission
from bowhead_core.subagent import _system_profile_snapshot
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def runtime(ctx, *, turn_id="t_doc", scope=SCOPE):
    """Admit an explicit test binding independently of edition entry points."""
    registry = AgentRegistry.bootstrap()
    profile = manifest_to_profile(registry.get("lead"))
    definition = _system_profile_snapshot(profile)
    definition["tool_whitelist"] = sorted(set(definition.get("tool_whitelist", [])) | {"template_document_submit"})
    binding = await AgentExecutionBindingStore(ctx.path).admit_system_execution(
        team_id=scope.team_id,
        user_id=scope.user_id,
        turn_id=turn_id,
        profile_name="lead",
        dispatch_key="document-admission:" + turn_id,
        definition=definition,
        manifest_fingerprint="sha256:" + "a" * 64,
        override_version=None,
        system_required=True,
    )
    return DocumentRuntimeAdmission("sha256:" + "b" * 64, 1, binding, True)


async def test_exact_admission_atomic_and_no_publication_from_receipt(ctx):
    admission = await runtime(ctx)
    receipt = await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=ctx.proposal, runtime=admission)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute(
            """SELECT c.status,c.output_contract,c.input_artifact_refs_json,a.status
            FROM document_dispatches d JOIN turn_capability_execution c ON c.id=d.capability_execution_id
            JOIN turn_agent_execution a ON a.id=c.agent_execution_id WHERE d.job_id=?""",
            (receipt.job_id,),
        ) as cursor:
            state, contract, inputs, parent = await cursor.fetchone()
        assert (state, contract, parent) == ("completed", "document.admission.v1", "completed")
        assert json.loads(inputs) == [ctx.artifact.id]
        async with db.execute("SELECT count(*) FROM document_executions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=ctx.proposal, runtime=admission) == receipt


async def test_exact_admission_rolls_back_with_document_receipt(ctx):
    admission = await runtime(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("CREATE TRIGGER fail_dispatch BEFORE INSERT ON document_dispatches BEGIN SELECT RAISE(ABORT,'test failure'); END")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="test failure"):
        await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=ctx.proposal, runtime=admission)
    async with open_shared_ctx(ctx.path) as db:
        for table in ("document_jobs", "document_command_receipts", "turn_capability_execution"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
        async with db.execute("SELECT status FROM turn_agent_execution") as cursor:
            assert (await cursor.fetchone())[0] == "admitted"


async def test_canonical_request_required_for_native_admission(ctx):
    admission = await runtime(ctx)
    proposal = ctx.proposal.model_copy(update={"evidence": ctx.proposal.evidence.model_copy(update={"text": "Invented request"})})
    with pytest.raises(DocumentRequestError):
        await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=proposal, runtime=admission)
