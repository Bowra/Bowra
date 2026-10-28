"""Prepare an exact Team or system Agent directly from canonical user input.

This server entry does not accept a runtime profile, behavior fingerprint or
permission ticket from the browser. It prepares bytes outside transactions and
returns an admission candidate; it does not enable an HTTP writer or dispatch.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import anyio

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_admission import ReferenceStartAdmission, reference_start_receiver
from bowhead_core.chat_reference_capability import prepare_reference_capability
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import (
    AgentResourceRef,
    InputDocument,
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
    ReferenceNode,
    SkillResourceRef,
    decode_reference_json,
)
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_mcp_preparation import prepare_reference_mcp_prompt_selection, prepare_reference_mcp_selection
from bowhead_core.skill_read_closure import prepare_skill_read_closure
from bowhead_core.skill_read_extensions import ExtensionReadSources, published_extension_policy, require_extension_roots, skill_read_provenance
from bowhead_core.skill_read_receiver import exact_reference_skill_grants, require_supported_skill_grants
from bowhead_core.skill_read_resolution import ResolvedSkillRead, SkillReadSourceProof, SkillReadSourceResolver

if TYPE_CHECKING:
    from bowhead_core.subagent import SubagentRunner


class ReferenceStartPreparationError(ValueError):
    """Finite preparation failure; no private source or Agent definition."""


# System Agents whose manifest grants are read on demand, as in their delegated runs.
_ON_DEMAND_SKILL_SYSTEM_AGENTS = frozenset({"agent-coach"})


@functools.cache
def _manifest_skill_grant_names(name: str) -> frozenset[str] | None:
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.system_skill_snapshot import parse_system_manifest_skill_grants

    declared = AgentRegistry.bootstrap().get(name).skill_grants
    return None if declared is None else frozenset(grant.name for grant in parse_system_manifest_skill_grants(declared))


def _only_manifest_reference_grants(name: str, definition: dict) -> bool:
    """True when the profile's grants are exactly its system manifest's built-in reference packages."""
    from bowhead_core.system_skill_snapshot import decode_system_skill_snapshot

    try:
        _, grants = decode_system_skill_snapshot(definition)
        manifest_names = _manifest_skill_grant_names(name)
    except (KeyError, ValueError):
        return False
    if grants is None or manifest_names is None:
        return False
    return (
        all(grant.kind == "builtin" and grant.distribution == "reference" for grant in grants) and {grant.name for grant in grants} == manifest_names
    )


def _grant(grants: tuple[ExactSkillGrant, ...], resource: SkillResourceRef) -> ExactSkillGrant:
    match = next((item for item in grants if (item.kind, item.id, item.revision) == (resource.source_kind, resource.id, resource.version)), None)
    if match is None or "read" not in match.permissions:
        raise ReferenceStartPreparationError("reference_skill_baseline_required")
    return match


class _CandidateReadSources:
    """Narrow preparation to the original read baseline, including dependencies."""

    def __init__(self, sources: SkillReadSourceResolver, scope: ReferenceRequestScope, grants: tuple[ExactSkillGrant, ...]):
        self.sources, self.scope, self.grants = sources, scope, grants

    def require(self, resource: SkillResourceRef, scope: ReferenceRequestScope) -> ExactSkillGrant:
        if scope != self.scope:
            raise ReferenceStartPreparationError("reference_start_preparation_unavailable")
        return _grant(self.grants, resource)

    async def prepare(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillRead:
        grant = self.require(resource, scope)
        resolved = await self.sources.prepare(resource, scope=scope)
        if resolved.proof.name != grant.name:
            raise ReferenceStartPreparationError("reference_skill_baseline_required")
        return resolved

    async def revalidate(self, proof: SkillReadSourceProof) -> None:
        grant = self.require(proof.resource, proof.scope)
        if proof.name != grant.name:
            raise ReferenceStartPreparationError("reference_skill_baseline_required")
        await self.sources.revalidate(proof)


async def prepare_reference_team_start_admission(
    db_path: str | Path,
    *,
    input_document: InputDocument,
    scope: ReferenceRequestScope,
    sources: SkillReadSourceResolver | None = None,
    automatic_receiver: AgentResourceRef | None = None,
    mcp_index: ReferenceMcpMetadataIndex | None = None,
    mcp_access: AccessControlStore | None = None,
    has_attachments: bool = False,
) -> ReferenceStartAdmission:
    """Resolve one exact Team Agent and selected read-only Skills.

    An automatic receiver stays a separate server field and is preparation-only
    until its durable admission contract is installed. No selection is dropped
    or synthesized to fit the existing explicit-mention writer.
    """
    try:
        document = InputDocument.model_validate(input_document.model_dump(mode="python", warnings="error"))
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
        if type(has_attachments) is not bool:
            raise ReferenceStartPreparationError("reference_start_unsupported")
        document.validate_task(has_attachments=has_attachments)
        refs = [node for node in document.nodes if isinstance(node, ReferenceNode)]
        skills = [node for node in refs if isinstance(node.resource, SkillResourceRef)]
        mcp = [node for node in refs if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)]
        prompts = [node for node in refs if isinstance(node.resource, McpPromptResourceRef)]
        receiver_resource = reference_start_receiver(document, automatic_receiver)
        if receiver_resource.source_kind != "team":
            raise ReferenceStartPreparationError("reference_start_unsupported")
        if (skills or sources is not None) and (not isinstance(sources, SkillReadSourceResolver) or Path(sources.db_path) != Path(db_path)):
            raise ReferenceStartPreparationError("reference_skill_preparation_required")
        if prompts and len(refs) != 1 + len(prompts):
            raise ReferenceStartPreparationError("reference_mcp_prompt_selection_unsupported")
        if (mcp or prompts) and (
            automatic_receiver is not None
            or not isinstance(mcp_index, ReferenceMcpMetadataIndex)
            or not isinstance(mcp_access, AccessControlStore)
            or Path(mcp_index.db_path).resolve() != Path(db_path).resolve()
            or Path(mcp_access.db_path).resolve() != Path(db_path).resolve()
        ):
            raise ReferenceStartPreparationError("reference_mcp_preparation_required")
        if not mcp and not prompts and (mcp_index is not None or mcp_access is not None):
            raise ReferenceStartPreparationError("reference_start_unsupported")
        bindings = AgentExecutionBindingStore(db_path)

        async def project():
            async with open_shared_ctx(db_path) as db:
                await db.execute("PRAGMA query_only=ON")
                await db.execute("BEGIN")
                try:
                    candidate = await bindings.project_reference_team_agent_in_connection(db, scope=scope, resource=receiver_resource)
                    grants, proofs, policy = (), [], None
                    if sources is not None:
                        definition = decode_reference_json(candidate.definition_json, max_bytes=1024 * 1024)
                        grants = exact_reference_skill_grants(definition)
                        require_supported_skill_grants(grants)
                        policy = await published_extension_policy(
                            db,
                            team_id=scope.team_id,
                            agent_id=candidate.expected.agent_id,
                            revision_ref=candidate.expected.revision_ref,
                            release_id=candidate.expected.release_id,
                            definition=definition,
                        )
                        if policy is not None:
                            require_extension_roots(grants, [node.resource for node in skills], policy)
                        else:
                            for node in skills:
                                _grant(grants, node.resource)
                        for grant in grants:
                            resource = SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=grant.revision)
                            proof = await sources.describe_in_connection(db, resource, scope=scope)
                            if proof.name != grant.name:
                                raise ReferenceStartPreparationError("reference_skill_baseline_required")
                            proofs.append(proof)
                    return candidate, grants, tuple(proofs), policy
                finally:
                    await db.rollback()

        before, grants, proofs, policy = await project()
        capability = prepare_reference_capability(document, before.behavior_descriptor)
        mcp_selection = (
            await prepare_reference_mcp_selection(
                db_path,
                input_document=document,
                scope=scope,
                receiver=receiver_resource,
                index=mcp_index,
                access=mcp_access,
            )
            if mcp and mcp_index is not None and mcp_access is not None
            else None
        )
        mcp_prompt_selection = (
            await prepare_reference_mcp_prompt_selection(
                db_path,
                input_document=document,
                scope=scope,
                receiver=receiver_resource,
                index=mcp_index,
                access=mcp_access,
            )
            if prompts and mcp_index is not None and mcp_access is not None
            else None
        )
        closure = None
        if skills:
            narrowed = (
                ExtensionReadSources(sources, scope=scope, grants=grants, policy=policy)
                if policy is not None
                else _CandidateReadSources(sources, scope, grants)
            )
            closure = await prepare_skill_read_closure(narrowed, [node.resource for node in skills], scope=scope)
            if any(item.proof not in proofs for item in closure.items if skill_read_provenance(grants, item.proof.resource, policy) == "baseline"):
                raise ReferenceStartPreparationError("reference_start_preparation_changed")
            # Optional execution requires actual current use, including when
            # legacy resource grants are configured in diagnostic shadow mode.
            extended = any(skill_read_provenance(grants, item.proof.resource, policy) != "baseline" for item in closure.items)
            if extended:
                async with open_shared_ctx(db_path) as db:
                    await db.execute("PRAGMA query_only=ON")
                    await db.execute("BEGIN")
                    for item in closure.items:
                        await sources.revalidate_retained_in_connection(db, item.proof)
            else:
                for item in closure.items:
                    await sources.revalidate(item.proof)
        if await project() != (before, grants, proofs, policy):
            raise ReferenceStartPreparationError("reference_start_preparation_changed")
        return ReferenceStartAdmission(
            input_document=document,
            automatic_receiver=receiver_resource if automatic_receiver is not None else None,
            policy_epoch=before.policy_epoch,
            profile_name=before.profile_name,
            expected=before.expected,
            skill_reads=closure,
            skill_sources=sources,
            capability=capability,
            mcp_selection=mcp_selection,
            mcp_prompt_selection=mcp_prompt_selection,
            mcp_index=mcp_index,
            mcp_access=mcp_access,
            required_skill_modes=tuple(sorted({skill_read_provenance(grants, item.proof.resource, policy) for item in closure.items}))
            if closure is not None
            else (),
            has_attachments=has_attachments,
        ).snapshot()
    except ReferenceStartPreparationError:
        raise
    except Exception:
        raise ReferenceStartPreparationError("reference_start_preparation_unavailable") from None


async def prepare_reference_system_start_admission(
    db_path: str | Path,
    *,
    input_document: InputDocument,
    scope: ReferenceRequestScope,
    runner: SubagentRunner,
    sources: SkillReadSourceResolver | None = None,
    automatic_receiver: AgentResourceRef | None = None,
    has_attachments: bool = False,
) -> ReferenceStartAdmission:
    """Prepare a registered system-required @Agent from its actual runtime base.

    Uses current team overrides and the same registry ordering as ordinary
    dispatch. No placeholder Turn, assignment, name fallback or client definition
    is created. A complete Skill baseline requires explicit versioned grants;
    legacy name allowlists never become source or invoke permissions.
    """
    from bowhead_core.agent_execution_binding import (
        ExactAgentBindingExpectation,
        _require_current_system_override,
        _system_revision_projection_base,
        project_system_agent_revision,
    )
    from bowhead_core.builtin_subagent_override_store import _row_to_override
    from bowhead_core.chat_reference_scope import require_reference_request_scope
    from bowhead_core.subagent import SubagentRunner, _system_profile_snapshot
    from bowhead_core.system_agent_marketplace import SYSTEM_AGENT_LISTINGS

    try:
        document = InputDocument.model_validate(input_document.model_dump(mode="python", warnings="error"))
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
        if type(has_attachments) is not bool:
            raise ReferenceStartPreparationError("reference_start_unsupported")
        document.validate_task(has_attachments=has_attachments)
        nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
        skills = [node for node in nodes if isinstance(node.resource, SkillResourceRef)]
        if any(isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef) for node in nodes):
            raise ReferenceStartPreparationError("reference_start_unsupported")
        resource = reference_start_receiver(document, automatic_receiver)
        if resource.source_kind != "system":
            raise ReferenceStartPreparationError("reference_start_unsupported")
        if (skills or sources is not None) and (
            type(sources) is not SkillReadSourceResolver or Path(sources.db_path).resolve() != Path(db_path).resolve()
        ):
            raise ReferenceStartPreparationError("reference_skill_preparation_required")
        name = resource.id.removeprefix("system:")

        if resource.id != "system:" + name or name not in SYSTEM_AGENT_LISTINGS:
            raise ReferenceStartPreparationError("reference_start_unsupported")
        if not isinstance(runner, SubagentRunner) or Path(getattr(runner.team_subagent_store, "db_path", "")) != Path(db_path):
            raise ReferenceStartPreparationError("reference_start_preparation_unavailable")
        runtime = copy.copy(runner)
        runtime._shadow_workspace_id = scope.workspace_id

        async def current_policy():
            async with open_shared_ctx(db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA query_only=ON")
                await db.execute("BEGIN")
                try:
                    epoch = await require_reference_request_scope(db, scope)
                    async with db.execute("SELECT * FROM team_builtin_subagent_override WHERE team_id=? AND name=?", (scope.team_id, name)) as cursor:
                        cursor.row_factory = aiosqlite.Row
                        row = await cursor.fetchone()
                    await _require_current_system_override(
                        db, team_id=scope.team_id, profile_name=name, expected_version=int(row["row_version"]) if row is not None else None
                    )
                    enabled = True
                    if row is not None and not enabled:
                        raise ReferenceStartPreparationError("reference_system_override_unavailable")
                    return epoch, json.dumps(dict(row), ensure_ascii=False, sort_keys=True) if row is not None else None, enabled
                finally:
                    await db.rollback()

        def definitions(policy):
            override = _row_to_override(json.loads(policy[1])) if policy[1] is not None else None
            known, base = runtime._resolve_system_profile(name, team_id=scope.team_id)
            _, effective = runtime._resolve_system_profile(name, team_id=scope.team_id, builtin_overrides={name: override} if override else None)
            if not known or base is None or effective is None:
                raise ReferenceStartPreparationError("reference_start_preparation_unavailable")
            encode = partial(json.dumps, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return encode(_system_profile_snapshot(base)), encode(_system_profile_snapshot(effective)), override.row_version if override else None

        before = await current_policy()
        base_json, definition_json, override_version = original = await anyio.to_thread.run_sync(definitions, before)
        definition = json.loads(definition_json)
        projection = _system_revision_projection_base(team_id=scope.team_id, profile_name=name, definition=definition)
        if (projection[0], projection[3]) != (resource.id, resource.version):
            raise ReferenceStartPreparationError("reference_agent_candidate_mismatch")
        prepared = await anyio.to_thread.run_sync(
            partial(project_system_agent_revision, team_id=scope.team_id, profile_name=name, definition=definition)
        )
        capability = prepare_reference_capability(document, prepared.behavior_descriptor)
        closure = None
        from bowhead_core.system_skill_snapshot import has_ordinary_system_skill_scope

        if not skills and capability is None and has_ordinary_system_skill_scope(definition):
            # Keep the original system profile and its ordinary Tool authority.
            # The exact Skill material adapter is needed only for an exact
            # baseline or explicit Skill selections; it cannot invent grants.
            sources = None
        elif not skills and capability is None and name in _ON_DEMAND_SKILL_SYSTEM_AGENTS and _only_manifest_reference_grants(name, definition):
            # @Coach runs like delegated Coach: its manifest's built-in reference
            # package is read with skill_read when needed, not injected as
            # required input. The profile's exact read scope still applies.
            sources = None
        if sources is not None:
            grants = exact_reference_skill_grants(definition)
            require_supported_skill_grants(grants)

            async def describe():
                async with open_shared_ctx(db_path) as db:
                    await db.execute("PRAGMA query_only=ON")
                    await db.execute("BEGIN")
                    proofs = []
                    for grant in grants:
                        proof = await sources.describe_in_connection(
                            db, SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=grant.revision), scope=scope
                        )
                        if proof.name != grant.name:
                            raise ReferenceStartPreparationError("reference_skill_baseline_required")
                        proofs.append(proof)
                    return tuple(proofs)

            proofs = await describe()
            if skills:
                closure = await prepare_skill_read_closure(
                    _CandidateReadSources(sources, scope, grants), [node.resource for node in skills], scope=scope
                )
                if any(item.proof not in proofs for item in closure.items):
                    raise ReferenceStartPreparationError("reference_start_preparation_changed")
            if await describe() != proofs:
                raise ReferenceStartPreparationError("reference_start_preparation_changed")
        # An unproven Capability descriptor does not disable the existing
        # single-Agent runtime. Preserve it unchanged; actual capability roles
        # and mixed Skill baselines retain their own admission checks.
        if await anyio.to_thread.run_sync(definitions, before) != original or await current_policy() != before:
            raise ReferenceStartPreparationError("reference_start_preparation_changed")
        return ReferenceStartAdmission(
            input_document=document,
            automatic_receiver=resource if automatic_receiver is not None else None,
            policy_epoch=before[0],
            profile_name=name,
            expected=ExactAgentBindingExpectation(
                agent_id=resource.id,
                assignment_id=None,
                revision_ref=resource.version,
                release_id=None,
                behavior_fingerprint=prepared.behavior_descriptor.behavior_fingerprint,
            ),
            system_revision=prepared,
            manifest_fingerprint="sha256:" + hashlib.sha256(base_json.encode()).hexdigest(),
            override_version=override_version,
            system_required=True,
            skill_reads=closure,
            skill_sources=sources,
            capability=capability,
            required_skill_modes=("baseline",) if closure is not None else (),
            has_attachments=has_attachments,
        ).snapshot()
    except ReferenceStartPreparationError:
        raise
    except Exception:
        raise ReferenceStartPreparationError("reference_start_preparation_unavailable") from None
