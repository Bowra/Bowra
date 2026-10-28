"""Selected resource sends retain original domain baselines and current scope."""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.coach_canvas_target import COACH_CANVAS_TARGET_KEY, CoachCanvasSendTarget, coach_canvas_projection
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes.chat import ChatSendRequest
from fastapi import HTTPException
from pydantic import ValidationError
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_editors import original_personal_path as original_personal_path
from test_agent_coach_editors import personal
from test_agent_coach_resources import resources as resources
from test_agent_coach_skills import skill_adapter as skill_adapter
from test_agent_coach_working_copy import live_agent as live_agent
from test_team_subagent_drafts_routes import _run

from bowhead_gateway import coach_canvas_target as adapter

pytestmark = pytest.mark.integration


@pytest.fixture
def original_scope(resources, monkeypatch):
    session = _run(
        SessionStore(resources.store.db_path).ensure_session(session_id=None, workspace_id=resources.workspace_id, user_id=resources.user.id)
    )
    resource = replace(resources, session_id=session.id)

    async def current_user(request):
        return resources.user

    monkeypatch.setattr(adapter, "_current_user", current_user)
    return resource


def selection(resource, target, **updates):
    return CoachCanvasSendTarget.model_validate(
        {
            "schema_version": 1,
            "user_id": resource.user.id,
            "team_id": resource.user.team_id,
            "workspace_id": resource.workspace_id,
            "session_id": resource.session_id,
            "target": target,
            "dirty": False,
            "pending_write": False,
            **updates,
        }
    )


def admit(resource, target):
    return _run(
        adapter.admit_coach_canvas_target(
            resource.request,
            target,
            user_id=resource.user.id,
            team_id=resource.user.team_id,
            workspace_id=resource.workspace_id,
            session_id=resource.session_id,
        )
    )


def test_personal_selection_keeps_original_package_and_denies_replaced_identity(original_scope, skill_adapter):
    reference = personal(original_scope, skill_adapter)
    target = selection(original_scope, reference)
    admitted = admit(original_scope, target)
    snapshot = _run(replace(original_scope, canvas_target=admitted).inspect())
    assert snapshot["selected_editor"]["authoring_target"] == {
        "kind": "personal_skill",
        "id": reference["id"],
        "name": "coached-method",
        "revision": reference["revision"],
    }
    assert coach_canvas_projection(admitted)[COACH_CANVAS_TARGET_KEY] == target.model_dump()
    with sqlite3.connect(original_scope.store.db_path) as db:
        db.execute("UPDATE skill_instances SET id='sk_replacement' WHERE id=?", (reference["id"],))
    with pytest.raises(HTTPException) as denied:
        _run(admitted.recheck())
    assert denied.value.detail["error"]["admission"] == "not_started"


def test_agent_selection_admits_absence_but_never_creates_or_absorbs_another_working_copy(original_scope, live_agent):
    identity, live = live_agent
    from bowhead_core.agent_release_read_store import AgentReleaseReadStore

    detail = _run(
        AgentReleaseReadStore(original_scope.store.db_path).working_copy(
            team_id=original_scope.user.team_id, agent_id=identity, actor_user_id=original_scope.user.id
        )
    )
    target = selection(
        original_scope,
        {"kind": "agent", "id": identity, "version": detail["aggregate_row_version"], "live_release_id": live["release_id"], "working_copy": None},
    )
    admitted = admit(original_scope, target)
    assert _run(admitted.recheck())["authoring_target"] is None
    opened = _run(original_scope.authoring.open_working_copy(agent_id=identity, expected_live_release_id=live["release_id"]))
    with pytest.raises(HTTPException):
        _run(admitted.recheck())
    current = target.model_dump()
    copy = opened["target"]
    current["target"]["working_copy"] = {"id": copy["working_copy_id"], "version": copy["version"], "base_release_id": copy["base_release_id"]}
    current_admission = admit(original_scope, CoachCanvasSendTarget.model_validate(current))
    assert _run(current_admission.recheck())["authoring_target"] == copy
    with sqlite3.connect(original_scope.store.db_path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
    with pytest.raises(HTTPException):
        _run(current_admission.recheck())


def test_plugin_selection_checks_original_row_version_and_current_admin_scope(original_scope):
    from test_agent_coach_plugins import administrator, create

    administrator(original_scope)
    result = create(original_scope)
    original = result["result_target"]
    selected = selection(original_scope, {**original, "kind": "plugin"})
    admitted = admit(original_scope, selected)
    assert _run(admitted.recheck())["authoring_target"] == original
    with sqlite3.connect(original_scope.store.db_path) as db:
        db.execute("UPDATE team_capability SET row_version=row_version+1 WHERE id=?", (original["id"],))
    with pytest.raises(HTTPException):
        _run(admitted.recheck())


@pytest.mark.parametrize(
    "changes", [{"dirty": True}, {"pending_write": True}, {"user_id": "u_other"}, {"workspace_id": "ws_other"}, {"session_id": "s_other"}]
)
def test_foreign_and_unsaved_selections_are_rejected_before_resource_access(original_scope, changes):
    target = selection(original_scope, {"kind": "skill", "id": "sk_unread", "source_kind": "user", "revision": "sha256:" + "a" * 64}, **changes)
    with pytest.raises(HTTPException) as denied:
        admit(original_scope, target)
    assert denied.value.status_code == 409
    assert denied.value.detail["error"]["admission"] == "not_started"


def test_selected_scope_is_rechecked_after_admission(original_scope, skill_adapter):
    admitted = admit(original_scope, selection(original_scope, personal(original_scope, skill_adapter)))
    with sqlite3.connect(original_scope.store.db_path) as db:
        db.execute("UPDATE workspace_members SET status='removed'")
    with pytest.raises(HTTPException):
        _run(admitted.recheck())


def test_schema_and_pause_keep_a_restriction_without_restoring_authority(original_scope):
    from bowhead_gateway.turn_service import build_resume_turn_request

    selected = selection(original_scope, {"kind": "skill", "id": "sk_original", "source_kind": "user", "revision": "sha256:" + "a" * 64})
    wire = {COACH_CANVAS_TARGET_KEY: selected.model_dump_json()}
    request = ChatSendRequest(workspace_id=original_scope.workspace_id, session_id=original_scope.session_id, client_info=wire)
    assert request.client_info == wire
    for extra in (
        {"planning_mode": "planning"},
        {"submission_kind": "replay"},
        {"client_info": {**wire, "agent_draft_id": "sad_other", "agent_draft_version": "1"}},
    ):
        with pytest.raises(ValidationError):
            ChatSendRequest.model_validate({"workspace_id": original_scope.workspace_id, "client_info": wire, **extra})
    clarification = SimpleNamespace(
        request_id="clar_original",
        turn_id="t_original",
        user_id=original_scope.user.id,
        workspace_id=original_scope.workspace_id,
        token="",
        chosen_values=[],
        chosen_value=None,
        resume_context={
            "user_text": "Edit the selected Skill",
            "session_id": original_scope.session_id,
            COACH_CANVAS_TARGET_KEY: {"requires_new_send": True},
        },
    )
    resumed = build_resume_turn_request(clarification)
    assert resumed.coach_canvas_target == "coach_canvas_target_requires_new_send"
    assert coach_canvas_projection(resumed.coach_canvas_target) == {}


def test_coach_preparation_requires_the_original_admission_and_scope(original_scope, skill_adapter):
    from bowhead_core.agent_coach import prepare_agent_coach
    from bowhead_core.tool_registry import ToolExecutionContext

    admitted = admit(original_scope, selection(original_scope, personal(original_scope, skill_adapter)))
    scoped = replace(original_scope, canvas_target=admitted)
    context = ToolExecutionContext(
        role="lead",
        member_uid=scoped.user.id,
        user_id=scoped.user.id,
        team_id=scoped.user.team_id,
        workspace_id=scoped.workspace_id,
        session_id=scoped.session_id,
        turn_id="t_original",
        datarc=None,
    )
    context.agent_coach_resources = scoped
    context.coach_canvas_target = admitted
    child, result = _run(prepare_agent_coach(context))
    assert child is not None and result["ok"]
    context.session_id = "s_other"
    child, result = _run(prepare_agent_coach(context))
    assert child is None and not result["ok"]
    context.session_id = scoped.session_id
    context.coach_canvas_target = admitted.target.model_dump()
    child, result = _run(prepare_agent_coach(context))
    assert child is None and result["error"] == "coach_canvas_target_requires_new_send"
