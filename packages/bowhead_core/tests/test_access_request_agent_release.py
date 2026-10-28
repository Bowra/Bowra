"""Access-request approval for release-managed Agents goes through the publication path."""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_core.access_request_agent_release import (
    RELEASE_CHANGED,
    UNPUBLISHED_CHANGES,
    AgentReleaseApprovalError,
    idempotency_key_for,
    reconcile_release_pending,
    request_audience_release,
    settle_stale_claim,
)
from bowhead_core.access_request_store import RELEASE_CLAIM, AccessRequestStore, DecisionDetails
from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionStore
from legacy_agent_release_fixture import seed_legacy_publication
from test_agent_revision_store import _Clock, _db, _definition, _listing, _seed, _Validator

pytestmark = pytest.mark.integration

_TEAM = "team_release"
_AGENT = "osa_release"


async def _setup(tmp_path, *, safety_status: str = "review_required"):
    db_path = _db(tmp_path)
    _seed(db_path)
    revisions = AgentRevisionStore(str(db_path), validator=_Validator(safety_status=safety_status), clock=_Clock())
    acr = AccessRequestStore(str(db_path))
    await acr.initialize()
    row, _ = await acr.enqueue_once(
        team_id=_TEAM, kind="team_subagent", resource_id=_AGENT, requester_id="user_member", owner_id="user_owner", message=None
    )
    return db_path, revisions, acr, row


async def _approve(revisions, acr, row):
    outcome = await request_audience_release(
        revisions,
        team_id=_TEAM,
        agent_id=_AGENT,
        owner_user_id="user_owner",
        requester_id=row.requester_id,
    )
    await _record_approval(acr, row, outcome)
    return outcome


async def _approve_historical(revisions, acr, row):
    """Seed an approval awaiting a pre-workbench publication for recovery coverage."""
    outcome = await _publish_historical_request(revisions, row)
    await _record_approval(acr, row, outcome)
    return outcome


async def _record_approval(acr, row, outcome):
    status = "approved" if outcome.state in {"activated", "noop"} else "approved_pending"
    await acr.set_decision_once(
        request_id=row.id,
        team_id=_TEAM,
        status=status,
        decided_by="user_owner",
        decision=DecisionDetails(granted_permission="use", release_candidate_id=outcome.candidate_id),
    )


def _candidates(db_path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT id, state, target_audience_json FROM team_subagent_release_candidate ORDER BY submitted_at").fetchall()


def _candidate_hash(db_path, candidate_id: str) -> str:
    with sqlite3.connect(db_path) as db:
        return str(db.execute("SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?", (candidate_id,)).fetchone()[0])


async def test_review_required_release_waits_then_settles_approved_when_it_goes_live(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    outcome = await _approve_historical(revisions, acr, row)
    assert outcome.state == "pending" and outcome.candidate_id
    [(candidate_id, state, target)] = _candidates(db_path)
    assert state == "pending" and '"user_ids":["user_member"]' in target
    # the live definition is reused unchanged: only the audience moves
    with sqlite3.connect(db_path) as db:
        live_def = db.execute("SELECT definition_json FROM team_subagent_revision WHERE id='arv_initial'").fetchone()[0]
        new_def = db.execute(
            "SELECT r.definition_json FROM team_subagent_release_candidate c JOIN team_subagent_revision r ON r.id=c.revision_id WHERE c.id=?",
            (candidate_id,),
        ).fetchone()[0]
    assert new_def == live_def

    assert await reconcile_release_pending(acr, str(db_path), agent_id=_AGENT) == []
    assert (await acr.get(row.id)).status == "approved_pending"

    await revisions.approve_candidate(
        team_id=_TEAM,
        candidate_id=candidate_id,
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash(db_path, candidate_id),
    )
    settled = await reconcile_release_pending(acr, str(db_path), agent_id=_AGENT)
    assert [(r.id, outcome) for r, outcome in settled] == [(row.id, "approved")]
    assert (await acr.get(row.id)).status == "approved"


async def test_review_rejection_settles_approve_failed(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    outcome = await _approve_historical(revisions, acr, row)
    await revisions.reject_candidate(
        team_id=_TEAM,
        candidate_id=str(outcome.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash(db_path, str(outcome.candidate_id)),
        reason_code="policy_not_met",
    )
    settled = await reconcile_release_pending(acr, str(db_path), team_id=_TEAM)
    assert [outcome for _, outcome in settled] == ["approve_failed"]
    assert (await acr.get(row.id)).status == "approve_failed"


async def test_unrelated_unpublished_edits_block_the_approval_without_touching_them(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    draft = await revisions.save_working_copy(
        team_id=_TEAM,
        agent_id=_AGENT,
        actor_user_id="user_owner",
        definition=_definition("A new prompt the Owner has not published."),
        listing=_listing("Old Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    with pytest.raises(AgentReleaseApprovalError) as error:
        await _approve(revisions, acr, row)
    assert error.value.code == UNPUBLISHED_CHANGES
    assert _candidates(db_path) == []
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT row_version, definition_json FROM team_subagent_working_copy").fetchone() == (
            draft.row_version,
            '{"max_iterations":10,"model":null,"output_schema":null,"skill_grants":[],"system_prompt":"A new prompt the Owner has not published.","tool_refs":[]}',
        )
    assert (await acr.get(row.id)).status == "pending"


async def test_a_working_copy_identical_to_live_is_reused(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    await revisions.save_working_copy(
        team_id=_TEAM,
        agent_id=_AGENT,
        actor_user_id="user_owner",
        definition=_definition("Old prompt."),
        listing=_listing("Old Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    assert (await _approve(revisions, acr, row)).state == "awaiting_owner_publish"


async def test_repeated_approval_preserves_the_pending_owner_draft(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    await _approve(revisions, acr, row)
    with sqlite3.connect(db_path) as db:
        saved = db.execute("SELECT * FROM team_subagent_working_copy").fetchone()
    assert saved is not None
    with pytest.raises(AgentReleaseApprovalError) as error:
        await request_audience_release(
            revisions,
            team_id=_TEAM,
            agent_id=_AGENT,
            owner_user_id="user_owner",
            requester_id=row.requester_id,
        )
    assert error.value.code == UNPUBLISHED_CHANGES
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT * FROM team_subagent_working_copy").fetchone() == saved


async def test_approval_saves_the_draft_and_waits_for_the_owner(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    outcome = await _approve(revisions, acr, row)
    assert outcome.state == "awaiting_owner_publish" and _candidates(db_path) == []
    assert await reconcile_release_pending(acr, str(db_path)) == []
    # the Owner discards the draft → the approval can no longer take effect
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM team_subagent_working_copy")
    assert [o for _, o in await reconcile_release_pending(acr, str(db_path))] == ["approve_failed"]


async def test_a_release_between_check_and_write_aborts_without_writing(tmp_path) -> None:
    """An audience contraction lands after the live check but before the draft is written."""
    db_path, revisions, acr, row = await _setup(tmp_path, safety_status="pass")
    shared = await revisions.save_working_copy(
        team_id=_TEAM,
        agent_id=_AGENT,
        actor_user_id="user_owner",
        definition=_definition("Old prompt."),
        listing=_listing("Old Agent"),
        audience=AgentAudience("custom", user_ids=("user_admin",)),
        expected_row_version=None,
    )
    published = await seed_legacy_publication(revisions, team_id=_TEAM, agent_id=_AGENT, actor_user_id="user_owner", working_copy_id=shared.id)
    assert published.state == "activated"
    original_save = revisions.save_working_copy

    async def contract_then_save(**kwargs):
        with sqlite3.connect(db_path) as db:
            version = db.execute("SELECT row_version FROM team_subagent WHERE id=?", (_AGENT,)).fetchone()[0]
        await revisions.contract_audience(
            team_id=_TEAM,
            agent_id=_AGENT,
            actor_user_id="user_owner",
            expected_aggregate_row_version=version,
            audience=AgentAudience("private"),
            idempotency_key="contract-race",
            reason_code="owner_removed_audience",
        )
        return await original_save(**kwargs)

    revisions.save_working_copy = contract_then_save  # type: ignore[method-assign]
    with pytest.raises(AgentReleaseApprovalError) as error:
        await _approve(revisions, acr, row)
    assert error.value.code == RELEASE_CHANGED
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_working_copy").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release_candidate WHERE state IN ('prepared','pending')").fetchone() == (0,)


async def test_rejected_approval_takes_the_requester_back_out_of_the_draft(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    outcome = await _approve_historical(revisions, acr, row)
    await revisions.reject_candidate(
        team_id=_TEAM,
        candidate_id=str(outcome.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash(db_path, str(outcome.candidate_id)),
        reason_code="policy_not_met",
    )
    with sqlite3.connect(db_path) as db:
        assert "user_member" in db.execute("SELECT audience_json FROM team_subagent_working_copy").fetchone()[0]
    settled = await reconcile_release_pending(acr, str(db_path), team_id=_TEAM, revision_store=revisions)
    assert [o for _, o in settled] == ["approve_failed"]
    with sqlite3.connect(db_path) as db:
        # the draft was exactly live + the requester, so it is discarded entirely
        assert db.execute("SELECT COUNT(*) FROM team_subagent_working_copy").fetchone() == (0,)


async def test_cleanup_keeps_other_owner_edits(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    outcome = await _approve_historical(revisions, acr, row)
    await revisions.reject_candidate(
        team_id=_TEAM,
        candidate_id=str(outcome.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash(db_path, str(outcome.candidate_id)),
        reason_code="policy_not_met",
    )
    with sqlite3.connect(db_path) as db:
        version = db.execute("SELECT row_version FROM team_subagent_working_copy").fetchone()[0]
    await revisions.save_working_copy(
        team_id=_TEAM,
        agent_id=_AGENT,
        actor_user_id="user_owner",
        definition=_definition("Owner kept editing."),
        listing=_listing("Old Agent"),
        audience=AgentAudience("custom", user_ids=("user_member",)),
        expected_row_version=version,
    )
    await reconcile_release_pending(acr, str(db_path), team_id=_TEAM, revision_store=revisions)
    with sqlite3.connect(db_path) as db:
        definition, audience = db.execute("SELECT definition_json, audience_json FROM team_subagent_working_copy").fetchone()
    assert "Owner kept editing." in definition and "user_member" not in audience


async def test_discard_working_copy_ignores_a_moved_version(tmp_path) -> None:
    db_path, revisions, _acr, _row = await _setup(tmp_path)
    draft = await revisions.save_working_copy(
        team_id=_TEAM,
        agent_id=_AGENT,
        actor_user_id="user_owner",
        definition=_definition("Old prompt."),
        listing=_listing("Old Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    assert not await revisions.discard_working_copy(
        team_id=_TEAM, agent_id=_AGENT, actor_user_id="user_owner", expected_working_copy_id=draft.id, expected_row_version=draft.row_version + 1
    )
    assert await revisions.discard_working_copy(
        team_id=_TEAM, agent_id=_AGENT, actor_user_id="user_owner", expected_working_copy_id=draft.id, expected_row_version=draft.row_version
    )


async def _claim(acr, row):
    _, ok = await acr.set_decision_once(
        request_id=row.id,
        team_id=_TEAM,
        status="approved_pending",
        decided_by="user_owner",
        decision=DecisionDetails(granted_permission="use", release_candidate_id=RELEASE_CLAIM),
    )
    assert ok


async def _publish_historical_request(revisions, row):
    """Seed a legacy publication receipt for existing-data recovery tests."""
    await request_audience_release(
        revisions,
        team_id=_TEAM,
        agent_id=_AGENT,
        owner_user_id="user_owner",
        requester_id=row.requester_id,
    )

    with sqlite3.connect(revisions.db_path) as db:
        (working_id,) = db.execute(
            "SELECT id FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?",
            (_TEAM, _AGENT),
        ).fetchone()
    return await seed_legacy_publication(
        revisions, team_id=_TEAM, agent_id=_AGENT, actor_user_id="user_owner", working_copy_id=working_id, receipt_key=idempotency_key_for(row.id)
    )


async def test_stale_claim_without_a_receipt_is_reverted(tmp_path) -> None:
    _db_path, revisions, acr, row = await _setup(tmp_path)
    await _claim(acr, row)
    assert await settle_stale_claim(acr, revisions, await acr.get(row.id)) == "reverted"
    assert (await acr.get(row.id)).status == "pending"


async def test_reverted_draft_claim_allows_the_next_approval(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path)
    await _claim(acr, row)
    # The current approval saves a draft; a crash before finalization leaves no receipt.
    outcome = await request_audience_release(
        revisions,
        team_id=_TEAM,
        agent_id=_AGENT,
        owner_user_id="user_owner",
        requester_id=row.requester_id,
    )
    assert outcome.state == "awaiting_owner_publish"
    assert await settle_stale_claim(acr, revisions, await acr.get(row.id)) == "reverted"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_working_copy").fetchone() == (0,)
    # the next approval is not blocked by our own leftover draft
    again = await _approve(revisions, acr, await acr.get(row.id))
    assert again.state == "awaiting_owner_publish"


async def test_stale_claim_with_a_pending_receipt_keeps_waiting_on_its_candidate(tmp_path) -> None:
    _db_path, revisions, acr, row = await _setup(tmp_path)
    await _claim(acr, row)
    outcome = await _publish_historical_request(revisions, row)
    assert await settle_stale_claim(acr, revisions, await acr.get(row.id)) == "approved_pending"
    settled = await acr.get(row.id)
    assert settled.status == "approved_pending" and settled.release_candidate_id == outcome.candidate_id


async def test_stale_claim_with_a_live_receipt_is_approved(tmp_path) -> None:
    _db_path, revisions, acr, row = await _setup(tmp_path, safety_status="pass")
    await _claim(acr, row)
    assert (await _publish_historical_request(revisions, row)).state == "activated"
    assert await settle_stale_claim(acr, revisions, await acr.get(row.id)) == "approved"
    assert (await acr.get(row.id)).status == "approved"


async def test_pending_request_whose_requester_already_has_access_is_closed_as_approved(tmp_path) -> None:
    db_path, revisions, acr, row = await _setup(tmp_path, safety_status="pass")
    # e.g. a claim was reverted although its release had gone live
    assert (await _publish_historical_request(revisions, row)).state == "activated"
    assert (await acr.get(row.id)).status == "pending"
    settled = await reconcile_release_pending(acr, str(db_path), team_id=_TEAM)
    assert [(r.id, outcome) for r, outcome in settled] == [(row.id, "approved")]
    assert (await acr.get(row.id)).status == "approved"
