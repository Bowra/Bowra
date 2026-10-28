"""Internal admission of an exact @Agent and optional baseline Skill reads.

No catalog/HTTP/worker entry is enabled here. A server-prepared candidate is
rechecked in the same transaction as the canonical source, plan and receipt.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import aiosqlite

from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore, ExactAgentBindingExpectation, ProjectedSystemAgentRevision
from bowhead_core.chat_reference_capability import PreparedReferenceCapability
from bowhead_core.chat_reference_execution import ReferencePrimaryInput
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import (
    ChatReferenceStore,
    ReferencePlan,
    ReferenceScope,
    reference_source_hash,
    supports_lead_reference_receiver,
)
from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    InputDocument,
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
    ReferenceNode,
    SkillResourceRef,
    requested_reference_operation,
)
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_mcp_preparation import (
    PreparedReferenceMcpPromptSelection,
    PreparedReferenceMcpSelection,
    project_reference_mcp_admissions_in_connection,
    project_reference_mcp_prompt_admissions_in_connection,
)
from bowhead_core.skill_read_closure import (
    PreparedSkillReadClosure,
    empty_baseline_selection,
    prepare_skill_read_closure,
    snapshot_prepared_skill_read_closure,
)
from bowhead_core.skill_read_receiver import require_prepared_primary_skill_baseline
from bowhead_core.skill_read_render import validate_prepared_skill_read_parameters
from bowhead_core.skill_read_resolution import SkillReadSourceResolver


def reference_start_receiver(document: InputDocument, automatic_receiver: AgentResourceRef | None = None) -> AgentResourceRef:
    """Keep the server's choice separate from the user's explicit references."""
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in nodes if isinstance(node.resource, AgentResourceRef)]
    skills = [node for node in nodes if isinstance(node.resource, SkillResourceRef)]
    capabilities = [node for node in nodes if isinstance(node.resource, CapabilityResourceRef)]
    mcp = [node for node in nodes if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)]
    prompts = [node for node in nodes if isinstance(node.resource, McpPromptResourceRef)]
    if (
        len(nodes) != len(agents) + len(skills) + len(capabilities) + len(mcp) + len(prompts)
        or any(node.parameters for node in agents)
        or len(skills) > 3
        or len(capabilities) > 1
        or len(prompts) > 3
        or prompts
        and (skills or capabilities or mcp)
    ):
        raise ValueError("reference_start_unsupported")
    if automatic_receiver is not None:
        if agents or mcp or prompts or not (1 <= len(skills) <= 3 or len(capabilities) == 1):
            raise ValueError("reference_start_unsupported")
        return AgentResourceRef.model_validate(automatic_receiver.model_dump(mode="python", warnings="error"))
    if len(agents) != 1:
        raise ValueError("reference_start_unsupported")
    return agents[0].resource


@dataclass(frozen=True, slots=True)
class ReferenceStartAdmission:
    """Server-only candidate; neither client metadata nor an authorization grant."""

    input_document: InputDocument
    policy_epoch: int
    profile_name: str
    expected: ExactAgentBindingExpectation
    orchestration_contract: Literal["lead-delegation.v1"] | None = None
    system_revision: ProjectedSystemAgentRevision | None = None
    manifest_fingerprint: str | None = None
    override_version: int | None = None
    system_required: bool = False
    skill_reads: PreparedSkillReadClosure | None = field(default=None, repr=False)
    skill_sources: SkillReadSourceResolver | None = field(default=None, repr=False)
    capability: PreparedReferenceCapability | None = field(default=None, repr=False)
    mcp_selection: PreparedReferenceMcpSelection | None = field(default=None, repr=False)
    mcp_prompt_selection: PreparedReferenceMcpPromptSelection | None = field(default=None, repr=False)
    mcp_index: ReferenceMcpMetadataIndex | None = field(default=None, repr=False)
    mcp_access: AccessControlStore | None = field(default=None, repr=False)
    # Server preparation metadata for entry support; never a permission grant.
    required_skill_modes: tuple[Literal["baseline", "readonly_extension"], ...] = ()
    require_released_entry: bool = False
    # Preparation must distinguish an attachment-only task from an empty task.
    # Attachment identities remain solely in the canonical Turn source.
    has_attachments: bool = False
    # Server-selected identity persists in the primary root with source=lead.
    # Never synthesize an explicit @ in InputDocument.
    automatic_receiver: AgentResourceRef | None = None

    def snapshot(self) -> ReferenceStartAdmission:
        if type(self.require_released_entry) is not bool or type(self.has_attachments) is not bool:
            raise ValueError("reference_start_invalid")
        if (
            type(self.required_skill_modes) is not tuple
            or any(type(mode) is not str for mode in self.required_skill_modes)
            or len(set(self.required_skill_modes)) != len(self.required_skill_modes)
            or any(mode not in {"baseline", "readonly_extension"} for mode in self.required_skill_modes)
        ):
            raise ValueError("reference_start_invalid")
        document = InputDocument.model_validate(self.input_document.model_dump(mode="python", exclude_none=True, warnings=False))
        document.validate_task(has_attachments=self.has_attachments)
        nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
        skills = [node for node in nodes if isinstance(node.resource, SkillResourceRef)]
        capabilities = [node for node in nodes if isinstance(node.resource, CapabilityResourceRef)]
        mcp = [node for node in nodes if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)]
        prompts = [node for node in nodes if isinstance(node.resource, McpPromptResourceRef)]
        resource = reference_start_receiver(document, self.automatic_receiver)
        prepared = None
        if skills:
            if self.skill_reads is None or not isinstance(self.skill_sources, SkillReadSourceResolver):
                raise ValueError("reference_skill_preparation_required")
            prepared = snapshot_prepared_skill_read_closure(self.skill_reads)
            validate_prepared_skill_read_parameters(prepared, skills)
            if any(item.proof.policy_epoch != self.policy_epoch for item in prepared.items):
                raise ValueError("reference_policy_changed")
        elif self.skill_reads is not None or (self.skill_sources is not None and not isinstance(self.skill_sources, SkillReadSourceResolver)):
            raise ValueError("reference_start_unsupported")
        if capabilities:
            capability = self.capability
            if (
                capability is None
                or capability.node_id != capabilities[0].node_id
                or capability.resource != capabilities[0].resource
                or not capability.implementation_ref
                or not capability.implementation_digest.startswith("sha256:")
                or not capability.policy_digest.startswith("sha256:")
                or capability.output_contract != "visual.artifact.v1"
            ):
                raise ValueError("reference_capability_preparation_required")
        elif self.capability is not None:
            raise ValueError("reference_start_unsupported")
        if mcp:
            if (
                self.mcp_selection is None
                or self.mcp_selection.receiver != resource
                or self.mcp_selection.policy_epoch != self.policy_epoch
                or len(self.mcp_selection.admissions) != len(mcp)
                or any(
                    (prepared.node_id, prepared.resource) != (selected.node_id, selected.resource)
                    for prepared, selected in zip(self.mcp_selection.admissions, mcp, strict=True)
                )
            ):
                raise ValueError("reference_mcp_preparation_required")
        elif self.mcp_selection is not None:
            raise ValueError("reference_start_unsupported")
        if prompts:
            if (
                self.mcp_prompt_selection is None
                or self.mcp_prompt_selection.receiver != resource
                or self.mcp_prompt_selection.policy_epoch != self.policy_epoch
                or len(self.mcp_prompt_selection.prompts) != len(prompts)
                or any(
                    (prepared.node_id, prepared.resource) != (selected.node_id, selected.resource)
                    for prepared, selected in zip(self.mcp_prompt_selection.prompts, prompts, strict=True)
                )
            ):
                raise ValueError("reference_mcp_prompt_preparation_required")
        elif self.mcp_prompt_selection is not None:
            raise ValueError("reference_start_unsupported")
        if mcp or prompts:
            if not isinstance(self.mcp_index, ReferenceMcpMetadataIndex) or not isinstance(self.mcp_access, AccessControlStore):
                raise ValueError("reference_mcp_preparation_required")
        elif self.mcp_index is not None or self.mcp_access is not None:
            raise ValueError("reference_start_unsupported")
        if (resource.id, resource.version, resource.assignment_id, resource.release_id) != (
            self.expected.agent_id,
            self.expected.revision_ref,
            self.expected.assignment_id,
            self.expected.release_id,
        ):
            raise ValueError("reference_agent_candidate_mismatch")
        if type(self.policy_epoch) is not int or self.policy_epoch < 0 or not self.profile_name:
            raise ValueError("reference_start_invalid")
        if type(self.system_required) is not bool:
            raise ValueError("reference_start_invalid")
        if resource.source_kind == "system":
            if self.system_revision is None or not self.manifest_fingerprint:
                raise ValueError("reference_system_preparation_required")
            if self.system_required:
                from bowhead_core.system_agent_marketplace import SYSTEM_AGENT_LISTINGS

                if self.profile_name not in SYSTEM_AGENT_LISTINGS or resource.id != "system:" + self.profile_name:
                    raise ValueError("reference_agent_candidate_mismatch")
        elif self.system_revision is not None or self.manifest_fingerprint is not None or self.override_version is not None or self.system_required:
            raise ValueError("reference_agent_candidate_mismatch")
        return replace(
            self,
            input_document=document,
            system_revision=copy.deepcopy(self.system_revision),
            skill_reads=prepared,
            capability=copy.deepcopy(self.capability),
            mcp_selection=copy.deepcopy(self.mcp_selection),
            mcp_prompt_selection=copy.deepcopy(self.mcp_prompt_selection),
            automatic_receiver=resource if self.automatic_receiver is not None else None,
        )


async def prepare_skill_reference_start_admission(
    admission: ReferenceStartAdmission, *, sources: SkillReadSourceResolver, scope: ReferenceRequestScope
) -> ReferenceStartAdmission:
    """Prepare source bytes/parameters before the finalizer acquires its write lock.

    The full input is retained. An Agent-only projection is used solely to reuse
    candidate identity validation, never as a persisted/executable fallback.
    """
    if admission.automatic_receiver is not None:
        raise ValueError("reference_start_unsupported")
    document = InputDocument.model_validate(admission.input_document.model_dump(mode="python", exclude_none=True, warnings="error"))
    document.validate_task(has_attachments=admission.has_attachments)
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    skills = [node for node in nodes if isinstance(node.resource, SkillResourceRef)]
    if not 1 <= len(skills) <= 3:
        raise ValueError("reference_start_unsupported")
    agent_candidate = document.model_copy(update={"nodes": [node for node in document.nodes if node not in skills]})
    candidate = replace(admission, input_document=agent_candidate, skill_reads=None, skill_sources=None).snapshot()
    closure = await prepare_skill_read_closure(sources, [node.resource for node in skills], scope=scope)
    return replace(candidate, input_document=document, skill_reads=closure, skill_sources=sources).snapshot()


async def admit_reference_start_in_connection(
    db: aiosqlite.Connection,
    *,
    admission: ReferenceStartAdmission,
    source: ChatTurnUserSource,
    team_id: str,
    bindings: AgentExecutionBindingStore,
    expires_at: str,
    requested_session_id: str | None = None,
) -> None:
    """Reserve the selected execution and persist its plan/root in one transaction."""
    admission = admission.snapshot()
    if admission.orchestration_contract is not None:
        from bowhead_core.chat_reference_execution import _lead_contract_missing

        if await _lead_contract_missing(
            db,
            skill_only=admission.automatic_receiver is not None,
            capability=admission.capability is not None,
            mcp=admission.mcp_selection is not None,
            prompt=admission.mcp_prompt_selection is not None,
        ):
            raise ValueError("reference_lead_migration_required")
    if admission.has_attachments != bool(source.attachment_ids) or admission.has_attachments != bool(source.attachment_refs):
        raise ValueError("reference_attachment_source_changed")
    if admission.automatic_receiver is not None:
        async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_automatic_receiver_source'") as cursor:
            if await cursor.fetchone() is None:
                raise ValueError("reference_automatic_admission_unavailable")
    if admission.capability is not None:
        async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_capability_composition'") as cursor:
            if await cursor.fetchone() is None:
                raise ValueError("reference_capability_admission_unavailable")
    nodes = [node for node in admission.input_document.nodes if isinstance(node, ReferenceNode)]
    closure = admission.skill_reads
    if admission.skill_sources is not None:
        sources = admission.skill_sources
        assert sources is not None
        expected_scope = ReferenceRequestScope(
            team_id=team_id, user_id=source.user_id, workspace_id=source.workspace_id, session_id=requested_session_id
        )
        if closure is None:
            closure = empty_baseline_selection(expected_scope)
        if closure.scope != expected_scope or Path(sources.db_path) != Path(bindings.db_path):
            raise ValueError("reference_skill_preparation_scope_mismatch")
        for item in closure.items:
            await sources.revalidate_in_connection(db, item.proof)
    resource = reference_start_receiver(admission.input_document, admission.automatic_receiver)
    mcp_admissions = {}
    if admission.mcp_selection is not None:
        expected_scope = ReferenceRequestScope(
            team_id=team_id,
            user_id=source.user_id,
            workspace_id=source.workspace_id,
            session_id=requested_session_id,
        )
        if admission.mcp_selection.scope != expected_scope:
            raise ValueError("reference_mcp_preparation_scope_mismatch")
        assert admission.mcp_index is not None and admission.mcp_access is not None
        projected = await project_reference_mcp_admissions_in_connection(
            db,
            db_path=bindings.db_path,
            prepared=admission.mcp_selection,
            input_document=admission.input_document,
            index=admission.mcp_index,
            access=admission.mcp_access,
        )
        mcp_admissions = {item.node_id: item for item in projected}
    if admission.mcp_prompt_selection is not None:
        expected_scope = ReferenceRequestScope(
            team_id=team_id,
            user_id=source.user_id,
            workspace_id=source.workspace_id,
            session_id=requested_session_id,
        )
        if admission.mcp_prompt_selection.scope != expected_scope:
            raise ValueError("reference_mcp_preparation_scope_mismatch")
        assert admission.mcp_index is not None and admission.mcp_access is not None
        projected = await project_reference_mcp_prompt_admissions_in_connection(
            db,
            db_path=bindings.db_path,
            prepared=admission.mcp_prompt_selection,
            input_document=admission.input_document,
            index=admission.mcp_index,
            access=admission.mcp_access,
        )
        mcp_admissions = {item.node_id: item for item in projected}
    common = {
        "team_id": team_id,
        "user_id": source.user_id,
        "turn_id": source.turn_id,
        "profile_name": admission.profile_name,
        "dispatch_source": "lead" if admission.automatic_receiver is not None else "explicit",
        "dispatch_key": "reference:primary",
        "expected": admission.expected,
    }
    if resource.source_kind == "team":
        binding = await bindings.admit_team_execution_in_connection(db, agent_id=resource.id, **common)
    else:
        prepared = admission.system_revision
        assert prepared is not None and admission.manifest_fingerprint is not None
        binding = await bindings.admit_system_execution_in_connection(
            db,
            **common,
            definition=prepared.definition,
            prepared_revision=prepared,
            manifest_fingerprint=admission.manifest_fingerprint,
            override_version=admission.override_version,
            system_required=admission.system_required,
            enforce_current_override=True,
        )
    plan = ReferencePlan.model_validate(
        {
            "schema_version": 1,
            "orchestration_contract": admission.orchestration_contract,
            "source_turn_id": source.turn_id,
            "logical_source_turn_id": source.turn_id,
            "snapshot_owner_turn_id": source.turn_id,
            "source_hash": reference_source_hash(source),
            "admissions": [
                {
                    "node_id": selected.node_id,
                    "resource": selected.resource.model_dump(exclude_none=True),
                    "operation": requested_reference_operation(selected.resource),
                    "policy_digest": binding.policy_fingerprint
                    if isinstance(selected.resource, AgentResourceRef)
                    else admission.capability.policy_digest
                    if isinstance(selected.resource, CapabilityResourceRef) and admission.capability is not None
                    else mcp_admissions[selected.node_id].policy_digest
                    if isinstance(selected.resource, McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef)
                    else canonical_fingerprint(
                        {
                            "contract": "baseline-skill-read-admission.v1",
                            "resource": selected.resource.model_dump(mode="json"),
                            "closure_digest": closure.closure_digest,
                            "receiver_policy": binding.policy_fingerprint,
                            "receiver_revision": binding.revision_ref,
                        }
                    ),
                }
                for selected in nodes
            ],
        }
    )
    if closure is not None:
        await require_prepared_primary_skill_baseline(db, ReferencePrimaryInput(source, plan, binding), closure, sources=admission.skill_sources)
    scope = ReferenceScope(
        team_id=team_id, user_id=source.user_id, workspace_id=source.workspace_id, session_id=source.session_id, policy_epoch=admission.policy_epoch
    )
    await ChatReferenceStore.put_plan_in_connection(db, scope=scope, turn_id=source.turn_id, plan=plan, expires_at=expires_at)
    await bindings.insert_root_binding_in_connection(
        db,
        binding=binding,
        user_id=source.user_id,
        assignment_id=resource.assignment_id,
        binding_source="lead" if admission.automatic_receiver is not None else "mention",
        bind_primary_execution=True,
    )

    if admission.require_released_entry:
        import os

        from bowhead_core.chat_reference_attachment_snapshot import supports_lead_attachment_refs
        from bowhead_core.chat_reference_catalog import initial_reference_entry_supported

        capability_enabled = admission.capability is None or os.environ.get("FEATURE_CHAT_REFERENCE_CAPABILITY", "0") == "1"
        lead_enabled = admission.orchestration_contract is None or os.environ.get("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1") == "1"
        lead_cell = admission.orchestration_contract is None or (
            supports_lead_reference_receiver(resource, (node.resource for node in nodes))
            and (not admission.has_attachments or supports_lead_attachment_refs(source.attachment_refs, source.attachment_ids))
        )
        if (
            os.environ.get("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1") != "1"
            or not capability_enabled
            or not lead_enabled
            or not lead_cell
            or not initial_reference_entry_supported(admission.input_document, required_skill_modes=admission.required_skill_modes)
        ):
            raise ValueError("reference_entry_unavailable")
