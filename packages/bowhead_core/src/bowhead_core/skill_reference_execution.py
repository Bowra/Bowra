"""Atomically retain baseline Skill reads and claim one initial primary worker.

Only the server-selected database, canonical source and exact published baseline
supply preparation. This does not schedule a model, grant temporary dependencies
or prove injection. Old single-@ consumers retain their own narrow contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, model_validator

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_capability import PreparedReferenceCapability, prepare_reference_capability
from bowhead_core.chat_reference_execution import (
    ReferencePrimaryInput,
    ReferencePrimaryWorkerClaim,
    _claim_reference_primary_for_worker,
    read_reference_skill_primary_in_connection,
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
from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    ContentHash,
    NodeId,
    ReferenceNode,
    SkillResourceRef,
    decode_reference_json,
    requested_reference_operation,
)
from bowhead_core.skill_baseline_material import PreparedSkillBaseline, prepare_baseline_selected_reads, restore_skill_baseline
from bowhead_core.skill_extension_material import PreparedSkillExtension, prepare_skill_extension, restore_skill_extension
from bowhead_core.skill_read_extensions import (
    SKILL_EXTENSION_RUNTIME_POLICY,
    primary_extension_policy,
    skill_read_provenance,
)
from bowhead_core.skill_read_primary import prepare_reference_primary_skill_reads
from bowhead_core.skill_read_receiver import (
    PRIMARY_BASELINE_RUNTIME_POLICY,
    SKILL_BASELINE_RUNTIME_POLICY,
    PreparedReceiverSkillReads,
    SkillReadReceiverBaseline,
    primary_skill_read_receiver,
    require_initial_skill_invocation_baseline,
    require_initial_skill_read_baseline,
)
from bowhead_core.skill_read_render import RenderedSkillReads, render_prepared_skill_reads
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from bowhead_core.skill_read_snapshot import PreparedSkillReadSnapshot, prepare_skill_read_snapshot, restore_skill_read_snapshot

SKILL_COMPOSITION_CONTENT_KEY = "reference:primary:skill-closure"
SKILL_BASELINE_CONTENT_KEY = "reference:primary:skill-baseline"
SKILL_EXTENSION_CONTENT_KEY = "reference:primary:skill-extension"


class SkillCompositionMember(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    resource: SkillResourceRef
    node_id: NodeId | None = None
    operation: Literal["read"] = "read"
    provenance: Literal["baseline", "readonly_extension"] = "baseline"
    content_hash: ContentHash
    materialization_hash: ContentHash


class ReferenceCapabilityComposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    resource: CapabilityResourceRef
    node_id: NodeId
    operation: Literal["execute"] = "execute"
    implementation_ref: str
    implementation_digest: ContentHash
    output_contract: Literal["visual.artifact.v1"]
    policy_digest: ContentHash

    @classmethod
    def from_prepared(cls, prepared: PreparedReferenceCapability) -> ReferenceCapabilityComposition:
        return cls(
            resource=prepared.resource,
            node_id=prepared.node_id,
            implementation_ref=prepared.implementation_ref,
            implementation_digest=prepared.implementation_digest,
            output_contract=prepared.output_contract,
            policy_digest=prepared.policy_digest,
        )


class ReferenceSkillPrimaryComposition(ReferencePrimaryComposition):
    runtime_policy: Literal[
        "initial-primary-skill-read.v1",
        "initial-primary-skill-read.v2",
        "initial-primary-skill-baseline.v1",
        "initial-primary-baseline.v1",
        "initial-primary-skill-extension.v1",
    ] = "initial-primary-skill-read.v1"
    receiver_status: Literal["admitted"] = "admitted"
    receiver_digest: ContentHash
    parameter_source: Literal["initial-source.v1"] = "initial-source.v1"
    render_contract: Literal["skill-read-json.v1"] = "skill-read-json.v1"
    preparation_digest: ContentHash
    selection_digest: ContentHash
    rendered_content_hash: ContentHash
    content: ContentDescriptor
    members: tuple[SkillCompositionMember, ...]
    capability: ReferenceCapabilityComposition | None = None
    extension_digest: ContentHash | None = None
    extension_policy_digest: ContentHash | None = None

    @model_validator(mode="after")
    def extension_contract(self):
        active = self.runtime_policy == SKILL_EXTENSION_RUNTIME_POLICY
        if active:
            if (
                self.extension_digest is None
                or self.extension_policy_digest is None
                or not any(m.provenance == "readonly_extension" for m in self.members)
            ):
                raise ValueError("reference_extension_composition_invalid")
        elif self.extension_digest is not None or self.extension_policy_digest is not None or any(m.provenance != "baseline" for m in self.members):
            raise ValueError("reference_extension_composition_invalid")
        return self


def _original_receiver(primary: ReferencePrimaryInput, scope: ReferenceScope) -> SkillReadReceiverBaseline:
    # The composition records the original preparation status explicitly. The
    # immutable definition/source supply all other fields; current ownership is
    # checked independently. Never mutate a running binding or old source proof.
    original = replace(primary, binding=replace(primary.binding, status="admitted"))
    return primary_skill_read_receiver(original, ReferenceRequestScope(**scope.model_dump(exclude={"policy_epoch"})), primary.source.turn_id)


def _composition(
    primary: ReferencePrimaryInput, prepared: PreparedReceiverSkillReads, scope: ReferenceScope
) -> tuple[ReferenceSkillPrimaryComposition, PreparedSkillReadSnapshot | PreparedSkillBaseline | PreparedSkillExtension, RenderedSkillReads]:
    document = primary.source.input_document
    if document is None or _original_receiver(primary, scope) != prepared.receiver:
        raise ValueError("reference_skill_receiver_changed")
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in nodes if isinstance(node.resource, AgentResourceRef)]
    skills = [node for node in nodes if isinstance(node.resource, SkillResourceRef)]
    capabilities = [node for node in nodes if isinstance(node.resource, CapabilityResourceRef)]
    automatic = primary.automatic_receiver is not None
    capability = prepare_reference_capability(document, prepared.receiver.baseline)
    if (
        len(agents) != (0 if automatic else 1)
        or any(agent.parameters for agent in agents)
        or len(skills) > 3
        or len(capabilities) > 1
        or (automatic and (not skills and not capabilities or prepared.baseline is None))
        or (not skills and prepared.baseline is None)
        or len(nodes) != len(skills) + len(capabilities) + len(agents)
        or len(primary.plan.admissions) != len(nodes)
        or any(
            (item.node_id, item.resource, item.operation)
            != (
                node.node_id,
                node.resource,
                requested_reference_operation(node.resource),
            )
            for item, node in zip(primary.plan.admissions, nodes, strict=True)
        )
        or (capability is None) != (not capabilities)
        or capability is not None
        and next(item.policy_digest for item in primary.plan.admissions if item.node_id == capability.node_id) != capability.policy_digest
    ):
        raise ValueError("reference_composition_unavailable")
    receiver = _primary_baseline_composition(primary, None if automatic else agents[0])
    rendered = render_prepared_skill_reads(prepared, skills)
    if prepared.extension is not None:
        snapshot = prepare_skill_extension(prepared)
        descriptor = ContentDescriptor(
            schema_version=1,
            content_key=SKILL_EXTENSION_CONTENT_KEY,
            node_id=receiver.node_id,
            receiver_source=receiver.receiver_source,
            resource=receiver.resource.model_dump(mode="python"),
            content_kind="skill_extension",
            materialization_hash=prepared.receiver.digest,
            content_hash=snapshot.content_hash,
            byte_count=len(snapshot.content),
        )
    elif prepared.baseline is not None:
        # The selected closure is a projection of these exact original packages.
        # It cannot carry an independently captured or caller-modified method.
        baseline = restore_skill_baseline(prepared.baseline.content, receiver=prepared.receiver, content_hash=prepared.baseline.content_hash)
        if baseline != prepared.baseline:
            raise ValueError("reference_skill_baseline_changed")
        for item in prepared.closure.items:
            package = next((package for package in baseline.packages if package.proof == item.proof), None)
            if package is None or package.material.selected_read() != item.material:
                raise ValueError("reference_skill_baseline_changed")
        snapshot = baseline
        descriptor = ContentDescriptor(
            schema_version=1,
            content_key=SKILL_BASELINE_CONTENT_KEY,
            node_id=receiver.node_id,
            receiver_source=receiver.receiver_source,
            resource=receiver.resource.model_dump(mode="python"),
            content_kind="skill_baseline",
            materialization_hash=prepared.receiver.digest,
            content_hash=baseline.content_hash,
            byte_count=len(baseline.content),
        )
    else:
        snapshot = prepare_skill_read_snapshot(prepared)
        descriptor = snapshot.descriptor(node_id=skills[0].node_id, content_key=SKILL_COMPOSITION_CONTENT_KEY)
    selected = {(node.resource.source_kind, node.resource.id): node.node_id for node in skills}
    value = receiver.model_dump(mode="python")
    value.update(
        runtime_policy=(
            SKILL_EXTENSION_RUNTIME_POLICY
            if prepared.extension is not None
            else PRIMARY_BASELINE_RUNTIME_POLICY
            if not skills
            else SKILL_BASELINE_RUNTIME_POLICY
            if prepared.baseline is not None and any("invoke" in grant.permissions for grant in prepared.receiver.grants)
            else "initial-primary-skill-read.v2"
            if prepared.baseline is not None
            else "initial-primary-skill-read.v1"
        ),
        extension_digest=prepared.extension.digest if prepared.extension else None,
        extension_policy_digest=prepared.extension.policy.digest if prepared.extension else None,
        receiver_digest=prepared.receiver.digest,
        preparation_digest=prepared.preparation_digest,
        selection_digest=rendered.selection_digest,
        rendered_content_hash=rendered.rendered_content_hash,
        content=descriptor,
        capability=ReferenceCapabilityComposition.from_prepared(capability) if capability is not None else None,
        members=tuple(
            SkillCompositionMember(
                resource=item.proof.resource,
                provenance=skill_read_provenance(prepared.receiver.grants, item.proof.resource, prepared.extension.policy)
                if prepared.extension
                else "baseline",
                node_id=selected.get((item.proof.resource.source_kind, item.proof.resource.id)),
                content_hash=item.material.content_hash,
                materialization_hash=item.material.materialization_hash,
            )
            for item in prepared.closure.items
        ),
    )
    composition = ReferenceSkillPrimaryComposition.model_validate(value)
    if len(composition.canonical_json().encode()) > MAX_REFERENCE_STORAGE_BYTES:
        raise ValueError("reference_composition_material_limit")
    return composition, snapshot, rendered


async def restore_skill_primary_composition_in_connection(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    scope: ReferenceScope,
    generation: int,
    cipher: ReferenceCipher,
) -> tuple[ReferenceSkillPrimaryComposition, PreparedReceiverSkillReads, RenderedSkillReads]:
    """Restore retained identity; caller must separately authorize current use.

    `primary` comes from the canonical transaction reader. An already-running
    retry uses this only to verify the original pointer, never to launch again.
    """
    _transaction(db)
    row = await _row(db, "SELECT * FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (primary.source.turn_id,))
    if row is None:
        raise ValueError("reference_composition_unavailable")
    try:
        decode_reference_json(row["composition_json"], max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20)
        saved = ReferenceSkillPrimaryComposition.model_validate_json(row["composition_json"], strict=True)
        retained = await ChatReferenceStore(cipher).get_encrypted_content_in_connection(
            db,
            scope=scope,
            turn_id=primary.source.turn_id,
            content_key=SKILL_EXTENSION_CONTENT_KEY
            if saved.runtime_policy == SKILL_EXTENSION_RUNTIME_POLICY
            else SKILL_BASELINE_CONTENT_KEY
            if saved.runtime_policy in {"initial-primary-skill-read.v2", SKILL_BASELINE_RUNTIME_POLICY, PRIMARY_BASELINE_RUNTIME_POLICY}
            else SKILL_COMPOSITION_CONTENT_KEY,
        )
        if retained is None:
            raise ValueError("missing content")
        descriptor, ciphertext = retained
        content = cipher.open(scope=scope, purpose="content", identity=f"{primary.source.turn_id}/{descriptor.content_key}", ciphertext=ciphertext)
        receiver = _original_receiver(primary, scope)
        if saved.runtime_policy == SKILL_EXTENSION_RUNTIME_POLICY:
            prepared = restore_skill_extension(content, receiver=receiver, content_hash=descriptor.content_hash)
            if prepared.extension != await primary_extension_policy(db, primary, receiver):
                raise ValueError("reference_skill_extension_changed")
        elif saved.runtime_policy in {"initial-primary-skill-read.v2", SKILL_BASELINE_RUNTIME_POLICY, PRIMARY_BASELINE_RUNTIME_POLICY}:
            baseline = restore_skill_baseline(content, receiver=receiver, content_hash=descriptor.content_hash)
            prepared = await prepare_baseline_selected_reads(
                baseline,
                [
                    node.resource
                    for node in primary.source.input_document.nodes
                    if isinstance(node, ReferenceNode) and isinstance(node.resource, SkillResourceRef)
                ],
            )
        else:
            prepared = restore_skill_read_snapshot(content, descriptor=descriptor, receiver=receiver)
        expected, _, rendered = _composition(primary, prepared, scope)
        if (
            type(generation) is not int
            or generation < 1
            or row["created_generation"] != 1
            or row["execution_id"] != expected.execution_id
            or row["source_turn_id"] != primary.source.turn_id
            or row["kind"] != "composition"
            or row["schema_version"] != 1
            or row["idempotency_key"] != "reference:primary"
            or saved != expected
            or row["composition_json"] != expected.canonical_json()
            or row["composition_digest"] != expected.digest
        ):
            raise ValueError("changed composition")
        if generation != 1:
            from bowhead_core.chat_reference_continuation import require_resume

            await require_resume(db, turn_id=primary.source.turn_id, generation=generation)
        return expected, prepared, rendered
    except Exception:
        raise ValueError("reference_composition_changed") from None


@dataclass(frozen=True, slots=True)
class SkillReferenceWorkerPreparation:
    """Internal bridge into the shared claim transaction, never wire input."""

    sources: SkillReadSourceResolver = field(repr=False)
    cipher: ReferenceCipher = field(repr=False)
    prepared: PreparedReceiverSkillReads | None = field(repr=False)

    async def require_existing(self, db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, scope: ReferenceScope, generation: int) -> None:
        await restore_skill_primary_composition_in_connection(db, primary=primary, scope=scope, generation=generation, cipher=self.cipher)
        from bowhead_core.chat_reference_capability import require_reference_capability_execution_in_connection

        await require_reference_capability_execution_in_connection(db, primary=primary, generation=generation)

    async def revalidate(self, db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, scope: ReferenceScope) -> None:
        if self.prepared is None or primary.binding.status != "admitted":
            raise ValueError("reference_skill_receiver_changed")
        if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_skill_composition_material'", ()) is None:
            raise ValueError("reference_skill_composition_migration_required")
        if (
            self.prepared.baseline is None
            or await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_skill_baseline_composition'", ()) is None
        ):
            raise ValueError("reference_skill_baseline_migration_required")
        _composition(primary, self.prepared, scope)
        if prepare_reference_capability(primary.source.input_document, self.prepared.receiver.baseline) is not None:
            if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_capability_composition'", ()) is None:
                raise ValueError("reference_capability_migration_required")
        if self.prepared.extension is not None:
            if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_extension_epoch_check'", ()) is None:
                raise ValueError("reference_skill_extension_migration_required")
            if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_skill_extension_composition'", ()) is None:
                raise ValueError("reference_skill_extension_migration_required")
            if self.prepared.extension != await primary_extension_policy(db, primary, self.prepared.receiver):
                raise ValueError("reference_skill_extension_changed")
            from bowhead_core.skill_read_extensions import revalidate_extension_sources

            await revalidate_extension_sources(db, self.sources, self.prepared)
        if (
            not self.prepared.closure.roots
            and await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_primary_baseline_composition'", ()) is None
        ):
            raise ValueError("reference_primary_baseline_migration_required")
        if any("invoke" in grant.permissions for grant in self.prepared.receiver.grants):
            if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_skill_invocation_composition'", ()) is None:
                raise ValueError("reference_skill_invocation_migration_required")
            require_initial_skill_invocation_baseline(self.prepared.receiver, self.prepared.closure, self.prepared.baseline)
        else:
            require_initial_skill_read_baseline(self.prepared.receiver, self.prepared.closure, self.prepared.baseline)
        for item in self.prepared.baseline.packages:
            await self.sources.revalidate_in_connection(db, item.proof)

    async def persist(
        self, db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, scope: ReferenceScope, generation: int, owner_id: str
    ) -> None:
        if self.prepared is None:
            raise ValueError("reference_skill_receiver_changed")
        composition, snapshot, _ = _composition(primary, self.prepared, scope)
        plan = await _row(db, "SELECT expires_at FROM chat_reference_plans WHERE turn_id=?", (primary.source.turn_id,))
        await ChatReferenceStore(self.cipher).put_content_in_connection(
            db,
            scope=scope,
            turn_id=primary.source.turn_id,
            descriptor=composition.content,
            content=snapshot.content,
            generation=generation,
            owner_id=owner_id,
            expires_at=plan["expires_at"],
        )
        await _put_primary_composition_in_connection(db, composition=composition, generation=generation)
        from bowhead_core.chat_reference_capability import start_reference_capability_execution_in_connection

        await start_reference_capability_execution_in_connection(db, primary=primary, generation=generation)
        await require_reference_worker_in_connection(db, scope=scope, turn_id=primary.source.turn_id, owner_id=owner_id, generation=generation)


async def claim_reference_skill_primary_for_worker(
    *,
    sources: SkillReadSourceResolver,
    cipher: ReferenceCipher,
    scope: ReferenceRequestScope,
    turn_id: str,
    owner_id: str,
    expected_generation: int,
    lease_seconds: int = 30,
) -> ReferencePrimaryWorkerClaim:
    """Prepare outside the writer lock; retain materials and claim atomically.

    Only `claimed` may proceed to the future mixed scheduler. `already_running`
    neither renews ownership nor authorizes any use, even for the same owner.
    """
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    bindings = AgentExecutionBindingStore(sources.db_path)
    async with open_shared_ctx(sources.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            epoch = await require_reference_request_scope(db, scope)
            stored_scope = ReferenceScope(**scope.model_dump(), policy_epoch=epoch)
            primary = await read_reference_skill_primary_in_connection(db, scope=stored_scope, turn_id=turn_id, bindings=bindings)
        finally:
            await db.rollback()
    prepared = None
    if primary.binding.status == "admitted":
        prepared, _ = await prepare_reference_primary_skill_reads(sources, scope=scope, turn_id=turn_id)
    return await _claim_reference_primary_for_worker(
        scope=stored_scope,
        turn_id=turn_id,
        owner_id=owner_id,
        expected_generation=expected_generation,
        bindings=bindings,
        lease_seconds=lease_seconds,
        skills=SkillReferenceWorkerPreparation(sources, cipher, prepared),
    )
