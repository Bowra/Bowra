"""Real upload -> stored/downloaded exact bytes, with feature/ACL boundaries.

Mutation checks remove multipart inspection and the presign Office fence.
Fixtures are minimal outer OPC containers, not rendered Office documents.
"""

from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZIP_DEFLATED, ZipFile

import aiosqlite
import anyio
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.documents import package_io as office
from bowhead_core.documents import upload_confirmation as confirmation
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import files, files_presign
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _office_bytes(kind: str, *, text: str = "") -> bytes:
    is_word = kind in ("docx", "dotx")
    main = "word/document.xml" if is_word else "ppt/presentation.xml"
    mime = office.OFFICE_MIMES["." + kind]
    main_xml = (
        f'<w:document xmlns:w="{office.WORD_NS}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
        if is_word
        else f'<p:presentation xmlns:p="{office.PPT_NS}"/>'
    )
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            f'<Types xmlns="{office.CT_NS}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/{main}" ContentType="{mime}.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{office.REL_NS}"><Relationship Id="rId1" Type="{office.OFFICE_REL}" Target="{main}"/></Relationships>',
        )
        archive.writestr(main, main_xml)
    return output.getvalue()


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    backend = LocalBackend(
        root_dir=tmp_path / "files",
        allow_local_receiver=True,
        gateway_base_url="http://testserver",
        local_presign_secret="local-test-key-with-at-least-32-characters",
    )
    store = ArtifactStore(backend=backend, db_path=tmp_path / "office.db")
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{store.db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    await store.initialize()
    sessions = SessionStore(store.db_path)
    await sessions.initialize()
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.include_router(files_presign.router)
    app.state.artifact_store = store
    app.state.artifact_backend = backend
    app.state.session_store = sessions
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u_owner")

    # Keep the production background path; mock only team/model resolution.
    app.state.user_store = SimpleNamespace(
        get_user=AsyncMock(return_value=SimpleNamespace(team_id="team_test")), rate_limit_check=AsyncMock(return_value=True)
    )
    monkeypatch.setattr("bowhead_gateway.routes._deps.resolve_team_router", AsyncMock(return_value=(None, None)))
    return TestClient(app), store, sessions, backend


async def _count(store) -> int:
    async with aiosqlite.connect(store.db_path) as db:
        return (await (await db.execute("SELECT count(*) FROM artifacts")).fetchone())[0]


@pytest.mark.parametrize("kind", ["docx", "pptx"])
async def test_multipart_office_upload_has_send_binding_and_immutable_read_receipt(ctx, kind):
    from bowhead_gateway.chat_attachment_access import validate_chat_attachment_access

    client, store, _, _ = ctx
    content = _office_bytes(kind)
    response = client.post(
        "/api/v1/files/upload",
        files={"file": (f"source.{kind}", content, office.OFFICE_MIMES["." + kind])},
        headers={"X-Space-Id": "ws_upload"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    artifact = await store.get(data["file_id"])
    refs = [{"fileId": artifact.id, "filename": artifact.filename, "mime": artifact.mime, "size": artifact.size, "referenceKind": "upload"}]
    await validate_chat_attachment_access(
        artifact_store=store, refs=refs, runtime_ids=[artifact.id], user_id="u_owner", workspace_id="ws_upload", session_id=None
    )
    assert (
        await confirmation.confirmed_content_hash(store, artifact, user_id="u_owner", workspace_id="ws_upload")
        == "sha256:" + hashlib.sha256(content).hexdigest()
    )
    receipt = await confirmation.get_confirmation(store.db_path, artifact.id)
    await store.backend.put(aid=artifact.id, content=b"replaced quarantine", filename=artifact.filename, mime=artifact.mime)
    assert await store.backend.read(receipt["candidate_path"]) == content
    assert await store.get_ready_uploads_for_chat([artifact.id], user_id="another-user", workspace_id="ws_upload") == {}
    assert await store.get_ready_uploads_for_chat([artifact.id], user_id="u_owner", workspace_id="another-space") == {}


@pytest.mark.parametrize("kind", ["docx", "dotx", "pptx", "potx"])
@pytest.mark.parametrize("mime", ["application/octet-stream", "application/zip", "exact"])
async def test_upload_download_preserves_exact_source_bytes(ctx, monkeypatch, kind, mime) -> None:
    client, store, _, _ = ctx
    content = _office_bytes(kind)
    declared = office.OFFICE_MIMES["." + kind] if mime == "exact" else mime
    response = client.post("/api/v1/files/upload", files={"file": (f"source.{kind}", content, declared)})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mime"] == office.OFFICE_MIMES["." + kind]
    assert result["size"] == len(content)
    artifact = await store.get(result["file_id"])
    assert artifact.status == "ready" and artifact.created_by == "u_owner"
    assert artifact.derived_from is None  # Upload is not a generated version.
    assert client.get(result["url"]).content == content
    assert client.get(result["url"], headers={"X-Bowhead-User-Id": "u_other"}).status_code == 404


@pytest.mark.parametrize(
    "filename,content,mime,code",
    [
        ("fake.pptx", b"<html>not Office</html>", office.OFFICE_MIMES[".pptx"], "file_office_invalid_package"),
        ("wrong.pptx", _office_bytes("docx"), "application/zip", "file_office_type_mismatch"),
        ("wrong.potx", _office_bytes("potx"), "image/png", "file_office_type_mismatch"),
    ],
    # Explicit IDs: zip bytes contain collection-time timestamps, so default IDs vary by second; inconsistent test names across xdist workers
    # cause 'Different tests were collected' for the entire run, and `make test` executes no tests.
    ids=["html-as-pptx", "docx-named-pptx", "potx-as-png"],
)
async def test_bad_source_never_reaches_ready_or_blob_storage(ctx, filename, content, mime, code) -> None:
    client, store, _, backend = ctx
    response = client.post("/api/v1/files/upload", files={"file": (filename, content, mime)})
    assert response.status_code == 415
    assert response.json()["detail"]["error"]["code"] == code
    assert await _count(store) == 0
    assert list(backend.root_dir.iterdir()) == []


async def test_office_limits_are_checked_before_storage(ctx, monkeypatch) -> None:
    client, store, _, _ = ctx
    monkeypatch.setattr(files, "MAX_OFFICE_BYTES", 10)
    response = client.post("/api/v1/files/upload", files={"file": ("x.pptx", _office_bytes("pptx"), "application/zip")})
    assert response.status_code == 413
    assert response.json()["detail"]["error"]["code"] == "file_office_package_limit"
    assert await _count(store) == 0


async def test_ordinary_reference_file_still_uploads_with_feature_on(ctx) -> None:
    client, _, _, _ = ctx
    response = client.post("/api/v1/files/upload", files={"file": ("notes.txt", b"reference", "text/plain")})
    assert response.status_code == 200


@pytest.mark.parametrize("kind", ["docx", "dotx", "pptx", "potx"])
async def test_office_upload_never_schedules_classification_or_auto_index(ctx, monkeypatch, kind) -> None:
    client, store, _, _ = ctx
    reader = AsyncMock(wraps=store.open_read)
    monkeypatch.setattr(store, "open_read", reader)
    response = client.post("/api/v1/files/upload", files={"file": (f"source.{kind}", _office_bytes(kind), "application/zip")})
    assert response.status_code == 200
    assert not getattr(client.app.state, "_classify_bg_tasks", set())
    client.app.state.user_store.get_user.assert_not_awaited()
    reader.assert_not_awaited()
    art = await store.get(response.json()["file_id"])
    assert art.category is None and art.retention_hint is None and art.inject == "full"
    # A queued callback admitted before the policy changed must also defer.
    await files._classify_uploaded_artifact(client.app, art)
    client.app.state.user_store.get_user.assert_not_awaited()
    reader.assert_not_awaited()


async def test_non_office_classification_and_explicit_office_content_still_work(ctx, monkeypatch) -> None:
    client, store, _, _ = ctx
    # Exercise the real background handler deterministically; no background
    # sleep/polling. Existing categories/retention are never cleared by policy.
    text = await store.put_bytes(content=b"short reference", filename="notes.txt", mime="text/plain", source="upload", created_by="u_owner")
    await files._classify_uploaded_artifact(client.app, text)
    assert (await store.get(text.id)).category == "other"
    assert client.app.state.user_store.get_user.await_count == 1
    client.app.state.user_store.get_user.reset_mock()
    source = await store.put_bytes(
        content=_office_bytes("docx", text="short Office reference"),
        filename="source.docx",
        mime=office.OFFICE_MIMES[".docx"],
        source="upload",
        created_by="u_owner",
    )
    await files._classify_uploaded_artifact(client.app, source)
    assert client.app.state.user_store.get_user.await_count == 0
    # Current authorized preview still uses the real Word extractor.
    content = client.get(f"/api/v1/files/{source.id}/preview")
    assert content.status_code == 200, content.text
    assert "short Office reference" in content.text


@pytest.mark.parametrize("kind", ["docx", "dotx", "pptx", "potx"])
async def test_presign_confirms_an_immutable_copy_of_each_office_format(ctx, kind) -> None:
    from bowhead_core.documents.upload_confirmation import get_confirmation

    client, store, _, backend = ctx
    content = _office_bytes(kind)
    filename = f"template.{kind}"
    response = client.post("/api/v1/files/presign", json={"filename": filename, "mime": "application/zip", "size_bytes": len(content)})
    assert response.status_code == 200, response.text
    aid = response.json()["file_id"]
    receipt = await get_confirmation(store.db_path, aid)
    assert receipt["phase"] == "pending"
    quarantine = await backend.put(aid=aid, filename=filename, content=content, mime=office.OFFICE_MIMES["." + kind])
    confirmed = client.post(response.json()["confirm_url"], json={})
    assert confirmed.status_code == 200, confirmed.text
    artifact = await store.get(aid)
    assert artifact.path != quarantine
    assert client.get(confirmed.json()["url"]).content == content
    await backend.put(aid=aid, filename=filename, content=b"late browser overwrite", mime=artifact.mime)
    assert client.get(confirmed.json()["url"]).content == content
    replay = client.post(response.json()["confirm_url"], json={})
    assert replay.status_code == 200 and replay.json()["idempotent_replay"]
    assert await _count(store) == 1


async def test_oversized_presign_does_not_instruct_an_impossible_fallback(ctx) -> None:
    client, store, _, _ = ctx
    response = client.post(
        "/api/v1/files/presign", json={"filename": "big.pptx", "mime": "application/zip", "size_bytes": office.MAX_OFFICE_BYTES + 1}
    )
    assert response.status_code == 413
    assert await _count(store) == 0


@pytest.mark.parametrize("mime", [office.OFFICE_MIMES[".pptx"], "application/zip"])
async def test_presigned_envelope_cannot_skip_byte_validation(ctx, monkeypatch, mime) -> None:
    client, store, _, backend = ctx
    untrusted = b"this is not an Office document"
    response = client.post("/api/v1/files/presign", json={"filename": "old.pptx", "mime": mime, "size_bytes": len(untrusted)})
    assert response.status_code == 200, response.text
    await backend.put(aid=response.json()["file_id"], filename="old.pptx", content=untrusted, mime=office.OFFICE_MIMES[".pptx"])
    confirmed = client.post(response.json()["confirm_url"], json={})
    assert confirmed.status_code == 415
    assert confirmed.json()["detail"]["error"]["code"] == "file_office_invalid_package"
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT status FROM artifacts WHERE id=?", (response.json()["file_id"],))).fetchone()
    assert row[0] == "confirming"  # Rejected candidate is never a ready file.


async def test_session_ownership_is_preserved_for_multipart_fallback(ctx) -> None:
    client, store, sessions, _ = ctx
    own = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_owner")
    foreign = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_other")
    content = _office_bytes("pptx")
    denied = client.post(f"/api/v1/files/upload?session_id={foreign.id}", files={"file": ("x.pptx", content, "application/zip")})
    assert denied.status_code == 403 and await _count(store) == 0
    accepted = client.post(f"/api/v1/files/upload?session_id={own.id}", files={"file": ("x.pptx", content, "application/zip")})
    assert accepted.status_code == 200, accepted.text
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT session_id FROM session_attachments WHERE artifact_id=?", (accepted.json()["file_id"],))).fetchone()
    assert row[0] == own.id


async def _pending(ctx, *, content: bytes | None = None, session_id: str | None = None, checksum: str | None = None):
    client, store, _, backend = ctx
    data = content if content is not None else _office_bytes("pptx")
    response = client.post(
        "/api/v1/files/presign",
        json={
            "filename": "source.pptx",
            "mime": "application/zip",
            "size_bytes": len(data),
            "session_id": session_id,
            "checksum_sha256": checksum,
        },
    )
    assert response.status_code == 200, response.text
    aid = response.json()["file_id"]
    await backend.put(aid=aid, filename="source.pptx", content=data, mime=office.OFFICE_MIMES[".pptx"])
    return response.json(), await confirmation.get_confirmation(store.db_path, aid), data


async def _allow() -> None:
    pass


async def _confirm_core(store, aid: str, authorize=_allow):
    return await confirmation.confirm_upload(
        store,
        artifact_id=aid,
        user_id="u_owner",
        session_id=None,
        checksum_sha256=None,
        authorize=authorize,
    )


async def test_lost_copy_ack_recovers_same_candidate_after_source_overwrite(ctx, monkeypatch) -> None:
    client, store, _, backend = ctx
    envelope, row, content = await _pending(ctx)
    real_put = backend.put_immutable_upload

    async def lost_ack(path, **kwargs):
        await real_put(path, **kwargs)
        raise OSError("simulated lost write acknowledgement")

    monkeypatch.setattr(backend, "put_immutable_upload", lost_ack)
    assert client.post(envelope["confirm_url"], json={}).status_code == 503
    monkeypatch.setattr(backend, "put_immutable_upload", real_put)
    await backend.put(aid=row["artifact_id"], filename="source.pptx", content=b"changed source", mime=row["mime"])
    recovered = client.post(envelope["confirm_url"], json={})
    assert recovered.status_code == 200, recovered.text
    assert client.get(recovered.json()["url"]).content == content
    receipt = await confirmation.get_confirmation(store.db_path, row["artifact_id"])
    assert receipt["candidate_sha256"] == hashlib.sha256(content).hexdigest()
    assert receipt["check_profile"] == confirmation.CHECK_PROFILE
    assert receipt["epoch"] == 2


@pytest.mark.parametrize("candidate_action", ["keep", "remove", "corrupt"])
async def test_validated_checkpoint_never_recopies_mutable_source(ctx, candidate_action, monkeypatch) -> None:
    client, store, _, backend = ctx
    envelope, row, content = await _pending(ctx)
    calls = 0

    async def crash_before_publish():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated process fault before publication")

    with pytest.raises(OSError):
        await _confirm_core(store, row["artifact_id"], crash_before_publish)
    receipt = await confirmation.get_confirmation(store.db_path, row["artifact_id"])
    assert receipt["phase"] == "validating"
    await backend.put(aid=row["artifact_id"], filename="source.pptx", content=b"different source", mime=row["mime"])
    if candidate_action == "remove":
        await backend.delete(row["candidate_path"])
    elif candidate_action == "corrupt":
        # Storage fault, not a supported application writer.
        Path(row["candidate_path"]).write_bytes(b"corrupted candidate")
    reads = []
    real_read = backend.read_bounded

    async def tracked_read(path, **kwargs):
        reads.append(path)
        return await real_read(path, **kwargs)

    monkeypatch.setattr(backend, "read_bounded", tracked_read)
    response = client.post(envelope["confirm_url"], json={})
    assert response.status_code == (200 if candidate_action == "keep" else 503), response.text
    assert row["quarantine_path"] not in reads
    if candidate_action == "keep":
        assert client.get(response.json()["url"]).content == content
    else:
        artifact = await store.get(row["artifact_id"])
        assert artifact.status == "confirming" and artifact.path == row["quarantine_path"]


async def test_concurrent_confirms_and_expired_worker_cannot_change_winner(ctx, monkeypatch) -> None:
    _, store, _, backend = ctx
    _, row, content = await _pending(ctx)
    copy_started, release_copy = anyio.Event(), anyio.Event()
    real_put = backend.put_immutable_upload
    calls = 0
    outcomes = []

    async def blocked_first_copy(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            copy_started.set()
            await release_copy.wait()
        return await real_put(path, **kwargs)

    async def first_worker():
        try:
            await _confirm_core(store, row["artifact_id"])
        except confirmation.UploadConfirmationError as exc:
            outcomes.append(exc.code)

    monkeypatch.setattr(backend, "put_immutable_upload", blocked_first_copy)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first_worker)
        await copy_started.wait()
        with pytest.raises(confirmation.UploadConfirmationError, match="file_confirm_in_progress"):
            await _confirm_core(store, row["artifact_id"])
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE artifact_upload_confirmations SET lease_until=0 WHERE artifact_id=?", (row["artifact_id"],))
            await db.commit()
        winner, replay = await _confirm_core(store, row["artifact_id"])
        assert winner["epoch"] == 2 and not replay
        release_copy.set()
    assert outcomes == ["file_confirm_in_progress"]
    artifact = await store.get(row["artifact_id"])
    assert artifact.status == "ready" and await backend.read(artifact.path) == content
    final = await confirmation.get_confirmation(store.db_path, row["artifact_id"])
    assert final["phase"] == "ready" and final["epoch"] == 2


@pytest.mark.parametrize("fault", ["size", "sha", "actual_limit", "bad_package"])
async def test_confirmation_uses_candidate_bytes_not_head_or_client_claim(ctx, monkeypatch, fault) -> None:
    client, store, _, backend = ctx
    envelope, row, _ = await _pending(ctx, checksum="0" * 64 if fault == "sha" else None)
    if fault == "actual_limit":
        monkeypatch.setattr(confirmation, "MAX_OFFICE_BYTES", 10)
    elif fault == "size":
        await backend.put(aid=row["artifact_id"], filename="source.pptx", content=_office_bytes("pptx") + b"extra", mime=row["mime"])
    elif fault == "bad_package":
        await backend.put(aid=row["artifact_id"], filename="source.pptx", content=b"not Office", mime=row["mime"])
    response = client.post(envelope["confirm_url"], json={})
    assert response.status_code == (413 if fault == "actual_limit" else 415), response.text
    receipt = await confirmation.get_confirmation(store.db_path, row["artifact_id"])
    assert receipt["phase"] == "rejected"
    assert (await store.get(row["artifact_id"])).status != "ready"


async def test_missing_upload_is_retryable_without_losing_confirmation_identity(ctx) -> None:
    client, store, _, backend = ctx
    envelope, row, content = await _pending(ctx)
    await backend.delete(row["quarantine_path"])
    response = client.post(envelope["confirm_url"], json={})
    assert response.status_code == 404
    await backend.put(aid=row["artifact_id"], filename="source.pptx", content=content, mime=row["mime"])
    response = client.post(envelope["confirm_url"], json={})
    assert response.status_code == 200, response.text
    assert (await store.get(row["artifact_id"])).path == row["candidate_path"]


async def test_admitted_confirmation_keeps_scope_guards(ctx, monkeypatch) -> None:
    client, store, sessions, _ = ctx
    own = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_owner")
    foreign = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_other")
    data = _office_bytes("pptx")
    envelope, row, content = await _pending(ctx, content=data, session_id=own.id, checksum=hashlib.sha256(data).hexdigest())
    denied = client.post(envelope["confirm_url"], json={}, headers={"X-Bowhead-User-Id": "u_other"})
    assert denied.status_code == 404
    assert client.post(envelope["confirm_url"], json={"session_id": foreign.id}).status_code == 409
    assert client.post(envelope["confirm_url"], json={}).status_code == 200
    assert (await store.get(row["artifact_id"])).path == row["candidate_path"]
    assert client.get(f"/api/v1/files/{row['artifact_id']}").content == content
    async with aiosqlite.connect(store.db_path) as db:
        assert await (
            await db.execute("SELECT 1 FROM session_attachments WHERE session_id=? AND artifact_id=?", (own.id, row["artifact_id"]))
        ).fetchone()


async def test_confirm_cannot_attach_to_a_foreign_session_omitted_at_presign(ctx) -> None:
    client, _, sessions, _ = ctx
    foreign = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_other")
    envelope, _, _ = await _pending(ctx)
    assert client.post(envelope["confirm_url"], json={"session_id": foreign.id}).status_code == 403


async def test_session_pin_failure_rolls_back_ready_and_recovers(ctx) -> None:
    client, store, sessions, _ = ctx
    own = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_owner")
    envelope, row, _ = await _pending(ctx, session_id=own.id)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("CREATE TRIGGER fail_pin BEFORE INSERT ON session_attachments BEGIN SELECT RAISE(ABORT, 'simulated pin failure'); END")
        await db.commit()
    assert client.post(envelope["confirm_url"], json={}).status_code == 503
    assert (await store.get(row["artifact_id"])).status == "confirming"
    assert (await confirmation.get_confirmation(store.db_path, row["artifact_id"]))["phase"] == "validating"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP TRIGGER fail_pin")
        await db.commit()
    assert client.post(envelope["confirm_url"], json={}).status_code == 200


@pytest.mark.parametrize("ready", [True, False])
async def test_cleanup_retries_exact_keys_and_retains_published_candidate(ctx, monkeypatch, ready) -> None:
    from datetime import UTC, datetime

    from bowhead_gateway.presign_sweeper import _sweep_once

    client, store, _, backend = ctx
    envelope, row, content = await _pending(ctx)
    if ready:
        assert client.post(envelope["confirm_url"], json={}).status_code == 200
    future = row["upload_expires_at"] + confirmation.RECOVERY_SECONDS + 1
    real_delete = backend.delete
    failures = 0

    async def fail_once(path):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise OSError("simulated delete failure")
        await real_delete(path)

    monkeypatch.setattr(backend, "delete", fail_once)
    assert await confirmation.cleanup_confirmations(store, now=future) == 0
    await _sweep_once(artifact_store=store, kb_files_store=None, now_iso=datetime.fromtimestamp(future, tz=UTC).isoformat())
    assert not Path(row["quarantine_path"]).exists()
    assert Path(row["candidate_path"]).exists() == ready
    if ready:
        assert client.get(f"/api/v1/files/{row['artifact_id']}").content == content
    else:
        assert client.post(envelope["confirm_url"], json={}).status_code == 404
    # A browser request finishing late cannot leave an unreachable orphan.
    await backend.put(aid=row["artifact_id"], filename="source.pptx", content=b"late bytes", mime=row["mime"])
    assert await confirmation.cleanup_confirmations(store, now=future + confirmation.RECOVERY_SECONDS + 1) == 1
    assert not Path(row["quarantine_path"]).exists()


async def test_cleanup_respects_live_claim_and_legacy_sweeper_excludes_new_upload(ctx) -> None:
    from datetime import UTC, datetime

    from bowhead_gateway.presign_sweeper import _sweep_once

    _, store, _, _ = ctx
    _, row, _ = await _pending(ctx)
    await _sweep_once(artifact_store=store, kb_files_store=None, now_iso=datetime.fromtimestamp(row["upload_expires_at"] + 1, tz=UTC).isoformat())
    assert (await store.get(row["artifact_id"])).status == "pending_upload"
    future = row["upload_expires_at"] + confirmation.RECOVERY_SECONDS + 1
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE artifact_upload_confirmations SET phase='copying', lease_owner='live', lease_until=? WHERE artifact_id=?",
            (future + 120, row["artifact_id"]),
        )
        await db.commit()
    assert await confirmation.cleanup_confirmations(store, now=future) == 0
    assert Path(row["quarantine_path"]).exists()


async def test_unmigrated_store_fails_closed_when_office_feature_is_enabled(ctx) -> None:
    client, store, _, _ = ctx
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP TABLE artifact_upload_confirmations")
        await db.commit()
    response = client.post("/api/v1/files/presign", json={"filename": "x.pptx", "mime": "application/zip", "size_bytes": 100})
    assert response.status_code == 503 and await _count(store) == 0


async def test_reclaimed_lease_fences_stale_publisher_before_any_ready_file(ctx) -> None:
    _, store, _, _ = ctx
    _, row, _ = await _pending(ctx)
    calls = 0

    async def reclaimed_before_publish():
        nonlocal calls
        calls += 1
        if calls == 2:
            async with aiosqlite.connect(store.db_path) as db:
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET epoch=epoch+1, lease_owner='recovery-worker', lease_until=? WHERE artifact_id=?",
                    (time.time() + 120, row["artifact_id"]),
                )
                await db.commit()

    with pytest.raises(confirmation.UploadConfirmationError, match="file_confirm_in_progress"):
        await _confirm_core(store, row["artifact_id"], reclaimed_before_publish)
    assert (await store.get(row["artifact_id"])).status == "confirming"
    receipt = await confirmation.get_confirmation(store.db_path, row["artifact_id"])
    assert receipt["phase"] == "validating" and receipt["lease_owner"] == "recovery-worker"


async def test_deleted_session_cannot_be_pinned_between_authorization_and_publish(ctx) -> None:
    _, store, sessions, _ = ctx
    own = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_owner")
    _, row, _ = await _pending(ctx, session_id=own.id)
    calls = 0

    async def delete_after_authorization():
        nonlocal calls
        calls += 1
        if calls == 2:
            async with aiosqlite.connect(store.db_path) as db:
                await db.execute("UPDATE sessions SET deleted_at='2026-09-05' WHERE id=?", (own.id,))
                await db.commit()

    with pytest.raises(confirmation.UploadConfirmationError, match="file_upload_session_denied"):
        await confirmation.confirm_upload(
            store, artifact_id=row["artifact_id"], user_id="u_owner", session_id=own.id, checksum_sha256=None, authorize=delete_after_authorization
        )
    assert (await store.get(row["artifact_id"])).status == "confirming"


async def test_successful_put_with_wrong_readback_cannot_be_retried_into_ready(ctx, monkeypatch) -> None:
    client, store, _, backend = ctx
    output = io.BytesIO(_office_bytes("pptx"))
    with ZipFile(output, "a") as archive:
        archive.comment = b"A"
    envelope, row, content = await _pending(ctx, content=output.getvalue())
    changed = content[:-1] + b"B"
    assert office.inspect_office_package(changed, filename="source.pptx", declared_mime=row["mime"]).size_bytes == len(content)
    real_put = backend.put_immutable_upload

    async def wrong_bytes(path, **kwargs):
        return await real_put(path, content=changed, mime=kwargs["mime"])

    monkeypatch.setattr(backend, "put_immutable_upload", wrong_bytes)
    assert client.post(envelope["confirm_url"], json={}).status_code == 415
    monkeypatch.setattr(backend, "put_immutable_upload", real_put)
    assert client.post(envelope["confirm_url"], json={}).status_code == 415
    assert (await store.get(row["artifact_id"])).status == "confirming"


# ───────────────────── Attribution metrics (`files_upload` wiring point) ─────
#
# `files_upload` reason uses the **actual persisted value** of `art.workspace_id` (not request intent);
# each branch also asserts the persisted value, tying reason to result.
# Drive the real route: `with client:` + `attribution_metrics.drain(client)` deterministically await background-task writes.

_METRICS_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"
_DOCX = ("s.docx", _office_bytes("docx"), office.OFFICE_MIMES[".docx"])


async def _persisted_workspace(store, artifact_id: str) -> str | None:
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT workspace_id FROM artifacts WHERE id=?", (artifact_id,))).fetchone()
    assert row is not None, "产物没落进 artifacts —— 这条用例证明不了任何事"
    return row[0]


@pytest.mark.parametrize(
    ("leg", "expected_reason", "expected_persisted"),
    [
        # ★ All three frontend callers (file library / settings template / chat attachment) omit session; this was C4's sole NULL on 116.
        ("no_session", "none", None),
        ("session_with_space", "session", "ws_from_session"),
        # ★ B review: session supplied but attribution unresolved (empty session space)⇒persist NULL⇒must record none.
        # Intent-based reason classification such as 'session_id present means session' previously survived the entire suite.
        ("session_with_empty_workspace", "none", None),
        # ★ B review: `/upload` **does not accept** the space header (caller omits `workspace_id`); even with the same space header as session,
        # attribution still comes from the session ladder and must record `session`; wrongly passing the header as explicit space to reason survived the suite.
        ("session_with_matching_space_header", "session", "ws_from_session"),
    ],
)
async def test_files_upload_metric_reason_follows_the_persisted_value(
    ctx, attribution_metrics, monkeypatch, leg: str, expected_reason: str, expected_persisted: str | None
) -> None:
    client, store, sessions, _ = ctx
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(client.app)

    url = "/api/v1/files/upload"
    headers: dict[str, str] = {}
    if leg in ("session_with_space", "session_with_matching_space_header"):
        url += f"?session_id={(await sessions.ensure_session(session_id=None, workspace_id='ws_from_session', user_id='u_owner')).id}"
    elif leg == "session_with_empty_workspace":
        url += f"?session_id={(await sessions.ensure_session(session_id=None, workspace_id='', user_id='u_owner')).id}"
    if leg == "session_with_matching_space_header":
        headers["X-Space-Id"] = "ws_from_session"

    with client:
        response = client.post(url, files={"file": _DOCX}, headers=headers)
        assert response.status_code == 200, response.text
        attribution_metrics.drain(client)

    assert await _persisted_workspace(store, response.json()["file_id"]) == expected_persisted, f"前置不成立：{leg} 腿的落值不是预期"
    assert attribution_metrics.events() == [{"route": "files_upload", "reason": expected_reason, "source": "upload"}]
    assert attribution_metrics.user_ids() == [None]


@pytest.mark.parametrize(
    ("query", "header", "expected_source"),
    [
        ("", None, "upload"),  # Default.
        ("?source=python_sandbox", None, "python_sandbox"),  # ★ Sandbox runner POSTs every output file here.
        ("?source=sandbox_output", None, "sandbox_output"),
        ("", "canvas_export", "canvas_export"),
        # ★ Bounded cardinality: sources outside the allowlist fall back to "upload"; arbitrary source headers cannot blow up rollup groups.
        ("?source=totally-made-up", None, "upload"),
        ("", "'; DROP TABLE artifacts; --", "upload"),
        # ★ B review: resolve source by taking the **first valid** value from header then query; an invalid header **must not** short-circuit a valid query,
        # and a valid header takes precedence. Previously only an unrelated test file **rewrote** the algorithm instead of calling the route, masking errors.
        ("?source=python_sandbox", "bogus-header", "python_sandbox"),
        ("?source=python_sandbox", "canvas_export", "canvas_export"),
    ],
)
async def test_files_upload_metric_source_is_bounded_by_the_upload_allowlist(
    ctx, attribution_metrics, monkeypatch, query: str, header: str | None, expected_source: str
) -> None:
    """★ `source` 必须进 attrs：沙箱 runner 会把**每个输出文件**都 POST 到 `/files/upload`，
    不带它，`reason=none` 的占比会被机器产出的量淹没，读不出「用户自己上传」的真实比例。
    而它取值受 `_ALLOWED_UPLOAD_SOURCES` 白名单约束（4 个），所以是安全的低基数键。
    """
    client, _store, _, _ = ctx
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(client.app)

    with client:
        response = client.post(
            f"/api/v1/files/upload{query}",
            files={"file": _DOCX},
            headers={"X-Bowhead-Upload-Source": header} if header else {},
        )
        assert response.status_code == 200, response.text
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "files_upload", "reason": "none", "source": expected_source}]


async def test_files_upload_metric_rejected_requests_are_not_counted(ctx, attribution_metrics, monkeypatch) -> None:
    """★ 被拒的请求（415 非法 Office 包 / 403 别人的 session）不计数。每个请求先断言状态码，防空过。"""
    client, _store, sessions, _ = ctx
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(client.app)
    foreign = await sessions.ensure_session(session_id=None, workspace_id="", user_id="u_other")

    with client:
        invalid = client.post(
            "/api/v1/files/upload",
            files={"file": ("fake.pptx", b"<html>not Office</html>", office.OFFICE_MIMES[".pptx"])},
        )
        assert invalid.status_code == 415, invalid.text
        denied = client.post(f"/api/v1/files/upload?session_id={foreign.id}", files={"file": _DOCX})
        assert denied.status_code == 403, denied.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


async def test_files_upload_metric_flag_off_control_writes_nothing(ctx, attribution_metrics) -> None:
    """★ 对照：同一请求，flag 关着必须 0 行（drain 之后再读）。"""
    client, _store, _, _ = ctx
    attribution_metrics.attach(client.app)
    with client:
        assert client.post("/api/v1/files/upload", files={"file": _DOCX}).status_code == 200
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []
