"""Retain exact MCP read scope while claiming one initial Team Agent worker.

The claim path remains network-free.  It fixes the local operation contracts,
connection subject and current user authority before ownership can commit; no
remote request or credential lookup happens in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_execution import (
    ReferencePrimaryInput,
    ReferencePrimaryWorkerClaim,
    _claim_reference_primary_for_worker,
    read_reference_mcp_primary_in_connection,
)
from bowhead_core.chat_reference_runtime_store import (
    ReferencePrimaryComposition,
    _primary_baseline_composition,
    _put_primary_composition_in_connection,
    require_reference_worker_in_connection,
)
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES, ReferenceScope, _row, _transaction
from bowhead_core.chat_references import (
    AgentResourceRef,
    ContentHash,
    McpSourceResourceRef,
    McpToolResourceRef,
    NodeId,
    ReferenceNode,
    requested_reference_operation,
)
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_mcp_preparation import (
    PreparedReferenceMcpSelection,
    prepare_reference_mcp_selection,
    revalidate_reference_mcp_selection_in_connection,
)


class ReferenceMcpAdmissionComposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: NodeId
    resource: McpSourceResourceRef | McpToolResourceRef
    operation: Literal["provide", "invoke"]
    policy_digest: ContentHash
    expanded_operation_names: tuple[str, ...]


class ReferenceMcpOperationComposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    resource: McpToolResourceRef
    source_revision: str
    parameter_schema_json: str
    policy_digest: ContentHash


class ReferenceMcpPrimaryComposition(ReferencePrimaryComposition):
    runtime_policy: Literal["initial-primary-mcp-read.v1"] = "initial-primary-mcp-read.v1"
    mcp_policy_epoch: int
    mcp_selection_digest: ContentHash
    mcp_admissions: tuple[ReferenceMcpAdmissionComposition, ...]
    mcp_operations: tuple[ReferenceMcpOperationComposition, ...]


def _composition(primary: ReferencePrimaryInput, prepared: PreparedReferenceMcpSelection) -> ReferenceMcpPrimaryComposition:
    document = primary.source.input_document
    if document is None or primary.automatic_receiver is not None or prepared.receiver.source_kind != "team":
        raise ValueError("reference_mcp_composition_unavailable")
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in nodes if isinstance(node.resource, AgentResourceRef)]
    mcp_nodes = [node for node in nodes if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)]
    if (
        len(agents) != 1
        or agents[0].resource != prepared.receiver
        or agents[0].parameters
        or not mcp_nodes
        or len(mcp_nodes) != len(nodes) - 1
        or len(prepared.admissions) != len(mcp_nodes)
        or len(primary.plan.admissions) != len(nodes)
    ):
        raise ValueError("reference_mcp_composition_unavailable")
    prepared_by_node = {item.node_id: item for item in prepared.admissions}
    if len(prepared_by_node) != len(prepared.admissions):
        raise ValueError("reference_mcp_composition_unavailable")
    for node, admitted in zip(nodes, primary.plan.admissions, strict=True):
        if (admitted.node_id, admitted.resource, admitted.operation) != (node.node_id, node.resource, requested_reference_operation(node.resource)):
            raise ValueError("reference_mcp_composition_unavailable")
        if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef):
            item = prepared_by_node.get(node.node_id)
            if item is None or (item.resource, item.operation, item.policy_digest) != (
                admitted.resource,
                admitted.operation,
                admitted.policy_digest,
            ):
                raise ValueError("reference_mcp_composition_unavailable")
    base = _primary_baseline_composition(primary, agents[0])
    value = base.model_dump(mode="python")
    value.update(
        runtime_policy="initial-primary-mcp-read.v1",
        mcp_policy_epoch=prepared.policy_epoch,
        mcp_selection_digest=prepared.selection_digest,
        mcp_admissions=tuple(
            ReferenceMcpAdmissionComposition(
                node_id=item.node_id,
                resource=item.resource,
                operation=item.operation,
                policy_digest=item.policy_digest,
                expanded_operation_names=item.expanded_operation_names,
            )
            for item in prepared.admissions
        ),
        mcp_operations=tuple(
            ReferenceMcpOperationComposition(
                resource=item.resource,
                source_revision=item.source_revision,
                parameter_schema_json=item.parameter_schema_json,
                policy_digest=item.policy_digest,
            )
            for item in prepared.operations
        ),
    )
    composition = ReferenceMcpPrimaryComposition.model_validate(value)
    if len(composition.canonical_json().encode()) > MAX_REFERENCE_STORAGE_BYTES:
        raise ValueError("reference_composition_material_limit")
    return composition


async def restore_reference_mcp_composition_in_connection(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    generation: int,
) -> ReferenceMcpPrimaryComposition:
    """Restore the immutable local MCP scope without granting current use."""
    _transaction(db)
    row = await _row(db, "SELECT * FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (primary.source.turn_id,))
    if row is None:
        raise ValueError("reference_mcp_composition_unavailable")
    try:
        saved = ReferenceMcpPrimaryComposition.model_validate_json(row["composition_json"], strict=True)
        base = _primary_baseline_composition(
            primary,
            next(
                node
                for node in primary.source.input_document.nodes
                if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
            ),
        )
        expected_base = base.model_dump(mode="python")
        expected_base["runtime_policy"] = "initial-primary-mcp-read.v1"
        if (
            (generation != 1 and primary.plan.orchestration_contract != "lead-delegation.v1")
            or row["created_generation"] != 1
            or row["execution_id"] != primary.binding.execution_id
            or row["source_turn_id"] != primary.source.turn_id
            or row["kind"] != "composition"
            or row["schema_version"] != 1
            or row["idempotency_key"] != "reference:primary"
            or saved.model_dump(mode="python", include=set(ReferencePrimaryComposition.model_fields)) != expected_base
            or row["composition_json"] != saved.canonical_json()
            or row["composition_digest"] != saved.digest
            or saved.mcp_policy_epoch != scope.policy_epoch
        ):
            raise ValueError("changed composition")
        plan_mcp = [item for item in primary.plan.admissions if isinstance(item.resource, McpSourceResourceRef | McpToolResourceRef)]
        if [(item.node_id, item.resource, item.operation, item.policy_digest) for item in plan_mcp] != [
            (item.node_id, item.resource, item.operation, item.policy_digest) for item in saved.mcp_admissions
        ]:
            raise ValueError("changed composition")
        return saved
    except Exception:
        raise ValueError("reference_mcp_composition_changed") from None


@dataclass(frozen=True, slots=True)
class McpReferenceWorkerPreparation:
    index: ReferenceMcpMetadataIndex = field(repr=False)
    access: AccessControlStore = field(repr=False)
    prepared: PreparedReferenceMcpSelection | None = field(repr=False)

    async def require_existing(self, db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, scope: ReferenceScope, generation: int) -> None:
        await restore_reference_mcp_composition_in_connection(db, primary=primary, scope=scope, generation=generation)

    async def revalidate(self, db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, scope: ReferenceScope) -> None:
        if self.prepared is None or primary.binding.status != "admitted":
            raise ValueError("reference_mcp_receiver_changed")
        expected_scope = ReferenceRequestScope(**scope.identity())
        if self.prepared.scope != expected_scope or self.prepared.policy_epoch != scope.policy_epoch:
            raise ValueError("reference_mcp_source_scope_changed")
        if Path(self.index.db_path).resolve() != Path(self.access.db_path).resolve():
            raise ValueError("reference_mcp_source_scope_changed")
        if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_mcp_composition'", ()) is None:
            raise ValueError("reference_mcp_composition_migration_required")
        await revalidate_reference_mcp_selection_in_connection(
            db,
            db_path=self.index.db_path,
            prepared=self.prepared,
            input_document=primary.source.input_document,
            index=self.index,
            access=self.access,
        )
        _composition(primary, self.prepared)

    async def persist(
        self,
        db: aiosqlite.Connection,
        *,
        primary: ReferencePrimaryInput,
        scope: ReferenceScope,
        generation: int,
        owner_id: str,
    ) -> None:
        if self.prepared is None:
            raise ValueError("reference_mcp_receiver_changed")
        composition = _composition(primary, self.prepared)
        await _put_primary_composition_in_connection(db, composition=composition, generation=generation)
        await require_reference_worker_in_connection(
            db,
            scope=scope,
            turn_id=primary.source.turn_id,
            owner_id=owner_id,
            generation=generation,
        )


async def claim_reference_mcp_primary_for_worker(
    *,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
    scope: ReferenceRequestScope,
    turn_id: str,
    owner_id: str,
    expected_generation: int,
    lease_seconds: int = 30,
) -> ReferencePrimaryWorkerClaim:
    """Prepare current MCP scope outside the lock, then claim it atomically."""
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    if Path(index.db_path).resolve() != Path(access.db_path).resolve():
        raise ValueError("reference_mcp_source_scope_changed")
    bindings = AgentExecutionBindingStore(index.db_path)
    async with open_shared_ctx(index.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            epoch = await require_reference_request_scope(db, scope)
            stored_scope = ReferenceScope(**scope.model_dump(), policy_epoch=epoch)
            primary = await read_reference_mcp_primary_in_connection(
                db,
                scope=stored_scope,
                turn_id=turn_id,
                bindings=bindings,
            )
        finally:
            await db.rollback()
    prepared = None
    if primary.binding.status == "admitted":
        receiver = next(
            node.resource
            for node in primary.source.input_document.nodes
            if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
        )
        prepared = await prepare_reference_mcp_selection(
            index.db_path,
            input_document=primary.source.input_document,
            scope=scope,
            receiver=receiver,
            index=index,
            access=access,
        )
    return await _claim_reference_primary_for_worker(
        scope=stored_scope,
        turn_id=turn_id,
        owner_id=owner_id,
        expected_generation=expected_generation,
        bindings=bindings,
        lease_seconds=lease_seconds,
        mcp=McpReferenceWorkerPreparation(index, access, prepared),
    )


__all__ = [
    "ReferenceMcpPrimaryComposition",
    "claim_reference_mcp_primary_for_worker",
    "restore_reference_mcp_composition_in_connection",
]
