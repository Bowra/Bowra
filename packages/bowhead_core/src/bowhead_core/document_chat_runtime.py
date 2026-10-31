"""A narrow document adapter over the shared Turn finalization and card runtime."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bowhead_core._db import open_shared_ctx
from bowhead_core.document_chat_continuation import DocumentResumeReservation, DocumentTurnOwner
from bowhead_core.document_clarification_lifecycle import (
    consume_document_resume_in_tx,
    document_card_payload,
    prepare_document_pause_in_tx,
)
from bowhead_core.published_diagnostics import get_or_build_chat_turn_failure
from bowhead_core.types import IntentType, TurnResult, TurnStatus

if TYPE_CHECKING:
    from bowhead_core.documents.continuation import DocumentPlanningOutcome

logger = logging.getLogger(__name__)


def worker_from_context(ctx) -> DocumentTurnOwner:
    return DocumentTurnOwner(
        team_id=ctx.team_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        generation=ctx.execution_generation,
        owner_id=ctx.execution_owner_id,
    )


def outcome_message(ctx, kind: str) -> str:
    messages = {
        "en-US": {
            "review": "The draft is ready for review. Check the previews and changes, then confirm publication.",
            "failed": "The document task could not finish. No files were published.",
            "document_authoring_script_failed": "The presentation editing script failed. No files were published. Ask in chat to correct or simplify the requested edits.",
            "document_candidate_invalid": "The presentation failed its file-structure check. No files were published. Check the original template or ask in chat for a corrected draft.",
            "history_unavailable": "Earlier clarification answers cannot be safely reused for this document. No files were published. Start a new request and state the confirmed choices explicitly.",
            "incomplete": "The document still needs information after your answer. This task has stopped; no files were published.",
        },
        "zh-CN": {
            "review": "草稿已可审阅。请检查预览和修改内容，再确认发布。",
            "failed": "文档任务未能完成，未发布文件。",
            "document_authoring_script_failed": "演示文稿编辑脚本执行失败，未发布文件。请在聊天中要求修正或简化本次修改。",
            "document_candidate_invalid": "演示文稿未通过文件结构检查，未发布文件。请检查原模板，或在聊天中要求修正草稿。",
            "history_unavailable": "无法安全复用此前的澄清回答，未发布文件。请发起新请求，并明确写出已确认的选择。",
            "incomplete": "补充回答后，文档仍缺少必要信息。本次任务已停止，未发布文件。",
        },
        "de-DE": {
            "review": "Der Entwurf ist zur Prüfung bereit. Prüfen Sie die Vorschauen und Änderungen und bestätigen Sie dann die Veröffentlichung.",
            "failed": "Die Dokumentaufgabe konnte nicht abgeschlossen werden. Es wurden keine Dateien veröffentlicht.",
            "document_authoring_script_failed": "Das Skript zur Bearbeitung der Präsentation ist fehlgeschlagen. Es wurden keine Dateien veröffentlicht. Bitten Sie im Chat darum, die Änderungen zu korrigieren oder zu vereinfachen.",
            "document_candidate_invalid": "Die Präsentation hat die Dateistrukturprüfung nicht bestanden. Es wurden keine Dateien veröffentlicht. Prüfen Sie die ursprüngliche Vorlage oder bitten Sie im Chat um einen korrigierten Entwurf.",
            "history_unavailable": "Frühere Klärungsantworten können für dieses Dokument nicht sicher wiederverwendet werden. Es wurden keine Dateien veröffentlicht. Starten Sie eine neue Anfrage und nennen Sie die bestätigten Entscheidungen ausdrücklich.",
            "incomplete": "Nach Ihrer Antwort fehlen weiterhin Informationen. Die Aufgabe wurde beendet; es wurden keine Dateien veröffentlicht.",
        },
        "fr-FR": {
            "review": "Le brouillon est prêt à être examiné. Vérifiez les aperçus et les modifications, puis confirmez la publication.",
            "failed": "La tâche documentaire n’a pas pu être terminée. Aucun fichier n’a été publié.",
            "document_authoring_script_failed": "Le script de modification de la présentation a échoué. Aucun fichier n’a été publié. Demandez dans le chat de corriger ou de simplifier les modifications.",
            "document_candidate_invalid": "La présentation n’a pas réussi la vérification de structure du fichier. Aucun fichier n’a été publié. Vérifiez le modèle d’origine ou demandez dans le chat un brouillon corrigé.",
            "history_unavailable": "Les réponses de clarification précédentes ne peuvent pas être réutilisées en toute sécurité pour ce document. Aucun fichier n’a été publié. Lancez une nouvelle demande en précisant les choix confirmés.",
            "incomplete": "Des informations manquent encore après votre réponse. La tâche est arrêtée ; aucun fichier n’a été publié.",
        },
    }
    language = getattr(ctx.language_context, "output_language", "en-US")
    return messages.get(language, messages["en-US"])[kind]


def document_failure_reason(error_code: str, kind: str) -> str:
    """Project finite domain failures without exposing script or source content."""
    if error_code in {"document_history_unavailable", "document_authoring_script_failed"}:
        return error_code
    if error_code in {"pptx_slide_identity_invalid", "pptx_shape_identity_invalid", "pptx_structure_unsupported"}:
        return "document_candidate_invalid"
    return "document_information_incomplete" if kind == "incomplete" else "document_planning_failed"


async def finish_document_planning(ctx, outcome: DocumentPlanningOutcome, *, bus, tokens_input: int = 0, tokens_output: int = 0) -> TurnResult:
    """Finalize trusted domain results; token arguments exclude document usage."""
    from bowhead_core.agents.lead_orchestrator import _emit_event
    from bowhead_core.documents.continuation import DocumentPlanningFailure, DocumentPlanningPause, DocumentPlanningReview

    if not isinstance(outcome, (DocumentPlanningPause, DocumentPlanningReview, DocumentPlanningFailure)):
        raise ValueError("document_planning_outcome_unavailable")
    worker = worker_from_context(ctx)
    card = None
    open_event = None
    failure = None
    if isinstance(outcome, DocumentPlanningPause):
        if outcome.job_id != outcome.checkpoint.job_id:
            raise ValueError("document_planning_outcome_unavailable")
        store = ctx.clarification_store
        async with open_shared_ctx(store.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                card = await prepare_document_pause_in_tx(
                    db,
                    store=store,
                    worker=worker,
                    checkpoint=outcome.checkpoint,
                    questions=[{"question": outcome.question, "type": "free_text", "options": []}],
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        status = TurnStatus.CLARIFICATION_PENDING
        answer = ""
        error = None
        error_kind = "needs_clarification"
    else:
        status = TurnStatus.COMPLETED if isinstance(outcome, DocumentPlanningReview) else TurnStatus.FAILED
        reason = document_failure_reason(outcome.error_code, outcome.kind) if isinstance(outcome, DocumentPlanningFailure) else None
        message_kind = "history_unavailable" if reason == "document_history_unavailable" else reason
        answer = outcome_message(
            ctx,
            message_kind
            if message_kind in {"history_unavailable", "document_authoring_script_failed", "document_candidate_invalid"}
            else outcome.kind,
        )
        error = None if status == TurnStatus.COMPLETED else outcome.error_code
        error_kind = None if status == TurnStatus.COMPLETED else "document_planning_failed"
        if isinstance(outcome, DocumentPlanningFailure):
            failure = get_or_build_chat_turn_failure(
                carrier=bus,
                reason_code=reason,
                turn_id=worker.turn_id,
            )
            error_kind = failure.reason_code
        if isinstance(outcome, DocumentPlanningReview):
            from bowhead_core.documents.canvas_open import build_document_draft_open_in_tx

            try:
                async with open_shared_ctx(ctx.clarification_store.db_path) as db:
                    await db.execute("BEGIN")
                    open_event = await build_document_draft_open_in_tx(db, worker=worker, review=outcome)
            except ValueError:
                # Optional navigation cannot invalidate a retained review. Its
                # original reader still decides whether the user may open it.
                logger.info("document.review_open_unavailable turn_id=%s", worker.turn_id)
    bus._pi_turn_state.update(
        intent=IntentType.AMBIGUOUS,
        final_answer=answer,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        error=error,
        error_kind=error_kind,
    )
    if card is not None:
        await _emit_event(bus, worker.turn_id, "clarification_required", document_card_payload(card))
    elif answer:
        if open_event is not None and not await bus.emit(open_event):
            raise RuntimeError("document_review_event_unavailable")
        await _emit_event(bus, worker.turn_id, "final_answer", {"text": answer, "response_kind": "chat"})
    from bowhead_core.document_chat_usage import document_usage_projection

    display_usage = await document_usage_projection(
        ctx.clarification_store.db_path, turn_id=worker.turn_id, generation_input=tokens_input, generation_output=tokens_output
    )
    await _emit_event(
        bus,
        worker.turn_id,
        "done",
        {
            "turn_id": worker.turn_id,
            "display_usage": display_usage,
            "status": status.value,
            "response_kind": "chat",
            "schema_version": "0.9.0",
            "intent": IntentType.AMBIGUOUS.value,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "error": error,
            "error_kind": error_kind,
            **({"reason_code": failure.reason_code, "diagnostics": failure.diagnostics} if failure is not None else {}),
        },
    )
    return TurnResult(
        turn_id=worker.turn_id,
        status=status,
        intent=IntentType.AMBIGUOUS,
        final_answer=answer,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        error=error,
    )


async def resume_document_planning(runner, request, bus) -> TurnResult:
    """Resume the retained attempt before any Lead, attachment, or model selection."""
    from bowhead_core.agents.lead_orchestrator import _emit_event
    from bowhead_core.effective_language import pin_turn_output_language
    from bowhead_core.tool_registry import ToolExecutionContext

    orch = runner._orch
    service = getattr(orch, "document_service", None)
    if service is None or request.document_checkpoint is None or request.team_router is None:
        raise ValueError("document_continuation_unavailable")
    ctx = ToolExecutionContext(
        workspace_id=request.workspace_id,
        member_uid="",
        user_id=request.user_id,
        turn_id=request.turn_id,
        session_id=request.session_id,
        team_id=request.team_id,
        datarc=None,
        execution_generation=request.execution_generation,
        execution_owner_id=request.execution_owner_id,
        language_context=request.language_context,
        preferred_provider=request.preferred_provider,
        preferred_model=request.preferred_model,
        router=request.team_router,
        access_context=request.access_context,
        clarification_store=orch.clarification_store,
        parent_bus=bus,
        role="lead",
    )
    worker = worker_from_context(ctx)
    reservation = DocumentResumeReservation(
        card_id=request.document_clarification_id, checkpoint=request.document_checkpoint, generation=request.execution_generation
    )
    async with open_shared_ctx(orch.session_store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            attempt = await consume_document_resume_in_tx(db, worker=worker, reservation=reservation)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    bus._pi_persist_turn_state = runner._persist_turn_state
    bus._lead_emit_event = _emit_event
    bus._pi_turn_state = {
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }
    pin_turn_output_language(bus, request.language_context.output_language)
    runner._check_cancel()
    outcome = await service.resume_turn(ctx, attempt)
    runner._check_cancel()
    return await finish_document_planning(ctx, outcome, bus=bus)
