"""Bound specialists start only under the lead-delegation.v1 contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
from bowhead_gateway.chat_reference_start import _lead_delegation_writer_selected, require_supported_orchestration_contract
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes.chat import _reference_rejected_before_admission
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _reject_specialist(contract: str | None, profile: str = "agent-coach") -> HTTPException:
    with pytest.raises(HTTPException) as rejected:
        require_supported_orchestration_contract(SimpleNamespace(profile_name=profile, orchestration_contract=contract), team_id="team_a")
    return rejected.value


def test_coach_mentioned_with_other_content_says_what_to_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", raising=False)
    # Attachments the lead-delegation writer cannot carry select the direct contract.
    selected = _lead_delegation_writer_selected(SimpleNamespace(nodes=[]), entry_mode="ordinary", has_attachments=True, attachment_refs=None)
    assert selected is False

    rejected = _reject_specialist(None)
    assert rejected.status_code == 409
    assert rejected.detail == {"error": {"code": "coach_mention_unsupported"}}


def test_coach_mention_on_a_deployment_without_lead_delegation_points_to_the_agent_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
    assert _lead_delegation_writer_selected(SimpleNamespace(nodes=[]), entry_mode="ordinary") is False

    rejected = _reject_specialist(None)
    assert rejected.status_code == 409
    assert rejected.detail == {"error": {"code": "coach_mention_unavailable"}}


@pytest.mark.parametrize(
    ("profile", "flag", "code", "expected"),
    [
        ("agent-coach", "1", "coach_mention_unsupported", "Mention Coach on its own"),
        ("agent-coach", "0", "coach_mention_unavailable", "ask Coach through the Agent page"),
        ("configuration-agent", "1", "configuration_mention_unsupported", "Mention Configuration Agent on its own"),
        ("configuration-agent", "0", "configuration_mention_unavailable", "ask for the settings change in ordinary chat"),
    ],
)
def test_specialist_rejection_reaches_the_member_as_a_specific_localized_message(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    flag: str,
    code: str,
    expected: str,
) -> None:
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", flag)
    app = FastAPI()
    register_error_handlers(app)
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())

    @app.get("/specialist-mention")
    async def reject():
        raise _reference_rejected_before_admission(_reject_specialist(None, profile))

    response = TestClient(app, cookies={"NEXT_LOCALE": "en-US"}).get("/specialist-mention")
    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == code
    # Nothing was written, so the client keeps the draft editable.
    assert error["admission"] == "not_started"
    assert expected in error["message"]


@pytest.mark.parametrize(
    ("profile", "contract"),
    [("agent-coach", "lead-delegation.v1"), ("compute", None), ("configuration-agent", "lead-delegation.v1")],
)
def test_other_starts_keep_their_contract(profile: str, contract: str | None) -> None:
    require_supported_orchestration_contract(SimpleNamespace(profile_name=profile, orchestration_contract=contract), team_id="team_a")


@pytest.mark.parametrize("enabled", [True, False])
def test_configuration_reference_requires_its_original_settings_binding(monkeypatch, enabled):
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1" if enabled else "0")
    with pytest.raises(HTTPException) as rejected:
        require_supported_orchestration_contract(SimpleNamespace(profile_name="configuration-agent", orchestration_contract=None), team_id="team_a")
    expected = "configuration_mention_unsupported" if enabled else "configuration_mention_unavailable"
    assert rejected.value.status_code == 409
    assert rejected.value.detail == {"error": {"code": expected}}
