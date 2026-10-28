"""Past conversations offered for evaluation cases (R14).

The Owner gets their own readable Turns and Turns other members shared with the Agent's Owner, one
Turn at a time; an unshared or withdrawn Turn of someone else is never listed nor readable.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_eval_conversation_cases import (
    AgentEvalConversationError,
    list_conversation_candidates,
    list_shareable_turns,
    read_conversation_turns,
    share_turn,
    withdraw_turn_share,
)
from test_agent_rankings import completed_use, ranking_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

ME = "usr_chart_0"
OTHER = "usr_chart_1"


async def _candidates(fixture, user_id=ME):
    return await list_conversation_candidates(fixture.db_path, team_id=fixture.team_id, agent_id="osa_exact", user_id=user_id)


async def _read(fixture, turn_ids, user_id=ME):
    return await read_conversation_turns(fixture.db_path, team_id=fixture.team_id, agent_id="osa_exact", user_id=user_id, turn_ids=turn_ids)


async def _share(fixture, turn_id, user_id=OTHER):
    return await share_turn(fixture.db_path, team_id=fixture.team_id, user_id=user_id, turn_id=turn_id)


async def _hire_owner(fixture) -> str:
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id, user_id=fixture.owner_id, agent_id="osa_exact", agent_source_kind="team", live_access=True
    )
    return fixture.owner_id


def _ids(items):
    return [item["turn_id"] for item in items]


async def test_only_my_completed_conversations_are_offered(ranking_db, monkeypatch) -> None:  # noqa: F811
    mine = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="a")
    newer = await completed_use(ranking_db, monkeypatch, actor=ME, day="2030-01-31", suffix="b")
    theirs = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="c")
    failed = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="d")
    deleted = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="e")
    dry_run = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="f", origin="workflow_dryrun")
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("UPDATE turns SET status='failed' WHERE id=?", (failed,))
        db.execute("UPDATE sessions SET deleted_at='2030-01-31' WHERE id=(SELECT session_id FROM turns WHERE id=?)", (deleted,))

    items = await _candidates(ranking_db)
    assert _ids(items) == [newer, mine]
    assert items[0]["question"] == "Summarize the report"
    # The list never carries the answer; the requester's own Turns have no sharer.
    assert set(items[0]) == {"turn_id", "question", "answered_at", "shared_by", "feedback"}
    assert items[0]["shared_by"] is None
    assert theirs not in _ids(await _candidates(ranking_db))
    assert dry_run not in _ids(items)


async def test_reading_returns_question_and_answer_in_request_order(ranking_db, monkeypatch) -> None:  # noqa: F811
    first = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="a")
    second = await completed_use(ranking_db, monkeypatch, actor=ME, day="2030-01-31", suffix="b")

    turns = await _read(ranking_db, [first, second])
    assert _ids(turns) == [first, second]
    assert turns[0] == {"turn_id": first, "question": "Summarize the report", "answer": "Report summary"}


async def test_the_question_leaves_out_the_mention_of_the_agent_that_answered(ranking_db, monkeypatch) -> None:  # noqa: F811
    import json

    turn = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="a")
    document = {
        "schema_version": 1,
        "nodes": [
            {"type": "reference", "node_id": "n1", "resource": {"kind": "agent", "source_kind": "team", "id": "osa_exact"}, "label": "Sales Helper"},
            {"type": "text", "text": " Summarize the report with "},
            {"type": "reference", "node_id": "n2", "resource": {"kind": "skill", "source_kind": "team", "id": "sk_1"}, "label": "brief"},
        ],
    }
    with sqlite3.connect(ranking_db.db_path) as db:
        session_id, workspace_id = db.execute(
            "SELECT s.id,s.workspace_id FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=?", (turn,)
        ).fetchone()
        db.execute("DELETE FROM chat_turn_user_sources WHERE turn_id=?", (turn,))
        db.execute(
            "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,"
            "created_at,input_document_json) VALUES(?,?,?,?,?,'[]','[]','2030-01-30',?)",
            (turn, session_id, ME, workspace_id, "@Sales Helper Summarize the report with /brief", json.dumps(document)),
        )

    # The list, the read and the alignment loop's answers all quote what the member asked.
    assert (await _candidates(ranking_db))[0]["question"] == "Summarize the report with /brief"
    assert (await _read(ranking_db, [turn]))[0]["question"] == "Summarize the report with /brief"
    from bowhead_core.agent_eval_conversation_cases import readable_turn_answers_conn

    async with aiosqlite.connect(ranking_db.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await readable_turn_answers_conn(db, team_id=ranking_db.team_id, agent_id="osa_exact", user_id=ME, turn_ids=[turn])
    assert rows[0]["question"] == "Summarize the report with /brief"


async def test_someone_elses_or_missing_conversation_is_unavailable_without_saying_which(ranking_db, monkeypatch) -> None:  # noqa: F811
    mine = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="a")
    theirs = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="b")

    for selection in ([mine, theirs], [mine, "t_missing"]):
        with pytest.raises(AgentEvalConversationError) as error:
            await _read(ranking_db, selection)
        assert error.value.code == "agent_eval_conversation_unavailable"
    # The other member reads their own conversation; access follows the conversation's owner.
    assert _ids(await _read(ranking_db, [theirs], user_id=OTHER)) == [theirs]


async def test_a_selection_must_be_distinct_and_at_most_thirty_two(ranking_db, monkeypatch) -> None:  # noqa: F811
    mine = await completed_use(ranking_db, monkeypatch, actor=ME, suffix="a")
    for selection in ([mine, mine], [], [f"t_{index}" for index in range(33)]):
        with pytest.raises(AgentEvalConversationError) as error:
            await _read(ranking_db, selection)
        assert error.value.code == "agent_eval_conversation_selection_invalid"


# ── Turns shared by other members ────────────────────────────────────────────


async def test_a_shared_turn_is_offered_with_the_sharers_name_and_withdrawing_removes_it(ranking_db, monkeypatch) -> None:  # noqa: F811
    owner = await _hire_owner(ranking_db)
    shared = await completed_use(ranking_db, monkeypatch, actor=OTHER, day="2030-01-31", suffix="a")
    unshared = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="b")
    own = await completed_use(ranking_db, monkeypatch, actor=owner, suffix="c")
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("UPDATE users SET display_name='Lin Wei' WHERE id=?", (OTHER,))

    # Consent is required: nothing of the other member's is offered before they share.
    assert _ids(await _candidates(ranking_db, owner)) == [own]
    with pytest.raises(AgentEvalConversationError):
        await _read(ranking_db, [shared], user_id=owner)

    receipt = await _share(ranking_db, shared)
    assert receipt == {"turn_id": shared, "agent_id": "osa_exact", "agent_name": "Exact Agent", "shared": True}
    await _share(ranking_db, shared)  # repeating keeps one consent

    items = await _candidates(ranking_db, owner)
    assert _ids(items) == [shared, own]  # newest first, the Owner's own Turns still included
    assert items[0]["shared_by"] == "Lin Wei"
    assert set(items[0]) == {"turn_id", "question", "answered_at", "shared_by", "feedback"}
    assert unshared not in _ids(items)
    assert (await _read(ranking_db, [shared], user_id=owner))[0]["answer"] == "Report summary"
    with pytest.raises(AgentEvalConversationError) as error:
        await _read(ranking_db, [shared, unshared], user_id=owner)
    assert error.value.code == "agent_eval_conversation_unavailable"
    # Sharing is with the Agent's Owner only: another member still doesn't see it.
    assert shared not in _ids(await _candidates(ranking_db, ME))

    assert await withdraw_turn_share(ranking_db.db_path, team_id=ranking_db.team_id, user_id=OTHER, turn_id=shared) == {
        "turn_id": shared,
        "shared": False,
    }
    assert _ids(await _candidates(ranking_db, owner)) == [own]
    with pytest.raises(AgentEvalConversationError):
        await _read(ranking_db, [shared], user_id=owner)


async def test_only_the_author_can_withdraw_their_share(ranking_db, monkeypatch) -> None:  # noqa: F811
    shared = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="a")
    await _share(ranking_db, shared)
    await withdraw_turn_share(ranking_db.db_path, team_id=ranking_db.team_id, user_id=ranking_db.owner_id, turn_id=shared)
    assert _ids(await _candidates(ranking_db, ranking_db.owner_id)) == [shared]


@pytest.mark.parametrize(
    "break_access",
    [
        # The Turn is no longer the sharer's.
        "UPDATE sessions SET user_id='usr_chart_2' WHERE id=(SELECT session_id FROM turns WHERE id=:turn)",
        # The conversation was deleted.
        "UPDATE sessions SET deleted_at='2030-02-01' WHERE id=(SELECT session_id FROM turns WHERE id=:turn)",
        # Not a normal conversation any more.
        "UPDATE sessions SET origin='workflow_dryrun' WHERE id=(SELECT session_id FROM turns WHERE id=:turn)",
        # The sharer left the Space.
        "UPDATE workspace_members SET status='removed' WHERE user_id='usr_chart_1'",
        # The Space is archived.
        "UPDATE workspaces SET status='archived'",
        # The sharer left the Team.
        "UPDATE team_members SET status='removed' WHERE user_id='usr_chart_1'",
        # The Turn is bound to another Agent.
        "UPDATE turn_agent_binding SET requested_agent_id='osa_other' WHERE turn_id=:turn",
    ],
)
async def test_a_shared_turn_that_fails_any_access_check_is_neither_listed_nor_readable(
    ranking_db,  # noqa: F811
    monkeypatch,
    break_access,
) -> None:
    owner = ranking_db.owner_id
    shared = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="a")
    await _share(ranking_db, shared)
    assert _ids(await _candidates(ranking_db, owner)) == [shared]
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for trigger in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='turn_agent_binding'").fetchall():
            db.execute(f'DROP TRIGGER "{trigger[0]}"')
        db.execute(break_access, {"turn": shared})

    assert _ids(await _candidates(ranking_db, owner)) == []
    with pytest.raises(AgentEvalConversationError) as error:
        await _read(ranking_db, [shared], user_id=owner)
    assert error.value.code == "agent_eval_conversation_unavailable"


async def test_a_member_lists_and_shares_only_their_own_turns_with_an_agent_they_dont_own(ranking_db, monkeypatch) -> None:  # noqa: F811
    owner = await _hire_owner(ranking_db)
    theirs = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="a")
    own = await completed_use(ranking_db, monkeypatch, actor=owner, suffix="b")
    failed = await completed_use(ranking_db, monkeypatch, actor=OTHER, suffix="c")
    with sqlite3.connect(ranking_db.db_path) as db:
        db.execute("UPDATE turns SET status='failed' WHERE id=?", (failed,))
        session_of = dict(db.execute("SELECT id,session_id FROM turns").fetchall())

    async def shareable(user_id, turn_id):
        return await list_shareable_turns(ranking_db.db_path, team_id=ranking_db.team_id, user_id=user_id, session_id=session_of[turn_id])

    assert await shareable(OTHER, theirs) == [
        {
            "turn_id": theirs,
            "agent_id": "osa_exact",
            "agent_name": "Exact Agent",
            "shared": False,
            "shareable": True,
            "owner_name": "owner@example.com",
            "feedback": None,
        }
    ]
    # The Owner's own Turns are listed for a thumbs-down but need no share; someone else's session lists nothing.
    assert [(item["turn_id"], item["shareable"]) for item in await shareable(owner, own)] == [(own, False)]
    assert await shareable(ME, theirs) == []
    assert await shareable(OTHER, failed) == []

    for user_id, turn_id in ((owner, own), (ME, theirs), (OTHER, failed), (OTHER, "t_missing")):
        with pytest.raises(AgentEvalConversationError) as error:
            await _share(ranking_db, turn_id, user_id=user_id)
        assert error.value.code == "turn_eval_share_unavailable"

    await _share(ranking_db, theirs)
    assert (await shareable(OTHER, theirs))[0]["shared"] is True
