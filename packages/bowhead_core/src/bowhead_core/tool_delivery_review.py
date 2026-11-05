"""Bounded delivery review shared by terminal Tools and ordinary closeout.

The model assesses semantic coverage; code verifies call identities and actual
outcomes. This never authorizes redispatch or changes effect settlement.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.tool_outcome import classify_tool_outcome, invocation_result

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolSpec

_REVIEW_ERROR = "tool_delivery_review_required"
_MAX_REVIEW_REJECTIONS = 2
_MAX_REVIEW_CALLS = 2000
_REQUIRED_GAP_GUIDANCE = (
    "A missing required result remains unfulfilled even when its absence is correctly disclosed. "
    "Keep the original requested outcome as the requirement; never replace it with explaining why it is missing. "
    "Reporting that required work was not delivered is not delivery of that work. "
)
_DELIVERY_REPLY_GUIDANCE = (
    "Describe the delivered results and any limitations that affect their use. State unmet requirements and needed user action when delivery is incomplete. "
    "Keep recovered, covered and optional failed attempts with no remaining impact in the internal execution record. "
    "Omit delivery-review narration and tool failure tallies or claims such as 'no calls failed' from ordinary replies. "
    "Report actual failures when the user asks for diagnostics or a negative-test observation. "
)
_DELIVERY_SCOPE_GUIDANCE = (
    "Delivery review is internal control feedback. Apply this reply policy to user-facing progress, answers and caveats. "
    + _DELIVERY_REPLY_GUIDANCE
    + "Keep evidence bookkeeping and raw protocol fields internal unless technical diagnostics were requested. A rejected review is not failed business work and does not erase "
    "retained files. Requirements describe required final outcomes, not Tool-call transcripts. Do not add separate fulfilled success "
    "requirements merely for reporting an observation or proving no retry, write or file creation occurred. Those constraints still "
    "apply: an actual violation remains an unmet requirement. Record operational attempts in issues. For an explicitly user-requested "
    "negative test, expected_outcome=failure cites actual known failed call IDs from this run, with that issue resolved as expected using "
    "the same receipt. The observed refusal and accurate account of whether business work ran are one diagnostic outcome, not additional "
    "positive-data requirements. Return that observation as normal chat delivery, not an error/bail envelope; do not fabricate report "
    "evidence. This proves the requested failure observation, never successful business delivery. Other fulfilled success requirements "
    "must cite exact IDs from review_context.successful_calls. A partial call is not positive evidence for its successful subset or proof "
    "that dispatch failed. For mixed BI, verify the required retained value with an already granted artifact reader when the user permits "
    'reading. If python_exec is granted, read the original BI _ref_artifact_id through inputs={"rows":{"artifact_id":"<original '
    "reference>\"}} and INPUTS['rows']['path']; print retained values without querying BI again or creating files. Cite that successful "
    "read's exact current-run call ID for the business requirement and resolution covered. Keep a merely optional refusal in that issue's "
    "explanation, not an extra required negative test. Never cite the partial BI call as positive evidence, even if its sample is "
    "accurate. If reading is unavailable, keep delivery partial. A caller reviewing a successful delegation of a negative observation "
    "cites its own successful delegation receipt with expected_outcome=success for delivery of that observation; the rejected underlying "
    "operation did not succeed. Never cite child call IDs. A helper's review does not replace the caller's scope assessment. Never repeat "
    "queries, invent evidence or bypass source access. Optional failure alone does not make required delivery incomplete; never remove or "
    "downgrade genuine required outcomes. "
) + _REQUIRED_GAP_GUIDANCE


class TerminalCorrectionRefusal(dict):
    """Trusted pre-publication correction, never a business-operation receipt."""


class DeliveryReviewRefusal(TerminalCorrectionRefusal):
    """Native policy provenance which JSON returned by a Tool cannot forge."""


class ReviewedDeliveryResult(dict):
    """Current native review provenance, never reconstructed from ordinary JSON."""


def delivery_review_enabled() -> bool:
    return os.environ.get("FEATURE_TOOL_DELIVERY_REVIEW", "1") == "1"


def failed_calls(invocations: Sequence[Any]) -> dict[str, Any]:
    """Retain every failed attempt; a different same-name success proves no recovery."""
    return {
        inv.call_id: inv
        for inv in invocations
        if getattr(inv, "call_id", None)
        and classify_tool_outcome(invocation_result(inv), inv.error).state in {"failure", "partial"}
        and (inv.error is not None or not isinstance(invocation_result(inv), TerminalCorrectionRefusal))
    }


def requires_delivery_review(invocations: Sequence[Any]) -> bool:
    """Include reviewed child failures without making a successful delegation fail."""
    return bool(failed_calls(invocations)) or any(
        inv.tool_name == "subagent_spawn" and isinstance(invocation_result(inv), ReviewedDeliveryResult) for inv in invocations
    )


def _review_call_context(invocations: Sequence[Any]) -> dict[str, Any]:
    """Expose current-run evidence identities without copying arguments or results."""
    calls = invocations[-_MAX_REVIEW_CALLS:]
    failures = [
        {
            "call_id": inv.call_id,
            "tool": inv.tool_name,
            "state": classify_tool_outcome(invocation_result(inv), inv.error).state,
            "failure_code": classify_tool_outcome(invocation_result(inv), inv.error).code,
        }
        for inv in failed_calls(calls).values()
    ]
    successes = [
        {"call_id": inv.call_id, "tool": inv.tool_name}
        for inv in calls
        if getattr(inv, "call_id", None)
        and not isinstance(invocation_result(inv), TerminalCorrectionRefusal)
        and inv.tool_name != LeadDeliveryReview.name
        and classify_tool_outcome(invocation_result(inv), inv.error).state == "success"
    ]
    return {"failed_calls": failures, "successful_calls": successes, "truncated": len(invocations) > _MAX_REVIEW_CALLS}


def review_guidance(invocations: Sequence[Any], *, include_call_context: bool = True, review_tool: str | None = None) -> str:
    import json

    submission = (
        f"Call {review_tool} alone with payload.delivery_review, then answer in ordinary prose without protocol JSON. Include requirements "
        if review_tool
        else "In a terminal payload include delivery_review with requirements "
    )
    guidance = (
        "Before finishing, check the current user request and confirmed scope against actual delivered results. "
        "Account for every failed call below. Do not repeat confirmed writes. A later success with the same "
        "Tool name is not proof of recovery. Resolve missing required work within the existing budget, or "
        "return the useful results and clearly disclose the unmet requirement. Optional work can be covered "
        "by an equivalent successful result. "
        + submission
        + "[{requirement,status:fulfilled|unfulfilled,expected_outcome:success|failure,evidence_call_ids:[...]}] and issues "
        "[{call_id,resolution:recovered|covered|expected|unfulfilled,evidence_call_ids:[...],explanation}]. "
        "Use review_context.successful_calls for successful delivery evidence and review_context.failed_calls "
        "for failed attempts or explicitly requested negative tests. Artifact reference numbers, artifact IDs "
        "and section IDs are not call IDs. "
        "These call IDs are only for delivery_review, never artifact inputs. Coverage is your assessment, "
        "not an execution receipt. Native closeout corrections are not business failures or delivery evidence. "
        "Unmet work without a failed invocation belongs only in requirements as unfulfilled; never invent an issue call_id. "
        "Use recovered only for a later success of the same Tool with identical arguments; use covered with an explanation for a changed alternative. "
        + _DELIVERY_SCOPE_GUIDANCE
    )
    if include_call_context:
        guidance += "review_context (JSON data): " + json.dumps(_review_call_context(invocations), ensure_ascii=False)
    return guidance


@dataclass(frozen=True, slots=True)
class DeliveryReview:
    complete: bool
    valid: bool
    problems: tuple[str, ...] = ()
    invalid_evidence_fields: tuple[str, ...] = ()


def validate_delivery_review(review: Any, invocations: Sequence[Any]) -> DeliveryReview:
    """Verify reference identity and ordering, without pretending to prove semantics."""
    failures = failed_calls(invocations)
    if not requires_delivery_review(invocations) and review is None:
        return DeliveryReview(True, True)
    if not isinstance(review, dict):
        return DeliveryReview(False, False, ("delivery_review is required to verify current delivery",))
    positions = {inv.call_id: index for index, inv in enumerate(invocations)}
    successes = {
        inv.call_id: inv
        for inv in invocations
        if classify_tool_outcome(invocation_result(inv), inv.error).state == "success"
        and not isinstance(invocation_result(inv), TerminalCorrectionRefusal)
        and inv.tool_name != LeadDeliveryReview.name
    }
    requirements, issues = review.get("requirements"), review.get("issues")
    if not isinstance(requirements, list) or not requirements or not isinstance(issues, list):
        return DeliveryReview(False, False, ("requirements and issues must be nonempty review lists",))
    if len(requirements) > 100 or len(issues) > _MAX_REVIEW_CALLS:
        return DeliveryReview(False, False, ("review exceeds the bounded run evidence capacity",))
    seen: set[str] = set()
    complete = True
    problems: list[str] = []
    invalid_evidence_fields: list[str] = []
    expected_failures: set[str] = set()

    def evidence(item: dict[str, Any], *, field: str, failed_id: str | None = None) -> None:
        ids = item.get("evidence_call_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(cid, str) or cid not in successes for cid in ids):
            problems.append("fulfilled coverage requires actual successful evidence_call_ids from this run")
            invalid_evidence_fields.append(field)
            return
        if failed_id is not None and item.get("resolution") == "recovered":
            original = failures[failed_id]
            if not any(
                positions[cid] > positions[failed_id]
                and successes[cid].tool_name == original.tool_name
                and successes[cid].arguments == original.arguments
                for cid in ids
            ):
                problems.append(
                    f"{failed_id}: recovery requires a later successful call of the same operation and arguments; use covered with an explanation for a corrected alternative"
                )
                invalid_evidence_fields.append(field)

    for index, item in enumerate(requirements):
        if not isinstance(item, dict) or not isinstance(item.get("requirement"), str) or not item["requirement"].strip():
            problems.append("each requirement must identify the requested work")
            continue
        expected = item.get("expected_outcome", "success")
        if expected not in ("success", "failure"):
            problems.append("expected_outcome must be success or failure")
        if item.get("status") == "fulfilled":
            if expected == "failure":
                ids = item.get("evidence_call_ids")
                if (
                    not isinstance(ids, list)
                    or not ids
                    or any(
                        not isinstance(cid, str)
                        or cid not in failures
                        or (outcome := classify_tool_outcome(invocation_result(failures[cid]), failures[cid].error)).state != "failure"
                        or outcome.code == "tool_execution_unknown"
                        for cid in ids
                    )
                ):
                    problems.append("a fulfilled negative test requires actual known failure evidence_call_ids from this run")
                    invalid_evidence_fields.append(f"requirements[{index}].evidence_call_ids")
                else:
                    expected_failures.update(ids)
            else:
                evidence(item, field=f"requirements[{index}].evidence_call_ids")
        elif item.get("status") == "unfulfilled":
            complete = False
        else:
            problems.append("requirement status must be fulfilled or unfulfilled")
    for index, item in enumerate(issues):
        if not isinstance(item, dict):
            problems.append("each issue must be an object")
            continue
        cid = item.get("call_id")
        if not isinstance(cid, str) or cid not in failures or cid in seen:
            problems.append(
                "issues must identify each actual failed call exactly once; keep unmet work without a failed call in requirements, not an invented issue"
            )
            invalid_evidence_fields.append(f"issues[{index}].call_id")
            continue
        seen.add(cid)
        original_outcome = classify_tool_outcome(invocation_result(failures[cid]), failures[cid].error)
        if original_outcome.code == "tool_execution_unknown" and item.get("resolution") != "unfulfilled":
            problems.append(f"{cid}: an unknown effect requires the original effect reconciliation, not a model completion declaration")
        if item.get("resolution") in {"recovered", "covered"}:
            if not isinstance(item.get("explanation"), str) or not item["explanation"].strip():
                problems.append(f"{cid}: explain how the result covers the original work")
            evidence(item, field=f"issues[{index}].evidence_call_ids", failed_id=cid)
        elif item.get("resolution") == "expected":
            if cid not in expected_failures or item.get("evidence_call_ids") != [cid]:
                problems.append(f"{cid}: expected resolution requires the same receipt in a fulfilled explicit negative-test requirement")
                invalid_evidence_fields.append(f"issues[{index}].evidence_call_ids")
            if not isinstance(item.get("explanation"), str) or not item["explanation"].strip():
                problems.append(f"{cid}: explain the explicitly requested negative test and its observed failure")
        elif item.get("resolution") == "unfulfilled":
            complete = False
        else:
            problems.append(f"{cid}: unknown resolution")
    if seen != set(failures):
        problems.append("every failed call must be accounted for")
    return DeliveryReview(complete and not problems, not problems, tuple(dict.fromkeys(problems)), tuple(dict.fromkeys(invalid_evidence_fields)))


def review_terminal_payload(
    payload: dict[str, Any],
    invocations: Sequence[Any],
    *,
    language: str = "en-US",
    evidence_problem: str | None = None,
    review_tool: str | None = None,
) -> dict[str, Any] | None:
    """Return correction feedback, or annotate accepted partial delivery in place."""
    if not delivery_review_enabled():
        return None
    if isinstance(payload.get("error"), dict) and payload["error"].get("kind") == "needs_user_input":
        return None
    if not requires_delivery_review(invocations) and payload.get("delivery_review") is None:
        return None
    review = validate_delivery_review(payload.get("delivery_review"), invocations)
    if evidence_problem:
        review = DeliveryReview(False, False, (*review.problems, evidence_problem), review.invalid_evidence_fields)
    if not review.valid:
        rejected = sum(isinstance(invocation_result(inv), DeliveryReviewRefusal) for inv in invocations)
        if rejected < _MAX_REVIEW_REJECTIONS:
            return DeliveryReviewRefusal(
                ok=False,
                error_kind=_REVIEW_ERROR,
                error=" ".join(review.problems) + "\n" + review_guidance(invocations, include_call_context=False, review_tool=review_tool),
                review_context={**_review_call_context(invocations), "invalid_evidence_fields": list(review.invalid_evidence_fields)},
                retryable=False,
            )
    payload["completion"] = "complete" if review.complete and payload.get("completion") != "partial" else "partial"
    if review.valid:
        # Requested negative observations are deliverables. Coverage explanations are internal evidence.
        caveats = list(payload.get("caveats") or []) if isinstance(payload.get("caveats"), list) else []
        for issue in payload["delivery_review"]["issues"]:
            if issue.get("resolution") == "expected" and issue["explanation"].strip() not in caveats:
                caveats.append(issue["explanation"].strip())
        if caveats:
            payload["caveats"] = caveats
    if not review.complete:
        # Existing payload caveats are the shared report/chat disclosure carrier.
        caveats = payload.get("caveats")
        caveats = list(caveats) if isinstance(caveats, list) else []
        requirements = (payload.get("delivery_review") or {}).get("requirements", []) if isinstance(payload.get("delivery_review"), dict) else []
        gaps = [
            item["requirement"].strip()
            for item in requirements
            if isinstance(item, dict) and item.get("status") == "unfulfilled" and isinstance(item.get("requirement"), str)
        ]
        from bowhead_core.report_chrome_text import chrome_text

        if review.valid:
            caveats.extend(gap for gap in gaps if gap not in caveats)
        else:
            caveats.append(chrome_text("delivery_unverified", language) + (" " + "; ".join(gaps) if gaps else ""))
        payload["caveats"] = caveats
    return None


def terminal_review_schema(schema: Any, payload_field: str) -> Any:
    """Advertise the common review field on a trusted native payload schema."""
    import copy
    from dataclasses import replace

    if not delivery_review_enabled():
        return schema
    definition = copy.deepcopy(schema.input_schema)
    payload = definition.get("properties", {}).get(payload_field)
    if not isinstance(payload, dict):
        return schema
    item = {
        "type": "object",
        "properties": {
            "requirement": {
                "type": "string",
                "description": "A required final outcome. Do not split one requested negative observation into extra successful reporting or no-action requirements. Actual constraint violations remain unfulfilled.",
            },
            "status": {"type": "string", "enum": ["fulfilled", "unfulfilled"], "description": _REQUIRED_GAP_GUIDANCE},
            "expected_outcome": {
                "type": "string",
                "enum": ["success", "failure"],
                "description": "Defaults to success. Use failure only when citing the actual known failed operation in this run for an explicitly requested negative test. Delivery of a delegated negative observation cites the caller's successful delegation receipt with success. Never use failure for missing business delivery or cite child call IDs.",
            },
            "evidence_call_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For fulfilled success, copy exact IDs from review_context.successful_calls, never a partial receipt. For an explicitly requested negative test, cite its actual known failed ID with expected_outcome failure. Unfulfilled requirements may use an empty list.",
            },
        },
        "required": ["requirement", "status", "evidence_call_ids"],
    }
    issue = {
        "type": "object",
        "properties": {
            "call_id": {"type": "string"},
            "resolution": {
                "type": "string",
                "enum": ["recovered", "covered", "expected", "unfulfilled"],
                "description": "Recovered requires the same Tool and identical arguments; use covered for a changed alternative with successful evidence and an explanation.",
            },
            "evidence_call_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Covered/recovered cite exact successful_calls IDs; expected cites only the same failed call ID as the explicit negative-test requirement. A partial BI receipt is never successful evidence.",
            },
            "explanation": {
                "type": "string",
                "description": "Internal reason the cited evidence resolves the issue. State material limitations of the delivered result separately in the user-facing answer or caveats. For an explicitly requested negative test, describe the observed result.",
            },
        },
        "required": ["call_id", "resolution", "evidence_call_ids", "explanation"],
    }
    properties = payload.setdefault("properties", {})
    if schema.name != LeadDeliveryReview.name:
        properties["completion"] = {
            "type": "string",
            "enum": ["complete", "partial"],
            "description": "Use partial whenever a required result is missing or unverified, even if every Tool succeeded. Explaining the gap does not fulfill it. Disclose what is missing and any needed material in the answer. Optional caveats alone do not make delivery partial. Use delivery_review to record unfulfilled requirements; never invent failed calls.",
        }
    properties["delivery_review"] = {
        "type": "object",
        "description": "After a failed call or when required work remains unfulfilled, review the current task and confirmed scope against delivered results. Account for every failed call. Successful delivery needs successful call IDs; explicit negative tests need known failure call IDs with expected_outcome failure. Without a failed call, record the missing requirement as unfulfilled with issues empty. Do not replay confirmed writes.",
        "properties": {
            "requirements": {"type": "array", "description": _DELIVERY_SCOPE_GUIDANCE, "items": item},
            "issues": {"type": "array", "items": issue},
        },
        "required": ["requirements", "issues"],
    }
    return replace(schema, input_schema=definition)


async def _require_evidence_scope(ctx: Any) -> None:
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope

    async with open_shared_ctx(ctx.artifact_store.db_path) as db:
        await require_reference_request_scope(
            db,
            ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id),
        )


async def review_evidence_availability(review: Any, invocations: Sequence[Any], ctx: Any) -> str | None:
    """Recheck retained evidence through the existing artifact/Canvas boundaries."""
    if not isinstance(review, dict) or not validate_delivery_review(review, invocations).valid:
        return None
    referenced = {cid for entry in [*review["requirements"], *review["issues"]] for cid in entry.get("evidence_call_ids", [])}
    scope_checked = False
    for invocation in invocations:
        result = invocation_result(invocation)
        if invocation.call_id not in referenced or not isinstance(result, dict):
            continue
        artifacts = [result.get("artifact_id")]
        outputs = result.get("output_artifacts")
        if isinstance(outputs, list):
            artifacts.extend(item.get("artifact_id") for item in outputs if isinstance(item, dict) and item.get("data_role") != "stdout")
        try:
            section_id = result.get("section_id")
            if not scope_checked and (any(isinstance(aid, str) and aid.startswith("art_") for aid in artifacts) or section_id):
                await _require_evidence_scope(ctx)
                scope_checked = True
            for artifact_id in artifacts:
                if isinstance(artifact_id, str) and artifact_id.startswith("art_"):
                    from bowhead_core.artifact_presentation import available_artifact

                    await available_artifact(ctx, artifact_id)
            section_id = result.get("section_id")
            if isinstance(section_id, str) and section_id:
                canvas = await ctx.canvas_store.get_canvas_by_session(ctx.session_id)
                if canvas is None or await ctx.canvas_store.get_section_canvas_id(section_id) != canvas.id:
                    return f"Evidence from {invocation.call_id} no longer exists in this session."
        except Exception:
            return f"Evidence from {invocation.call_id} is unavailable under current access or storage conditions."
    return None


def delivery_summary_input(result: Any) -> dict[str, str]:
    """Supply request-local review facts consumed before durable event emission."""
    if not delivery_review_enabled():
        return {}
    if getattr(result, "ordinary_completion_reason", None) in {"model_unknown", "tool_unknown"}:
        return {"_delivery_completion": "unverified"}
    if result.completion == "partial":
        return {"_delivery_completion": "partial"}
    if getattr(result, "reviewed_delivery_complete", False) is True:
        return {"_delivery_completion": "reviewed_complete"}
    if not failed_calls(getattr(result, "tool_invocations", [])):
        return {}
    return {"_delivery_completion": "complete"}


class LeadDeliveryReview:
    """Run-local review control; no business execution or terminal write authority."""

    name = "review_delivery"

    def __init__(self, *, reader_available: bool = False) -> None:
        self.reader_available = reader_available
        self.payload: dict[str, Any] | None = None
        self.call_ids: tuple[str, ...] = ()

    @staticmethod
    def _business_ids(invocations: Sequence[Any]) -> tuple[str, ...]:
        return tuple(
            inv.call_id
            for inv in invocations
            if inv.tool_name != LeadDeliveryReview.name and not isinstance(invocation_result(inv), TerminalCorrectionRefusal)
        )

    @staticmethod
    def _pending_source_readback(invocations: Sequence[Any]) -> list[str]:
        """Require one original-reader attempt per retained source-closeout reference."""
        pending: dict[str, None] = {}
        for inv in invocations:
            if inv.tool_name == "subagent_spawn":
                result = invocation_result(inv)
                if not isinstance(result, dict) or result.get("ok") is not False or result.get("failure_stage") != "source_verification":
                    continue
                for item in result.get("retained_artifacts") or []:
                    if isinstance(item, dict) and isinstance(item.get("artifact_id"), str):
                        pending[item["artifact_id"]] = None
            elif inv.tool_name == "present_artifacts":
                items = inv.arguments.get("artifacts")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("artifact_id"), str):
                        pending.pop(item["artifact_id"], None)
        return list(pending)[:16]

    async def current(self, invocations: Sequence[Any], ctx: Any) -> dict[str, Any] | None:
        if self.payload is None or self.call_ids != self._business_ids(invocations):
            return None
        review = self.payload.get("delivery_review")
        if not validate_delivery_review(review, invocations).valid or await review_evidence_availability(review, invocations, ctx):
            return None
        return self.payload

    def tool_spec(self) -> ToolSpec:
        from bowhead_core.agents._lead_context import run_invocations
        from bowhead_core.effective_language import effective_output_language
        from bowhead_core.terminal_effect_contract import EffectClass, ToolEffectIdempotency, ToolEffectSpec
        from bowhead_core.tool_registry import ToolSchema, ToolSpec
        from bowhead_core.types import ToolAnnotations

        async def submit(arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
            import copy

            self.payload = None
            invocations = run_invocations(ctx)

            def refuse(message: str) -> dict[str, Any]:
                if sum(isinstance(invocation_result(inv), DeliveryReviewRefusal) for inv in invocations) >= _MAX_REVIEW_REJECTIONS:
                    return {
                        "ok": True,
                        "reviewed_completion": "partial",
                        "instruction": "Disclose unverified delivery in the ordinary answer. Do not repeat confirmed writes.",
                    }
                return DeliveryReviewRefusal(ok=False, error_kind=_REVIEW_ERROR, error=message, retryable=False)

            if getattr(ctx, "terminal_batch_conflict", False):
                return refuse(f"Call {self.name} alone after the other calls settle. Do not repeat confirmed writes.")
            if self.reader_available and (pending := self._pending_source_readback(invocations)):
                return refuse(
                    "Before reviewing delivery, call present_artifacts with "
                    + json.dumps({"artifacts": [{"artifact_id": aid} for aid in pending]})
                    + ". Use its observed result for the retained files. Preserve the failed source check separately."
                )
            payload = arguments.get("payload")
            if not isinstance(payload, dict) or set(payload) != {"delivery_review"} or not isinstance(payload["delivery_review"], dict):
                return refuse(f"Call {self.name} with only payload.delivery_review as a JSON object.")
            payload = copy.deepcopy(payload)
            failure = review_terminal_payload(
                payload,
                invocations,
                language=effective_output_language(bus=getattr(ctx, "parent_bus", None), language_context=getattr(ctx, "language_context", None)),
                evidence_problem=await review_evidence_availability(payload["delivery_review"], invocations, ctx),
                review_tool=self.name,
            )
            if failure is not None:
                return failure
            self.payload = payload
            self.call_ids = self._business_ids(invocations)
            return {
                "ok": True,
                "reviewed_completion": payload.get("completion", "complete"),
                "instruction": "Answer the user in ordinary prose. "
                + _DELIVERY_REPLY_GUIDANCE
                + "Do not output protocol JSON or repeat confirmed writes. Review again if you execute more work.",
            }

        schema = terminal_review_schema(
            ToolSchema(
                name=self.name,
                description="Review current-run delivery evidence without executing work or writing a report. Call alone after work settles, then answer normally. Later work invalidates this review.",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["payload"],
                    "properties": {"payload": {"type": "object", "additionalProperties": False, "required": ["delivery_review"], "properties": {}}},
                },
            ),
            "payload",
        )
        return ToolSpec(
            schema=schema,
            handler=submit,
            allowed_for_roles=frozenset({"lead"}),
            annotations=ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False),
            effect_spec=ToolEffectSpec(EffectClass.NONE, ToolEffectIdempotency.NONE),
        )
