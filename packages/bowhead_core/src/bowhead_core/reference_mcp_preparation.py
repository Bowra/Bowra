"""Prepare exact, network-free MCP read selections for initial references.

This module proves only the local contract, current connection subject, current
user access and one exact Team Agent baseline.  It never connects to a remote
server, reads credentials, admits a Turn or grants a handler permission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore, resource_grants_enforce_enabled
from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection
from bowhead_core.chat_reference_storage import ReferenceAdmission
from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    InputDocument,
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
    NodeId,
    ReferenceNode,
    SkillResourceRef,
)
from bowhead_core.reference_mcp_metadata import (
    ReferenceMcpMetadataIndex,
    ReferenceMcpOperationMetadata,
    ReferenceMcpPromptMetadata,
    ReferenceMcpSourceMetadata,
)
from bowhead_core.reference_receiver_metadata import ReferenceTeamToolBaseline, read_reference_team_tools_in_connection


class ReferenceMcpPreparationError(ValueError):
    """Finite local preparation failure without remote or credential details."""


@dataclass(frozen=True, slots=True)
class PreparedReferenceMcpOperation:
    resource: McpToolResourceRef
    source_revision: str
    parameter_schema_json: str
    policy_digest: str


@dataclass(frozen=True, slots=True)
class PreparedReferenceMcpAdmission:
    node_id: NodeId
    resource: McpSourceResourceRef | McpToolResourceRef
    operation: Literal["provide", "invoke"]
    policy_digest: str
    expanded_operation_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedReferenceMcpSelection:
    scope: ReferenceRequestScope
    receiver: AgentResourceRef
    policy_epoch: int
    admissions: tuple[PreparedReferenceMcpAdmission, ...]
    operations: tuple[PreparedReferenceMcpOperation, ...]
    selection_digest: str


@dataclass(frozen=True, slots=True)
class PreparedReferenceMcpPrompt:
    node_id: NodeId
    resource: McpPromptResourceRef
    source_revision: str
    parameter_schema_json: str
    policy_digest: str


@dataclass(frozen=True, slots=True)
class PreparedReferenceMcpPromptSelection:
    scope: ReferenceRequestScope
    receiver: AgentResourceRef
    policy_epoch: int
    prompts: tuple[PreparedReferenceMcpPrompt, ...]
    selection_digest: str


def _operation_contract(operation: ReferenceMcpOperationMetadata) -> dict:
    schema = operation.parameter_schema()
    if schema is None or operation.resource is None:
        raise ReferenceMcpPreparationError("reference_mcp_contract_unavailable")
    return {
        "resource": operation.resource.model_dump(mode="json"),
        "source_revision": operation.source_revision,
        "parameter_schema": schema,
        "effect": "read",
    }


def _validate_parameters(operation: ReferenceMcpOperationMetadata, parameters: dict) -> str:
    contract = _operation_contract(operation)
    try:
        Draft202012Validator(contract["parameter_schema"], format_checker=FormatChecker()).validate(parameters)
    except (SchemaError, ValidationError):
        raise ReferenceMcpPreparationError("reference_mcp_parameters_invalid") from None
    return canonical_fingerprint(parameters)


def _validate_prompt_parameters(prompt: ReferenceMcpPromptMetadata, parameters: dict) -> tuple[str, str]:
    schema = prompt.parameter_schema()
    if schema is None or prompt.resource is None or prompt.parameter_schema_json is None:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_contract_unavailable")
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(parameters)
    except (SchemaError, ValidationError):
        raise ReferenceMcpPreparationError("reference_mcp_prompt_parameters_invalid") from None
    return prompt.parameter_schema_json, canonical_fingerprint(parameters)


def reference_mcp_prompt_policy_digest(
    *,
    receiver: AgentResourceRef,
    resource: McpPromptResourceRef,
    source_revision: str,
    parameter_schema_json: str,
    parameters: dict,
) -> str:
    """Rebuild the per-Prompt admission proof from canonical source inputs."""
    try:
        schema = json.loads(parameter_schema_json)
        if not isinstance(schema, dict) or not isinstance(source_revision, str) or not source_revision:
            raise ValueError
        return canonical_fingerprint(
            {
                "contract": "initial-mcp-prompt-admission.v1",
                "receiver": receiver.model_dump(mode="json"),
                "resource": resource.model_dump(mode="json"),
                "source_revision": source_revision,
                "parameter_schema": schema,
                "parameters": canonical_fingerprint(parameters),
            }
        )
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_contract_unavailable") from None


def reference_mcp_prompt_selection_digest(
    *,
    scope: ReferenceRequestScope,
    receiver: AgentResourceRef,
    policy_epoch: int,
    prompts: tuple[tuple[NodeId, McpPromptResourceRef, str, str, str], ...],
) -> str:
    """Rebuild the ordered Prompt selection proof without current authority reads."""
    try:
        if type(policy_epoch) is not int or policy_epoch < 0:
            raise ValueError
        values = []
        for node_id, resource, source_revision, parameter_schema_json, policy_digest in prompts:
            schema = json.loads(parameter_schema_json)
            if not isinstance(schema, dict) or not source_revision:
                raise ValueError
            values.append(
                {
                    "node_id": node_id,
                    "resource": resource.model_dump(mode="json"),
                    "source_revision": source_revision,
                    "parameter_schema": schema,
                    "policy_digest": policy_digest,
                }
            )
        return canonical_fingerprint(
            {
                "contract": "initial-mcp-prompt-selection.v1",
                "scope": scope.model_dump(mode="json"),
                "receiver": receiver.model_dump(mode="json"),
                "policy_epoch": policy_epoch,
                "prompts": values,
            }
        )
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_contract_unavailable") from None


def _baseline(
    baselines: tuple[ReferenceTeamToolBaseline, ...],
    operation: ReferenceMcpOperationMetadata,
) -> ReferenceTeamToolBaseline | None:
    return next(
        (
            item
            for item in baselines
            if item.source_id == operation.source_id
            and item.operation_name == operation.id
            and item.source_revision == operation.source_revision
            and item.risk == "read"
            and not item.approval_required
        ),
        None,
    )


async def _allowed(
    db: aiosqlite.Connection,
    *,
    access: AccessControlStore,
    context,
    source_id: str,
    operation_name: str | None = None,
) -> bool:
    source = await access.explain_decision(context, "team_tool_source", source_id, "use", connection=db)
    if resource_grants_enforce_enabled() and not source.get("allowed"):
        return False
    if operation_name is None:
        return True
    operation = await access.explain_decision(context, "team_tool_op", operation_name, "use", connection=db)
    return bool(operation.get("allowed") or not resource_grants_enforce_enabled())


def _metadata_sources(index: ReferenceMcpMetadataIndex, *, db_path: Path, team_id: str) -> dict[str, ReferenceMcpSourceMetadata]:
    if Path(index.db_path).resolve() != db_path.resolve() or not index.ready or team_id in index.incomplete_teams:
        raise ReferenceMcpPreparationError("reference_mcp_metadata_unavailable")
    values = {source.source_id: source for source in index.for_team(team_id)}
    if len(values) != len(index.for_team(team_id)):
        raise ReferenceMcpPreparationError("reference_mcp_metadata_unavailable")
    return values


async def _prepare_in_connection(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
    input_document: InputDocument,
    scope: ReferenceRequestScope,
    receiver: AgentResourceRef,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> PreparedReferenceMcpSelection:
    if not db.in_transaction:
        raise ReferenceMcpPreparationError("reference_transaction_required")
    if Path(access.db_path).resolve() != db_path.resolve():
        raise ReferenceMcpPreparationError("reference_mcp_access_unavailable")
    document = InputDocument.model_validate(input_document.model_dump(mode="python", warnings="error"))
    # Task content is validated with typed attachments by admission and the
    # canonical source reader; selection preparation has no attachment context.
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    receiver = AgentResourceRef.model_validate(receiver.model_dump(mode="python", warnings="error"))
    refs = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in refs if isinstance(node.resource, AgentResourceRef)]
    mcp_nodes = [node for node in refs if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)]
    if receiver.source_kind != "team" or len(agents) != 1 or agents[0].resource != receiver or agents[0].parameters or not mcp_nodes:
        raise ReferenceMcpPreparationError("reference_mcp_selection_unsupported")
    if any(
        not isinstance(
            node.resource,
            AgentResourceRef | SkillResourceRef | CapabilityResourceRef | McpSourceResourceRef | McpToolResourceRef,
        )
        for node in refs
    ):
        raise ReferenceMcpPreparationError("reference_mcp_selection_unsupported")

    sources = _metadata_sources(index, db_path=db_path, team_id=scope.team_id)
    async with db.execute(
        "SELECT id,status,updated_at,visibility,created_by FROM team_tool_source WHERE team_id=?",
        (scope.team_id,),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        current = {str(row["id"]): row for row in await cursor.fetchall()}
    if {source_id for source_id, row in current.items() if row["status"] == "enabled"} != set(sources):
        raise ReferenceMcpPreparationError("reference_mcp_metadata_changed")

    baselines = await read_reference_team_tools_in_connection(db, team_id=scope.team_id, resource=receiver)
    if baselines is None:
        raise ReferenceMcpPreparationError("reference_mcp_receiver_changed")
    context = await read_reference_access_context_in_connection(db, scope=scope, access_store=access)
    prepared_operations: dict[tuple[str, str], PreparedReferenceMcpOperation] = {}
    admissions: list[PreparedReferenceMcpAdmission] = []

    async def exact_source(source_id: str) -> ReferenceMcpSourceMetadata:
        metadata = sources.get(source_id)
        row = current.get(source_id)
        if (
            metadata is None
            or metadata.resource is None
            or row is None
            or row["status"] != "enabled"
            or metadata.state_version != str(row["updated_at"])
            or metadata.visibility != str(row["visibility"])
            or metadata.created_by != (str(row["created_by"]) if row["created_by"] is not None else None)
            or metadata.visibility == "creator_only"
            and metadata.created_by != scope.user_id
            or not await _allowed(db, access=access, context=context, source_id=source_id)
        ):
            raise ReferenceMcpPreparationError("reference_mcp_source_unavailable")
        return metadata

    async def exact_operation(source: ReferenceMcpSourceMetadata, operation_name: str) -> ReferenceMcpOperationMetadata:
        matches = [item for item in source.operations if item.id == operation_name]
        if len(matches) != 1:
            raise ReferenceMcpPreparationError("reference_mcp_contract_unavailable")
        operation = matches[0]
        if (
            not operation.reviewed_read_contract
            or operation.resource is None
            or _baseline(baselines, operation) is None
            or not await _allowed(
                db,
                access=access,
                context=context,
                source_id=source.source_id,
                operation_name=operation.id,
            )
        ):
            raise ReferenceMcpPreparationError("reference_mcp_contract_unavailable")
        return operation

    for node in mcp_nodes:
        source = await exact_source(node.resource.source_id)
        if isinstance(node.resource, McpToolResourceRef):
            operation = await exact_operation(source, node.resource.operation_name)
            if operation.resource != node.resource:
                raise ReferenceMcpPreparationError("reference_mcp_contract_changed")
            parameter_digest = _validate_parameters(operation, node.parameters)
            contract = _operation_contract(operation)
            policy_digest = canonical_fingerprint(
                {
                    "contract": "initial-mcp-read-admission.v1",
                    "receiver": receiver.model_dump(mode="json"),
                    "operation": contract,
                    "parameters": parameter_digest,
                }
            )
            operation_digest = canonical_fingerprint(
                {
                    "contract": "initial-mcp-read-operation.v1",
                    "receiver": receiver.model_dump(mode="json"),
                    "operation": contract,
                }
            )
            prepared_operations[(source.source_id, operation.id)] = PreparedReferenceMcpOperation(
                operation.resource,
                operation.source_revision,
                json.dumps(contract["parameter_schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                operation_digest,
            )
            admissions.append(PreparedReferenceMcpAdmission(node.node_id, node.resource, "invoke", policy_digest, (operation.id,)))
            continue
        if node.resource != source.resource or node.parameters:
            raise ReferenceMcpPreparationError("reference_mcp_contract_changed")
        expanded = []
        for candidate in source.operations:
            try:
                operation = await exact_operation(source, candidate.id)
            except ReferenceMcpPreparationError:
                continue
            contract = _operation_contract(operation)
            operation_digest = canonical_fingerprint(
                {
                    "contract": "initial-mcp-read-operation.v1",
                    "receiver": receiver.model_dump(mode="json"),
                    "operation": contract,
                }
            )
            prepared_operations[(source.source_id, operation.id)] = PreparedReferenceMcpOperation(
                operation.resource,
                operation.source_revision,
                json.dumps(contract["parameter_schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                operation_digest,
            )
            expanded.append(operation.id)
        if not expanded:
            raise ReferenceMcpPreparationError("reference_mcp_contract_unavailable")
        names = tuple(sorted(expanded))
        policy_digest = canonical_fingerprint(
            {
                "contract": "initial-mcp-source-admission.v1",
                "receiver": receiver.model_dump(mode="json"),
                "resource": node.resource.model_dump(mode="json"),
                "operations": names,
            }
        )
        admissions.append(PreparedReferenceMcpAdmission(node.node_id, node.resource, "provide", policy_digest, names))

    operations = tuple(sorted(prepared_operations.values(), key=lambda item: (item.resource.source_id, item.resource.operation_name)))
    admissions_tuple = tuple(admissions)
    selection_digest = canonical_fingerprint(
        {
            "contract": "initial-mcp-read-selection.v1",
            "scope": scope.model_dump(mode="json"),
            "receiver": receiver.model_dump(mode="json"),
            "policy_epoch": context.policy_epoch,
            "admissions": [
                {
                    "node_id": item.node_id,
                    "resource": item.resource.model_dump(mode="json"),
                    "operation": item.operation,
                    "policy_digest": item.policy_digest,
                    "expanded_operation_names": item.expanded_operation_names,
                }
                for item in admissions_tuple
            ],
            "operations": [
                {
                    "resource": item.resource.model_dump(mode="json"),
                    "source_revision": item.source_revision,
                    "parameter_schema_json": item.parameter_schema_json,
                    "policy_digest": item.policy_digest,
                }
                for item in operations
            ],
        }
    )
    return PreparedReferenceMcpSelection(scope, receiver, context.policy_epoch, admissions_tuple, operations, selection_digest)


async def prepare_reference_mcp_selection(
    db_path: str | Path,
    *,
    input_document: InputDocument,
    scope: ReferenceRequestScope,
    receiver: AgentResourceRef,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> PreparedReferenceMcpSelection:
    """Prepare current exact read-only MCP dependencies without remote I/O."""
    path = Path(db_path)
    try:
        async with open_shared_ctx(path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            return await _prepare_in_connection(
                db,
                db_path=path,
                input_document=input_document,
                scope=scope,
                receiver=receiver,
                index=index,
                access=access,
            )
    except ReferenceMcpPreparationError:
        raise
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_preparation_unavailable") from None


async def _prepare_prompts_in_connection(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
    input_document: InputDocument,
    scope: ReferenceRequestScope,
    receiver: AgentResourceRef,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> PreparedReferenceMcpPromptSelection:
    if not db.in_transaction:
        raise ReferenceMcpPreparationError("reference_transaction_required")
    if Path(access.db_path).resolve() != db_path.resolve() or not index.prompt_cache_ready:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_metadata_unavailable")
    document = InputDocument.model_validate(input_document.model_dump(mode="python", warnings="error"))
    # Task content is validated with typed attachments by admission and the
    # canonical source reader; selection preparation has no attachment context.
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    receiver = AgentResourceRef.model_validate(receiver.model_dump(mode="python", warnings="error"))
    refs = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in refs if isinstance(node.resource, AgentResourceRef)]
    prompt_nodes = [node for node in refs if isinstance(node.resource, McpPromptResourceRef)]
    if (
        receiver.source_kind != "team"
        or len(agents) != 1
        or agents[0].resource != receiver
        or agents[0].parameters
        or not 1 <= len(prompt_nodes) <= 3
        or len(refs) != len(agents) + len(prompt_nodes)
    ):
        raise ReferenceMcpPreparationError("reference_mcp_prompt_selection_unsupported")

    sources = _metadata_sources(index, db_path=db_path, team_id=scope.team_id)
    async with db.execute(
        "SELECT id,status,updated_at,visibility,created_by FROM team_tool_source WHERE team_id=?",
        (scope.team_id,),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        current = {str(row["id"]): row for row in await cursor.fetchall()}
    if {source_id for source_id, row in current.items() if row["status"] == "enabled"} != set(sources):
        raise ReferenceMcpPreparationError("reference_mcp_metadata_changed")
    try:
        await AgentExecutionBindingStore(db_path).project_reference_team_agent_in_connection(
            db,
            scope=scope,
            resource=receiver,
        )
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_receiver_changed") from None

    context = await read_reference_access_context_in_connection(db, scope=scope, access_store=access)
    prepared: list[PreparedReferenceMcpPrompt] = []
    for node in prompt_nodes:
        source = sources.get(node.resource.source_id)
        row = current.get(node.resource.source_id)
        if (
            source is None
            or row is None
            or row["status"] != "enabled"
            or source.state_version != str(row["updated_at"])
            or source.visibility != str(row["visibility"])
            or source.created_by != (str(row["created_by"]) if row["created_by"] is not None else None)
            or source.visibility == "creator_only"
            and source.created_by != scope.user_id
            or not await _allowed(db, access=access, context=context, source_id=source.source_id)
        ):
            raise ReferenceMcpPreparationError("reference_mcp_source_unavailable")
        matches = [item for item in source.prompts if item.prompt_name == node.resource.prompt_name]
        if len(matches) != 1 or matches[0].resource != node.resource:
            raise ReferenceMcpPreparationError("reference_mcp_prompt_contract_changed")
        prompt = matches[0]
        schema_json, _parameter_digest = _validate_prompt_parameters(prompt, node.parameters)
        policy_digest = reference_mcp_prompt_policy_digest(
            receiver=receiver,
            resource=node.resource,
            source_revision=prompt.source_revision,
            parameter_schema_json=schema_json,
            parameters=node.parameters,
        )
        prepared.append(
            PreparedReferenceMcpPrompt(
                node.node_id,
                node.resource,
                prompt.source_revision,
                schema_json,
                policy_digest,
            )
        )

    prompts = tuple(prepared)
    selection_digest = reference_mcp_prompt_selection_digest(
        scope=scope,
        receiver=receiver,
        policy_epoch=context.policy_epoch,
        prompts=tuple((item.node_id, item.resource, item.source_revision, item.parameter_schema_json, item.policy_digest) for item in prompts),
    )
    return PreparedReferenceMcpPromptSelection(
        scope,
        receiver,
        context.policy_epoch,
        prompts,
        selection_digest,
    )


async def prepare_reference_mcp_prompt_selection(
    db_path: str | Path,
    *,
    input_document: InputDocument,
    scope: ReferenceRequestScope,
    receiver: AgentResourceRef,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> PreparedReferenceMcpPromptSelection:
    """Prepare exact current Prompt contracts and parameters without remote I/O."""
    path = Path(db_path)
    try:
        async with open_shared_ctx(path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            return await _prepare_prompts_in_connection(
                db,
                db_path=path,
                input_document=input_document,
                scope=scope,
                receiver=receiver,
                index=index,
                access=access,
            )
    except ReferenceMcpPreparationError:
        raise
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_preparation_unavailable") from None


async def revalidate_reference_mcp_prompt_selection_in_connection(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    prepared: PreparedReferenceMcpPromptSelection,
    input_document: InputDocument,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> None:
    """Rebuild the Prompt selection under the admission transaction's snapshot."""
    try:
        current = await _prepare_prompts_in_connection(
            db,
            db_path=Path(db_path),
            input_document=input_document,
            scope=prepared.scope,
            receiver=prepared.receiver,
            index=index,
            access=access,
        )
    except ReferenceMcpPreparationError:
        raise
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_preparation_unavailable") from None
    if current != prepared:
        raise ReferenceMcpPreparationError("reference_mcp_prompt_preparation_changed")


async def project_reference_mcp_prompt_admissions_in_connection(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    prepared: PreparedReferenceMcpPromptSelection,
    input_document: InputDocument,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> tuple[ReferenceAdmission, ...]:
    """Project exact Prompt expand admissions after current transactional revalidation."""
    await revalidate_reference_mcp_prompt_selection_in_connection(
        db,
        db_path=db_path,
        prepared=prepared,
        input_document=input_document,
        index=index,
        access=access,
    )
    return tuple(
        ReferenceAdmission(
            node_id=item.node_id,
            resource=item.resource.model_dump(mode="python"),
            operation="expand",
            policy_digest=item.policy_digest,
        )
        for item in prepared.prompts
    )


async def revalidate_reference_mcp_selection_in_connection(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    prepared: PreparedReferenceMcpSelection,
    input_document: InputDocument,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> None:
    """Require that the preparation still matches inside an admission transaction."""
    try:
        current = await _prepare_in_connection(
            db,
            db_path=Path(db_path),
            input_document=input_document,
            scope=prepared.scope,
            receiver=prepared.receiver,
            index=index,
            access=access,
        )
    except ReferenceMcpPreparationError:
        raise
    except Exception:
        raise ReferenceMcpPreparationError("reference_mcp_preparation_unavailable") from None
    if current != prepared:
        raise ReferenceMcpPreparationError("reference_mcp_preparation_changed")


async def project_reference_mcp_admissions_in_connection(
    db: aiosqlite.Connection,
    *,
    db_path: str | Path,
    prepared: PreparedReferenceMcpSelection,
    input_document: InputDocument,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
) -> tuple[ReferenceAdmission, ...]:
    """Revalidate and project exact MCP plan rows in the caller's transaction.

    This does not write a plan or reserve an execution.  It supplies a future
    admission orchestrator with immutable rows only after the current source,
    receiver and user authority have been rechecked under its lock.
    """
    await revalidate_reference_mcp_selection_in_connection(
        db,
        db_path=db_path,
        prepared=prepared,
        input_document=input_document,
        index=index,
        access=access,
    )
    document = InputDocument.model_validate(input_document.model_dump(mode="python", warnings="error"))
    mcp_nodes = {
        node.node_id: node
        for node in document.nodes
        if isinstance(node, ReferenceNode) and isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)
    }
    if len(mcp_nodes) != len(prepared.admissions) or set(mcp_nodes) != {item.node_id for item in prepared.admissions}:
        raise ReferenceMcpPreparationError("reference_mcp_preparation_changed")
    return tuple(
        ReferenceAdmission(
            node_id=item.node_id,
            resource=item.resource.model_dump(mode="python"),
            operation=item.operation,
            policy_digest=item.policy_digest,
        )
        for item in prepared.admissions
    )


__all__ = [
    "PreparedReferenceMcpAdmission",
    "PreparedReferenceMcpOperation",
    "PreparedReferenceMcpPrompt",
    "PreparedReferenceMcpPromptSelection",
    "reference_mcp_prompt_policy_digest",
    "reference_mcp_prompt_selection_digest",
    "PreparedReferenceMcpSelection",
    "ReferenceMcpPreparationError",
    "prepare_reference_mcp_selection",
    "prepare_reference_mcp_prompt_selection",
    "project_reference_mcp_admissions_in_connection",
    "project_reference_mcp_prompt_admissions_in_connection",
    "revalidate_reference_mcp_prompt_selection_in_connection",
    "revalidate_reference_mcp_selection_in_connection",
]
