"""Native document selection and admission result handling."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolCall, ToolSchema

if TYPE_CHECKING:
    from bowhead_core.documents.continuation import DocumentPlanningOutcome
    from bowhead_core.types import ToolInvocation

ORIGINAL_REQUEST_QUOTE_GUIDANCE = (
    "On resumed tasks, quote the decoded original_request string, not its JSON wrapper or escapes. "
    "clarification_responses contains decisions, not original-request quotation evidence. "
)

NATIVE_INPUT_GUIDANCE = (
    "Use each artifact only once with one role and exactly one template. A separate data file is optional. "
    "The server supplies retained clarification answers automatically; do not invent an answer-file artifact or repeat the template as data. "
)

NATIVE_DOCUMENT_GUIDANCE = (
    "Template document workflow: when the user identifies an uploaded PPTX as a template and asks to update it, "
    "use template_document_submit directly; the document worker reads the template and data. "
    "If the user explicitly requests inspection before generation, use one read-only subagent_spawn first "
    "with delegation_role=helper (ordinary omission also returns to Lead); never use primary for inspection. "
    "It may return Chat observations, never publish files. Use native clarification_ask for an explicitly requested decision. "
    "After inspection or its answered clarification, return to template_document_submit, not another helper. "
    "Do not route this task to a BI Agent, recreate slides, or generate a generic downloadable file. "
    "Any requested PDF must be derived from the reviewed source presentation in this same workflow, not from a separate report. "
    "For generate, include top-level evidence.text copied exactly from the user's current request, plus inputs "
    "with their own exact current-message role quotations and artifact IDs. The server resolves file positions. "
    + ORIGINAL_REQUEST_QUOTE_GUIDANCE
    + NATIVE_INPUT_GUIDANCE
    + "The server returns a private draft request, not a finished file or proof of a working review entry. "
    "Publication requires review of rendered pages, changes and font substitutions, followed by explicit user confirmation. "
    "Confirmation does not call the model again. Do not invent a review control or claim that this receipt publishes files. "
    "If a short follow-up answers a document question, list_questions first, then answer that exact job. "
    "For an explicitly bound document revision target, use action=revise with evidence from the current request only. "
    "The server retains the selected draft and frozen inputs; do not supply a parent ID or fall back to generate. "
    "An unrelated PowerPoint reading, analysis or new presentation request still follows its normal workflow."
)

UNAVAILABLE_DOCUMENT_GUIDANCE = (
    "Uploaded-template updates are not available in this task. If requested, explain that limitation; "
    "do not substitute scripts or a new presentation for a layout-preserving update or its source-derived PDF. "
    "Ordinary reading, analysis and explicitly requested new presentations retain their own available Tools."
)

DOCUMENT_REVISION_GUIDANCE = (
    "This request explicitly revises the exact saved document draft selected by the user. "
    "Call template_document_submit with action revise and evidence.text copied exactly from this request. "
    "The server supplies the selected draft and retained inputs. Do not supply a job ID, digest, template, or input list. "
    "Do not generate an unrelated file or switch to another workflow if revision fails."
)


@dataclass
class DocumentTurnState:
    generation_selected: bool = False
    native_in_batch: bool = False
    attempts: int = 0
    receipt: dict[str, Any] | None = None
    revision_required: bool = False
    planning_result: DocumentPlanningOutcome | None = None

    def __post_init__(self) -> None:
        if self.revision_required:
            self.generation_selected = True

    def select(self, calls: Iterable[ToolCall]) -> None:
        # Latch before sibling calls execute, regardless of their order.
        self.native_in_batch = any(
            call.name == "template_document_submit" and (call.arguments or {}).get("action") != "list_questions" for call in calls
        )
        if self.native_in_batch:
            self.generation_selected = True

    def request_tools(self, schemas: list[ToolSchema]) -> list[ToolSchema]:
        """Keep an explicitly selected native workflow on its original tool."""
        if self.generation_selected:
            return [schema for schema in schemas if schema.name == "template_document_submit"]
        return schemas

    def denial(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.generation_selected and tool_name != "template_document_submit":
            return {
                "ok": False,
                "error": "document_workflow_required",
                "error_kind": "validation",
                "retryable": False,
                "document_terminal": not self.native_in_batch and self.attempts > 0,
                "hint": "This request has selected private document review. Continue with template_document_submit.",
            }
        return None


def empty_lookup_before_question(invocations: list[ToolInvocation], state: DocumentTurnState | None) -> bool:
    """Qualify only the native, effect-free document lookup/question prefix."""
    if (
        state is None
        or state.revision_required
        or state.generation_selected
        or state.native_in_batch
        or state.attempts
        or state.receipt is not None
        or state.planning_result is not None
        or len(invocations) != 2
        or any(inv.error is not None or inv.nested for inv in invocations)
    ):
        return False
    lookup, question = invocations
    return (
        lookup.tool_name == "template_document_submit"
        and lookup.arguments == {"action": "list_questions"}
        and isinstance(lookup.result, dict)
        and lookup.result.get("ok") is True
        and lookup.result.get("error") is None
        and isinstance(lookup.result.get("questions"), list)
        and lookup.result["questions"] == []
        and question.tool_name == "clarification_ask"
        and isinstance(question.result, dict)
        and question.result.get("error") is None
        and question.result.get("_clarification_pending") is True
    )


def abandoned_generation_rejection(invocations: list[ToolInvocation], state: DocumentTurnState | None, *, ordinary: bool) -> bool:
    """Identify a known pre-admission refusal, never infer one from missing files."""
    if (
        not ordinary
        or state is None
        or not state.generation_selected
        or state.revision_required
        or state.attempts != 1
        or state.receipt is not None
        or state.planning_result is not None
        or any(inv.nested for inv in invocations)
    ):
        return False
    native = [inv for inv in invocations if inv.tool_name == "template_document_submit" and inv.arguments.get("action") != "list_questions"]
    if len(native) != 1:
        return False
    call = native[0]
    result = call.result
    return (
        call.arguments.get("action") == "generate"
        and call.error is None
        and isinstance(result, dict)
        and result.get("ok") is False
        and result.get("error_kind") == "validation"
        and result.get("retryable") is False
        and result.get("document_terminal") is not True
        and result.get("document_job_id") is None
        and result.get("error")
        in {"document_request_invalid", "document_evidence_mismatch", "document_evidence_ambiguous", "document_reference_ambiguous"}
    )


def pending_source_action_message(ctx: Any) -> str:
    language = str(getattr(getattr(ctx, "language_context", None), "output_language", "") or "en-US")
    messages = {
        "en-US": "No draft was created because a tool explicitly selected for this task has not completed. Run that selected tool first, then request the document draft.",
        "zh-CN": "本次未创建草稿，因为这项任务明确选中的工具尚未完成。请先完成所选工具的操作，再请求生成文档草稿。",
        "de-DE": "Es wurde kein Entwurf erstellt, weil ein ausdrücklich für diese Aufgabe ausgewähltes Werkzeug noch nicht abgeschlossen wurde. Führen Sie zuerst dieses Werkzeug aus und fordern Sie dann den Dokumententwurf an.",
        "fr-FR": "Aucun brouillon n’a été créé, car un outil explicitement sélectionné pour cette tâche n’a pas terminé son opération. Exécutez d’abord cet outil, puis demandez le brouillon du document.",
    }
    return messages.get(language, messages["en-US"])


def terminal_message(ctx: Any, *, accepted: bool | None, conflict: bool = False) -> str:
    language = str(getattr(getattr(ctx, "language_context", None), "output_language", "") or "en-US")
    messages = {
        "en-US": (
            "Your template request has been accepted. This receipt does not mean the draft or files are ready. Publication requires your review of the previews and changes, followed by explicit confirmation.",
            "The template request could not be accepted, so no draft was created. Please check the request and try again.",
            "The template request status could not be confirmed. Do not resend it merely because the status is uncertain; this response does not establish whether files were published.",
            "This submission conflicts with the existing template request, which was not replaced. Do not start another request merely to resolve this conflict.",
        ),
        "zh-CN": (
            "已受理模板生成请求。这条回执不代表草稿或文件已经完成；发布仍需你审阅预览与修改摘要，并明确确认。",
            "模板请求未能受理，尚未创建草稿。请检查请求后重试。",
            "暂时无法确认模板请求状态。请勿仅因状态未知而重复提交；本条回复不能证明文件是否已经发布。",
            "本次提交与已有模板请求冲突，原请求未替换。请勿仅为解决此冲突而重复发起请求。",
        ),
        "de-DE": (
            "Ihre Vorlagenanfrage wurde angenommen. Diese Bestätigung bedeutet nicht, dass der Entwurf oder die Dateien fertig sind. Vor der Veröffentlichung müssen Sie die Vorschauen und Änderungen prüfen und ausdrücklich bestätigen.",
            "Die Vorlagenanfrage konnte nicht angenommen werden. Es wurde kein Entwurf erstellt. Prüfen Sie die Anfrage und versuchen Sie es erneut.",
            "Der Status der Vorlagenanfrage konnte nicht bestätigt werden. Senden Sie die Anfrage nicht allein wegen des ungewissen Status erneut; diese Antwort bestätigt nicht, ob Dateien veröffentlicht wurden.",
            "Diese Übermittlung steht im Konflikt mit der bestehenden Vorlagenanfrage. Die ursprüngliche Anfrage wurde nicht ersetzt. Starten Sie nicht allein zur Behebung dieses Konflikts eine weitere Anfrage.",
        ),
        "fr-FR": (
            "Votre demande a été acceptée. Cet accusé de réception ne signifie pas que le brouillon ou les fichiers sont prêts. La publication nécessite votre vérification des aperçus et des modifications, puis votre confirmation explicite.",
            "La demande n’a pas pu être acceptée et aucun brouillon n’a été créé. Vérifiez la demande et réessayez.",
            "Le statut de la demande n’a pas pu être confirmé. Ne renvoyez pas la demande uniquement parce que son statut est incertain ; cette réponse ne permet pas de savoir si des fichiers ont été publiés.",
            "Cet envoi est en conflit avec la demande de modèle existante, qui n’a pas été remplacée. Ne lancez pas une autre demande uniquement pour résoudre ce conflit.",
        ),
    }
    return messages.get(language, messages["en-US"])[3 if conflict else 2 if accepted is None else 0 if accepted else 1]
