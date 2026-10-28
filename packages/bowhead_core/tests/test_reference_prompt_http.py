"""Prompt discovery -> HTTP admission -> native worker -> persisted evidence.

Only MCP SDK transport and model HTTP are replaced; auth, metadata, exact
receiver, original Skill baseline, admission and the production spawn run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx
from bowhead_core.chat_references import InputDocument, ReferenceNode
from bowhead_core.reference_mcp_metadata import build_reference_mcp_metadata
from bowhead_gateway.chat_reference_start import resolve_reference_prompt_endpoint
from test_reference_http_start import http_start as _http_start
from test_reference_http_start import receiver_case as _receiver_case
from test_reference_http_start import settled
from test_reference_http_start import start_case as _start_case
from test_reference_mcp_metadata import _insert_source
from test_reference_mcp_preparation import _document
from test_reference_mcp_prompt_materialization import _remote_result
from test_reference_start_preparation import raw_document
from test_skill_extension_runtime import tool_response
from test_skill_model_invocation import success

http_start, receiver_case, start_case = _http_start, _receiver_case, _start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True),
]


@pytest.fixture
async def prompt_http(http_start, monkeypatch):
    case = http_start
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "1")
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "1")
    _insert_source(
        case.path,
        team_id=case.fixture.scope.team_id,
        source_id="ots_http_prompt",
        kind="mcp",
        tools=[],
        prompts=[
            {
                "name": "profitability",
                "title": "Profitability",
                "description": "Analyze profit",
                "arguments": [{"name": "period", "description": "Period", "required": True}],
            }
        ],
        created_by=case.fixture.scope.user_id,
    )
    index = await build_reference_mcp_metadata(case.path)
    source = next(item for item in index.for_team(case.fixture.scope.team_id) if item.source_id == "ots_http_prompt")
    prompt = source.prompts[0]
    agent = next(node.resource for node in raw_document(case.fixture, skills=False).nodes if isinstance(node, ReferenceNode))
    document = _document(agent, (prompt.resource, "Profitability", {"period": "FY2026"}))
    case.app.state.reference_mcp_metadata = SimpleNamespace(current=lambda: index)
    case.app.state.orchestrator.reference_mcp_metadata = case.app.state.reference_mcp_metadata
    body = {
        "workspace_id": case.fixture.scope.workspace_id,
        "message": document.display_text(),
        "input_document": document.model_dump(mode="json", exclude_none=True),
        "client_submission_id": str(uuid4()),
        "submission_kind": "start",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
    }
    calls = []
    after_fetch = []

    class Session:
        def get_server_capabilities(self):
            return SimpleNamespace(prompts=object())

        async def get_prompt(self, name, arguments):
            calls.append((name, arguments))
            for callback in after_fetch:
                callback()
            return _remote_result("REMOTE_PROFITABILITY_METHOD")

    @asynccontextmanager
    async def connect(url, headers, **kwargs):
        assert url == "https://ots_http_prompt.example/api"
        assert headers is None and kwargs["auth"] is None
        assert kwargs["response_bytes_limit"] == 1024 * 1024
        yield Session()

    monkeypatch.setattr("bowhead_core.mcp_prompt_expansion.connect_remote_mcp", connect)
    return SimpleNamespace(case=case, prompt=prompt, agent=agent, body=body, calls=calls, after_fetch=after_fetch)


async def test_prompt_http_catalog_native_baseline_and_replay(prompt_http, monkeypatch):
    p = prompt_http
    case = p.case
    query = {"workspace_id": case.fixture.scope.workspace_id, "trigger": "/", "selected_refs": [p.agent.model_dump(mode="json")]}
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0")
    closed = await case.client.post("/api/v1/chat/references/catalog", json=query, headers={"Content-Type": "application/json"})
    assert closed.status_code == 200
    assert not next(cell for cell in closed.json()["entry_support"] if cell["resource_kind"] == "mcp_prompt")["supported"]
    assert (await case.client.post("/api/v1/chat/send", json=p.body)).status_code == 409
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "1")
    catalog = await case.client.post("/api/v1/chat/references/catalog", json=query, headers={"Content-Type": "application/json"})
    assert catalog.status_code == 200, catalog.text
    (item,) = [item for group in catalog.json()["groups"] for item in group["items"] if item["kind"] == "mcp_prompt"]
    assert item["availability"] == "baseline", item
    assert item["parameter_schema"]["required"] == ["period"]
    assert item["resource"] == p.prompt.resource.model_dump(mode="json")
    for mode in ("plan", "resume"):
        unsupported = await case.client.post(
            "/api/v1/chat/references/catalog", json={**query, "entry_mode": mode}, headers={"Content-Type": "application/json"}
        )
        assert unsupported.status_code == 200
        assert not next(cell for cell in unsupported.json()["entry_support"] if cell["resource_kind"] == "mcp_prompt")["supported"]
    model_calls = []

    def respond(request):
        body = json.loads(request.content)
        model_calls.append(body)
        assert "REMOTE_PROFITABILITY_METHOD" in json.dumps(body)
        if len(model_calls) == 1:
            return tool_response("skill_read", {"name": "unused", "ref": "guide"}, stream=body.get("stream", False))
        tool = next(message for message in body["messages"] if message["role"] == "tool")
        assert "BASELINE_GUIDE_unused" in tool["content"] and "error_kind" not in tool["content"]
        return success("openai", "stream" if body.get("stream") else "invoke")

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=p.body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        assert turn is not None and turn.status == "completed", turn
        assert route.call_count == 2
        replay = await case.client.post("/api/v1/chat/send", json=p.body)
        assert replay.status_code == 200 and replay.json() == response.json()
        assert route.call_count == 2
    assert p.calls == [("profitability", {"period": "FY2026"})]
    evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
    assert evidence.status_code == 200, evidence.text
    fact = next(item for item in evidence.json()["references"] if item["resource_kind"] == "mcp_prompt")
    assert fact["outcome"] == "fulfilled", fact
    assert "REMOTE_PROFITABILITY_METHOD" not in evidence.text
    content = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence?include_content=true")
    assert content.status_code == 200 and "REMOTE_PROFITABILITY_METHOD" in content.text
    assert "ots_http_prompt.example" not in evidence.text


@pytest.mark.parametrize("change", ["url", "auth", "disabled", "revision", "prompt", "team"])
async def test_prompt_transport_uses_only_the_exact_registered_connection(prompt_http, change):
    p = prompt_http
    case = p.case
    updates = {
        "url": ("base_url", "https://replacement.example"),
        "auth": ("auth_type", "bearer"),
        "disabled": ("status", "disabled"),
        "revision": ("updated_at", "changed"),
        "prompt": ("prompts_schema_json", "[]"),
        "team": ("team_id", "t_other"),
    }
    column, value = updates[change]
    with sqlite3.connect(case.path) as db:
        db.execute(f"UPDATE team_tool_source SET {column}=? WHERE id=?", (value, p.prompt.source_id))
    with pytest.raises(ValueError, match="reference_mcp_prompt_source_changed"):
        await resolve_reference_prompt_endpoint(
            db_path=case.path, team_id=case.fixture.scope.team_id, resource=p.prompt.resource, source_revision=p.prompt.source_revision
        )
    assert p.calls == []


async def test_prompt_source_revoked_during_expansion_never_reaches_model(prompt_http):
    p = prompt_http
    case = p.case

    def revoke():
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE team_tool_source SET status='disabled' WHERE id=?", (p.prompt.source_id,))

    p.after_fetch.append(revoke)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
        response = await case.client.post("/api/v1/chat/send", json=p.body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        assert route.call_count == 0
    assert p.calls == [("profitability", {"period": "FY2026"})]
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn is not None and turn.status == "failed"
    with sqlite3.connect(case.path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=? AND json_extract(descriptor_json, '$.content_kind')='prompt_result'",
            (turn_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("change", ["missing_parameter", "agent_missing", "system_agent", "mixed_skill", "missing_mcp_flag", "missing_prompt_flag"])
async def test_prompt_http_rejects_unsupported_input_before_acceptance(prompt_http, monkeypatch, change):
    p = prompt_http
    case = p.case
    nodes = p.body["input_document"]["nodes"]
    if change == "missing_parameter":
        next(node for node in nodes if node.get("resource", {}).get("kind") == "mcp_prompt")["parameters"] = {}
    elif change == "agent_missing":
        nodes[:] = [node for node in nodes if node.get("resource", {}).get("kind") != "agent"]
    elif change == "system_agent":
        next(node for node in nodes if node.get("resource", {}).get("kind") == "agent")["resource"] = {
            "kind": "agent",
            "source_kind": "system",
            "id": "system:compute",
            "version": "sha256:" + "a" * 64,
        }
    elif change == "mixed_skill":
        nodes.append(
            {
                "type": "reference",
                "node_id": str(uuid4()),
                "resource": {"kind": "skill", "source_kind": "builtin", "id": "builtin:unused", "version": "sha256:" + "a" * 64},
                "label": "Unused",
                "parameters": {},
            }
        )
    else:
        monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP" if change == "missing_mcp_flag" else "FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0")
    p.body["message"] = InputDocument.model_validate(p.body["input_document"]).display_text()
    response = await case.client.post("/api/v1/chat/send", json=p.body)
    assert response.status_code == 409, response.text
    assert p.calls == []
    with sqlite3.connect(case.path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_submission_receipts WHERE client_submission_id=?", (p.body["client_submission_id"],)
        ).fetchone() == (0,)


async def test_prompt_catalog_preserves_receiver_and_resource_boundaries(prompt_http):
    p = prompt_http
    case = p.case
    prompt = p.prompt.resource.model_dump(mode="json")
    agent = p.agent.model_dump(mode="json")

    async def catalog(trigger, refs):
        response = await case.client.post(
            "/api/v1/chat/references/catalog",
            json={"workspace_id": case.fixture.scope.workspace_id, "trigger": trigger, "selected_refs": refs},
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        return [item for group in response.json()["groups"] for item in group["items"]]

    peers = await catalog("@", [prompt])
    assert next(item for item in peers if item["resource"] == agent)["availability"] == "baseline"
    assert all(item["availability"] == "unavailable" for item in peers if item["source_kind"] == "system")
    choices = await catalog("/", [agent, prompt])
    assert all(item["availability"] == "unavailable" for item in choices if item["kind"] != "mcp_prompt" or item["resource"] == prompt)
    mixed = await catalog("/", [agent, {"kind": "capability", "id": "cap_visualize", "version": 1}])
    assert all(item["availability"] == "unavailable" for item in mixed if item["kind"] == "mcp_prompt")
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE team_tool_source SET status='disabled' WHERE id=?", (p.prompt.source_id,))
    assert all(item["kind"] != "mcp_prompt" for item in await catalog("/", [agent]))


async def test_prompt_rechecks_baseline_after_endpoint_lookup(prompt_http, monkeypatch):
    from test_reference_http_start import deny_skill

    from bowhead_gateway import chat_reference_start

    p = prompt_http
    case = p.case
    original = chat_reference_start.resolve_reference_prompt_endpoint

    async def resolve_then_revoke(**kwargs):
        endpoint = await original(**kwargs)
        deny_skill(case, "unused")
        return endpoint

    monkeypatch.setattr(chat_reference_start, "resolve_reference_prompt_endpoint", resolve_then_revoke)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
        response = await case.client.post("/api/v1/chat/send", json=p.body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        assert route.call_count == 0
    assert p.calls == []
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn is not None and turn.status == "failed"


@pytest.mark.parametrize("after_acceptance", ["unchanged", "close_writers", "revoke_source", "cancel", "attachment_only", "bi_resume"])
async def test_prompt_steer_runs_exact_child_after_parent(prompt_http, monkeypatch, after_acceptance):
    """Exercise real parent finalization and child spawn, including stop-write drain.

    The model boundary holds the parent open; no queue, claim or runtime is
    replaced. Removing the child Prompt dispatch must fail these assertions.
    """
    p = prompt_http
    case = p.case
    parent_entered, release_parent = asyncio.Event(), asyncio.Event()
    parent_document = raw_document(case.fixture, skills=False)
    parent_body = {
        **p.body,
        "client_submission_id": str(uuid4()),
        "message": parent_document.display_text(),
        "input_document": parent_document.model_dump(mode="json", exclude_none=True),
    }
    if after_acceptance == "attachment_only":
        p.body = await _prompt_upload_body(p)
    if after_acceptance == "bi_resume":
        from bowhead_gateway.datarc_context import DatarcTurnContext
        from bowhead_gateway.routes import chat, clarifications

        from bowhead_gateway import datarc_context

        case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")

        async def connector(*args, **kwargs):
            assert kwargs["team_id"] == case.fixture.scope.team_id
            return DatarcTurnContext(
                datarc_client=object(),
                accessible_datarc_workspaces=({"id": "project_a", "name": "A"}, {"id": "project_b", "name": "B"}),
                active_datarc_project=kwargs.get("pinned_project"),
            )

        monkeypatch.setattr(chat, "resolve_datarc_turn_context", connector)
        monkeypatch.setattr(datarc_context, "resolve_datarc_turn_context", connector)
    child_model_calls = []

    async def respond(request):
        body = json.loads(request.content)
        if "REMOTE_PROFITABILITY_METHOD" not in json.dumps(body):
            parent_entered.set()
            await asyncio.wait_for(release_parent.wait(), 15)
            return success("openai", "stream" if body.get("stream") else "invoke")
        child_model_calls.append(body)
        if len(child_model_calls) == 1:
            return tool_response("skill_read", {"name": "unused", "ref": "guide"}, stream=body.get("stream", False))
        if after_acceptance == "bi_resume" and len(child_model_calls) == 2:
            return tool_response("bi_query", {"query": "profit"}, stream=body.get("stream", False))
        tool = next(message for message in body["messages"] if message["role"] == "tool")
        assert "BASELINE_GUIDE_unused" in tool["content"] and "error_kind" not in tool["content"]
        return success("openai", "stream" if body.get("stream") else "invoke")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        parent = await case.client.post("/api/v1/chat/send", json=parent_body)
        assert parent.status_code == 200, parent.text
        parent_id = parent.json()["turn_id"]
        session_id = parent.json()["session_id"]
        try:
            await asyncio.wait_for(parent_entered.wait(), 10)
            body = {
                **p.body,
                "session_id": session_id,
                "submission_kind": "steer",
                "expected_parent_turn_id": parent_id,
                "expected_execution_generation": 1,
            }
            query = {
                "workspace_id": case.fixture.scope.workspace_id,
                "session_id": session_id,
                "trigger": "/",
                "entry_mode": "steer",
                "selected_refs": [p.agent.model_dump(mode="json")],
            }
            catalog = await case.client.post("/api/v1/chat/references/catalog", json=query, headers={"Content-Type": "application/json"})
            assert catalog.status_code == 200, catalog.text
            assert any(
                item["kind"] == "mcp_prompt" and item["availability"] == "baseline" for group in catalog.json()["groups"] for item in group["items"]
            )
            if after_acceptance == "attachment_only":
                empty = {key: value for key, value in body.items() if key not in {"attachments", "attachment_refs"}}
                assert (await case.client.post("/api/v1/chat/send", json=empty)).status_code == 422
                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE artifacts SET deleted_at='2026-09-20T00:00:00+00:00' WHERE id=?", (body["attachments"][0],))
                denied = await case.client.post("/api/v1/chat/send", json={**body, "client_submission_id": str(uuid4())})
                assert denied.status_code == 409 and denied.json()["detail"]["error"]["code"] == "attachment_unavailable"
                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE artifacts SET deleted_at=NULL WHERE id=?", (body["attachments"][0],))
            accepted = await case.client.post("/api/v1/chat/send", json=body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "buffered"
            assert accepted.json()["turn_id"] == parent_id
            replay = await case.client.post("/api/v1/chat/send", json=body)
            assert replay.json() == accepted.json()
            assert p.calls == [] and child_model_calls == []
            source_url = (
                f"/api/v1/chat/sessions/{session_id}/reference-queue/{body['client_submission_id']}/evidence"
                f"?workspace_id={case.fixture.scope.workspace_id}"
            )
            source = await case.client.get(source_url)
            assert source.status_code == 200, source.text
            assert source.headers["cache-control"] == "private, no-store"
            original = await case.client.get(f"/api/v1/turns/{parent_id}/reference-evidence?submission_id={body['client_submission_id']}")
            assert original.json() == source.json()
            wrong_session = await case.client.get(source_url.replace(f"/sessions/{session_id}/", "/sessions/s_other/"))
            assert wrong_session.status_code == 404
            assert "no-store" in wrong_session.headers["cache-control"]
            wrong_submission = await case.client.get(source_url.replace(body["client_submission_id"], str(uuid4())))
            assert wrong_submission.status_code == 404
            labels = {node["node_id"]: node["label"] for node in body["input_document"]["nodes"] if node["type"] == "reference"}
            assert {item["node_id"]: item["label"] for item in source.json()["references"]} == labels
            accepted_identity = source.json()["selection_identity"]
            assert source.json()["source_locator"]["kind"] == "queued_submission"
            assert source.json()["source_locator"]["submission_id"] == body["client_submission_id"]
            assert source.json()["source_locator"]["accepted_parent_turn_id"] == parent_id
            assert source.json()["source_locator"]["turn_id"] is None
            assert source.json()["queue"]["state"] == "queued"
            assert source.json()["execution_evidence_turn_id"] is None
            assert all("text" not in span for span in source.json()["source_locator"]["text_spans"])
            assert all("parameters" not in item for item in source.json()["references"])
            content = await case.client.get(source_url + "&include_content=true")
            assert content.status_code == 200, content.text
            assert (
                "".join(span["text"] for span in content.json()["source_locator"]["text_spans"])
                == InputDocument.model_validate(body["input_document"]).text_content()
            )
            selected_prompt = next(node for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "mcp_prompt")
            assert (
                next(item["parameters"] for item in content.json()["references"] if item["resource_kind"] == "mcp_prompt")
                == selected_prompt["parameters"]
            )
            assert (await case.client.get(source_url)).json() == source.json()
            assert (await case.client.get(f"/api/v1/turns/{parent_id}/reference-evidence?submission_id={uuid4()}")).status_code == 404
            assert p.calls == [] and child_model_calls == []
            if after_acceptance == "close_writers":
                monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "0")
                monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0")
                rejected = await case.client.post("/api/v1/chat/send", json={**body, "client_submission_id": str(uuid4())})
                assert rejected.status_code == 409
                assert (await case.client.post("/api/v1/chat/send", json=body)).json() == accepted.json()
            elif after_acceptance == "revoke_source":
                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE team_tool_source SET status='disabled' WHERE id=?", (p.prompt.source_id,))
            elif after_acceptance == "cancel":
                cancel_body = {"workspace_id": case.fixture.scope.workspace_id, "requested_session_id": session_id}
                url = f"/api/v1/chat/submissions/{body['client_submission_id']}/cancel"
                cancelled = await case.client.post(url, json=cancel_body, headers={"Content-Type": "application/json"})
                assert cancelled.status_code == 200 and cancelled.json()["outcome"] == "cancelled"
                assert (await case.client.post(url, json=cancel_body, headers={"Content-Type": "application/json"})).json() == cancelled.json()
        finally:
            release_parent.set()
            try:
                await settled(parent_id)
            except asyncio.CancelledError as exc:
                # Revoked queued work is handed to the existing durable drain
                # recovery; its dispatch fence can cancel the old parent's tail.
                assert after_acceptance == "revoke_source" and str(exc) == "dispatch_owner_lost"
        if after_acceptance == "revoke_source":
            from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher

            recovered = await case.fixture.case.store.recover_expired_chat_spawn_dispatches(
                reference_store=ChatReferenceStore(ReferenceCipher(os.environ["BOWHEAD_FERNET_KEY"])),
            )
            assert recovered == [parent_id]
        with sqlite3.connect(case.path) as db:
            queue = db.execute(
                "SELECT state,materialized_turn_id FROM chat_reference_queued_inputs WHERE client_submission_id=?", (body["client_submission_id"],)
            ).fetchone()
        source = await case.client.get(source_url)
        assert source.status_code == 200, source.text
        assert source.json()["selection_identity"] == accepted_identity
        assert source.json()["queue"]["state"] == queue[0]
        assert source.json()["source_locator"]["turn_id"] == queue[1]
        assert source.json()["execution_evidence_turn_id"] == queue[1]
        if after_acceptance == "revoke_source":
            prompt_source = next(item for item in source.json()["references"] if item["resource_kind"] == "mcp_prompt")
            assert prompt_source["resource"] is None and prompt_source["resource_visibility"] == "unavailable"
            assert prompt_source["label"] is None
            assert "resource_hidden" in source.json()["evidence_completeness"]["reason_codes"]
        if after_acceptance == "cancel":
            assert source.json()["queue"]["cancel_reason"] == "user_cancelled"
            import jwt

            owner = case.fixture.case.fixture.owner_id
            await case.fixture.case.fixture.users.set_team_creator(case.fixture.scope.team_id, owner)
            owner_token = jwt.encode(
                {"sub": owner, "team_id": case.fixture.scope.team_id, "pwd_v": 0}, os.environ["BOWHEAD_JWT_SECRET"], algorithm="HS256"
            )
            owner_headers = {"Authorization": "Bearer " + owner_token}
            assert (await case.client.get(f"/api/v1/turns/{parent_id}/events", headers=owner_headers)).status_code == 200
            assert (await case.client.get(source_url + "&include_content=true", headers=owner_headers)).status_code == 403
            assert (
                await case.client.get(
                    f"/api/v1/turns/{parent_id}/reference-evidence?submission_id={body['client_submission_id']}", headers=owner_headers
                )
            ).status_code == 404

            async def failed_audit(**_kwargs):
                raise OSError("audit storage unavailable")

            with monkeypatch.context() as patch:
                patch.setattr(case.app.state.user_store, "log_audit", failed_audit)
                refused = await case.client.get(source_url + "&include_content=true")
                metadata = await case.client.get(source_url)
            assert refused.status_code == 503 and refused.json()["detail"]["error"]["code"] == "reference_content_audit_unavailable"
            assert metadata.status_code == 200 and metadata.json()["queue"]["state"] == "cancelled"
            with monkeypatch.context() as patch:
                patch.delenv("BOWHEAD_FERNET_KEY")
                no_key = await case.client.get(source_url)
            assert no_key.status_code == 200, no_key.text
            assert no_key.json()["queue"]["state"] == "cancelled" and no_key.json()["references"] == []
            assert "content_key_unavailable" in no_key.json()["evidence_completeness"]["reason_codes"]
            with sqlite3.connect(case.path) as db:
                db.execute(
                    "UPDATE chat_reference_queued_inputs SET retention_state='purged',ciphertext=NULL,row_version=row_version+1 WHERE client_submission_id=?",
                    (body["client_submission_id"],),
                )
            purged = await case.client.get(source_url + "&include_content=true")
            assert purged.status_code == 200, purged.text
            assert purged.json()["queue"]["state"] == "cancelled"
            assert purged.json()["references"] == [] and purged.json()["source_locator"]["text_spans"] == []
            assert "original_input_unavailable" in purged.json()["evidence_completeness"]["reason_codes"]
            with sqlite3.connect(case.path) as db:
                audit = db.execute(
                    "SELECT payload_json FROM user_audit_log WHERE event_type='reference_evidence_content_read' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            assert json.loads(audit[0])["submission_id"] == body["client_submission_id"]
        elif queue[1] is not None:
            assert (
                await case.client.get(f"/api/v1/turns/{queue[1]}/reference-evidence?submission_id={body['client_submission_id']}")
            ).status_code == 404
        if after_acceptance in {"revoke_source", "cancel"}:
            if after_acceptance == "cancel":
                assert queue == ("cancelled", None), queue
            else:
                assert queue[0] == "materialized", queue
                blocked = await case.fixture.case.store.get_turn(queue[1])
                assert blocked.status == "failed" and blocked.error_kind == "reference_queue_blocked"
                source = await case.fixture.case.store.get_chat_turn_user_source(
                    turn_id=queue[1],
                    user_id=case.fixture.scope.user_id,
                    workspace_id=case.fixture.scope.workspace_id,
                )
                assert source.input_document == InputDocument.model_validate(p.body["input_document"])
                evidence = await case.client.get(f"/api/v1/turns/{queue[1]}/reference-evidence")
                assert evidence.status_code == 200, evidence.text
                locator = evidence.json()["source_locator"]
                assert locator["kind"] == "queued_submission" and locator["submission_id"] == body["client_submission_id"]
                assert locator["accepted_parent_turn_id"] == parent_id and locator["turn_id"] == queue[1]
                prompt = next(item for item in evidence.json()["references"] if item["resource_kind"] == "mcp_prompt")
                assert prompt["resource"] is None and prompt["outcome"] == "blocked"
                replay = await case.client.get(f"/api/v1/chat/stream/{parent_id}")
                assert replay.status_code == 200 and queue[1] in replay.text and "event: turn.follow_up" in replay.text
            assert p.calls == [] and child_model_calls == []
            return
        assert queue[0] == "materialized" and queue[1] != parent_id, queue
        child_id = queue[1]
        # The real parent schedules its child before finalizing. The child may
        # have already finished, in which case persisted state is authoritative.
        child = await case.fixture.case.store.get_turn(child_id)
        if child.status not in {"completed", "failed", "cancelled", "clarification_pending"}:
            await settled(child_id)
            child = await case.fixture.case.store.get_turn(child_id)
        if after_acceptance == "bi_resume":
            assert child.status == "clarification_pending"
            # The card can be durable before its old dispatch monitor settles.
            # Wait for that real tail before claiming the next generation.
            tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + child_id)]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 30)
            with sqlite3.connect(case.path) as db:
                question_id = db.execute("SELECT clarification_id FROM chat_reference_bi_continuations WHERE turn_id=?", (child_id,)).fetchone()[0]
            _, won = await case.app.state.clarification_store.resolve_once(question_id, "project_b")
            assert won
            resumed = await case.client.post(f"/api/v1/clarifications/{question_id}/resume_turn")
            assert resumed.status_code == 200 and resumed.json()["execution_generation"] == 2, resumed.text
            await settled(child_id)
            child = await case.fixture.case.store.get_turn(child_id)
            assert any(m.get("tool_call_id") == "call-bi_query" and "project_b" in m["content"] for m in child_model_calls[-1]["messages"])
            with sqlite3.connect(case.path) as db:
                assert db.execute("SELECT parent_turn_id,dispatch_kind FROM chat_turn_spawn_dispatch WHERE turn_id=?", (child_id,)).fetchone() == (
                    parent_id,
                    "follow_up",
                )
        assert child.status == "completed", child
        assert p.calls == [("profitability", {"period": "FY2026"})]
        assert len(child_model_calls) == (3 if after_acceptance == "bi_resume" else 2)
        if after_acceptance == "attachment_only":
            assert all("period,profit\\nFY2026,42" in json.dumps(model) for model in child_model_calls)
        source = await case.fixture.case.store.get_chat_turn_user_source(
            turn_id=child_id,
            user_id=case.fixture.scope.user_id,
            workspace_id=case.fixture.scope.workspace_id,
        )
        assert source.input_document == InputDocument.model_validate(p.body["input_document"])
        assert list(source.attachment_ids) == p.body.get("attachments", [])
        assert list(source.attachment_refs) == p.body.get("attachment_refs", [])
        evidence = await case.client.get(f"/api/v1/turns/{child_id}/reference-evidence")
        assert evidence.status_code == 200, evidence.text
        assert next(item for item in evidence.json()["references"] if item["resource_kind"] == "mcp_prompt")["outcome"] == "fulfilled"
        locator = evidence.json()["source_locator"]
        assert locator["kind"] == "queued_submission"
        assert locator["submission_id"] == body["client_submission_id"]
        assert locator["accepted_parent_turn_id"] == parent_id
        assert locator["turn_id"] == locator["logical_source_turn_id"] == child_id
        assert all("text" not in span for span in locator["text_spans"])
        state = await case.client.get(f"/api/v1/turns/{child_id}/references")
        assert state.status_code == 200 and state.json()["source_locator"] == locator
        assert (await case.client.get(f"/api/v1/turns/{child_id}/reference-evidence")).json() == evidence.json()
        # The parent's own source must never be rewritten as the queued input.
        parent_evidence = await case.client.get(f"/api/v1/turns/{parent_id}/reference-evidence")
        assert parent_evidence.status_code == 200
        assert parent_evidence.json()["source_locator"]["kind"] == "turn_source"
        replay = await case.client.post("/api/v1/chat/send", json=body)
        assert replay.json() == accepted.json()
        assert len(child_model_calls) == (3 if after_acceptance == "bi_resume" else 2) and len(p.calls) == 1


async def test_queued_evidence_keeps_accepted_parent_after_holder_transfer(prompt_http):
    from bowhead_gateway.routes.chat import _inflight_tasks

    p = prompt_http
    case = p.case
    entered, release = asyncio.Event(), asyncio.Event()
    document = raw_document(case.fixture, skills=False)

    async def respond(request):
        model = json.loads(request.content)
        if "REMOTE_PROFITABILITY_METHOD" not in json.dumps(model):
            entered.set()
            await asyncio.wait_for(release.wait(), 15)
        return success("openai", "stream" if model.get("stream") else "invoke")

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        parent = await case.client.post(
            "/api/v1/chat/send",
            json={
                **p.body,
                "client_submission_id": str(uuid4()),
                "message": document.display_text(),
                "input_document": document.model_dump(mode="json", exclude_none=True),
            },
        )
        assert parent.status_code == 200, parent.text
        parent_id = parent.json()["turn_id"]
        submissions = []
        try:
            await asyncio.wait_for(entered.wait(), 10)
            for _ in range(2):
                body = {
                    **p.body,
                    "client_submission_id": str(uuid4()),
                    "session_id": parent.json()["session_id"],
                    "submission_kind": "steer",
                    "expected_parent_turn_id": parent_id,
                    "expected_execution_generation": 1,
                }
                accepted = await case.client.post("/api/v1/chat/send", json=body)
                assert accepted.status_code == 200 and accepted.json()["status"] == "buffered", accepted.text
                submissions.append(body["client_submission_id"])
        finally:
            release.set()
            await settled(parent_id)
        children = []
        for submission in submissions:
            with sqlite3.connect(case.path) as db:
                child_id = db.execute(
                    "SELECT materialized_turn_id FROM chat_reference_queued_inputs WHERE client_submission_id=?", (submission,)
                ).fetchone()[0]
            assert child_id
            pending = [task for task in _inflight_tasks if task.get_name().endswith(f":{child_id}")]
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 30)
            assert (await case.fixture.case.store.get_turn(child_id)).status == "completed"
            evidence = await case.client.get(f"/api/v1/turns/{child_id}/reference-evidence")
            assert evidence.status_code == 200, evidence.text
            locator = evidence.json()["source_locator"]
            assert locator["kind"] == "queued_submission" and locator["submission_id"] == submission
            assert locator["accepted_parent_turn_id"] == parent_id and locator["turn_id"] == child_id
            source = await case.client.get(
                f"/api/v1/chat/sessions/{parent.json()['session_id']}/reference-queue/{submission}/evidence"
                f"?workspace_id={case.fixture.scope.workspace_id}"
            )
            assert source.status_code == 200, source.text
            for key in ("kind", "submission_id", "accepted_parent_turn_id", "turn_id"):
                assert source.json()["source_locator"][key] == locator[key]
            assert source.json()["execution_evidence_turn_id"] == child_id
            children.append(child_id)
        assert len(set(children)) == 2
        with sqlite3.connect(case.path) as db:
            assert db.execute("SELECT parent_turn_id FROM chat_turn_spawn_dispatch WHERE turn_id=?", (children[1],)).fetchone() == (children[0],)
        assert route.call_count == 3 and len(p.calls) == 2
        # The original queue identity remains required: another submission or a
        # cyclic dispatch chain cannot authorize this child, even in one Session.
        import aiosqlite
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.chat_reference_runtime_store import reference_dispatch_origin_valid_in_connection
        from bowhead_core.chat_reference_storage import ReferenceScope

        scope = ReferenceScope(**{**case.fixture.scope.model_dump(), "session_id": parent.json()["session_id"], "policy_epoch": 1})
        async with open_shared_ctx(str(case.path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:

                async def valid():
                    async with db.execute("SELECT * FROM chat_turn_spawn_dispatch WHERE turn_id=?", (children[1],)) as cursor:
                        row = await cursor.fetchone()
                    return await reference_dispatch_origin_valid_in_connection(db, row=row, scope=scope, turn_id=children[1])

                assert await valid()
                for sql, args in (
                    ("UPDATE chat_turn_spawn_dispatch SET parent_turn_id=? WHERE turn_id=?", (children[1], children[1])),
                    ("UPDATE chat_turn_spawn_dispatch SET parent_turn_id=? WHERE turn_id=?", (children[1], children[0])),
                    (
                        "UPDATE chat_turn_spawn_dispatch SET source_submission_id=?,source_submission_ids_json=? WHERE turn_id=?",
                        (submissions[0], json.dumps([submissions[0]]), children[1]),
                    ),
                ):
                    await db.execute("SAVEPOINT malformed_origin")
                    await db.execute(sql, args)
                    assert not await valid()
                    await db.execute("ROLLBACK TO SAVEPOINT malformed_origin")
                    await db.execute("RELEASE SAVEPOINT malformed_origin")
            finally:
                await db.rollback()


async def _prompt_upload_body(p, *, content=b"period,profit\nFY2026,42\n", filename="profit.csv", mime="text/csv"):
    from datetime import UTC, datetime

    from bowhead_core.artifact_store import ArtifactStore, LocalBackend

    case = p.case
    uploads = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "prompt-uploads"))
    case.app.state.artifact_store = uploads
    case.app.state.orchestrator.artifact_store = uploads
    document = InputDocument.model_validate(
        {
            **p.body["input_document"],
            "nodes": [node for node in p.body["input_document"]["nodes"] if node["type"] == "reference"],
        }
    )
    assert document.text_content() == ""
    body = {**p.body, "input_document": document.model_dump(mode="json", exclude_none=True), "message": document.display_text()}
    artifact = await uploads.put_bytes(
        content=content,
        filename=filename,
        mime=mime,
        created_by=case.fixture.scope.user_id,
        workspace_id=case.fixture.scope.workspace_id,
    )
    with sqlite3.connect(case.path) as db:
        db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES (?,?,?,?)",
            (artifact.id, case.fixture.scope.workspace_id, case.fixture.scope.user_id, datetime.now(UTC).isoformat()),
        )
    body.update(
        attachments=[artifact.id],
        attachment_refs=[
            {
                "fileId": artifact.id,
                "filename": artifact.filename,
                "mime": artifact.mime,
                "size": artifact.size,
                "referenceKind": "upload",
            }
        ],
    )
    return body


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("selection", ["prompt", "agent", "skill", "automatic"])
async def test_reference_start_retains_typed_upload_and_replays_once(prompt_http, monkeypatch, with_text, selection):
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1" if with_text else "0")
    p = prompt_http
    case = p.case
    if selection != "prompt":
        document = raw_document(case.fixture, skills=selection in {"skill", "automatic"})
        if selection == "automatic":
            document = InputDocument.model_validate(
                {
                    "schema_version": 1,
                    "nodes": [
                        node.model_dump(mode="python", exclude_none=True)
                        for node in document.nodes
                        if not isinstance(node, ReferenceNode) or node.resource.kind != "agent"
                    ],
                }
            )
        p.body.update(input_document=document.model_dump(mode="json", exclude_none=True), message=document.display_text())
    body = await _prompt_upload_body(p)
    if with_text:
        body["input_document"] = p.body["input_document"]
        body["message"] = p.body["message"]
    calls = []

    def respond(request):
        model = json.loads(request.content)
        calls.append(model)
        assert ("REMOTE_PROFITABILITY_METHOD" in json.dumps(model)) == (selection == "prompt")
        return success("openai", "stream" if model.get("stream") else "invoke")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
        # A completed receipt is a replay, not a fresh grant to read the file.
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE artifacts SET deleted_at='2026-09-20T00:00:00+00:00' WHERE id=?", (body["attachments"][0],))
        assert (await case.client.post("/api/v1/chat/send", json=body)).json() == response.json()
    assert len(calls) == 1 and len(p.calls) == (1 if selection == "prompt" else 0)
    assert "profit.csv" in json.dumps(calls[0])
    assert "period,profit\\nFY2026,42" in json.dumps(calls[0])
    material = [message for message in calls[0]["messages"] if "profit.csv" in str(message.get("content"))]
    assert material and all(message["role"] == "user" for message in material)
    source = await case.fixture.case.store.get_chat_turn_user_source(
        turn_id=turn_id,
        user_id=case.fixture.scope.user_id,
        workspace_id=case.fixture.scope.workspace_id,
    )
    assert source.input_document == InputDocument.model_validate(body["input_document"])
    assert list(source.attachment_ids) == body["attachments"]
    assert list(source.attachment_refs) == body["attachment_refs"]


@pytest.mark.parametrize("change", ["deleted", "deleted_after_lookup", "metadata", "untyped"])
async def test_reference_start_attachment_rejection_is_atomic(prompt_http, monkeypatch, change):
    from test_reference_http_start import acceptance_counts

    p = prompt_http
    case = p.case
    body = await _prompt_upload_body(p)
    artifact_id = body["attachments"][0]

    def remove_upload():
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE artifacts SET deleted_at='2026-09-20T00:00:00+00:00' WHERE id=?", (artifact_id,))

    if change == "deleted":
        remove_upload()
    elif change == "deleted_after_lookup":
        uploads = case.app.state.artifact_store
        read = uploads.get_ready_uploads_for_chat

        async def revoke_after_read(*args, **kwargs):
            found = await read(*args, **kwargs)
            assert artifact_id in found
            remove_upload()
            return found

        monkeypatch.setattr(uploads, "get_ready_uploads_for_chat", revoke_after_read)
    elif change == "metadata":
        body["attachment_refs"][0]["filename"] = "replacement.csv"
    else:
        del body["attachment_refs"]
    before = acceptance_counts(case)
    with respx.mock(assert_all_called=False) as mock:
        provider = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == {"untyped": "unsupported_entry", "metadata": "attachment_metadata_stale"}.get(
            change, "attachment_unavailable"
        )
        assert provider.call_count == 0
    assert acceptance_counts(case) == before
    assert p.calls == []


@pytest.mark.parametrize("stage", ["after_prompt", "after_model"])
async def test_reference_attachment_revocation_stops_subsequent_model_requests(prompt_http, stage):
    p = prompt_http
    case = p.case
    body = await _prompt_upload_body(p)

    def revoke():
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE artifacts SET deleted_at='2026-09-20T00:00:00+00:00' WHERE id=?", (body["attachments"][0],))

    if stage == "after_prompt":
        p.after_fetch.append(revoke)
    calls = []

    def respond(request):
        model = json.loads(request.content)
        calls.append(model)
        if stage == "after_model" and len(calls) == 1:
            revoke()
            return tool_response("skill_read", {"name": "unused", "ref": "guide"}, stream=model.get("stream", False))
        return success("openai", "stream" if model.get("stream") else "invoke")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        if stage == "after_model":
            from datetime import UTC, datetime, timedelta

            with pytest.raises(RuntimeError, match="legacy_chat_durability_recovery_handoff"):
                await settled(turn_id)
            with sqlite3.connect(case.path) as db:
                db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), turn_id),
                )
            assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == [turn_id]
        else:
            await settled(turn_id)
    assert len(calls) == (1 if stage == "after_model" else 0)
    assert (await case.fixture.case.store.get_turn(turn_id)).status == "failed"


@pytest.mark.parametrize(
    "change,with_file",
    [
        ("unchanged", False),
        ("writer_closed", False),
        ("source_revoked", False),
        ("skill_revoked", False),
        ("material_purged", False),
        ("unchanged", True),
        ("writer_closed", True),
        ("file_revoked", True),
    ],
)
@pytest.mark.parametrize("read_before_pause", [False, True])
async def test_prompt_http_resumes_original_material_once(prompt_http, monkeypatch, tmp_path, change, with_file, read_before_pause):
    from bowhead_gateway.datarc_context import DatarcTurnContext
    from bowhead_gateway.routes import chat, clarifications

    from bowhead_gateway import datarc_context

    p = prompt_http
    case = p.case
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    workspaces = ({"id": "project_a", "name": "A"}, {"id": "project_b", "name": "B"})

    async def connector(*args, **kwargs):
        assert kwargs["team_id"] == case.fixture.scope.team_id
        return DatarcTurnContext(
            datarc_client=object(),
            accessible_datarc_workspaces=workspaces,
            active_datarc_project=kwargs.get("pinned_project"),
        )

    monkeypatch.setattr(chat, "resolve_datarc_turn_context", connector)
    monkeypatch.setattr(datarc_context, "resolve_datarc_turn_context", connector)
    if with_file:
        p.body = await _prompt_upload_body(p)
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1 and read_before_pause:
            return tool_response("skill_read", {"name": "unused", "ref": "guide"}, stream=body.get("stream", False))
        if len(bodies) == 1 + int(read_before_pause):
            return tool_response("bi_query", {"query": "profit"}, stream=body.get("stream", False))
        return success("openai", "stream" if body.get("stream") else "invoke")

    def saved(table):
        with sqlite3.connect(case.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE turn_id=?", (turn_id,))]

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=p.body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
        pause = saved("chat_reference_bi_continuations")[0]
        assert pause["state"] == "ready" and pause["checkpoint_version"] == (3 if with_file else 1 + int(read_before_pause))
        if change == "unchanged" and not with_file:
            from test_chat_reference_storage import _historical_fixture, _upgrade
            from test_reference_model_context_migration import snapshot

            previous = tmp_path / "paused-0104.db"
            _historical_fixture(case.path, previous, "0104_reference_prompt_continuation")
            retained = snapshot(previous)
            assert retained["chat_reference_bi_continuations"]
            _upgrade(previous, "0105_reference_attachment_checkpoint")
            assert snapshot(previous) == retained
        paused_evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
        assert paused_evidence.status_code == 200
        assert next(item for item in paused_evidence.json()["references"] if item["resource_kind"] == "mcp_prompt")["outcome"] != "fulfilled"
        before = saved("chat_reference_resolutions")
        execution = saved("turn_agent_execution")[0]["id"]
        question_id = pause["clarification_id"]
        _, won = await case.app.state.clarification_store.resolve_once(question_id, "project_b")
        assert won
        if change == "writer_closed":
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "0")
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0")
        elif change == "source_revoked":
            with sqlite3.connect(case.path) as db:
                db.execute("UPDATE team_tool_source SET status='disabled' WHERE id='ots_http_prompt'")
        elif change == "skill_revoked":
            await case.fixture.sources.instances.upsert(
                user_id=case.fixture.scope.user_id,
                name="unused",
                source_type="builtin",
                source_path=str(case.fixture.root / "unused"),
                enabled=False,
            )
        elif change == "material_purged":
            with sqlite3.connect(case.path) as db:
                db.execute(
                    "UPDATE chat_reference_content_snapshots SET retention_state='purged',ciphertext=NULL "
                    "WHERE turn_id=? AND json_extract(descriptor_json,'$.content_kind')='prompt_result'",
                    (turn_id,),
                )
        if with_file:
            if change == "file_revoked":
                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE artifacts SET deleted_at='2026-09-20T00:00:00+00:00' WHERE id=?", (p.body["attachments"][0],))
            # Changing backing bytes must not replace the original model data.
            # A second parse would also produce new step events (checked below).
            for file in (case.path.parent / "prompt-uploads").rglob("*"):
                if file.is_file():
                    file.write_bytes(b"REPLACED_FILE_MATERIAL")
        with sqlite3.connect(case.path) as db:
            parse_events = db.execute(
                "SELECT COUNT(*) FROM turn_events WHERE turn_id=? AND json_extract(payload_json,'$.step_type')='parse'", (turn_id,)
            ).fetchone()[0]
        if with_file:
            assert parse_events > 0
        # The resumed Agent must use its original Skill material, too.
        for path in case.fixture.root.rglob("*.md"):
            path.unlink()
        resumed = await case.client.post(f"/api/v1/clarifications/{question_id}/resume_turn")
        if change in {"source_revoked", "skill_revoked", "material_purged", "file_revoked"}:
            assert resumed.status_code == 409, resumed.text
            assert route.call_count == 1 + int(read_before_pause)
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            assert saved("chat_reference_bi_continuations")[0]["state"] == "ready"
        else:
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["execution_generation"] == 2
            await settled(turn_id)
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
            assert route.call_count == 2 + int(read_before_pause)
            assert (await case.client.post(f"/api/v1/clarifications/{question_id}/resume_turn")).json() == resumed.json()
            assert route.call_count == 2 + int(read_before_pause)
            assert saved("chat_reference_bi_continuations")[0]["state"] == "closed"
            assert saved("chat_reference_resolutions") == before
            assert saved("turn_agent_execution")[0]["id"] == execution
            final_evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
            assert final_evidence.status_code == 200
            assert next(item for item in final_evidence.json()["references"] if item["resource_kind"] == "mcp_prompt")["outcome"] == "fulfilled"
            assert {item["generation"] for item in final_evidence.json()["model_invocations"]} == {1, 2}
            messages = bodies[-1]["messages"]
            assert any(m.get("tool_call_id") == "call-bi_query" and "project_b" in m["content"] for m in messages)
            if read_before_pause:
                assert any(m.get("tool_call_id") == "call-skill_read" and "BASELINE_GUIDE_unused" in m["content"] for m in messages)
            invocations = saved("chat_reference_model_invocations")
            assert sorted(row["created_generation"] for row in invocations) == [1] * (1 + int(read_before_pause)) + [2]
        assert p.calls == [("profitability", {"period": "FY2026"})]
        assert all("REMOTE_PROFITABILITY_METHOD" in json.dumps(body) for body in bodies)
        if with_file:
            for body in bodies:
                material = [message for message in body["messages"] if "profit.csv" in str(message.get("content"))]
                assert len(material) == 1 and material[0]["role"] == "user"
                assert "period,profit\nFY2026,42" in material[0]["content"]
                assert "REPLACED_FILE_MATERIAL" not in json.dumps(body)
            with sqlite3.connect(case.path) as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM turn_events WHERE turn_id=? AND json_extract(payload_json,'$.step_type')='parse'", (turn_id,)
                    ).fetchone()[0]
                    == parse_events
                )


async def test_prompt_resume_migration_preserves_existing_encrypted_results(prompt_http, tmp_path):
    from importlib import import_module

    from test_chat_reference_storage import _historical_fixture, _upgrade
    from test_reference_model_context_migration import snapshot

    p = prompt_http
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(
            side_effect=lambda request: success("openai", "stream" if json.loads(request.content).get("stream") else "invoke")
        )
        response = await p.case.client.post("/api/v1/chat/send", json=p.body)
        assert response.status_code == 200, response.text
        await settled(response.json()["turn_id"])
    migration = import_module("bowhead_core.migrations.versions.0104_reference_prompt_continuation")
    path = tmp_path / "previous.db"
    _historical_fixture(p.case.path, path, migration.down_revision)
    before = snapshot(path)
    assert before["chat_reference_content_snapshots"] and before["chat_reference_model_invocations"]
    _upgrade(path, migration.revision)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
