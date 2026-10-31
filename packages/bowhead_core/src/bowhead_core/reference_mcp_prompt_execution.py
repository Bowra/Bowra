"""Claim and materialize an exact MCP Prompt primary.

The claim fixes the current local Prompt contracts, parameters, Agent receiver
and worker owner in one transaction.  Expansion happens outside database
transactions; only a candidate that still matches the current worker, authority
and immutable composition is committed as encrypted task material.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_execution import (
    ReferencePrimaryInput,
    ReferencePrimaryWorkerClaim,
    _claim_reference_primary_for_worker,
    read_reference_mcp_prompt_primary_in_connection,
)
from bowhead_core.chat_reference_runtime_store import (
    ReferencePrimaryComposition,
    _primary_baseline_composition,
    _put_primary_composition_in_connection,
    require_reference_worker_in_connection,
)
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import (
    MAX_REFERENCE_STORAGE_BYTES,
    ChatReferenceStore,
    ContentDescriptor,
    ReferenceCipher,
    ReferenceScope,
    _row,
    _transaction,
)
from bowhead_core.chat_references import AgentResourceRef, ContentHash, McpPromptResourceRef, NodeId, ReferenceNode, requested_reference_operation
from bowhead_core.mcp_prompt_expansion import (
    McpPromptExpansionDocument,
    PreparedMcpPromptExpansion,
    restore_mcp_prompt_expansion,
)
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_mcp_preparation import (
    PreparedReferenceMcpPrompt,
    PreparedReferenceMcpPromptSelection,
    prepare_reference_mcp_prompt_selection,
    reference_mcp_prompt_policy_digest,
    reference_mcp_prompt_selection_digest,
    revalidate_reference_mcp_prompt_selection_in_connection,
)
from bowhead_core.skill_baseline_material import PreparedSkillBaseline, prepare_primary_skill_baseline, restore_skill_baseline
from bowhead_core.skill_read_receiver import SkillReadReceiverBaseline, primary_skill_read_receiver, require_supported_skill_grants
from bowhead_core.skill_read_resolution import SkillReadSourceResolver

MCP_PROMPT_RESULT_RESOLUTION_SEQ = 2
MCP_PROMPT_RESULT_IDEMPOTENCY_KEY = "reference:primary:mcp-prompt-results"
MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX = "reference:primary:mcp-prompt-result:"
MCP_PROMPT_SKILL_BASELINE_KEY = "reference:primary:mcp-prompt-skill-baseline"


def _skill_receiver(primary: ReferencePrimaryInput, scope: ReferenceScope) -> SkillReadReceiverBaseline:
    original = replace(primary, binding=replace(primary.binding, status="admitted"))
    receiver = primary_skill_read_receiver(original, ReferenceRequestScope(**scope.identity()), primary.source.turn_id)
    require_supported_skill_grants(receiver.grants)
    return receiver


async def restore_prompt_skill_baseline(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    sources: SkillReadSourceResolver | None,
    cipher: ReferenceCipher | None,
) -> PreparedSkillBaseline | None:
    """Read the original packages from the existing immutable content store."""
    receiver = _skill_receiver(primary, scope)
    if not receiver.grants:
        return None
    if sources is None or cipher is None:
        raise ValueError("reference_mcp_prompt_skill_baseline_unavailable")
    retained = await ChatReferenceStore(cipher).get_encrypted_content_in_connection(
        db, scope=scope, turn_id=primary.source.turn_id, content_key=MCP_PROMPT_SKILL_BASELINE_KEY
    )
    if retained is None:
        raise ValueError("reference_mcp_prompt_skill_baseline_unavailable")
    descriptor, ciphertext = retained
    agent = next(
        node for node in primary.source.input_document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
    )
    if (
        descriptor.node_id != agent.node_id
        or descriptor.resource != agent.resource
        or descriptor.content_kind != "skill_baseline"
        or descriptor.materialization_hash != receiver.digest
    ):
        raise ValueError("reference_mcp_prompt_skill_baseline_changed")
    content = cipher.open(scope=scope, purpose="content", identity=f"{primary.source.turn_id}/{descriptor.content_key}", ciphertext=ciphertext)
    baseline = restore_skill_baseline(content, receiver=receiver, content_hash=descriptor.content_hash)
    for package in baseline.packages:
        await sources.revalidate_in_connection(db, package.proof)
    return baseline


class ReferenceMcpPromptCompositionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: NodeId
    resource: McpPromptResourceRef
    operation: Literal["expand"] = "expand"
    source_revision: str
    parameter_schema_json: str
    policy_digest: ContentHash


class ReferenceMcpPromptPrimaryComposition(ReferencePrimaryComposition):
    runtime_policy: Literal["initial-primary-mcp-prompt.v1"] = "initial-primary-mcp-prompt.v1"
    prompt_policy_epoch: int
    prompt_selection_digest: ContentHash
    prompts: tuple[ReferenceMcpPromptCompositionItem, ...]


class ReferenceMcpPromptResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: NodeId
    resource: McpPromptResourceRef
    content: ContentDescriptor


class ReferenceMcpPromptResultResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    runtime_policy: Literal["initial-primary-mcp-prompt-results.v1", "lead-mcp-prompt-results.v1"] = "initial-primary-mcp-prompt-results.v1"
    source_turn_id: str
    execution_id: str
    primary_composition_digest: ContentHash
    prompt_selection_digest: ContentHash
    results: tuple[ReferenceMcpPromptResultItem, ...]

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.model_dump(mode="json", exclude_none=True))


@dataclass(frozen=True, slots=True)
class ReferenceMcpPromptMaterialization:
    resolution: ReferenceMcpPromptResultResolution
    expansions: tuple[PreparedMcpPromptExpansion, ...] = field(repr=False)
    documents: tuple[McpPromptExpansionDocument, ...] = field(repr=False)
    reused: bool
    resolution_seq: int = MCP_PROMPT_RESULT_RESOLUTION_SEQ


McpPromptExpansionFetcher = Callable[
    [McpPromptResourceRef, dict[str, str], str],
    Awaitable[PreparedMcpPromptExpansion],
]


def _composition(
    primary: ReferencePrimaryInput,
    prepared: PreparedReferenceMcpPromptSelection,
) -> ReferenceMcpPromptPrimaryComposition:
    document = primary.source.input_document
    if document is None or primary.automatic_receiver is not None or prepared.receiver.source_kind != "team":
        raise ValueError("reference_mcp_prompt_composition_unavailable")
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in nodes if isinstance(node.resource, AgentResourceRef)]
    prompt_nodes = [node for node in nodes if isinstance(node.resource, McpPromptResourceRef)]
    if (
        len(agents) != 1
        or agents[0].resource != prepared.receiver
        or agents[0].parameters
        or not 1 <= len(prompt_nodes) <= 3
        or len(nodes) != 1 + len(prompt_nodes)
        or len(prepared.prompts) != len(prompt_nodes)
        or len(primary.plan.admissions) != len(nodes)
    ):
        raise ValueError("reference_mcp_prompt_composition_unavailable")
    prepared_by_node = {item.node_id: item for item in prepared.prompts}
    if len(prepared_by_node) != len(prepared.prompts):
        raise ValueError("reference_mcp_prompt_composition_unavailable")
    for node, admitted in zip(nodes, primary.plan.admissions, strict=True):
        if (admitted.node_id, admitted.resource, admitted.operation) != (node.node_id, node.resource, requested_reference_operation(node.resource)):
            raise ValueError("reference_mcp_prompt_composition_unavailable")
        if isinstance(node.resource, McpPromptResourceRef):
            item = prepared_by_node.get(node.node_id)
            if item is None or (item.resource, item.policy_digest) != (admitted.resource, admitted.policy_digest):
                raise ValueError("reference_mcp_prompt_composition_unavailable")
    base = _primary_baseline_composition(primary, agents[0])
    value = base.model_dump(mode="python")
    value.update(
        runtime_policy="initial-primary-mcp-prompt.v1",
        prompt_policy_epoch=prepared.policy_epoch,
        prompt_selection_digest=prepared.selection_digest,
        prompts=tuple(
            ReferenceMcpPromptCompositionItem(
                node_id=item.node_id,
                resource=item.resource,
                source_revision=item.source_revision,
                parameter_schema_json=item.parameter_schema_json,
                policy_digest=item.policy_digest,
            )
            for item in prepared.prompts
        ),
    )
    composition = ReferenceMcpPromptPrimaryComposition.model_validate(value)
    if len(composition.canonical_json().encode()) > MAX_REFERENCE_STORAGE_BYTES:
        raise ValueError("reference_composition_material_limit")
    return composition


def _validate_composition(
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    composition: ReferenceMcpPromptPrimaryComposition,
) -> None:
    """Cryptographically bind retained Prompt contracts to canonical parameters."""
    document = primary.source.input_document
    if document is None:
        raise ValueError("reference_mcp_prompt_composition_changed")
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in nodes if isinstance(node.resource, AgentResourceRef)]
    prompt_nodes = [node for node in nodes if isinstance(node.resource, McpPromptResourceRef)]
    plan_prompts = [item for item in primary.plan.admissions if isinstance(item.resource, McpPromptResourceRef)]
    saved_by_node = {item.node_id: item for item in composition.prompts}
    plan_by_node = {item.node_id: item for item in plan_prompts}
    if len(agents) != 1:
        raise ValueError("reference_mcp_prompt_composition_changed")
    expected_base = _primary_baseline_composition(primary, agents[0]).model_dump(mode="python")
    expected_base["runtime_policy"] = "initial-primary-mcp-prompt.v1"
    if (
        composition.resource.source_kind != "team"
        or composition.model_dump(mode="python", include=set(ReferencePrimaryComposition.model_fields)) != expected_base
        or composition.prompt_policy_epoch != scope.policy_epoch
        or not 1 <= len(prompt_nodes) <= 3
        or len(nodes) != len(prompt_nodes) + 1
        or len(saved_by_node) != len(prompt_nodes)
        or set(saved_by_node) != {node.node_id for node in prompt_nodes}
        or set(plan_by_node) != set(saved_by_node)
        or [(node.node_id, node.resource) for node in prompt_nodes] != [(item.node_id, item.resource) for item in plan_prompts]
        or [(item.node_id, item.resource) for item in plan_prompts] != [(item.node_id, item.resource) for item in composition.prompts]
    ):
        raise ValueError("reference_mcp_prompt_composition_changed")
    for node in prompt_nodes:
        saved = saved_by_node[node.node_id]
        admitted = plan_by_node[node.node_id]
        expected_policy = reference_mcp_prompt_policy_digest(
            receiver=composition.resource,
            resource=saved.resource,
            source_revision=saved.source_revision,
            parameter_schema_json=saved.parameter_schema_json,
            parameters=node.parameters,
        )
        if (
            saved.resource != node.resource
            or saved.operation != "expand"
            or saved.policy_digest != expected_policy
            or (admitted.resource, admitted.operation, admitted.policy_digest) != (saved.resource, saved.operation, saved.policy_digest)
        ):
            raise ValueError("reference_mcp_prompt_composition_changed")
    expected_selection = reference_mcp_prompt_selection_digest(
        scope=ReferenceRequestScope(**scope.identity()),
        receiver=composition.resource,
        policy_epoch=composition.prompt_policy_epoch,
        prompts=tuple(
            (item.node_id, item.resource, item.source_revision, item.parameter_schema_json, item.policy_digest) for item in composition.prompts
        ),
    )
    if composition.prompt_selection_digest != expected_selection:
        raise ValueError("reference_mcp_prompt_composition_changed")


async def restore_reference_mcp_prompt_composition_in_connection(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    generation: int,
) -> ReferenceMcpPromptPrimaryComposition:
    """Restore the immutable Prompt scope without granting current use."""
    _transaction(db)
    row = await _row(db, "SELECT * FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (primary.source.turn_id,))
    if row is None:
        raise ValueError("reference_mcp_prompt_composition_unavailable")
    try:
        saved = ReferenceMcpPromptPrimaryComposition.model_validate_json(row["composition_json"], strict=True)
        agent = next(
            node for node in primary.source.input_document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
        )
        expected_base = _primary_baseline_composition(primary, agent).model_dump(mode="python")
        expected_base["runtime_policy"] = "initial-primary-mcp-prompt.v1"
        if (
            type(generation) is not int
            or generation < 1
            or row["created_generation"] != 1
            or row["execution_id"] != primary.binding.execution_id
            or row["source_turn_id"] != primary.source.turn_id
            or row["kind"] != "composition"
            or row["schema_version"] != 1
            or row["idempotency_key"] != "reference:primary"
            or saved.model_dump(mode="python", include=set(ReferencePrimaryComposition.model_fields)) != expected_base
            or row["composition_json"] != saved.canonical_json()
            or row["composition_digest"] != saved.digest
            or saved.prompt_policy_epoch != scope.policy_epoch
        ):
            raise ValueError("changed composition")
        plan_prompts = [item for item in primary.plan.admissions if isinstance(item.resource, McpPromptResourceRef)]
        if [(item.node_id, item.resource, item.operation, item.policy_digest) for item in plan_prompts] != [
            (item.node_id, item.resource, item.operation, item.policy_digest) for item in saved.prompts
        ]:
            raise ValueError("changed composition")
        _validate_composition(primary, scope, saved)
        if generation > 1 and primary.plan.orchestration_contract != "lead-delegation.v1":
            from bowhead_core.chat_reference_bi_continuation import require_resume

            pause, _ = await require_resume(db, turn_id=primary.source.turn_id, generation=generation)
            if (pause["execution_id"], pause["composition_digest"], pause["source_hash"]) != (
                saved.execution_id,
                saved.digest,
                primary.plan.source_hash,
            ):
                raise ValueError("changed continuation")
        return saved
    except Exception:
        raise ValueError("reference_mcp_prompt_composition_changed") from None


def _prepared_from_composition(
    *,
    scope: ReferenceScope,
    composition: ReferenceMcpPromptPrimaryComposition,
) -> PreparedReferenceMcpPromptSelection:
    return PreparedReferenceMcpPromptSelection(
        scope=ReferenceRequestScope(**scope.identity()),
        receiver=composition.resource,
        policy_epoch=composition.prompt_policy_epoch,
        prompts=tuple(
            PreparedReferenceMcpPrompt(
                node_id=item.node_id,
                resource=item.resource,
                source_revision=item.source_revision,
                parameter_schema_json=item.parameter_schema_json,
                policy_digest=item.policy_digest,
            )
            for item in composition.prompts
        ),
        selection_digest=composition.prompt_selection_digest,
    )


def _prompt_parameters(primary: ReferencePrimaryInput) -> dict[NodeId, dict[str, str]]:
    document = primary.source.input_document
    if document is None:
        raise ValueError("reference_mcp_prompt_result_changed")
    values: dict[NodeId, dict[str, str]] = {}
    for node in document.nodes:
        if not isinstance(node, ReferenceNode) or not isinstance(node.resource, McpPromptResourceRef):
            continue
        if node.node_id in values or any(type(key) is not str or type(value) is not str for key, value in node.parameters.items()):
            raise ValueError("reference_mcp_prompt_result_changed")
        values[node.node_id] = dict(node.parameters)
    return values


def _result_content_key(position: int) -> str:
    if position not in {1, 2, 3}:
        raise ValueError("reference_mcp_prompt_result_changed")
    return f"{MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX}{position}"


def _result_resolution(
    *,
    primary: ReferencePrimaryInput,
    composition: ReferenceMcpPromptPrimaryComposition,
    expansions: tuple[PreparedMcpPromptExpansion, ...],
) -> tuple[
    ReferenceMcpPromptResultResolution,
    tuple[PreparedMcpPromptExpansion, ...],
    tuple[McpPromptExpansionDocument, ...],
]:
    parameters = _prompt_parameters(primary)
    if len(expansions) != len(composition.prompts):
        raise ValueError("reference_mcp_prompt_result_changed")
    results: list[ReferenceMcpPromptResultItem] = []
    verified: list[PreparedMcpPromptExpansion] = []
    documents: list[McpPromptExpansionDocument] = []
    for position, (item, candidate) in enumerate(zip(composition.prompts, expansions, strict=True), 1):
        descriptor = candidate.descriptor(
            node_id=item.node_id,
            content_key=_result_content_key(position),
        )
        restored = restore_mcp_prompt_expansion(candidate.content, descriptor=descriptor)
        document = restored.document()
        if (
            candidate != restored
            or descriptor.node_id != item.node_id
            or descriptor.resource != item.resource
            or document.resource != item.resource
            or document.parameters != parameters.get(item.node_id)
        ):
            raise ValueError("reference_mcp_prompt_result_changed")
        results.append(
            ReferenceMcpPromptResultItem(
                node_id=item.node_id,
                resource=item.resource,
                content=descriptor,
            )
        )
        verified.append(restored)
        documents.append(document)
    resolution = ReferenceMcpPromptResultResolution(
        runtime_policy="lead-mcp-prompt-results.v1"
        if primary.plan.orchestration_contract == "lead-delegation.v1"
        else "initial-primary-mcp-prompt-results.v1",
        source_turn_id=primary.source.turn_id,
        execution_id=composition.execution_id,
        primary_composition_digest=composition.digest,
        prompt_selection_digest=composition.prompt_selection_digest,
        results=tuple(results),
    )
    if len(resolution.canonical_json().encode()) > MAX_REFERENCE_STORAGE_BYTES:
        raise ValueError("reference_mcp_prompt_result_too_large")
    return resolution, tuple(verified), tuple(documents)


async def _restore_reference_mcp_prompt_results_in_connection(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    composition: ReferenceMcpPromptPrimaryComposition,
    cipher: ReferenceCipher,
) -> ReferenceMcpPromptMaterialization | None:
    _transaction(db)
    lead = primary.plan.orchestration_contract == "lead-delegation.v1"
    row = await _row(
        db,
        "SELECT * FROM chat_reference_resolutions WHERE turn_id=? AND " + ("idempotency_key=?" if lead else "resolution_seq=?"),
        (primary.source.turn_id, MCP_PROMPT_RESULT_IDEMPOTENCY_KEY if lead else MCP_PROMPT_RESULT_RESOLUTION_SEQ),
    )
    if row is None:
        orphan = await _row(
            db,
            "SELECT 1 FROM chat_reference_content_snapshots WHERE turn_id=? "
            "AND json_extract(descriptor_json,'$.content_kind')='prompt_result' LIMIT 1",
            (primary.source.turn_id,),
        )
        if orphan is not None:
            raise ValueError("reference_mcp_prompt_result_changed")
        return None
    try:
        saved = ReferenceMcpPromptResultResolution.model_validate_json(row["composition_json"], strict=True)
        if (
            (row["resolution_seq"] <= 2 if lead else row["resolution_seq"] != MCP_PROMPT_RESULT_RESOLUTION_SEQ)
            or saved.runtime_policy != ("lead-mcp-prompt-results.v1" if lead else "initial-primary-mcp-prompt-results.v1")
            or row["schema_version"] != 1
            or row["kind"] != "composition"
            or row["idempotency_key"] != MCP_PROMPT_RESULT_IDEMPOTENCY_KEY
            or row["source_turn_id"] != primary.source.turn_id
            or (row["created_generation"] < 1 if lead else row["created_generation"] != 1)
            or row["execution_id"] != composition.execution_id
            or row["composition_json"] != saved.canonical_json()
            or row["composition_digest"] != saved.digest
            or saved.source_turn_id != primary.source.turn_id
            or saved.execution_id != composition.execution_id
            or saved.primary_composition_digest != composition.digest
            or saved.prompt_selection_digest != composition.prompt_selection_digest
            or [(item.node_id, item.resource) for item in saved.results] != [(item.node_id, item.resource) for item in composition.prompts]
        ):
            raise ValueError
        if lead and row["created_generation"] > 2:
            await _require_late_prompt_origin_in_connection(db, primary=primary, composition=composition, generation=row["created_generation"])
        restored: list[PreparedMcpPromptExpansion] = []
        documents: list[McpPromptExpansionDocument] = []
        parameters = _prompt_parameters(primary)
        store = ChatReferenceStore(cipher)
        for position, (result, prompt) in enumerate(zip(saved.results, composition.prompts, strict=True), 1):
            if (
                result.content.content_key != _result_content_key(position)
                or result.content.node_id != prompt.node_id
                or result.content.resource != prompt.resource
                or result.content.content_kind != "prompt_result"
            ):
                raise ValueError
            retained = await store.get_encrypted_content_in_connection(
                db,
                scope=scope,
                turn_id=primary.source.turn_id,
                content_key=result.content.content_key,
            )
            if retained is None or retained[0] != result.content:
                raise ValueError
            descriptor, ciphertext = retained
            content = cipher.open(
                scope=scope,
                purpose="content",
                identity=f"{primary.source.turn_id}/{descriptor.content_key}",
                ciphertext=ciphertext,
            )
            expansion = restore_mcp_prompt_expansion(content, descriptor=descriptor)
            document = expansion.document()
            if document.resource != prompt.resource or document.parameters != parameters.get(prompt.node_id):
                raise ValueError
            restored.append(expansion)
            documents.append(document)
        return ReferenceMcpPromptMaterialization(saved, tuple(restored), tuple(documents), True, row["resolution_seq"])
    except Exception:
        raise ValueError("reference_mcp_prompt_result_changed") from None


async def _require_first_prompt_material_in_connection(
    db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, composition: ReferenceMcpPromptPrimaryComposition, generation: int
) -> None:
    """Allow first expansion only before this exact Lead primary is dispatched."""
    _transaction(db)
    lead = primary.plan.orchestration_contract == "lead-delegation.v1"
    if generation == 1:
        if lead and primary.binding.status != "admitted":
            raise ValueError("reference_mcp_prompt_result_unavailable")
        return
    if not lead or primary.binding.status != "admitted":
        raise ValueError("reference_mcp_prompt_result_unavailable")
    from bowhead_core.chat_reference_lead_continuation import all_answers, require_resume
    from bowhead_core.clarification_rounds import history_contexts, read_history, resume_generation_bounds

    row = await require_resume(db, turn_id=primary.source.turn_id, generation=generation)
    if row["primary_execution_id"] != composition.execution_id or row["composition_digest"] != composition.digest:
        raise ValueError("reference_mcp_prompt_result_unavailable")
    # Decode the original answers through their original codec. A marker or
    # resolved status alone cannot replace the retained user material.
    await all_answers(db, row)
    if "question_round" not in json.loads(row["resume_context_json"]):
        if generation != 2:
            raise ValueError("reference_mcp_prompt_result_unavailable")
        return
    history = await read_history(
        db, turn_id=primary.source.turn_id, **{key: row["original_" + key] for key in ("user_id", "workspace_id", "session_id")}
    )
    contexts = history_contexts(history)
    if not history or history[-1]["request_id"] != row["request_id"] or any(card["status"] != "resolved" for card in history):
        raise ValueError("reference_mcp_prompt_result_unavailable")
    for index, card in enumerate(history):
        lower, upper = resume_generation_bounds(contexts, index)
        upper = generation if upper is None else upper
        consumed = await _row(
            db,
            "SELECT MAX(json_extract(payload_json,'$.execution_generation')) FROM turn_events "
            "WHERE turn_id=? AND event_type='turn.resumed' AND json_valid(payload_json) "
            "AND json_type(payload_json,'$.execution_generation')='integer' "
            "AND json_extract(payload_json,'$.execution_generation')>? "
            "AND json_extract(payload_json,'$.execution_generation')<=? "
            "AND json_extract(payload_json,'$.request_id')=?",
            (primary.source.turn_id, lower, upper, card["request_id"]),
        )
        if consumed[0] != upper:
            raise ValueError("reference_mcp_prompt_result_unavailable")


async def _require_late_prompt_origin_in_connection(
    db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, composition: ReferenceMcpPromptPrimaryComposition, generation: int
) -> None:
    """Reuse late material only at its proven original primary generation."""
    if primary.binding.status == "admitted":
        await _require_first_prompt_material_in_connection(db, primary=primary, composition=composition, generation=generation)
        return
    from bowhead_core.chat_reference_question_origin import read_question_origin_in_tx

    post = await _row(
        db,
        "SELECT request_id FROM clarification_requests WHERE turn_id=? AND CASE WHEN json_valid(resume_context_json) "
        "THEN json_extract(resume_context_json,'$.reference_continuation.contract') END='reference-lead-postdispatch.v1'",
        (primary.source.turn_id,),
    )
    origin = await read_question_origin_in_tx(
        db, turn_id=primary.source.turn_id, postdispatch_card_id=post["request_id"] if post is not None else None
    )
    if origin.generation != generation or origin.execution_id != composition.execution_id:
        raise ValueError("reference_mcp_prompt_result_unavailable")


async def _require_prompt_runtime_in_connection(
    db: aiosqlite.Connection,
    *,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    generation: int,
    bindings: AgentExecutionBindingStore,
    read_output: bool = False,
) -> tuple[ReferencePrimaryInput, ReferenceMcpPromptPrimaryComposition]:
    _transaction(db)
    result_trigger_count = await _row(
        db,
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN (?,?)",
        (
            "trg_reference_mcp_prompt_result_snapshot",
            "trg_reference_mcp_prompt_result_resolution",
        ),
    )
    if result_trigger_count is None or result_trigger_count[0] != 2:
        raise ValueError("reference_mcp_prompt_result_migration_required")
    primary = await read_reference_mcp_prompt_primary_in_connection(
        db,
        scope=scope,
        turn_id=turn_id,
        bindings=bindings,
        read_output=read_output,
    )
    lead = primary.plan.orchestration_contract == "lead-delegation.v1"
    if primary.binding.status != "running" and not (lead and (primary.binding.status == "admitted" or read_output)):
        raise ValueError("reference_mcp_prompt_runtime_changed")
    composition = await restore_reference_mcp_prompt_composition_in_connection(
        db,
        primary=primary,
        scope=scope,
        generation=generation,
    )
    prepared = _prepared_from_composition(scope=scope, composition=composition)
    await revalidate_reference_mcp_prompt_selection_in_connection(
        db,
        db_path=index.db_path,
        prepared=prepared,
        input_document=primary.source.input_document,
        index=index,
        access=access,
    )
    if _composition(primary, prepared) != composition:
        raise ValueError("reference_mcp_prompt_runtime_changed")
    await require_reference_worker_in_connection(
        db,
        scope=scope,
        turn_id=turn_id,
        owner_id=owner_id,
        generation=generation,
    )
    return primary, composition


async def materialize_reference_mcp_prompt_results(
    *,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
    cipher: ReferenceCipher,
    scope: ReferenceRequestScope,
    turn_id: str,
    owner_id: str,
    expected_generation: int,
    fetch: McpPromptExpansionFetcher,
) -> ReferenceMcpPromptMaterialization:
    """Fetch missing Prompt results and atomically retain their exact snapshots.

    The first transaction reads only local authority and an existing committed
    result. Remote I/O happens after rollback. A second write transaction repeats
    every authority and worker check before storing all snapshots plus the result
    resolution. A committed result is always restored without calling ``fetch``.
    """
    request_scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    if (
        not callable(fetch)
        or type(expected_generation) is not int
        or expected_generation < 1
        or Path(index.db_path).resolve() != Path(access.db_path).resolve()
    ):
        raise ValueError("reference_mcp_prompt_runtime_changed")
    bindings = AgentExecutionBindingStore(index.db_path)
    async with open_shared_ctx(index.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            epoch = await require_reference_request_scope(db, request_scope)
            stored_scope = ReferenceScope(**request_scope.model_dump(), policy_epoch=epoch)
            primary, composition = await _require_prompt_runtime_in_connection(
                db,
                index=index,
                access=access,
                scope=stored_scope,
                turn_id=turn_id,
                owner_id=owner_id,
                generation=expected_generation,
                bindings=bindings,
            )
            existing = await _restore_reference_mcp_prompt_results_in_connection(
                db,
                primary=primary,
                scope=stored_scope,
                composition=composition,
                cipher=cipher,
            )
            if existing is not None:
                return existing
            # Only an answered pre-dispatch Lead turn may expand for the first
            # time after a pause. Missing post-dispatch material never refetches.
            await _require_first_prompt_material_in_connection(db, primary=primary, composition=composition, generation=expected_generation)
            parameters = _prompt_parameters(primary)
            requests = tuple((item.resource, parameters[item.node_id], item.source_revision) for item in composition.prompts)
        finally:
            await db.rollback()

    candidates: list[PreparedMcpPromptExpansion] = []
    for position, (resource, arguments, source_revision) in enumerate(requests):
        if position:
            # A prior remote call may have outlived a revocation, cancellation or
            # competing idempotent commit. Recheck before issuing another call;
            # the final write transaction still repeats the same checks.
            async with open_shared_ctx(index.db_path) as db:
                await db.execute("PRAGMA query_only=ON")
                await db.execute("BEGIN")
                try:
                    epoch = await require_reference_request_scope(db, request_scope)
                    current_scope = ReferenceScope(**request_scope.model_dump(), policy_epoch=epoch)
                    if current_scope != stored_scope:
                        raise ValueError("reference_mcp_prompt_runtime_changed")
                    current_primary, current_composition = await _require_prompt_runtime_in_connection(
                        db,
                        index=index,
                        access=access,
                        scope=current_scope,
                        turn_id=turn_id,
                        owner_id=owner_id,
                        generation=expected_generation,
                        bindings=bindings,
                    )
                    if current_composition != composition:
                        raise ValueError("reference_mcp_prompt_runtime_changed")
                    existing = await _restore_reference_mcp_prompt_results_in_connection(
                        db,
                        primary=current_primary,
                        scope=current_scope,
                        composition=current_composition,
                        cipher=cipher,
                    )
                    if existing is not None:
                        return existing
                    await _require_first_prompt_material_in_connection(
                        db, primary=current_primary, composition=current_composition, generation=expected_generation
                    )
                finally:
                    await db.rollback()
        candidates.append(await fetch(resource, dict(arguments), source_revision))

    async with open_shared_ctx(index.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            epoch = await require_reference_request_scope(db, request_scope)
            current_scope = ReferenceScope(**request_scope.model_dump(), policy_epoch=epoch)
            if current_scope != stored_scope:
                raise ValueError("reference_mcp_prompt_runtime_changed")
            primary, composition = await _require_prompt_runtime_in_connection(
                db,
                index=index,
                access=access,
                scope=current_scope,
                turn_id=turn_id,
                owner_id=owner_id,
                generation=expected_generation,
                bindings=bindings,
            )
            existing = await _restore_reference_mcp_prompt_results_in_connection(
                db,
                primary=primary,
                scope=current_scope,
                composition=composition,
                cipher=cipher,
            )
            if existing is not None:
                await db.rollback()
                return existing
            await _require_first_prompt_material_in_connection(db, primary=primary, composition=composition, generation=expected_generation)
            resolution, expansions, documents = _result_resolution(
                primary=primary,
                composition=composition,
                expansions=tuple(candidates),
            )
            plan = await _row(
                db,
                "SELECT expires_at FROM chat_reference_plans WHERE turn_id=?",
                (turn_id,),
            )
            if plan is None:
                raise ValueError("reference_mcp_prompt_runtime_changed")
            store = ChatReferenceStore(cipher)
            for result, expansion in zip(resolution.results, expansions, strict=True):
                await store.put_content_in_connection(
                    db,
                    scope=current_scope,
                    turn_id=turn_id,
                    descriptor=result.content,
                    content=expansion.content,
                    generation=expected_generation,
                    owner_id=owner_id,
                    expires_at=plan["expires_at"],
                )
            resolution_seq = MCP_PROMPT_RESULT_RESOLUTION_SEQ
            if primary.plan.orchestration_contract == "lead-delegation.v1":
                row = await _row(db, "SELECT MAX(2,COALESCE(MAX(resolution_seq),0))+1 FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,))
                resolution_seq = row[0]
            await db.execute(
                "INSERT INTO chat_reference_resolutions("
                "turn_id,resolution_seq,schema_version,kind,idempotency_key,source_turn_id,created_generation,"
                "execution_id,composition_digest,composition_json,created_at) "
                "VALUES (?,?,1,'composition',?,?,?,?,?,?,?)",
                (
                    turn_id,
                    resolution_seq,
                    MCP_PROMPT_RESULT_IDEMPOTENCY_KEY,
                    turn_id,
                    expected_generation,
                    composition.execution_id,
                    resolution.digest,
                    resolution.canonical_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await require_reference_worker_in_connection(
                db,
                scope=current_scope,
                turn_id=turn_id,
                owner_id=owner_id,
                generation=expected_generation,
            )
            await db.commit()
            return ReferenceMcpPromptMaterialization(
                resolution,
                expansions,
                documents,
                False,
                resolution_seq,
            )
        except BaseException:
            if db.in_transaction:
                await db.rollback()
            raise


@dataclass(frozen=True, slots=True)
class McpPromptReferenceWorkerPreparation:
    index: ReferenceMcpMetadataIndex = field(repr=False)
    access: AccessControlStore = field(repr=False)
    prepared: PreparedReferenceMcpPromptSelection | None = field(repr=False)
    sources: SkillReadSourceResolver | None = field(default=None, repr=False)
    cipher: ReferenceCipher | None = field(default=None, repr=False)
    baseline: PreparedSkillBaseline | None = field(default=None, repr=False)

    async def require_existing(
        self,
        db: aiosqlite.Connection,
        *,
        primary: ReferencePrimaryInput,
        scope: ReferenceScope,
        generation: int,
    ) -> None:
        await restore_reference_mcp_prompt_composition_in_connection(
            db,
            primary=primary,
            scope=scope,
            generation=generation,
        )
        await restore_prompt_skill_baseline(db, primary=primary, scope=scope, sources=self.sources, cipher=self.cipher)

    async def revalidate(
        self,
        db: aiosqlite.Connection,
        *,
        primary: ReferencePrimaryInput,
        scope: ReferenceScope,
    ) -> None:
        if self.prepared is None or primary.binding.status != "admitted":
            raise ValueError("reference_mcp_receiver_changed")
        expected_scope = ReferenceRequestScope(**scope.identity())
        if self.prepared.scope != expected_scope or self.prepared.policy_epoch != scope.policy_epoch:
            raise ValueError("reference_mcp_prompt_scope_changed")
        if Path(self.index.db_path).resolve() != Path(self.access.db_path).resolve():
            raise ValueError("reference_mcp_prompt_scope_changed")
        if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_mcp_prompt_composition'", ()) is None:
            raise ValueError("reference_mcp_prompt_composition_migration_required")
        await revalidate_reference_mcp_prompt_selection_in_connection(
            db,
            db_path=self.index.db_path,
            prepared=self.prepared,
            input_document=primary.source.input_document,
            index=self.index,
            access=self.access,
        )
        _composition(primary, self.prepared)
        receiver = _skill_receiver(primary, scope)
        if receiver.grants:
            if self.sources is None or self.cipher is None or self.baseline is None or self.baseline.receiver != receiver:
                raise ValueError("reference_mcp_prompt_skill_baseline_unavailable")
            restore_skill_baseline(self.baseline.content, receiver=receiver, content_hash=self.baseline.content_hash)
            for package in self.baseline.packages:
                await self.sources.revalidate_in_connection(db, package.proof)

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
        _validate_composition(primary, scope, composition)
        if self.baseline is not None:
            descriptor = ContentDescriptor(
                schema_version=1,
                content_key=MCP_PROMPT_SKILL_BASELINE_KEY,
                node_id=composition.node_id,
                resource=composition.resource.model_dump(mode="python"),
                content_kind="skill_baseline",
                materialization_hash=self.baseline.receiver.digest,
                content_hash=self.baseline.content_hash,
                byte_count=len(self.baseline.content),
            )
            plan = await _row(db, "SELECT expires_at FROM chat_reference_plans WHERE turn_id=?", (primary.source.turn_id,))
            await ChatReferenceStore(self.cipher).put_content_in_connection(
                db,
                scope=scope,
                turn_id=primary.source.turn_id,
                descriptor=descriptor,
                content=self.baseline.content,
                generation=generation,
                owner_id=owner_id,
                expires_at=plan["expires_at"],
            )
        await _put_primary_composition_in_connection(db, composition=composition, generation=generation)
        await require_reference_worker_in_connection(
            db,
            scope=scope,
            turn_id=primary.source.turn_id,
            owner_id=owner_id,
            generation=generation,
        )


async def claim_reference_mcp_prompt_primary_for_worker(
    *,
    index: ReferenceMcpMetadataIndex,
    access: AccessControlStore,
    scope: ReferenceRequestScope,
    turn_id: str,
    owner_id: str,
    expected_generation: int,
    lease_seconds: int = 30,
    sources: SkillReadSourceResolver | None = None,
    cipher: ReferenceCipher | None = None,
) -> ReferencePrimaryWorkerClaim:
    """Prepare the current local Prompt scope, then claim it atomically."""
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    if Path(index.db_path).resolve() != Path(access.db_path).resolve():
        raise ValueError("reference_mcp_prompt_scope_changed")
    if sources is not None and Path(sources.db_path).resolve() != Path(index.db_path).resolve():
        raise ValueError("reference_mcp_prompt_scope_changed")
    bindings = AgentExecutionBindingStore(index.db_path)
    async with open_shared_ctx(index.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            epoch = await require_reference_request_scope(db, scope)
            stored_scope = ReferenceScope(**scope.model_dump(), policy_epoch=epoch)
            primary = await read_reference_mcp_prompt_primary_in_connection(
                db,
                scope=stored_scope,
                turn_id=turn_id,
                bindings=bindings,
            )
        finally:
            await db.rollback()
    prepared = None
    baseline = None
    if primary.binding.status == "admitted":
        receiver = next(
            node.resource
            for node in primary.source.input_document.nodes
            if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
        )
        prepared = await prepare_reference_mcp_prompt_selection(
            index.db_path,
            input_document=primary.source.input_document,
            scope=scope,
            receiver=receiver,
            index=index,
            access=access,
        )
        if _skill_receiver(primary, stored_scope).grants:
            if sources is None or cipher is None:
                raise ValueError("reference_mcp_prompt_skill_baseline_unavailable")

            async def current_primary():
                async with open_shared_ctx(index.db_path) as db:
                    await db.execute("PRAGMA query_only=ON")
                    await db.execute("BEGIN")
                    return await read_reference_mcp_prompt_primary_in_connection(db, scope=stored_scope, turn_id=turn_id, bindings=bindings)

            baseline = await prepare_primary_skill_baseline(sources, scope=scope, turn_id=turn_id, primary_factory=current_primary)
    return await _claim_reference_primary_for_worker(
        scope=stored_scope,
        turn_id=turn_id,
        owner_id=owner_id,
        expected_generation=expected_generation,
        bindings=bindings,
        lease_seconds=lease_seconds,
        mcp_prompt=McpPromptReferenceWorkerPreparation(index, access, prepared, sources, cipher, baseline),
    )


__all__ = [
    "McpPromptExpansionFetcher",
    "ReferenceMcpPromptMaterialization",
    "ReferenceMcpPromptPrimaryComposition",
    "ReferenceMcpPromptResultResolution",
    "claim_reference_mcp_prompt_primary_for_worker",
    "materialize_reference_mcp_prompt_results",
    "restore_reference_mcp_prompt_composition_in_connection",
]
