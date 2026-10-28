"""A member's thumbs-down on a team Agent's answer, and what the Agent's Owner may see of it.

The author always reads their own thumbs-down. The Owner reads the reasons and comment only for their
own Turns and for Turns currently shared with them, and a shared thumbs-down waits on the Owner's
Pending list until the Owner dismisses it or opens it in Coach, or the member withdraws the share.
"""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_eval_conversation_cases import (
    AgentEvalConversationError,
    clear_answer_feedback,
    handle_pending_feedback,
    list_conversation_candidates,
    list_pending_feedback,
    list_shareable_turns,
    record_answer_feedback,
    share_turn,
    withdraw_turn_share,
)
from test_agent_rankings import completed_use, ranking_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

MEMBER = "usr_chart_1"
BYSTANDER = "usr_chart_0"


async def _rate(fixture, turn_id, *, user_id=MEMBER, reasons=("fact_wrong",), comment="Wrong quarter"):
    return await record_answer_feedback(
        fixture.db_path, team_id=fixture.team_id, user_id=user_id, turn_id=turn_id, reason_codes=list(reasons), comment=comment
    )


async def _pending(fixture, user_id=None):
    return await list_pending_feedback(fixture.db_path, team_id=fixture.team_id, agent_id="osa_exact", user_id=user_id or fixture.owner_id)


async def _candidates(fixture, user_id=None):
    return await list_conversation_candidates(fixture.db_path, team_id=fixture.team_id, agent_id="osa_exact", user_id=user_id or fixture.owner_id)


async def _handle(fixture, turn_id, action="dismissed", user_id=None):
    return await handle_pending_feedback(
        fixture.db_path, team_id=fixture.team_id, agent_id="osa_exact", user_id=user_id or fixture.owner_id, turn_id=turn_id, action=action
    )


async def _share(fixture, turn_id, user_id=MEMBER):
    return await share_turn(fixture.db_path, team_id=fixture.team_id, user_id=user_id, turn_id=turn_id)


def _session_of(fixture, turn_id):
    with sqlite3.connect(fixture.db_path) as db:
        return db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()[0]


def _ids(items):
    return [item["turn_id"] for item in items]


async def test_a_member_rates_only_their_own_team_agent_answers(ranking_db, monkeypatch) -> None:  # noqa: F811
    team_answer = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    ordinary = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="b", system="lead")
    someone_elses = await completed_use(ranking_db, monkeypatch, actor=BYSTANDER, suffix="c")

    receipt = await _rate(ranking_db, team_answer, reasons=["too_brief", "fact_wrong", "too_brief"], comment="  Wrong quarter  ")
    # Repeats collapse, the comment is trimmed; a thumbs-down never shares.
    assert receipt["feedback"] == {
        "score": -1,
        "reason_codes": ["too_brief", "fact_wrong"],
        "comment": "Wrong quarter",
        "given_at": receipt["feedback"]["given_at"],
    }
    assert receipt["shared"] is False and receipt["shareable"] is True
    # The member is told whose consent the share would be: the Agent's Owner.
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("UPDATE users SET display_name='Liu Yixin' WHERE id=?", (ranking_db.owner_id,))
    receipt = await _rate(ranking_db, team_answer)
    assert receipt["owner_name"] == "Liu Yixin"
    # Only the fixed reason set is accepted, including none of the older satisfaction codes.
    for unknown in (["made_up"], ["inaccurate"], ["missed_dim"]):
        with pytest.raises(AgentEvalConversationError) as error:
            await _rate(ranking_db, team_answer, reasons=unknown)
        assert error.value.code == "answer_feedback_reason_invalid"

    # Ordinary (non-team-Agent) answers, someone else's Turn and a missing Turn can't be rated.
    for user_id, turn_id in ((MEMBER, ordinary), (MEMBER, someone_elses), (MEMBER, "t_missing")):
        with pytest.raises(AgentEvalConversationError) as error:
            await _rate(ranking_db, turn_id, user_id=user_id)
        assert error.value.code == "answer_feedback_unavailable"

    listed = await list_shareable_turns(
        ranking_db.db_path, team_id=ranking_db.team_id, user_id=MEMBER, session_id=_session_of(ranking_db, team_answer)
    )
    assert listed == [{**receipt}]
    assert (
        await list_shareable_turns(ranking_db.db_path, team_id=ranking_db.team_id, user_id=MEMBER, session_id=_session_of(ranking_db, ordinary)) == []
    )

    # Rating again updates the one thumbs-down; an empty comment is no comment.
    updated = await _rate(ranking_db, team_answer, reasons=[], comment="   ")
    assert updated["feedback"]["reason_codes"] == [] and updated["feedback"]["comment"] is None
    assert await clear_answer_feedback(ranking_db.db_path, team_id=ranking_db.team_id, user_id=MEMBER, turn_id=team_answer) == {
        "turn_id": team_answer,
        "feedback": None,
    }
    assert (
        await list_shareable_turns(ranking_db.db_path, team_id=ranking_db.team_id, user_id=MEMBER, session_id=_session_of(ranking_db, team_answer))
    )[0]["feedback"] is None


async def test_the_owner_sees_a_thumbs_down_only_after_the_member_shares_the_turn(ranking_db, monkeypatch) -> None:  # noqa: F811
    turn = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    await _rate(ranking_db, turn)

    # No consent yet: neither the Turn nor its reasons and comment reach the Owner.
    assert await _pending(ranking_db) == []
    assert turn not in _ids(await _candidates(ranking_db))
    with pytest.raises(AgentEvalConversationError) as error:
        await _handle(ranking_db, turn)
    assert error.value.code == "agent_answer_feedback_unavailable"

    await _share(ranking_db, turn)
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("UPDATE users SET display_name='Lin Wei' WHERE id=?", (MEMBER,))
    [item] = await _pending(ranking_db)
    assert item["turn_id"] == turn and item["shared_by"] == "Lin Wei" and item["question"] == "Summarize the report"
    assert item["feedback"]["reason_codes"] == ["fact_wrong"] and item["feedback"]["comment"] == "Wrong quarter"
    assert "answer" not in item
    [candidate] = await _candidates(ranking_db)
    assert candidate["feedback"]["score"] == -1 and candidate["feedback"]["comment"] == "Wrong quarter"

    # Shared with the Agent's Owner only: another member gets nothing.
    assert await _pending(ranking_db, BYSTANDER) == []
    with pytest.raises(AgentEvalConversationError):
        await _handle(ranking_db, turn, user_id=BYSTANDER)


async def test_a_share_without_thumbs_down_is_a_candidate_but_not_pending(ranking_db, monkeypatch) -> None:  # noqa: F811
    turn = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    await _share(ranking_db, turn)
    assert await _pending(ranking_db) == []
    [candidate] = await _candidates(ranking_db)
    assert candidate["turn_id"] == turn and candidate["feedback"] is None


async def test_the_owners_own_thumbs_down_is_theirs_to_read_but_not_pending(ranking_db, monkeypatch) -> None:  # noqa: F811
    owner = ranking_db.owner_id
    await AgentAssignmentStore(ranking_db.db_path).hire(
        team_id=ranking_db.team_id, user_id=owner, agent_id="osa_exact", agent_source_kind="team", live_access=True
    )
    turn = await completed_use(ranking_db, monkeypatch, actor=owner, suffix="a")
    receipt = await _rate(ranking_db, turn, user_id=owner)
    # Their own Agent: rated, never shareable.
    assert receipt["shareable"] is False
    [candidate] = await _candidates(ranking_db)
    assert candidate["feedback"]["comment"] == "Wrong quarter"
    assert await _pending(ranking_db) == []


@pytest.mark.parametrize("action", ["dismissed", "coach"])
async def test_the_owner_takes_an_item_off_pending_and_the_turn_stays_a_candidate(ranking_db, monkeypatch, action) -> None:  # noqa: F811
    turn = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    await _rate(ranking_db, turn)
    await _share(ranking_db, turn)

    assert await _handle(ranking_db, turn, action) == {"turn_id": turn, "handled": action}
    assert await _pending(ranking_db) == []
    [candidate] = await _candidates(ranking_db)
    assert candidate["feedback"]["score"] == -1
    with pytest.raises(AgentEvalConversationError):
        await _handle(ranking_db, turn, action)
    # The member editing their reasons keeps it handled; it doesn't come back.
    await _rate(ranking_db, turn, reasons=["presentation"])
    assert await _pending(ranking_db) == []


async def test_an_unknown_action_is_rejected(ranking_db, monkeypatch) -> None:  # noqa: F811
    turn = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    with pytest.raises(AgentEvalConversationError) as error:
        await _handle(ranking_db, turn, "deleted")
    assert error.value.code == "agent_answer_feedback_action_invalid"


async def test_withdrawing_the_share_or_clearing_the_thumbs_down_removes_the_item(ranking_db, monkeypatch) -> None:  # noqa: F811
    withdrawn = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    cleared = await completed_use(ranking_db, monkeypatch, actor=MEMBER, day="2030-01-31", suffix="b")
    for turn in (withdrawn, cleared):
        await _rate(ranking_db, turn)
        await _share(ranking_db, turn)
    assert set(_ids(await _pending(ranking_db))) == {withdrawn, cleared}

    await withdraw_turn_share(ranking_db.db_path, team_id=ranking_db.team_id, user_id=MEMBER, turn_id=withdrawn)
    await clear_answer_feedback(ranking_db.db_path, team_id=ranking_db.team_id, user_id=MEMBER, turn_id=cleared)
    assert await _pending(ranking_db) == []
    with pytest.raises(AgentEvalConversationError):
        await _handle(ranking_db, withdrawn)
    # The cleared Turn is still shared, without a thumbs-down; the withdrawn one is gone with its reasons.
    assert [(item["turn_id"], item["feedback"]) for item in await _candidates(ranking_db)] == [(cleared, None)]


@pytest.mark.parametrize(
    "break_access",
    [
        "UPDATE sessions SET user_id='usr_chart_2' WHERE id=(SELECT session_id FROM turns WHERE id=:turn)",
        "UPDATE sessions SET deleted_at='2030-02-01' WHERE id=(SELECT session_id FROM turns WHERE id=:turn)",
        "UPDATE workspace_members SET status='removed' WHERE user_id='usr_chart_1'",
        "UPDATE team_members SET status='removed' WHERE user_id='usr_chart_1'",
        "UPDATE turn_agent_binding SET requested_agent_id='osa_other' WHERE turn_id=:turn",
        # The requester is no longer the Agent's Owner.
        "UPDATE team_subagent SET owner_user_id='usr_chart_0' WHERE id='osa_exact'",
    ],
)
async def test_pending_rechecks_the_share_conditions(ranking_db, monkeypatch, break_access) -> None:  # noqa: F811
    turn = await completed_use(ranking_db, monkeypatch, actor=MEMBER, suffix="a")
    await _rate(ranking_db, turn)
    await _share(ranking_db, turn)
    assert _ids(await _pending(ranking_db)) == [turn]
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for trigger in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('turn_agent_binding','team_subagent')"
        ).fetchall():
            db.execute(f'DROP TRIGGER "{trigger[0]}"')
        db.execute(break_access, {"turn": turn})

    assert await _pending(ranking_db) == []
    with pytest.raises(AgentEvalConversationError):
        await _handle(ranking_db, turn)
