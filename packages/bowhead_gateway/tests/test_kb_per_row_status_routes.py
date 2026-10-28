"""Sprint 25/26 regression: per-row extraction status endpoints (kb_admin router).

Root cause of the 0.44.2 prod 500: these endpoints used ``user.workspace_id``
which does NOT exist on the User model (only ``id`` + ``team_id``). The endpoints
had ZERO TestClient coverage, so the AttributeError reached prod and only
surfaced during real-HTTP dogfood.

These tests lock the fix (resolve ws from ``X-Workspace-Id`` header OR
``user.team_id``, matching kb.py:_make_scope_context) and guard the cross-ws 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.per_row_jobs_store import KbPerRowJobsStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_admin
from bowhead_gateway.routes.auth import _current_user, _require_workspace_admin
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

WS = "team_test"  # matches user.team_id fallback when no X-Workspace-Id header


def _user() -> User:
    # NOTE: User has NO workspace_id attribute — that was the bug.
    return User(id="u_alice", email="alice@test.com", team_id=WS, tier="ultra", email_verified=True)


@pytest.fixture
async def client(initialize_migrated_kb_files_database, tmp_path: Path) -> TestClient:
    a = FastAPI()
    a.include_router(kb_admin.router, tags=["kb-admin"])
    a.dependency_overrides[_current_user] = _user
    a.dependency_overrides[_require_workspace_admin] = _user

    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    jobs_store = KbPerRowJobsStore(tmp_path / "jobs.db")
    await jobs_store.initialize()
    a.state.kb_files_store = files_store
    a.state.kb_per_row_jobs_store = jobs_store

    # Insert a file owned by WS (so header X-Workspace-Id=WS matches).
    file_id = await files_store.create(
        artifact_id="art_1",
        filename="明细账.pdf",
        mime_type="application/pdf",
        size_bytes=1234,
        scope="personal",
        scope_id="u_alice",
        workspace_id=WS,
        team_id=WS,
        uploaded_by="u_alice",
    )
    c = TestClient(a)
    c._test_file_id = file_id  # type: ignore[attr-defined]
    return c


def test_status_resolves_ws_from_header_no_attribute_error(client: TestClient) -> None:
    """REGRESSION: must NOT 500 with AttributeError 'workspace_id'. ws from header."""
    fid = client._test_file_id  # type: ignore[attr-defined]
    r = client.get(f"/api/v1/kb/files/{fid}/extract_rows_status", headers={"X-Workspace-Id": WS})
    assert r.status_code == 200, r.text
    body = r.json()
    # No job exists yet → status "na"
    assert body["status"] == "na"
    assert body["job_id"] is None


def test_status_missing_ws_context_400(client: TestClient) -> None:
    """批1/c4: no X-Workspace-Id header → 400 missing_workspace_context (team id no
    longer stands in as a workspace — the old fallback bled team_* into KB tables)."""
    fid = client._test_file_id  # type: ignore[attr-defined]
    r = client.get(f"/api/v1/kb/files/{fid}/extract_rows_status")
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"]["code"] == "missing_workspace_context"


def test_status_cross_ws_returns_404(client: TestClient) -> None:
    """File owned by WS, caller presents a different ws → 404 enumeration-resist."""
    fid = client._test_file_id  # type: ignore[attr-defined]
    r = client.get(f"/api/v1/kb/files/{fid}/extract_rows_status", headers={"X-Workspace-Id": "team_someone_else"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"]["code"] == "kb_file_not_found"


def test_status_batch_resolves_ws_no_attribute_error(client: TestClient) -> None:
    """REGRESSION: batch endpoint must resolve ws from header, not user.workspace_id."""
    fid = client._test_file_id  # type: ignore[attr-defined]
    r = client.get("/api/v1/kb/files/_batch/extract_rows_status", params={"file_ids": fid}, headers={"X-Workspace-Id": WS})
    assert r.status_code == 200, r.text
    assert "jobs" in r.json()


def test_llm_cost_summary_resolves_ws_no_attribute_error(client: TestClient) -> None:
    """REGRESSION: cost summary endpoint must resolve ws, not crash on user.workspace_id.

    No quotas store wired → graceful note, but ws_id must be present (not crash)."""
    r = client.get("/api/v1/kb/admin/llm_cost_summary", headers={"X-Workspace-Id": WS})
    assert r.status_code == 200, r.text
    assert r.json()["workspace_id"] == WS


def test_backfill_matching_ws_passes_rbac(client: TestClient) -> None:
    """REGRESSION: trigger_backfill used getattr(user,'workspace_id','')→'' which
    made body.workspace_id != '' ALWAYS true → permanent 403. Fix resolves ws
    from header. Matching ws must pass the RBAC gate (then 503 since no worker
    wired in this minimal fixture — NOT 403)."""
    r = client.post("/api/v1/admin/kb/files/backfill", headers={"X-Workspace-Id": WS}, json={"workspace_id": WS})
    assert r.status_code != 403, f"RBAC gate still always-rejecting: {r.text}"
    # No kb_worker in fixture → 503 kb_not_ready proves we got PAST the ws check.
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail == {"error": {"code": "kb_not_ready"}}
    assert "msg" not in detail["error"]


def test_backfill_cross_ws_still_403(client: TestClient) -> None:
    """Cross-ws backfill must still be forbidden (header ws != body ws)."""
    r = client.post("/api/v1/admin/kb/files/backfill", headers={"X-Workspace-Id": WS}, json={"workspace_id": "team_other"})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail == {"error": {"code": "cross_ws_backfill_forbidden"}}
    assert "msg" not in detail["error"]
