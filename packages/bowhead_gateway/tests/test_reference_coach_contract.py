"""@Coach starts only under the lead-delegation.v1 contract."""

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


def _reject_coach(contract: str | None) -> HTTPException:
    with pytest.raises(HTTPException) as rejected:
        require_supported_orchestration_contract(SimpleNamespace(profile_name="agent-coach", orchestration_contract=contract), team_id="team_a")
    return rejected.value


def test_coach_mentioned_with_other_content_says_what_to_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", raising=False)
    # Attachments the lead-delegation writer cannot carry select the direct contract.
    selected = _lead_delegation_writer_selected(SimpleNamespace(nodes=[]), entry_mode="ordinary", has_attachments=True, attachment_refs=None)
    assert selected is False

    rejected = _reject_coach(None)
    assert rejected.status_code == 409
    assert rejected.detail == {"error": {"code": "coach_mention_unsupported"}}


def test_coach_mention_on_a_deployment_without_lead_delegation_points_to_the_agent_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
    assert _lead_delegation_writer_selected(SimpleNamespace(nodes=[]), entry_mode="ordinary") is False

    rejected = _reject_coach(None)
    assert rejected.status_code == 409
    assert rejected.detail == {"error": {"code": "coach_mention_unavailable"}}


@pytest.mark.parametrize(
    ("flag", "code", "expected"),
    [
        ("1", "coach_mention_unsupported", "Mention Coach on its own"),
        ("0", "coach_mention_unavailable", "ask Coach through the Agent page"),
    ],
)
def test_coach_rejection_reaches_the_member_as_a_specific_localized_message(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    code: str,
    expected: str,
) -> None:
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", flag)
    app = FastAPI()
    register_error_handlers(app)
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())

    @app.get("/coach-mention")
    async def reject():
        raise _reference_rejected_before_admission(_reject_coach(None))

    response = TestClient(app, cookies={"NEXT_LOCALE": "en-US"}).get("/coach-mention")
    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == code
    # Nothing was written, so the client keeps the draft editable.
    assert error["admission"] == "not_started"
    assert expected in error["message"]


@pytest.mark.parametrize(
    ("profile", "contract"),
    [("agent-coach", "lead-delegation.v1"), ("compute", None), ("configuration-agent", None)],
)
def test_other_starts_keep_their_contract(profile: str, contract: str | None) -> None:
    require_supported_orchestration_contract(SimpleNamespace(profile_name=profile, orchestration_contract=contract), team_id="team_a")
