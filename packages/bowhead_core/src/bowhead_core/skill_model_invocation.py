"""Initial legacy reference physical receipts, never a second money ledger.

Opt-in internal runtime adapter. Existing Terminal hooks cannot be replaced.
Pending is a write-ahead dispatch reservation, not proof of network delivery.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from uuid import uuid4

from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.invocation import PhysicalInvocationSpec, physical_invocation_required, physical_invocation_scope
from bowhead_router.outbound import OUTBOUND_PARAMS
from bowhead_router.types import TokenUsage

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import _row
from bowhead_core.reference_mcp_prompt_model import ReferenceMcpPromptModelAccess
from bowhead_core.skill_model_context import ModelContextReceipt
from bowhead_core.skill_read_access import ReferenceSkillReadAccess
from bowhead_core.skill_read_model_input import RequiredSkillModelInput
from bowhead_core.tool_registry import ToolExecutionContext


class ReferenceModelInvocationHook:
    def __init__(self, *, access: ReferenceSkillReadAccess | ReferenceMcpPromptModelAccess, ctx: ToolExecutionContext):
        self.access = access
        self.ctx = ctx
        self._lock = asyncio.Lock()
        self._poisoned = False

    def _require_receiver(self) -> None:
        if type(self.access) is ReferenceMcpPromptModelAccess:
            self.access.require_receiver(self.ctx)
            return
        if type(self.access) is not ReferenceSkillReadAccess:
            raise ValueError()
        self.access.require_model_receiver(self.ctx)

    async def begin(self, spec: PhysicalInvocationSpec) -> ReferenceModelInvocationAttempt:
        inserted = False
        try:
            async with self._lock:
                if self._poisoned:
                    raise ValueError()
                proof = spec.input_receipt
                if type(proof) is not ModelContextReceipt or type(proof.generation) is not int:
                    raise ValueError()
                if type(self.access) is ReferenceSkillReadAccess:
                    await self.ctx.ensure_reference_skill_context()
                self._require_receiver()
                async with open_shared_ctx(self.access.source_db_path) as db:
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        prepared = None
                        if type(self.access) is ReferenceMcpPromptModelAccess:
                            composition, original = await self.access.read_in_connection(db, ctx=self.ctx)
                        else:
                            composition, rendered, prepared = await self.access.read_model_material_in_connection(db, ctx=self.ctx)
                            original = RequiredSkillModelInput.from_rendered(rendered)
                        if (
                            proof.turn_id,
                            proof.execution_id,
                            proof.generation,
                            proof.subagent_id,
                            proof.composition_digest,
                            proof.required_content_hash,
                        ) != (
                            self.access.turn_id,
                            self.access.execution_id,
                            self.access.generation,
                            self.access.subagent_id,
                            composition.digest,
                            original.content_hash,
                        ):
                            raise ValueError()
                        context = await _row(
                            db, "SELECT * FROM chat_reference_model_contexts WHERE turn_id=? AND context_id=?", (proof.turn_id, proof.context_id)
                        )
                        if (
                            context is None
                            or context["retention_state"] != "available"
                            or any(
                                context[key] != value
                                for key, value in {
                                    "execution_id": proof.execution_id,
                                    "created_generation": proof.generation,
                                    "subagent_id": proof.subagent_id,
                                    "composition_digest": proof.composition_digest,
                                    "input_hash": proof.input_hash,
                                    "required_content_hash": proof.required_content_hash,
                                }.items()
                            )
                        ):
                            raise ValueError()
                        unresolved = await _row(
                            db,
                            "SELECT 1 FROM chat_reference_model_invocations WHERE execution_id=? AND created_generation=? AND status IN ('pending','delivery_unknown')",
                            (proof.execution_id, proof.generation),
                        )
                        if unresolved:
                            raise ValueError()
                        values = {
                            "turn_id": proof.turn_id,
                            "invocation_id": "rmi_" + uuid4().hex,
                            "context_id": proof.context_id,
                            "schema_version": 1,
                            "execution_id": proof.execution_id,
                            "created_generation": proof.generation,
                            "subagent_id": proof.subagent_id,
                            "provider": spec.provider,
                            "model": spec.model,
                            "adapter_contract_key": spec.adapter_contract_key,
                            "mode": spec.mode,
                            "request_digest": spec.request_digest,
                            "status": "pending",
                            "retention_state": "available",
                            "created_at": datetime.now(UTC).isoformat(),
                        }
                        if prepared is not None and prepared.extension is not None:
                            from bowhead_core.skill_read_extensions import record_extension_invocation_check

                            await record_extension_invocation_check(db, prepared=prepared, composition_digest=composition.digest, invocation=values)
                        await db.execute(
                            "INSERT INTO chat_reference_model_invocations (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                            tuple(values.values()),
                        )
                        inserted = True
                        await self.access.require_worker_in_connection(db)
                        self._require_receiver()
                        await db.commit()
                    except BaseException:
                        await db.rollback()
                        raise
                return ReferenceModelInvocationAttempt(self, values)
        except BaseException as exc:
            if inserted:
                self._poisoned = True
            if isinstance(exc, Exception):
                raise LLMInputAdmissionError("reference_model_invocation_unavailable") from None
            raise

    async def settle(self, identity: dict, *, status: str, response_digest: str | None = None, usage: TokenUsage | None = None) -> bool:
        committing = False
        try:
            async with self._lock:
                if status == "delivery_unknown":
                    self._poisoned = True
                expected = {
                    "status": status,
                    "response_digest": response_digest,
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "cached_tokens": usage.cached_tokens if usage else None,
                }
                if status not in {"completed", "not_applied", "delivery_unknown"}:
                    raise ValueError()
                if status != "delivery_unknown" and (
                    type(response_digest) is not str
                    or not re.fullmatch("[0-9a-f]{64}", response_digest)
                    or usage is None
                    or any(type(v) is not int or v < 0 for v in (usage.input_tokens, usage.output_tokens, usage.cached_tokens))
                ):
                    raise ValueError()
                async with open_shared_ctx(self.access.source_db_path) as db:
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        row = await _row(db, "SELECT * FROM chat_reference_model_invocations WHERE invocation_id=?", (identity["invocation_id"],))
                        if row is None or any(row[key] != value for key, value in identity.items() if key not in {"status", "retention_state"}):
                            raise ValueError()
                        # Late receipts settle only this original invocation,
                        # even after revocation, Stop, retention purge or a lost owner.
                        # They never read method bytes or grant a new dispatch.
                        if row["status"] != "pending":
                            await db.rollback()
                            if status == "delivery_unknown":
                                return True  # Never erase a confirmed receipt.
                            matched = all(row[key] == value for key, value in expected.items())
                            if not matched:
                                self._poisoned = True
                            return matched
                        values = {**expected, "ended_at": datetime.now(UTC).isoformat()}
                        await db.execute(
                            "UPDATE chat_reference_model_invocations SET "
                            + ",".join(key + "=?" for key in values)
                            + " WHERE invocation_id=? AND status='pending'",
                            (*values.values(), identity["invocation_id"]),
                        )
                        committing = True
                        await db.commit()
                        return True
                    except BaseException:
                        await db.rollback()
                        raise
        except BaseException as exc:
            # The callback still owns the actual response. A lost commit ACK is
            # recoverable only by comparing the exact durable receipt, never by
            # retrying HTTP or replaying an unrelated successful invocation.
            if isinstance(exc, Exception) and committing:
                with suppress(Exception):
                    async with open_shared_ctx(self.access.source_db_path) as db:
                        await db.execute("BEGIN")
                        try:
                            row = await _row(db, "SELECT * FROM chat_reference_model_invocations WHERE invocation_id=?", (identity["invocation_id"],))
                            if (
                                row is not None
                                and all(row[key] == value for key, value in identity.items() if key not in {"status", "retention_state"})
                                and all(row[key] == value for key, value in values.items())
                            ):
                                return True
                        finally:
                            await db.rollback()
            self._poisoned = True
            if isinstance(exc, Exception):
                raise InvocationOutcomeUnknownError("reference_model_settlement_unavailable") from None
            raise


class ReferenceModelInvocationAttempt:
    def __init__(self, hook: ReferenceModelInvocationHook, identity: dict):
        self._hook = hook
        self._identity = dict(identity)

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        return await self._hook.settle(self._identity, status="completed", usage=usage, response_digest=response_digest)

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        # Only the existing native adapters' recognized parameter rejections
        # permit another transport attempt. Arbitrary error text is not evidence.
        if type(evidence_code) is not str or evidence_code not in {
            "parallel_tool_calls_parameter_rejected/v1",
            *(f"outbound_param_rejected/{param}/v1" for param in OUTBOUND_PARAMS),
        }:
            await self.unknown(reason_code="unrecognized_non_application")
            return False
        return await self._hook.settle(self._identity, status="not_applied", response_digest=evidence_digest, usage=TokenUsage())

    async def unknown(self, *, reason_code: str) -> None:
        # Persist only the finite outcome, never transport/provider error text.
        await self._hook.settle(self._identity, status="delivery_unknown")


@contextmanager
def reference_model_invocation_scope(
    *, access: ReferenceSkillReadAccess | ReferenceMcpPromptModelAccess, ctx: ToolExecutionContext
) -> Iterator[ReferenceModelInvocationHook]:
    if physical_invocation_required():
        raise LLMInputAdmissionError("reference_model_invocation_scope_conflict")
    hook = ReferenceModelInvocationHook(access=access, ctx=ctx)
    with physical_invocation_scope(hook):
        yield hook
