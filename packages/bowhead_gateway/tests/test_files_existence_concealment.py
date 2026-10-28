"""Per-id file routes never reveal whether an id exists to a caller who cannot see it.

"Missing", "someone else's", "not shared with you" and "outside a Space you may read"
must produce the same status, headers and body. Positive controls prove the same
routes still serve the owner and a legitimate share recipient.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_store import SessionStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "usr_owner"
_PEER = "usr_peer"
_TEAM = "team_1"
_WS = "ws_1"
_MISSING = "art_missing"
_H_OWNER = {"X-Bowhead-User-Id": _OWNER, "X-Space-Id": _WS}
_H_PEER = {"X-Bowhead-User-Id": _PEER, "X-Space-Id": _WS}

# Every per-id route of the files router (see `routes/files.py`), with a valid body.
_ROUTES = [
    ("GET", "/api/v1/files/{id}", None),
    ("GET", "/api/v1/files/{id}/preview", None),
    ("GET", "/api/v1/files/{id}/usages", None),
    ("GET", "/api/v1/files/{id}/representations/details", None),
    ("GET", "/api/v1/files/{id}/shares", None),
    ("POST", "/api/v1/files/{id}/category", {"category": "dataset"}),
    ("POST", "/api/v1/files/{id}/share", {"subject_kind": "workspace", "subject_id": _WS}),
    ("POST", "/api/v1/files/{id}/restore", None),
    ("DELETE", "/api/v1/files/{id}/share/workspace/" + _WS, None),
    ("DELETE", "/api/v1/files/{id}", None),
]
_ROUTE_IDS = [f"{method} {path}" for method, path, _ in _ROUTES]


class _FakeUserStore:
    async def get_user(self, user_id: str):  # noqa: ANN201
        return SimpleNamespace(id=user_id, team_id=_TEAM)


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "a.db"
    sessions = SessionStore(db_path)
    await sessions.initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store.initialize()
    access = AccessControlStore(tmp_path / "ac.db")
    await access.initialize()

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.access_control_store = access
    app.state.user_store = _FakeUserStore()
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    art = await store.put_bytes(
        content=b"col_a,col_b\n1,2\n",
        filename="rows.csv",
        mime="text/csv",
        source="upload",
        created_by=_OWNER,
        kind="user_upload",
    )
    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), store=store, art=art)


def _call(client: TestClient, method: str, path: str, body, artifact_id: str, headers: dict[str, str]):  # noqa: ANN001, ANN202
    url = path.format(id=artifact_id)
    if body is None:
        return client.request(method, url, headers=headers)
    return client.request(method, url, json=body, headers=headers)


def _assert_same_answer(existing, missing) -> None:  # noqa: ANN001
    assert existing.status_code == missing.status_code == 404, existing.text
    assert existing.content == missing.content
    assert dict(existing.headers) == dict(missing.headers)


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES, ids=_ROUTE_IDS)
async def test_someone_elses_file_answers_exactly_like_a_missing_one(env, method, path, body) -> None:
    existing = _call(env.client, method, path, body, env.art.id, _H_PEER)
    missing = _call(env.client, method, path, body, _MISSING, _H_PEER)

    _assert_same_answer(existing, missing)
    assert b"rows.csv" not in existing.content
    assert (await env.store.get(env.art.id)) is not None


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES, ids=_ROUTE_IDS)
async def test_space_denial_answers_exactly_like_a_missing_one(env, method, path, body, monkeypatch, caplog) -> None:
    """The owner lost access to the file's Space: same answer as a missing id, reason kept in the log."""
    caplog.set_level(logging.INFO, logger="bowhead_gateway.routes.files")

    async def deny(*_args, **_kwargs) -> None:
        raise files_routes._file_error(403, "workspace_access_denied")

    monkeypatch.setattr(files_routes, "_require_artifact_operation", deny)
    existing = _call(env.client, method, path, body, env.art.id, _H_OWNER)
    missing = _call(env.client, method, path, body, _MISSING, _H_OWNER)

    _assert_same_answer(existing, missing)
    if "representations" not in path:  # that route keeps its own `artifact_scope_unavailable` contract
        assert "reason=workspace_access_denied" in caplog.text


async def test_share_context_denial_on_preview_answers_like_a_missing_one(env, monkeypatch) -> None:
    """Resolving the caller's Space for a share lookup must not leak the id either."""

    async def deny(*_args, **_kwargs):  # noqa: ANN202
        raise HTTPException(403, detail={"error": {"code": "workspace_access_denied"}})

    monkeypatch.setattr(files_routes, "_share_ctx", deny)
    existing = env.client.get(f"/api/v1/files/{env.art.id}/preview", headers=_H_PEER)
    missing = env.client.get(f"/api/v1/files/{_MISSING}/preview", headers=_H_PEER)

    _assert_same_answer(existing, missing)


async def test_lifecycle_errors_are_not_concealed(env, monkeypatch) -> None:
    """An archived Space is about the caller's Space, not the file: 409 still reaches the owner."""

    async def archived(*_args, **_kwargs) -> None:
        raise files_routes._file_error(409, "workspace_archived")

    monkeypatch.setattr(files_routes, "_require_artifact_operation", archived)
    response = env.client.post(f"/api/v1/files/{env.art.id}/category", json={"category": "dataset"}, headers=_H_OWNER)

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"


async def test_owner_and_share_recipient_still_reach_the_file(env) -> None:
    owner_reads = [
        env.client.get(f"/api/v1/files/{env.art.id}", headers=_H_OWNER),
        env.client.get(f"/api/v1/files/{env.art.id}/preview", headers=_H_OWNER),
        env.client.get(f"/api/v1/files/{env.art.id}/usages", headers=_H_OWNER),
        env.client.get(f"/api/v1/files/{env.art.id}/shares", headers=_H_OWNER),
    ]
    assert [r.status_code for r in owner_reads] == [200, 200, 200, 200]
    shared = env.client.post(f"/api/v1/files/{env.art.id}/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER)
    assert shared.status_code == 201, shared.text

    download = env.client.get(f"/api/v1/files/{env.art.id}", headers=_H_PEER)
    preview = env.client.get(f"/api/v1/files/{env.art.id}/preview", headers=_H_PEER)

    assert download.status_code == 200 and download.content == b"col_a,col_b\n1,2\n"
    assert preview.status_code == 200, preview.text
