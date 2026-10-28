"""Document-owned physical model receipts and cumulative credit settlement."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import PhysicalInvocationSpec
from bowhead_router.types import TokenUsage

from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.delivery import _execution, _fence, _now
from bowhead_core.documents.pptx_planning import REPAIRABLE_ERRORS
from bowhead_core.documents.request import DocumentModelSelection, DocumentRequestError, DocumentScope
from bowhead_core.rate_card import ModelCreditRates, ModelTokenUsage, ModelUsageLine, credits_for_tokens_at_version
from bowhead_core.user_store import _deduct_credit_with_stable_id_in_tx


@dataclass
class DocumentModelHook:
    path: str
    scope: DocumentScope
    job_id: str
    owner: str
    call_no: int
    attempt_no: int = 1

    async def begin(self, spec: PhysicalInvocationSpec):
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            execution = await _fence(db, self.scope, self.job_id, self.owner)
            if execution["state"] != "planning" or type(self.attempt_no) is not int or self.attempt_no not in (1, 2) or self.call_no not in (1, 2):
                raise InvocationOutcomeUnknownError("document_execution_fenced")
            async with db.execute("SELECT model_selection_json FROM document_dispatches WHERE job_id=?", (self.job_id,)) as cursor:
                dispatch = await cursor.fetchone()
            if dispatch is not None and dispatch[0] is not None:
                selected = DocumentModelSelection.model_validate_json(dispatch[0])
                if (spec.provider, spec.model) != (selected.provider, selected.model):
                    raise DocumentRequestError("document_model_configuration_changed")
            async with db.execute(
                "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=?", (self.job_id, self.attempt_no)
            ) as cursor:
                attempt = await cursor.fetchone()
            async with db.execute("SELECT 1 FROM document_turn_dispatches WHERE job_id=?", (self.job_id,)) as cursor:
                turn_owned = await cursor.fetchone() is not None
            if (attempt is None and (turn_owned or self.attempt_no != 1)) or (attempt is not None and attempt["owner_id"] != self.owner):
                raise InvocationOutcomeUnknownError("document_execution_fenced")
            if self.call_no == 2 and execution["error_code"] not in REPAIRABLE_ERRORS:
                raise InvocationOutcomeUnknownError("document_model_repair_forbidden")
            async with db.execute("SELECT 1 FROM document_usage_receipts WHERE job_id=? AND settled_at IS NULL", (self.job_id,)) as cursor:
                if await cursor.fetchone():
                    raise DocumentRequestError("document_usage_settlement_pending")
            async with db.execute("SELECT * FROM document_model_calls WHERE job_id=? ORDER BY attempt_no,call_no", (self.job_id,)) as cursor:
                all_calls = list(await cursor.fetchall())
            calls = [call for call in all_calls if call["attempt_no"] == self.attempt_no]
            if len(calls) != self.call_no - 1 or (
                any(call["state"] != "completed" or (call["provider"], call["model"]) != (spec.provider, spec.model) for call in all_calls)
            ):
                raise InvocationOutcomeUnknownError("document_model_replay_forbidden")
            async with db.execute(
                "SELECT u.credit_balance,t.team_credit_balance FROM users u JOIN teams t ON t.id=? WHERE u.id=?",
                (self.scope.team_id, self.scope.user_id),
            ) as cursor:
                balance = await cursor.fetchone()
            if balance is None or min(balance) <= 0:
                raise DocumentRequestError("document_credit_insufficient")
            await db.execute(
                """INSERT INTO document_model_calls(job_id,attempt_no,call_no,state,provider,model,request_digest,created_at,credit_rates_json,usage_family)
                VALUES (?,?,?,'dispatched',?,?,?,?,?,?)""",
                (
                    self.job_id,
                    self.attempt_no,
                    self.call_no,
                    spec.provider,
                    spec.model,
                    spec.request_digest,
                    _now(),
                    json.dumps(
                        ModelCreditRates.from_config(dict(spec.credit_rates) if spec.credit_rates is not None else None).as_json(), sort_keys=True
                    ),
                    spec.usage_family,
                ),
            )
            await db.commit()
        return self

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE document_model_calls SET state='not_applied',error_code=?,response_digest=? WHERE job_id=? AND attempt_no=? AND call_no=? AND state='dispatched'",
                (evidence_code, evidence_digest, self.job_id, self.attempt_no, self.call_no),
            )
            await db.commit()
        # No implicit fallback or second transport under the same document call.
        return False

    async def unknown(self, *, reason_code: str) -> None:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE document_model_calls SET state='unknown',error_code=? WHERE job_id=? AND attempt_no=? AND call_no=? AND state='dispatched'",
                (reason_code, self.job_id, self.attempt_no, self.call_no),
            )
            await db.commit()

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        if any(type(value) is not int or value < 0 for value in (usage.input_tokens, usage.output_tokens, usage.cached_tokens)):
            raise ValueError("Authoritative document usage is required")
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            execution = await _execution(db, self.job_id)
            async with db.execute(
                "SELECT * FROM document_model_calls WHERE job_id=? AND attempt_no=? AND call_no=?", (self.job_id, self.attempt_no, self.call_no)
            ) as cursor:
                call = await cursor.fetchone()
            async with db.execute(
                "SELECT owner_id FROM document_planning_attempts WHERE job_id=? AND attempt_no=?", (self.job_id, self.attempt_no)
            ) as cursor:
                attempt = await cursor.fetchone()
            identity = (response_digest, usage.input_tokens, usage.output_tokens, usage.cached_tokens)
            owner = attempt["owner_id"] if attempt is not None else (execution["owner"] if execution is not None else None)
            if call is None or owner != self.owner:
                return False
            if call["state"] not in {"dispatched", "completed", "unknown"}:
                return False
            async with db.execute(
                "SELECT * FROM document_usage_receipts WHERE job_id=? AND attempt_no=? AND call_no=?", (self.job_id, self.attempt_no, self.call_no)
            ) as cursor:
                receipt = await cursor.fetchone()
            if receipt is not None:
                if identity != (receipt["response_digest"], receipt["tokens_input"], receipt["tokens_output"], receipt["tokens_cached"]):
                    return False
            else:
                await db.execute(
                    """INSERT INTO document_usage_receipts(job_id,attempt_no,call_no,response_digest,tokens_input,tokens_output,tokens_cached,created_at,credit_rates_json,usage_family)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (self.job_id, self.attempt_no, self.call_no, *identity, _now(), call["credit_rates_json"], call["usage_family"]),
                )
            if call["state"] == "dispatched":
                await db.execute(
                    "UPDATE document_model_calls SET state='completed',response_digest=?,tokens_input=?,tokens_output=?,tokens_cached=? WHERE job_id=? AND attempt_no=? AND call_no=?",
                    (*identity, self.job_id, self.attempt_no, self.call_no),
                )
            # Preserve delivered evidence before attempting any balance mutation.
            # An unknown call may acquire usage evidence, never execution permission.
            await db.commit()
        await settle_document_usage(self.path, self.job_id)
        return True


async def settle_document_usage(path: str, job_id: str) -> None:
    """Settle only durable known receipts; never invoke or resume a model."""
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        execution = await _execution(db, job_id)
        async with db.execute("SELECT * FROM document_jobs WHERE id=?", (job_id,)) as cursor:
            job = await cursor.fetchone()
        async with db.execute(
            "SELECT r.*,c.provider,c.model FROM document_usage_receipts r "
            "JOIN document_model_calls c ON c.job_id=r.job_id AND c.attempt_no=r.attempt_no AND c.call_no=r.call_no "
            "WHERE r.job_id=? ORDER BY r.attempt_no,r.call_no",
            (job_id,),
        ) as cursor:
            receipts = await cursor.fetchall()
        tokens_input = tokens_output = 0
        model_usage: list[ModelUsageLine] = []
        settled = execution["settled_credits"]
        for receipt in receipts:
            tokens_input += receipt["tokens_input"]
            tokens_output += receipt["tokens_output"]
            line = None
            if execution["rate_card_version"] == "v2":
                if receipt["credit_rates_json"] is None:
                    raise DocumentRequestError("document_usage_settlement_pending")
                line = ModelUsageLine(
                    receipt["provider"],
                    receipt["model"],
                    ModelTokenUsage.from_provider_counts(
                        receipt["tokens_input"],
                        receipt["tokens_cached"],
                        receipt["tokens_output"],
                        cached_in_input=receipt["usage_family"] != "anthropic",
                        rates=ModelCreditRates.from_config(json.loads(receipt["credit_rates_json"])),
                    ),
                )
                model_usage.append(line)
            if receipt["settled_at"] is not None:
                continue
            cumulative = credits_for_tokens_at_version(
                execution["rate_card_version"],
                tokens_input,
                tokens_output,
                model_usage=[item.usage for item in model_usage] if line is not None else None,
            )
            delta = cumulative - settled
            if delta < 0:
                raise ValueError("Document settlement must be monotonic")
            result, _ = await _deduct_credit_with_stable_id_in_tx(
                db,
                ledger_id=(
                    f"document:{job_id}:{receipt['call_no']}"
                    if receipt["attempt_no"] == 1
                    else f"document:{job_id}:attempt:{receipt['attempt_no']}:call:{receipt['call_no']}"
                ),
                user_id=job["requested_by"],
                team_id=job["team_id"],
                cost_credits=delta,
                turn_id=None,
                workspace_id=job["workspace_id"],
                tokens_input=receipt["tokens_input"],
                tokens_output=receipt["tokens_output"],
                reason="template_document",
                rate_card_version=execution["rate_card_version"],
                model_usage=(line,) if line is not None else (),
                cumulative_v2=line is not None,
            )
            if result != "confirmed_applied":
                raise DocumentRequestError("document_usage_settlement_pending")
            settled = cumulative
            await db.execute(
                "UPDATE document_usage_receipts SET settled_at=? WHERE job_id=? AND attempt_no=? AND call_no=?",
                (_now(), job_id, receipt["attempt_no"], receipt["call_no"]),
            )
        await db.execute(
            """UPDATE document_executions SET settled_credits=?,error_code=CASE
            WHEN error_code IN ('document_usage_settlement_pending','document_usage_unresolved') AND NOT EXISTS(
              SELECT 1 FROM document_model_calls c LEFT JOIN document_usage_receipts r ON r.job_id=c.job_id AND r.attempt_no=c.attempt_no AND r.call_no=c.call_no
              WHERE c.job_id=document_executions.job_id AND c.state IN ('dispatched','unknown') AND r.job_id IS NULL)
            THEN 'document_execution_interrupted' ELSE error_code END WHERE job_id=?""",
            (settled, job_id),
        )
        await db.commit()


async def settle_pending_usage(path: str) -> None:
    """A bounded, feature-independent sweep; retry only the settlement ledger."""
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT job_id FROM document_usage_receipts WHERE settled_at IS NULL AND retry_after<=? GROUP BY job_id ORDER BY MIN(retry_after),job_id LIMIT 25",
            (time.time(),),
        ) as cursor:
            jobs = [row[0] for row in await cursor.fetchall()]
        for job_id in jobs:
            await db.execute("UPDATE document_usage_receipts SET retry_after=? WHERE job_id=? AND settled_at IS NULL", (time.time() + 60, job_id))
        await db.commit()
    for job_id in jobs:
        try:
            await settle_document_usage(path, job_id)
        except Exception as exc:
            import structlog

            structlog.get_logger(__name__).warning("document.settlement_retry_failed", job_id=job_id, error_type=type(exc).__name__)
