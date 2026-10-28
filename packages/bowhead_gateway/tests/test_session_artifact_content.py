"""Owner session-scoped artifact byte proxy contracts for Results renderers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def content_ctx(tmp_path: Path):
    settings = Settings()
    owner = settings.dev_default_user
    shared_db = tmp_path / "app.db"
    sessions = SessionStore(shared_db)
    await sessions.initialize()
    await sessions.ensure_session(
        session_id="s_owner",
        workspace_id="w1",
        user_id=owner,
    )
    await sessions.ensure_session(
        session_id="s_foreign",
        workspace_id="w1",
        user_id="u_foreign",
    )
    await sessions.ensure_session(
        session_id="s_owner_other",
        workspace_id="w1",
        user_id=owner,
    )

    artifacts = ArtifactStore(
        backend=LocalBackend(root_dir=tmp_path / "files"),
        db_path=shared_db,
    )
    await artifacts.initialize()
    body = b"<!doctype html><h1>safe result</h1>"
    art = await artifacts.put_bytes(
        content=body,
        filename="result.html",
        mime="text/html",
        source="python_sandbox",
        created_by=owner,
        kind="deliverable",
        session_id="s_owner",
    )

    app = create_app()
    app.state.settings = settings
    app.state.session_store = sessions
    app.state.artifact_store = artifacts
    return app, artifacts, art, body, owner


@pytest.mark.integration
async def test_session_artifact_content_is_same_origin_stream_with_range(
    content_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, artifacts, art, body, _owner = content_ctx
    presign = AsyncMock(return_value="https://objects.invalid/presigned")
    monkeypatch.setattr(artifacts, "download_url", presign)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get(
            f"/api/v1/sessions/s_owner/artifacts/{art.id}/content",
            headers={"Range": "bytes=4-15"},
        )

    assert response.status_code == 206
    assert response.content == body[4:16]
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-range"] == f"bytes 4-15/{len(body)}"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in response.headers["content-disposition"]
    presign.assert_not_awaited()


@pytest.mark.integration
async def test_session_artifact_content_conceals_foreign_session_and_artifact(
    content_ctx,
) -> None:
    app, artifacts, art, _body, owner = content_ctx
    foreign_artifact = await artifacts.put_bytes(
        content=b"secret",
        filename="secret.pdf",
        mime="application/pdf",
        source="upload",
        created_by="u_foreign",
        kind="user_upload",
    )
    other_session_artifact = await artifacts.put_bytes(
        content=b"same owner, other session",
        filename="other.pdf",
        mime="application/pdf",
        # Regression: python_sandbox used to be admitted into every overlapping
        # session owned by the same user via the removed time-window heuristic.
        source="python_sandbox",
        created_by=owner,
        kind="deliverable",
        session_id="s_owner_other",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        foreign_session = await client.get(
            f"/api/v1/sessions/s_foreign/artifacts/{art.id}/content",
            headers={"X-Bowhead-User-Id": owner},
        )
        foreign_file = await client.get(
            f"/api/v1/sessions/s_owner/artifacts/{foreign_artifact.id}/content",
            headers={"X-Bowhead-User-Id": owner},
        )
        other_session_file = await client.get(
            f"/api/v1/sessions/s_owner/artifacts/{other_session_artifact.id}/content",
            headers={"X-Bowhead-User-Id": owner},
        )

    assert foreign_session.status_code == 404
    assert foreign_file.status_code == 404
    assert other_session_file.status_code == 404


@pytest.mark.integration
async def test_session_artifact_list_uses_precise_producer_origin(content_ctx) -> None:
    app, artifacts, _art, _body, owner = content_ctx
    exported = await artifacts.put_bytes(
        content=b"docx",
        filename="report.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source="legacy_unknown",
        producer_kind="canvas_export_skill",
        created_by=owner,
        kind="deliverable",
        session_id="s_owner",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/sessions/s_owner/artifacts")

    assert response.status_code == 200
    item = next(value for value in response.json()["items"] if value["id"] == exported.id)
    assert item["origin"] == "export"
