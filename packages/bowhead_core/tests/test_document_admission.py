"""Native admission repairs, server-owned references and durable receipt reads."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import anyio
import httpx2
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.documents.executor import PROFILE
from bowhead_core.documents.request import DocumentRequestError, UserQuote
from bowhead_core.documents.tool import register_document_tool
from bowhead_core.documents.turn_policy import DocumentTurnState
from bowhead_core.team_llm_config import REQUIRED_AGENT_TYPES
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.user_store import UserStore
from bowhead_gateway.document_service import DocumentService
from bowhead_router.registry import ProviderRegistry
from bowhead_router.router import Router
from cryptography.fernet import Fernet
from test_document_capability import runtime
from test_document_recovery import question, turn
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

from bowhead_router import AgentBinding

pytestmark = pytest.mark.integration


@pytest.fixture
async def admission(ctx, monkeypatch):
    # The readiness fixture below is the pre-continuation executor; same-Turn continuation has its own suites.
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    users = UserStore(ctx.path)
    await users.update_team_llm_config(
        SCOPE.team_id,
        {
            "schema_version": 2,
            "providers": {
                "team": {"type": "openai_compatible", "base_url": "https://provider.example/v1", "api_key": "test-only", "models": ["planner"]}
            },
            "agents": {
                role: (
                    {"default": {"primary": {"provider": "team", "model": "planner"}}}
                    if role == "analyst"
                    else {"primary": {"provider": "team", "model": "planner"}}
                )
                for role in REQUIRED_AGENT_TYPES
            },
        },
    )
    router = Router(ProviderRegistry())
    router.bind(AgentBinding(agent_type="lead", primary=("team", "planner")))
    service = DocumentService(SimpleNamespace(artifact_store=ctx.artifacts, user_store=users))
    service.authorize_model = AsyncMock(return_value=router)
    service.executor = lambda: SimpleNamespace(url="http://executor", token="test-only")
    context = ToolExecutionContext(
        team_id=SCOPE.team_id,
        workspace_id=SCOPE.workspace_id,
        user_id=SCOPE.user_id,
        turn_id="t_doc",
        session_id="s_doc",
        role="lead",
        member_uid="",
        datarc=None,
    )

    async def binding():
        value = await runtime(ctx, turn_id=context.turn_id)
        return value.binding, True

    context.document_admission_binding = AsyncMock(side_effect=binding)
    requests = []

    def readiness(request):
        assert request.url.path == "/document-runs/capabilities"
        requests.append(request)
        return httpx2.Response(200, json={"profile": PROFILE, "image_id": "sha256:" + "b" * 64})

    client = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: client(transport=httpx2.MockTransport(readiness), **kw))
    registry = ToolRegistry()
    register_document_tool(registry, service=lambda: service)
    arguments = ctx.proposal.model_dump(mode="json")
    for item in arguments["inputs"]:
        item.pop("current_message_position")
    return SimpleNamespace(service=service, context=context, registry=registry, arguments=arguments, requests=requests)


async def invoke(admission, arguments):
    return (await admission.registry.execute("template_document_submit", arguments, admission.context)).result


async def counts(ctx):
    async with open_shared_ctx(ctx.path) as db:
        result = {}
        for table in ("document_jobs", "document_dispatches", "turn_agent_execution", "document_command_receipts"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cur:
                result[table] = (await cur.fetchone())[0]
        return result


@pytest.mark.parametrize("kind", ["generate", "answer"])
@pytest.mark.parametrize("bad_field", ["request", "role"])
async def test_exact_quote_repair_creates_one_dispatch_without_failed_binding(ctx, admission, kind, bad_field):
    if kind == "answer":
        parent = await question(ctx)
        admission.context.turn_id = await turn(ctx, "answer", text="Use last year.", session_id="s_doc", attach=False)
        admission.arguments = {"action": "answer", "job_id": parent, "evidence": {"text": "Use last year."}}
    bad = copy.deepcopy(admission.arguments)
    target = bad["inputs"][0] if kind == "generate" and bad_field == "role" else bad
    target["evidence"] = {"text": "Invented quotation absent from this request"}
    first = await invoke(admission, bad)
    assert first["error"] == "document_evidence_mismatch" and not first["document_terminal"]
    assert first["field_errors"] == [
        {"field": "inputs.0.evidence" if kind == "generate" and bad_field == "role" else "evidence", "issue": "exact_current_quote_required"}
    ]
    assert target["evidence"]["text"] not in str(first)
    admission.context.document_admission_binding.assert_not_awaited()
    admission.service.authorize_model.assert_not_awaited()
    assert not admission.requests
    result = await invoke(admission, admission.arguments)
    assert result["ok"] and result["state"] == "accepted"
    assert (await counts(ctx))["document_dispatches"] == 1
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status,failure_code FROM turn_agent_execution") as cur:
        assert [tuple(row) for row in await cur.fetchall()] == [("completed", None)]
    job = await ctx.store.get(scope=SCOPE, job_id=result["document_job_id"])
    assert job.proposal.inputs[0].current_message_position == (1 if kind == "generate" else None)
    assert await invoke(admission, admission.arguments) == result


@pytest.mark.parametrize("boundary", ["exhausted", "cancelled", "revoked"])
async def test_failed_correction_does_not_allocate_execution(ctx, admission, boundary):
    bad = {**admission.arguments, "evidence": {"text": "Absent quotation"}}
    assert not (await invoke(admission, bad))["document_terminal"]
    if boundary == "cancelled":
        await ctx.sessions.mark_cancelled(turn_id="t_doc")
    elif boundary == "revoked":
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            await db.commit()
    result = await invoke(admission, bad if boundary == "exhausted" else admission.arguments)
    assert not result["ok"] and result["document_terminal"]
    assert set((await counts(ctx)).values()) == {0}
    assert not admission.requests


@pytest.mark.parametrize("kind", ["generate", "answer"])
async def test_lost_live_receipt_recovers_after_turn_end_without_readiness_or_binding(ctx, admission, monkeypatch, kind):
    if kind == "answer":
        parent = await question(ctx)
        admission.context.turn_id = await turn(ctx, "answer", text="Use last year.", session_id="s_doc", attach=False)
        admission.arguments = {"action": "answer", "job_id": parent, "evidence": {"text": "Use last year."}}
    result = await invoke(admission, admission.arguments)
    assert result["ok"]
    before = await counts(ctx)
    await ctx.sessions.mark_cancelled(turn_id=admission.context.turn_id)
    admission.service.authorize_model = AsyncMock(side_effect=AssertionError("Recovery must not authorize a new model call"))
    admission.context.document_admission_binding = AsyncMock(side_effect=AssertionError("Recovery must not reopen an execution"))
    admission.context.document_turn_state = DocumentTurnState()
    assert await invoke(admission, admission.arguments) == result
    assert await counts(ctx) == before
    assert len(admission.requests) == 1


@pytest.mark.parametrize("boundary", ["cancelled", "revoked", "input_detached", "borrowed"])
async def test_commit_revalidates_after_preflight_and_preserves_borrowed_binding(ctx, admission, boundary):
    async def binding():
        value = await runtime(ctx)
        if boundary == "cancelled":
            await ctx.sessions.mark_cancelled(turn_id="t_doc")
        else:
            async with open_shared_ctx(ctx.path) as db:
                if boundary == "revoked":
                    await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
                else:
                    await db.execute("DELETE FROM turn_attachments WHERE turn_id='t_doc'")
                await db.commit()
        return value.binding, boundary != "borrowed"

    admission.context.document_admission_binding = binding
    result = await invoke(admission, admission.arguments)
    assert not result["ok"] and result["document_terminal"]
    assert (await counts(ctx))["document_jobs"] == 0
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status,failure_code FROM turn_agent_execution") as cur:
        assert tuple(await cur.fetchone()) == (("admitted", None) if boundary == "borrowed" else ("failed", "document_admission_failed"))


@pytest.mark.parametrize("change", ["outputs", "scope", "deleted_session"])
async def test_recovery_cannot_replace_request_or_bypass_current_access(ctx, admission, change):
    assert (await invoke(admission, admission.arguments))["ok"]
    proposal = ctx.proposal
    if change == "outputs":
        proposal = proposal.model_copy(update={"requested_outputs": ("source", "pdf")})
        if proposal == ctx.proposal:
            proposal = proposal.model_copy(update={"requested_outputs": ("source",)})
    else:
        async with open_shared_ctx(ctx.path) as db:
            if change == "scope":
                await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            else:
                await db.execute("UPDATE sessions SET deleted_at='2026-10-03T00:00:00Z' WHERE id='s_doc'")
            await db.commit()
    with pytest.raises(DocumentRequestError):
        await admission.service.submit(admission.context, proposal)
    assert (await counts(ctx))["document_dispatches"] == 1


async def test_concurrent_service_admission_returns_one_durable_receipt(ctx, admission):
    first, second = await asyncio.gather(
        admission.service.submit(admission.context, ctx.proposal), admission.service.submit(admission.context, ctx.proposal)
    )
    assert first == second
    assert set((await counts(ctx)).values()) == {1}


@pytest.mark.parametrize("kind", ["generate", "answer"])
@pytest.mark.parametrize("change", ["model", "image", "request"])
async def test_concurrent_admission_recovers_original_runtime_but_not_changed_request(ctx, admission, monkeypatch, kind, change):
    parent = None
    proposal = ctx.proposal
    if kind == "answer":
        parent = await question(ctx)
        admission.context.turn_id = await turn(ctx, "answer", text="Use last year.", session_id="s_doc", attach=False)
        proposal = UserQuote(text="Use last year.")
    other = proposal
    if change == "request":
        other = proposal.model_copy(update={"start": 0}) if kind == "answer" else proposal.model_copy(update={"requested_outputs": ("source",)})
        assert other != proposal
    config, _ = await admission.service.state.user_store.get_team_llm_config_with_version(SCOPE.team_id)
    versions = iter([1, 2 if change == "model" else 1])
    task_versions = {}

    async def configuration(_team_id):
        task = asyncio.current_task()
        if task not in task_versions:
            task_versions[task] = next(versions)
        return config, task_versions[task]

    admission.service.state.user_store.get_team_llm_config_with_version = configuration
    if change == "image":
        client = httpx2.AsyncClient
        images = iter(["a", "b"])

        def readiness(**kwargs):
            instance = client(**kwargs)
            instance.get = AsyncMock(
                return_value=httpx2.Response(
                    200, json={"profile": PROFILE, "image_id": "sha256:" + next(images) * 64}, request=httpx2.Request("GET", "http://executor")
                )
            )
            return instance

        monkeypatch.setattr(httpx2, "AsyncClient", readiness)
    arrived = 0
    barrier = asyncio.Event()

    async def binding():
        nonlocal arrived
        value = await runtime(ctx, turn_id=admission.context.turn_id)
        arrived += 1
        if arrived == 2:
            barrier.set()
        await barrier.wait()
        return value.binding, True

    admission.context.document_admission_binding = binding
    results = await asyncio.wait_for(
        asyncio.gather(
            admission.service.submit(admission.context, proposal, answer_to_job_id=parent),
            admission.service.submit(admission.context, other, answer_to_job_id=parent),
            return_exceptions=True,
        ),
        timeout=10,
    )
    if change == "request":
        errors = [result for result in results if isinstance(result, DocumentRequestError)]
        assert len(errors) == 1 and errors[0].code == "document_command_conflict"
    else:
        assert not any(isinstance(result, BaseException) for result in results)
        assert results[0] == results[1]
    assert (await counts(ctx))["document_dispatches"] == 1
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status,failure_code FROM turn_agent_execution") as cur:
        assert [tuple(row) for row in await cur.fetchall()] == [("completed", None)]


@pytest.mark.parametrize(
    "language,expected", [("en-US", "not replaced"), ("zh-CN", "未替换"), ("de-DE", "nicht ersetzt"), ("fr-FR", "pas été remplacée")]
)
async def test_conflicting_tool_replay_preserves_original_job_and_reports_conflict(ctx, admission, language, expected):
    original = await invoke(admission, admission.arguments)
    admission.context.document_turn_state = DocumentTurnState()
    admission.context.language_context = SimpleNamespace(output_language=language)
    result = await invoke(admission, {**admission.arguments, "requested_outputs": ["source"]})
    assert not result["ok"] and result["document_terminal"] and result["error"] == "document_command_conflict"
    assert expected in result["message"]
    assert "The existing request was not replaced" in result["hint"]
    assert (await ctx.store.get(scope=SCOPE, job_id=original["document_job_id"])).status == "queued"
    assert set((await counts(ctx)).values()) == {1}


@pytest.mark.parametrize("cancellation", ["task", "scope"])
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_binding_commit_cancellation_does_not_leave_admission_orphan(ctx, admission, monkeypatch, cancellation, boundary):
    commit = aiosqlite.Connection.commit
    injected = False

    async def interrupt_commit(db):
        nonlocal injected
        async with db.execute("SELECT 1 FROM turn_agent_execution WHERE dispatch_key='document-admission:t_doc' AND status='admitted'") as cur:
            target = await cur.fetchone() is not None and not injected
        if boundary == "after_commit" or not target:
            await commit(db)
        if target:
            injected = True
            if cancellation == "task":
                asyncio.current_task().cancel()
            else:
                cancel_scope.cancel()
            await anyio.sleep(0)

    monkeypatch.setattr(aiosqlite.Connection, "commit", interrupt_commit)

    async def submit():
        nonlocal cancel_scope
        with anyio.CancelScope() as cancel_scope:
            with pytest.raises(asyncio.CancelledError):
                await admission.service.submit(admission.context, ctx.proposal)

    cancel_scope = None
    await asyncio.wait_for(asyncio.create_task(submit()), timeout=10)
    assert injected
    outcome = await ctx.sessions.request_cancel_turn(
        team_id=SCOPE.team_id,
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        turn_id="t_doc",
        cancel_request_id="test-document-stop",
        expected_execution_generation=1,
        include_plan_outcome=True,
    )
    assert outcome == ("cancelled", False)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status FROM turn_agent_execution") as cur:
        assert [row[0] for row in await cur.fetchall()] == (["cancelled"] if boundary == "after_commit" else [])
    assert (await counts(ctx))["document_jobs"] == 0


@pytest.mark.parametrize("scope_field,value", [("team_id", "team_other"), ("workspace_id", "ws_other"), ("user_id", "u_other")])
async def test_admission_cleanup_requires_exact_owner_scope(ctx, scope_field, value):
    await runtime(ctx)
    scope = SCOPE.model_copy(update={scope_field: value})
    await ctx.store.close_uncommitted_admission(scope=scope, turn_id="t_doc", cancelled=True)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status FROM turn_agent_execution") as cur:
        assert (await cur.fetchone())[0] == "admitted"


@pytest.mark.parametrize("borrowed", [False, True])
async def test_interrupted_binding_reply_cleans_only_reserved_document_execution(ctx, admission, borrowed):
    value = await runtime(ctx)
    ordinary = await AgentExecutionBindingStore(ctx.path).admit_system_execution(
        team_id=SCOPE.team_id,
        user_id=SCOPE.user_id,
        turn_id="t_doc",
        profile_name="lead",
        dispatch_key="ordinary-lead",
        definition=value.binding.definition,
        manifest_fingerprint="sha256:" + "a" * 64,
        override_version=None,
        system_required=True,
    )

    async def binding():
        if borrowed:
            return ordinary, False
        raise OSError("Binding reply lost after commit")

    admission.context.document_admission_binding = binding
    if borrowed:
        admission.service.jobs.submit_from_turn = AsyncMock(side_effect=OSError("Admission interrupted"))
    with pytest.raises(OSError):
        await admission.service.submit(admission.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT dispatch_key,status FROM turn_agent_execution") as cur:
        assert dict(await cur.fetchall()) == {
            "ordinary-lead": "admitted",
            "document-admission:t_doc": "admitted" if borrowed else "failed",
        }
    assert (await counts(ctx))["document_jobs"] == 0


async def test_real_job_commit_cancellation_preserves_accepted_request(ctx, admission, monkeypatch):
    commit = aiosqlite.Connection.commit
    injected = False

    async def interrupt_commit(db):
        nonlocal injected
        await commit(db)
        async with db.execute("SELECT 1 FROM document_dispatches") as cur:
            target = await cur.fetchone() is not None and not injected
        if target:
            injected = True
            asyncio.current_task().cancel()
            await anyio.sleep(0)

    monkeypatch.setattr(aiosqlite.Connection, "commit", interrupt_commit)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(admission.service.submit(admission.context, ctx.proposal))
    recovered = await admission.service.submit(admission.context, ctx.proposal)
    assert injected and (await ctx.store.get(scope=SCOPE, job_id=recovered.job_id)).status == "queued"
    assert set((await counts(ctx)).values()) == {1}
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status FROM turn_agent_execution") as cur:
        assert (await cur.fetchone())[0] == "completed"
    admission.context.document_admission_binding.assert_awaited_once()


async def test_readiness_failure_is_known_refusal_not_a_missing_draft(ctx, admission, monkeypatch):
    client = httpx2.AsyncClient

    def unavailable(**kwargs):
        instance = client(**kwargs)
        instance.get = AsyncMock(side_effect=httpx2.ConnectError("Executor unavailable"))
        return instance

    monkeypatch.setattr(httpx2, "AsyncClient", unavailable)
    result = await invoke(admission, admission.arguments)
    assert result["error"] == "document_runtime_unavailable"
    assert "no draft was created" in result["message"]
    assert set((await counts(ctx)).values()) == {0}


async def test_answer_receipt_rejects_a_different_question(ctx, admission):
    parent = await question(ctx)
    admission.context.turn_id = await turn(ctx, "answer", text="Use last year.", session_id="s_doc", attach=False)
    await admission.service.submit(admission.context, UserQuote(text="Use last year."), answer_to_job_id=parent)
    with pytest.raises(DocumentRequestError, match="document_command_conflict"):
        await admission.service.submit(admission.context, UserQuote(text="Use last year."), answer_to_job_id="djob_other")


async def test_cancelled_binding_without_a_job_is_a_known_refusal(ctx, admission):
    value = await runtime(ctx)
    await AgentExecutionBindingStore(ctx.path).mark_terminal(value.binding.execution_id, status="cancelled")
    result = await invoke(admission, admission.arguments)
    assert result["error"] == "document_capability_unavailable"
    assert "no draft was created" in result["message"]
    assert (await counts(ctx))["document_jobs"] == 0
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status FROM turn_agent_execution") as cur:
        assert (await cur.fetchone())[0] == "cancelled"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_lost_response_after_commit_preserves_receipt_and_cancellation(ctx, admission, monkeypatch, cancelled):
    commit = admission.service.jobs.submit_from_turn

    async def lose_response(**kwargs):
        await commit(**kwargs)
        if cancelled:
            raise asyncio.CancelledError()
        raise OSError("Response lost after durable commit")

    monkeypatch.setattr(admission.service.jobs, "submit_from_turn", lose_response)
    with pytest.raises(asyncio.CancelledError if cancelled else OSError):
        await admission.service.submit(admission.context, ctx.proposal)
    recovered = await admission.service.submit(admission.context, ctx.proposal)
    assert (await ctx.store.get(scope=SCOPE, job_id=recovered.job_id)) is not None
    assert set((await counts(ctx)).values()) == {1}
    assert len(admission.requests) == 1
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status FROM turn_agent_execution") as cur:
        assert (await cur.fetchone())[0] == "completed"


@pytest.mark.parametrize("file_state", ["available", "detached", "foreign_owner", "unknown"])
async def test_historical_pin_uses_current_access_not_model_position(ctx, admission, file_state):
    await ctx.sessions.mark_cancelled(turn_id="t_doc")
    await ctx.sessions.finalize_turn(session_id="s_doc", current_turn_id="t_doc", next_turn_id=None)
    admission.context.turn_id = await turn(ctx, "pin", session_id="s_doc", attach=False)
    async with open_shared_ctx(ctx.path) as db:
        if file_state == "detached":
            await db.execute("UPDATE session_attachments SET detached_at='2026-10-03T00:00:00Z' WHERE artifact_id=?", (ctx.artifact.id,))
        elif file_state == "foreign_owner":
            await db.execute("UPDATE artifacts SET created_by='u_other' WHERE id=?", (ctx.artifact.id,))
        await db.commit()
    if file_state == "unknown":
        admission.arguments["inputs"][0]["artifact_id"] = "art_absent"
    result = await invoke(admission, admission.arguments)
    if file_state == "available":
        assert result["ok"]
        job = await ctx.store.get(scope=SCOPE, job_id=result["document_job_id"])
        assert job.proposal.inputs[0].current_message_position is None
    else:
        assert result["error"] == "document_input_unavailable"
        assert set((await counts(ctx)).values()) == {0}
        assert not admission.requests
