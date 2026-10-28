"""The Coach alignment loop: the Owner confirms a point by labelling Coach's verdicts on real answers.

Alignment-loop technical design §3–§8. Coach starts a loop from the Owner's words, drafts a point and
asks about a few answers; the server picks and grades them with the production judge (one answer per
call) and stores one question row per answer. Only the Owner's own session writes labels
(`label`), and the confirmation that follows is computed here from those labels, never by Coach.
Confirming writes the point into a new suite revision, the labelled answers as calibration examples
and, when every label agrees with the pinned judge, a calibrated outcome row, all in one transaction.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

import aiosqlite
import anyio

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_conversation_cases import readable_turn_answers_conn
from bowhead_core.agent_eval_judge import POINTS_PROMPT_VERSION, EvalPointsGrading
from bowhead_core.agent_eval_point_calibration import (
    CalibrationWriter,
    _attempt_answer_conn,
    _insert_example_conn,
    _parse_attempt_ref,
    is_minimal_edit,
    point_text_hash,
)
from bowhead_core.agent_eval_points import ExpectedPoint, is_held_out
from bowhead_core.agent_eval_product import _require_agent_owner
from bowhead_core.agent_eval_store import AgentEvalStoreError, EvalCaseDraft, append_eval_suite_cases_conn

_log = logging.getLogger(__name__)

PickReason = Literal["marked_bad", "judge_abstained", "feedback_conflict", "recent", "counterexample", "generated"]
OwnerLabel = Literal["right", "wrong", "unclear"]
Outcome = Literal["confirm", "rewrite", "split_or_informational", "unclear", "need_counterexample", "recheck_row", "continue"]

# A card starts with two real answers plus one minimal-edit counterexample (R4, R6); "Can't tell"
# pulls replacements up to five rows (R8).
REAL_ROWS_PER_CARD = 2
MAX_CARD_ROWS = 5
CONFIRM_RIGHT_LABELS = 3
UNCLEAR_LIMIT = 3
# After two of Coach's rewrites still disagree, Coach suggests splitting the point (R7).
MAX_REWRITES = 2
MAX_DRAFT_POINTS = 6
MAX_EXPLICIT_ANSWERS = 4
# Answers graded to choose a card's rows: the rows needed plus a little room to rank them.
PICK_POOL_EXTRA = 2
MAX_PICK_POOL = 8
EXCERPT_CHARS = 1200
TASK_CHARS = 4000
ANSWER_CHARS = 12000
WORDS_CHARS = 500
_PICK_RANK: dict[str, int] = {"marked_bad": 0, "judge_abstained": 1, "feedback_conflict": 2, "recent": 3}
_ALIGNED_KINDS = frozenset({"data_fact", "judgment"})
_RESOLVED_OUTCOMES = frozenset({"unclear", "split_or_informational"})
_LOOP_ID = re.compile(r"^aeal_[0-9a-f]{32}$")
_QUESTION_ID = re.compile(r"^aeaq_[0-9a-f]{32}$")
# Product defaults for a case the loop adds: the same as cases drafted from conversations.
_CASE_SCORER_CONFIG = {"max_cost_microusd": 5_000_000, "max_duration_ms": 120_000}


# What an Owner label means, for Coach's reads. Coach once told an Owner that "right" means the answer
# did what was asked; the card asks whether the evaluation's verdict on the answer is right.
LABEL_MEANING = (
    "Each label is the Owner's check of the evaluation's verdict on that answer, not a rating of the answer: "
    "right = the verdict is correct, wrong = the verdict is incorrect, unclear = the Owner can't tell. "
    "The card explains this to the Owner; don't restate or paraphrase the labels."
)

FEATURE_FLAG = "FEATURE_AGENT_EVAL_ALIGNMENT"
# Coach's alignment-loop Tools; offered and dispatched only while the switch is on.
ALIGNMENT_TOOLS = frozenset(
    {
        "agent_eval_turns_list",
        "agent_eval_alignment_start",
        "agent_eval_alignment_ask",
        "agent_eval_alignment_get",
        "agent_eval_alignment_report",
    }
)


def alignment_enabled() -> bool:
    """Deployment-wide rollout switch for the Coach alignment loop (rollout register: Coach alignment loop).

    On by default; ``0``, ``false``, ``no`` or ``off`` turn it off as a kill switch, hiding the loop's
    Coach Tools, Owner routes and Chat cards. Held-out redaction, Turn readability rechecks and
    share-withdrawal deletes stay on regardless.
    """
    import os

    return os.environ.get(FEATURE_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


class AgentEvalAlignmentError(AgentEvalStoreError):
    """An alignment-loop failure with its HTTP status and safe details for an actionable message."""

    def __init__(self, code: str, *, status: int = 422, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class AlignmentQuestion:
    id: str
    point_id: str
    point_text_hash: str
    card_seq: int
    row_index: int
    answer_ref: str
    turn_id: str | None
    answer_text: str | None
    task_text: str
    answer_excerpt: str
    judge_met: bool | None
    judge_quote: str | None
    judge_reason: str | None
    graded_prompt_version: str
    graded_model_fingerprint: str
    pick_reason: str
    replacement: bool
    owner_label: str | None
    label_via: str | None
    labelled_at: str | None
    created_at: str
    # The Turn a generated row was written from (its question, or a minimal edit of its answer).
    source_turn_id: str | None = None

    @property
    def linked_turn_id(self) -> str | None:
        return self.turn_id or self.source_turn_id


@dataclass(frozen=True, slots=True)
class AlignmentLoop:
    id: str
    team_id: str
    agent_id: str
    suite_id: str
    owner_id: str
    session_id: str
    owner_words: str | None
    words_by: str | None
    origin_turn_id: str | None
    judge_prompt_version: str
    judge_model_fingerprint: str
    pinned_at: str
    state: str
    baseline_comparison_id: str | None
    points: tuple[dict[str, Any], ...]
    report: dict[str, Any] | None
    created_at: str
    updated_at: str
    # "owner": the Owner's own words; "member_feedback": a member's thumbs-down, whose comment is never stored.
    words_source: str = "owner"

    def point(self, point_id: str) -> dict[str, Any]:
        found = next((point for point in self.points if point["id"] == point_id), None)
        if found is None:
            raise AgentEvalAlignmentError("agent_eval_alignment_point_not_found", status=404, details={"point_id": point_id})
        return found


# ── outcome rules (technical design §5.4) ───────────────────────────────


def epoch_questions(questions: Sequence[AlignmentQuestion], point: Mapping[str, Any]) -> list[AlignmentQuestion]:
    """Rows that count for the point now: its current text, asked since the text last changed."""

    text_hash = point_text_hash(str(point["text"]))
    since = int(point.get("epoch_card") or 1)
    return [q for q in questions if q.point_id == point["id"] and q.point_text_hash == text_hash and q.card_seq >= since]


def attention_check_failed(questions: Sequence[AlignmentQuestion]) -> list[str]:
    """Counterexamples the judge graded met that the Owner passed only through "The rest are judged right".

    An explicit per-row "Judged right" on the counterexample, or "Judged wrong", is a normal label.
    """

    return [
        q.id
        for q in questions
        if q.pick_reason == "counterexample" and q.judge_met is True and q.owner_label == "right" and q.label_via == "rest_right"
    ]


def alignment_outcome(questions: Sequence[AlignmentQuestion], *, rewrites: int) -> Outcome:
    """The next step for a point from the Owner's labels on its current text (precedence top to bottom)."""

    labelled = [q for q in questions if q.owner_label is not None]
    if any(q.owner_label == "wrong" for q in labelled):
        return "split_or_informational" if rewrites >= MAX_REWRITES else "rewrite"
    if sum(q.owner_label == "unclear" for q in labelled) >= UNCLEAR_LIMIT:
        return "unclear"
    if attention_check_failed(labelled):
        return "recheck_row"
    # A "Judged right" on a verdict the judge abstained from says nothing about met or unmet.
    rights = [q for q in labelled if q.owner_label == "right" and q.judge_met is not None]
    if len(rights) >= CONFIRM_RIGHT_LABELS:
        verdicts = {q.judge_met for q in rights}
        attention_passed = any(q.pick_reason == "counterexample" for q in rights)
        if verdicts == {True, False} and attention_passed:
            return "confirm"
        return "need_counterexample"
    return "continue"


def point_outcome(questions: Sequence[AlignmentQuestion], point: Mapping[str, Any]) -> Outcome | Literal["confirmed"]:
    if point.get("status") == "confirmed":
        return "confirmed"
    return alignment_outcome(epoch_questions(questions, point), rewrites=int(point.get("rewrites") or 0))


# ── answer picking (technical design §5.1–§5.3) ─────────────────────────


@dataclass(slots=True)
class AnswerCandidate:
    answer_ref: str
    task_text: str
    answer: str
    group: str
    answered_at: str
    marked_bad: bool = False
    turn_id: str | None = None
    met: bool | None = None
    quote: str | None = None
    reason: str | None = None
    graded: bool = False
    # The Turn the loop started from: the answer the Owner's words are about, so it leads its session.
    origin: bool = False
    # For a generated answer: the Turn its question or answer was taken from.
    source_turn_id: str | None = None
    # What the judge grades when it differs from the text shown: a Live attempt's structured answer, graded
    # as a check grades it, while the card shows its Chat text.
    graded_output: Any = None


def reader_answer(raw: str) -> tuple[str, Any]:
    """`(text a reader sees, output the judge grades)` for a stored answer.

    A Live attempt stores the Agent's structured answer as JSON; its Chat text is `exec_summary`. Showing
    the JSON put `{"delivery_surface": …}` on the card (116 dogfood, 1.5.84). Anything else is shown and
    graded as stored.
    """

    text = raw.strip()
    if not text.startswith("{"):
        return raw, None
    try:
        payload = json.loads(text)
    except ValueError:
        return raw, None
    summary = payload.get("exec_summary") if isinstance(payload, dict) else None
    if not isinstance(summary, str) or not summary.strip():
        return raw, None
    return summary.strip(), payload


def classify(candidate: AnswerCandidate) -> str:
    if candidate.marked_bad and candidate.met is True:
        return "feedback_conflict"
    if candidate.marked_bad:
        return "marked_bad"
    if candidate.met is None:
        return "judge_abstained"
    return "recent"


def pick_order(candidates: Sequence[AnswerCandidate]) -> list[AnswerCandidate]:
    """The loop's origin Turn first, then thumbs-downs, abstentions, verdicts that contradict feedback
    and the rest.

    One answer per session: two answers from one conversation say the same thing, so a session is
    represented by its most relevant answer, never merely its first.
    """

    ordered = sorted(enumerate(candidates), key=lambda item: (not item[1].origin, _PICK_RANK[classify(item[1])], item[0]))
    seen: set[str] = set()
    chosen: list[AnswerCandidate] = []
    for _index, candidate in ordered:
        if candidate.group in seen:
            continue
        seen.add(candidate.group)
        chosen.append(candidate)
    return chosen


def pool_order(candidates: Sequence[AnswerCandidate]) -> list[AnswerCandidate]:
    """Before grading: the loop's origin Turn first, then thumbs-downs, then newest, one per session."""

    ordered = sorted(enumerate(candidates), key=lambda item: (not item[1].origin, not item[1].marked_bad, item[0]))
    seen: set[str] = set()
    pool: list[AnswerCandidate] = []
    for _index, candidate in ordered:
        if candidate.group not in seen:
            seen.add(candidate.group)
            pool.append(candidate)
    return pool


def excerpt_around(answer: str, quote: str | None, limit: int = EXCERPT_CHARS) -> str:
    """The part of the answer a reader needs: around the judge's quote when there is one."""

    text = answer.strip()
    if len(text) <= limit:
        return text
    position = text.find(quote.strip()) if quote else -1
    if position < 0:
        return text[:limit]
    start = max(0, min(position - limit // 3, len(text) - limit))
    return text[start : start + limit]


# ── the judge and Coach's writer as the service sees them ───────────────


class AlignmentJudge(Protocol):
    async def grade_points(
        self,
        *,
        task: str,
        output: Any,
        points: Sequence[Mapping[str, Any]],
        role_ref: str,
        expected_model_fingerprint: str,
    ) -> EvalPointsGrading: ...


# ── storage ──────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _loop_from_row(row: Mapping[str, Any]) -> AlignmentLoop:
    return AlignmentLoop(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        suite_id=str(row["suite_id"]),
        owner_id=str(row["owner_id"]),
        session_id=str(row["session_id"]),
        owner_words=row["owner_words"],
        words_by=row["words_by"],
        words_source=str(row["words_source"]),
        origin_turn_id=row["origin_turn_id"],
        judge_prompt_version=str(row["judge_prompt_version"]),
        judge_model_fingerprint=str(row["judge_model_fingerprint"]),
        pinned_at=str(row["pinned_at"]),
        state=str(row["state"]),
        baseline_comparison_id=row["baseline_comparison_id"],
        points=tuple(json.loads(str(row["draft_points_json"]))),
        report=json.loads(row["report_json"]) if row["report_json"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _question_from_row(row: Mapping[str, Any]) -> AlignmentQuestion:
    return AlignmentQuestion(
        id=str(row["id"]),
        point_id=str(row["point_id"]),
        point_text_hash=str(row["point_text_hash"]),
        card_seq=int(row["card_seq"]),
        row_index=int(row["row_index"]),
        answer_ref=str(row["answer_ref"]),
        turn_id=row["turn_id"],
        answer_text=row["answer_text"],
        task_text=str(row["task_text"]),
        answer_excerpt=str(row["answer_excerpt"]),
        judge_met=None if row["judge_met"] is None else bool(row["judge_met"]),
        judge_quote=row["judge_quote"],
        judge_reason=row["judge_reason"],
        graded_prompt_version=str(row["graded_prompt_version"]),
        graded_model_fingerprint=str(row["graded_model_fingerprint"]),
        pick_reason=str(row["pick_reason"]),
        replacement=bool(row["replacement"]),
        owner_label=row["owner_label"],
        label_via=row["label_via"],
        labelled_at=row["labelled_at"],
        created_at=str(row["created_at"]),
        source_turn_id=row["source_turn_id"],
    )


async def _loop_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str, loop_id: str) -> AlignmentLoop:
    """The Owner's own loop; anyone else (other members, other Owners, admins) gets "not found"."""

    await _require_agent_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    if not _LOOP_ID.fullmatch(loop_id):
        raise AgentEvalAlignmentError("agent_eval_alignment_not_found", status=404)
    async with db.execute(
        "SELECT * FROM agent_eval_alignment_loop WHERE id=? AND team_id=? AND agent_id=? AND owner_id=?",
        (loop_id, team_id, agent_id, actor_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalAlignmentError("agent_eval_alignment_not_found", status=404)
    return _loop_from_row(row)


async def _questions_conn(db: aiosqlite.Connection, loop_id: str) -> list[AlignmentQuestion]:
    async with db.execute("SELECT * FROM agent_eval_alignment_question WHERE loop_id=? ORDER BY card_seq,row_index", (loop_id,)) as cursor:
        return [_question_from_row(row) for row in await cursor.fetchall()]


async def _save_points_conn(db: aiosqlite.Connection, loop: AlignmentLoop, points: Sequence[Mapping[str, Any]], *, state: str | None = None) -> None:
    await db.execute(
        "UPDATE agent_eval_alignment_loop SET draft_points_json=?,state=?,updated_at=? WHERE id=?",
        (json.dumps(list(points), ensure_ascii=False), state or loop.state, _now(), loop.id),
    )


async def _owner_suite_revision_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, suite_id: str | None) -> tuple[str, str]:
    """The Owner suite the loop writes into and its latest revision."""

    async with db.execute(
        """
        SELECT suite.id,(SELECT revision.id FROM agent_eval_suite_revision revision
                         WHERE revision.suite_id=suite.id ORDER BY revision.sequence DESC LIMIT 1) AS revision_id
        FROM agent_eval_suite suite
        WHERE suite.team_id=? AND suite.agent_id=? AND suite.agent_source_kind='team' AND suite.suite_kind='owner_quality'
          AND suite.state!='archived' AND (? IS NULL OR suite.id=?)
        ORDER BY suite.created_at,suite.id LIMIT 1
        """,
        (team_id, agent_id, suite_id, suite_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[1] is None:
        # Actionable: the Owner creates a suite first (Coach proposes an initial suite).
        raise AgentEvalAlignmentError("agent_eval_alignment_suite_required", details={"agent_id": agent_id})
    return str(row[0]), str(row[1])


async def _readable_turns_conn(db: aiosqlite.Connection, loop: AlignmentLoop, turn_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not turn_ids:
        return {}
    rows = await readable_turn_answers_conn(
        db, team_id=loop.team_id, agent_id=loop.agent_id, user_id=loop.owner_id, turn_ids=list(turn_ids), limit=len(set(turn_ids))
    )
    return {row["turn_id"]: row for row in rows}


class AgentEvalAlignmentStore:
    """Loops, question rows and the Owner's labels; every read and write is the loop Owner's."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def start(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        session_id: str,
        words: str | None,
        origin_turn_id: str | None,
        words_from_feedback: bool,
        judge_model_fingerprint: str,
        suite_id: str | None = None,
    ) -> AlignmentLoop:
        """Create the Owner's loop and pin the judge; an earlier open loop on this Agent is abandoned."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_agent_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                suite, _revision = await _owner_suite_revision_conn(db, team_id=team_id, agent_id=agent_id, suite_id=suite_id)
                words_source, words_by = "owner", actor_user_id
                text: str | None = " ".join((words or "").split())
                if origin_turn_id is not None:
                    rows = await readable_turn_answers_conn(
                        db, team_id=team_id, agent_id=agent_id, user_id=actor_user_id, turn_ids=[origin_turn_id], limit=1
                    )
                    if not rows:
                        raise AgentEvalAlignmentError("agent_eval_conversation_unavailable", status=404)
                    if words_from_feedback:
                        feedback = rows[0]["feedback"] or {}
                        comment = " ".join(str(feedback.get("comment") or "").split())
                        if not comment:
                            # R9: Coach asks the Owner "What's wrong with this answer?" and starts with the reply.
                            raise AgentEvalAlignmentError("agent_eval_alignment_feedback_comment_missing", details={"turn_id": origin_turn_id})
                        # The member's comment stays theirs: it is read live through the share, never copied.
                        words_source, words_by, text = "member_feedback", None, None
                elif words_from_feedback:
                    raise AgentEvalAlignmentError("agent_eval_alignment_origin_turn_required")
                if words_source == "owner":
                    if not text:
                        raise AgentEvalAlignmentError("agent_eval_alignment_words_required")
                    text = text[:WORDS_CHARS]
                await db.execute(
                    "UPDATE agent_eval_alignment_loop SET state='abandoned',updated_at=? WHERE team_id=? AND agent_id=? AND owner_id=? AND state!='abandoned'",
                    (_now(), team_id, agent_id, actor_user_id),
                )
                loop_id = f"aeal_{uuid4().hex}"
                timestamp = _now()
                await db.execute(
                    """
                    INSERT INTO agent_eval_alignment_loop(
                      id,team_id,agent_id,suite_id,owner_id,session_id,words_source,owner_words,words_by,origin_turn_id,
                      judge_prompt_version,judge_model_fingerprint,pinned_at,state,draft_points_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'aligning','[]',?,?)
                    """,
                    (
                        loop_id,
                        team_id,
                        agent_id,
                        suite,
                        actor_user_id,
                        session_id,
                        words_source,
                        text,
                        words_by,
                        origin_turn_id,
                        POINTS_PROMPT_VERSION,
                        judge_model_fingerprint,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return loop

    async def read(self, *, team_id: str, agent_id: str, actor_user_id: str, loop_id: str) -> tuple[AlignmentLoop, list[AlignmentQuestion]]:
        """The loop and its card rows. Rows from a Turn the Owner can no longer read are dropped first (§6),
        so neither the Owner's view nor Coach's read ever shows them."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                await _drop_unreadable_conn(db, loop, await _questions_conn(db, loop.id))
                questions = await _questions_conn(db, loop.id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            return loop, questions

    async def open_loop(self, *, team_id: str, actor_user_id: str, agent_id: str | None, session_id: str | None) -> tuple[str, str] | None:
        """`(agent_id, loop_id)` of the caller's current open loop, for a Coach that wasn't told the loop id.

        With an Agent, its open loop (starting a loop abandons the previous one, so there is at most one);
        without one, the newest open loop started in this conversation. Only the caller's own loops are
        found; the caller then reads the loop through `read`, which rechecks ownership and Turn
        readability like every other read."""

        if agent_id is None and not session_id:
            return None
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT agent_id,id FROM agent_eval_alignment_loop
                WHERE team_id=? AND owner_id=? AND state!='abandoned'
                  AND (? IS NULL OR agent_id=?) AND (? IS NOT NULL OR session_id=?)
                ORDER BY updated_at DESC,id DESC LIMIT 1
                """,
                (team_id, actor_user_id, agent_id, agent_id, agent_id, session_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return (str(row[0]), str(row[1])) if row is not None else None

    async def sharers(self, loop: AlignmentLoop, questions: Sequence[AlignmentQuestion]) -> dict[str, str]:
        """`{question_id: shared_by}` for rows from a Turn another member shared, read live through the
        readable-Turn query: a withdrawn share reads as nothing. For the Owner's view only."""

        turn_ids = sorted({q.turn_id for q in questions if q.turn_id})
        if not turn_ids:
            return {}
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            readable = await _readable_turns_conn(db, loop, turn_ids)
        return {q.id: str(readable[q.turn_id]["shared_by"]) for q in questions if q.turn_id in readable and readable[q.turn_id].get("shared_by")}

    async def feedback_comment(self, loop: AlignmentLoop) -> str | None:
        """A member's thumbs-down comment, read live while the Owner may read its Turn (never stored)."""

        if loop.words_source != "member_feedback" or loop.origin_turn_id is None:
            return None
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            readable = await _readable_turns_conn(db, loop, [loop.origin_turn_id])
        feedback = (readable.get(loop.origin_turn_id) or {}).get("feedback") or {}
        comment = feedback.get("comment")
        return " ".join(str(comment).split())[:WORDS_CHARS] if comment else None

    async def prepare_point(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        loop_id: str,
        point_id: str | None,
        kind: str,
        text: str,
    ) -> dict[str, Any]:
        """Add Coach's drafted point to the loop, or apply Coach's rewrite after a "Judged wrong".

        Nothing reaches a suite here; the text lives in the loop until the Owner's labels confirm it.
        """

        if kind not in _ALIGNED_KINDS:
            # Required actions are checked by rules; there is no model verdict to label.
            raise AgentEvalAlignmentError("agent_eval_alignment_point_kind_invalid", details={"kind": kind})
        text = " ".join(text.split())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                if loop.state == "abandoned":
                    raise AgentEvalAlignmentError("agent_eval_alignment_closed", status=409)
                questions = await _questions_conn(db, loop.id)
                points = [dict(point) for point in loop.points]
                existing = next((point for point in points if point["id"] == point_id), None) if point_id else None
                if point_id and existing is None:
                    # New points get server ids, so one can never collide with a suite point or its calibration.
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_not_found", status=404, details={"point_id": point_id})
                if existing is None:
                    blocking = [
                        point["id"]
                        for point in points
                        if point.get("status") != "confirmed"
                        and epoch_questions(questions, point)
                        and point_outcome(questions, point) not in _RESOLVED_OUTCOMES
                    ]
                    if blocking:
                        # One question card at a time (§5.5): finish the open card first.
                        raise AgentEvalAlignmentError("agent_eval_alignment_card_open", status=409, details={"point_id": blocking[0]})
                    if len(points) >= MAX_DRAFT_POINTS:
                        raise AgentEvalAlignmentError("agent_eval_alignment_too_many_points", details={"limit": MAX_DRAFT_POINTS})
                    new_id = await _new_point_id_conn(db, loop, points)
                    existing = {
                        "id": new_id,
                        "kind": kind,
                        "text": text,
                        "origin": _origin(loop),
                        "status": "aligning",
                        "rewrites": 0,
                        "previous_text": None,
                        "epoch_card": 1,
                    }
                    _validate_point(existing)
                    points.append(existing)
                elif existing.get("status") == "confirmed":
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_resolved", status=409, details={"point_id": existing["id"]})
                elif existing["text"] != text or existing["kind"] != kind:
                    asked = epoch_questions(questions, existing)
                    outcome = point_outcome(questions, existing)
                    if asked and outcome not in {"rewrite", "split_or_informational", "unclear"}:
                        # The Owner rewords a point that has labels; Coach rewrites only after "Judged wrong".
                        raise AgentEvalAlignmentError("agent_eval_alignment_point_text_locked", status=409, details={"point_id": existing["id"]})
                    if asked:
                        existing["rewrites"] = int(existing.get("rewrites") or 0) + 1
                        existing["previous_text"] = existing["text"]
                    existing.update(kind=kind, text=text, epoch_card=_next_card(questions))
                    _validate_point(existing)
                await _save_points_conn(db, loop, points, state="aligning")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return existing

    async def add_rows(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        loop_id: str,
        point_id: str,
        text_hash: str,
        rows: Sequence[Mapping[str, Any]],
        replacement: bool,
    ) -> tuple[int, list[AlignmentQuestion]]:
        """Store a new question card, or replacement rows on the point's latest card."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                point = loop.point(point_id)
                if point_text_hash(str(point["text"])) != text_hash:
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_changed", status=409, details={"point_id": point_id})
                questions = await _questions_conn(db, loop.id)
                if replacement:
                    current = epoch_questions(questions, point)
                    if not current:
                        raise AgentEvalAlignmentError("agent_eval_alignment_card_missing", status=409, details={"point_id": point_id})
                    card_seq = max(q.card_seq for q in current)
                    used = [q.row_index for q in questions if q.card_seq == card_seq]
                else:
                    card_seq, used = _next_card(questions), []
                if len(used) + len(rows) > MAX_CARD_ROWS:
                    raise AgentEvalAlignmentError("agent_eval_alignment_card_full", status=409, details={"limit": MAX_CARD_ROWS})
                timestamp = _now()
                free = [index for index in range(1, MAX_CARD_ROWS + 1) if index not in used]
                for row_index, row in zip(free, rows, strict=False):
                    question_id = str(row.get("id") or f"aeaq_{uuid4().hex}")
                    ref = str(row["answer_ref"])
                    await db.execute(
                        """
                        INSERT INTO agent_eval_alignment_question(
                          id,loop_id,point_id,point_text_hash,card_seq,row_index,answer_ref,turn_id,source_turn_id,answer_text,
                          task_text,answer_excerpt,judge_met,judge_quote,judge_reason,graded_prompt_version,
                          graded_model_fingerprint,pick_reason,replacement,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            question_id,
                            loop.id,
                            point_id,
                            text_hash,
                            card_seq,
                            row_index,
                            ref,
                            ref[5:] if ref.startswith("turn:") else None,
                            row.get("source_turn_id") if ref.startswith("coach:") else None,
                            row.get("answer_text"),
                            str(row["task_text"])[:TASK_CHARS],
                            str(row["answer_excerpt"])[:EXCERPT_CHARS],
                            None if row.get("judge_met") is None else int(bool(row["judge_met"])),
                            (row.get("judge_quote") or None) and str(row["judge_quote"])[:300],
                            (row.get("judge_reason") or None) and str(row["judge_reason"])[:120],
                            loop.judge_prompt_version,
                            loop.judge_model_fingerprint,
                            row["pick_reason"],
                            int(replacement),
                            timestamp,
                        ),
                    )
                await db.execute("UPDATE agent_eval_alignment_loop SET updated_at=? WHERE id=?", (timestamp, loop.id))
                await db.commit()
                return card_seq, [q for q in await _questions_conn(db, loop.id) if q.card_seq == card_seq]
            except BaseException:
                await db.rollback()
                raise

    async def label(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        loop_id: str,
        labels: Sequence[Mapping[str, str]],
        rest_right: bool,
        card_seq: int,
    ) -> dict[str, Any]:
        """Record the Owner's labels on one card and confirm a point the labels now confirm.

        Labels and "The rest are judged right" apply to `card_seq` only, and only while it is its
        point's current card: pressing it on an older card must never label rows the Owner never saw.

        Only the Owner's session reaches this (the labels route); no Coach Tool calls it. The point
        is confirmed here, by the server, from the labels — in the same transaction that writes them.
        """

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                if loop.state == "abandoned":
                    raise AgentEvalAlignmentError("agent_eval_alignment_closed", status=409)
                # Every Turn-backed row is checked again, not only the ones labelled now: confirmation and
                # the new case read the whole epoch (§6).
                dropped = await _drop_unreadable_conn(db, loop, await _questions_conn(db, loop.id))
                questions = {q.id: q for q in await _questions_conn(db, loop.id)}
                points = {point["id"]: dict(point) for point in loop.points}
                on_card = [q for q in questions.values() if q.card_seq == card_seq]
                current_card = _current_card(list(questions.values()), on_card[0].point_id if on_card else None)
                if not on_card or current_card != card_seq:
                    raise AgentEvalAlignmentError("agent_eval_alignment_card_stale", status=409, details={"card_seq": current_card})
                explicit: dict[str, str] = {}
                for item in labels:
                    if item.get("label") not in {"right", "wrong", "unclear"}:
                        raise AgentEvalAlignmentError("agent_eval_alignment_label_invalid", details={"question_id": item.get("question_id")})
                    question = questions.get(str(item.get("question_id")))
                    if question is None:
                        # Deleted with its Turn (share withdrawn, conversation deleted): it left the card,
                        # like a row dropped now. The rest of the request still applies.
                        dropped.add(str(item.get("question_id")))
                        continue
                    if question.card_seq != card_seq:
                        raise AgentEvalAlignmentError("agent_eval_alignment_card_stale", status=409, details={"card_seq": current_card})
                    explicit[question.id] = str(item["label"])
                targets = list(explicit)
                if rest_right:
                    targets += [q.id for q in on_card if q.owner_label is None and q.id not in explicit]
                if not targets and not dropped:
                    raise AgentEvalAlignmentError("agent_eval_alignment_labels_empty")
                # Graded on other text, or asked before the current epoch (a reword or re-pin): not labelable.
                current_ids = {q.id for point in points.values() for q in epoch_questions(list(questions.values()), point)}
                stale = sorted(question_id for question_id in targets if question_id not in current_ids)
                if stale:
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_changed", status=409, details={"question_ids": stale})
                resolved = sorted({questions[q].point_id for q in targets if points[questions[q].point_id].get("status") == "confirmed"})
                if resolved:
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_resolved", status=409, details={"point_ids": resolved})
                timestamp = _now()
                for question_id in targets:
                    label, via = (explicit[question_id], "row") if question_id in explicit else ("right", "rest_right")
                    question = questions[question_id]
                    if (question.owner_label, question.label_via) == (label, via):
                        continue  # idempotent per question
                    await db.execute(
                        "UPDATE agent_eval_alignment_question SET owner_label=?,label_via=?,labelled_at=? WHERE id=?",
                        (label, via, timestamp, question_id),
                    )
                current = await _questions_conn(db, loop.id)
                touched = {questions[q].point_id for q in targets}
                outcomes: dict[str, str] = {}
                confirmed: dict[str, dict[str, Any]] = {}
                for point_id in sorted(touched):
                    point = points[point_id]
                    outcome = point_outcome(current, point)
                    outcomes[point_id] = outcome
                    if outcome == "confirm":
                        confirmed[point_id] = await _confirm_conn(db, loop, point, epoch_questions(current, point))
                        point.update(confirmed[point_id]["point_state"])
                        outcomes[point_id] = "confirmed"
                if confirmed:
                    open_points = [point for point in points.values() if point.get("status") != "confirmed"]
                    await _save_points_conn(db, loop, list(points.values()), state="aligning" if open_points else "fixing")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "outcomes": outcomes,
            "confirmed": {key: {k: v for k, v in value.items() if k != "point_state"} for key, value in confirmed.items()},
            "dropped_question_ids": sorted(dropped),
            "recheck_question_ids": attention_check_failed(
                [q for q in current if q.point_id in touched and q.point_text_hash == point_text_hash(str(points[q.point_id]["text"]))]
            ),
        }

    async def reword(self, *, team_id: str, agent_id: str, actor_user_id: str, loop_id: str, point_id: str, text: str) -> dict[str, Any]:
        """The Owner rewords a point on its card: alignment restarts on the new text (R9).

        Rewording back to the text Coach replaced is "Keep the original"; the next card asks two more answers.
        """

        text = " ".join(text.split())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                if loop.state == "abandoned":
                    raise AgentEvalAlignmentError("agent_eval_alignment_closed", status=409)
                points = [dict(point) for point in loop.points]
                point = next((item for item in points if item["id"] == point_id), None)
                if point is None:
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_not_found", status=404, details={"point_id": point_id})
                if point.get("status") == "confirmed":
                    raise AgentEvalAlignmentError("agent_eval_alignment_point_resolved", status=409, details={"point_id": point_id})
                if text != point["text"]:
                    kept_original = text == point.get("previous_text")
                    point.update(
                        text=text,
                        previous_text=None if kept_original else point["text"],
                        epoch_card=_next_card(await _questions_conn(db, loop.id)),
                        kept_original=kept_original,
                    )
                    _validate_point(point)
                    await _save_points_conn(db, loop, points, state="aligning")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return point

    async def repin(self, *, team_id: str, agent_id: str, actor_user_id: str, loop_id: str, judge_model_fingerprint: str) -> AlignmentLoop:
        """Re-pin the loop to the current judge; checks graded before now no longer count (R13)."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                if loop.state == "abandoned":
                    raise AgentEvalAlignmentError("agent_eval_alignment_closed", status=409)
                open_points = [point for point in loop.points if point.get("status") != "confirmed"]
                # Verdicts graded under the old judge stop counting: every open point starts a new epoch.
                next_card = _next_card(await _questions_conn(db, loop.id))
                points = [point if point.get("status") == "confirmed" else {**point, "epoch_card": next_card} for point in loop.points]
                await _save_points_conn(db, loop, points)
                timestamp = _now()
                await db.execute(
                    """
                    UPDATE agent_eval_alignment_loop
                    SET judge_prompt_version=?,judge_model_fingerprint=?,pinned_at=?,baseline_comparison_id=NULL,report_json=NULL,
                        state=?,updated_at=?
                    WHERE id=?
                    """,
                    (POINTS_PROMPT_VERSION, judge_model_fingerprint, timestamp, "aligning" if open_points else "fixing", timestamp, loop.id),
                )
                loop = await _loop_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return loop

    async def save_report(self, *, loop: AlignmentLoop, report: Mapping[str, Any], baseline_comparison_id: str | None) -> None:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE agent_eval_alignment_loop SET report_json=?,baseline_comparison_id=?,state=?,updated_at=?
                    WHERE id=? AND state!='abandoned' AND pinned_at=?
                    """,
                    (
                        json.dumps(dict(report), ensure_ascii=False),
                        baseline_comparison_id,
                        "reported" if report.get("status") == "measured" else loop.state,
                        _now(),
                        loop.id,
                        loop.pinned_at,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise


def _current_card(questions: Sequence[AlignmentQuestion], point_id: str | None) -> int | None:
    """The point's newest card: the only one the Owner can label."""

    if point_id is None:
        return max((q.card_seq for q in questions), default=None)
    return max((q.card_seq for q in questions if q.point_id == point_id), default=None)


def _next_card(questions: Sequence[AlignmentQuestion]) -> int:
    return max((q.card_seq for q in questions), default=0) + 1


def _validate_point(point: Mapping[str, Any]) -> None:
    try:
        ExpectedPoint.model_validate(_suite_point(point))
    except ValueError as exc:
        raise AgentEvalAlignmentError("agent_eval_alignment_point_invalid") from exc


def _suite_point(point: Mapping[str, Any]) -> dict[str, Any]:
    """The point as it is saved in a suite revision once confirmed."""

    return {
        "id": point["id"],
        "kind": point["kind"],
        "text": point["text"],
        "source": "coach_alignment",
        "state": "confirmed",
        "origin": dict(point["origin"]),
    }


def _origin(loop: AlignmentLoop) -> dict[str, Any]:
    """Where a loop point came from. A member's thumbs-down is recorded by kind only: their comment and
    id never reach the point or the suite revision (it stays readable through the share while it lasts)."""

    if loop.words_source == "member_feedback":
        return {"kind": "member_feedback", "at": loop.created_at, "loop_id": loop.id}
    return {"kind": "owner_words", "words": loop.owner_words, "by": loop.words_by, "at": loop.created_at, "loop_id": loop.id}


async def _new_point_id_conn(db: aiosqlite.Connection, loop: AlignmentLoop, points: Sequence[Mapping[str, Any]]) -> str:
    """A point id unused in the loop, in every revision of the loop's suite and in its calibration rows."""

    taken = {str(point["id"]) for point in points}
    for _ in range(8):
        candidate = f"p_{uuid4().hex[:12]}"
        if candidate not in taken and not await _point_id_in_suite_conn(db, suite_id=loop.suite_id, point_id=candidate):
            return candidate
    raise AgentEvalAlignmentError("agent_eval_alignment_point_conflict", status=409)


async def _point_id_in_suite_conn(db: aiosqlite.Connection, *, suite_id: str, point_id: str) -> bool:
    async with db.execute(
        """
        SELECT 1 FROM agent_eval_case_revision case_revision
        JOIN agent_eval_suite_revision revision ON revision.id=case_revision.suite_revision_id,
             json_each(case_revision.expected_contract_json,'$.points') point
        WHERE revision.suite_id=? AND json_extract(point.value,'$.id')=?
        UNION ALL SELECT 1 FROM agent_eval_point_calibration WHERE suite_id=? AND point_id=?
        UNION ALL SELECT 1 FROM agent_eval_point_example WHERE suite_id=? AND point_id=?
        LIMIT 1
        """,
        (suite_id, point_id, suite_id, point_id, suite_id, point_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _drop_unreadable_conn(db: aiosqlite.Connection, loop: AlignmentLoop, questions: Sequence[AlignmentQuestion]) -> set[str]:
    """A Turn the Owner can no longer read (share withdrawn, session deleted, access lost) leaves the
    card (§6), together with any row generated from it."""

    linked = [q for q in questions if q.linked_turn_id is not None]
    readable = await _readable_turns_conn(db, loop, sorted({str(q.linked_turn_id) for q in linked}))
    dropped = {q.id for q in linked if q.linked_turn_id not in readable}
    for question_id in dropped:
        await db.execute("DELETE FROM agent_eval_alignment_question WHERE id=?", (question_id,))
    return dropped


async def _confirm_task_conn(db: aiosqlite.Connection, loop: AlignmentLoop, questions: Sequence[AlignmentQuestion]) -> str:
    """The new case's task: the Turn the loop started from, else the first real answer the Owner confirmed."""

    if loop.origin_turn_id is not None:
        readable = await _readable_turns_conn(db, loop, [loop.origin_turn_id])
        if loop.origin_turn_id in readable:
            return str(readable[loop.origin_turn_id]["question"])
    real = [q for q in questions if not q.answer_ref.startswith("coach:") and q.owner_label == "right"]
    return (real or list(questions))[0].task_text


async def _confirm_conn(
    db: aiosqlite.Connection, loop: AlignmentLoop, point: Mapping[str, Any], questions: Sequence[AlignmentQuestion]
) -> dict[str, Any]:
    """The server's confirmation, inside the labels transaction (technical design §6).

    1. The point is saved `confirmed` in a new case of a new suite revision (the existing append path).
    2. Each right/wrong label becomes a calibration example (`owner_alignment`), graded as shown on the card.
    3. When every example was graded under the loop's pinned judge and agrees, the point is calibrated.
    """

    from bowhead_core.agent_eval_scorers import validate_core_scoring_contract

    _suite_id, revision_id = await _owner_suite_revision_conn(db, team_id=loop.team_id, agent_id=loop.agent_id, suite_id=loop.suite_id)
    if await _point_id_in_suite_conn(db, suite_id=loop.suite_id, point_id=str(point["id"])):
        # Never overwrite another point's case, examples or calibration row.
        raise AgentEvalAlignmentError("agent_eval_alignment_point_conflict", status=409, details={"point_id": point["id"]})
    suite_point = _suite_point(point)
    task = (await _confirm_task_conn(db, loop, questions))[:8000]
    case_key = f"align.{point['id']}"
    expected = {"allowed_terminal_statuses": ["completed"], "points": [suite_point]}
    validate_core_scoring_contract(expected, _CASE_SCORER_CONFIG, allow_schema_ref=False)
    revision = await append_eval_suite_cases_conn(
        db,
        team_id=loop.team_id,
        agent_id=loop.agent_id,
        expected_suite_revision_id=revision_id,
        cases=(
            EvalCaseDraft(
                case_key=case_key,
                input={"task": task},
                fixture_ref={},
                expected_contract=expected,
                scorer_config=_CASE_SCORER_CONFIG,
                effect_policy={"mode": "no_effects"},
            ),
        ),
        actor_user_id=loop.owner_id,
    )
    case_revision_id = next(case.case_revision_id for case in revision.cases if case.case_key == case_key)
    text_hash = point_text_hash(str(point["text"]))
    timestamp = _now()
    seeded: list[tuple[bool, bool, str, str]] = []
    for question in questions:
        if question.owner_label not in {"right", "wrong"} or question.judge_met is None:
            continue
        expected_met = question.judge_met if question.owner_label == "right" else not question.judge_met
        example_id = await _insert_example_conn(
            db,
            team_id=loop.team_id,
            agent_id=loop.agent_id,
            suite_id=revision.suite_id,
            point_id=str(point["id"]),
            case_revision_id=case_revision_id,
            kind="good" if expected_met else "bad",
            answer_ref=question.answer_ref,
            answer_text=question.answer_text,
            expected_met=expected_met,
            actor_user_id=loop.owner_id,
            label_source="owner_alignment",
            task_text=question.task_text,
            source_turn_id=question.source_turn_id,
        )
        await db.execute(
            """
            UPDATE agent_eval_point_example
            SET graded_text_hash=?,graded_prompt_version=?,graded_model_fingerprint=?,graded_met=?,graded_at=?
            WHERE id=?
            """,
            (text_hash, question.graded_prompt_version, question.graded_model_fingerprint, int(question.judge_met), timestamp, example_id),
        )
        seeded.append((expected_met, question.judge_met, question.graded_prompt_version, question.graded_model_fingerprint))
    calibrated = seed_agrees(seeded, prompt_version=loop.judge_prompt_version, model_fingerprint=loop.judge_model_fingerprint)
    if calibrated:
        await db.execute(
            """
            INSERT INTO agent_eval_point_calibration(
              suite_id,point_id,team_id,agent_id,text_hash,prompt_version,model_fingerprint,state,failure_reason,rewording,updated_at
            ) VALUES(?,?,?,?,?,?,?,'calibrated',NULL,NULL,?)
            """,
            (
                revision.suite_id,
                point["id"],
                loop.team_id,
                loop.agent_id,
                text_hash,
                loop.judge_prompt_version,
                loop.judge_model_fingerprint,
                timestamp,
            ),
        )
    return {
        "suite_id": revision.suite_id,
        "suite_revision_id": revision.suite_revision_id,
        "case_key": case_key,
        "case_revision_id": case_revision_id,
        "calibration": "seeded" if calibrated else "pending",
        "point_state": {
            "status": "confirmed",
            "case_key": case_key,
            "case_revision_id": case_revision_id,
            "suite_revision_id": revision.suite_revision_id,
            "confirmed_at": timestamp,
        },
    }


def seed_agrees(examples: Sequence[tuple[bool, bool, str, str]], *, prompt_version: str, model_fingerprint: str) -> bool:
    """Calibrated from labels: a met and an unmet example, all graded under the pinned judge, all agreeing.

    Each item is `(expected_met, graded_met, graded_prompt_version, graded_model_fingerprint)`. This is the
    same condition `is_calibrated` checks on the stored examples, so the two agree on the same data.
    """

    return (
        {expected for expected, *_rest in examples} == {True, False}
        and all(expected == graded for expected, graded, *_rest in examples)
        and all((version, fingerprint) == (prompt_version, model_fingerprint) for *_head, version, fingerprint in examples)
    )


# ── the service Coach's Tools call ──────────────────────────────────────


@dataclass
class AlignmentService:
    """Ask: pick and grade answers for a point and store a question card. Labels never go through here."""

    store: AgentEvalAlignmentStore
    judge: AlignmentJudge | None
    writer: CalibrationWriter | None
    current_judge_fingerprint: Callable[[], Awaitable[str | None]]
    live_answers: Callable[[], Awaitable[list[dict[str, Any]]]] = field(default=lambda: _no_live_answers())  # type: ignore[assignment]

    async def ask(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        loop_id: str,
        point_id: str | None,
        kind: str,
        text: str,
        answer_refs: Sequence[str] = (),
        replace_unclear: bool = False,
    ) -> dict[str, Any]:
        loop, _questions = await self.store.read(team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
        current = await self.current_judge_fingerprint()
        if current is None or self.judge is None:
            raise AgentEvalAlignmentError("agent_eval_judge_unavailable", status=503)
        if current != loop.judge_model_fingerprint or loop.judge_prompt_version != POINTS_PROMPT_VERSION:
            # A7: the loop's verdicts stay comparable only under one judge; the Owner re-pins.
            raise AgentEvalAlignmentError("agent_eval_alignment_judge_changed", status=409, details={"action": "repin"})
        point = await self.store.prepare_point(
            team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id, point_id=point_id, kind=kind, text=text
        )
        loop, questions = await self.store.read(team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, loop_id=loop_id)
        epoch = epoch_questions(questions, point)
        if replace_unclear:
            if not epoch:
                raise AgentEvalAlignmentError("agent_eval_alignment_card_missing", status=409, details={"point_id": point["id"]})
            card = max(q.card_seq for q in epoch)
            on_card = [q for q in questions if q.card_seq == card]
            wanted = sum(q.owner_label == "unclear" for q in on_card) - sum(q.replacement for q in on_card)
            needed = max(0, min(wanted, MAX_CARD_ROWS - len(on_card)))
            if needed == 0:
                raise AgentEvalAlignmentError("agent_eval_alignment_no_replacement_due", status=409, details={"point_id": point["id"]})
        else:
            if epoch:
                latest = max(q.card_seq for q in epoch)
                waiting = [q.id for q in epoch if q.card_seq == latest and q.owner_label is None]
                if waiting:
                    # The Owner hasn't answered the open card yet: wait for their labels and Continue.
                    raise AgentEvalAlignmentError(
                        "agent_eval_alignment_card_open",
                        status=409,
                        details={"point_id": point["id"], "card_seq": latest, "action": "wait_for_owner_labels"},
                    )
            needed = REAL_ROWS_PER_CARD
            if point_outcome(questions, point) == "need_counterexample":
                # The Owner's labels already agree on enough real answers; what is missing is the attention check.
                # Asking real answers again re-showed the same ones (116 dogfood, 1.5.84), so this card carries
                # only the counterexample, written from an answer the Owner agreed meets the point.
                return await self._counterexample_card(loop, point, epoch, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
        asked = {q.answer_ref for q in epoch}
        asked_groups = {q.task_text for q in epoch}
        candidates = await self._candidates(loop, answer_refs=answer_refs, exclude=asked)
        candidates = [candidate for candidate in candidates if candidate.task_text not in asked_groups or answer_refs]
        if not candidates and not epoch:
            # R3: no real answers yet; Coach drafts from the definition as today.
            return {"cold_start": True, "point": point, "rows": [], "card_seq": None}
        pool = pool_order(candidates)[: min(MAX_PICK_POOL, needed + PICK_POOL_EXTRA)] if not answer_refs else candidates
        await self._grade(loop, point, pool)
        if all(candidate.reason == "agent_eval_judge_unavailable" for candidate in pool) and pool:
            raise AgentEvalAlignmentError("agent_eval_judge_unavailable", status=503)
        chosen = pick_order(pool)[:needed]
        if replace_unclear and not chosen:
            raise AgentEvalAlignmentError("agent_eval_alignment_no_more_answers", status=409, details={"point_id": point["id"]})
        rows = [self._row(candidate, point) for candidate in chosen]
        counterexample: str | None = None
        if not replace_unclear:
            generated, counterexample = await self._generated_rows(loop, point, chosen)
            rows.extend(generated)
        card_seq, stored = await self.store.add_rows(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            loop_id=loop_id,
            point_id=point["id"],
            text_hash=point_text_hash(str(point["text"])),
            rows=rows,
            replacement=replace_unclear,
        )
        first_card = card_seq == 1
        return {"cold_start": False, "point": point, "card_seq": card_seq, "rows": stored, "first_card": first_card, "counterexample": counterexample}

    async def _counterexample_card(
        self, loop: AlignmentLoop, point: Mapping[str, Any], epoch: Sequence[AlignmentQuestion], *, team_id: str, agent_id: str, actor_user_id: str
    ) -> dict[str, Any]:
        """A card with only the counterexample, for a point whose outcome is `need_counterexample`.

        Its base is a real answer the judge graded met and the Owner labelled right (newest first); without
        one, the writer's own good answer is graded and added too, so the card still holds a met verdict.
        """

        agreed = [q for q in sorted(epoch, key=lambda q: (q.card_seq, q.row_index), reverse=True) if q.owner_label == "right" and q.judge_met is True]
        base: AnswerCandidate | None = None
        async with open_shared_ctx(self.store.db_path) as db:
            db.row_factory = aiosqlite.Row
            for question in agreed:
                if question.answer_ref.startswith("coach:"):
                    if question.pick_reason != "generated" or not question.answer_text:
                        continue
                    base = AnswerCandidate(
                        answer_ref=question.answer_ref, task_text=question.task_text, answer=question.answer_text, group="generated", answered_at=""
                    )
                else:
                    try:
                        found = await self._explicit_candidates(db, loop, [question.answer_ref])
                    except AgentEvalAlignmentError:
                        continue  # no longer readable: try the next agreed answer
                    base = found[0]
                    base.source_turn_id = base.turn_id
                base.met, base.graded = True, True
                break
        generated, status = await self._generated_rows(loop, point, [base] if base is not None else [], fill=base is None)
        if not any(row["pick_reason"] == "counterexample" for row in generated):
            raise AgentEvalAlignmentError(
                "agent_eval_alignment_counterexample_unavailable",
                status=503,
                details={"point_id": point["id"], "reason": status, "action": "ask again for the same point id in a later turn"},
            )
        card_seq, stored = await self.store.add_rows(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            loop_id=loop.id,
            point_id=point["id"],
            text_hash=point_text_hash(str(point["text"])),
            rows=generated,
            replacement=False,
        )
        return {"cold_start": False, "point": point, "card_seq": card_seq, "rows": stored, "first_card": card_seq == 1, "counterexample": status}

    async def _candidates(self, loop: AlignmentLoop, *, answer_refs: Sequence[str], exclude: set[str]) -> list[AnswerCandidate]:
        """Readable real answers: the Owner's and shared Turns (normal-origin sessions only, so no evaluation
        scratch Turn), then Live-release attempts on the Owner's own cases.

        A Live attempt counts only when its case belongs to an `owner_quality` suite: the platform contract
        case's task is the platform's, not a question anyone asked, and one per task it displaced real answers.
        """

        async with open_shared_ctx(self.store.db_path) as db:
            db.row_factory = aiosqlite.Row
            if answer_refs:
                return await self._explicit_candidates(db, loop, answer_refs)
            turns = await readable_turn_answers_conn(db, team_id=loop.team_id, agent_id=loop.agent_id, user_id=loop.owner_id)
            origin = loop.origin_turn_id
            if origin is not None and all(row["turn_id"] != origin for row in turns):
                # Older than the newest page: still the answer this loop is about, while it stays readable.
                turns = [*(await _readable_turns_conn(db, loop, [origin])).values(), *turns]
        found = [
            AnswerCandidate(
                answer_ref=f"turn:{row['turn_id']}",
                task_text=row["question"],
                answer=row["answer"],
                group=f"session:{row['session_id']}",
                answered_at=row["answered_at"],
                marked_bad=(row["feedback"] or {}).get("score") == -1,
                turn_id=row["turn_id"],
                origin=row["turn_id"] == origin,
            )
            for row in turns
            if row["question"] and row["answer"]
        ]
        for item in await self.live_answers():
            # A held-out case reaches Coach only as a count (A6): never as a card row, a counterexample
            # base or the task of a new case.
            if item.get("suite_kind") != "owner_quality" or item.get("held_out", True):
                continue
            if item.get("answer") and item.get("task"):
                text, graded = reader_answer(str(item["answer"]))
                found.append(
                    AnswerCandidate(
                        answer_ref=str(item["answer_ref"]),
                        task_text=str(item["task"])[:TASK_CHARS],
                        answer=text[:ANSWER_CHARS],
                        group=f"task:{item['task']}",
                        answered_at=str(item["answered_at"]),
                        graded_output=graded,
                    )
                )
        return [candidate for candidate in found if candidate.answer_ref not in exclude]

    async def _explicit_candidates(self, db: aiosqlite.Connection, loop: AlignmentLoop, answer_refs: Sequence[str]) -> list[AnswerCandidate]:
        """Answers Coach named; each must be readable to the Owner, one per session."""

        refs = list(dict.fromkeys(answer_refs))
        if len(refs) != len(answer_refs) or len(refs) > MAX_EXPLICIT_ANSWERS:
            raise AgentEvalAlignmentError("agent_eval_alignment_answers_invalid", details={"limit": MAX_EXPLICIT_ANSWERS})
        turn_ids = [ref[5:] for ref in refs if ref.startswith("turn:")]
        readable = await _readable_turns_conn(db, loop, turn_ids)
        found: list[AnswerCandidate] = []
        for ref in refs:
            if ref.startswith("turn:"):
                row = readable.get(ref[5:])
                if row is None:
                    # Someone else's unshared Turn reads the same as a missing one.
                    raise AgentEvalAlignmentError("agent_eval_conversation_unavailable", status=404, details={"answer_ref": ref})
                found.append(
                    AnswerCandidate(
                        answer_ref=ref,
                        task_text=row["question"],
                        answer=row["answer"],
                        group=f"session:{row['session_id']}",
                        answered_at=row["answered_at"],
                        marked_bad=(row["feedback"] or {}).get("score") == -1,
                        turn_id=row["turn_id"],
                    )
                )
            elif ref.startswith("attempt:"):
                run_id, case_revision_id, _attempt = _parse_attempt_ref(ref)
                async with db.execute(
                    """
                    SELECT json_extract(case_revision.input_json,'$.task') AS task,
                           COALESCE(json_extract(case_revision.expected_contract_json,'$.held_out'),0) AS held_out,
                           suite.suite_kind AS suite_kind
                    FROM agent_eval_run run
                    JOIN agent_eval_case_result result ON result.run_id=run.id AND result.case_revision_id=?
                    JOIN agent_eval_case_revision case_revision ON case_revision.id=result.case_revision_id
                    JOIN agent_eval_suite_revision suite_revision ON suite_revision.id=case_revision.suite_revision_id
                    JOIN agent_eval_suite suite ON suite.id=suite_revision.suite_id
                    WHERE run.id=? AND run.team_id=? AND run.agent_source_kind='team' AND run.agent_id=?
                    LIMIT 1
                    """,
                    (case_revision_id, run_id, loop.team_id, loop.agent_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None and row[1] == 1:
                    raise AgentEvalAlignmentError("agent_eval_case_held_out", details={"answer_ref": ref})
                if row is not None and row[2] != "owner_quality":
                    # The platform's own contract case: an answer to the platform's task, not to anyone's question.
                    raise AgentEvalAlignmentError(
                        "agent_eval_alignment_answer_not_owner_case",
                        details={"answer_ref": ref, "suite_kind": row[2], "action": "omit answer_refs to let the server pick real answers"},
                    )
                answer = await _attempt_answer_conn(db, ref) if row is not None else None
                if row is None or not answer or not isinstance(row[0], str):
                    raise AgentEvalAlignmentError("agent_eval_point_answer_not_found", status=404, details={"answer_ref": ref})
                text, graded = reader_answer(answer)
                found.append(
                    AnswerCandidate(
                        answer_ref=ref, task_text=row[0][:TASK_CHARS], answer=text, group=f"task:{row[0]}", answered_at="", graded_output=graded
                    )
                )
            else:
                # Coach never supplies an answer text of its own; counterexamples are generated server-side.
                raise AgentEvalAlignmentError("agent_eval_alignment_answers_invalid", details={"answer_ref": ref})
        groups = [candidate.group for candidate in found]
        if len(groups) != len(set(groups)):
            raise AgentEvalAlignmentError("agent_eval_alignment_one_answer_per_session")
        return found

    async def _grade_one(self, loop: AlignmentLoop, point: Mapping[str, Any], candidate: AnswerCandidate) -> None:
        """One answer per production `grade_points` call, so the card shows what a check would grade (§5)."""

        assert self.judge is not None
        grading = await self.judge.grade_points(
            task=candidate.task_text,
            output=candidate.answer if candidate.graded_output is None else candidate.graded_output,
            points=[{"id": point["id"], "kind": point["kind"], "text": point["text"]}],
            role_ref="judge",
            expected_model_fingerprint=loop.judge_model_fingerprint,
        )
        verdict = grading.verdicts.get(str(point["id"])) if grading.status == "graded" else None
        candidate.graded = True
        if verdict is None:
            candidate.met, candidate.quote, candidate.reason = None, None, grading.failure_code or "agent_eval_judge_unavailable"
        else:
            candidate.met, candidate.quote, candidate.reason = verdict.met, verdict.quote, verdict.reason

    async def _grade(self, loop: AlignmentLoop, point: Mapping[str, Any], candidates: Sequence[AnswerCandidate]) -> None:
        async with anyio.create_task_group() as group:
            for candidate in candidates:
                group.start_soon(self._grade_one, loop, point, candidate)

    def _row(self, candidate: AnswerCandidate, point: Mapping[str, Any], *, pick_reason: str | None = None) -> dict[str, Any]:
        return {
            "answer_ref": candidate.answer_ref,
            "answer_text": candidate.answer if candidate.answer_ref.startswith("coach:") else None,
            "task_text": candidate.task_text,
            "answer_excerpt": excerpt_around(candidate.answer, candidate.quote),
            "judge_met": candidate.met,
            "judge_quote": candidate.quote,
            "judge_reason": candidate.reason,
            "pick_reason": pick_reason or classify(candidate),
            "id": candidate.answer_ref[6:] if candidate.answer_ref.startswith("coach:") else None,
            "source_turn_id": candidate.source_turn_id,
        }

    async def _generated_rows(
        self, loop: AlignmentLoop, point: Mapping[str, Any], chosen: Sequence[AnswerCandidate], *, fill: bool = True
    ) -> tuple[list[dict[str, Any]], str]:
        """The card's minimal-edit counterexample (attention check, R6), plus a generated good answer
        when fewer than two real answers exist (R3), and what happened to the counterexample: `added`, or
        why it is missing. A missing counterexample leaves the outcome at `need_counterexample`; it is logged
        and returned to Coach, never dropped silently. `fill=False` writes only the counterexample."""

        if self.writer is None:
            _log.warning("agent_eval_alignment.counterexample_missing loop=%s point=%s reason=writer_unavailable", loop.id, point["id"])
            return [], "writer_unavailable"
        rows: list[dict[str, Any]] = []
        base = next((candidate for candidate in chosen if candidate.met is True), None)
        origin = base if base is not None else (chosen[0] if chosen else None)
        task = origin.task_text if origin is not None else (loop.owner_words or str(point["text"]))
        # A generated row written from a member's Turn is linked to it, so it leaves with the Turn (§6).
        source_turn_id = (origin.turn_id or origin.source_turn_id) if origin is not None else None
        generated_text: str | None = None
        if fill and (base is None or len(chosen) < REAL_ROWS_PER_CARD):
            generated_text = await self.writer.good_answer(task=task, point=point)
        if generated_text and len(chosen) < REAL_ROWS_PER_CARD:
            # R3: one or two real answers are filled to a full card with a generated answer, marked as such.
            generated = AnswerCandidate(
                answer_ref=f"coach:aeaq_{uuid4().hex}",
                task_text=task,
                answer=generated_text[:ANSWER_CHARS],
                group="generated",
                answered_at="",
                source_turn_id=source_turn_id,
            )
            await self._grade(loop, point, [generated])
            rows.append(self._row(generated, point, pick_reason="generated"))
        good_text = base.answer if base is not None else generated_text
        if not good_text:
            return rows, self._missing(loop, point, "good_answer_unavailable", answer_chars=0)
        # The writer retries a rejected edit itself; a second call here only doubled the wait on a timeout.
        candidate = await self.writer.bad_answer(task=task, point=point, good_answer=good_text)
        if not candidate:
            return rows, self._missing(loop, point, "counterexample_not_written", answer_chars=len(good_text))
        if not is_minimal_edit(good_text, candidate):
            return rows, self._missing(loop, point, "counterexample_not_minimal", answer_chars=len(good_text))
        bad_text = candidate[:ANSWER_CHARS]
        counter = AnswerCandidate(
            answer_ref=f"coach:aeaq_{uuid4().hex}",
            task_text=task,
            answer=bad_text,
            group="counterexample",
            answered_at="",
            source_turn_id=source_turn_id,
        )
        await self._grade(loop, point, [counter])
        rows.append(self._row(counter, point, pick_reason="counterexample"))
        return rows, "added"

    @staticmethod
    def _missing(loop: AlignmentLoop, point: Mapping[str, Any], reason: str, *, answer_chars: int) -> str:
        _log.warning(
            "agent_eval_alignment.counterexample_missing loop=%s point=%s reason=%s base_answer_chars=%d", loop.id, point["id"], reason, answer_chars
        )
        return reason


# ── projections ──────────────────────────────────────────────────────────


def question_view(question: AlignmentQuestion, *, for_coach: bool, stale: bool = False, shared_by: str | None = None) -> dict[str, Any]:
    """One card row. Coach gets a question preview; the Owner's card gets the full question."""

    view: dict[str, Any] = {
        "question_id": question.id,
        "point_id": question.point_id,
        "card_seq": question.card_seq,
        "row_index": question.row_index,
        "answer_ref": question.answer_ref,
        "generated": question.answer_ref.startswith("coach:"),
        "question": " ".join(question.task_text.split())[:200] if for_coach else question.task_text,
        "answer_excerpt": question.answer_excerpt,
        "verdict": {"met": question.judge_met, "quote": question.judge_quote, "reason": question.judge_reason},
        "pick_reason": question.pick_reason,
        "replacement": question.replacement,
        "label": question.owner_label,
        "labelled_at": question.labelled_at,
        # Graded on other text than the point's current text, or from an earlier epoch: shown, not labelable.
        "stale": stale,
    }
    if not for_coach:
        view["label_via"] = question.label_via
        # Who shared this Turn (and gave its thumbs-down), read live; the Owner's own Turns name no one,
        # and Coach never gets it.
        view["shared_by"] = shared_by
    return view


def next_step(outcome: str, *, rows_waiting: bool = True) -> str:
    """What Coach does next. `rows_waiting` says the point's newest card still has unlabelled rows."""

    if outcome == "continue" and not rows_waiting:
        # Every row is labelled but too few count yet (a "Judged right" on an abstained verdict says
        # nothing): waiting would never end, so Coach asks another card for the same point.
        return "ask_more_answers"
    return {
        "confirmed": "propose_cases",
        "confirm": "confirm",
        "rewrite": "propose_rewrite",
        "split_or_informational": "suggest_split_or_informational",
        "unclear": "ask_owner_to_restate",
        "need_counterexample": "ask_counterexample",
        "recheck_row": "ask_about_row",
        "continue": "wait_for_labels",
    }[outcome]


def _rows_waiting(current: Sequence[AlignmentQuestion]) -> bool:
    if not current:
        return False
    latest = max(q.card_seq for q in current)
    return any(q.owner_label is None for q in current if q.card_seq == latest)


def loop_view(
    loop: AlignmentLoop,
    questions: Sequence[AlignmentQuestion],
    *,
    for_coach: bool,
    feedback_comment: str | None = None,
    sharers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The loop for the Owner's cards or Coach. `feedback_comment` is the member's comment read live
    (`AgentEvalAlignmentStore.feedback_comment`); it is never stored on the loop."""
    points = []
    current_ids = {q.id for point in loop.points for q in epoch_questions(questions, point)}
    for point in loop.points:
        outcome = point_outcome(questions, point)
        current = epoch_questions(questions, point)
        points.append(
            {
                "point_id": point["id"],
                "kind": point["kind"],
                "text": point["text"],
                "previous_text": point.get("previous_text"),
                "origin": point["origin"],
                "status": point.get("status", "aligning"),
                "rewrites": int(point.get("rewrites") or 0),
                "outcome": outcome,
                "next_step": next_step(outcome, rows_waiting=_rows_waiting(current)),
                "recheck_question_ids": attention_check_failed(current),
                "labels": {label: sum(q.owner_label == label for q in current) for label in ("right", "wrong", "unclear")},
                "case_key": point.get("case_key"),
                "case_revision_id": point.get("case_revision_id"),
            }
        )
    return {
        "kind": "agent_eval_alignment_loop",
        "schema_version": 1,
        "loop_id": loop.id,
        "agent_id": loop.agent_id,
        "suite_id": loop.suite_id,
        "state": loop.state,
        "words_source": loop.words_source,
        "owner_words": loop.owner_words,
        "words_by": loop.words_by,
        "feedback_comment": feedback_comment,
        "origin_turn_id": loop.origin_turn_id,
        "judge": {"prompt_version": loop.judge_prompt_version, "model_fingerprint": loop.judge_model_fingerprint, "pinned_at": loop.pinned_at},
        "points": points,
        "questions": [
            question_view(q, for_coach=for_coach, stale=q.id not in current_ids, shared_by=None if for_coach else (sharers or {}).get(q.id))
            for q in questions
        ],
        "report": loop.report,
        "created_at": loop.created_at,
        "updated_at": loop.updated_at,
        **({"labels_mean": LABEL_MEANING} if for_coach else {}),
    }


def card_pointer(loop: AlignmentLoop, point_id: str, card_seq: int) -> dict[str, Any]:
    """What the chat renders a question card from (tool-result pointer, like slice 7 receipt rows)."""

    return {
        "kind": "agent_eval_alignment_card",
        "schema_version": 1,
        "agent_id": loop.agent_id,
        "loop_id": loop.id,
        "point_id": point_id,
        "card_seq": card_seq,
    }


def held_out_case_revision_ids_sql(suite_revision_column: str) -> str:
    """SQL subquery: the held-out case revisions of a suite revision."""

    return (
        "SELECT held.id FROM agent_eval_case_revision held"
        f" WHERE held.suite_revision_id={suite_revision_column} AND COALESCE(json_extract(held.expected_contract_json,'$.held_out'),0)=1"
    )


# ── report (technical design §7–§8) ─────────────────────────────────────

_SETTLED = frozenset({"passed", "failed"})
MIN_CASES_FOR_RATE = 3
TO_READ = 2


def _judge_prompt_version(judge: Mapping[str, Any]) -> str | None:
    if isinstance(judge.get("points_grading"), Mapping):
        return judge["points_grading"].get("prompt_version")
    return judge.get("prompt_version")


def _counts(met: int, cases: int) -> dict[str, Any]:
    # Below three cases a share would overstate what one answer shows (R12): counts only.
    value: dict[str, Any] = {"met": met, "cases": cases}
    if cases >= MIN_CASES_FOR_RATE:
        value["rate"] = round(met / cases, 4)
    return value


def _passing(passing: int, cases: int) -> dict[str, Any]:
    value: dict[str, Any] = {"passing": passing, "cases": cases}
    if cases >= MIN_CASES_FOR_RATE:
        value["rate"] = round(passing / cases, 4)
    return value


async def latest_loop_execution_conn(db: aiosqlite.Connection, loop: AlignmentLoop) -> str | None:
    """The loop's latest settled check: a comparison Coach's check started from this conversation after the pin."""

    async with db.execute(
        """
        SELECT execution.id FROM agent_eval_comparison_execution execution
        JOIN coach_ci_delegation delegation ON delegation.id=execution.coach_delegation_id
        WHERE execution.team_id=? AND execution.agent_id=? AND execution.status='completed'
          AND delegation.owner_user_id=? AND delegation.source_session_id=? AND execution.requested_at>=?
        ORDER BY execution.finished_at DESC,execution.id DESC LIMIT 1
        """,
        (loop.team_id, loop.agent_id, loop.owner_id, loop.session_id, loop.pinned_at),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else str(row[0])


async def build_report_conn(db: aiosqlite.Connection, loop: AlignmentLoop, *, execution_id: str) -> dict[str, Any]:
    """Before (the check's Live side) → after (its candidate side), under the loop's pinned judge.

    Both sides of one comparison are graded by the same judge at the same time, so the target point's
    before and after are comparable; a check graded under another judge doesn't count (A7, R13).
    """

    from bowhead_core.agent_coach_evidence import _excerpt
    from bowhead_core.agent_eval_point_calibration import attempt_answer_ref

    targets = [point for point in loop.points if point.get("status") == "confirmed"]
    target_ids = {str(point["id"]) for point in targets}
    async with db.execute(
        """
        SELECT link.subject_side,run.judge_role_ref,run.judge_model_fingerprint,result.run_id,result.case_revision_id,
               result.attempt,result.status,result.judge_json,case_revision.case_key,case_revision.expected_contract_json
        FROM agent_eval_comparison_run link
        JOIN agent_eval_suite_revision revision ON revision.id=link.suite_revision_id AND revision.suite_id=?
        JOIN agent_eval_run run ON run.id=link.run_id
        JOIN agent_eval_case_result result ON result.run_id=link.run_id
        JOIN agent_eval_case_revision case_revision ON case_revision.id=result.case_revision_id
        WHERE link.execution_id=?
        ORDER BY case_revision.case_key,result.attempt
        """,
        (loop.suite_id, execution_id),
    ) as cursor:
        rows = await cursor.fetchall()
    cases: dict[str, dict[str, Any]] = {}
    judge_state = "pinned"
    for row in rows:
        expected = json.loads(str(row["expected_contract_json"]))
        judge = json.loads(row["judge_json"]) if row["judge_json"] else {}
        case_points = {str(item.get("id")) for item in (expected.get("points") or []) if isinstance(item, Mapping)}
        if case_points & target_ids:
            if row["judge_role_ref"] is None or row["judge_model_fingerprint"] is None:
                judge_state = "judge_unavailable"
            elif row["judge_model_fingerprint"] != loop.judge_model_fingerprint or (
                isinstance(judge, Mapping) and judge.get("points") is not None and _judge_prompt_version(judge) != loop.judge_prompt_version
            ):
                judge_state = "judge_changed" if judge_state == "pinned" else judge_state
        case = cases.setdefault(
            str(row["case_key"]),
            {"held_out": is_held_out(expected), "points": case_points, "sides": {"baseline": [], "candidate": []}},
        )
        verdicts = {
            str(item["id"]): item.get("met")
            for item in (judge.get("points") or [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"] in target_ids
        }
        case["sides"][str(row["subject_side"])].append(
            {
                "status": str(row["status"]),
                "verdicts": verdicts,
                "ref": attempt_answer_ref(run_id=str(row["run_id"]), case_revision_id=str(row["case_revision_id"]), attempt=int(row["attempt"])),
            }
        )
    base: dict[str, Any] = {
        "kind": "agent_eval_alignment_report",
        "schema_version": 1,
        "loop_id": loop.id,
        "agent_id": loop.agent_id,
        "execution_id": execution_id,
        "judge": {"prompt_version": loop.judge_prompt_version, "model_fingerprint": loop.judge_model_fingerprint},
    }
    if judge_state != "pinned":
        # The point verdicts of this check are treated as not graded (met=None, reason judge_changed).
        return {**base, "status": judge_state, "next_action": "repin" if judge_state == "judge_changed" else "configure_evaluation_model"}

    def passed(attempts: Sequence[Mapping[str, Any]]) -> bool | None:
        settled = [attempt["status"] for attempt in attempts if attempt["status"] in _SETTLED]
        return None if not settled else all(status == "passed" for status in settled)

    def point_met(attempts: Sequence[Mapping[str, Any]], point_id: str) -> bool | None:
        graded = [attempt["verdicts"].get(point_id) for attempt in attempts if attempt["verdicts"].get(point_id) is not None]
        return None if not graded else all(value is True for value in graded)

    visible = {key: case for key, case in cases.items() if not case["held_out"]}
    held = {key: case for key, case in cases.items() if case["held_out"]}
    target_rows = []
    for point in targets:
        point_id = str(point["id"])
        with_point = [case for case in visible.values() if point_id in case["points"]]
        before = sum(point_met(case["sides"]["baseline"], point_id) is True for case in with_point)
        after = sum(point_met(case["sides"]["candidate"], point_id) is True for case in with_point)
        independent = any(point_id in case["points"] for case in held.values())
        target_rows.append(
            {
                "point_id": point_id,
                "text": point["text"],
                "before": _counts(before, len(with_point)),
                "after": _counts(after, len(with_point)),
                # R12: with its held-out case turned off, a point's change can't be checked independently.
                "independent_check": independent,
            }
        )
    regressions = sorted(
        key for key, case in visible.items() if passed(case["sides"]["baseline"]) is True and passed(case["sides"]["candidate"]) is False
    )
    held_before = sum(passed(case["sides"]["baseline"]) is True for case in held.values())
    held_after = sum(passed(case["sides"]["candidate"]) is True for case in held.values())
    held_regressions = sum(passed(case["sides"]["baseline"]) is True and passed(case["sides"]["candidate"]) is False for case in held.values())
    readable = sorted(visible.items(), key=lambda item: (not (item[1]["points"] & target_ids), item[0]))
    to_read: list[dict[str, Any]] = []
    for key, case in readable:
        if len(to_read) >= TO_READ:
            break
        for attempt in case["sides"]["candidate"]:
            answer = await _attempt_answer_conn(db, attempt["ref"])
            if answer:
                to_read.append({"case_key": key, "excerpt": _excerpt(answer)})
                break
    return {
        **base,
        "status": "measured",
        "target_points": target_rows,
        # Held-out cases are counts only, for Coach and the Owner alike (A6).
        "held_out": {
            "cases": len(held),
            "before": _passing(held_before, len(held)),
            "after": _passing(held_after, len(held)),
            "regressions": held_regressions,
        },
        "regressions": regressions,
        "to_read": to_read,
    }


__all__ = [
    "AgentEvalAlignmentError",
    "AgentEvalAlignmentStore",
    "AlignmentLoop",
    "AlignmentQuestion",
    "AlignmentService",
    "AnswerCandidate",
    "alignment_outcome",
    "attention_check_failed",
    "build_report_conn",
    "latest_loop_execution_conn",
    "card_pointer",
    "epoch_questions",
    "excerpt_around",
    "held_out_case_revision_ids_sql",
    "is_held_out",
    "loop_view",
    "next_step",
    "pick_order",
    "point_outcome",
    "seed_agrees",
]


async def _no_live_answers() -> list[dict[str, Any]]:
    return []
