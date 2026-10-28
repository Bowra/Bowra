"""Rebuild the exact team Tool handler scope for one claimed MCP reference.

It binds the immutable composition to the current source/ACL/Agent projection
and the actual per-turn ``ToolSpec`` handlers.  Selected handlers are wrapped
so an exact call is durable before the remote read and its encrypted result is
durable before release.  A saved composition is never current authorization.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_execution import ReferencePrimaryInput, read_reference_mcp_primary_in_connection
from bowhead_core.chat_reference_runtime_store import require_reference_worker_in_connection
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope, _row
from bowhead_core.chat_references import AgentResourceRef, McpSourceResourceRef, McpToolResourceRef, ReferenceNode
from bowhead_core.events import TerminalDurabilityAbort
from bowhead_core.reference_mcp_execution import (
    ReferenceMcpPrimaryComposition,
    _composition,
    restore_reference_mcp_composition_in_connection,
)
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_mcp_preparation import (
    PreparedReferenceMcpAdmission,
    PreparedReferenceMcpOperation,
    PreparedReferenceMcpSelection,
    revalidate_reference_mcp_selection_in_connection,
)
from bowhead_core.tool_registry import ToolRegistry, ToolSpec

_MAX_RESULT_BYTES = 1024 * 1024


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _prepared(
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    composition: ReferenceMcpPrimaryComposition,
) -> PreparedReferenceMcpSelection:
    document = primary.source.input_document
    if document is None:
        raise ValueError("reference_mcp_handler_scope_changed")
    receivers = [node.resource for node in document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)]
    if len(receivers) != 1 or receivers[0].source_kind != "team":
        raise ValueError("reference_mcp_handler_scope_changed")
    from bowhead_core.chat_reference_scope import ReferenceRequestScope

    return PreparedReferenceMcpSelection(
        scope=ReferenceRequestScope(**scope.identity()),
        receiver=receivers[0],
        policy_epoch=composition.mcp_policy_epoch,
        admissions=tuple(
            PreparedReferenceMcpAdmission(
                node_id=item.node_id,
                resource=item.resource,
                operation=item.operation,
                policy_digest=item.policy_digest,
                expanded_operation_names=item.expanded_operation_names,
            )
            for item in composition.mcp_admissions
        ),
        operations=tuple(
            PreparedReferenceMcpOperation(
                resource=item.resource,
                source_revision=item.source_revision,
                parameter_schema_json=item.parameter_schema_json,
                policy_digest=item.policy_digest,
            )
            for item in composition.mcp_operations
        ),
        selection_digest=composition.mcp_selection_digest,
    )


def _handler(spec: ToolSpec, *, team_id: str, operation) -> None:
    binding = spec.team_tool_binding
    annotations = spec.annotations
    if (
        spec.schema.name != operation.resource.operation_name
        or binding is None
        or binding.team_id != team_id
        or binding.source_id != operation.resource.source_id
        or binding.advertised_name != operation.resource.operation_name
        or spec.needs_approval
        or spec.risk_level != "low"
        or spec.source != "external"
        or annotations is None
        or annotations.read_only is not True
        or annotations.destructive is not False
        or _canonical_json(spec.schema.input_schema) != operation.parameter_schema_json
    ):
        raise ValueError("reference_mcp_handler_scope_changed")


def require_reference_mcp_handler_view(
    view: ToolRegistry,
    *,
    team_id: str,
    composition: ReferenceMcpPrimaryComposition,
) -> tuple[ToolSpec, ...]:
    """Return the selected real handlers after exact local contract checks."""
    if type(view) is not ToolRegistry or not composition.mcp_operations:
        raise ValueError("reference_mcp_handler_scope_changed")
    specs: list[ToolSpec] = []
    names: set[str] = set()
    for operation in composition.mcp_operations:
        name = operation.resource.operation_name
        if name in names or name not in view.overlay_names:
            raise ValueError("reference_mcp_handler_scope_changed")
        names.add(name)
        try:
            spec = view.get(name)
        except KeyError:
            raise ValueError("reference_mcp_handler_scope_changed") from None
        _handler(spec, team_id=team_id, operation=operation)
        specs.append(spec)
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class ReferenceMcpHandlerScope:
    """Current authority plus exact per-turn handlers for a claimed worker."""

    scope: ReferenceScope
    turn_id: str
    owner_id: str
    generation: int
    composition: ReferenceMcpPrimaryComposition
    handlers: tuple[ToolSpec, ...] = field(repr=False, compare=False)
    index: ReferenceMcpMetadataIndex = field(repr=False, compare=False)
    access: AccessControlStore = field(repr=False, compare=False)
    bindings: AgentExecutionBindingStore = field(repr=False, compare=False)

    @classmethod
    async def bind(
        cls,
        *,
        scope: ReferenceScope,
        turn_id: str,
        owner_id: str,
        generation: int,
        index: ReferenceMcpMetadataIndex,
        access: AccessControlStore,
        bindings: AgentExecutionBindingStore,
        view: ToolRegistry,
    ) -> ReferenceMcpHandlerScope:
        scope = ReferenceScope.model_validate(scope.model_dump(mode="python"))
        if (
            not owner_id
            or type(generation) is not int
            or generation < 1
            or Path(index.db_path).resolve() != Path(access.db_path).resolve()
            or Path(index.db_path).resolve() != Path(bindings.db_path).resolve()
        ):
            raise ValueError("reference_mcp_handler_scope_changed")
        async with open_shared_ctx(str(bindings.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                primary, composition = await require_reference_mcp_runtime_in_connection(
                    db,
                    scope=scope,
                    turn_id=turn_id,
                    owner_id=owner_id,
                    generation=generation,
                    index=index,
                    access=access,
                    bindings=bindings,
                )
                if primary.binding.status != "running":
                    raise ValueError("reference_mcp_handler_scope_changed")
                handlers = require_reference_mcp_handler_view(view, team_id=scope.team_id, composition=composition)
            finally:
                await db.rollback()
        return cls(scope, turn_id, owner_id, generation, composition, handlers, index, access, bindings)

    async def revalidate_in_connection(self, db: aiosqlite.Connection, *, view: ToolRegistry) -> ReferencePrimaryInput:
        """Recheck current authority and handler identity in the caller snapshot."""
        if not db.in_transaction:
            raise ValueError("reference_transaction_required")
        primary, composition = await require_reference_mcp_runtime_in_connection(
            db,
            scope=self.scope,
            turn_id=self.turn_id,
            owner_id=self.owner_id,
            generation=self.generation,
            index=self.index,
            access=self.access,
            bindings=self.bindings,
        )
        if primary.binding.status != "running" or composition != self.composition:
            raise ValueError("reference_mcp_handler_scope_changed")
        handlers = require_reference_mcp_handler_view(view, team_id=self.scope.team_id, composition=composition)
        if tuple(spec.team_tool_binding for spec in handlers) != tuple(spec.team_tool_binding for spec in self.handlers):
            raise ValueError("reference_mcp_handler_scope_changed")
        return primary

    async def require_current(self, *, view: ToolRegistry) -> ReferencePrimaryInput:
        async with open_shared_ctx(str(self.bindings.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                return await self.revalidate_in_connection(db, view=view)
            finally:
                await db.rollback()

    def wrap_view(self, view: ToolRegistry, *, cipher: ReferenceCipher, resumed_call_ids: tuple[str, ...] = ()) -> ToolRegistry:
        """Wrap only selected handlers with durable call/result bookkeeping."""
        handlers = require_reference_mcp_handler_view(view, team_id=self.scope.team_id, composition=self.composition)
        wrapped = view
        for original in handlers:

            async def handler(
                arguments: dict[str, Any],
                ctx: Any,
                *,
                _original: ToolSpec = original,
            ) -> dict[str, Any]:
                if any(getattr(ctx, key, None) != value for key, value in {**self.scope.identity(), "turn_id": self.turn_id}.items()):
                    return _denial("reference_mcp_scope_changed")
                started = await self._begin_call(view=view, spec=_original, arguments=arguments, cipher=cipher, resumed_call_ids=resumed_call_ids)
                if started.result is not None:
                    self._record_read(ctx, started, _original, arguments, started.result)
                    return started.result
                try:
                    result = await _original.handler(arguments, ctx)
                except asyncio.CancelledError:
                    await asyncio.shield(self._interrupt_call(started, "handler_cancelled"))
                    raise
                except Exception:
                    await self._interrupt_call(started, "handler_failed")
                    raise
                await self._complete_call(started, result=result, cipher=cipher)
                try:
                    await self.require_current(view=view)
                except Exception:
                    return _denial("reference_mcp_result_withheld")
                self._record_read(ctx, started, _original, arguments, result)
                return result

            wrapped = wrapped.clone_with_spec_override(replace(original, handler=handler))
        return wrapped

    @staticmethod
    def _record_read(ctx, call, spec, arguments, result) -> None:
        receipts = getattr(ctx, "reference_mcp_read_receipts", None)
        if receipts is not None and call.call_id and isinstance(result, dict) and result.get("ok") is True:
            receipts.append(
                {
                    "call_id": call.call_id,
                    "operation_name": spec.schema.name,
                    "parameter_digest": canonical_fingerprint(arguments),
                    "result_hash": canonical_fingerprint(result),
                }
            )

    async def _begin_call(
        self,
        *,
        view: ToolRegistry,
        spec: ToolSpec,
        arguments: dict[str, Any],
        cipher: ReferenceCipher,
        resumed_call_ids: tuple[str, ...] = (),
    ) -> ReferenceMcpCall:
        if type(arguments) is not dict:
            return ReferenceMcpCall.denied(_denial("reference_mcp_parameters_invalid"))
        operation = next(
            (item for item in self.composition.mcp_operations if item.resource.operation_name == spec.schema.name),
            None,
        )
        if operation is None:
            return ReferenceMcpCall.denied(_denial("reference_mcp_handler_scope_changed"))
        try:
            schema = json.loads(operation.parameter_schema_json)
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(arguments)
        except (TypeError, ValueError, json.JSONDecodeError, SchemaError, ValidationError):
            return ReferenceMcpCall.denied(_denial("reference_mcp_parameters_invalid"))
        parameter_digest = canonical_fingerprint(arguments)
        async with open_shared_ctx(str(self.bindings.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                primary = await self.revalidate_in_connection(db, view=view)
                node_id, required = _call_node(primary, self.composition, operation.resource, parameter_digest)
                if node_id is None:
                    await db.rollback()
                    return ReferenceMcpCall.denied(_denial("reference_mcp_parameters_changed"))
                if not required and resumed_call_ids:
                    prior = await db.execute_fetchall(
                        "SELECT * FROM chat_reference_mcp_calls WHERE turn_id=? AND execution_id=? AND operation_name=? AND parameter_digest=?",
                        (self.turn_id, primary.binding.execution_id, spec.schema.name, parameter_digest),
                    )
                    existing = next((row for row in prior if row["call_id"] in resumed_call_ids), None)
                    if existing is not None:
                        result = _saved_result(existing, scope=self.scope, cipher=cipher)
                        await db.rollback()
                        return ReferenceMcpCall(call_id=existing["call_id"], execution_id=existing["execution_id"], result=result)
                if required:
                    existing = await _row(
                        db,
                        "SELECT * FROM chat_reference_mcp_calls WHERE turn_id=? AND node_id=? AND required_tool=1",
                        (self.turn_id, node_id),
                    )
                    if existing is not None:
                        result = _saved_result(existing, scope=self.scope, cipher=cipher)
                        await db.rollback()
                        return ReferenceMcpCall(call_id=existing["call_id"], execution_id=existing["execution_id"], result=result)
                row = await _row(
                    db,
                    "SELECT COALESCE(MAX(call_seq),0)+1 AS next_seq FROM chat_reference_mcp_calls WHERE turn_id=?",
                    (self.turn_id,),
                )
                call_seq = int(row["next_seq"])
                call_id = (
                    "mcp_" + hashlib.sha256(f"{self.turn_id}/{primary.binding.execution_id}/{self.generation}/{call_seq}".encode()).hexdigest()[:24]
                )
                now = datetime.now(UTC).isoformat()
                deadline = await _row(db, "SELECT expires_at FROM chat_reference_plans WHERE turn_id=?", (self.turn_id,))
                await db.execute(
                    "INSERT INTO chat_reference_mcp_calls(turn_id,call_id,call_seq,schema_version,resolution_seq,execution_id,"
                    "created_generation,owner_id,node_id,required_tool,source_id,operation_name,local_contract_revision,"
                    "connection_binding_id,connection_subject_fingerprint,operation_contract_revision,parameter_digest,status,"
                    "result_ok,result_hash,result_ciphertext,failure_code,retention_state,started_at,completed_at,expires_at) "
                    "VALUES (?,?,?,1,1,?,?,?, ?,?,?,?,?,?,?,?,?, 'started',NULL,NULL,NULL,NULL,'available',?,NULL,?)",
                    (
                        self.turn_id,
                        call_id,
                        call_seq,
                        primary.binding.execution_id,
                        self.generation,
                        self.owner_id,
                        node_id,
                        int(required),
                        operation.resource.source_id,
                        operation.resource.operation_name,
                        operation.resource.local_contract_revision,
                        operation.resource.connection_binding_id,
                        operation.resource.connection_subject_fingerprint,
                        operation.resource.operation_contract_revision,
                        parameter_digest,
                        now,
                        deadline["expires_at"],
                    ),
                )
                await db.commit()
                return ReferenceMcpCall(
                    call_id=call_id,
                    execution_id=primary.binding.execution_id,
                )
            except BaseException:
                await db.rollback()
                raise

    async def _complete_call(self, call: ReferenceMcpCall, *, result: dict[str, Any], cipher: ReferenceCipher) -> None:
        try:
            content = _result_bytes(result)
            result_hash = "sha256:" + hashlib.sha256(content).hexdigest()
            ciphertext = cipher.seal(
                scope=self.scope,
                purpose="reference_mcp_result",
                identity=f"{self.turn_id}/{call.call_id}",
                content=content,
            )
            ok = result.get("ok") is True
            async with open_shared_ctx(str(self.bindings.db_path)) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE chat_reference_mcp_calls SET status=?,result_ok=?,result_hash=?,result_ciphertext=?,failure_code=?,completed_at=? "
                    "WHERE turn_id=? AND call_id=? AND execution_id=? AND owner_id=? AND status='started' AND retention_state='available'",
                    (
                        "completed" if ok else "failed",
                        int(ok),
                        result_hash,
                        ciphertext,
                        None if ok else _failure_code(result),
                        datetime.now(UTC).isoformat(),
                        self.turn_id,
                        call.call_id,
                        call.execution_id,
                        self.owner_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("reference_mcp_call_fence_lost")
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TerminalDurabilityAbort("reference_mcp_result_not_durable") from exc

    async def _interrupt_call(self, call: ReferenceMcpCall, failure_code: str) -> None:
        if not call.call_id:
            return
        try:
            async with open_shared_ctx(str(self.bindings.db_path)) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE chat_reference_mcp_calls SET status='interrupted',failure_code=?,completed_at=? "
                    "WHERE turn_id=? AND call_id=? AND execution_id=? AND owner_id=? AND status='started' AND retention_state='available'",
                    (
                        failure_code,
                        datetime.now(UTC).isoformat(),
                        self.turn_id,
                        call.call_id,
                        call.execution_id,
                        self.owner_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("reference_mcp_call_fence_lost")
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TerminalDurabilityAbort("reference_mcp_result_not_durable") from exc

    async def require_explicit_tools_completed(self, *, resumed_call_ids: tuple[str, ...] = ()) -> None:
        """Require one durable successful result for every explicit Tool node."""
        required = {item.node_id for item in self.composition.mcp_admissions if isinstance(item.resource, McpToolResourceRef)}
        if not required:
            return
        async with open_shared_ctx(str(self.bindings.db_path)) as db:
            await db.execute("PRAGMA query_only=ON")
            async with db.execute(
                "SELECT node_id,call_id,created_generation FROM chat_reference_mcp_calls WHERE turn_id=? AND execution_id=? "
                "AND required_tool=1 AND status='completed' AND result_ok=1 AND retention_state='available'",
                (self.turn_id, self.composition.execution_id),
            ) as cursor:
                completed = {str(row[0]) for row in await cursor.fetchall() if row[2] == self.generation or row[1] in resumed_call_ids}
        if completed != required:
            raise ValueError("reference_mcp_required_tool_incomplete")


async def require_saved_mcp_reads(db: aiosqlite.Connection, *, checkpoint, scope: ReferenceScope, cipher: ReferenceCipher) -> None:
    """Reopen only the exact successful receipts in this sealed continuation."""
    if not db.in_transaction:
        raise ValueError("reference_transaction_required")
    for receipt in checkpoint.mcp_receipts():
        row = await _row(
            db,
            "SELECT * FROM chat_reference_mcp_calls WHERE turn_id=? AND execution_id=? AND call_id=? "
            "AND created_generation=1 AND status='completed' AND result_ok=1 AND retention_state='available' "
            "AND julianday(expires_at)>julianday('now')",
            (checkpoint.turn_id, checkpoint.execution_id, receipt["call_id"]),
        )
        if row is None or any(row[key] != value for key, value in receipt.items()):
            raise ValueError("reference_continuation_changed")
        if _saved_result(row, scope=scope, cipher=cipher).get("ok") is not True:
            raise ValueError("reference_continuation_changed")


@dataclass(frozen=True, slots=True)
class ReferenceMcpCall:
    call_id: str = ""
    execution_id: str = ""
    result: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def denied(cls, result: dict[str, Any]) -> ReferenceMcpCall:
        return cls(result=result)


def _call_node(primary, composition, resource, parameter_digest: str) -> tuple[str | None, bool]:
    document = primary.source.input_document
    if document is None:
        return None, False
    admissions = {item.node_id: item for item in composition.mcp_admissions}
    source_node: str | None = None
    for node in document.nodes:
        if not isinstance(node, ReferenceNode):
            continue
        admission = admissions.get(node.node_id)
        if admission is None or resource.operation_name not in admission.expanded_operation_names:
            continue
        if isinstance(node.resource, McpToolResourceRef) and node.resource == resource:
            if canonical_fingerprint(node.parameters) == parameter_digest:
                return node.node_id, True
        elif isinstance(node.resource, McpSourceResourceRef) and node.resource.source_id == resource.source_id:
            source_node = node.node_id
    return source_node, False


def _denial(code: str) -> dict[str, Any]:
    return {"ok": False, "error_kind": code, "error": "The selected MCP reference could not be used for this task."}


def _failure_code(result: dict[str, Any]) -> str:
    value = str(result.get("error_kind") or "tool_error")
    return value[:128] if value else "tool_error"


def _result_bytes(result: dict[str, Any]) -> bytes:
    if type(result) is not dict:
        raise ValueError("reference_mcp_result_invalid")
    content = _canonical_json(result).encode()
    if len(content) > _MAX_RESULT_BYTES:
        raise ValueError("reference_mcp_result_limit")
    return content


def _saved_result(row: aiosqlite.Row, *, scope: ReferenceScope, cipher: ReferenceCipher) -> dict[str, Any]:
    if row["status"] not in {"completed", "failed"} or row["retention_state"] != "available" or row["result_ciphertext"] is None:
        return _denial("reference_mcp_call_unavailable")
    try:
        content = cipher.open(
            scope=scope,
            purpose="reference_mcp_result",
            identity=f"{row['turn_id']}/{row['call_id']}",
            ciphertext=str(row["result_ciphertext"]),
        )
        if "sha256:" + hashlib.sha256(content).hexdigest() != row["result_hash"]:
            raise ValueError("result hash mismatch")
        value = json.loads(content)
        if type(value) is not dict:
            raise ValueError("result shape mismatch")
        return value
    except Exception:
        raise TerminalDurabilityAbort("reference_mcp_result_not_durable") from None


async def require_reference_mcp_runtime_in_connection(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    generation: int,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
    bindings: AgentExecutionBindingStore,
    read_output: bool = False,
) -> tuple[ReferencePrimaryInput, ReferenceMcpPrimaryComposition]:
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_mcp_calls'", ()) is None:
        raise ValueError("reference_mcp_call_migration_required")
    primary = await read_reference_mcp_primary_in_connection(db, scope=scope, turn_id=turn_id, bindings=bindings, read_output=read_output)
    lead = primary.plan.orchestration_contract == "lead-delegation.v1"
    if primary.binding.status != "running" and not (lead and (primary.binding.status == "admitted" or read_output)):
        raise ValueError("reference_mcp_handler_scope_changed")
    composition = await restore_reference_mcp_composition_in_connection(
        db,
        primary=primary,
        scope=scope,
        generation=generation,
    )
    prepared = _prepared(primary, scope, composition)
    await revalidate_reference_mcp_selection_in_connection(
        db,
        db_path=bindings.db_path,
        prepared=prepared,
        input_document=primary.source.input_document,
        index=index,
        access=access,
    )
    if _composition(primary, prepared) != composition:
        raise ValueError("reference_mcp_handler_scope_changed")
    await require_reference_worker_in_connection(
        db,
        scope=scope,
        turn_id=turn_id,
        owner_id=owner_id,
        generation=generation,
    )
    return primary, composition


__all__ = [
    "ReferenceMcpHandlerScope",
    "require_reference_mcp_handler_view",
    "require_reference_mcp_runtime_in_connection",
]
