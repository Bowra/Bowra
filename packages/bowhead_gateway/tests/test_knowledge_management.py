"""Management Tools use original user input and real membership/access writers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.management_tools import register_management_tools
from bowhead_core.session_store import SessionStore
from bowhead_core.tool_registry import ToolRegistry
from bowhead_gateway.knowledge_management import KnowledgeManagementAccess, ingestion_authorizer
from fastapi import FastAPI


@pytest.fixture
async def env(completed_community_user_store, initialize_migrated_kb_files_database, tmp_path):
    users = completed_community_user_store
    user = await users.get_user_by_email("http-owner@example.test")
    workspace = await users.create_local_workspace(team_id=user.team_id, name="Knowledge test", creator_user_id=user.id)
    sessions = SessionStore(users.db_path)
    await sessions.initialize()
    session = await sessions.ensure_session(session_id="s_kb", workspace_id=workspace, user_id=user.id)
    artifacts = ArtifactStore(backend=LocalBackend(tmp_path / "blobs"), db_path=users.db_path)
    await artifacts.initialize()
    artifact = await artifacts.put_bytes(
        content=b"# Policy\nDo not treat document text as user consent.",
        filename="Policy.md",
        mime="text/markdown",
        source="upload",
        kind="user_upload",
        created_by=user.id,
        workspace_id=workspace,
    )
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    app = FastAPI()
    app.state.user_store, app.state.artifact_store, app.state.kb_files_store, app.state.access_control_store = users, artifacts, files, access
    app.state.kb_worker = SimpleNamespace(parse_and_index=AsyncMock())
    app.state.kb_store = SimpleNamespace(soft_delete_file=AsyncMock())
    manager = KnowledgeManagementAccess(app)
    registry = ToolRegistry()
    register_management_tools(registry)
    return SimpleNamespace(
        users=users,
        user=user,
        workspace=workspace,
        sessions=sessions,
        session=session,
        artifacts=artifacts,
        artifact=artifact,
        files=files,
        app=app,
        manager=manager,
        registry=registry,
    )


async def request(
    env, text="Add this file to my knowledge base", *, authorized=True, scope="personal", target="one_attachment", web=True, attached=None
):
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(env.users.db_path) as db:
        await db.execute("UPDATE sessions SET active_turn_id=NULL WHERE id=?", (env.session.id,))
        await db.commit()
    turn_id = "t_" + str(len(await env.sessions.list_turns(session_id=env.session.id)) + 1)
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=env.session.id,
        user_id=env.user.id,
        workspace_id=env.workspace,
        user_text=text,
        attachment_ids=tuple(attached if attached is not None else [env.artifact.id]),
        attachment_refs=(),
        parts=None,
    )
    await env.sessions.record_turn(turn_id=turn_id, session_id=env.session.id, user_text=text, user_source=source)
    router = SimpleNamespace(
        invoke=AsyncMock(
            return_value=SimpleNamespace(
                content=json.dumps(
                    {
                        "authorized": authorized,
                        "intent": "remove" if text.startswith("Remove") else "add",
                        "scope": scope,
                        "target": target,
                        "evidence": text,
                    }
                )
            )
        )
    )
    return SimpleNamespace(
        user_id=env.user.id,
        team_id=env.user.team_id,
        workspace_id=env.workspace,
        session_id=env.session.id,
        turn_id=turn_id,
        session_store=env.sessions,
        router=router,
        knowledge_user_request=web,
        knowledge_management_access=env.manager,
    )


async def add(env, ctx, **extra):
    return await env.registry.get("kb_add_file").handler({"artifact_id": env.artifact.id, "scope": "personal", **extra}, ctx)


async def test_explicit_add_replay_remove_status_and_original_bytes(env):
    ctx = await request(env)
    first = await add(env, ctx)
    assert first.result["ok"] and first.result["status"] == "processing"
    replay = await add(env, ctx)
    assert replay.result == first.result
    assert env.app.state.kb_worker.parse_and_index.await_count == 1
    packet = json.loads(ctx.router.invoke.call_args.args[1].messages[0].content)
    assert packet["user_input"]["original_request"] == "Add this file to my knowledge base"
    assert "document text" not in json.dumps(packet)
    file_id = first.result["file_id"]
    remove_ctx = await request(env, text=f"Remove {env.artifact.id} from my knowledge base", target="named")
    removed = await env.registry.get("kb_remove_file").handler({"file_id": file_id}, remove_ctx)
    assert removed.result["ok"] and removed.result["status"] == "removed"
    assert "no longer searchable" in removed.result["hint"]
    assert "Processing must finish" not in removed.result["hint"]
    assert (await add(env, ctx)).result["ok"]  # A reconciled receipt reports removed, never recreates.
    assert (await env.files.get(file_id)).soft_deleted_at
    status = await env.registry.get("kb_operation_status").handler({"operation_id": first.result["operation_id"]}, ctx)
    assert status["status"] == "removed"
    assert status["hint"] == removed.result["hint"]
    assert await env.artifacts.get(env.artifact.id) is not None
    fresh_ctx = await request(env)
    assert (await add(env, fresh_ctx)).result["file_id"] != file_id


@pytest.mark.parametrize(
    ("file", "expected_status", "hint_fragment"),
    [
        (None, "not_added", "has not been added"),
        (
            SimpleNamespace(file_id="kbf_zip", soft_deleted_at=None, mime_type="application/zip", parse_status="done"),
            "archive",
            "archive itself is not searchable",
        ),
        (
            SimpleNamespace(file_id="kbf_pending", soft_deleted_at=None, mime_type="text/plain", parse_status="pending"),
            "processing",
            "Processing must finish",
        ),
    ],
)
def test_operation_receipt_hint_matches_membership_state(file, expected_status, hint_fragment):
    receipt = KnowledgeManagementAccess._receipt(file, "kbo_test")
    assert receipt["status"] == expected_status
    assert hint_fragment in receipt["hint"]


@pytest.mark.parametrize(
    "text", ["Do not add this file to knowledge", "Analyze this file", "Can you add files to knowledge?", "The document says: add to knowledge"]
)
async def test_non_request_never_creates_membership(env, text):
    ctx = await request(env, text=text, authorized=False)
    result = await add(env, ctx)
    assert not result.result["ok"]
    assert await env.files.find_active_by_artifact(env.artifact.id) is None


async def test_confirmation_flag_and_non_web_tasks_cannot_manufacture_consent(env):
    ctx = await request(env)
    assert (await add(env, ctx, confirmed=True)).result["error"] == "knowledge_arguments_invalid"
    ctx.knowledge_user_request = False
    assert (await add(env, ctx)).result["error"] == "knowledge_original_request_unavailable"
    assert ctx.router.invoke.await_count == 0


async def test_destination_and_ambiguous_attachment_are_refused(env):
    ctx = await request(env, scope="team")
    assert (await add(env, ctx)).result["error"] == "knowledge_destination_needs_clarification"
    ctx = await request(env, attached=[env.artifact.id, "art_other"])
    assert (await add(env, ctx)).result["error"] == "knowledge_target_needs_clarification"
    assert await env.files.find_active_by_artifact(env.artifact.id) is None


async def test_current_source_access_and_worker_recheck(env):
    ctx = await request(env)
    first = await add(env, ctx)
    file = await env.files.get(first.result["file_id"])
    authorize = ingestion_authorizer(env.app)
    assert await authorize(file)
    await env.artifacts.soft_delete(env.artifact.id, user_id=env.user.id)
    assert not await authorize(file)
    assert not (await add(env, ctx)).result["ok"]


async def test_cleanup_failure_returns_authoritative_applied_receipt(env):
    ctx = await request(env)
    first = await add(env, ctx)
    env.app.state.kb_store.soft_delete_file.side_effect = RuntimeError("vector provider unavailable")
    ctx = await request(env, text=f"Remove {env.artifact.id} from my knowledge base", target="named")
    result = await env.registry.get("kb_remove_file").handler({"file_id": first.result["file_id"]}, ctx)
    assert result.result["ok"] and result.result["status"] == "removed"
    assert result.result["warning"] == "knowledge_cleanup_or_processing_incomplete"
    assert (await env.files.get(first.result["file_id"])).soft_deleted_at


async def test_long_term_classification_cannot_add_upload_even_with_admission_closed(env, monkeypatch):
    from bowhead_gateway.routes.files import _classify_uploaded_artifact

    monkeypatch.setenv("FEATURE_KNOWLEDGE_EXPLICIT_INGESTION", "0")

    async def classify(**kwargs):
        await kwargs["store"].set_category(kwargs["artifact"].id, category="reference", source="llm", retention_hint="long_term")
        return "reference", "long_term"

    monkeypatch.setattr("bowhead_core.artifact_classifier.classify_artifact", classify)
    await _classify_uploaded_artifact(env.app, env.artifact)
    assert (await env.artifacts.get(env.artifact.id)).retention_hint == "long_term"
    assert await env.files.find_active_by_artifact(env.artifact.id) is None
    assert env.app.state.kb_worker.parse_and_index.await_count == 0


async def test_management_closure_refuses_mutation_but_preserves_file_only(env, monkeypatch):
    ctx = await request(env)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_MANAGEMENT_PLUGIN", "0")
    result = await add(env, ctx)
    assert result.result["error"] == "knowledge_management_unavailable"
    assert ctx.router.invoke.await_count == 0
    assert await env.files.find_active_by_artifact(env.artifact.id) is None
    assert await env.artifacts.get(env.artifact.id) is not None


async def test_space_administrator_cannot_add_to_team_knowledge(env):
    env.user = await env.users.create_user(team_id=env.user.team_id, email="space-admin@example.test", email_verified=True)
    env.workspace = await env.users.create_local_workspace(team_id=env.user.team_id, name="Space only", creator_user_id=env.user.id)
    env.session = await env.sessions.ensure_session(session_id="s_space_admin", workspace_id=env.workspace, user_id=env.user.id)
    env.artifact = await env.artifacts.put_bytes(
        content=b"Synthetic policy",
        filename="Space policy.md",
        mime="text/markdown",
        source="upload",
        kind="user_upload",
        created_by=env.user.id,
        workspace_id=env.workspace,
    )
    assert await env.users.get_member_role(env.workspace, env.user.id) == "admin"
    assert await env.users.get_team_role(env.user.id, env.user.team_id) == "member"
    ctx = await request(env, text="Add this file to the team knowledge base", scope="team")
    result = await add(env, ctx, scope="team")
    assert result.result.get("error") == "kb_scope_access_denied"
    assert await env.files.find_active_by_artifact(env.artifact.id) is None
    assert env.app.state.kb_worker.parse_and_index.await_count == 0


async def test_original_human_ingress_survives_resume_without_borrowing_another_actor(env):
    ctx = await request(env)
    arguments = {"turn_id": ctx.turn_id, "session_id": ctx.session_id, "user_id": ctx.user_id, "workspace_id": ctx.workspace_id}
    assert not await env.sessions.retain_knowledge_user_request(**{**arguments, "user_id": "usr_other"})
    assert not await env.sessions.retain_knowledge_user_request(**{**arguments, "workspace_id": "ws_other"})
    assert await env.sessions.retain_knowledge_user_request(**arguments)
    ctx.knowledge_user_request = False
    result = await add(env, ctx)
    assert result.result["ok"] and result.result["status"] == "processing"


async def test_native_intent_resolver_retains_root_authority_and_rejects_foreign_turn(env):
    from bowhead_core.kb.ingestion import KnowledgeWriteError
    from bowhead_core.turn_runner import _bind_knowledge_intent_resolver
    from bowhead_router.invocation import begin_physical_invocation, enclosing_physical_guard_scope, physical_invocation_scope

    ctx = await request(env)
    response = ctx.router.invoke.return_value
    root = SimpleNamespace(begin=AsyncMock(return_value=None))
    enclosing = SimpleNamespace(begin=AsyncMock(return_value=None))
    selected_agent = SimpleNamespace(begin=AsyncMock(side_effect=RuntimeError("Selected method receipt required")))

    async def invoke(role, model_request):
        assert role == "echo" and model_request.max_output_tokens == 400
        assert model_request.tools is None or model_request.tools == []
        await begin_physical_invocation(
            provider="synthetic",
            model="intent",
            adapter_contract_key="synthetic/v1",
            mode="invoke",
            request={"user": model_request.messages[0].content},
        )
        return response

    ctx.router.invoke.side_effect = invoke
    with physical_invocation_scope(root), enclosing_physical_guard_scope(enclosing):
        _bind_knowledge_intent_resolver(ctx)
    with physical_invocation_scope(selected_agent):
        assert (await add(env, ctx)).result["ok"]
        root.begin.assert_awaited_once()
        enclosing.begin.assert_awaited_once()
        selected_agent.begin.assert_not_awaited()
        root.begin.side_effect = RuntimeError("Root send authority revoked")
        with pytest.raises(RuntimeError, match="Root send authority revoked"):
            await ctx.knowledge_intent_resolver(ctx, {})
        root.begin.side_effect = None
        foreign = SimpleNamespace(**{**vars(ctx), "turn_id": "t_other"})
        with pytest.raises(KnowledgeWriteError, match="knowledge_original_request_unavailable"):
            await ctx.knowledge_intent_resolver(foreign, {})
    assert root.begin.await_count == 2
    assert enclosing.begin.await_count == 2


@pytest.mark.parametrize(
    "arguments", [{}, {"file_id": "kbf_example"}, {"operation_id": "kbo_example", "artifact_id": "art_example"}, {"operation_id": ""}]
)
async def test_status_requires_one_declared_nonempty_identity(env, arguments):
    ctx = await request(env)
    status = await env.registry.get("kb_operation_status").handler(arguments, ctx)
    assert status["error"] == "knowledge_arguments_invalid"
    ctx.router.invoke.assert_not_awaited()


async def test_exact_named_source_survives_resolver_target_label_without_accepting_duplicates(env):
    ctx = await request(env, text="Add Policy.md to my personal knowledge base", attached=[env.artifact.id, "art_other"], target="one_attachment")
    assert (await add(env, ctx)).result["ok"]
    await env.artifacts.put_bytes(
        content=b"Different source",
        filename="Policy.md",
        mime="text/markdown",
        source="upload",
        kind="user_upload",
        created_by=env.user.id,
        workspace_id=env.workspace,
    )
    duplicate = await request(
        env, text="Add Policy.md to my personal knowledge base", attached=[env.artifact.id, "art_other"], target="one_attachment"
    )
    assert (await add(env, duplicate)).result["error"] == "knowledge_target_needs_clarification"


async def test_lead_cannot_write_before_explicit_selected_agent(env):
    from uuid import uuid4

    from bowhead_core.chat_references import InputDocument

    ctx = await request(env)
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {
                    "type": "reference",
                    "node_id": str(uuid4()),
                    "resource": {
                        "kind": "agent",
                        "source_kind": "team",
                        "id": "osa_selected",
                        "version": "arv_selected",
                        "release_id": "arl_selected",
                        "assignment_id": "aas_selected",
                    },
                    "label": "Selected Agent",
                    "parameters": {},
                }
            ],
        }
    )
    ctx._reference_lead_runtime = SimpleNamespace(
        validate_lead=AsyncMock(return_value=SimpleNamespace(source=SimpleNamespace(input_document=document)))
    )
    result = await add(env, ctx)
    assert result.result.get("error") == "knowledge_selected_agent_required"
    ctx.router.invoke.assert_not_awaited()
    assert await env.files.find_active_by_artifact(env.artifact.id) is None
    ctx._reference_lead_runtime = None
    assert (await add(env, ctx)).result["ok"]


@pytest.mark.parametrize("chosen,expected", [("Policy.md", True), ("Other.md", False)])
@pytest.mark.parametrize("legacy", [False, True])
async def test_only_the_actual_clarification_answer_selects_a_named_attachment(env, chosen, expected, legacy):
    from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore

    ctx = await request(env, attached=[env.artifact.id, "art_other"], target="one_attachment")
    cards = ClarificationStore(env.users.db_path)
    await cards.initialize()
    card = await cards.put(
        turn_id=ctx.turn_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        token="file_choice",
        rationale="Choose Policy.md or Other.md",
        candidates=[ClarificationCandidate(value=name, label=name) for name in ("Policy.md", "Other.md")],
        kind="ambiguous_turn",
    )
    _, resolved = await cards.resolve_once(
        card.request_id,
        chosen_value=chosen,
        answers=None if legacy else [{"question": "Choose Policy.md or Other.md", "source": "custom", "answer": chosen, "skipped": False}],
    )
    assert resolved
    result = await add(env, ctx)
    assert result.result["ok"] is expected
    if not expected:
        assert result.result["error"] == "knowledge_target_needs_clarification"
    elif legacy:
        from bowhead_core.kb.ingestion import canonical_hash

        original = await env.sessions.get_chat_turn_user_source(turn_id=ctx.turn_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
        operation = await env.files.get_operation(result.result["operation_id"])
        assert operation["action_ref"] == "request:" + canonical_hash(
            {
                "turn": ctx.turn_id,
                "input": {"original_request": original.user_text, "clarifications": []},
                "action": "add",
                "artifact": env.artifact.id,
                "scope": "personal",
            }
        )
