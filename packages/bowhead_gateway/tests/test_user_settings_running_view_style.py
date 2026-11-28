"""The chat running view's display style is one account setting (chat running state R3.2, ``running_view_style``).

The Appearance page and the running card's switcher both write it; another browser or device reads the same value.
Unset means the list; resetting deletes the key.
"""

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


@pytest.mark.parametrize("style", ["list", "chips", "lanes", "timeline"])
def test_a_picked_style_is_kept_on_the_account_of_that_user_only(client: TestClient, style: str) -> None:
    assert client.put("/api/v1/user-settings/running_view_style", json={"value": style}).status_code == 200
    assert client.get("/api/v1/user-settings").json()["settings"]["running_view_style"] == style

    client.current["id"] = "u_b"  # type: ignore[attr-defined]
    assert "running_view_style" not in client.get("/api/v1/user-settings").json()["settings"]


@pytest.mark.parametrize("value", ["grid", "LIST", "", "timeline "])
def test_an_unknown_style_is_refused_and_nothing_is_kept(client: TestClient, value: str) -> None:
    response = client.put("/api/v1/user-settings/running_view_style", json={"value": value})

    assert response.status_code == 400
    assert "running_view_style" in response.json()["detail"]
    assert "running_view_style" not in client.get("/api/v1/user-settings").json()["settings"]


def test_resetting_returns_to_the_list(client: TestClient) -> None:
    client.put("/api/v1/user-settings/running_view_style", json={"value": "lanes"})
    assert client.delete("/api/v1/user-settings/running_view_style").status_code in {200, 204}
    assert "running_view_style" not in client.get("/api/v1/user-settings").json()["settings"]
