"""Coach's alignment-loop Tools over the Owner's evaluation (alignment-loop technical design §4).

Every operation runs as the Coach conversation's user and requires that user to be the Agent's
current Owner (`authorize_owner`, then the store's own Owner check). There is deliberately no
operation here that records a label, confirms a point or changes a case's held-out flag: those are
the Owner's own HTTP actions (`routes/agents.py`), which a Coach Tool call can't reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_alignment import (
    LABEL_MEANING,
    AgentEvalAlignmentError,
    AgentEvalAlignmentStore,
    AlignmentService,
    build_report_conn,
    card_pointer,
    latest_loop_execution_conn,
    loop_view,
    question_view,
)
from bowhead_core.agent_eval_conversation_cases import list_conversation_candidates, question_text
from bowhead_core.agent_eval_judge import TeamRouterEvalJudge
from bowhead_core.agent_eval_point_calibration import TeamRouterCalibrationWriter
from bowhead_core.agent_eval_product import AgentEvalProductStore
from bowhead_router.usage import usage_context

from bowhead_gateway._entitlement_gate import assert_not_readonly

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources

# Shown once on the loop's first question card (R16); the frontend renders its own localized copy.
HELD_OUT_NOTICE = "held_out_once_per_point"


async def judge_fingerprint(request: Any, team_id: str) -> str | None:
    runtime = getattr(request.app.state, "agent_eval_runtime", None)
    if runtime is None:
        return None
    return await runtime.judge_model_hash(team_id=team_id)


async def alignment_service(request: Any, *, team_id: str, agent_id: str, actor_user_id: str) -> AlignmentService:
    """The production judge (one answer per call) and Coach's minimal-edit writer, via the Team router."""

    from bowhead_gateway.routes._deps import resolve_team_router

    db_path = str(request.app.state.user_store.db_path)
    judge_router, _judge_miss = await resolve_team_router(request.app.state, team_id, agent_type="judge", site="agent_eval_alignment")
    writer_router, _writer_miss = await resolve_team_router(request.app.state, team_id, agent_type="echo", site="agent_eval_alignment")
    product = AgentEvalProductStore(db_path)

    async def fingerprint() -> str | None:
        return await judge_fingerprint(request, team_id)

    async def live() -> list[dict[str, Any]]:
        # The service keeps only owner_quality cases; platform contract attempts are never answers.
        return await product.live_answers(team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, limit=20, exclude_held_out=True)

    return AlignmentService(
        store=AgentEvalAlignmentStore(db_path),
        judge=TeamRouterEvalJudge(router=judge_router) if judge_router is not None else None,
        writer=TeamRouterCalibrationWriter(router=writer_router) if writer_router is not None else None,
        current_judge_fingerprint=fingerprint,
        live_answers=live,
    )


@dataclass(frozen=True)
class AgentCoachAlignment:
    resources: AgentCoachResources

    @property
    def db_path(self) -> str:
        return str(self.resources.store.db_path)

    @property
    def store(self) -> AgentEvalAlignmentStore:
        return AgentEvalAlignmentStore(self.db_path)

    def owner(self, agent_id: str) -> dict[str, str]:
        return {"team_id": self.resources.user.team_id, "agent_id": agent_id, "actor_user_id": self.resources.user.id}

    async def authorize(self, agent_id: str, *, write: bool) -> None:
        if write:
            await assert_not_readonly(self.resources.request)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await self.resources.evaluations.authorize_owner(db, agent_id)

    async def turns(self, *, agent_id: str, limit: int) -> dict[str, Any]:
        await self.authorize(agent_id, write=False)
        items = await list_conversation_candidates(
            self.db_path, team_id=self.resources.user.team_id, agent_id=agent_id, user_id=self.resources.user.id, limit=limit
        )
        return {"ok": True, "items": items, "content_role": "untrusted_conversation_data", "answers_included": False}

    async def owner_messages(self, turn_id: str) -> tuple[str | None, list[str]]:
        """This Turn's message and the Owner's messages in this conversation (newest first), each without
        the @ mention, read from the Owner's own session."""

        if not self.resources.session_id:
            return None, []
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT turn.id,COALESCE(source.user_text,turn.user_text),source.input_document_json
                FROM turns turn
                JOIN sessions session ON session.id=turn.session_id AND session.user_id=? AND session.deleted_at IS NULL
                LEFT JOIN chat_turn_user_sources source ON source.turn_id=turn.id AND source.user_id=session.user_id
                WHERE turn.session_id=?
                ORDER BY turn.started_at DESC,turn.id DESC LIMIT 50
                """,
                (self.resources.user.id, self.resources.session_id),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        messages = [(str(row[0]), " ".join(question_text(str(row[1] or ""), row[2]).split())) for row in rows]
        current = next((text for row_turn, text in messages if row_turn == turn_id and text), None)
        return current, [text for _turn, text in messages if text]

    async def owner_statement(self, words: str | None, *, turn_id: str) -> str | None:
        """The words the loop quotes as the Owner's (§13 origin). Coach's `words` stand only when they are
        copied from one of the Owner's messages in this conversation, so a point never quotes Coach as the Owner.

        A paraphrase (often Coach's own point text) is replaced by the Owner's message in this Turn only when
        that is the Owner's only message. Otherwise this Turn's message is often a follow-up ("don't edit the
        prompt yet"), not the complaint, and quoting it misstated the origin of every point (116 dogfood,
        1.5.84): the refusal lists the Owner's messages for Coach to copy the complaint from."""

        said = " ".join((words or "").split())
        if not said:
            return words
        current, messages = await self.owner_messages(turn_id)
        if any(said.casefold() in message.casefold() for message in messages):
            return said
        if current and len(messages) == 1:
            return current
        raise AgentEvalAlignmentError(
            "agent_eval_alignment_words_not_owner",
            details={
                "action": "pass the Owner's complaint copied verbatim from one of these messages, or ask the Owner what is wrong "
                "and start with the reply",
                "owner_messages": [message[:500] for message in messages[:5]],
            },
        )

    async def start(
        self,
        *,
        agent_id: str,
        words: str | None,
        origin_turn_id: str | None,
        words_from_feedback: bool,
        suite_id: str | None,
        turn_id: str = "",
    ) -> dict[str, Any]:
        await self.authorize(agent_id, write=True)
        if not words_from_feedback:
            words = await self.owner_statement(words, turn_id=turn_id)
        fingerprint = await judge_fingerprint(self.resources.request, self.resources.user.team_id)
        if fingerprint is None:
            # Actionable: the Team evaluation model is what grades the loop's answers.
            return {"ok": False, "error": "agent_eval_judge_unavailable", "action": "configure_team_evaluation_model"}
        loop = await self.store.start(
            **self.owner(agent_id),
            session_id=self.resources.session_id,
            words=words,
            origin_turn_id=origin_turn_id,
            words_from_feedback=words_from_feedback,
            judge_model_fingerprint=fingerprint,
            suite_id=suite_id,
        )
        return {
            "ok": True,
            "loop_id": loop.id,
            "suite_id": loop.suite_id,
            "words_source": loop.words_source,
            # A member's comment is read live, never stored: restate it as the point.
            "owner_words": loop.owner_words if loop.words_source == "owner" else await self.store.feedback_comment(loop),
            "words_by_owner": loop.words_source == "owner",
            "judge_pinned": True,
            "labels_recorded": False,
        }

    async def ask(self, *, agent_id: str, loop_id: str, point: dict[str, Any], answer_refs: list[str], replace_unclear: bool) -> dict[str, Any]:
        await self.authorize(agent_id, write=True)
        user = self.resources.user
        service = await alignment_service(self.resources.request, team_id=user.team_id, agent_id=agent_id, actor_user_id=user.id)
        # Judge and writer calls count toward the Team's evaluation usage (E6).
        with usage_context(team_id=user.team_id, user_id=user.id, reason="agent_eval_alignment"):
            result = await service.ask(
                **self.owner(agent_id),
                loop_id=loop_id,
                point_id=point.get("id"),
                kind=point["kind"],
                text=point["text"],
                answer_refs=answer_refs,
                replace_unclear=replace_unclear,
            )
        loop, _questions = await self.store.read(**self.owner(agent_id), loop_id=loop_id)
        if result["cold_start"]:
            return {
                "ok": True,
                "cold_start": True,
                "point_id": result["point"]["id"],
                "next_step": "draft_from_definition",
                "message": "This Agent has no real answers yet (no Live evaluation attempts, no own or shared Turns).",
            }
        return {
            "ok": True,
            "cold_start": False,
            "point_id": result["point"]["id"],
            "card": card_pointer(loop, result["point"]["id"], result["card_seq"]),
            "rows": [question_view(row, for_coach=True) for row in result["rows"]],
            # `added`, or why this card has no counterexample (the point then can't confirm from this card alone).
            "counterexample": result.get("counterexample"),
            "notice": HELD_OUT_NOTICE if result["first_card"] else None,
            "labels_recorded": False,
            "labels_mean": LABEL_MEANING,
            "next_step": "wait_for_owner_labels",
            # Coach kept reading and re-asking after a card until it ran out of steps, and the Turn ended as a
            # failure under a valid card.
            "message": "The card is in the chat now. End this turn with one short line asking the Owner to check whether "
            "the verdicts on the card are right; the card explains the labels, so don't. Call no more Tools until the Owner "
            "continues.",
        }

    async def resolve(self, agent_id: str | None, loop_id: str | None) -> tuple[str, str]:
        """The loop a read names, or the Owner's current open loop when Coach omits `loop_id`: on `agent_id`
        when given, else the newest one started in this conversation. A loop id always needs its Agent."""

        if loop_id is not None:
            if agent_id is None:
                raise AgentEvalAlignmentError("agent_eval_alignment_agent_required", details={"loop_id": loop_id})
            return agent_id, loop_id
        found = await self.store.open_loop(
            team_id=self.resources.user.team_id,
            actor_user_id=self.resources.user.id,
            agent_id=agent_id,
            session_id=self.resources.session_id or None,
        )
        if found is None:
            # Actionable: nothing open to continue, so Coach starts a loop from the Owner's words.
            raise AgentEvalAlignmentError(
                "agent_eval_alignment_not_found",
                status=404,
                details={"agent_id": agent_id, "scope": "agent" if agent_id else "conversation", "action": "agent_eval_alignment_start"},
            )
        return found

    async def get(self, *, agent_id: str | None = None, loop_id: str | None = None) -> dict[str, Any]:
        agent_id, loop_id = await self.resolve(agent_id, loop_id)
        await self.authorize(agent_id, write=False)
        loop, questions = await self.store.read(**self.owner(agent_id), loop_id=loop_id)
        comment = await self.store.feedback_comment(loop)
        return {"ok": True, **loop_view(loop, questions, for_coach=True, feedback_comment=comment), "content_role": "untrusted_evaluation_data"}

    async def report(self, *, agent_id: str | None = None, loop_id: str | None = None) -> dict[str, Any]:
        agent_id, loop_id = await self.resolve(agent_id, loop_id)
        await self.authorize(agent_id, write=True)
        loop, _questions = await self.store.read(**self.owner(agent_id), loop_id=loop_id)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            execution_id = await latest_loop_execution_conn(db, loop)
            if execution_id is None:
                return {"ok": True, "kind": "agent_eval_alignment_report", "loop_id": loop.id, "status": "no_check", "next_action": "request_check"}
            report = await build_report_conn(db, loop, execution_id=execution_id)
        # A new id per report: only the current one is stored, so the chat collapses older report cards.
        report = {"report_id": f"aealr_{uuid4().hex}", **report}
        await self.store.save_report(loop=loop, report=report, baseline_comparison_id=execution_id)
        return {
            "ok": True,
            **report,
            "report_card": {"kind": "agent_eval_alignment_report", "agent_id": agent_id, "loop_id": loop.id, "report_id": report["report_id"]},
        }

    async def call(self, operation: str, args: dict, *, turn_id: str) -> dict[str, Any]:
        if operation == "agent_eval_turns_list":
            return await self.turns(**args)
        if operation == "agent_eval_alignment_start":
            return await self.start(**args, turn_id=turn_id)
        if operation == "agent_eval_alignment_ask":
            return await self.ask(**args)
        if operation == "agent_eval_alignment_get":
            return await self.get(**args)
        if operation == "agent_eval_alignment_report":
            return await self.report(**args)
        raise ValueError("agent_eval_alignment_operation_unsupported")
