"""Durable observation at the shared transport seam, separate from authority.

The existing usage ledger remains the single usage writer for each observed
call. Source content is encrypted and never included in public projections.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite
from bowhead_router.invocation import PhysicalInvocationSpec, canonical_transport_digest
from bowhead_router.types import LLMRequest, TokenUsage
from bowhead_router.usage import current_usage_context

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.rate_card import ModelCreditRates, ModelTokenUsage, ModelUsageLine

MAX_SOURCE_BYTES = 64 * 1024


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _wire_tool_results(body: Mapping[str, object], family: str | None) -> list[tuple[str, str]]:
    result = []
    messages = body.get("input" if family == "openai_responses" else "messages")
    if not isinstance(messages, list):
        return result
    for message in messages:
        if not isinstance(message, dict):
            continue
        if family == "openai" and message.get("role") == "tool":
            result.append((message.get("tool_call_id"), message.get("content")))
        elif family == "openai_responses" and message.get("type") == "function_call_output":
            result.append((message.get("call_id"), message.get("output")))
        elif family == "anthropic" and message.get("role") == "user" and isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    content = part.get("content")
                    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in content):
                        content = "".join(p.get("text", "") for p in content)
                    result.append((part.get("tool_use_id"), content))
    return [(identity, content) for identity, content in result if isinstance(identity, str) and isinstance(content, str)]


def source_fragments(request: LLMRequest | None, body: Mapping[str, object], family: str | None) -> tuple[list[dict], list[dict], bool]:
    """Match exact native slots, not arbitrary text that resembles a source."""
    if request is None or family not in {"openai", "openai_responses", "anthropic"}:
        return [], [], False
    calls = {}
    duplicate_calls = set()
    for message in request.messages:
        if message.role == "assistant":
            for call in message.tool_calls or []:
                if call.id in calls:
                    duplicate_calls.add(call.id)
                calls[call.id] = call
    wire = _wire_tool_results(body, family)
    wire_ids = Counter(identity for identity, _ in wire)
    logical_ids = Counter(message.tool_call_id for message in request.messages if message.role == "tool")
    receipts, fragments = [], []
    total = 0
    complete = True
    for index, message in enumerate(request.messages):
        if message.role != "tool":
            continue
        identity = message.tool_call_id
        call = calls.get(identity)
        if (
            call is None
            or identity in duplicate_calls
            or logical_ids[identity] != 1
            or wire_ids[identity] != 1
            or (identity, message.content) not in wire
        ):
            complete = False
            continue
        content = message.content.encode("utf-8")
        if len(receipts) >= 100 or total + len(content) > MAX_SOURCE_BYTES:
            complete = False
            continue
        total += len(content)
        receipts.append(
            {
                "kind": "tool_result",
                "tool_call_id": identity,
                "tool_name": call.name,
                "arguments_digest": canonical_transport_digest(call.arguments),
                "message_index": index,
                "content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
                "content_bytes": len(content),
                "association": "exact_outbound_slot",
                "resource_version": None,
            }
        )
        fragments.append({"tool_call_id": identity, "content": message.content})
    return receipts, fragments, complete


class PhysicalUsageObserver:
    def __init__(
        self,
        db_path: str,
        *,
        cipher: ReferenceCipher,
        turn_id: str,
        generation: int,
        plan_id: str | None = None,
        model_credit_rates: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    ):
        self.db_path = db_path
        self.cipher = cipher
        self.turn_id = turn_id
        self.generation = generation
        self.plan_id = plan_id
        self.model_credit_rates = model_credit_rates or {}
        self.failed = False

    async def begin(self, spec: PhysicalInvocationSpec, *, request: LLMRequest | None, body: Mapping[str, object], family: str | None):
        if self.failed:
            raise ValueError("physical_usage_observer_failed")
        ctx = current_usage_context()
        if ctx.turn_id != self.turn_id:
            raise ValueError("physical_usage_turn_changed")
        identity = "lu_" + uuid4().hex
        now = datetime.now(UTC)
        sources, _fragments, exact = source_fragments(request, body, family)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT t.execution_generation,s.id AS session_id FROM turns t JOIN sessions s ON s.id=t.session_id "
                    "JOIN workspaces w ON w.id=s.workspace_id WHERE t.id=? AND s.user_id=? AND s.workspace_id=? AND w.team_id=? AND t.execution_generation=?",
                    (ctx.turn_id, ctx.user_id, ctx.workspace_id, ctx.team_id, self.generation),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ValueError("physical_usage_scope_unavailable")
                source_turn_id = getattr(spec.input_receipt, "turn_id", None) or ctx.turn_id
                if self.plan_id is not None:
                    async with db.execute(
                        "SELECT spec_json FROM plans WHERE id=? AND session_id=? AND user_id=? AND workspace_id=?",
                        (self.plan_id, row["session_id"], ctx.user_id, ctx.workspace_id),
                    ) as cursor:
                        plan = await cursor.fetchone()
                    if plan is not None and plan["spec_json"]:
                        reference = json.loads(plan["spec_json"]).get("reference")
                        if isinstance(reference, dict) and isinstance(reference.get("source_turn_id"), str):
                            source_turn_id = reference["source_turn_id"]
                receipt = {
                    "contract": "physical-usage.v1",
                    "source_turn_id": source_turn_id,
                    "request_digest": spec.request_digest,
                    "adapter_contract_key": spec.adapter_contract_key,
                    "generation": row["execution_generation"],
                    "context_id": getattr(spec.input_receipt, "context_id", None),
                    "execution_id": getattr(spec.input_receipt, "execution_id", None) or (request.invocation_execution_id if request else None),
                    "subagent_id": request.invocation_subagent_id if request else None,
                    "sources": sources,
                    "source_coverage": "exact" if exact else "partial",
                }
                from bowhead_core.rate_card import ModelCreditRates

                receipt["credit_rates"] = ModelCreditRates.from_config(
                    dict(spec.credit_rates or self.model_credit_rates.get((spec.provider, spec.model), {}))
                    if spec.credit_rates is not None or (spec.provider, spec.model) in self.model_credit_rates
                    else None
                ).as_json()
                receipt["usage_family"] = spec.usage_family or family
                await db.execute(
                    "INSERT INTO llm_usage_events(id,team_id,user_id,workspace_id,turn_id,reason,agent_type,provider,model,streamed,created_at,"
                    "receipt_json,receipt_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                    (
                        identity,
                        ctx.team_id,
                        ctx.user_id,
                        ctx.workspace_id,
                        ctx.turn_id,
                        ctx.reason,
                        request.invocation_agent_type if request else "unknown",
                        spec.provider,
                        spec.model,
                        int(spec.mode == "stream"),
                        now.isoformat(),
                        _json(receipt),
                    ),
                )
                from bowhead_core.documents.continuation import current_document_call, link_physical_receipt_in_tx

                document_call = current_document_call()
                if document_call is not None:
                    await link_physical_receipt_in_tx(db, call=document_call, spec=spec, receipt_id=identity)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _UsageAttempt(self, identity)


class _UsageAttempt:
    def __init__(self, observer: PhysicalUsageObserver, identity: str):
        self.observer = observer
        self.identity = identity

    async def _settle(self, state: str, *, code: str | None = None, response_digest: str | None = None, usage: TokenUsage | None = None) -> bool:
        expected = (
            state,
            code,
            response_digest,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            usage.cached_tokens if usage else 0,
        )
        try:
            async with open_shared_ctx(self.observer.db_path) as db:
                await db.execute(
                    "UPDATE llm_usage_events SET receipt_state=?,outcome_code=?,response_digest=?,tokens_input=?,tokens_output=?,tokens_cached=?,settled_at=? "
                    "WHERE id=? AND receipt_state='pending'",
                    (*expected, datetime.now(UTC).isoformat(), self.identity),
                )
                await db.commit()
                async with db.execute(
                    "SELECT receipt_state,outcome_code,response_digest,tokens_input,tokens_output,tokens_cached FROM llm_usage_events WHERE id=?",
                    (self.identity,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or tuple(row) != expected:
                    raise ValueError("physical_usage_settlement_conflict")
        except BaseException:
            self.observer.failed = True
            raise
        return True

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        return await self._settle("not_applied", code=evidence_code, response_digest=evidence_digest)

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        return await self._settle("completed", usage=usage, response_digest=response_digest)

    async def unknown(self, *, reason_code: str) -> None:
        await self._settle(
            "usage_missing"
            if reason_code == "authoritative_usage_missing"
            else "not_applied"
            if reason_code == "authority_admission_failed"
            else "unknown",
            code=reason_code,
        )


async def turn_model_usage_lines(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    generation: int,
    projected_input: int,
    projected_output: int,
) -> list[ModelUsageLine]:
    """Read completed physical calls at the exact generation; retain an explicit residual."""
    async with db.execute(
        "SELECT provider,model,tokens_input,tokens_output,tokens_cached,receipt_json "
        "FROM llm_usage_events WHERE turn_id=? AND reason='turn' AND receipt_state='completed' "
        "AND json_extract(receipt_json,'$.generation')=? ORDER BY created_at,id",
        (turn_id, generation),
    ) as cursor:
        rows = await cursor.fetchall()
    lines: list[ModelUsageLine] = []
    observed_input = observed_output = 0
    for row in rows:
        receipt = json.loads(row[5])
        input_tokens, output_tokens, cached_tokens = int(row[2]), int(row[3]), int(row[4])
        if observed_input + input_tokens > projected_input or observed_output + output_tokens > projected_output:
            continue
        usage = ModelTokenUsage.from_provider_counts(
            input_tokens,
            cached_tokens,
            output_tokens,
            cached_in_input=receipt.get("usage_family") != "anthropic",
            rates=ModelCreditRates.from_config(receipt.get("credit_rates")),
        )
        lines.append(ModelUsageLine(row[0], row[1], usage))
        observed_input += input_tokens
        observed_output += output_tokens
    if observed_input < projected_input or observed_output < projected_output:
        lines.append(
            ModelUsageLine(
                None,
                None,
                ModelTokenUsage(max(0, projected_input - observed_input), 0, max(0, projected_output - observed_output)),
            )
        )
    return lines


async def read_turn_credit_usage(
    db_path: str,
    *,
    turn_id: str,
    generation: int,
    projected_input: int,
    projected_output: int,
) -> tuple[str, tuple[ModelUsageLine, ...]]:
    async with open_shared_ctx(db_path) as db:
        async with db.execute("SELECT billing_rate_card_version FROM turns WHERE id=?", (turn_id,)) as cursor:
            turn = await cursor.fetchone()
        if turn is None:
            raise ValueError("credit Turn is unavailable")
        version = turn[0] or "v1"
        if version == "v1":
            return version, ()
        lines = await turn_model_usage_lines(
            db,
            turn_id=turn_id,
            generation=generation,
            projected_input=projected_input,
            projected_output=projected_output,
        )
        return version, tuple(lines)
