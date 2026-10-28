"""Narrow read-only preparation to an already bound primary Agent's exact baseline.

The factory is a trusted canonical primary reader, never a client snapshot. This
produces preparation evidence only: source intent, durable composition, current
worker generation and actual model injection remain separate admission gates.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NoReturn

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, canonical_fingerprint
from bowhead_core.agent_dependency_compiler import ExactSkillGrant, parse_exact_skill_grants
from bowhead_core.chat_reference_execution import ReferencePrimaryInput
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import ContentHash
from bowhead_core.chat_references import Identity, SkillResourceRef, _bounded_json
from bowhead_core.skill_read_closure import MAX_SELECTED_SKILLS, PreparedSkillReadClosure, SkillReadClosureError, prepare_skill_read_closure
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_resolution import ResolvedSkillRead, SkillReadResolutionError, SkillReadSourceProof, SkillReadSourceResolver

PrimaryFactory = Callable[[], Awaitable[ReferencePrimaryInput]]

if TYPE_CHECKING:
    from bowhead_core.skill_baseline_material import PreparedSkillBaseline
    from bowhead_core.skill_read_extensions import SkillReadExtensionPolicy


class SkillReadReceiverError(SkillReadResolutionError):
    """Finite internal receiver error with no hidden grant names or definition."""


def _fail(code: str = "reference_skill_receiver_unavailable") -> NoReturn:
    raise SkillReadReceiverError(code) from None


class SkillReadReceiverBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    scope: ReferenceRequestScope
    turn_id: Identity
    source_hash: ContentHash
    execution_id: Identity
    agent_id: Identity
    agent_source_kind: Literal["system", "team"]
    revision_ref: Identity
    release_id: Identity | None
    status: Literal["admitted", "running"]
    capability_fingerprint: Identity
    policy_fingerprint: ContentHash
    baseline: AgentBehaviorDescriptor = Field(repr=False)
    grants: tuple[ExactSkillGrant, ...] = Field(repr=False)

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.model_dump(mode="json"))

    def require_skill_read(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ExactSkillGrant:
        """Match this exact baseline only; current source use is checked separately."""
        grant = next((grant for grant in self.grants if (grant.kind, grant.id) == (resource.source_kind, resource.id)), None)
        if scope != self.scope or grant is None or grant.revision != resource.version or "read" not in grant.permissions:
            _fail("reference_skill_baseline_required")
        return grant


@dataclass(frozen=True, slots=True)
class PreparedReceiverSkillReads:
    receiver: SkillReadReceiverBaseline
    closure: PreparedSkillReadClosure = field(repr=False)
    baseline: PreparedSkillBaseline | None = field(default=None, repr=False)
    extension: SkillReadExtensionPolicy | None = field(default=None, repr=False)

    @property
    def preparation_digest(self) -> str:
        if self.extension is not None:
            return canonical_fingerprint(
                {"schema_version": 2, "receiver": self.receiver.digest, "closure": self.closure.closure_digest, "extension": self.extension.digest}
            )
        return canonical_fingerprint({"schema_version": 1, "receiver": self.receiver.digest, "closure": self.closure.closure_digest})


SKILL_BASELINE_RUNTIME_POLICY = "initial-primary-skill-baseline.v1"
PRIMARY_BASELINE_RUNTIME_POLICY = "initial-primary-baseline.v1"
INVOKABLE_BASELINE_RUNTIME_POLICIES = frozenset(
    {SKILL_BASELINE_RUNTIME_POLICY, PRIMARY_BASELINE_RUNTIME_POLICY, "initial-primary-skill-extension.v1"}
)


def require_supported_skill_grants(grants: tuple[ExactSkillGrant, ...]) -> None:
    # Read-only v1/v2 keep their existing distribution contract. Invocable
    # baselines require the actual current-source adapter for every dependency.
    if any("invoke" in grant.permissions for grant in grants) and any(grant.distribution != "reference" for grant in grants):
        _fail("reference_skill_tool_baseline_unavailable")


def require_initial_skill_invocation_baseline(
    receiver: SkillReadReceiverBaseline, closure: PreparedSkillReadClosure, baseline: PreparedSkillBaseline | None
) -> None:
    require_supported_skill_grants(receiver.grants)
    if baseline is None or baseline.receiver != receiver:
        _fail("reference_skill_tool_baseline_unavailable")
    retained = {(item.proof.resource.source_kind, item.proof.resource.id, item.proof.resource.version) for item in baseline.packages}
    if any((grant.kind, grant.id, grant.revision) not in retained for grant in receiver.grants):
        _fail("reference_skill_tool_baseline_unavailable")


def require_read_only_skill_grants(receiver: SkillReadReceiverBaseline) -> None:
    if any(grant.permissions != ("read",) for grant in receiver.grants):
        _fail("reference_skill_tool_baseline_unavailable")


def require_initial_skill_read_baseline(
    receiver: SkillReadReceiverBaseline, closure: PreparedSkillReadClosure, baseline: PreparedSkillBaseline | None = None
) -> None:
    """Require a baseline that the initial read-only executor can preserve.

    Preparation alone may inspect valid broader baselines. Acceptance and first
    claim must reject unsupported ones before owning any work. Never remove
    grants or add unselected methods to mandatory model input to make it pass.
    Current source permission and exact member/name checks remain separate.
    """
    require_read_only_skill_grants(receiver)
    if baseline is not None and baseline.receiver != receiver:
        _fail("reference_skill_tool_baseline_unavailable")
    members = baseline.packages if baseline is not None else closure.items
    retained = {(item.proof.resource.source_kind, item.proof.resource.id, item.proof.resource.version) for item in members}
    if any((grant.kind, grant.id, grant.revision) not in retained for grant in receiver.grants):
        _fail("reference_skill_tool_baseline_unavailable")


def primary_skill_read_receiver(primary: ReferencePrimaryInput, scope: ReferenceRequestScope, turn_id: str) -> SkillReadReceiverBaseline:
    source, binding = primary.source, primary.binding
    if scope.session_id is None or (source.user_id, source.workspace_id, source.session_id, source.turn_id, binding.team_id, binding.turn_id) != (
        scope.user_id,
        scope.workspace_id,
        scope.session_id,
        turn_id,
        scope.team_id,
        turn_id,
    ):
        _fail("reference_skill_receiver_scope_mismatch")
    _bounded_json(binding.definition, max_depth=20, max_bytes=1024 * 1024)
    # Detach the mutable binding dictionary before any later await.
    definition = json.loads(json.dumps(binding.definition, ensure_ascii=False, allow_nan=False))
    if binding.behavior_descriptor is None:
        _fail()
    baseline = AgentBehaviorDescriptor.model_validate(binding.behavior_descriptor.model_dump(mode="json"))
    # The descriptor hashes the authored definition. A team Agent with Plugins runs the
    # Plugin-expanded definition, which its binding carries beside the authored one.
    authored = binding.authored_definition if binding.authored_definition is not None else definition
    if baseline.source_definition_hash != canonical_fingerprint(authored) or baseline.verification_state == "blocked":
        _fail()
    # A descriptor can be shadow solely because no Capability implementation is
    # selected. Release/Safety authorization belongs to the canonical reader;
    # descriptor state alone neither grants nor removes exact Skill permissions.
    grants = exact_reference_skill_grants(definition)
    if binding.agent_source_kind == "team" and not binding.release_id:
        _fail()
    return SkillReadReceiverBaseline(
        scope=scope,
        turn_id=turn_id,
        source_hash=primary.plan.source_hash,
        execution_id=binding.execution_id,
        agent_id=binding.agent_id,
        agent_source_kind=binding.agent_source_kind,
        revision_ref=binding.revision_ref,
        release_id=binding.release_id,
        status=binding.status,
        capability_fingerprint=binding.capability_fingerprint,
        policy_fingerprint=binding.policy_fingerprint,
        baseline=baseline,
        grants=grants,
    )


def exact_reference_skill_grants(definition: dict) -> tuple[ExactSkillGrant, ...]:
    """Read published operation grants without inventing a legacy name binding."""
    if definition.get("schema_version") in (2, 3) and "skill_runtime_scope" in definition:
        from bowhead_core.system_skill_snapshot import decode_system_skill_snapshot

        scope, grants = decode_system_skill_snapshot(definition)
        if grants is None or (
            scope is not None
            and (
                scope.read_names != tuple(sorted(grant.name for grant in grants if "read" in grant.permissions))
                or scope.invoke_names != tuple(sorted(grant.name for grant in grants if "invoke" in grant.permissions))
            )
        ):
            # A narrower name scope is a valid ordinary profile. This adapter
            # cannot silently rewrite its frozen grants to claim a full baseline.
            _fail()
        return grants
    for key in ("skills", "skills_json"):
        legacy = definition.get(key)
        if legacy is not None:
            legacy = json.loads(legacy) if key == "skills_json" else legacy
            if legacy is not None and legacy != []:
                _fail()
    return parse_exact_skill_grants(definition.get("skill_grants"))


class _BaselineSources:
    def __init__(self, sources: SkillReadSourceResolver, receiver: SkillReadReceiverBaseline):
        self.sources = sources
        self.receiver = receiver

    def require(self, resource: SkillResourceRef, scope: ReferenceRequestScope) -> ExactSkillGrant:
        return self.receiver.require_skill_read(resource, scope=scope)

    async def prepare(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillRead:
        grant = self.require(resource, scope)
        resolved = await self.sources.prepare(resource, scope=scope)
        if resolved.proof.name != grant.name:
            _fail("reference_skill_baseline_required")
        return resolved

    async def revalidate(self, proof: SkillReadSourceProof) -> None:
        grant = self.require(proof.resource, proof.scope)
        if proof.name != grant.name:
            _fail("reference_skill_baseline_required")
        await self.sources.revalidate(proof)


async def prepare_primary_baseline_skill_reads(
    sources: SkillReadSourceResolver,
    resources: Sequence[SkillResourceRef],
    *,
    scope: ReferenceRequestScope,
    turn_id: str,
    primary_factory: PrimaryFactory,
) -> PreparedReceiverSkillReads:
    """Require every root/dependency in the exact bound read baseline.

    No baseline match means denial, even when the caller owns the resource. An
    explicit published extension policy is not yet implemented by this adapter.
    """
    try:
        if not 1 <= len(resources) <= MAX_SELECTED_SKILLS:
            _fail()
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="json"))
        selected = tuple(SkillResourceRef.model_validate(resource.model_dump(mode="json")) for resource in resources)
        before = primary_skill_read_receiver(await primary_factory(), scope, turn_id)
        narrowed = _BaselineSources(sources, before)
        for resource in selected:
            narrowed.require(resource, scope)
        closure = await prepare_skill_read_closure(narrowed, selected, scope=scope)
        after = primary_skill_read_receiver(await primary_factory(), scope, turn_id)
        if after != before:
            _fail("reference_skill_receiver_changed")
        return PreparedReceiverSkillReads(before, closure)
    except (SkillReadReceiverError, SkillReadResolutionError, SkillReadMaterialError, SkillReadClosureError):
        raise
    except Exception:
        _fail()


async def require_prepared_primary_skill_baseline(
    db: aiosqlite.Connection, primary: ReferencePrimaryInput, closure: PreparedSkillReadClosure, *, sources: SkillReadSourceResolver
) -> None:
    """Check a freshly admitted primary against server-prepared source facts.

    A new-session preflight has session_id=None. The canonical source/binding
    carries the actual session; only that one transition is allowed here. This
    does not rebind or persist the preflight proof/material as a runtime result.
    """
    try:
        scope = ReferenceRequestScope(
            team_id=primary.binding.team_id,
            user_id=primary.source.user_id,
            workspace_id=primary.source.workspace_id,
            session_id=primary.source.session_id,
        )
        receiver = primary_skill_read_receiver(primary, scope, primary.source.turn_id)
        if closure.scope.model_dump(exclude={"session_id"}) != scope.model_dump(exclude={"session_id"}) or closure.scope.session_id not in {
            None,
            scope.session_id,
        }:
            _fail("reference_skill_receiver_scope_mismatch")
        from bowhead_core.skill_read_extensions import (
            primary_extension_policy,
            require_extension_member_name,
            require_extension_roots,
            skill_read_provenance,
        )

        extension = await primary_extension_policy(db, primary, receiver)
        policy = extension.policy if extension is not None else None
        require_extension_roots(receiver.grants, closure.roots, policy)
        has_extensions = False
        for item in closure.items:
            has_extensions |= skill_read_provenance(receiver.grants, item.proof.resource, policy) == "readonly_extension"
            require_extension_member_name(receiver.grants, item.proof.resource, item.proof.name)
            if item.proof.scope != closure.scope:
                _fail("reference_skill_baseline_required")
        if has_extensions:
            async with db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name IN ('trg_reference_skill_extension_composition','trg_reference_extension_epoch_check')"
            ) as cursor:
                if (await cursor.fetchone())[0] != 2:
                    _fail("reference_skill_extension_migration_required")
            for item in closure.items:
                await sources.revalidate_retained_in_connection(db, item.proof)
        require_supported_skill_grants(receiver.grants)
        if not closure.roots:
            async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_primary_baseline_composition'") as cursor:
                if await cursor.fetchone() is None:
                    _fail("reference_primary_baseline_migration_required")
        if any("invoke" in grant.permissions for grant in receiver.grants):
            async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_skill_invocation_composition'") as cursor:
                if await cursor.fetchone() is None:
                    _fail("reference_skill_invocation_migration_required")
        # All baseline reads must be currently usable, including ordinary SOPs
        # without a selected read-only entry. Material capture stays outside the
        # writer lock at claim; no new source grant is created by this check.
        for grant in receiver.grants:
            proof = await sources.describe_in_connection(
                db, SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=grant.revision), scope=scope
            )
            if proof.name != grant.name:
                _fail("reference_skill_baseline_required")
            if has_extensions:
                await sources.revalidate_retained_in_connection(db, proof)
    except SkillReadReceiverError:
        raise
    except Exception:
        _fail()
