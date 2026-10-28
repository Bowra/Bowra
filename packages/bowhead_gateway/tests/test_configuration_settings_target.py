"""Settings send admission checks the exact caller and original Space access."""

import json
from types import SimpleNamespace

import pytest
from bowhead_core.configuration_settings_target import WorkspaceSettingsSendTarget
from bowhead_gateway.routes.chat import ChatSendRequest
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError

from bowhead_gateway import configuration_settings_target as adapter

pytestmark = pytest.mark.unit


class AccessStore:
    role = "ws_admin"

    async def get_workspace_team_id(self, workspace_id):
        return "team_1"

    async def get_workspace(self, workspace_id):
        return {"status": "active"}

    async def get_member_role(self, workspace_id, user_id):
        return self.role


def target(**updates):
    return WorkspaceSettingsSendTarget.model_validate(
        {
            "form_id": "workspace_i18n",
            "user_id": "u_1",
            "team_id": "team_1",
            "workspace_id": "ws_1",
            "edit_generation": 3,
            "saved_ready": True,
            "pending_writes": 0,
            "unconfirmed_fields": [],
            **updates,
        }
    )


@pytest.fixture
def request_ctx(monkeypatch):
    app = FastAPI()
    app.state.user_store = AccessStore()

    async def current_user(request):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(adapter, "_current_user", current_user)
    return Request({"type": "http", "app": app, "headers": []})


@pytest.mark.parametrize("form_id", ["workspace_i18n", "workspace_metadata"])
async def test_admission_keeps_original_scope_and_rechecks_revoked_access(request_ctx, form_id):
    admitted = await adapter.admit_workspace_settings_target(
        request_ctx, target(form_id=form_id), user_id="u_1", team_id="team_1", workspace_id="ws_1"
    )
    assert admitted.target.workspace_id == "ws_1"
    request_ctx.app.state.user_store.role = "member"
    with pytest.raises(HTTPException) as denial:
        await admitted.recheck()
    assert denial.value.status_code == 403


@pytest.mark.parametrize(
    "updates",
    [
        {"user_id": "u_other"},
        {"team_id": "team_other"},
        {"workspace_id": "ws_other"},
        {"pending_writes": 1},
        {"unconfirmed_fields": ["week_start_day"]},
    ],
)
async def test_admission_rejects_foreign_or_unconfirmed_targets(request_ctx, updates):
    with pytest.raises(HTTPException) as denial:
        await adapter.admit_workspace_settings_target(request_ctx, target(**updates), user_id="u_1", team_id="team_1", workspace_id="ws_1")
    assert denial.value.status_code in {403, 409}


def test_send_schema_rejects_unknown_forms_and_mixed_authoring_targets():
    wire = target().model_dump_json()
    request = ChatSendRequest(workspace_id="ws_1", message="Change week start", client_info={"configuration_settings_target": wire})
    assert json.loads(request.client_info["configuration_settings_target"])["edit_generation"] == 3
    with pytest.raises(ValidationError):
        ChatSendRequest(
            workspace_id="ws_1", client_info={"configuration_settings_target": wire, "agent_draft_id": "sad_one", "agent_draft_version": "1"}
        )
    with pytest.raises(ValidationError):
        ChatSendRequest(workspace_id="ws_1", client_info={"configuration_settings_target": wire.replace("workspace_i18n", "personal_appearance")})
    with pytest.raises(ValidationError):
        ChatSendRequest(workspace_id="ws_1", planning_mode="planning", client_info={"configuration_settings_target": wire})


async def test_shared_resume_constructor_cannot_restore_settings_authority():
    from bowhead_core.configuration_settings_target import validate_settings_target
    from bowhead_gateway.turn_service import build_resume_turn_request

    clarification = SimpleNamespace(
        request_id="clar_original",
        turn_id="t_original",
        user_id="u_1",
        workspace_id="ws_1",
        token="",
        chosen_values=[],
        chosen_value=None,
        resume_context={"user_text": "Change week start", "session_id": "s_original", "configuration_settings_target": {"requires_new_send": True}},
    )
    request = build_resume_turn_request(clarification)
    assert request is not None and request.configuration_settings_target is not None
    assert (await validate_settings_target(request, "workspace_i18n"))["error"] == "configuration_target_not_admitted"


@pytest.mark.parametrize("value", [False, 1, "true"])
def test_send_readiness_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        target(saved_ready=value)


async def test_actual_send_rejects_a_foreign_selection_before_creating_a_turn(request_ctx, monkeypatch):
    from bowhead_gateway.routes import auth, chat

    async def resolve(request):
        return "u_1", "team_1"

    monkeypatch.setattr(auth, "resolve_user_or_stub", resolve)
    monkeypatch.setattr(chat, "_get_orchestrator", lambda request: SimpleNamespace())
    body = ChatSendRequest(
        workspace_id="ws_1",
        message="Change the selected preferences",
        client_info={"configuration_settings_target": target(workspace_id="ws_other").model_dump_json()},
    )
    with pytest.raises(HTTPException) as denial:
        await chat._send_impl(body, request_ctx, Response())
    assert denial.value.status_code == 403
    assert denial.value.detail["error"]["admission"] == "not_started"
