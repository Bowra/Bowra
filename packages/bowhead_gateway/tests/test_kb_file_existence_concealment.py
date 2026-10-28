"""Per-file KB routes never reveal whether a file id exists to a caller who cannot read it.

"Missing", "soft-deleted", "outside your scope" and "denied by a read grant" must produce
the same status, headers and body (``404 kb_file_not_found``). Once the caller can read the
file, a refused *change* stays ``403 kb_file_access_denied``: the file is already visible to
them, so saying "you cannot manage it" discloses nothing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.store import KbStore
from bowhead_core.user_store import User
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEAM = "team_1"
WS = "ws_1"
ADMIN = User(id="u_admin", email="boss@admin.test", team_id=TEAM, tier="ultra", email_verified=True)
MEMBER = User(id="u_member", email="m@member.test", team_id=TEAM, tier="ultra", email_verified=True)
MISSING = "kbf_missing"

# Every per-file route of the KB router (see `routes/kb.py`), with a valid body.
_ROUTES = [
    ("GET", "/api/v1/kb/files/{id}", None),
    ("GET", "/api/v1/kb/files/{id}/download", None),
    ("GET", "/api/v1/kb/files/{id}/preview", None),
    ("GET", "/api/v1/kb/files/{id}/chunks", None),
    ("GET", "/api/v1/kb/files/{id}/health", None),
    ("POST", "/api/v1/kb/files/{id}/verify", None),
    ("POST", "/api/v1/kb/files/{id}/rebuild", None),
    ("POST", "/api/v1/kb/files/{id}/reparse", None),
    ("DELETE", "/api/v1/kb/files/{id}", None),
    ("PATCH", "/api/v1/kb/files/{id}/scope", {"new_scope": "team", "new_scope_id": TEAM}),
    ("PATCH", "/api/v1/kb/files/{id}/doc_type", {"doc_type_id": None}),
]
_ROUTE_IDS = [f"{method} {path}" for method, path, _ in _ROUTES]
_BULK_ROUTES = [
    ("/api/v1/kb/files/bulk_delete", {}),
    ("/api/v1/kb/files/bulk_scope", {"new_scope": "team", "new_scope_id": TEAM}),
    ("/api/v1/kb/files/bulk_doc_type", {"doc_type_id": None}),
]


class _ArtifactStore:
    def __init__(self) -> None:
        self.backend = SimpleNamespace(read=self._read)

    async def get(self, artifact_id: str):  # noqa: ANN201
        return SimpleNamespace(path=f"/blob/{artifact_id}", status="ready")

    async def _read(self, path: str) -> bytes:
        return b"original bytes"


@pytest.fixture
async def env(initialize_migrated_kb_files_database, tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "admin.test")
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(kb.router)
    current = {"user": ADMIN}
    app.dependency_overrides[_current_user] = lambda: current["user"]
    app.state.kb_files_store = files
    app.state.kb_store = KbStore(str(tmp_path / f"chroma_{uuid.uuid4().hex}"))
    app.state.artifact_store = _ArtifactStore()

    async def create(*, scope: str, scope_id: str, uploaded_by: str) -> str:
        return await files.create(
            artifact_id=f"art_{uuid.uuid4().hex[:6]}",
            filename="secret-plan.md",
            mime_type="text/markdown",
            size_bytes=10,
            scope=scope,
            scope_id=scope_id,
            workspace_id=WS,
            team_id=TEAM,
            uploaded_by=uploaded_by,
            initial_parse_status="done",
        )

    def as_user(user: User) -> None:
        current["user"] = user

    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), files=files, create=create, as_user=as_user)


def _call(client: TestClient, method: str, path: str, body, file_id: str):  # noqa: ANN001, ANN202
    url = path.format(id=file_id)
    if body is None:
        return client.request(method, url)
    return client.request(method, url, json=body)


def _assert_same_answer(existing, missing) -> None:  # noqa: ANN001
    assert existing.status_code == missing.status_code == 404, existing.text
    assert existing.content == missing.content
    assert dict(existing.headers) == dict(missing.headers)
    assert existing.json()["detail"]["error"]["code"] == "kb_file_not_found"
    assert b"secret-plan" not in existing.content


@pytest.fixture
def trust_flags(monkeypatch):
    """Turn on the flag-gated routes (health / verify / rebuild) so they reach the file check."""
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    monkeypatch.setenv("FEATURE_KB_USE_GUARD", "1")


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES, ids=_ROUTE_IDS)
async def test_unreadable_file_answers_exactly_like_a_missing_one(env, trust_flags, method, path, body) -> None:
    file_id = await env.create(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id)
    env.as_user(MEMBER)

    _assert_same_answer(_call(env.client, method, path, body, file_id), _call(env.client, method, path, body, MISSING))
    assert (await env.files.get(file_id)).soft_deleted_at is None


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES, ids=_ROUTE_IDS)
async def test_soft_deleted_file_answers_exactly_like_a_missing_one(env, trust_flags, method, path, body) -> None:
    file_id = await env.create(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id)
    await env.files.soft_delete(file_id)

    _assert_same_answer(_call(env.client, method, path, body, file_id), _call(env.client, method, path, body, MISSING))


async def test_read_grant_denial_answers_exactly_like_a_missing_one(env, monkeypatch) -> None:
    file_id = await env.create(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id)

    async def deny(request, ctx, file, *, permission, strict=True):  # noqa: ANN001, ANN202
        raise kb._kb_error(403, "resource_grant_denied", resource_kind="kb_file", resource_id=file.file_id, permission=permission)

    monkeypatch.setattr(kb, "_require_kb_file_grant", deny)

    _assert_same_answer(env.client.get(f"/api/v1/kb/files/{file_id}"), env.client.get(f"/api/v1/kb/files/{MISSING}"))


@pytest.mark.parametrize(("path", "extra"), _BULK_ROUTES, ids=[p for p, _ in _BULK_ROUTES])
async def test_bulk_item_results_do_not_tell_unreadable_from_missing(env, path, extra) -> None:
    file_id = await env.create(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id)
    env.as_user(MEMBER)

    response = env.client.post(path, json={"file_ids": [file_id, MISSING], **extra})

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [(r["ok"], r["error_code"]) for r in results] == [(False, "kb_file_not_found")] * 2


async def test_remove_by_artifact_does_not_tell_unreadable_from_not_in_kb(env) -> None:
    file_id = await env.create(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id)
    artifact_id = (await env.files.get(file_id)).artifact_id
    env.as_user(MEMBER)

    existing = env.client.delete(f"/api/v1/kb/files/by-artifact/{artifact_id}")
    missing = env.client.delete("/api/v1/kb/files/by-artifact/art_missing")

    assert existing.status_code == missing.status_code == 404, existing.text
    assert existing.content == missing.content
    assert (await env.files.get(file_id)).soft_deleted_at is None


async def test_readers_keep_access_and_a_visible_file_still_reports_write_denial(env) -> None:
    personal = await env.create(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id)
    team_file = await env.create(scope="team", scope_id=TEAM, uploaded_by=ADMIN.id)

    for path in (f"/api/v1/kb/files/{personal}", f"/api/v1/kb/files/{personal}/download", f"/api/v1/kb/files/{personal}/chunks"):
        assert env.client.get(path).status_code == 200, path

    env.as_user(MEMBER)
    assert env.client.get(f"/api/v1/kb/files/{team_file}").status_code == 200
    denied = env.client.delete(f"/api/v1/kb/files/{team_file}")
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["error"]["code"] == "kb_file_access_denied"
    assert (await env.files.get(team_file)).soft_deleted_at is None
