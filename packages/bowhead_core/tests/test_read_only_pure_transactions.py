"""Pure-read transactions take no cross-file authority lease, so they no longer fail beside an authority writer.

A deferred ``BEGIN`` on a write-capable connection pins the shared lease of every attached domain it
reads, and inside a transaction a refused lease fails at once ("SQLite authority lease is busy;
retry the complete transaction"). These reads write nothing, so they run on the read-only pool,
which takes no lease (docs/sprints/read-only-pure-transactions-2026-11-18/).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.coach_plugin_authoring import CoachPluginAuthoring
from bowhead_core.coach_skill_authoring import CoachSkillAuthoring
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents import authoring_runner
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domains import domain_database_path
from test_document_store import SCOPE as DOCUMENT_SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_sqlite_domain_stores import domain_directory as domain_directory

pytestmark = pytest.mark.integration

SCOPE = ReferenceRequestScope(team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id="s_1")


@asynccontextmanager
async def execution_authority_writer(core: Path, session_id: str) -> AsyncIterator[None]:
    """Hold the execution file's exclusive authority lease the way a session deletion does."""
    async with open_shared_ctx(domain_database_path(core, "execution")) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            await writer.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            yield
        finally:
            await writer.rollback()


async def held_session(core: Path, session_id: str) -> None:
    await SessionStore(core).ensure_session(session_id=session_id, workspace_id="ws_1", user_id="u_1")


async def scope_authorize(db, *_args) -> bool:
    # The coach callbacks read the request scope across files: sessions live in the execution file.
    await require_reference_request_scope(db, SCOPE)
    return True


async def ready(candidate, observed_tools) -> dict:
    return {"status": "ready", "fingerprint": "sha256:" + "0" * 64}


BODY = "---\nname: owned-method\ndescription: Summarize supplied evidence\nversion: 1.0.0\n---\nRead the supplied evidence.\n"


async def test_coach_skill_reads_proceed_beside_an_execution_authority_writer(domain_directory: Path, tmp_path: Path) -> None:
    core = domain_directory / "bowhead.db"
    await held_session(core, "s_skill_hold")
    service = CoachSkillAuthoring(str(core), tmp_path / "skills", SCOPE, scope_authorize)
    empty = (await service.inspect(name="owned-method"))["target"]
    changes = [{"path": "SKILL.md", "content": BODY}]
    saved = await service.save(target=empty, changes=changes, request_id="create", turn_id="t_coach")
    assert saved["ok"], saved
    target = saved["result"]["target"]
    async with execution_authority_writer(core, "s_skill_hold"):
        assert (await asyncio.wait_for(service.inspect(name="owned-method"), timeout=5))["target"] == target
        assert (await asyncio.wait_for(service.read(target=target, file="SKILL.md"), timeout=5))["content"] == BODY
        checked = await asyncio.wait_for(service.validate(target=target, changes=changes), timeout=5)
        assert checked["structural_validation"] == "passed"
        receipt = await asyncio.wait_for(service.get(operation_id=saved["operation_id"]), timeout=5)
        assert receipt["state"] == "succeeded"


async def test_coach_plugin_reads_proceed_beside_an_execution_authority_writer(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    await held_session(core, "s_plugin_hold")
    service = CoachPluginAuthoring(str(core), SCOPE, scope_authorize, ready)
    async with execution_authority_writer(core, "s_plugin_hold"):
        with pytest.raises(ValueError, match="plugin_operation_unavailable"):
            await asyncio.wait_for(service.get(operation_id="cpw_missing"), timeout=5)
        with pytest.raises(ValueError, match="plugin_unavailable"):
            await asyncio.wait_for(service.inspect(plugin_id="tcap_missing"), timeout=5)


@asynccontextmanager
async def core_authority_writer(core: Path) -> AsyncIterator[None]:
    """Hold core's exclusive authority lease the way a membership or Space save does."""
    async with open_shared_ctx(str(core)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            await writer.execute(
                "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (DOCUMENT_SCOPE.workspace_id, DOCUMENT_SCOPE.user_id)
            )
            yield
        finally:
            await writer.rollback()


async def test_authoring_runner_checks_proceed_beside_a_core_authority_writer(ctx) -> None:
    receipt = await ctx.submit()
    await DocumentInputStore(ctx.artifacts).freeze(scope=DOCUMENT_SCOPE, job_id=receipt.job_id)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    owner = await delivery.claim(scope=DOCUMENT_SCOPE, job_id=receipt.job_id)
    await delivery.transition(scope=DOCUMENT_SCOPE, job_id=receipt.job_id, owner=owner, state="planning", image_id="sha256:test")
    worker = SimpleNamespace(owner_id=owner)
    async with core_authority_writer(ctx.path):
        # The fence's scope check reads core; it now passes on the read snapshot and the
        # checks report the job's own state instead of a lost lease.
        with pytest.raises(DocumentRequestError, match="document_usage_unresolved"):
            await asyncio.wait_for(authoring_runner._observed(delivery, DOCUMENT_SCOPE, worker, receipt.job_id, 1, 1), timeout=5)
        # This job has no running Turn lease, so the recheck fences it; it no longer loses the lease first.
        turn_worker = DocumentTurnOwner(
            team_id=DOCUMENT_SCOPE.team_id,
            user_id=DOCUMENT_SCOPE.user_id,
            workspace_id=DOCUMENT_SCOPE.workspace_id,
            session_id="s_doc",
            turn_id="t_doc",
            generation=1,
            owner_id=owner,
        )
        with pytest.raises(DocumentRequestError, match="document_execution_fenced"):
            await asyncio.wait_for(authoring_runner._recheck_worker(delivery, DOCUMENT_SCOPE, receipt.job_id, turn_worker), timeout=5)
