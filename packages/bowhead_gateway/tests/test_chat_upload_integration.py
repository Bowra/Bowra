"""Actual upload writers -> ordinary Chat authorization -> immutable user source.

Only the external model/connector worker is replaced. No upload bindings or
canonical sources are inserted by these fixtures. Presign uses the real local
receiver; production object-storage transport still needs browser acceptance.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from types import SimpleNamespace
from zipfile import ZipFile

import httpx2
import jwt
import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import current_attachment_positions
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.session_store import SessionStore
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.datarc_context import DatarcTurnContext
from bowhead_gateway.routes import chat, files, files_local_receiver, files_presign
from fastapi import FastAPI
from test_chat_datarc_decouple import FakeOrchestrator
from test_files_office_upload import _office_bytes

from bowhead_gateway import document_service, turn_service

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
_SECRET = "ordinary-chat-upload-test-secret-32-bytes"


@pytest.fixture
async def case(tmp_path, monkeypatch, initialize_migrated_core_database):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.delenv("BOWHEAD_JWT_SECRETS", raising=False)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    # Deployments always set the instance Fernet root; team chat turns build the
    # collaboration cipher from it (16e44bbc5). Do not rely on a checkout .env.
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(b"u" * 32).decode())
    path = tmp_path / TARGET_FILES["core"]
    initialize_migrated_core_database(path)
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Upload integration", max_seats=3)
    alice = await users.create_user(team_id=team.id, email="alice@example.test", email_verified=True)
    bob = await users.create_user(team_id=team.id, email="bob@example.test", email_verified=True)
    spaces = []
    for name in ("First", "Second"):
        space = await users.sync_workspace_from_datarc(team_id=team.id, datarc_workspace_id=name, name=name)
        for user in (alice, bob):
            await users.add_member(workspace_id=space, user_id=user.id)
        spaces.append(space)
    sessions = SessionStore(path)
    await sessions.initialize()
    backend = LocalBackend(root_dir=tmp_path / "blobs", allow_local_receiver=True, gateway_base_url="http://test", local_presign_secret=_SECRET)
    artifacts = ArtifactStore(db_path=path, backend=backend)
    await artifacts.initialize()
    logs = TurnLogStore(path)
    registry = EventBusRegistry(default_log_sink=OrderedTurnLogSink(logs))
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v1/chat")
    app.include_router(files.router, prefix="/api/v1/files")
    app.include_router(files_presign.router)
    app.include_router(files_local_receiver.router)
    app.state.settings = SimpleNamespace(auth_mode="prod")
    app.state.user_store = users
    app.state.access_control_store = AccessControlStore(path)
    await app.state.access_control_store.initialize()
    app.state.session_store = sessions
    app.state.artifact_store = artifacts
    app.state.artifact_backend = backend
    app.state.local_presign_secret = _SECRET
    app.state.terminal_protocol_store = TerminalProtocolStore(path)
    app.state.turn_log_store = logs
    app.state.orchestrator = FakeOrchestrator(sessions, registry)
    captured = []
    document_probe = SimpleNamespace(run=None, results=[])

    async def connector(*args, **kwargs):
        return DatarcTurnContext(datarc_absence_reason="no_connection")

    async def model(*args, **kwargs):
        return turn_service.TeamModelRuntimeResolution(router=object(), result="team_safe_success", reason="test_provider")

    async def worker(_orchestrator, req, **kwargs):
        # A fresh reader sees the canonical request before any external work.
        source = await SessionStore(path).get_chat_turn_user_source(turn_id=req.turn_id, user_id=req.user_id, workspace_id=req.workspace_id)
        assert source is not None and source.user_text == req.text
        assert source.attachment_ids == tuple(req.attachments)
        if document_probe.run is not None:
            document_probe.results.append(await document_probe.run(req, source))
        await kwargs["admitted_legacy_bus"].emit(OrchestratorEvent(event_type="final_answer", turn_id=req.turn_id, payload={"text": "ok"}))
        await sessions.mark_completed(turn_id=req.turn_id, intent="lookup", final_answer="ok", tokens_input=0, tokens_output=0, cost_usd=0.0)
        await kwargs["admitted_legacy_bus"].emit(OrchestratorEvent(event_type="done", turn_id=req.turn_id, payload={"status": "completed"}))
        await sessions.finalize_turn(session_id=req.session_id, current_turn_id=req.turn_id, next_turn_id=None)
        captured.append((req, source))

    monkeypatch.setattr(chat, "resolve_datarc_turn_context", connector)
    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", model)
    # DocumentService binds its own import; provider isolation must not depend on collection order.
    monkeypatch.setattr(document_service, "resolve_team_model_runtime", model)
    monkeypatch.setattr(chat, "_run_turn_and_cleanup", worker)

    def headers(*, user=alice, space=spaces[0]):
        token = jwt.encode({"sub": user.id, "team_id": team.id, "pwd_v": 0}, _SECRET, algorithm="HS256")
        return {"Authorization": "Bearer " + token, **({"X-Space-Id": space} if space else {})}

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            path=path,
            users=users,
            alice=alice,
            bob=bob,
            spaces=spaces,
            sessions=sessions,
            artifacts=artifacts,
            backend=backend,
            headers=headers,
            captured=captured,
            document_probe=document_probe,
        )
    pending = [task for task in chat._inflight_tasks if not task.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=3)
    for req, _ in captured:
        await registry.close(req.turn_id)


def _file(kind):
    if kind == "pptx":
        return "source.pptx", _office_bytes("pptx"), "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "source.json", b'{"revenue":100,"profit":15}', "application/json"


def _ref(data):
    return {"fileId": data["file_id"], **{key: data[key] for key in ("filename", "mime", "size")}}


async def _upload(case, transport, *, kind="json", session_id=None, headers=None, confirm=True, payload=None):
    filename, content, mime = payload or _file(kind)
    headers = case.headers() if headers is None else headers
    if transport == "local":
        case.backend.allow_local_receiver = False
        response = await case.client.post(
            "/api/v1/files/presign",
            headers=headers,
            json={"filename": filename, "mime": mime, "size_bytes": len(content), "session_id": session_id},
        )
        assert response.status_code == 409 and "file_multipart_required" in response.text, response.text
        async with open_shared_ctx(case.path) as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM artifacts WHERE status='pending_upload'")).fetchone())[0] == 0
    if transport in ("multipart", "local"):
        response = await case.client.post(
            "/api/v1/files/upload",
            headers=headers,
            params={"session_id": session_id} if session_id else {},
            files={"file": (filename, content, mime)},
        )
        assert response.status_code == 200, response.text
        return _ref(response.json())
    checksum = hashlib.sha256(content).hexdigest()
    response = await case.client.post(
        "/api/v1/files/presign",
        headers=headers,
        json={
            "filename": filename,
            "mime": mime,
            "size_bytes": len(content),
            "session_id": session_id,
            "checksum_sha256": checksum,
        },
    )
    assert response.status_code == 200, response.text
    presign = response.json()
    pending_ref = {"fileId": presign["file_id"], "filename": filename, "mime": mime, "size": len(content)}
    if not confirm:
        return pending_ref, presign, content
    transfer = await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
    assert transfer.status_code == 204, transfer.text
    response = await case.client.post(
        presign["confirm_url"],
        headers=headers,
        json={
            "session_id": session_id,
            "uploaded_size": len(content),
            "checksum_sha256": checksum,
        },
    )
    assert response.status_code == 200, response.text
    replay = await case.client.post(presign["confirm_url"], headers=headers, json={"session_id": session_id, "checksum_sha256": checksum})
    assert replay.status_code == 200 and replay.json()["idempotent_replay"], replay.text
    return _ref(response.json())


async def _send(case, refs, *, user=None, space=None, session_id=None, message="Use the attached files in the order selected."):
    return await case.client.post(
        "/api/v1/chat/send",
        headers=case.headers(user=user or case.alice, space=space or case.spaces[0]),
        json={
            "workspace_id": space or case.spaces[0],
            "session_id": session_id,
            "message": message,
            "attachments": list(dict.fromkeys(aid for ref in refs for aid in (ref.get("bundleArtifactIds") or [ref["fileId"]]))),
            "attachment_refs": refs,
        },
    )


async def _assert_accepted(case, response, refs):
    assert response.status_code == 200, response.text
    turn_id = response.json()["turn_id"]
    # /chat/send spawns the worker before it responds; wait for that exact task
    # instead of a wall-clock poll, which a loaded run can outlast.
    workers = [task for task in chat._inflight_tasks if task.get_name().endswith(f":{turn_id}")]
    if workers:
        await asyncio.wait(workers)
    captured = [pair for pair in case.captured if pair[0].turn_id == turn_id]
    assert len(captured) == 1, f"worker did not capture turn {turn_id}"
    req, source = captured[0]
    assert list(source.attachment_refs) == refs
    assert req.terminal_runtime is None
    reread = await case.sessions.get_chat_turn_user_source(turn_id=turn_id, user_id=case.alice.id, workspace_id=case.spaces[0])
    assert reread == source
    assert await SessionStore(case.path).list_turn_attachments(turn_id=turn_id) == list(source.attachment_ids)
    assert current_attachment_positions(source) == {
        aid: index for index, ref in enumerate(refs, 1) for aid in (ref.get("bundleArtifactIds") or [ref["fileId"]])
    }
    return source


@pytest.mark.parametrize("transport", ["multipart", "presign"])
@pytest.mark.parametrize("kind", ["json", "pptx"])
@pytest.mark.parametrize("scope", ["home", "existing", "session_only"])
async def test_real_upload_then_ordinary_send(case, transport, kind, scope):
    session_id = None
    if scope != "home":
        session_id = (await case.sessions.ensure_session(session_id=None, workspace_id=case.spaces[0], user_id=case.alice.id)).id
    ref = await _upload(case, transport, kind=kind, session_id=session_id, headers=case.headers(space=None) if scope == "session_only" else None)
    source = await _assert_accepted(case, await _send(case, [ref], session_id=session_id), [ref])
    async with open_shared_ctx(case.path) as db:
        row = await (
            await db.execute("SELECT workspace_id,user_id,session_id FROM artifact_upload_bindings WHERE artifact_id=?", (ref["fileId"],))
        ).fetchone()
    assert tuple(row) == (case.spaces[0], case.alice.id, session_id)
    # Rejection of a later stale request cannot replace the original source.
    stale = {**ref, "filename": "changed.json"}
    rejected = await _send(case, [stale])
    assert rejected.status_code == 409, rejected.text
    assert await case.sessions.get_chat_turn_user_source(turn_id=source.turn_id, user_id=case.alice.id, workspace_id=case.spaces[0]) == source


@pytest.mark.parametrize("transport", ["multipart", "presign"])
@pytest.mark.parametrize("invalid", ["owner", "space", "filename", "mime", "size", "deleted", "unscoped", "bundle"])
async def test_uploaded_file_cannot_cross_ordinary_send_fences(case, transport, invalid):
    ref = await _upload(case, transport, headers=case.headers(space=None) if invalid == "unscoped" else None)
    if invalid in {"filename", "mime", "size"}:
        ref[invalid] = ref[invalid] + 1 if invalid == "size" else "changed"
    if invalid == "deleted":
        response = await case.client.delete(f"/api/v1/files/{ref['fileId']}", headers=case.headers())
        assert response.status_code in (200, 204), response.text
    if invalid == "bundle":
        child = await _upload(case, transport)
        ref["bundleArtifactIds"] = [child["fileId"]]
    response = await _send(case, [ref], user=case.bob if invalid == "owner" else None, space=case.spaces[1] if invalid == "space" else None)
    assert response.status_code == 409, response.text
    assert not case.captured
    async with open_shared_ctx(case.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM turns")).fetchone())[0] == 0


async def test_pending_upload_is_not_sendable_and_retry_preserves_binding(case):
    ref, presign, content = await _upload(case, "presign", confirm=False)
    assert (await _send(case, [ref])).status_code == 409
    missing = await case.client.post(presign["confirm_url"], headers=case.headers(), json={})
    assert missing.status_code == 404
    transfer = await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
    assert transfer.status_code == 204
    confirmed = await case.client.post(presign["confirm_url"], headers=case.headers(), json={})
    assert confirmed.status_code == 200, confirmed.text
    await _assert_accepted(case, await _send(case, [_ref(confirmed.json())]), [_ref(confirmed.json())])


@pytest.mark.parametrize("change", ["owner", "space", "header", "session", "missing_store"])
async def test_confirm_cannot_rebind_upload_or_pin_into_unowned_session(case, change):
    original = await case.sessions.ensure_session(session_id=None, workspace_id=case.spaces[0], user_id=case.alice.id)
    ref, presign, content = await _upload(case, "presign", session_id=original.id if change == "session" else None, confirm=False)
    transfer = await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
    assert transfer.status_code == 204
    target = await case.sessions.ensure_session(
        session_id=None,
        workspace_id=case.spaces[1] if change == "space" else case.spaces[0],
        user_id=case.bob.id if change == "owner" else case.alice.id,
    )
    if change == "missing_store":
        case.app.state.session_store = None
    response = await case.client.post(
        presign["confirm_url"], headers=case.headers(space=case.spaces[1] if change == "header" else case.spaces[0]), json={"session_id": target.id}
    )
    assert response.status_code in (403, 409), response.text
    assert (await case.artifacts.get(ref["fileId"])).status == "pending_upload"
    async with open_shared_ctx(case.path) as db:
        pins = await (await db.execute("SELECT count(*) FROM session_attachments WHERE artifact_id=?", (ref["fileId"],))).fetchone()
        binding = await (
            await db.execute("SELECT workspace_id,session_id FROM artifact_upload_bindings WHERE artifact_id=?", (ref["fileId"],))
        ).fetchone()
    assert pins[0] == 0
    assert tuple(binding) == (case.spaces[0], original.id if change == "session" else None)


async def test_deletion_during_confirmation_never_returns_ready(case, monkeypatch):
    ref, presign, content = await _upload(case, "presign", confirm=False)
    await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
    original_head = case.backend.head

    async def delete_before_head(path):
        head = await original_head(path)
        response = await case.client.delete(f"/api/v1/files/{ref['fileId']}", headers=case.headers())
        assert response.status_code in (200, 204), response.text
        return head

    monkeypatch.setattr(case.backend, "head", delete_before_head)
    response = await case.client.post(presign["confirm_url"], headers=case.headers(), json={})
    assert response.status_code in (404, 409), response.text
    assert (await _send(case, [ref])).status_code == 409


@pytest.mark.parametrize("transport", ["multipart", "presign"])
async def test_upload_rechecks_current_space_membership(case, transport):
    await case.users.remove_member(case.spaces[0], case.alice.id)
    filename, content, mime = _file("json")
    response = await case.client.post(
        "/api/v1/files/upload" if transport == "multipart" else "/api/v1/files/presign",
        headers=case.headers(),
        **(
            {"files": {"file": (filename, content, mime)}}
            if transport == "multipart"
            else {"json": {"filename": filename, "mime": mime, "size_bytes": len(content)}}
        ),
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("transport", ["multipart", "presign"])
async def test_upload_rejects_someone_elses_session(case, transport):
    session = await case.sessions.ensure_session(session_id=None, workspace_id=case.spaces[0], user_id=case.bob.id)
    filename, content, mime = _file("json")
    if transport == "multipart":
        response = await case.client.post(
            "/api/v1/files/upload", headers=case.headers(), params={"session_id": session.id}, files={"file": (filename, content, mime)}
        )
    else:
        response = await case.client.post(
            "/api/v1/files/presign",
            headers=case.headers(),
            json={"filename": filename, "mime": mime, "size_bytes": len(content), "session_id": session.id},
        )
    assert response.status_code == 403, response.text


async def test_confirmation_rechecks_revoked_space(case):
    ref, presign, content = await _upload(case, "presign", confirm=False)
    await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
    await case.users.remove_member(case.spaces[0], case.alice.id)
    response = await case.client.post(presign["confirm_url"], headers=case.headers(), json={})
    assert response.status_code == 403, response.text
    assert (await case.artifacts.get(ref["fileId"])).status == "pending_upload"


async def test_multipart_binding_failure_cannot_publish_ready_artifact(case):
    async with open_shared_ctx(case.path) as db:
        await db.execute(
            "CREATE TRIGGER reject_test_binding BEFORE INSERT ON artifact_upload_bindings BEGIN SELECT RAISE(ABORT, 'test binding unavailable'); END"
        )
        await db.commit()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=case.app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/api/v1/files/upload", headers=case.headers(), files={"file": _file("json")})
    assert response.status_code == 500
    async with open_shared_ctx(case.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM artifacts")).fetchone())[0] == 0


@pytest.fixture
async def kb_case(case, tmp_path, initialize_migrated_kb_files_database):
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_gateway.routes import kb, kb_presign
    from test_kb_presign_bundle_phase14c import _FakeDocTypeStore, _FakeWorker

    kb_path = tmp_path / "kb_files.db"
    initialize_migrated_kb_files_database(kb_path)
    case.app.state.kb_files_store = KbFilesStore(kb_path)
    await case.app.state.kb_files_store.initialize()
    case.app.state.kb_worker = _FakeWorker()
    case.app.state.kb_doc_type_store = _FakeDocTypeStore()
    case.app.include_router(kb.router)
    case.app.include_router(kb_presign.router)
    return case


@pytest.mark.parametrize("transport", ["multipart", "presign", "local"])
async def test_real_bundle_upload_then_send_checks_exact_members(kb_case, transport):
    case = kb_case
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("first.txt", "First source" + ("x" * (11 * 1024 * 1024) if transport == "local" else ""))
        archive.writestr("second.txt", "Second source")
    content = data.getvalue()
    if transport == "local":
        case.backend.allow_local_receiver = False
        response = await case.client.post(
            "/api/v1/kb/presign/bundle",
            headers=case.headers(),
            json={"filename": "sources.zip", "mime": "application/zip", "size_bytes": len(content), "scope": "personal", "scope_id": case.alice.id},
        )
        assert response.status_code == 409 and "file_multipart_required" in response.text, response.text
    if transport in ("multipart", "local"):
        response = await case.client.post(
            "/api/v1/kb/files/bundle",
            headers=case.headers(),
            files={"file": ("sources.zip", content, "application/zip")},
            data={"scope": "personal", "scope_id": case.alice.id, "use_llm": "false"},
        )
    else:
        response = await case.client.post(
            "/api/v1/kb/presign/bundle",
            headers=case.headers(),
            json={
                "filename": "sources.zip",
                "mime": "application/zip",
                "size_bytes": len(content),
                "scope": "personal",
                "scope_id": case.alice.id,
                "use_llm": False,
            },
        )
        assert response.status_code == 200, response.text
        presign = response.json()
        transfer = await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
        assert transfer.status_code == 204, transfer.text
        response = await case.client.post(presign["confirm_url"], headers=case.headers(space=case.spaces[1]), json={"uploaded_size": len(content)})
    assert response.status_code == 200, response.text
    uploaded = response.json()
    assert len(uploaded["uploaded"]) == 2
    root = await case.artifacts.get(uploaded["bundle_artifact_id"])
    assert uploaded["bundle_mime"] == root.mime and uploaded["bundle_size"] == root.size
    ref = {
        "fileId": root.id,
        "filename": uploaded["bundle_filename"],
        "mime": uploaded["bundle_mime"],
        "size": uploaded["bundle_size"],
        "bundleArtifactIds": [item["artifact_id"] for item in uploaded["uploaded"]],
    }
    source = await _assert_accepted(case, await _send(case, [ref]), [ref])
    unrelated = await _upload(case, "multipart")
    invalid = {**ref, "bundleArtifactIds": [ref["bundleArtifactIds"][0], unrelated["fileId"]]}
    rejected = await _send(case, [invalid])
    assert rejected.status_code == 409 and "attachment_bundle_mismatch" in rejected.text
    assert await case.sessions.get_chat_turn_user_source(turn_id=source.turn_id, user_id=case.alice.id, workspace_id=case.spaces[0]) == source


async def test_mixed_transports_preserve_current_message_order(case):
    first = await _upload(case, "multipart", kind="json")
    second = await _upload(case, "presign", kind="pptx")
    await _assert_accepted(case, await _send(case, [second, first]), [second, first])


async def test_historical_unbound_file_is_not_backfilled_by_send(case):
    artifact = await case.artifacts.put_bytes(
        content=b"old", filename="old.txt", mime="text/plain", created_by=case.alice.id, workspace_id=case.spaces[0]
    )
    ref = {"fileId": artifact.id, "filename": artifact.filename, "size": artifact.size, "mime": artifact.mime}
    response = await _send(case, [ref])
    assert response.status_code == 409 and "attachment_unavailable" in response.text
    async with open_shared_ctx(case.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM artifact_upload_bindings WHERE artifact_id=?", (artifact.id,))).fetchone())[0] == 0


async def test_generic_confirmation_does_not_infer_space_from_later_session(case):
    ref, presign, content = await _upload(case, "presign", headers=case.headers(space=None), confirm=False)
    transfer = await case.client.post(presign["upload"]["url"], headers=presign["upload"]["fields"], content=content)
    assert transfer.status_code == 204
    session = await case.sessions.ensure_session(session_id=None, workspace_id=case.spaces[0], user_id=case.alice.id)
    response = await case.client.post(presign["confirm_url"], headers=case.headers(), json={"session_id": session.id})
    assert response.status_code == 200, response.text
    assert (await _send(case, [_ref(response.json())], session_id=session.id)).status_code == 409
    async with open_shared_ctx(case.path) as db:
        binding = await (await db.execute("SELECT workspace_id FROM artifact_upload_bindings WHERE artifact_id=?", (ref["fileId"],))).fetchone()
        assert binding[0] == ""


@pytest.mark.parametrize("kind", ["json", "pptx"])
async def test_long_multipart_filename_remains_exact_through_send(case, kind):
    _, content, mime = _file(kind)
    filename = "source_" + "x" * 220 + "." + kind
    response = await case.client.post("/api/v1/files/upload", headers=case.headers(), files={"file": (filename, content, mime)})
    assert response.status_code == 200, response.text
    ref = _ref(response.json())
    assert ref["filename"] == filename
    await _assert_accepted(case, await _send(case, [ref]), [ref])


@pytest.mark.parametrize("transport", ["multipart", "presign", "local"])
@pytest.mark.parametrize("existing_session", [False, True])
async def test_current_uploads_reach_native_document_admission(case, monkeypatch, transport, existing_session):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.documents.executor import PROFILE
    from bowhead_core.documents.request import DocumentScope
    from bowhead_core.documents.tool import register_document_tool
    from bowhead_core.subagent import _system_profile_snapshot
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
    from bowhead_gateway.document_service import DocumentService

    session_id = None
    if existing_session:
        session_id = (await case.sessions.ensure_session(session_id=None, workspace_id=case.spaces[0], user_id=case.alice.id)).id
    refs = []
    for kind in ("json", "pptx"):
        filename, content, mime = _file(kind)
        if transport == "local":
            if kind == "json":
                content = b'{"padding":"' + b"x" * (11 * 1024 * 1024) + b'"}'
            else:
                archive_bytes = io.BytesIO()
                with ZipFile(io.BytesIO(content)) as original, ZipFile(archive_bytes, "w") as archive:
                    for name in original.namelist():
                        part = original.read(name)
                        if name == "ppt/presentation.xml":
                            part += b"<!--" + b"x" * (11 * 1024 * 1024) + b"-->"
                        archive.writestr(name, part)
                content = archive_bytes.getvalue()
        refs.append(await _upload(case, transport, session_id=session_id, payload=(filename, content, mime)))
    message = "Use the first attachment as data and the second attachment as template. Generate a PowerPoint and PDF."
    service = DocumentService(case.app.state)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(case.path.parent))
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://executor")
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "test-only")
    # The readiness fixture below is the pre-continuation executor; same-Turn continuation has its own suites.
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    client_type = httpx2.AsyncClient

    def readiness(request):
        assert request.url.path == "/document-runs/capabilities"
        return httpx2.Response(200, json={"profile": PROFILE, "image_id": "sha256:" + "b" * 64})

    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: client_type(transport=httpx2.MockTransport(readiness), **kwargs))
    registry = ToolRegistry()
    register_document_tool(registry, service=lambda: service)

    async def probe(req, source):
        async def binding():
            profile = manifest_to_profile(AgentRegistry.bootstrap().get("lead"))
            admitted = await AgentExecutionBindingStore(case.path).admit_system_execution(
                team_id=case.alice.team_id,
                user_id=req.user_id,
                turn_id=req.turn_id,
                profile_name="lead",
                dispatch_key="document-admission:" + req.turn_id,
                definition=_system_profile_snapshot(profile),
                manifest_fingerprint="sha256:" + "a" * 64,
                override_version=None,
                system_required=True,
            )
            return admitted, True

        context = ToolExecutionContext(
            workspace_id=req.workspace_id,
            team_id=case.alice.team_id,
            user_id=req.user_id,
            member_uid="member",
            turn_id=req.turn_id,
            session_id=req.session_id,
            role="lead",
            datarc=None,
            document_admission_binding=binding,
        )
        arguments = {
            "action": "generate",
            "evidence": {"text": source.user_text},
            "inputs": [
                {"artifact_id": ref["fileId"], "role": role, "evidence": {"text": quote}}
                for ref, role, quote in zip(refs, ("data", "template"), ("first attachment as data", "second attachment as template"), strict=True)
            ],
            "requested_outputs": ["source", "pdf"],
        }
        return (await registry.execute("template_document_submit", arguments, context)).result

    case.document_probe.run = probe
    source = await _assert_accepted(case, await _send(case, refs, session_id=session_id, message=message), refs)
    result = case.document_probe.results[0]
    assert result["ok"] is True, result
    scope = DocumentScope(team_id=case.alice.team_id, workspace_id=case.spaces[0], user_id=case.alice.id)
    jobs = await service.jobs.list_for_session(scope=scope, session_id=source.session_id)
    assert len(jobs) == 1 and jobs[0].id == result["document_job_id"]
    assert jobs[0].status == "queued"
    assert [(item.artifact_id, item.current_message_position) for item in jobs[0].proposal.inputs] == [(refs[0]["fileId"], 1), (refs[1]["fileId"], 2)]
    assert await SessionStore(case.path).list_turn_attachments(turn_id=source.turn_id) == list(source.attachment_ids)
    async with open_shared_ctx(case.path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM document_dispatches WHERE job_id=?", (jobs[0].id,))).fetchone())[0] == 1


async def test_turn_attachment_write_failure_rolls_back_admission(case):
    ref = await _upload(case, "multipart")
    async with open_shared_ctx(case.path) as db:
        await db.execute(
            "CREATE TRIGGER reject_turn_attachment BEFORE INSERT ON turn_attachments BEGIN SELECT RAISE(ABORT, 'test association failure'); END"
        )
        await db.commit()
    response = await _send(case, [ref])
    assert response.status_code >= 400, response.text
    assert case.captured == []
    async with open_shared_ctx(case.path) as db:
        for table in ("turns", "chat_turn_user_sources", "turn_attachments"):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM sessions WHERE active_turn_id IS NOT NULL")).fetchone())[0] == 0


async def test_local_kb_upload_negotiation_preserves_library_scope(kb_case):
    case = kb_case
    case.backend.allow_local_receiver = False
    content = b"Source data " + b"x" * (11 * 1024 * 1024)
    response = await case.client.post(
        "/api/v1/kb/presign",
        headers=case.headers(),
        json={"filename": "source.txt", "mime": "text/plain", "size_bytes": len(content), "scope": "personal", "scope_id": case.alice.id},
    )
    assert response.status_code == 409 and "file_multipart_required" in response.text, response.text
    response = await case.client.post(
        "/api/v1/kb/files",
        headers=case.headers(),
        files={"file": ("source.txt", content, "text/plain")},
        data={"scope": "personal", "scope_id": case.alice.id, "use_llm": "false"},
    )
    assert response.status_code == 200, response.text
    uploaded = response.json()
    artifact = await case.artifacts.get(uploaded["artifact_id"])
    assert artifact.status == "ready" and artifact.created_by == case.alice.id
    assert artifact.size == len(content)
    assert uploaded["scope"] == "personal" and uploaded["scope_id"] == case.alice.id
    assert case.captured == []
