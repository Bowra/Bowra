"""The Owner draft trial: switch, request rules and read models.

docs/sprints/agent-page-redesign-2026-11-24/trial-tech-design.md §4–§6. An Agent's Owner chats with the
saved draft frozen as a release candidate. A trial Turn is an ordinary reference Turn whose primary binds
the candidate's revision through the Turn's ``agent_owner_trial_turn`` row; the admission itself lives in
``agent_execution_binding``. This module holds what the gateway and the readers share: the deployment
switch, the shape of a trial request, the session rule, and the reads behind the trial list, attribution
and the "draft under test" notice.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, OwnerTrialTurn, read_owner_trial_turn_in_connection
from bowhead_core.chat_references import AgentResourceRef, InputDocument, ReferenceNode
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.sqlite_readable_schema import readable_table_exists

TRIAL_SESSION_ORIGIN = "agent_trial"
RECENT_TRIALS_LIMIT = 10
_QUESTION_PREVIEW_CHARS = 200
_TRUTHY = {"1", "true", "yes", "on"}

# Admission failures with a public trial meaning. Everything else stays the generic reference refusal.
_BINDING_CODES = {
    "agent_trial_owner_required": "agent_trial_owner_required",
    "agent_trial_candidate_not_prepared": "agent_trial_candidate_not_prepared",
    "agent_trial_candidate_unavailable": "agent_trial_candidate_unavailable",
    "agent_trial_turn_conflict": "agent_trial_session_mismatch",
    "agent_trial_session_mismatch": "agent_trial_session_mismatch",
    "agent_trial_unavailable": "agent_trial_unavailable",
    # The requested revision is no longer the candidate's: the draft was frozen again.
    "agent_candidate_stale": "agent_trial_candidate_changed",
    "agent_not_runnable": "agent_trial_agent_unavailable",
}


class OwnerTrialRefusal(ValueError):
    """A trial request this deployment, actor or session cannot accept; ``code`` is public."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def agent_draft_trial_enabled() -> bool:
    """FEATURE_AGENT_DRAFT_TRIAL_V1 (default off; rollout register: Owner draft trial)."""
    return os.environ.get("FEATURE_AGENT_DRAFT_TRIAL_V1", "0").strip().lower() in _TRUTHY


def trial_reference(document: InputDocument | None) -> AgentResourceRef | None:
    """The Team Agent reference that names a trial candidate, if the document carries one."""
    if document is None:
        return None
    for node in document.nodes:
        if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef) and node.resource.trial_candidate_id is not None:
            return node.resource
    return None


def require_trial_document(document: InputDocument) -> AgentResourceRef:
    """A trial message carries exactly one reference: the trial Agent, without parameters."""
    references = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    if (
        len(references) != 1
        or not isinstance(references[0].resource, AgentResourceRef)
        or references[0].resource.trial_candidate_id is None
        or references[0].parameters
    ):
        raise OwnerTrialRefusal("agent_trial_extra_references")
    return references[0].resource


def trial_refusal_code(exc: BaseException) -> str | None:
    """The public trial code of a trial preparation or admission failure.

    Follows explicit causes and unsuppressed contexts only: a failure deliberately reduced to the
    generic preparation code (``raise ... from None``) stays generic. Callers apply it to trial
    requests only.
    """
    current: BaseException | None = exc
    for _ in range(6):
        if current is None:
            return None
        if isinstance(current, OwnerTrialRefusal):
            return current.code
        if isinstance(current, AgentExecutionBindingError) and current.code in _BINDING_CODES:
            return _BINDING_CODES[current.code]
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return None


@dataclass(frozen=True, slots=True)
class TrialSessionFacts:
    origin: str
    user_id: str
    agent_ids: frozenset[str]


async def read_trial_session_facts(db_path: str | Path, *, session_id: str) -> TrialSessionFacts | None:
    """Origin, owner and the Agents tried in one session; ``None`` when the session does not exist."""
    async with open_pooled_read_ctx(domain_database_path(db_path, "execution")) as db:
        async with db.execute("SELECT origin,user_id FROM sessions WHERE id=?", (session_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        agent_ids: frozenset[str] = frozenset()
        if str(row[0]) == TRIAL_SESSION_ORIGIN and await readable_table_exists(db, "agent_owner_trial_turn"):
            async with db.execute("SELECT DISTINCT agent_id FROM agent_owner_trial_turn WHERE session_id=?", (session_id,)) as cursor:
                agent_ids = frozenset(str(item[0]) for item in await cursor.fetchall())
    return TrialSessionFacts(origin=str(row[0]), user_id=str(row[1]), agent_ids=agent_ids)


def require_trial_session(facts: TrialSessionFacts | None, *, user_id: str, agent_id: str) -> None:
    """A trial continues only its Owner's own trial session of the same Agent; a new session is created for it."""
    if facts is None:
        return
    if facts.origin != TRIAL_SESSION_ORIGIN or facts.user_id != user_id or not facts.agent_ids <= {agent_id}:
        raise OwnerTrialRefusal("agent_trial_session_mismatch")


async def read_owner_trial(db_path: str | Path, *, turn_id: str) -> OwnerTrialTurn | None:
    """The trial a Turn runs, or ``None`` (also on a layout without trials)."""
    async with open_pooled_read_ctx(domain_database_path(db_path, "execution")) as db:
        return await read_owner_trial_turn_in_connection(db, turn_id=turn_id)


async def session_is_owner_trial(db_path: str | Path, *, session_id: str | None) -> bool:
    if not session_id:
        return False
    async with (
        open_pooled_read_ctx(domain_database_path(db_path, "execution")) as db,
        db.execute("SELECT 1 FROM sessions WHERE id=? AND origin=?", (session_id, TRIAL_SESSION_ORIGIN)) as cursor,
    ):
        return await cursor.fetchone() is not None


async def turn_is_owner_trial(db_path: str | Path, *, turn_id: str | None) -> bool:
    if not turn_id:
        return False
    async with (
        open_pooled_read_ctx(domain_database_path(db_path, "execution")) as db,
        db.execute(
            "SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=? AND s.origin=?",
            (turn_id, TRIAL_SESSION_ORIGIN),
        ) as cursor,
    ):
        return await cursor.fetchone() is not None


async def session_trial_agent_id(db_path: str | Path, *, session_id: str) -> str | None:
    """The Agent an ``agent_trial`` session tries (its latest trial Turn)."""
    async with open_pooled_read_ctx(domain_database_path(db_path, "execution")) as db:
        if not await readable_table_exists(db, "agent_owner_trial_turn"):
            return None
        async with db.execute(
            "SELECT agent_id FROM agent_owner_trial_turn WHERE session_id=? ORDER BY created_at DESC,turn_id DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return str(row[0]) if row is not None else None


async def candidate_under_test(db_path: str | Path, *, team_id: str, agent_id: str, candidate_id: str) -> bool:
    """TD1: a test runs on this candidate while the saved draft has moved past it.

    The trial keeps using the candidate under test (replacing it would stop the test); the
    client tells the Owner that this message used the draft being tested.
    """
    async with (
        open_pooled_read_ctx(domain_database_path(db_path, "core")) as db,
        db.execute(
            """
            SELECT 1
            FROM team_subagent_release_candidate candidate
            JOIN team_subagent_working_copy draft
              ON draft.team_id=candidate.team_id AND draft.subagent_id=candidate.subagent_id
            WHERE candidate.team_id=? AND candidate.subagent_id=? AND candidate.id=?
              AND datetime(draft.updated_at)>datetime(candidate.submitted_at)
              AND EXISTS (
                SELECT 1 FROM agent_eval_run run
                WHERE run.team_id=candidate.team_id AND run.agent_id=candidate.subagent_id
                  AND run.candidate_id=candidate.id AND run.status='running'
              )
            """,
            (team_id, agent_id, candidate_id),
        ) as cursor,
    ):
        return await cursor.fetchone() is not None


def trial_attribution(trial: OwnerTrialTurn) -> dict[str, str]:
    """The ``trial`` member of an Agent attribution: which frozen draft answered."""
    return {"candidate_id": trial.candidate_id, "candidate_kind": trial.candidate_kind, "frozen_at": trial.frozen_at}


async def list_recent_owner_trials(
    db_path: str | Path, *, team_id: str, agent_id: str, owner_user_id: str, limit: int = RECENT_TRIALS_LIMIT
) -> list[dict[str, Any]]:
    """The Owner's latest trial sessions of one Agent, newest first; archived and deleted ones are hidden.

    Only the Agent's current Owner gets a list; anyone else gets none.
    """
    async with open_pooled_read_ctx(domain_database_path(db_path, "execution")) as db:
        if not await readable_table_exists(db, "agent_owner_trial_turn"):
            return []
        async with db.execute(
            "SELECT 1 FROM team_subagent WHERE team_id=? AND id=? AND COALESCE(owner_user_id,created_by)=?",
            (team_id, agent_id, owner_user_id),
        ) as cursor:
            if await cursor.fetchone() is None:
                return []
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.id AS session_id,s.last_active_at,
                   (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id) AS turn_count,
                   (SELECT COALESCE(source.user_text,t.user_text) FROM turns t
                      LEFT JOIN chat_turn_user_sources source ON source.turn_id=t.id
                     WHERE t.session_id=s.id ORDER BY t.seq LIMIT 1) AS first_question
            FROM sessions s
            WHERE s.origin='agent_trial' AND s.user_id=? AND s.deleted_at IS NULL
              AND EXISTS (
                SELECT 1 FROM agent_owner_trial_turn trial
                WHERE trial.session_id=s.id AND trial.team_id=? AND trial.agent_id=? AND trial.owner_user_id=?
              )
              AND NOT EXISTS (SELECT 1 FROM user_session_archives archive WHERE archive.session_id=s.id AND archive.user_id=?)
            ORDER BY s.last_active_at DESC,s.id DESC
            LIMIT ?
            """,
            (owner_user_id, team_id, agent_id, owner_user_id, owner_user_id, max(1, min(limit, RECENT_TRIALS_LIMIT))),
        ) as cursor:
            sessions = await cursor.fetchall()
        drafts = await _drafts_used(db, team_id=team_id, agent_id=agent_id, session_ids=[str(row["session_id"]) for row in sessions])
    items = []
    for row in sessions:
        used = drafts.get(str(row["session_id"]), [])
        latest = used[-1] if used else None
        items.append(
            {
                "session_id": str(row["session_id"]),
                "first_question": " ".join(str(row["first_question"] or "").split())[:_QUESTION_PREVIEW_CHARS],
                "last_message_at": str(row["last_active_at"]),
                "turn_count": int(row["turn_count"]),
                "drafts_used": used,
                # The trial ended when the draft it last used was released.
                "released_as": latest["released_as"] if latest is not None else None,
            }
        )
    return items


async def _drafts_used(db: aiosqlite.Connection, *, team_id: str, agent_id: str, session_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    ids = list(session_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    async with db.execute(
        f"""
        SELECT trial.session_id,trial.candidate_id,trial.candidate_kind,trial.frozen_at,
               MIN(trial.created_at) AS first_used_at,
               release.id AS release_id,release.release_sequence
        FROM agent_owner_trial_turn trial
        LEFT JOIN team_subagent_release_candidate candidate
          ON candidate.id=trial.candidate_id AND candidate.team_id=trial.team_id AND candidate.subagent_id=trial.agent_id
         AND candidate.state='activated'
        LEFT JOIN team_subagent_release release
          ON release.team_id=candidate.team_id AND release.subagent_id=candidate.subagent_id AND release.id=candidate.activated_release_id
        WHERE trial.team_id=? AND trial.agent_id=? AND trial.session_id IN ({placeholders})
        GROUP BY trial.session_id,trial.candidate_id
        ORDER BY trial.session_id,first_used_at,trial.candidate_id
        """,  # noqa: S608 -- placeholders only
        (team_id, agent_id, *ids),
    ) as cursor:
        rows = await cursor.fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row["session_id"]), []).append(
            {
                "candidate_id": str(row["candidate_id"]),
                "candidate_kind": str(row["candidate_kind"]),
                "frozen_at": str(row["frozen_at"]),
                "released_as": (
                    {"release_id": str(row["release_id"]), "release_sequence": int(row["release_sequence"])}
                    if row["release_id"] is not None
                    else None
                ),
            }
        )
    return result


__all__ = [
    "RECENT_TRIALS_LIMIT",
    "TRIAL_SESSION_ORIGIN",
    "OwnerTrialRefusal",
    "TrialSessionFacts",
    "agent_draft_trial_enabled",
    "candidate_under_test",
    "list_recent_owner_trials",
    "read_owner_trial",
    "read_trial_session_facts",
    "require_trial_document",
    "require_trial_session",
    "session_is_owner_trial",
    "session_trial_agent_id",
    "trial_attribution",
    "trial_reference",
    "trial_refusal_code",
    "turn_is_owner_trial",
]
