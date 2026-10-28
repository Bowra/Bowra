"""Real upload, review ledger and ordinary Chat admission; only rendering is synthetic."""

import asyncio
import base64
import hashlib
from uuid import uuid4

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.request import DocumentProposal, DocumentScope, InputRoleProposal, UserQuote
from bowhead_core.documents.store import DocumentStore
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes.auth import _auth_session_generation_hash
from test_chat_upload_integration import _assert_accepted, _send, _upload
from test_chat_upload_integration import case as case

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def review(case, monkeypatch):
    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(b"x" * 32).decode())
    ref = await _upload(case, "multipart", kind="pptx")
    instruction = "Use the attached template to create a presentation and PDF."
    scope = DocumentScope(team_id=case.alice.team_id, user_id=case.alice.id, workspace_id=case.spaces[0])
    quote = UserQuote(text=instruction)

    async def admit(req, source):
        return await DocumentStore(case.path).submit_from_turn(
            scope=scope,
            turn_id=source.turn_id,
            command_id="initial-document",
            proposal=DocumentProposal(
                evidence=quote,
                inputs=(InputRoleProposal(artifact_id=ref["fileId"], role="template", evidence=quote, current_message_position=1),),
                requested_outputs=("source", "pdf"),
            ),
        )

    case.document_probe.run = admit
    source = await _assert_accepted(case, await _send(case, [ref], message=instruction), [ref])
    receipt = case.document_probe.results[0]
    for _ in range(100):
        if await case.sessions.get_chat_spawn_dispatch_projection_state(turn_id=source.turn_id) == "settled":
            break
        await asyncio.sleep(0.01)
    assert await case.sessions.get_chat_spawn_dispatch_projection_state(turn_id=source.turn_id) == "settled"
    case.document_probe.run = None
    await DocumentInputStore(case.artifacts).freeze(scope=scope, job_id=receipt.job_id)
    delivery = DocumentDeliveryStore(case.artifacts)
    owner = await delivery.claim(scope=scope, job_id=receipt.job_id)
    await delivery.transition(scope=scope, job_id=receipt.job_id, owner=owner, state="planning", image_id="sha256:test")
    await delivery.transition(scope=scope, job_id=receipt.job_id, owner=owner, state="rendering")
    await delivery.save_draft(
        scope=scope,
        job_id=receipt.job_id,
        owner=owner,
        result=WorkerResult(
            {"kind": "draft", "manifest": {"semantic_status": "unapproved"}, "preview": {"native_powerpoint_certified": False}},
            {"candidate.pptx": b"synthetic rendered candidate", "candidate.pdf": b"%PDF-synthetic", "page-1.png": b"synthetic preview"},
            "sha256:test",
        ),
    )
    draft = await delivery.get(scope=scope, job_id=receipt.job_id)
    headers = case.headers()
    token = headers["Authorization"].removeprefix("Bearer ")
    generation = "legacy_" + hashlib.sha256(f"bowhead-auth-session-legacy.v1\0{token}".encode()).hexdigest()[:32]
    await case.sessions.register_chat_auth_session_generation(
        user_id=case.alice.id, auth_session_generation_hash=_auth_session_generation_hash(generation)
    )
    payload = {
        "session_id": source.session_id,
        "workspace_id": scope.workspace_id,
        "message": "Shorten the title.",
        "client_submission_id": str(uuid4()),
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "document_revision_target": {
            "kind": "document_revision",
            "version": 1,
            "session_id": source.session_id,
            "job_id": receipt.job_id,
            "draft_digest": draft["draft_digest"],
        },
    }
    return case, delivery, scope, payload, headers


async def test_real_revision_send_keeps_parent_and_replays_receipt_when_disabled(review, monkeypatch):
    case, delivery, scope, payload, headers = review
    response = await case.client.post("/api/v1/chat/send", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["document_revision_target"] == payload["document_revision_target"]
    source = await SessionStore(case.path).get_chat_turn_user_source(
        turn_id=accepted["turn_id"], user_id=scope.user_id, workspace_id=scope.workspace_id
    )
    assert source.document_revision_target.model_dump() == payload["document_revision_target"]
    assert (await delivery.get(scope=scope, job_id=payload["document_revision_target"]["job_id"]))["state"] == "review"
    for _ in range(100):
        if any(req.turn_id == accepted["turn_id"] for req, _ in case.captured):
            break
        await asyncio.sleep(0.01)
    req = next(req for req, _ in case.captured if req.turn_id == accepted["turn_id"])
    assert req.document_revision_target == source.document_revision_target
    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "0")
    replay = await case.client.post("/api/v1/chat/send", headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json() == accepted
    fresh = await case.client.post("/api/v1/chat/send", headers=headers, json={**payload, "client_submission_id": str(uuid4())})
    assert fresh.status_code == 409 and fresh.json()["detail"]["error"]["code"] == "document_revision_unavailable"
    replay_as_new = {
        **payload,
        "client_submission_id": str(uuid4()),
        "message": "",
        "submission_kind": "replay",
        "replay_source_turn_id": accepted["turn_id"],
    }
    del replay_as_new["document_revision_target"]
    refused = await case.client.post("/api/v1/chat/send", headers=headers, json=replay_as_new)
    assert refused.status_code == 409 and refused.json()["detail"]["error"]["code"] == "unsupported_entry"
    # Browser opt-in remains disabled. The retained API substrate still accepts
    # ordinary explicit clients exactly as before this revision feature.
    from bowhead_core.composer_rollout import composer_submission_protocol_enabled

    assert composer_submission_protocol_enabled() is False
    ordinary = {**payload, "client_submission_id": str(uuid4())}
    del ordinary["document_revision_target"]
    result = await case.client.post("/api/v1/chat/send", headers=headers, json=ordinary)
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("change", ["stale", "published", "wrong_session", "write_failure"])
async def test_real_revision_rejection_rolls_back_turn_source_and_dispatch(review, change):
    case, delivery, scope, payload, headers = review
    if change == "stale":
        payload["document_revision_target"]["draft_digest"] = "f" * 64
    elif change == "published":
        await delivery.confirm(
            scope=scope,
            job_id=payload["document_revision_target"]["job_id"],
            command_id="confirm-wins",
            draft_digest=payload["document_revision_target"]["draft_digest"],
        )
    elif change == "wrong_session":
        session = await case.sessions.ensure_session(session_id=None, user_id=scope.user_id, workspace_id=scope.workspace_id)
        payload["session_id"] = payload["document_revision_target"]["session_id"] = session.id
    else:
        async with open_shared_ctx(case.path) as db:
            await db.execute(
                "CREATE TRIGGER reject_revision_source BEFORE INSERT ON chat_turn_user_sources WHEN NEW.document_revision_target_json IS NOT NULL BEGIN SELECT RAISE(ABORT,'test source failure'); END"
            )
            await db.commit()
    response = await case.client.post("/api/v1/chat/send", headers=headers, json=payload)
    assert response.status_code == (503 if change == "write_failure" else 409), response.text
    async with open_shared_ctx(case.path) as db:
        for table in ("turns", "chat_turn_user_sources"):
            assert (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT count(*) FROM chat_turn_spawn_dispatch WHERE turn_id NOT IN (SELECT id FROM turns)")).fetchone())[
            0
        ] == 0
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 1
    if change != "write_failure":
        assert response.json()["detail"]["error"]["code"] == ("document_not_found" if change == "wrong_session" else "document_revision_conflict")
        replay = await case.client.post("/api/v1/chat/send", headers=headers, json=payload)
        assert replay.status_code == response.status_code and replay.json() == response.json()
