"""Coach writes the existing Agent's draft directly; each field can be undone and reapplied."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, AgentChangeProposalStore, AgentProposalBaseline
from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionConflictError, AgentRevisionStore
from test_agent_revision_store import _db, _definition, _listing, _seed, _Validator

pytestmark = pytest.mark.unit

PROMPT = "/definition/system_prompt"
DESCRIPTION = "/listing/description"


async def _setup(tmp_path):
    db_path = _db(tmp_path)
    _seed(db_path)
    revisions = AgentRevisionStore(str(db_path), validator=_Validator())
    working = await revisions.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Base prompt."),
        listing=_listing("Base Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    return db_path, revisions, AgentChangeProposalStore(str(db_path), revision_store=revisions), working


async def _coach_write(store, working, ref, operations, *, version=None):
    proposal, version, _ = await store.create_applied(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_ref=f"coach:turn:{ref}",
        operations=operations,
        atomic_groups=[{"id": f"g{index}", "operation_indexes": [index]} for index in range(len(operations))],
        expected_baseline=AgentProposalBaseline(working.id, working.row_version if version is None else version, working.base_release_id),
        rationale="Coach edit.",
    )
    return proposal, version


async def _coach_rows(store):
    rows = await store.draft_changes(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")
    return {row["path"]: row for row in rows}


async def _draft(revisions):
    with sqlite3.connect(revisions.db_path) as db:
        row = db.execute(
            "SELECT definition_json,listing_json,audience_json,row_version FROM team_subagent_working_copy WHERE subagent_id='osa_release'"
        ).fetchone()
    return SimpleNamespace(
        definition=json.loads(row[0]), listing=json.loads(row[1]), audience=AgentAudience.from_mapping(json.loads(row[2])), row_version=row[3]
    )


async def test_coach_write_lands_in_the_draft_with_an_accept_decision(tmp_path):
    db_path, revisions, store, working = await _setup(tmp_path)
    proposal, version = await _coach_write(store, working, "a", [{"op": "replace", "path": PROMPT, "value": "Coach prompt."}])
    draft = await _draft(revisions)
    assert proposal.status == "accepted" and version == working.row_version + 1 == draft.row_version
    assert draft.definition["system_prompt"] == "Coach prompt."
    replay, replay_version = await _coach_write(store, working, "a", [{"op": "replace", "path": PROMPT, "value": "Coach prompt."}])
    assert (replay.id, replay_version) == (proposal.id, version)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT decision_kind FROM agent_change_proposal_decision").fetchall() == [("patch_accept",)]


async def test_failed_coach_write_leaves_no_proposal(tmp_path):
    db_path, _, store, working = await _setup(tmp_path)
    with pytest.raises(AgentChangeProposalError, match="version_conflict"):
        await _coach_write(store, working, "stale", [{"op": "replace", "path": PROMPT, "value": "Lost."}], version=working.row_version + 5)
    with pytest.raises(AgentChangeProposalError):
        await _coach_write(store, working, "bad", [{"op": "replace", "path": "/definition/secret", "value": "x"}])
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


async def test_undo_restores_the_value_before_consecutive_coach_writes_and_reapply_restores_coach(tmp_path):
    db_path, revisions, store, working = await _setup(tmp_path)
    _, v1 = await _coach_write(store, working, "1", [{"op": "replace", "path": PROMPT, "value": "First."}])
    _, v2 = await _coach_write(
        store,
        working,
        "2",
        [{"op": "replace", "path": PROMPT, "value": "Second."}, {"op": "replace", "path": DESCRIPTION, "value": "Coach description."}],
        version=v1,
    )
    changes = await _coach_rows(store)
    assert changes[PROMPT]["source"] == "coach" and changes[PROMPT]["live"] == "Old prompt." and changes[PROMPT]["draft"] == "Second."
    assert changes[PROMPT]["coach"] == {
        "state": "applied",
        "proposal_id": changes[PROMPT]["coach"]["proposal_id"],
        "group_ids": ["g0"],
        "before": "Base prompt.",
        "value": "Second.",
        "conflict": False,
    }
    assert changes[DESCRIPTION]["coach"]["value"] == "Coach description."

    undone = await store.undo_fields(
        team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", fields=[PROMPT], expected_working_copy_row_version=v2
    )
    draft = await _draft(revisions)
    assert draft.definition["system_prompt"] == "Base prompt." and draft.listing["description"] == "Coach description."
    changes = await _coach_rows(store)
    assert changes[PROMPT]["coach"]["state"] == "undone" and changes[PROMPT]["source"] == "manual"
    assert changes[DESCRIPTION]["coach"]["state"] == "applied"

    await store.reapply_fields(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        fields=[PROMPT],
        expected_working_copy_row_version=undone["working_copy"].row_version,
    )
    assert (await _draft(revisions)).definition["system_prompt"] == "Second."
    with sqlite3.connect(db_path) as db:
        kinds = [row[0] for row in db.execute("SELECT decision_kind FROM agent_change_proposal_decision ORDER BY created_at,id")]
    assert kinds.count("patch_undo") == 2 and kinds.count("patch_reapply") == 2


async def test_undo_after_a_manual_edit_is_a_conflict_that_writes_nothing(tmp_path):
    _, revisions, store, working = await _setup(tmp_path)
    _, version = await _coach_write(store, working, "1", [{"op": "replace", "path": PROMPT, "value": "Coach prompt."}])
    draft = await _draft(revisions)
    manual = await revisions.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition={**draft.definition, "system_prompt": "Manual prompt."},
        listing=draft.listing,
        audience=draft.audience,
        expected_row_version=version,
    )
    with pytest.raises(AgentChangeProposalError) as error:
        await store.undo_fields(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            fields=[PROMPT],
            expected_working_copy_row_version=manual.row_version,
        )
    assert error.value.code == "agent_change_undo_conflict"
    assert error.value.conflicts == ({"path": PROMPT, "before": "Base prompt.", "coach": "Coach prompt.", "current": "Manual prompt."},)
    assert (await _draft(revisions)).row_version == manual.row_version


async def test_undo_rejects_stale_versions_and_fields_coach_did_not_write(tmp_path):
    _, _, store, working = await _setup(tmp_path)
    _, version = await _coach_write(store, working, "1", [{"op": "replace", "path": PROMPT, "value": "Coach prompt."}])
    args = {"team_id": "team_release", "agent_id": "osa_release", "actor_user_id": "user_owner"}
    with pytest.raises(AgentRevisionConflictError, match="version_conflict"):
        await store.undo_fields(**args, fields=[PROMPT], expected_working_copy_row_version=version - 1)
    with pytest.raises(AgentChangeProposalError, match="agent_coach_change_not_found"):
        await store.undo_fields(**args, fields=[DESCRIPTION], expected_working_copy_row_version=version)
    with pytest.raises(AgentChangeProposalError, match="agent_coach_change_not_found"):
        await store.reapply_fields(**args, fields=[PROMPT], expected_working_copy_row_version=version)


async def test_accepted_ci_proposals_are_not_undone_as_coach_changes(tmp_path):
    _, _, store, working = await _setup(tmp_path)
    proposal = await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="feedback",
        source_ref="feedback:1",
        operations=[{"op": "replace", "path": PROMPT, "value": "Feedback prompt."}],
        atomic_groups=[{"id": "g0", "operation_indexes": [0]}],
    )
    applied = await store.apply(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        selected_group_ids=["g0"],
        expected_proposal_row_version=proposal.row_version,
        expected_working_copy_row_version=working.row_version,
    )
    rows = await _coach_rows(store)
    assert rows[PROMPT]["source"] == "feedback" and "coach" not in rows[PROMPT]
    with pytest.raises(AgentChangeProposalError, match="agent_coach_change_not_found"):
        await store.undo_fields(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            fields=[PROMPT],
            expected_working_copy_row_version=applied["working_copy"].row_version,
        )


async def test_keeping_the_manual_value_resolves_the_conflict_without_touching_the_draft(tmp_path):
    db_path, revisions, store, working = await _setup(tmp_path)
    _, version = await _coach_write(store, working, "1", [{"op": "replace", "path": PROMPT, "value": "Coach prompt."}])
    args = {"team_id": "team_release", "agent_id": "osa_release", "actor_user_id": "user_owner"}
    with pytest.raises(AgentChangeProposalError, match="agent_coach_change_not_in_conflict"):
        await store.keep_fields(**args, fields=[PROMPT], expected_working_copy_row_version=version)
    draft = await _draft(revisions)
    manual = await revisions.save_working_copy(
        **args,
        definition={**draft.definition, "system_prompt": "Manual prompt."},
        listing=draft.listing,
        audience=draft.audience,
        expected_row_version=version,
    )
    kept = await store.keep_fields(**args, fields=[PROMPT], expected_working_copy_row_version=manual.row_version)
    assert kept["working_copy"].row_version == manual.row_version == (await _draft(revisions)).row_version
    row = (await _coach_rows(store))[PROMPT]
    assert row["source"] == "manual" and row["draft"] == "Manual prompt." and "coach" not in row
    with pytest.raises(AgentChangeProposalError, match="agent_coach_change_not_found"):
        await store.undo_fields(**args, fields=[PROMPT], expected_working_copy_row_version=manual.row_version)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT status FROM agent_change_proposal").fetchone()[0] == "accepted"


async def test_change_list_returns_a_long_prompt_whole_so_it_can_be_shown_and_restored(tmp_path):
    _, _, store, working = await _setup(tmp_path)
    long_prompt = "Explain the period first. " * 400
    await _coach_write(store, working, "long", [{"op": "replace", "path": PROMPT, "value": long_prompt}])
    row = (await _coach_rows(store))[PROMPT]
    assert len(long_prompt) > 2_000
    assert row["draft"] == long_prompt and row["coach"]["value"] == long_prompt and row["coach"]["before"] == "Base prompt."


async def test_undo_after_a_manual_edit_between_coach_writes_never_reaches_the_older_write(tmp_path):
    _, revisions, store, working = await _setup(tmp_path)
    args = {"team_id": "team_release", "agent_id": "osa_release", "actor_user_id": "user_owner"}
    _, first = await _coach_write(store, working, "1", [{"op": "replace", "path": PROMPT, "value": "Coach one."}])
    draft = await _draft(revisions)
    manual = await revisions.save_working_copy(
        **args,
        definition={**draft.definition, "system_prompt": "Manual."},
        listing=draft.listing,
        audience=draft.audience,
        expected_row_version=first,
    )
    _, second = await _coach_write(store, working, "2", [{"op": "replace", "path": PROMPT, "value": "Coach two."}], version=manual.row_version)
    undone = await store.undo_fields(**args, fields=[PROMPT], expected_working_copy_row_version=second)
    assert (await _draft(revisions)).definition["system_prompt"] == "Manual."
    row = (await _coach_rows(store))[PROMPT]
    # The row offers Reapply for the latest write, not a conflict on the write before the manual edit.
    assert row["coach"]["state"] == "undone" and row["coach"]["value"] == "Coach two." and not row["coach"]["conflict"]
    with pytest.raises(AgentChangeProposalError, match="agent_coach_change_not_found"):
        await store.undo_fields(**args, fields=[PROMPT], expected_working_copy_row_version=undone["working_copy"].row_version)
    await store.reapply_fields(**args, fields=[PROMPT], expected_working_copy_row_version=undone["working_copy"].row_version)
    assert (await _draft(revisions)).definition["system_prompt"] == "Coach two."


async def test_restoring_the_value_before_coach_settles_the_conflict_in_one_write(tmp_path):
    db_path, revisions, store, working = await _setup(tmp_path)
    args = {"team_id": "team_release", "agent_id": "osa_release", "actor_user_id": "user_owner"}
    _, version = await _coach_write(store, working, "1", [{"op": "replace", "path": PROMPT, "value": "Coach prompt."}])
    draft = await _draft(revisions)
    manual = await revisions.save_working_copy(
        **args,
        definition={**draft.definition, "system_prompt": "Manual prompt."},
        listing=draft.listing,
        audience=draft.audience,
        expected_row_version=version,
    )
    with pytest.raises(AgentRevisionConflictError):
        await store.keep_fields(**args, fields=[PROMPT], expected_working_copy_row_version=version, restore=True)
    restored = await store.keep_fields(**args, fields=[PROMPT], expected_working_copy_row_version=manual.row_version, restore=True)
    draft = await _draft(revisions)
    assert draft.definition["system_prompt"] == "Base prompt." and draft.row_version == restored["working_copy"].row_version == manual.row_version + 1
    assert draft.listing == manual.listing
    rows = await _coach_rows(store)
    assert "coach" not in rows.get(PROMPT, {})
    with sqlite3.connect(db_path) as db:
        reason = db.execute("SELECT decision_reason_json FROM agent_change_proposal_decision WHERE decision_kind='patch_dismiss'").fetchone()[0]
    assert json.loads(reason)["restored"] is True


async def test_draft_changes_ignore_fields_saved_empty_where_live_has_none(tmp_path):
    """Editing only the prompt on Configure also writes empty fields live never stored; they are not changes."""
    _, revisions, store, _ = await _setup(tmp_path)
    working = await _draft(revisions)
    await revisions.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition={
            **working.definition,
            "system_prompt": "New prompt.",
            "team_tool_connections": [],
            "capability_ids": [],
            "primary_capability_ref": None,
        },
        listing=_listing("Old Agent"),
        audience=AgentAudience("private"),
        expected_row_version=working.row_version,
    )
    rows = await store.draft_changes(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")
    assert [row["path"] for row in rows if row["path"].startswith("/definition/")] == [PROMPT]
