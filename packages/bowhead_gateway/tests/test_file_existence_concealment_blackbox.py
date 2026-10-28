"""Black-box: no per-id file or KB route tells an existing-but-unreadable id from a missing one.

Runs the real Space authorization (`build_access_context`) with a fake membership store, under
four ``X-Space-Id`` conditions: none, a valid Space where the prober is a plain member, a forged
Space, and an archived Space. For every per-id route the prober's request for another member's
file must match the request for a missing id byte for byte (status, headers, body).

The route list is taken from the production app's route table: a new per-id route fails
`test_every_per_id_route_is_covered_or_exempt` until it is added here or exempted with a reason.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import User
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import files as files_routes
from bowhead_gateway.routes import files_presign, kb, kb_admin, kb_presign
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_1"
WS = "ws_main"
WS_ARCHIVED = "ws_archived"
WS_FORGED = "ws_forged"
OWNER = "u_owner"
PROBER = "u_prober"
MISSING = "id_missing_000"

_ROLES = {(WS, OWNER): "ws_admin", (WS, PROBER): "member", (WS_ARCHIVED, PROBER): "member"}
_STATUSES = {WS: "active", WS_ARCHIVED: "archived"}
_HEADERS = {
    "no_space": {},
    "valid_member_space": {"X-Space-Id": WS},
    "forged_space": {"X-Space-Id": WS_FORGED},
    "archived_space": {"X-Space-Id": WS_ARCHIVED},
}

# Per-id routes that belong to other resource families, each with the reason it is out of scope.
_EXEMPT = {
    ("DELETE", "/api/v1/sessions/{session_id}/attachments/{artifact_id}"): "session family: session access is checked first",
    ("GET", "/api/v1/sessions/{session_id}/artifacts/{artifact_id}/content"): "session family: session access is checked first",
    ("GET", "/api/v1/canvas/by-session/{session_id}/artifacts/{artifact_id}"): "report family: report_unavailable contract",
    ("GET", "/api/v1/canvas/by-session/{session_id}/artifacts/{artifact_id}/representations/{representation}"): "report family",
    ("GET", "/api/v1/canvas/by-session/{session_id}/artifacts/{artifact_id}/preview"): "report family",
    ("GET", "/api/v1/branding/assets/{artifact_id}"): "team branding assets are public brand resources",
    ("HEAD", "/api/v1/branding/assets/{artifact_id}"): "team branding assets are public brand resources",
    ("DELETE", "/api/v1/team-branding/assets/{artifact_id}"): "team branding: team admin route",
    ("POST", "/api/v1/kb/matrices/{matrix_id}/cells/{file_id}/{column_id}/rerun"): "matrix-scoped; see test_kb_matrix_authz",
    ("PATCH", "/api/v1/kb/matrices/{matrix_id}/cells/{file_id}/{column_id}"): "matrix-scoped; see test_kb_matrix_authz",
}

# (method, path) -> (which seeded id fills the path, request kwargs)
_JSON = "json"
_COVERED: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {
    ("GET", "/api/v1/files/{artifact_id}"): ("artifact", {}),
    ("DELETE", "/api/v1/files/{artifact_id}"): ("artifact", {}),
    ("POST", "/api/v1/files/{artifact_id}/restore"): ("artifact", {}),
    ("GET", "/api/v1/files/{artifact_id}/usages"): ("artifact", {}),
    ("GET", "/api/v1/files/{artifact_id}/representations/{representation}"): ("artifact", {"representation": "details"}),
    ("GET", "/api/v1/files/{artifact_id}/preview"): ("artifact", {}),
    ("POST", "/api/v1/files/{artifact_id}/category"): ("artifact", {_JSON: {"category": "dataset"}}),
    ("POST", "/api/v1/files/{artifact_id}/share"): ("artifact", {_JSON: {"subject_kind": "workspace", "subject_id": WS}}),
    ("DELETE", "/api/v1/files/{artifact_id}/share/{subject_kind}/{subject_id}"): ("artifact", {"subject_kind": "workspace", "subject_id": WS}),
    ("GET", "/api/v1/files/{artifact_id}/shares"): ("artifact", {}),
    ("POST", "/api/v1/files/presign/{file_id}/confirm"): ("artifact", {_JSON: {}}),
    ("GET", "/api/v1/kb/files/{file_id}"): ("kb_file", {}),
    ("GET", "/api/v1/kb/files/{file_id}/health"): ("kb_file", {}),
    ("GET", "/api/v1/kb/files/{file_id}/preview"): ("kb_file", {}),
    ("GET", "/api/v1/kb/files/{file_id}/download"): ("kb_file", {}),
    ("DELETE", "/api/v1/kb/files/{file_id}"): ("kb_file", {}),
    ("DELETE", "/api/v1/kb/files/by-artifact/{artifact_id}"): ("kb_artifact", {}),
    ("PATCH", "/api/v1/kb/files/{file_id}/scope"): ("kb_file", {_JSON: {"new_scope": "team", "new_scope_id": TEAM}}),
    ("PATCH", "/api/v1/kb/files/{file_id}/doc_type"): ("kb_file", {_JSON: {"doc_type_id": None}}),
    ("POST", "/api/v1/kb/files/{file_id}/reparse"): ("kb_file", {}),
    ("POST", "/api/v1/kb/files/{file_id}/verify"): ("kb_file", {}),
    ("POST", "/api/v1/kb/files/{file_id}/restore-source"): ("kb_file", {"files": {"upload": ("a.md", b"x", "text/markdown")}}),
    ("POST", "/api/v1/kb/files/{file_id}/rebuild"): ("kb_file", {}),
    ("GET", "/api/v1/kb/files/{file_id}/chunks"): ("kb_file", {}),
    ("POST", "/api/v1/kb/files/{file_id}/extract_rows"): ("kb_file", {_JSON: {"force": False}}),
    ("GET", "/api/v1/kb/files/{file_id}/extract_rows_status"): ("kb_file", {}),
    ("POST", "/api/v1/kb/presign/{file_id}/confirm"): ("kb_file", {_JSON: {}}),
    ("POST", "/api/v1/kb/presign/bundle/{file_id}/confirm"): ("kb_file", {_JSON: {}}),
}
_COVERED_IDS = [f"{method} {path}" for method, path in _COVERED]


def _is_per_id(path: str) -> bool:
    return "{artifact_id}" in path or "{file_id}" in path


def test_every_per_id_route_is_covered_or_exempt() -> None:
    from bowhead_gateway.main import app as production_app

    live = {
        (method, route.path)
        for route in production_app.routes
        if _is_per_id(getattr(route, "path", ""))
        for method in getattr(route, "methods", None) or ()
    }
    unaccounted = live - set(_COVERED) - set(_EXEMPT)
    assert not unaccounted, f"new per-id routes need a concealment case or an explicit exemption: {sorted(unaccounted)}"
    stale = (set(_COVERED) | set(_EXEMPT)) - live
    assert not stale, f"listed routes no longer exist: {sorted(stale)}"


class _MembershipStore:
    """Just enough of UserStore for the real `build_access_context`."""

    async def get_user(self, user_id: str):  # noqa: ANN201
        return SimpleNamespace(id=user_id, team_id=TEAM, status="active")

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        return TEAM if workspace_id in _STATUSES else None

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        return _ROLES.get((workspace_id, user_id))

    async def get_workspace(self, workspace_id: str) -> dict[str, str] | None:
        status = _STATUSES.get(workspace_id)
        return {"id": workspace_id, "status": status} if status else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "member"

    async def get_team_member_labels(self, team_id: str, user_ids: list[str]) -> dict[str, str]:
        return {}


class _BlobStore:
    """Artifact store stand-in for KB routes: the blob exists and reads succeed."""

    def __init__(self, real: ArtifactStore) -> None:
        self._real = real
        self.backend = SimpleNamespace(read=self._read)

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._real, name)

    async def _read(self, path: str) -> bytes:
        return b"secret-plan body"


@pytest.fixture
async def env(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    monkeypatch.setenv("FEATURE_KB_USE_GUARD", "1")
    db_path = tmp_path / "a.db"
    sessions = SessionStore(db_path)
    await sessions.initialize()
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await artifacts.initialize()
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    kb_files = KbFilesStore(tmp_path / "kb_files.db")
    await kb_files.initialize()

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.include_router(files_presign.router)
    app.include_router(kb.router)
    app.include_router(kb_admin.router)
    app.include_router(kb_presign.router)

    def current_user(request: Request) -> User:
        uid = request.headers["X-Bowhead-User-Id"]
        return User(id=uid, email=f"{uid}@member.test", team_id=TEAM, tier="ultra", email_verified=True)

    app.dependency_overrides[_current_user] = current_user
    app.state.settings = SimpleNamespace(dev_default_user=OWNER, auth_mode="dev")
    app.state.user_store = _MembershipStore()
    app.state.access_control_store = None
    app.state.artifact_store = _BlobStore(artifacts)
    app.state.kb_files_store = kb_files
    app.state.session_store = sessions  # lets the provenance ladder place session-pinned artifacts in WS

    art = await artifacts.put_bytes(
        content=b"secret-plan body", filename="secret-plan.csv", mime="text/csv", source="upload", created_by=OWNER, kind="user_upload"
    )
    kb_art = await artifacts.put_bytes(
        content=b"secret-plan body", filename="secret-plan.md", mime="text/markdown", source="kb_upload", created_by=OWNER, kind="user_upload"
    )
    kb_file = await kb_files.create(
        artifact_id=kb_art.id,
        filename="secret-plan.md",
        mime_type="text/markdown",
        size_bytes=16,
        scope="personal",
        scope_id=OWNER,
        workspace_id=WS,
        team_id=TEAM,
        uploaded_by=OWNER,
        initial_parse_status="done",
    )
    ids = {"artifact": art.id, "kb_file": kb_file, "kb_artifact": kb_art.id}
    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), ids=ids, sessions=sessions)


def _send(env, method: str, path: str, spec: tuple[str, dict[str, Any]], resource_id: str, user: str, headers: dict[str, str]):  # noqa: ANN001, ANN202
    kind, kwargs = spec
    params = {key: value for key, value in kwargs.items() if key not in {_JSON, "files"}}
    id_name = "artifact_id" if "{artifact_id}" in path else "file_id"
    url = path.format(**{id_name: resource_id, **params})
    extra = {key: kwargs[key] for key in (_JSON, "files") if key in kwargs}
    return env.client.request(method, url, headers={"X-Bowhead-User-Id": user, **headers}, **extra)


@pytest.mark.parametrize("header", list(_HEADERS))
@pytest.mark.parametrize(("method", "path"), list(_COVERED), ids=_COVERED_IDS)
async def test_unreadable_id_is_indistinguishable_from_missing(env, method, path, header) -> None:
    spec = _COVERED[(method, path)]
    headers = _HEADERS[header]

    existing = _send(env, method, path, spec, env.ids[spec[0]], PROBER, headers)
    missing = _send(env, method, path, spec, MISSING, PROBER, headers)

    assert existing.status_code >= 400, existing.text
    assert (existing.status_code, dict(existing.headers), existing.content) == (missing.status_code, dict(missing.headers), missing.content)
    assert b"secret-plan" not in existing.content


async def test_owner_still_reaches_each_family(env) -> None:
    """Positive control: the same fixture serves the owner, so the 404s above are the gate."""
    owner = {"X-Bowhead-User-Id": OWNER, "X-Space-Id": WS}
    paths = [
        f"/api/v1/files/{env.ids['artifact']}",
        f"/api/v1/files/{env.ids['artifact']}/usages",
        f"/api/v1/kb/files/{env.ids['kb_file']}",
        f"/api/v1/kb/files/{env.ids['kb_file']}/download",
        f"/api/v1/kb/files/{env.ids['kb_file']}/extract_rows_status",
    ]
    for path in paths:
        response = env.client.get(path, headers=owner)
        assert response.status_code == 200, f"{path}: {response.text}"


async def test_admin_cannot_read_extraction_of_someone_elses_personal_file(env) -> None:
    """A Space admin gets the same 404 as for a missing id on another member's personal file."""
    personal = await env.client.app.state.kb_files_store.create(
        artifact_id=f"art_{uuid.uuid4().hex[:6]}",
        filename="private.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="personal",
        scope_id=PROBER,
        workspace_id=WS,
        team_id=TEAM,
        uploaded_by=PROBER,
        initial_parse_status="done",
    )
    admin = {"X-Bowhead-User-Id": OWNER, "X-Space-Id": WS}
    for method, suffix, kwargs in (("GET", "extract_rows_status", {}), ("POST", "extract_rows", {"json": {"force": True}})):
        existing = env.client.request(method, f"/api/v1/kb/files/{personal}/{suffix}", headers=admin, **kwargs)
        missing = env.client.request(method, f"/api/v1/kb/files/{MISSING}/{suffix}", headers=admin, **kwargs)
        assert existing.status_code == 404, existing.text
        assert existing.content == missing.content


# ─── Owner-less artifacts (empty ``created_by``) ───
# Readable inside the Space they belong to, but nobody may mutate them: every mutation must
# answer exactly like a missing id, so the refusal does not reveal that the id exists.

OUTSIDER = "u_outsider"  # same team, not a member of WS


async def _ownerless(env, *, filename: str = "ownerless-plan.csv", deleted: bool = False, in_kb: bool = False) -> str:  # noqa: ANN001
    app = env.client.app
    store = app.state.artifact_store
    session = await env.sessions.ensure_session(session_id=None, workspace_id=WS, user_id=OWNER)
    art = await store.put_bytes(
        content=b"ownerless body", filename=filename, mime="text/csv", source="bi_fetch_rows", created_by=None, session_id=session.id
    )
    assert art.created_by is None
    if deleted:
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE artifacts SET deleted_at = '2026-09-01T00:00:00+00:00' WHERE id = ?", (art.id,))
            await db.commit()
    if in_kb:
        await app.state.kb_files_store.create(
            artifact_id=art.id,
            filename=filename,
            mime_type="text/csv",
            size_bytes=14,
            scope="space",
            scope_id=WS,
            workspace_id=WS,
            team_id=TEAM,
            uploaded_by=OWNER,
            initial_parse_status="done",
        )
    return art.id


def _assert_same_as_missing(env, method: str, url_tpl: str, resource_id: str, user: str, *, space: str | None = WS, **kwargs: Any) -> None:  # noqa: ANN001
    headers = {"X-Bowhead-User-Id": user, **({"X-Space-Id": space} if space else {})}
    existing = env.client.request(method, url_tpl.format(id=resource_id), headers=headers, **kwargs)
    missing = env.client.request(method, url_tpl.format(id=MISSING), headers=headers, **kwargs)
    assert existing.status_code == 404, f"{method} {url_tpl}: {existing.text}"
    assert (existing.status_code, dict(existing.headers), existing.content) == (missing.status_code, dict(missing.headers), missing.content)


@pytest.mark.parametrize("user", [PROBER, OWNER], ids=["space_member", "space_admin"])
async def test_ownerless_mutations_answer_like_missing(env, user) -> None:
    store = env.client.app.state.artifact_store
    live = await _ownerless(env)
    referenced = await _ownerless(env, filename="ownerless-kb.csv", in_kb=True)
    trashed = await _ownerless(env, filename="ownerless-trash.csv", deleted=True)

    _assert_same_as_missing(env, "POST", "/api/v1/files/{id}/category", live, user, json={"category": "dataset"})
    _assert_same_as_missing(env, "DELETE", "/api/v1/files/{id}", live, user)
    # Referenced by the KB: the KB-reference guard (409) must not answer before the owner gate.
    _assert_same_as_missing(env, "DELETE", "/api/v1/files/{id}", referenced, user)
    _assert_same_as_missing(env, "POST", "/api/v1/files/{id}/restore", trashed, user)
    headers = {"X-Bowhead-User-Id": user, "X-Space-Id": WS}
    promoted = env.client.post("/api/v1/kb/files/from-artifact", json={"artifact_id": live, "scope": "personal"}, headers=headers)
    missing = env.client.post("/api/v1/kb/files/from-artifact", json={"artifact_id": MISSING, "scope": "personal"}, headers=headers)
    assert promoted.status_code == 404, promoted.text
    assert (promoted.status_code, dict(promoted.headers), promoted.content) == (missing.status_code, dict(missing.headers), missing.content)

    # Nothing changed underneath the refusals.
    after = await store.get(live)
    assert after is not None and after.deleted_at is None and after.category_source != "user"
    trashed_row = await store.get(trashed, include_deleted=True)
    assert trashed_row is not None and trashed_row.deleted_at is not None
    assert not await env.client.app.state.kb_files_store.artifact_ids_in_kb([live])


async def test_ownerless_reads_stay_open_inside_their_space(env) -> None:
    aid = await _ownerless(env)
    member = {"X-Bowhead-User-Id": PROBER, "X-Space-Id": WS}
    for path in (
        f"/api/v1/files/{aid}",
        f"/api/v1/files/{aid}/preview",
        f"/api/v1/files/{aid}/usages",
        f"/api/v1/files/{aid}/representations/details",
    ):
        response = env.client.get(path, headers=member)
        assert response.status_code == 200, f"{path}: {response.text}"
    assert b"ownerless body" in env.client.get(f"/api/v1/files/{aid}", headers=member).content


@pytest.mark.parametrize("space", [None, WS], ids=["no_space_header", "foreign_space_header"])
async def test_ownerless_reads_are_concealed_outside_their_space(env, space) -> None:
    aid = await _ownerless(env)
    for suffix in ("", "/preview", "/usages"):
        _assert_same_as_missing(env, "GET", "/api/v1/files/{id}" + suffix, aid, OUTSIDER, space=space)


async def test_owned_artifact_mutations_still_work_for_the_owner(env) -> None:
    """Positive control for the owner-less refusals: the same routes still serve the real owner."""

    async def _skip_indexing(*_args: Any, **_kwargs: Any) -> None:
        return None

    env.client.app.state.kb_worker = SimpleNamespace(parse_and_index=_skip_indexing)
    store = env.client.app.state.artifact_store
    session = await env.sessions.ensure_session(session_id=None, workspace_id=WS, user_id=PROBER)
    me = {"X-Bowhead-User-Id": PROBER, "X-Space-Id": WS}
    art = await store.put_bytes(
        content=b"mine", filename="mine.csv", mime="text/csv", source="bi_fetch_rows", created_by=PROBER, session_id=session.id
    )
    assert env.client.post(f"/api/v1/files/{art.id}/category", json={"category": "dataset"}, headers=me).status_code == 200
    promoted = env.client.post("/api/v1/kb/files/from-artifact", json={"artifact_id": art.id, "scope": "personal"}, headers=me)
    assert promoted.status_code == 201, promoted.text
    other = await store.put_bytes(
        content=b"mine2", filename="mine2.csv", mime="text/csv", source="bi_fetch_rows", created_by=PROBER, session_id=session.id
    )
    assert env.client.delete(f"/api/v1/files/{other.id}", headers=me).status_code == 204
    restored = env.client.post(f"/api/v1/files/{other.id}/restore", headers=me)
    assert restored.status_code == 200, restored.text
