"""Each user chooses whether Coach runs the release check after writing a draft (``agent_coach_after_edit``)."""

from __future__ import annotations

import pytest
from bowhead_gateway.agent_coach_after_edit import coach_after_edit
from fastapi.testclient import TestClient
from test_user_settings_kb_file_scope import client as client

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["check", "ask"])
def test_choice_is_saved(client: TestClient, value: str) -> None:
    assert client.put("/api/v1/user-settings/agent_coach_after_edit", json={"value": value}).status_code == 200
    assert client.get("/api/v1/user-settings").json()["settings"]["agent_coach_after_edit"] == value


@pytest.mark.parametrize("value", ["always", "", "Ask"])
def test_unknown_choice_is_rejected(client: TestClient, value: str) -> None:
    assert client.put("/api/v1/user-settings/agent_coach_after_edit", json={"value": value}).status_code == 400
    assert "agent_coach_after_edit" not in client.get("/api/v1/user-settings").json()["settings"]


def test_unset_means_check_after_edit() -> None:
    assert coach_after_edit(None) == "check" and coach_after_edit("ask") == "ask"
