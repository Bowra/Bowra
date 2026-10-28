"""The workbench lists draft changes by source; Coach's changes are undone and reapplied per field."""

import sqlite3

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore, AgentProposalBaseline
from test_agent_coach_proposals import working_agent
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit

PROMPT = "/definition/system_prompt"


async def _coach_write(db_path, target, value):
    _, version, _ = await AgentChangeProposalStore(str(db_path)).create_applied(
        team_id="team_1",
        agent_id="osa_1",
        effective_owner_user_id="user_1",
        source_ref="coach:turn_owner:edit",
        operations=[{"op": "replace", "path": PROMPT, "value": value}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
        expected_baseline=AgentProposalBaseline(target["working_copy_id"], target["version"], target["base_release_id"]),
        rationale="Clarify",
    )
    return version


async def test_coach_change_is_listed_undone_and_reapplied_through_the_workbench(app_client):
    client, db_path = app_client
    target = await working_agent(client, db_path)
    version = await _coach_write(db_path, target, "Coach instructions.")
    workbench = (await client.get("/api/v1/agents/osa_1/version-workbench")).json()
    # Members' shared thumbs-downs join Pending through the workbench; there are none here.
    assert workbench["member_feedback"] == []
    rows = {row["path"]: row for row in workbench["draft_changes"]}
    prompt = rows[PROMPT]
    assert prompt["source"] == "coach" and prompt["draft"] == "Coach instructions."
    assert prompt["coach"]["state"] == "applied" and prompt["coach"]["before"] == "Initial private instructions."
    assert rows["/listing/description"]["source"] == "manual" and "coach" not in rows["/listing/description"]

    body = {"fields": [PROMPT], "expected_working_copy_row_version": version}
    undone = await client.post("/api/v1/agents/osa_1/coach-changes/undo", json=body)
    assert undone.status_code == 200, undone.text
    assert undone.json()["working_copy"]["definition"]["system_prompt"] == "Initial private instructions."
    stale = await client.post("/api/v1/agents/osa_1/coach-changes/reapply", json=body)
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "agent_working_copy_version_conflict"
    redo = await client.post(
        "/api/v1/agents/osa_1/coach-changes/reapply", json={**body, "expected_working_copy_row_version": undone.json()["working_copy"]["row_version"]}
    )
    assert redo.status_code == 200 and redo.json()["working_copy"]["definition"]["system_prompt"] == "Coach instructions."
    unknown = await client.post(
        "/api/v1/agents/osa_1/coach-changes/undo",
        json={"fields": ["/listing/description"], "expected_working_copy_row_version": redo.json()["working_copy"]["row_version"]},
    )
    assert unknown.status_code == 404 and unknown.json()["detail"]["error"]["code"] == "agent_coach_change_not_found"


async def test_coach_undo_after_a_manual_edit_returns_the_three_values(app_client):
    client, db_path = app_client
    target = await working_agent(client, db_path)
    version = await _coach_write(db_path, target, "Coach instructions.")
    manual = await client.patch(
        "/api/v1/agents/osa_1/working-copy",
        json={
            "definition": {"system_prompt": "Owner instructions.", "tool_refs": [], "skill_grants": [], "max_iterations": 10},
            "listing": {"display_name": "Finance Helper", "description": "Owner draft"},
            "audience": {"schema_version": 1, "effective_mode": "private", "user_ids": [], "group_ids": []},
            "expected_row_version": version,
        },
    )
    assert manual.status_code == 200, manual.text
    conflict = await client.post(
        "/api/v1/agents/osa_1/coach-changes/undo",
        json={"fields": [PROMPT], "expected_working_copy_row_version": manual.json()["row_version"]},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["error"] == {
        "code": "agent_change_undo_conflict",
        "details": {
            "conflicts": [
                {"path": PROMPT, "before": "Initial private instructions.", "coach": "Coach instructions.", "current": "Owner instructions."}
            ]
        },
    }
    rows = {row["path"]: row for row in (await client.get("/api/v1/agents/osa_1/version-workbench")).json()["draft_changes"]}
    assert rows[PROMPT]["source"] == "manual" and rows[PROMPT]["coach"]["conflict"]

    kept = await client.post(
        "/api/v1/agents/osa_1/coach-changes/keep",
        json={"fields": [PROMPT], "expected_working_copy_row_version": manual.json()["row_version"]},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["working_copy"]["row_version"] == manual.json()["row_version"]
    rows = {row["path"]: row for row in (await client.get("/api/v1/agents/osa_1/version-workbench")).json()["draft_changes"]}
    assert rows[PROMPT]["source"] == "manual" and "coach" not in rows[PROMPT]


async def test_another_owners_agent_cannot_be_undone(app_client):
    client, db_path = app_client
    target = await working_agent(client, db_path)
    version = await _coach_write(db_path, target, "Coach instructions.")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    denied = await client.post("/api/v1/agents/osa_1/coach-changes/undo", json={"fields": [PROMPT], "expected_working_copy_row_version": version})
    assert denied.status_code == 404
