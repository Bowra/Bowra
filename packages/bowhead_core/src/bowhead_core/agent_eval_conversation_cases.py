"""Past conversations an Agent Owner can turn into evaluation cases (PRD R14).

The Owner is offered their own completed, undeleted conversations with the Agent, plus Turns other
members explicitly shared with the Agent's Owner, one Turn at a time. The Owner never browses other
members' conversation lists: a member's Turn appears only while their share row exists and every
read rechecks the same conditions as the author's own chat access (the author still owns the Turn,
the session is normal-origin and undeleted, the author is still an active Team member and an active
member of the active Space, and the Turn is still bound to this team Agent). Shared Turns are only
offered to the Agent's current Owner; the gateway also requires Owner access before calling in.

Members can also give a thumbs-down to an answer a team Agent gave them (`agent_answer_feedback`).
The author always sees their own; the Agent's Owner reads its reasons and comment only through the
same readable-Turn query, so only for the Owner's own Turns and Turns currently shared with them. A
shared thumbs-down waits on the Owner's Pending list until the Owner dismisses it or starts improving
the Agent from it; withdrawing the share removes it at once.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx

MAX_TURNS_PER_SUGGESTION = 32
MAX_CANDIDATES = 50
MAX_SHAREABLE_TURNS_PER_SESSION = 500
MAX_PENDING_FEEDBACK = 50
FEEDBACK_COMMENT_MAX_CHARS = 500
# The reasons a member can give with a thumbs-down (2026-10-19 decision). They are independent of the
# older satisfaction endpoint's codes; an unknown code is rejected, never recorded.
ANSWER_FEEDBACK_REASONS = ("fact_wrong", "not_answered", "missing_content", "too_brief", "too_verbose", "presentation", "other")
PENDING_FEEDBACK_ACTIONS = frozenset({"dismissed", "coach"})
_QUESTION_PREVIEW_CHARS = 200
_QUESTION_MAX_CHARS = 8000
_ANSWER_MAX_CHARS = 4000
# The judge reads at most this much of a task and an answer (agent_eval_judge.grade_points).
ALIGNMENT_QUESTION_MAX_CHARS = 4000
ALIGNMENT_ANSWER_MAX_CHARS = 12000

# A completed team-Agent Turn its author can still read in chat. `share` is the author's consent
# for this exact Turn and Agent; it is joined, never trusted on its own.
_TURN_SCOPE_FROM = """
FROM turn_agent_binding binding
JOIN turns turn ON turn.id=binding.turn_id
JOIN sessions session ON session.id=turn.session_id AND session.user_id=binding.user_id AND session.deleted_at IS NULL
JOIN workspaces space ON space.id=session.workspace_id AND space.team_id=binding.team_id AND space.status='active'
JOIN workspace_members space_member
  ON space_member.workspace_id=space.id AND space_member.user_id=session.user_id AND space_member.status='active'
JOIN users author ON author.id=binding.user_id AND author.status='active'
JOIN team_members team_member
  ON team_member.user_id=author.id AND team_member.team_id=binding.team_id AND team_member.status='active'
LEFT JOIN chat_turn_user_sources source ON source.turn_id=turn.id AND source.user_id=session.user_id
LEFT JOIN agent_eval_conversation_share share
  ON share.turn_id=turn.id AND share.user_id=binding.user_id AND share.team_id=binding.team_id
 AND share.agent_id=binding.requested_agent_id
LEFT JOIN agent_answer_feedback feedback
  ON feedback.turn_id=turn.id AND feedback.user_id=binding.user_id AND feedback.team_id=binding.team_id
 AND feedback.agent_id=binding.requested_agent_id
"""
_TURN_SCOPE_WHERE = """
binding.team_id=:team AND binding.agent_source_kind='team' AND session.origin='normal'
  AND turn.status='completed' AND turn.final_answer IS NOT NULL AND TRIM(turn.final_answer)!=''
  AND TRIM(COALESCE(source.user_text,turn.user_text))!=''
"""

# The Owner's view: their own Turns with the Agent, and Turns shared with the Agent's Owner.
_READABLE_TURNS_SQL = f"""
SELECT turn.id AS turn_id,
       turn.session_id AS session_id,
       binding.user_id AS author_id,
       COALESCE(source.user_text,turn.user_text) AS question,
       source.input_document_json AS input_document,
       turn.final_answer AS answer,
       COALESCE(turn.ended_at,turn.started_at) AS answered_at,
       CASE WHEN binding.user_id=:user THEN NULL
            ELSE COALESCE(NULLIF(TRIM(author.display_name),''),author.email) END AS shared_by,
       feedback.score AS feedback_score,
       feedback.reason_codes AS feedback_reason_codes,
       feedback.comment AS feedback_comment,
       feedback.updated_at AS feedback_at
{_TURN_SCOPE_FROM}
WHERE {_TURN_SCOPE_WHERE}
  AND binding.requested_agent_id=:agent
  AND (
    binding.user_id=:user
    OR (
      share.turn_id IS NOT NULL
      AND EXISTS(
        SELECT 1 FROM team_subagent agent
        WHERE agent.team_id=binding.team_id AND agent.id=binding.requested_agent_id
          AND COALESCE(agent.owner_user_id,agent.created_by)=:user
      )
    )
  )
"""  # noqa: S608 (fixed SQL fragments, values are bound)

# The author's view: their own Turns answered by a team Agent, with share and feedback state. Only
# Turns of an Agent someone else owns can be shared; the author's own feedback is always theirs to read.
_AUTHOR_TURNS_SQL = f"""
SELECT turn.id AS turn_id,
       binding.requested_agent_id AS agent_id,
       COALESCE(NULLIF(TRIM(agent.display_name),''),agent.name) AS agent_name,
       COALESCE(agent.owner_user_id,agent.created_by)!=:user AS shareable,
       COALESCE(NULLIF(TRIM(agent_owner.display_name),''),agent_owner.email) AS owner_name,
       share.turn_id IS NOT NULL AS shared,
       feedback.score AS feedback_score,
       feedback.reason_codes AS feedback_reason_codes,
       feedback.comment AS feedback_comment,
       feedback.updated_at AS feedback_at
{_TURN_SCOPE_FROM}
JOIN team_subagent agent ON agent.team_id=binding.team_id AND agent.id=binding.requested_agent_id
LEFT JOIN users agent_owner ON agent_owner.id=COALESCE(agent.owner_user_id,agent.created_by)
WHERE {_TURN_SCOPE_WHERE}
  AND binding.user_id=:user
"""  # noqa: S608 (fixed SQL fragments, values are bound)
_SHAREABLE_TURNS_SQL = _AUTHOR_TURNS_SQL + " AND COALESCE(agent.owner_user_id,agent.created_by)!=:user"

# The Owner's Pending list: other members' thumbs-downs on Turns they shared, not yet handled. Built on
# the readable-Turn query, so the share and every access check of its author apply.
_PENDING_FEEDBACK_SQL = (
    _READABLE_TURNS_SQL + " AND binding.user_id!=:user AND share.turn_id IS NOT NULL AND feedback.score=-1 AND feedback.owner_handled_at IS NULL"
)


class AgentEvalConversationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def list_conversation_candidates(
    db_path: str, *, team_id: str, agent_id: str, user_id: str, limit: int = MAX_CANDIDATES
) -> list[dict[str, Any]]:
    """Newest first; a question preview and who shared it, never the answer."""
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _READABLE_TURNS_SQL + " ORDER BY answered_at DESC,turn.id DESC LIMIT :limit",
            {"team": team_id, "agent": agent_id, "user": user_id, "limit": max(1, min(limit, MAX_CANDIDATES))},
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "turn_id": str(row["turn_id"]),
            "question": " ".join(_question(row).split())[:_QUESTION_PREVIEW_CHARS],
            "answered_at": str(row["answered_at"]),
            "shared_by": None if row["shared_by"] is None else str(row["shared_by"]),
            "feedback": _feedback(row),
        }
        for row in rows
    ]


async def read_conversation_turns(db_path: str, *, team_id: str, agent_id: str, user_id: str, turn_ids: Sequence[str]) -> list[dict[str, str]]:
    """Every requested turn, in request order, or `agent_eval_conversation_unavailable`.

    A turn the requester can't read (someone else's unshared or withdrawn Turn, or one that no longer
    passes its author's access checks) is reported the same way as one that doesn't exist, so the
    error doesn't reveal other people's conversations.
    """
    unique = list(dict.fromkeys(turn_ids))
    if not 1 <= len(unique) <= MAX_TURNS_PER_SUGGESTION or len(unique) != len(turn_ids):
        raise AgentEvalConversationError("agent_eval_conversation_selection_invalid")
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _READABLE_TURNS_SQL + " AND turn.id IN (SELECT value FROM json_each(:turns))",
            {"team": team_id, "agent": agent_id, "user": user_id, "turns": json.dumps(unique)},
        ) as cursor:
            rows = {str(row["turn_id"]): row for row in await cursor.fetchall()}
    if set(rows) != set(unique):
        raise AgentEvalConversationError("agent_eval_conversation_unavailable")
    return [
        {
            "turn_id": turn_id,
            "question": _question(rows[turn_id]).strip()[:_QUESTION_MAX_CHARS],
            "answer": str(rows[turn_id]["answer"]).strip()[:_ANSWER_MAX_CHARS],
        }
        for turn_id in unique
    ]


async def readable_turn_answers_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    user_id: str,
    turn_ids: Sequence[str] | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Turns the requester may use as evaluation answers, newest first, with question and answer.

    The alignment loop grades and labels these answers (alignment-loop technical design §5–§6). It
    reads through the same query as the candidate list, so only the requester's own Turns and Turns
    currently shared with the Agent's Owner come back; a Turn that is not readable is simply absent.
    """
    params: dict[str, Any] = {"team": team_id, "agent": agent_id, "user": user_id, "limit": max(1, min(limit, MAX_CANDIDATES))}
    sql = _READABLE_TURNS_SQL
    if turn_ids is not None:
        sql += " AND turn.id IN (SELECT value FROM json_each(:turns))"
        params["turns"] = json.dumps(list(dict.fromkeys(turn_ids)))
    async with db.execute(sql + " ORDER BY answered_at DESC,turn.id DESC LIMIT :limit", params) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "turn_id": str(row["turn_id"]),
            "session_id": str(row["session_id"]),
            "author_id": str(row["author_id"]),
            "question": _question(row).strip()[:ALIGNMENT_QUESTION_MAX_CHARS],
            "answer": str(row["answer"]).strip()[:ALIGNMENT_ANSWER_MAX_CHARS],
            "answered_at": str(row["answered_at"]),
            "shared_by": None if row["shared_by"] is None else str(row["shared_by"]),
            "feedback": _feedback(row),
        }
        for row in rows
    ]


def question_text(user_text: str, input_document_json: str | None) -> str:
    """The member's question without the @ mention of the Agent that answered it.

    A Turn sent with references stores its input document; its stored text starts with the "@Agent "
    display label, which is how the member addressed the Agent, not part of what they asked. Other
    references keep their "/label". Turns without a document, or with one that doesn't parse, keep
    their stored text.
    """

    text = str(user_text)
    if not input_document_json:
        return text
    try:
        parts = []
        for node in json.loads(input_document_json)["nodes"]:
            if node["type"] == "text":
                parts.append(str(node["text"]))
            elif node["resource"]["kind"] != "agent":
                parts.append("/" + str(node["label"]))
    except (ValueError, TypeError, KeyError):
        return text
    return "".join(parts).strip() or text


def _question(row: aiosqlite.Row) -> str:
    return question_text(row["question"], row["input_document"])


def _feedback(row: aiosqlite.Row) -> dict[str, Any] | None:
    """The thumbs-down on a row the query already admitted (own Turn, or shared with the Owner)."""
    if row["feedback_score"] is None:
        return None
    try:
        codes = json.loads(row["feedback_reason_codes"] or "[]")
    except ValueError:
        codes = []
    return {
        "score": int(row["feedback_score"]),
        "reason_codes": [str(code) for code in codes] if isinstance(codes, list) else [],
        "comment": row["feedback_comment"],
        "given_at": str(row["feedback_at"]),
    }


def _shareable_item(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "turn_id": str(row["turn_id"]),
        "agent_id": str(row["agent_id"]),
        "agent_name": str(row["agent_name"]),
        "shared": bool(row["shared"]),
    }


def _author_item(row: aiosqlite.Row) -> dict[str, Any]:
    # The member is told whose consent they give; a missing owner name leaves the line out.
    owner = row["owner_name"]
    return {
        **_shareable_item(row),
        "shareable": bool(row["shareable"]),
        "owner_name": None if owner is None else str(owner),
        "feedback": _feedback(row),
    }


async def list_shareable_turns(db_path: str, *, team_id: str, user_id: str, session_id: str) -> list[dict[str, Any]]:
    """The requester's own Turns in one session answered by a team Agent, with share and feedback state.

    `shareable` is false for an Agent the requester owns: those answers take a thumbs-down but need no
    share. Only team-Agent answers are listed, so the chat shows neither entry anywhere else. Someone
    else's session yields an empty list.
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _AUTHOR_TURNS_SQL + " AND session.id=:session ORDER BY turn.started_at,turn.id LIMIT :limit",
            {"team": team_id, "user": user_id, "session": session_id, "limit": MAX_SHAREABLE_TURNS_PER_SESSION},
        ) as cursor:
            rows = await cursor.fetchall()
    return [_author_item(row) for row in rows]


async def share_turn(db_path: str, *, team_id: str, user_id: str, turn_id: str) -> dict[str, Any]:
    """Let the Owner of the Agent that answered this Turn use it as an evaluation-case source.

    Repeating the share keeps the first consent. A Turn the requester can't share is reported as
    `turn_eval_share_unavailable`, the same for a missing Turn and for someone else's.
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _SHAREABLE_TURNS_SQL + " AND turn.id=:turn",
            {"team": team_id, "user": user_id, "turn": turn_id},
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentEvalConversationError("turn_eval_share_unavailable")
        await db.execute(
            "INSERT OR IGNORE INTO agent_eval_conversation_share(turn_id,team_id,agent_id,user_id,shared_at) VALUES(?,?,?,?,?)",
            (turn_id, team_id, str(row["agent_id"]), user_id, datetime.now(UTC).isoformat()),
        )
        await db.commit()
    return {**_shareable_item(row), "shared": True}


async def withdraw_turn_share(db_path: str, *, team_id: str, user_id: str, turn_id: str) -> dict[str, Any]:
    """Withdraw the requester's share of one Turn. Idempotent; cases already drafted from it stay the Owner's."""
    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "DELETE FROM agent_eval_conversation_share WHERE turn_id=? AND team_id=? AND user_id=?",
                (turn_id, team_id, user_id),
            )
            if cursor.rowcount:
                # The Owner's copies of this Turn in the alignment loop go with the consent, at once:
                # the question rows that show it and the calibration examples built from it (alignment
                # loop §6). Cases already saved from the question stay the Owner's (§8).
                for table in ("agent_eval_alignment_question", "agent_eval_point_example"):
                    await db.execute(f"DELETE FROM {table} WHERE turn_id=? OR source_turn_id=?", (turn_id, turn_id))  # noqa: S608
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return {"turn_id": turn_id, "shared": False}


def normalize_feedback_reasons(reason_codes: Sequence[str]) -> list[str]:
    """Codes in first-seen order without repeats; an unknown code is `answer_feedback_reason_invalid`."""
    result: list[str] = []
    for code in reason_codes:
        if code not in ANSWER_FEEDBACK_REASONS:
            raise AgentEvalConversationError("answer_feedback_reason_invalid")
        if code not in result:
            result.append(code)
    return result


async def record_answer_feedback(
    db_path: str, *, team_id: str, user_id: str, turn_id: str, reason_codes: Sequence[str], comment: str | None
) -> dict[str, Any]:
    """Give (or update) a thumbs-down on one of the requester's team-Agent answers.

    The reasons and comment stay the author's: the Owner sees them only while the Turn is shared with
    them. Updating keeps the Owner's Pending state. A Turn the requester can't rate is reported as
    `answer_feedback_unavailable`, the same for a missing Turn and for someone else's.
    """
    codes = normalize_feedback_reasons(reason_codes)
    text = (comment or "").strip()[:FEEDBACK_COMMENT_MAX_CHARS] or None
    now = datetime.now(UTC).isoformat()
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(_AUTHOR_TURNS_SQL + " AND turn.id=:turn", {"team": team_id, "user": user_id, "turn": turn_id}) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentEvalConversationError("answer_feedback_unavailable")
        await db.execute(
            """
            INSERT INTO agent_answer_feedback(turn_id,team_id,agent_id,user_id,score,reason_codes,comment,created_at,updated_at)
            VALUES(?,?,?,?,-1,?,?,?,?)
            ON CONFLICT(turn_id) DO UPDATE SET reason_codes=excluded.reason_codes,comment=excluded.comment,updated_at=excluded.updated_at
            """,
            (turn_id, team_id, str(row["agent_id"]), user_id, json.dumps(codes), text, now, now),
        )
        await db.commit()
        async with db.execute(_AUTHOR_TURNS_SQL + " AND turn.id=:turn", {"team": team_id, "user": user_id, "turn": turn_id}) as cursor:
            saved = await cursor.fetchone()
    if saved is None:
        raise AgentEvalConversationError("answer_feedback_unavailable")
    return _author_item(saved)


async def clear_answer_feedback(db_path: str, *, team_id: str, user_id: str, turn_id: str) -> dict[str, Any]:
    """Remove the requester's thumbs-down from one Turn. Idempotent; the share, if any, stays."""
    async with open_shared_ctx(db_path) as db:
        await db.execute(
            "DELETE FROM agent_answer_feedback WHERE turn_id=? AND team_id=? AND user_id=?",
            (turn_id, team_id, user_id),
        )
        await db.commit()
    return {"turn_id": turn_id, "feedback": None}


async def list_pending_feedback(
    db_path: str, *, team_id: str, agent_id: str, user_id: str, limit: int = MAX_PENDING_FEEDBACK
) -> list[dict[str, Any]]:
    """Members' shared thumbs-downs on this Agent the Owner hasn't handled yet, newest first.

    Only the Agent's current Owner gets items; anyone else gets an empty list.
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _PENDING_FEEDBACK_SQL + " ORDER BY feedback.updated_at DESC,turn.id DESC LIMIT :limit",
            {"team": team_id, "agent": agent_id, "user": user_id, "limit": max(1, min(limit, MAX_PENDING_FEEDBACK))},
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "turn_id": str(row["turn_id"]),
            "shared_by": str(row["shared_by"]),
            "question": " ".join(_question(row).split())[:_QUESTION_PREVIEW_CHARS],
            "answered_at": str(row["answered_at"]),
            "feedback": _feedback(row),
        }
        for row in rows
    ]


async def handle_pending_feedback(db_path: str, *, team_id: str, agent_id: str, user_id: str, turn_id: str, action: str) -> dict[str, Any]:
    """Take one shared thumbs-down off the Owner's Pending list: dismissed, or opened in Coach.

    The Turn stays a case candidate while it is shared. Handling an item that isn't on the list (never
    shared, withdrawn, already handled, or not this Owner's) is `agent_answer_feedback_unavailable`.
    """
    if action not in PENDING_FEEDBACK_ACTIONS:
        raise AgentEvalConversationError("agent_answer_feedback_action_invalid")
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _PENDING_FEEDBACK_SQL + " AND turn.id=:turn",
            {"team": team_id, "agent": agent_id, "user": user_id, "turn": turn_id},
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentEvalConversationError("agent_answer_feedback_unavailable")
        await db.execute(
            "UPDATE agent_answer_feedback SET owner_handled_at=?,owner_handled_action=? WHERE turn_id=? AND owner_handled_at IS NULL",
            (datetime.now(UTC).isoformat(), action, turn_id),
        )
        await db.commit()
    return {"turn_id": turn_id, "handled": action}
