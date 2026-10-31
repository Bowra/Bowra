"""Shared bounded policy for clarification before any task tool has executed."""

from __future__ import annotations

import hashlib
import json
import unicodedata

import aiosqlite

CONTEXT_KEY = "question_round"
# Card questions as shown to the user (localized option labels included), kept
# beside the round marker so HTTP readers can render the card; the marker itself
# stores only hashes.
QUESTIONS_KEY = "card_questions"
CONTRACT = "effect-free-questions.v1"
MAX_ROUNDS = 3


def question_hashes(questions: list, fallback: str | None) -> list[str]:
    texts = [q.get("question") for q in questions if isinstance(q, dict)] or [fallback]
    if not 1 <= len(texts) <= 5 or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("clarification_question_invalid")
    hashes = []
    for text in texts:
        normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
        hashes.append("sha256:" + hashlib.sha256(normalized.encode()).hexdigest())
    if len(set(hashes)) != len(hashes):
        raise ValueError("clarification_question_repeated")
    return hashes


def validate_answer_questions(answers: list[dict], context: dict) -> None:
    """Match question identities once while retaining every multi-select answer."""
    expected = context[CONTEXT_KEY]["question_hashes"]
    questions = context.get(QUESTIONS_KEY) or []
    declared = {question_hashes([question], None)[0]: question for question in questions}
    grouped: dict[str, list[dict]] = {}
    for answer in answers:
        identity = question_hashes([answer], None)[0]
        if identity not in expected:
            raise ValueError("clarification_answer_changed")
        grouped.setdefault(identity, []).append(answer)
    if set(grouped) != set(expected):
        raise ValueError("clarification_answer_changed")
    for identity, group in grouped.items():
        if len(group) > 1:
            if declared.get(identity, {}).get("type") not in {"multi_select", "rank_priorities"}:
                raise ValueError("clarification_answer_changed")
            # Repeated selections and a skip mixed with answers are not additional choices.
            values = [(answer.get("source"), answer.get("answer"), answer.get("option_index")) for answer in group]
            if any(answer.get("source") == "skip" for answer in group) or len(set(values)) != len(values):
                raise ValueError("clarification_answer_changed")


def history_contexts(rows: list) -> list[dict]:
    """Reject mixed old/new cards; never guess a missing prior round."""
    contexts = []
    previous = None
    previous_generation = 0
    for index, row in enumerate(rows, 1):
        context = json.loads(row["resume_context_json"] or "null")
        marker = context.get(CONTEXT_KEY) if isinstance(context, dict) else None
        if (
            not isinstance(marker, dict)
            or set(marker) != {"contract", "round", "generation", "prior_request_id", "question_hashes"}
            or marker["contract"] != CONTRACT
            or type(marker["round"]) is not int
            or type(marker["generation"]) is not int
            or marker["round"] != index
            or marker["generation"] <= previous_generation
            or index == 1
            and marker["generation"] != 1
            or marker["prior_request_id"] != previous
            or not isinstance(marker["question_hashes"], list)
            or not 1 <= len(marker["question_hashes"]) <= 5
            or any(not isinstance(value, str) for value in marker["question_hashes"])
            or index > MAX_ROUNDS
        ):
            raise ValueError("clarification_round_history_changed")
        contexts.append(context)
        previous = row["request_id"]
        previous_generation = marker["generation"]
    return contexts


def resume_generation_bounds(contexts: list[dict], index: int) -> tuple[int, int | None]:
    """Return the exclusive lower and inclusive upper bounds for an answer.

    A worker can create the next card while consuming this answer. Abandoned
    pre-worker attempts advance generation without consuming a question round.
    The caller must validate the complete history with history_contexts first.
    """
    return contexts[index][CONTEXT_KEY]["generation"], contexts[index + 1][CONTEXT_KEY]["generation"] if index + 1 < len(contexts) else None


async def read_history(db, *, turn_id: str, user_id: str, workspace_id: str, session_id: str):
    async with db.execute("SELECT * FROM clarification_requests WHERE turn_id=? ORDER BY created_at,request_id", (turn_id,)) as cursor:
        cursor.row_factory = aiosqlite.Row
        rows = await cursor.fetchall()
    if any(row["user_id"] != user_id or row["workspace_id"] != workspace_id or row["session_id"] != session_id for row in rows):
        raise ValueError("clarification_round_scope_changed")
    return rows


async def resume_ready(db, *, turn, session, clarification_id: str | None) -> bool:
    """Fence the original answered card in the generation-advance transaction."""
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name='trg_clarification_round_identity'") as cursor:
        if await cursor.fetchone() is None:
            return True
    async with db.execute(
        "SELECT 1 FROM clarification_requests WHERE turn_id=? AND CASE WHEN json_valid(resume_context_json) "
        "THEN json_type(resume_context_json,'$.question_round') IS NOT NULL ELSE 0 END LIMIT 1",
        (turn["id"],),
    ) as cursor:
        if await cursor.fetchone() is None:
            return True
    rows = await read_history(db, turn_id=turn["id"], user_id=session["user_id"], workspace_id=session["workspace_id"], session_id=session["id"])
    contexts = history_contexts(rows)
    if rows[-1]["request_id"] != clarification_id or any(row["status"] != "resolved" for row in rows):
        return False
    generation = turn["execution_generation"]
    question_generation = contexts[-1][CONTEXT_KEY]["generation"]
    if generation == question_generation:
        return True
    if generation < question_generation or turn["status"] != "clarification_pending":
        return False
    # Only the existing pre-worker rollback/recovery may bridge a gap. A
    # settled worker retains its owner and is not authority for a cold retry.
    async with db.execute(
        "SELECT 1 FROM chat_turn_spawn_dispatch WHERE turn_id=? AND session_id=? "
        "AND lease_kind='resume' AND lease_execution_generation=? AND state='terminal' "
        "AND owner_id IS NULL AND cancel_requested_at IS NULL",
        (turn["id"], session["id"], generation),
    ) as cursor:
        return await cursor.fetchone() is not None


async def next_round(
    db, *, turn_id: str, user_id: str, workspace_id: str, session_id: str, generation: int, owner_id: str, questions=None, fallback=None
):
    """Run under the caller's write transaction when admitting a card."""
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name='trg_clarification_round_identity'") as cursor:
        if await cursor.fetchone() is None:
            raise ValueError("clarification_round_migration_required")
    async with db.execute(
        "SELECT t.execution_generation FROM turns t JOIN sessions s ON s.id=t.session_id "
        "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id "
        "WHERE t.id=? AND s.id=? AND s.user_id=? AND s.workspace_id=? AND t.status='running' "
        "AND d.state='spawned' AND d.lease_execution_generation=t.execution_generation AND t.execution_generation=? AND d.owner_id=? "
        "AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')",
        (turn_id, session_id, user_id, workspace_id, generation, owner_id),
    ) as cursor:
        turn = await cursor.fetchone()
    if turn is None:
        raise ValueError("clarification_round_owner_unavailable")
    rows = await read_history(db, turn_id=turn_id, user_id=user_id, workspace_id=workspace_id, session_id=session_id)
    contexts = history_contexts(rows)
    if len(rows) >= MAX_ROUNDS:
        raise ValueError("clarification_round_budget_exhausted")
    prior_generation = contexts[-1][CONTEXT_KEY]["generation"] if contexts else 0
    if (not rows and turn[0] != 1) or turn[0] <= prior_generation or any(row["status"] != "resolved" for row in rows):
        raise ValueError("clarification_round_answer_required")
    hashes = question_hashes(questions, fallback) if questions is not None else []
    seen = {value for context in contexts for value in context[CONTEXT_KEY]["question_hashes"]}
    if seen.intersection(hashes):
        raise ValueError("clarification_question_repeated")
    return {
        "contract": CONTRACT,
        "round": len(rows) + 1,
        "generation": turn[0],
        "prior_request_id": rows[-1]["request_id"] if rows else None,
        "question_hashes": hashes,
    }


async def available(ctx, *, questions=None, fallback=None) -> bool:
    store = getattr(ctx, "clarification_store", None)
    if store is None:
        return False
    try:
        async with aiosqlite.connect(store.db_path) as db:
            await next_round(
                db,
                turn_id=ctx.turn_id,
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                session_id=ctx.session_id,
                questions=questions,
                fallback=fallback,
                **worker_identity(ctx),
            )
        return True
    except (AttributeError, ValueError, aiosqlite.Error):
        return False


def worker_identity(ctx) -> dict:
    runtime = getattr(ctx, "_reference_runtime", None)
    return {
        "generation": runtime.generation if runtime is not None else getattr(ctx, "execution_generation", 0),
        "owner_id": runtime.owner_id if runtime is not None else getattr(ctx, "execution_owner_id", None),
    }
