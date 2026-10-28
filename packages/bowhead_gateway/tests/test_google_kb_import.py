"""Google Drive → knowledge base import: caller's own grant, Office export, per-file outcomes."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest
from bowhead_gateway.routes import google_workspace as routes
from bowhead_gateway.routes import kb as kb_routes
from fastapi import BackgroundTasks, HTTPException
from test_google_workspace_routes import _DOMAIN, _authorize, _configure, _tokens, env, google  # noqa: F401 - fixtures

from bowhead_core import google_workspace

pytestmark = pytest.mark.anyio

_DOC = "1DocDocDocDocDocDocDocDocDoc"
_PDF = "1PdfPdfPdfPdfPdfPdfPdfPdfPdf"
_BIG = "1BigBigBigBigBigBigBigBigBig"
_GONE = "1GoneGoneGoneGoneGoneGoneGon"


class _Drive:
    def __init__(self, token_fake) -> None:
        self.token_fake = token_fake
        self.calls: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "oauth2.googleapis.com":
            return self.token_fake.handler(request)
        self.calls.append(request)
        path = request.url.path
        if path.endswith(f"/{_DOC}"):
            return httpx2.Response(200, json={"name": "季度报告", "mimeType": "application/vnd.google-apps.document"})
        if path.endswith(f"/{_DOC}/export"):
            return httpx2.Response(200, content=b"PK-docx")
        if path.endswith(f"/{_PDF}") and request.url.params.get("alt") == "media":
            return httpx2.Response(200, content=b"%PDF-1.7")
        if path.endswith(f"/{_PDF}"):
            return httpx2.Response(200, json={"name": "合同.pdf", "mimeType": "application/pdf", "size": "8"})
        if path.endswith(f"/{_BIG}"):
            return httpx2.Response(200, json={"name": "大表", "mimeType": "application/vnd.google-apps.spreadsheet"})
        if path.endswith(f"/{_BIG}/export"):
            return httpx2.Response(403, json={"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}})
        return httpx2.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


@pytest.fixture
async def ready(env, google, monkeypatch):  # noqa: F811
    drive = _Drive(google)
    google_workspace.set_http_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(drive.handler)))
    await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}"))
    await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)
    ingested: list[dict] = []

    async def authorize(_request, user, *, scope, scope_id):
        return SimpleNamespace(user_id=user.id, scope=scope, scope_id=scope_id)

    async def ingest(_request, _tasks, user, ctx, *, raw, filename, client_mime, scope, scope_id, use_llm=False, doc_type_id=None, route):
        ingested.append({"user": user.id, "raw": raw, "filename": filename, "mime": client_mime, "scope": scope, "route": route})
        return SimpleNamespace(file_id=f"kbf_{len(ingested)}", filename=filename)

    monkeypatch.setattr(kb_routes, "authorize_kb_scope_write", authorize)
    monkeypatch.setattr(kb_routes, "ingest_kb_file_bytes", ingest)
    return SimpleNamespace(env=env, drive=drive, google=google, ingested=ingested)


def _body(*file_ids: str) -> routes.KbImportBody:
    return routes.KbImportBody(file_ids=list(file_ids), scope="personal", scope_id="usr_x")


async def test_imports_picked_files_as_the_caller_with_office_export(ready) -> None:
    ready.google.token_responses.append((200, {"access_token": "admin-access", "expires_in": 3600}))
    result = await routes.kb_import(_body(_DOC, _PDF, _BIG, _GONE), ready.env.request, BackgroundTasks(), ready.env.admin)

    assert result["results"] == [
        {"file_id": _DOC, "name": "季度报告.docx", "ok": True, "kb_file_id": "kbf_1"},
        {"file_id": _PDF, "name": "合同.pdf", "ok": True, "kb_file_id": "kbf_2"},
        {"file_id": _BIG, "name": None, "ok": False, "error": "google_export_too_large"},
        {"file_id": _GONE, "name": None, "ok": False, "error": "google_file_not_found"},
    ]
    doc, pdf = ready.ingested
    assert doc["raw"] == b"PK-docx" and doc["mime"].endswith("wordprocessingml.document") and doc["route"] == "kb_google_import"
    assert pdf["raw"] == b"%PDF-1.7" and doc["user"] == ready.env.admin.id
    assert all(call.headers["Authorization"] == "Bearer admin-access" for call in ready.drive.calls)
    export = next(call for call in ready.drive.calls if call.url.path.endswith(f"/{_DOC}/export"))
    assert export.url.params["mimeType"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def test_member_without_own_grant_cannot_borrow_anyone_elses(ready) -> None:
    with pytest.raises(HTTPException) as exc:
        await routes.kb_import(_body(_DOC), ready.env.request, BackgroundTasks(), ready.env.member)
    assert exc.value.detail == {"code": "google_not_connected"}
    assert ready.ingested == [] and ready.drive.calls == []


async def test_rejects_malformed_file_ids(ready) -> None:
    with pytest.raises(HTTPException) as exc:
        await routes.kb_import(_body("../../etc"), ready.env.request, BackgroundTasks(), ready.env.admin)
    assert exc.value.detail == {"code": "google_file_reference_invalid"}


async def test_import_401_drops_cached_access_and_the_next_import_refreshes(ready) -> None:
    original = ready.drive.handler

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "oauth2.googleapis.com" and request.headers["Authorization"] == "Bearer stale-access":
            return httpx2.Response(401)
        return original(request)

    google_workspace.set_http_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    ready.google.token_responses.extend(
        [(200, {"access_token": "stale-access", "expires_in": 3600}), (200, {"access_token": "fresh-access", "expires_in": 3600})]
    )
    failed = await routes.kb_import(_body(_DOC), ready.env.request, BackgroundTasks(), ready.env.admin)
    assert failed["results"][0]["error"] == "google_reauth_required" and ready.ingested == []

    recovered = await routes.kb_import(_body(_DOC), ready.env.request, BackgroundTasks(), ready.env.admin)
    assert recovered["results"][0]["ok"] and len(ready.ingested) == 1
    assert all(call.headers["Authorization"] == "Bearer fresh-access" for call in ready.drive.calls)
