"""Coach suggestions preserve owner scope, human edits and exact recovery."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.subagent_draft_store import DraftClaimOutcome, SubagentDraftVersionConflictError
from bowhead_core.user_store import _now
from bowhead_gateway.agent_coach_draft_adapter import AgentCoachDraftAdapter
from bowhead_gateway.subagent_draft_chat_editor import SubagentDraftChatEditor
from starlette.requests import Request
from test_team_subagent_drafts_routes import _client, _run, _setup

pytestmark = pytest.mark.unit


@pytest.fixture
def saved_draft(tmp_path, monkeypatch):
    users, store, schemas, owner, other = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "1")
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT_PCT", "100")
    now = _now()
    with sqlite3.connect(users.db_path) as db:
        db.execute(
            "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", ("ws_a", owner.team_id, "Authoring Space", now, now)
        )
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)", ("ws_a", owner.id, "member", now))
    draft = _run(
        store.create_manual_draft(
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            locale="en-US",
            purpose="Read provided webpages",
            idempotency_key="initial-draft",
        )
    )
    definition = {
        "name": "evidence-reader",
        "display_name": "Evidence Reader",
        "description": "Read supplied webpages.",
        "system_prompt": "Use only supplied evidence.",
        "tool_refs": ["web_fetch"],
        "skill_grants": [],
        "capability_ids": [],
        "max_iterations": 8,
        "model": None,
        "output_schema": None,
        "output_schema_text": "",
        "visibility": "creator_only",
    }
    draft = _run(
        store.save_manual_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            expected_version=draft.row_version,
            purpose=draft.purpose_text,
            definition=definition,
            text_only_confirmed=False,
        )
    )
    with _client(users, store, schemas, owner) as client:
        request = Request({"type": "http", "app": client.app, "headers": []})
        adapter = AgentCoachDraftAdapter(SubagentDraftChatEditor(request, owner, draft.id, draft.row_version, workspace_id="ws_a"))
        yield adapter, store, users, draft, owner, other, client


def _suggestions(fixture):
    _, store, _, draft, owner, _, _ = fixture
    return _run(store.list_canvas_suggestions(draft.id, team_id=owner.team_id, owner_user_id=owner.id, workspace_id="ws_a"))


def _saved(fixture):
    _, store, _, draft, owner, _, _ = fixture
    return _run(store.get_draft(draft.id, team_id=owner.team_id, owner_user_id=owner.id))


def test_inspection_discovery_and_validation_do_not_mutate(saved_draft):
    adapter, _, _, draft, _, _, _ = saved_draft
    inspected = _run(adapter.inspect())
    assert inspected["ok"] and inspected["version"] == draft.row_version
    assert "visibility" not in inspected["patch_schema"]["properties"]
    tools = _run(adapter.tools(query="web", offset=0, limit=2))
    assert tools["ok"] and 0 < len(tools["items"]) <= 2
    assert all(not item["stable_ref"].startswith("agent_authoring_") for item in tools["items"])
    invalid = _run(adapter.validate({"name": "INVALID NAME"}))
    assert invalid["validation_status"] == "invalid", invalid
    valid = _run(adapter.validate({"description": "Read webpages and cite the evidence."}))
    assert valid["validation_status"] in {"valid", "warning"}, valid
    assert invalid["message"].startswith("Validation failed; no suggestion was submitted.")
    assert valid["message"].startswith("Candidate validation completed; no suggestion was submitted.")
    for result in (valid, invalid):
        assert "This call only validated the candidate; it did not attempt to save a suggestion" in result["message"]
    assert not valid["persisted"] and not valid["executed"]
    report = valid["validation_report"]
    assert report["checks"]["dependencies"]["status"] == "passed"
    assert report["checks"]["dependencies"]["fingerprint"]
    assert report["checks"]["business_trial"]["status"] == "not_run" and report["execution_id"] is None
    assert report["trial_support"]["status"] == "no_standalone_executor"
    assert invalid["validation_report"]["checks"]["structure"]["status"] == "failed"
    assert invalid["validation_report"]["checks"]["dependencies"]["status"] == "passed"
    missing = _run(adapter.validate({"tool_refs": ["missing_tool"]}))
    assert missing["validation_report"]["checks"]["dependencies"]["status"] == "blocked"
    assert missing["validation_report"]["candidate_hash"] != report["candidate_hash"]
    assert _saved(saved_draft).definition == draft.definition and not _suggestions(saved_draft)


def test_validate_saved_content_needs_no_dummy_edit_and_keeps_original_version(saved_draft):
    adapter, _, _, draft, _, _, _ = saved_draft
    result = _run(adapter.validate({}))
    assert result["ok"] and not result["persisted"] and not result["executed"]
    assert result["validation_report"]["original_target"] == {"kind": "agent_draft", "id": draft.id, "version": draft.row_version}
    assert result["validation_report"]["checks"]["business_trial"]["status"] == "not_run"
    assert _saved(saved_draft).definition == draft.definition and not _suggestions(saved_draft)
    stale = AgentCoachDraftAdapter(replace(adapter.editor, expected_version=draft.row_version + 1))
    assert not _run(stale.validate({}))["ok"]
    assert not _run(adapter.propose({}, request_id="empty", turn_id="t_empty"))["ok"]
    assert not _suggestions(saved_draft)


def test_incomplete_draft_does_not_turn_a_name_edit_into_a_tool_binding(saved_draft, monkeypatch):
    adapter, store, _, draft, owner, _, _ = saved_draft
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "0")
    incomplete = _run(
        store.save_manual_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            expected_version=draft.row_version,
            purpose=draft.purpose_text,
            definition={**draft.definition, "tool_refs": []},
            text_only_confirmed=False,
        )
    )
    adapter = AgentCoachDraftAdapter(replace(adapter.editor, expected_version=incomplete.row_version))
    patch = {"display_name": "Focused name edit"}
    validation = _run(adapter.validate(patch))
    proposal = _run(adapter.propose(patch, request_id="name-only", turn_id="t_name_only"))
    assert validation["validation_status"] == "invalid"
    assert proposal["error"] == "proposal_invalid"
    assert "This call only validated" in validation["message"]
    assert "This call only validated" not in proposal["message"]
    for result in (validation, proposal):
        assert any(issue["code"] == "subagent_tool_required" for issue in result["issues"])
        assert "unchanged fields" in result["message"] and "ask for that additional scope" in result["message"]
        assert "invalid candidate cannot be saved as a pending suggestion" in result["message"]
        assert "User confirmation cannot bypass validation" in result["message"]
    assert _saved(saved_draft).definition == incomplete.definition
    assert _saved(saved_draft).row_version == incomplete.row_version
    assert not _suggestions(saved_draft)


def test_validation_does_not_erase_an_earlier_saved_suggestion(saved_draft):
    adapter, _, _, draft, _, _, _ = saved_draft
    saved = _run(adapter.propose({"description": "Cite supplied evidence."}, request_id="saved", turn_id="t_saved"))
    assert saved["ok"]
    proposals = _suggestions(saved_draft)
    result = _run(adapter.validate({"name": "INVALID NAME"}))
    assert result["validation_status"] == "invalid"
    assert "This call only validated the candidate; it did not attempt to save a suggestion" in result["message"]
    assert _suggestions(saved_draft) == proposals
    assert _saved(saved_draft).definition == draft.definition
    assert _run(adapter.get_proposal(saved["proposal"]["id"]))["proposal"] == saved["proposal"]


def test_inspection_recovers_pending_proposal_identity_without_applying_it(saved_draft):
    adapter, store, _, draft, owner, other, _ = saved_draft
    saved = _run(adapter.propose({"description": "Cite supplied evidence."}, request_id="original", turn_id="t_original"))
    assert saved["ok"]
    proposal = saved["proposal"]
    inspected = _run(adapter.inspect())
    assert inspected["pending_proposals"] == [
        {"id": proposal["id"], "kind": "definition", "base_version": draft.row_version, "created_at": proposal["created_at"]}
    ]
    assert not inspected["pending_proposals_truncated"]
    assert inspected["definition"] == draft.definition
    assert inspected["patch_schema"]["required"] == []
    recovered = _run(adapter.get_proposal(inspected["pending_proposals"][0]["id"]))
    assert recovered["proposal"] == proposal and not recovered["applied"]
    unauthorized = AgentCoachDraftAdapter(replace(adapter.editor, user=other))
    assert _run(unauthorized.inspect()) == {"ok": False, "error": "draft_unavailable_or_changed"}
    _run(store.dismiss_canvas_suggestion(draft.id, proposal["id"], team_id=owner.team_id, owner_user_id=owner.id, workspace_id="ws_a"))
    assert _run(adapter.inspect())["pending_proposals"] == []
    assert _saved(saved_draft).definition == draft.definition


def test_empty_draft_inspection_explains_required_candidate_fields(saved_draft):
    adapter, store, _, _, owner, _, _ = saved_draft
    draft = _run(
        store.create_manual_draft(
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            locale="en-US",
            purpose="A fresh candidate",
            idempotency_key="empty-inspection",
        )
    )
    empty = AgentCoachDraftAdapter(replace(adapter.editor, draft_id=draft.id, expected_version=draft.row_version))
    inspected = _run(empty.inspect())
    assert inspected["ok"] and inspected["definition"] is None
    assert set(inspected["patch_schema"]["required"]) == {"name", "display_name", "description", "system_prompt", "tool_refs", "max_iterations"}
    assert inspected["pending_proposals"] == []


def test_coach_review_rejects_a_business_only_output_schema(saved_draft):
    adapter, _, _, draft, _, _, _ = saved_draft
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"revenue": {"type": "string"}, "profit": {"type": "string"}, "profit_margin": {"type": "string"}},
        "required": ["revenue", "profit", "profit_margin"],
    }
    checked = _run(adapter.validate({"output_schema": schema}))
    assert checked["validation_report"]["checks"]["structure"]["status"] == "failed", checked
    assert any(issue["code"] == "subagent_output_schema_invalid" for issue in checked["issues"])
    assert "internal AnalysisPayload" in _run(adapter.inspect())["patch_schema"]["properties"]["output_schema"]["description"]
    cleared = _run(adapter.validate({"output_schema": None}))
    assert cleared["validation_report"]["checks"]["structure"]["status"] != "failed"
    assert _saved(saved_draft).definition == draft.definition and not _suggestions(saved_draft)


def test_sparse_proposal_replays_exactly_then_user_applies_through_existing_route(saved_draft):
    adapter, _, users, draft, owner, _, client = saved_draft
    patch = {"description": "Read webpages and cite evidence."}
    receipt = _run(adapter.propose(patch, request_id="one", turn_id="t_original"))
    assert receipt["ok"] and receipt["effect"] == "suggestion_saved", receipt
    assert not receipt["applied"] and receipt["proposal"]["status"] == "pending"
    suggestion = receipt["proposal"]
    assert suggestion["payload"]["definition"] == {**draft.definition, **patch}
    assert suggestion["payload"]["changed_fields"] == ["description"]
    replay = _run(adapter.propose(patch, request_id="one", turn_id="t_original"))
    assert replay["replayed"] and replay["proposal"]["id"] == suggestion["id"]
    assert len(_suggestions(saved_draft)) == 1
    assert _saved(saved_draft).definition == draft.definition
    assert _run(users.get_team_subagent_by_name(owner.team_id, draft.definition["name"], only_enabled=False)) is None
    applied = client.post(
        f"/api/v1/team-subagents/drafts/{draft.id}/suggestions/{suggestion['id']}/apply",
        headers={
            "X-Space-Id": "ws_a",
            "If-Match": client.get(f"/api/v1/team-subagents/drafts/{draft.id}?schema_version=2", headers={"X-Space-Id": "ws_a"}).json()["etag"],
        },
        json={"schema_version": 2, "selected_fields": ["description"], "mutation_id": "11111111-1111-4111-8111-111111111111"},
    )
    assert applied.status_code == 200, applied.text
    current = _saved(saved_draft)
    assert current.definition == {**draft.definition, **patch}
    assert current.row_version == draft.row_version + 1
    read = _run(adapter.get_proposal(suggestion["id"]))
    assert read["applied"] and read["proposal"]["status"] == "applied"
    assert _run(adapter.propose(patch, request_id="one", turn_id="t_original"))["proposal"]["id"] == suggestion["id"]


def test_changed_intent_conflicts_and_stale_baseline_cannot_save(saved_draft):
    adapter, store, _, draft, owner, _, _ = saved_draft
    original = _run(adapter.propose({"description": "First suggestion."}, request_id="one", turn_id="t_original"))
    assert original["ok"], original
    conflict = _run(adapter.propose({"description": "Different intent."}, request_id="one", turn_id="t_original"))
    assert conflict["error"] == "idempotency_key_reused"
    _run(
        store.save_manual_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            expected_version=draft.row_version,
            purpose=draft.purpose_text,
            definition={**draft.definition, "description": "Human edit."},
            text_only_confirmed=False,
        )
    )
    for rejected in (
        _run(adapter.inspect()),
        _run(adapter.validate({"description": "Stale replacement."})),
        _run(adapter.propose({"description": "Stale replacement."}, request_id="two", turn_id="t_original")),
    ):
        assert not rejected["ok"] and rejected["retry"] == "new_user_turn"
        assert "server-bound baseline cannot be refreshed" in rejected["message"]
        assert "does not prove that local edits are unsaved" in rejected["message"]
        assert "If there are no local edits, review and send again without an extra save" in rejected["message"]
        assert "does not waive validation" in rejected["message"]
    replay = _run(adapter.propose({"description": "First suggestion."}, request_id="one", turn_id="t_original"))
    assert replay["proposal"]["id"] == original["proposal"]["id"]
    assert _saved(saved_draft).definition["description"] == "Human edit." and len(_suggestions(saved_draft)) == 1


def test_late_save_conflict_reports_frozen_baseline_and_preserves_newer_input(saved_draft, monkeypatch):
    adapter, store, _, draft, owner, _, _ = saved_draft
    original_write = store.record_canvas_suggestion

    async def save_after_manual_change(*args, **kwargs):
        await store.save_manual_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            expected_version=draft.row_version,
            purpose=draft.purpose_text,
            definition={**draft.definition, "description": "Newer manual work."},
            text_only_confirmed=False,
        )
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(store, "record_canvas_suggestion", save_after_manual_change)
    rejected = _run(adapter.propose({"display_name": "Old baseline name"}, request_id="late-write", turn_id="t_original"))
    assert rejected["error"] == "proposal_rejected" and rejected["retry"] == "new_user_turn"
    assert _saved(saved_draft).definition == {**draft.definition, "description": "Newer manual work."}
    assert not _suggestions(saved_draft)


@pytest.mark.parametrize(
    "patch",
    [
        {"visibility": "team"},
        {"draft_id": "sad_other"},
        {"max_iterations": "4"},
        {"tool_refs": ["agent_authoring_propose"]},
        {"tool_refs": ["missing_tool"]},
        {"name": "INVALID NAME"},
    ],
)
def test_invalid_candidate_cannot_be_saved(saved_draft, patch):
    adapter = saved_draft[0]
    assert not _run(adapter.propose(patch, request_id="invalid", turn_id="t_original"))["ok"]
    assert not _suggestions(saved_draft)


@pytest.mark.parametrize("change", ["owner", "space", "dirty", "disabled_user", "revoked_team", "revoked_space", "disabled_team", "draft_feature"])
def test_fresh_authority_is_required_for_each_operation(saved_draft, monkeypatch, change):
    adapter, _, users, _, _, other, _ = saved_draft
    assert _run(adapter.inspect())["ok"]
    if change == "owner":
        adapter = AgentCoachDraftAdapter(replace(adapter.editor, user=other))
    elif change == "space":
        adapter = AgentCoachDraftAdapter(replace(adapter.editor, workspace_id="ws_other"))
    elif change == "dirty":
        adapter = AgentCoachDraftAdapter(replace(adapter.editor, has_unsaved_local_changes=True))
    elif change == "draft_feature":
        monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "0")
    else:
        table = {"disabled_user": "users", "revoked_team": "team_members", "revoked_space": "workspace_members", "disabled_team": "teams"}[change]
        with sqlite3.connect(users.db_path) as db:
            db.execute(f"UPDATE {table} SET status = ?", ("removed" if change in {"revoked_team", "revoked_space"} else "disabled",))
    assert not _run(adapter.inspect())["ok"]
    assert not _run(adapter.propose({"description": "Blocked."}, request_id="blocked", turn_id="t_original"))["ok"]
    assert not _suggestions(saved_draft)


def test_store_compare_and_swap_rejects_race_after_validation(saved_draft, monkeypatch):
    adapter, store, _, draft, owner, _, _ = saved_draft
    record = store.record_canvas_suggestion

    async def race(*args, **kwargs):
        await store.save_manual_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            expected_version=draft.row_version,
            purpose=draft.purpose_text,
            definition={**draft.definition, "description": "Concurrent human edit."},
            text_only_confirmed=False,
        )
        return await record(*args, **kwargs)

    monkeypatch.setattr(store, "record_canvas_suggestion", race)
    result = _run(adapter.propose({"description": "Old proposal."}, request_id="race", turn_id="t_original"))
    assert not result["ok"] and not _suggestions(saved_draft)
    assert _saved(saved_draft).definition["description"] == "Concurrent human edit."


def test_exact_lookup_does_not_return_other_draft_proposal(saved_draft):
    adapter, store, _, _, owner, _, _ = saved_draft
    original = _run(adapter.propose({"description": "Suggested."}, request_id="one", turn_id="t_original"))
    assert original["ok"], original
    other = _run(
        store.create_manual_draft(
            team_id=owner.team_id, owner_user_id=owner.id, workspace_id="ws_a", locale="en-US", purpose="Other draft", idempotency_key="other-draft"
        )
    )
    other_adapter = AgentCoachDraftAdapter(replace(adapter.editor, draft_id=other.id, expected_version=other.row_version))
    assert _run(other_adapter.get_proposal(original["proposal"]["id"]))["error"] == "proposal_not_found"


def test_store_strict_base_rejection_keeps_request_recoverable(saved_draft):
    _, store, _, draft, owner, _, _ = saved_draft
    claim = _run(store.claim_request(team_id=owner.team_id, owner_user_id=owner.id, idempotency_key="cas-test-request", request={"draft": draft.id}))
    with pytest.raises(SubagentDraftVersionConflictError):
        _run(
            store.record_canvas_suggestion(
                draft.id,
                team_id=owner.team_id,
                owner_user_id=owner.id,
                workspace_id="ws_a",
                request_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                base_version=draft.row_version - 1,
                require_current_base=True,
                kind="definition",
                payload={"definition": draft.definition},
            )
        )
    assert not _suggestions(saved_draft)
    pending = _run(
        store.claim_request(team_id=owner.team_id, owner_user_id=owner.id, idempotency_key="cas-test-request", request={"draft": draft.id})
    )
    assert pending.outcome is DraftClaimOutcome.IN_PROGRESS


def test_lost_success_response_recovers_committed_suggestion(saved_draft, monkeypatch):
    adapter, store, _, _, _, _, _ = saved_draft
    record = store.record_canvas_suggestion

    async def lose_response(*args, **kwargs):
        await record(*args, **kwargs)
        raise ConnectionError("Response lost after commit")

    monkeypatch.setattr(store, "record_canvas_suggestion", lose_response)
    with pytest.raises(ConnectionError):
        _run(adapter.propose({"description": "Recovered suggestion."}, request_id="lost", turn_id="t_original"))
    saved = _suggestions(saved_draft)
    assert len(saved) == 1
    recovered = _run(adapter.propose({"description": "Recovered suggestion."}, request_id="lost", turn_id="t_original"))
    assert recovered["replayed"] and recovered["proposal"]["id"] == saved[0].id
    assert len(_suggestions(saved_draft)) == 1


def test_permission_change_during_validation_prevents_proposal(saved_draft, monkeypatch):
    from bowhead_gateway import agent_coach_draft_adapter as module

    adapter, _, users, _, _, _, _ = saved_draft
    review = module._review_update

    async def revoke(**kwargs):
        result = await review(**kwargs)
        with sqlite3.connect(users.db_path) as db:
            db.execute("UPDATE workspace_members SET status = 'removed'")
        return result

    monkeypatch.setattr(module, "_review_update", revoke)
    result = _run(adapter.propose({"description": "Unpermitted suggestion."}, request_id="revoked", turn_id="t_original"))
    assert not result["ok"] and not _suggestions(saved_draft)


def test_draft_chat_admission_checks_saved_version_scope_and_current_access(saved_draft, monkeypatch):
    from bowhead_core.agent_draft_send_target import AgentDraftSendTarget
    from fastapi import HTTPException

    from bowhead_gateway import agent_draft_send_target as admission

    adapter, _, users, draft, owner, other, _ = saved_draft
    request = adapter.editor.request

    async def current_user(_request):
        return owner

    monkeypatch.setattr(admission, "_current_user", current_user)
    target = AgentDraftSendTarget(id=draft.id, version=draft.row_version)

    def admit(**overrides):
        return _run(
            admission.admit_agent_draft_target(
                request, overrides.pop("target", target), **{"user_id": owner.id, "team_id": owner.team_id, "workspace_id": "ws_a", **overrides}
            )
        )

    editor = admit()
    assert editor.draft_id == draft.id and editor.expected_version == draft.row_version
    for overrides in (
        {"workspace_id": "ws_other"},
        {"team_id": "team_other"},
        {"user_id": other.id},
        {"target": AgentDraftSendTarget(id=draft.id, version=draft.row_version + 1)},
    ):
        with pytest.raises(HTTPException) as error:
            admit(**overrides)
        assert error.value.status_code == 409
        assert error.value.detail["error"] == {"code": "agent_draft_target_unavailable", "admission": "not_started"}
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "0")
    with pytest.raises(HTTPException):
        admit()
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "1")
    with sqlite3.connect(users.db_path) as db:
        db.execute("DELETE FROM workspace_members WHERE workspace_id='ws_a' AND user_id=?", (owner.id,))
    with pytest.raises(HTTPException):
        admit()
    assert _saved(saved_draft).definition == draft.definition
