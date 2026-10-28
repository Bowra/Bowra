"""Read-only optional methods, separate from an Agent's immutable Skill baseline.

Published policy is only one intersection. Canonical primary/current source
authorization, complete closure, durable composition and live worker ownership
must still be checked at their own boundaries. No executable package is captured
for an extension and no caller receives an invoke grant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, NoReturn

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.agent_runtime_extension_store import read_published_extension_policy_in_connection
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensions
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceScope, _row
from bowhead_core.chat_references import ContentHash, Identity, SkillResourceRef, decode_reference_json
from bowhead_core.skill_read_closure import PreparedSkillReadClosure, prepare_skill_read_closure, snapshot_prepared_skill_read_closure
from bowhead_core.skill_read_receiver import (
    PreparedReceiverSkillReads,
    SkillReadReceiverBaseline,
    SkillReadReceiverError,
    primary_skill_read_receiver,
)
from bowhead_core.skill_read_resolution import ResolvedSkillRead, SkillReadSourceProof, SkillReadSourceResolver

if TYPE_CHECKING:
    from bowhead_core.chat_reference_execution import ReferencePrimaryInput
    from bowhead_core.skill_baseline_material import PreparedSkillBaseline

SKILL_EXTENSION_RUNTIME_POLICY = "initial-primary-skill-extension.v1"


def _fail() -> NoReturn:
    raise SkillReadReceiverError("reference_skill_extension_denied") from None


class SkillReadExtensionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    receiver_digest: ContentHash
    policy: AgentRuntimeExtensions = Field(repr=False)

    @property
    def digest(self) -> str:
        return canonical_fingerprint({"schema_version": 1, "receiver_digest": self.receiver_digest, "policy_digest": self.policy.digest})


async def published_extension_policy(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, revision_ref: str, release_id: str | None, definition: Mapping
) -> AgentRuntimeExtensions | None:
    """Read after canonical Agent access checks, including the exact Audience."""
    policy = await read_published_extension_policy_in_connection(
        db, team_id=team_id, agent_id=agent_id, revision_ref=revision_ref, definition=definition
    )
    if policy is None:
        return None
    async with db.execute(
        "SELECT audience_snapshot_json FROM team_subagent_release WHERE team_id=? AND subagent_id=? AND revision_id=? AND id=?",
        (team_id, agent_id, revision_ref, release_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        _fail()
    try:
        audience = json.loads(str(row[0]))
        mode = audience["effective_mode"]
        if mode not in {"private", "custom", "teamwide"}:
            _fail()
        policy.require_private_audience(mode)
    except (KeyError, TypeError, ValueError):
        _fail()
    return policy


async def primary_extension_policy(
    db: aiosqlite.Connection, primary: ReferencePrimaryInput, receiver: SkillReadReceiverBaseline
) -> SkillReadExtensionPolicy | None:
    binding = primary.binding
    if binding.agent_source_kind != "team":
        return None
    original = replace(primary, binding=replace(binding, status=receiver.status))
    if primary_skill_read_receiver(original, receiver.scope, receiver.turn_id) != receiver:
        _fail()
    policy = await published_extension_policy(
        db,
        team_id=binding.team_id,
        agent_id=binding.agent_id,
        revision_ref=binding.revision_ref,
        release_id=binding.release_id,
        definition=binding.definition,
    )
    return SkillReadExtensionPolicy(receiver_digest=receiver.digest, policy=policy) if policy is not None else None


def skill_read_provenance(
    grants: tuple[ExactSkillGrant, ...], resource: SkillResourceRef, policy: AgentRuntimeExtensions | None
) -> Literal["baseline", "readonly_extension"]:
    original = next((item for item in grants if (item.kind, item.id) == (resource.source_kind, resource.id)), None)
    if original is not None:
        # An optional selector cannot replace a baseline's fixed version or
        # reinterpret a missing operation as temporary permission.
        if original.revision != resource.version or "read" not in original.permissions:
            _fail()
        return "baseline"
    if policy is None or not policy.skill_read.permits(resource):
        _fail()
    return "readonly_extension"


def require_extension_roots(
    grants: tuple[ExactSkillGrant, ...], resources: Sequence[SkillResourceRef], policy: AgentRuntimeExtensions | None
) -> bool:
    extensions = [resource for resource in resources if skill_read_provenance(grants, resource, policy) == "readonly_extension"]
    if extensions:
        if policy is None:
            _fail()
        policy.require_selection(extensions)
    return bool(extensions)


def require_extension_member_name(grants: tuple[ExactSkillGrant, ...], resource: SkillResourceRef, name: str) -> None:
    for grant in grants:
        same_identity = (grant.kind, grant.id) == (resource.source_kind, resource.id)
        if (same_identity and grant.name != name) or (not same_identity and grant.name == name):
            _fail()


def validate_extension_closure(receiver: SkillReadReceiverBaseline, closure: PreparedSkillReadClosure, extension: SkillReadExtensionPolicy) -> None:
    snapshot_prepared_skill_read_closure(closure)
    if receiver.agent_source_kind != "team" or extension.receiver_digest != receiver.digest or closure.scope != receiver.scope:
        _fail()
    require_extension_roots(receiver.grants, closure.roots, extension.policy)
    names: dict[str, SkillResourceRef] = {}
    has_extensions = False
    for item in closure.items:
        if item.proof.scope != receiver.scope:
            _fail()
        provenance = skill_read_provenance(receiver.grants, item.proof.resource, extension.policy)
        has_extensions |= provenance == "readonly_extension"
        require_extension_member_name(receiver.grants, item.proof.resource, item.proof.name)
        if item.proof.name in names and names[item.proof.name] != item.proof.resource:
            _fail()
        names[item.proof.name] = item.proof.resource
    if not has_extensions:
        _fail()


class SkillExtensionPolicyCheck(BaseModel):
    """Safe evidence of which live policy constrained one original selection."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: StrictInt = Field(default=2, ge=1, le=2)
    current_access_epoch: StrictInt | None = Field(default=None, ge=0)
    receiver_digest: ContentHash
    original_extension_digest: ContentHash
    current_revision_ref: Identity
    current_release_id: Identity
    current_definition_hash: ContentHash
    current_extension_policy_digest: ContentHash
    closure_digest: ContentHash

    @model_validator(mode="after")
    def epoch_contract(self):
        if (self.schema_version == 2) != (self.current_access_epoch is not None):
            raise ValueError("reference_extension_policy_check_invalid")
        return self

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.model_dump(mode="json", exclude_none=True))


async def require_current_extension_policy(
    db: aiosqlite.Connection, receiver: SkillReadReceiverBaseline, closure: PreparedSkillReadClosure, extension: SkillReadExtensionPolicy
) -> SkillExtensionPolicyCheck:
    """Intersect the original choice with the currently published tighter scope.

    Never substitute the new revision's prompt or baseline for the original
    execution. Working copies cannot tighten or expand runtime permission. The
    caller records this returned source/hash with the action's durable evidence.
    """
    from bowhead_core.agent_execution_binding import team_agent_dispatchable_state_sql

    if not db.in_transaction or receiver.agent_source_kind != "team":
        _fail()
    validate_extension_closure(receiver, closure, extension)
    async with db.execute(
        "SELECT r.id,l.id,r.definition_hash,r.definition_json FROM team_subagent a "
        "JOIN team_subagent_release l ON l.team_id=a.team_id AND l.subagent_id=a.id AND l.id=a.live_release_id "
        "JOIN team_subagent_revision r ON r.team_id=a.team_id AND r.subagent_id=a.id AND r.id=l.revision_id AND r.id=a.live_revision_id "
        "WHERE a.team_id=? AND a.id=? AND " + team_agent_dispatchable_state_sql("a"),
        (receiver.scope.team_id, receiver.agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        _fail()
    definition = decode_reference_json(str(row[3]), max_bytes=1024 * 1024)
    current = await published_extension_policy(
        db, team_id=receiver.scope.team_id, agent_id=receiver.agent_id, revision_ref=str(row[0]), release_id=str(row[1]), definition=definition
    )
    if current is None or not current.skill_read.enabled:
        _fail()
    require_extension_roots(receiver.grants, closure.roots, current)
    for item in closure.items:
        if skill_read_provenance(receiver.grants, item.proof.resource, extension.policy) == "readonly_extension":
            if not current.skill_read.permits(item.proof.resource):
                _fail()
    return SkillExtensionPolicyCheck(
        current_access_epoch=await require_reference_request_scope(db, receiver.scope),
        receiver_digest=receiver.digest,
        original_extension_digest=extension.digest,
        current_revision_ref=str(row[0]),
        current_release_id=str(row[1]),
        current_definition_hash=str(row[2]),
        current_extension_policy_digest=current.digest,
        closure_digest=closure.closure_digest,
    )


class ExtensionReadSources:
    """Current source preparation with a separate finite optional-policy fence."""

    def __init__(
        self,
        sources: SkillReadSourceResolver,
        *,
        scope: ReferenceRequestScope,
        grants: tuple[ExactSkillGrant, ...],
        policy: AgentRuntimeExtensions,
        baseline: PreparedSkillBaseline | None = None,
    ):
        self.sources, self.scope, self.grants, self.policy, self.baseline = sources, scope, grants, policy, baseline

    def require(self, resource: SkillResourceRef, scope: ReferenceRequestScope) -> str:
        if scope != self.scope:
            _fail()
        return skill_read_provenance(self.grants, resource, self.policy)

    async def prepare(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillRead:
        provenance = self.require(resource, scope)
        if provenance == "baseline" and self.baseline is not None:
            package = next((item for item in self.baseline.packages if item.proof.resource == resource), None)
            if package is None:
                _fail()
            resolved = ResolvedSkillRead(package.proof, package.material.selected_read())
        else:
            resolved = await self.sources.prepare(resource, scope=scope, require_declaration=provenance == "readonly_extension")
        require_extension_member_name(self.grants, resource, resolved.proof.name)
        return resolved

    async def revalidate(self, proof: SkillReadSourceProof) -> None:
        self.require(proof.resource, proof.scope)
        require_extension_member_name(self.grants, proof.resource, proof.name)
        await self.sources.revalidate(proof)


async def prepare_extended_skill_reads(
    sources: SkillReadSourceResolver, baseline: PreparedSkillBaseline, resources: Sequence[SkillResourceRef], extension: SkillReadExtensionPolicy
) -> PreparedReceiverSkillReads:
    receiver = baseline.receiver
    if extension.receiver_digest != receiver.digest:
        _fail()
    require_extension_roots(receiver.grants, resources, extension.policy)
    closure = await prepare_skill_read_closure(
        ExtensionReadSources(sources, scope=receiver.scope, grants=receiver.grants, policy=extension.policy, baseline=baseline),
        resources,
        scope=receiver.scope,
    )
    if all(skill_read_provenance(receiver.grants, item.proof.resource, extension.policy) == "baseline" for item in closure.items):
        return PreparedReceiverSkillReads(receiver, closure, baseline)
    validate_extension_closure(receiver, closure, extension)
    return PreparedReceiverSkillReads(receiver, closure, baseline, extension)


async def record_extension_invocation_check(
    db: aiosqlite.Connection, *, prepared: PreparedReceiverSkillReads, composition_digest: str, invocation: dict
) -> None:
    if prepared.extension is None:
        _fail()
    checked = await require_current_extension_policy(db, prepared.receiver, prepared.closure, prepared.extension)
    values = {key: invocation[key] for key in ("invocation_id", "turn_id", "context_id", "execution_id", "created_generation", "created_at")}
    values.update(
        composition_digest=composition_digest,
        policy_check_json=json.dumps(checked.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":")),
        policy_check_digest=checked.digest,
    )
    await db.execute(
        "INSERT INTO chat_reference_extension_checks (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
        tuple(values.values()),
    )


async def current_extension_scope(db: aiosqlite.Connection, scope: ReferenceScope, turn_id: str) -> ReferenceScope:
    """Choose a current read scope only for a retained extension composition.

    This lookup grants nothing. The caller must restore the canonical primary,
    material and current sources/policy and check worker ownership before use.
    Existing contracts retain their strict epoch fence. No saved scope changes.
    """
    if not db.in_transaction:
        _fail()
    row = await _row(
        db,
        "SELECT 1 FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1 AND json_extract(composition_json,'$.runtime_policy')=?",
        (turn_id, SKILL_EXTENSION_RUNTIME_POLICY),
    )
    if row is None:
        return scope
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_extension_epoch_check'", ()) is None:
        _fail()
    epoch = await require_reference_request_scope(db, ReferenceRequestScope(**scope.identity()))
    if epoch < scope.policy_epoch:
        _fail()
    return scope.model_copy(update={"policy_epoch": epoch})


async def revalidate_extension_sources(db: aiosqlite.Connection, sources: SkillReadSourceResolver, prepared: PreparedReceiverSkillReads) -> None:
    if prepared.extension is None or prepared.baseline is None:
        _fail()
    await require_current_extension_policy(db, prepared.receiver, prepared.closure, prepared.extension)
    for item in (*prepared.baseline.packages, *prepared.closure.items):
        await sources.revalidate_retained_in_connection(db, item.proof)
