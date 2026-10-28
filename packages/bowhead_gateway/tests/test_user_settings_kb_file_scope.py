"""The knowledge-base file list remembers the last scope filter per user (``kb_file_scope``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import user_settings
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    store = UserSettingsStore(tmp_path / "settings.db")
    await store.init()
    app = FastAPI()
    app.state.user_settings_store = store
    app.include_router(user_settings.router)
    current = {"id": "u_a"}
    app.dependency_overrides[_current_user] = lambda: User(
        id=current["id"], email=f"{current['id']}@example.test", team_id="team_1", tier="ultra", email_verified=True
    )
    test_client = TestClient(app)
    test_client.current = current  # type: ignore[attr-defined]
    return test_client


@pytest.mark.parametrize("scope", ["all", "personal", "space", "team"])
def test_saved_scope_is_returned_to_the_same_user_only(client: TestClient, scope: str) -> None:
    assert client.put("/api/v1/user-settings/kb_file_scope", json={"value": scope}).status_code == 200
    assert client.get("/api/v1/user-settings").json()["settings"]["kb_file_scope"] == scope

    client.current["id"] = "u_b"  # type: ignore[attr-defined]
    assert "kb_file_scope" not in client.get("/api/v1/user-settings").json()["settings"]


@pytest.mark.parametrize("value", ["workspace", "everything", ""])
def test_unknown_scope_is_rejected(client: TestClient, value: str) -> None:
    response = client.put("/api/v1/user-settings/kb_file_scope", json={"value": value})

    assert response.status_code == 400
    assert "kb_file_scope" not in client.get("/api/v1/user-settings").json()["settings"]
