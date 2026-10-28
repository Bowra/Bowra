"""Coach can establish an exact Owner baseline without replacing later edits."""

import json
import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.agent_release_read_store import AgentReleaseReadStore
from bowhead_core.agent_revision_store import AgentRevisionConflictError, AgentRevisionForbiddenError
from bowhead_gateway.agent_coach_authoring import AgentCoachAuthoring
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_draft_bindings import accept, activate, create, review
from test_agent_coach_resources import resources as resources
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.integration


@pytest.fixture
def live_agent(resources, saved_draft, monkeypatch):
    activate(resources)
    url, applied = accept(saved_draft, {"description": "Read supplied evidence."})
    response = create(saved_draft[-1], url, review(saved_draft[-1], url, applied))
    assert response.status_code == 201
    identity = response.json()["definition_id"]
    detail = _run(
        AgentReleaseReadStore(resources.store.db_path).working_copy(
            team_id=resources.user.team_id, agent_id=identity, actor_user_id=resources.user.id
        )
    )
    assert detail["working_copy"] is None
    return identity, detail["live"]


def test_original_release_baseline_and_repeated_open_preserve_newer_edits(resources, live_agent):
    identity, live = live_agent
    args = {"agent_id": identity, "expected_live_release_id": live["release_id"]}
    first = _run(resources.authoring.open_working_copy(**args))
    assert first["created_baseline"] and not first["executed"] and not first["released"]
    with sqlite3.connect(resources.store.db_path) as db:
        original = db.execute("SELECT definition_json,listing_json,audience_json FROM team_subagent_working_copy").fetchone()
        assert tuple(map(json.loads, original)) == (live["definition"], live["listing"], live["audience"])
        changed = {**live["definition"], "system_prompt": "Newer Owner input."}
        db.execute("UPDATE team_subagent_working_copy SET definition_json=?,row_version=row_version+1", (json.dumps(changed),))
    again = _run(resources.authoring.open_working_copy(**args))
    assert not again["created_baseline"] and again["target"]["working_copy_id"] == first["target"]["working_copy_id"]
    assert again["target"]["version"] == first["target"]["version"] + 1
    with sqlite3.connect(resources.store.db_path) as db:
        assert json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy").fetchone()[0]) == changed
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 1
        assert db.execute("SELECT live_release_id FROM team_subagent WHERE id=?", (identity,)).fetchone()[0] == live["release_id"]


def test_stale_release_and_other_owner_cannot_establish_a_baseline(resources, live_agent, saved_draft):
    identity, live = live_agent
    # A real stored release makes this a stale-baseline check, not an FK failure.
    with sqlite3.connect(resources.store.db_path) as db:
        db.row_factory = sqlite3.Row
        stale = dict(db.execute("SELECT * FROM team_subagent_release WHERE id=?", (live["release_id"],)).fetchone())
        stale["id"] = "arl_stale_baseline"
        stale["release_sequence"] += 1
        columns = ",".join(stale)
        placeholders = ",".join("?" for _ in stale)
        db.execute(f"INSERT INTO team_subagent_release({columns}) VALUES({placeholders})", tuple(stale.values()))
    with pytest.raises(AgentRevisionConflictError, match="base_stale"):
        _run(resources.authoring.open_working_copy(agent_id=identity, expected_live_release_id=stale["id"]))
    other = saved_draft[5]
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?, 'member','2026-10-03')",
            (resources.workspace_id, other.id),
        )
    with pytest.raises(AgentRevisionForbiddenError):
        _run(replace(resources, user=other).authoring.open_working_copy(agent_id=identity, expected_live_release_id=live["release_id"]))
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0


def test_authorization_failure_after_insertion_rolls_back_the_new_baseline(resources, live_agent, monkeypatch):
    identity, live = live_agent
    original = AgentCoachAuthoring._authorize_agent
    calls = 0

    async def revoked(self, connection):
        nonlocal calls
        calls += 1
        await original(self, connection)
        if calls == 2:
            raise ValueError("Access revoked before commit")

    monkeypatch.setattr(AgentCoachAuthoring, "_authorize_agent", revoked)
    with pytest.raises(ValueError, match="revoked"):
        _run(resources.authoring.open_working_copy(agent_id=identity, expected_live_release_id=live["release_id"]))
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0
