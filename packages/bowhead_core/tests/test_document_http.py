"""Authenticated product routes with real identity, ACL, SQL and private blobs."""

import time
from types import SimpleNamespace

import httpx2
import jwt
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.user_store import UserStore
from bowhead_gateway.document_service import DocumentService
from bowhead_gateway.routes import documents, files
from fastapi import FastAPI
from test_document_delivery import _review
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
SECRET = "document-http-test-secret-32-characters"


def headers(user=SCOPE.user_id):
    return {
        "Authorization": "Bearer " + jwt.encode({"sub": user, "team_id": SCOPE.team_id, "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    }


@pytest.fixture
async def http_case(ctx, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    delivery, job_id, draft, result = await _review(ctx)
    app = FastAPI()
    app.state.settings = SimpleNamespace(auth_mode="prod")
    app.state.user_store = UserStore(ctx.path)
    app.state.session_store = ctx.sessions
    app.state.artifact_store = ctx.artifacts
    app.state.document_service = DocumentService(app.state)
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(files.router, prefix="/api/v1/files")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        yield client, job_id, draft, result


async def test_preview_confirm_refresh_and_both_exact_downloads(http_case):
    client, job_id, draft, result = http_case
    base = f"/api/v1/documents/{job_id}"
    assert (await client.get(base + "/preview/page-1.png")).status_code == 401
    assert (await client.get(base + "/download/candidate.pptx", headers=headers())).status_code == 404
    preview = await client.get(base + "/preview/page-1.png", headers=headers())
    assert preview.status_code == 200 and preview.content == result.files["page-1.png"]
    assert preview.headers["cache-control"] == "no-store, private"
    published = await client.post(base + "/confirm", headers=headers(), json={"command_id": "click", "draft_digest": draft["draft_digest"]})
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    fresh = await client.get("/api/v1/sessions/s_doc/documents", headers=headers())
    assert fresh.status_code == 200 and fresh.json()["jobs"][0]["draft_digest"] == draft["draft_digest"]
    exact = await client.get(f"/api/v1/sessions/s_doc/documents/{job_id}", headers=headers())
    assert exact.status_code == 200 and exact.json() == fresh.json()["jobs"][0]
    for name in ("candidate.pptx", "candidate.pdf"):
        downloaded = await client.get(base + "/download/" + name, headers=headers())
        assert downloaded.status_code == 200 and downloaded.content == result.files[name]


async def test_exact_draft_reader_matches_list_without_publishing(http_case, ctx):
    client, job_id, _, _ = http_case
    response = await client.get(f"/api/v1/sessions/s_doc/documents/{job_id}", headers=headers())
    listed = await client.get("/api/v1/sessions/s_doc/documents", headers=headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.json() == listed.json()["jobs"][0]
    assert response.json()["state"] == "review"
    assert response.json()["can_confirm"] is True
    assert (await client.get(f"/api/v1/documents/{job_id}/download/candidate.pptx", headers=headers())).status_code == 404
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT COUNT(*) FROM document_confirmations WHERE job_id=?", (job_id,)) as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_superseded_review_remains_private_read_only_after_successor_failure(http_case, ctx, monkeypatch):
    from bowhead_core.documents.delivery import DocumentDeliveryStore
    from test_document_revision import admit_source
    from test_document_revision_validation import revision_source

    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "1")
    client, parent, draft, result = http_case
    arguments = await admit_source(ctx, revision_source(ctx, parent, draft["draft_digest"]))
    successor = await ctx.store.submit_revision_from_turn(**arguments)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    owner = await delivery.claim(scope=SCOPE, job_id=successor.job_id)
    assert owner is not None
    await delivery.transition(scope=SCOPE, job_id=successor.job_id, owner=owner, state="failed", error_code="document_render_failed")
    exact = await client.get(f"/api/v1/sessions/s_doc/documents/{parent}", headers=headers())
    listed = await client.get("/api/v1/sessions/s_doc/documents", headers=headers())
    assert exact.status_code == 200
    assert exact.json() == next(job for job in listed.json()["jobs"] if job["id"] == parent)
    assert exact.json()["superseded_by_job_id"] == successor.job_id
    assert exact.json()["draft"] == draft["draft"]
    assert exact.json()["can_confirm"] is False and exact.json()["can_revise"] is False
    base = f"/api/v1/documents/{parent}"
    preview = await client.get(base + "/preview/page-1.png", headers=headers())
    assert preview.status_code == 200 and preview.content == result.files["page-1.png"]
    assert preview.headers["cache-control"] == "no-store, private"
    assert (await client.get(base + "/preview/page-1.png", headers=headers("u_other"))).status_code == 404
    for name in ("candidate.pptx", "candidate.pdf"):
        assert (await client.get(base + "/download/" + name, headers=headers())).status_code == 404
    response = await client.post(base + "/confirm", headers=headers(), json={"command_id": "old-confirm", "draft_digest": draft["draft_digest"]})
    assert response.status_code == 409


async def test_exact_draft_reader_rejects_unknown_wrong_session_and_other_owner(http_case):
    client, job_id, _, _ = http_case
    url = f"/api/v1/sessions/s_doc/documents/{job_id}"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=headers("u_other"))).status_code == 404
    assert (await client.get("/api/v1/sessions/s_doc/documents/djob_missing", headers=headers())).status_code == 404
    assert (await client.get(f"/api/v1/sessions/s_other/documents/{job_id}", headers=headers())).status_code == 404


async def test_exact_draft_reader_respects_read_only_and_revoked_access(http_case, ctx):
    client, job_id, _, _ = http_case
    url = f"/api/v1/sessions/s_doc/documents/{job_id}"
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        await db.commit()
    response = await client.get(url, headers=headers())
    assert response.status_code == 200
    assert response.json()["can_confirm"] is False
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        await db.commit()
    assert (await client.get(url, headers=headers())).status_code == 403


async def test_other_owner_and_revoked_membership_cannot_preview_or_publish(http_case, ctx):
    client, job_id, draft, _ = http_case
    base = f"/api/v1/documents/{job_id}"
    assert (await client.get(base + "/preview/page-1.png", headers=headers("u_other"))).status_code == 404
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        await db.commit()
    assert (await client.get(base + "/preview/page-1.png", headers=headers())).status_code == 403
    response = await client.post(base + "/confirm", headers=headers(), json={"command_id": "click", "draft_digest": draft["draft_digest"]})
    assert response.status_code == 403


async def test_pdf_download_requires_current_parent_even_through_generic_files(http_case, ctx):
    client, job_id, draft, result = http_case
    await client.post(f"/api/v1/documents/{job_id}/confirm", headers=headers(), json={"command_id": "click", "draft_digest": draft["draft_digest"]})
    source_id = draft["draft"]["files"]["candidate.pptx"]["artifact_id"]
    pdf_id = draft["draft"]["files"]["candidate.pdf"]["artifact_id"]
    url = f"/api/v1/files/{pdf_id}"
    response = await client.get(url, headers=headers())
    assert response.status_code == 200 and response.content == result.files["candidate.pdf"], response.text
    await ctx.artifacts.soft_delete(source_id, user_id=SCOPE.user_id)
    assert (await client.get(url, headers=headers())).status_code == 404
    assert (await client.get(f"/api/v1/documents/{job_id}/download/candidate.pdf", headers=headers())).status_code == 404
    for suffix in ("/preview", "/representations/preview", "/representations/details"):
        assert (await client.get(url + suffix, headers=headers())).status_code == 404


async def test_password_rotation_invalidates_confirmation_token(http_case, ctx):
    client, job_id, draft, _ = http_case
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE users SET pwd_version=1 WHERE id=?", (SCOPE.user_id,))
        await db.commit()
    response = await client.post(
        f"/api/v1/documents/{job_id}/confirm", headers=headers(), json={"command_id": "click", "draft_digest": draft["draft_digest"]}
    )
    assert response.status_code == 401
