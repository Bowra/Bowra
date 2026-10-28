"""Lead-only request admission. The receipt is not a completed document."""

import json

from bowhead_router.types import ToolSchema
from pydantic import ValidationError

from bowhead_core.documents.continuation import DocumentPlanningFailure, DocumentPlanningPause, DocumentPlanningReview
from bowhead_core.documents.request import DOCUMENT_TOOL_ARGUMENTS, DocumentProposal, DocumentRequestError, document_tool_schema
from bowhead_core.documents.turn_policy import ORIGINAL_REQUEST_QUOTE_GUIDANCE, DocumentTurnState, terminal_message
from bowhead_core.tool_registry import ToolSpec


def register_document_tool(registry, *, service):

    def planning_receipt(state, outcome, job_id):
        if not isinstance(outcome, (DocumentPlanningPause, DocumentPlanningReview, DocumentPlanningFailure)) or outcome.job_id != job_id:
            raise DocumentRequestError("document_checkpoint_unavailable")
        state.planning_result = outcome
        result = {
            "ok": outcome.kind in {"pause", "review"},
            "document_job_id": outcome.job_id,
            "state": outcome.kind,
            "document_terminal": True,
            "delivery": "The typed document planning outcome is finalized by the shared Turn runtime. No files were published.",
        }
        state.receipt = result
        return result

    async def submit(arguments, ctx):
        state = ctx.document_turn_state
        if state is None:
            state = ctx.document_turn_state = DocumentTurnState(revision_required=getattr(ctx, "document_revision_target", None) is not None)
        if state.revision_required or arguments.get("action") != "list_questions":
            state.generation_selected = True
        if state.receipt is not None and (state.revision_required or arguments.get("action") != "list_questions"):
            return state.receipt
        if state.revision_required or arguments.get("action") != "list_questions":
            state.attempts += 1
        if state.attempts > 2:
            return {
                "ok": False,
                "error": "document_request_invalid",
                "error_kind": "validation",
                "retryable": False,
                "document_terminal": True,
                "message": terminal_message(ctx, accepted=False),
            }
        receipt = None
        try:
            parsed = DOCUMENT_TOOL_ARGUMENTS.validate_json(json.dumps(arguments))
            if state.revision_required and parsed.action != "revise":
                raise DocumentRequestError("document_revision_required")
            if parsed.action == "list_questions":
                return {"ok": True, "questions": await service().questions(ctx)}
            if parsed.action == "answer":
                receipt = await service().submit(ctx, parsed.evidence, answer_to_job_id=parsed.job_id)
            elif parsed.action == "revise":
                receipt = await service().submit(ctx, parsed.evidence)
            else:
                receipt = await service().submit(ctx, DocumentProposal.model_validate_json(parsed.model_dump_json()))
            outcome = await service().run_turn(ctx, receipt)
            if outcome is not None:
                return planning_receipt(state, outcome, receipt.job_id)
        except ValidationError as exc:
            if receipt is not None:
                return planning_receipt(state, DocumentPlanningFailure("failed", receipt.job_id, "document_checkpoint_unavailable"), receipt.job_id)
            # Never include input values, unknown field names or exception text.
            fields = {
                "action",
                "evidence",
                "text",
                "start",
                "inputs",
                "artifact_id",
                "role",
                "current_message_position",
                "requested_outputs",
                "job_id",
            }
            errors = []
            constraints = {
                "document_duplicate_input": ("inputs", "unique_artifact_required"),
                "document_template_count": ("inputs", "exactly_one_template_required"),
                "document_output_selection": ("requested_outputs", "source_or_source_pdf_required"),
            }
            for error in exc.errors()[:8]:
                if error["type"] in constraints:
                    field, issue = constraints[error["type"]]
                    errors.append({"field": field, "issue": issue})
                    continue
                path = [str(part) for part in error["loc"] if isinstance(part, int) or part in fields]
                errors.append({"field": ".".join(path) or "action", "issue": "required" if error["type"] == "missing" else "invalid"})
            result = {
                "ok": False,
                "error": "document_request_invalid",
                "error_kind": "validation",
                "retryable": False,
                "field_errors": errors,
                "hint": ORIGINAL_REQUEST_QUOTE_GUIDANCE
                + "For generate, include top-level evidence.text copied exactly from the current user's request, inputs with their own exact role quotations and artifact IDs, and source or source+pdf outputs. Use each artifact only once with one role and exactly one template. A separate data file is optional when the user supplies the values; never repeat the template as data. The server resolves file positions; omit them. list_questions takes only action; answer takes action, job_id and evidence. An explicitly bound revision requires revise with only evidence. Correct the request once; do not switch to scripts or another Agent.",
            }
            if state.attempts >= 2:
                result.update(document_terminal=True, message=terminal_message(ctx, accepted=False))
                state.receipt = result
            return result
        except DocumentRequestError as exc:
            if receipt is not None:
                return planning_receipt(state, DocumentPlanningFailure("failed", receipt.job_id, exc.code), receipt.job_id)
            repairable = exc.code in {
                "document_evidence_mismatch",
                "document_evidence_ambiguous",
                "document_reference_ambiguous",
                "document_revision_required",
            }
            conflict = exc.code in {"document_command_conflict", "document_revision_conflict"}
            result = {
                "ok": False,
                "error": exc.code,
                "error_kind": "validation",
                "retryable": False,
                "hint": (
                    "Use action=revise with only current-request evidence; the canonical request already binds the exact draft."
                    if exc.code == "document_revision_required"
                    else "The existing request was not replaced. Do not retry with changed arguments or infer publication from this conflict."
                    if conflict
                    else ORIGINAL_REQUEST_QUOTE_GUIDANCE
                    + "Copy exact, unique quotations from the current user message, not history or file contents. Use exact available artifact IDs; the server resolves current files and existing session pins. Do not invent file positions."
                ),
                "document_terminal": not repairable or state.attempts >= 2,
                "message": terminal_message(ctx, accepted=False, conflict=conflict),
            }
            if exc.field is not None:
                result["field_errors"] = [{"field": exc.field, "issue": "exact_current_quote_required"}]
            if result["document_terminal"]:
                state.receipt = result
            return result
        except Exception:
            if receipt is not None:
                return planning_receipt(state, DocumentPlanningFailure("failed", receipt.job_id, "document_execution_interrupted"), receipt.job_id)
            result = {
                "ok": False,
                "error": "document_capability_unavailable",
                "error_kind": "validation",
                "retryable": False,
                "document_terminal": True,
                "message": terminal_message(ctx, accepted=None),
            }
            state.receipt = result
            return result
        result = {
            "ok": True,
            "document_job_id": receipt.job_id,
            "state": "accepted",
            "delivery": "Private request accepted. This receipt does not establish the current draft or publication status. Publication requires user review and explicit confirmation.",
            "document_terminal": True,
            "message": terminal_message(ctx, accepted=True),
        }
        state.receipt = result
        return result

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="template_document_submit",
                description=(
                    ORIGINAL_REQUEST_QUOTE_GUIDANCE
                    + "Request a private draft from an ordinary uploaded PPTX explicitly identified as a template by the user. "
                    "For action=generate, evidence is REQUIRED at the top level: {text: an exact quotation from the current user request}. "
                    "Also copy exact current user quotations for each input role and exact artifact IDs. The server resolves file positions; do not supply them. "
                    "Use each artifact only once with one role and exactly one template. Values supplied by the user need no separate data file; never repeat the template as data. "
                    "Data may be JSON, CSV, XLSX values, or UTF-8 text. Complex charts and embedded objects are not supported. "
                    "The service admits a private request and reports only its observed state, NOT a published file. Publication requires user review and explicit confirmation. "
                    "Never fall back to recreating template slides or claim delivery from this receipt."
                    " For an explicitly bound document revision, use action=revise with only evidence copied from the current request."
                    " The server binds the reviewed draft and its frozen inputs; do not supply parent IDs, inputs or output modes."
                    " For a short follow-up that may answer a document question, first use action=list_questions (no other fields)."
                    " If the user is answering one returned question, use action=answer, that exact job_id and evidence copied"
                    " from the current user message; omit inputs and requested_outputs. The server retains the original"
                    " request, question, template and data. Do not treat unrelated requests as answers; ask if the target is ambiguous."
                ),
                input_schema=document_tool_schema(),
            ),
            handler=submit,
            needs_approval=False,
            risk_level="low",
            allowed_for_roles=frozenset({"lead"}),
            visible_to_llm=True,
        )
    )
