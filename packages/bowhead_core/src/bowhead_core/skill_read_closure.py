"""Bounded exact read-only Skill closure preparation; no receiver authorization.

Every node goes through the current source resolver. Results are preparation
facts, not runtime grants. Receiver baseline/extension checks, parameter
resolution, model budgets and durable composition still precede injection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_read_material import SkillReadMaterialError, validate_prepared_skill_read
from bowhead_core.skill_read_resolution import ResolvedSkillRead, SkillReadResolutionError, SkillReadSourceProof

MAX_SELECTED_SKILLS = 3
MAX_CLOSURE_SKILLS = 32
MAX_CLOSURE_DEPTH = 8
MAX_PACKAGE_TEXT_BYTES = 256 * 1024
MAX_CLOSURE_BYTES = 1024 * 1024

SkillIdentity = tuple[str, str]


class SkillReadClosureError(ValueError):
    """Finite closure reason without private dependency identities or text."""


class SkillReadClosureSource(Protocol):
    """Source-store boundary, optionally narrowed by the exact receiver baseline."""

    async def prepare(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillRead: ...

    async def revalidate(self, proof: SkillReadSourceProof) -> None: ...


def _fail(code: str) -> NoReturn:
    raise SkillReadClosureError(code) from None


@dataclass(frozen=True, slots=True)
class PreparedSkillReadClosure:
    scope: ReferenceRequestScope
    roots: tuple[SkillResourceRef, ...]
    # Deterministic dependency-first order, each exact resource prepared once.
    items: tuple[ResolvedSkillRead, ...] = field(repr=False)
    text_bytes: int
    material_bytes: int
    closure_digest: str


def _closure_digest(scope: ReferenceRequestScope, roots: tuple[SkillResourceRef, ...], topology: list[dict]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": scope.model_dump(mode="json"),
                    "roots": [root.model_dump(mode="json") for root in roots],
                    "items": topology,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )


def empty_baseline_selection(scope: ReferenceRequestScope) -> PreparedSkillReadClosure:
    """No explicit method was selected; this does not prepare or authorize a Skill."""
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    return PreparedSkillReadClosure(scope, (), (), 0, 0, _closure_digest(scope, (), []))


def snapshot_prepared_skill_read_closure(prepared: PreparedSkillReadClosure) -> PreparedSkillReadClosure:
    """Detach and check server-prepared bytes/graph before a transactional await.

    This proves internal consistency, not that arbitrary supplied bytes were
    captured from the selected package. Only the source preparation path creates
    admission candidates; current source/receiver authority is checked separately.
    """
    try:
        if not 1 <= len(prepared.roots) <= MAX_SELECTED_SKILLS or not 1 <= len(prepared.items) <= MAX_CLOSURE_SKILLS:
            _fail("reference_skill_dependency_limit")
        scope = ReferenceRequestScope.model_validate(prepared.scope.model_dump(mode="python", warnings="error"))
        roots = tuple(SkillResourceRef.model_validate(root.model_dump(mode="python", warnings="error")) for root in prepared.roots)
        if len({(root.source_kind, root.id) for root in roots}) != len(roots):
            _fail("reference_skill_dependency_duplicate_selection")
        items = []
        by_identity = {}
        children_by_resource = {}
        heights = {}
        topology = []
        text_bytes = material_bytes = 0
        epoch = None
        for item in prepared.items:
            resource = SkillResourceRef.model_validate(item.proof.resource.model_dump(mode="python", warnings="error"))
            if item.proof.scope != scope or (resource.source_kind, resource.id) in by_identity:
                _fail("reference_skill_dependency_unavailable")
            if type(item.proof.policy_epoch) is not int or item.proof.policy_epoch < 0:
                _fail("reference_skill_dependency_unavailable")
            if epoch is None:
                epoch = item.proof.policy_epoch
            elif epoch != item.proof.policy_epoch:
                _fail("reference_skill_source_changed")
            material = validate_prepared_skill_read(item.material, resource=resource)
            document = material.document()
            children = tuple(SkillResourceRef.model_validate(child) for child in document["read_only_entry"]["required_skill_refs"])
            if any(child not in heights for child in children):
                _fail("reference_skill_dependency_unavailable")
            heights[resource] = 1 + max((heights[child] for child in children), default=0)
            package_bytes = sum(len(file["text"].encode("utf-8")) for file in document["files"])
            text_bytes += package_bytes
            material_bytes += len(material.content)
            if package_bytes > MAX_PACKAGE_TEXT_BYTES or text_bytes > MAX_CLOSURE_BYTES or material_bytes > MAX_CLOSURE_BYTES:
                _fail("reference_skill_dependency_material_limit")
            by_identity[(resource.source_kind, resource.id)] = resource
            children_by_resource[resource] = children
            proof = SkillReadSourceProof(
                scope, resource, item.proof.name, item.proof.policy_epoch, item.proof.trust_basis, item.proof.source_state_digest
            )
            items.append(ResolvedSkillRead(proof, material))
            topology.append(
                {
                    "resource": resource.model_dump(mode="json"),
                    "required_skill_refs": [child.model_dump(mode="json") for child in children],
                    "content_hash": material.content_hash,
                    "materialization_hash": material.materialization_hash,
                    "source_state_digest": proof.source_state_digest,
                }
            )
        if any(root not in heights or heights[root] > MAX_CLOSURE_DEPTH for root in roots):
            _fail("reference_skill_dependency_limit")
        reachable = set()
        pending = list(roots)
        while pending:
            resource = pending.pop()
            if resource in reachable:
                continue
            reachable.add(resource)
            pending.extend(children_by_resource[resource])
        if reachable != set(children_by_resource):
            _fail("reference_skill_dependency_unavailable")
        digest = _closure_digest(scope, roots, topology)
        if (text_bytes, material_bytes, digest) != (prepared.text_bytes, prepared.material_bytes, prepared.closure_digest):
            _fail("reference_skill_dependency_unavailable")
        return PreparedSkillReadClosure(scope, roots, tuple(items), text_bytes, material_bytes, digest)
    except (SkillReadClosureError, SkillReadMaterialError):
        raise
    except Exception:
        _fail("reference_skill_dependency_unavailable")


async def prepare_skill_read_closure(
    resolver: SkillReadClosureSource, resources: Sequence[SkillResourceRef], *, scope: ReferenceRequestScope
) -> PreparedSkillReadClosure:
    """Prepare an all-or-nothing closure from explicit roots and declared refs.

    Repeated dependencies are shared only inside this call and scope. Root order
    and each declaration's order are retained; no global/name-based cache exists.
    """
    try:
        if not 1 <= len(resources) <= MAX_SELECTED_SKILLS:
            _fail("reference_skill_dependency_limit")
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="json"))
        roots = tuple(SkillResourceRef.model_validate(resource.model_dump(mode="json")) for resource in resources)
        versions: dict[SkillIdentity, str] = {}
        visiting: set[SkillIdentity] = set()
        heights: dict[SkillIdentity, int] = {}
        items: list[ResolvedSkillRead] = []
        topology: list[dict] = []
        text_bytes = material_bytes = 0
        policy_epoch: int | None = None

        def register(resource: SkillResourceRef) -> SkillIdentity:
            key = (resource.source_kind, resource.id)
            if key in versions:
                if versions[key] != resource.version:
                    _fail("reference_skill_dependency_version_conflict")
            else:
                if len(versions) >= MAX_CLOSURE_SKILLS:
                    _fail("reference_skill_dependency_limit")
                versions[key] = resource.version
            return key

        # Discover conflicting explicit versions before opening any package.
        root_keys = [register(resource) for resource in roots]
        if len(set(root_keys)) != len(root_keys):
            _fail("reference_skill_dependency_duplicate_selection")

        async def visit(resource: SkillResourceRef, depth: int) -> int:
            nonlocal text_bytes, material_bytes, policy_epoch
            if depth > MAX_CLOSURE_DEPTH:
                _fail("reference_skill_dependency_limit")
            key = register(resource)
            if key in visiting:
                _fail("reference_skill_dependency_cycle")
            if key in heights:
                # A shared subtree first reached by a short path must not let a
                # later longer path bypass the depth limit without another IO.
                if depth + heights[key] - 1 > MAX_CLOSURE_DEPTH:
                    _fail("reference_skill_dependency_limit")
                return heights[key]
            visiting.add(key)
            resolved = await resolver.prepare(resource, scope=scope)
            if resolved.proof.resource != resource or resolved.proof.scope != scope:
                _fail("reference_skill_dependency_unavailable")
            if policy_epoch is None:
                policy_epoch = resolved.proof.policy_epoch
            elif resolved.proof.policy_epoch != policy_epoch:
                _fail("reference_skill_source_changed")
            document = resolved.material.document()
            package_bytes = sum(len(file["text"].encode("utf-8")) for file in document["files"])
            text_bytes += package_bytes
            material_bytes += len(resolved.material.content)
            if package_bytes > MAX_PACKAGE_TEXT_BYTES or text_bytes > MAX_CLOSURE_BYTES or material_bytes > MAX_CLOSURE_BYTES:
                _fail("reference_skill_dependency_material_limit")
            children = tuple(SkillResourceRef.model_validate(ref) for ref in document["read_only_entry"]["required_skill_refs"])
            height = 1
            for child in children:
                height = max(height, 1 + await visit(child, depth + 1))
            heights[key] = height
            visiting.remove(key)
            items.append(resolved)
            topology.append(
                {
                    "resource": resource.model_dump(mode="json"),
                    "required_skill_refs": [child.model_dump(mode="json") for child in children],
                    "content_hash": resolved.material.content_hash,
                    "materialization_hash": resolved.material.materialization_hash,
                    "source_state_digest": resolved.proof.source_state_digest,
                }
            )
            return height

        for resource in roots:
            await visit(resource, 1)
        # A child prepared later can revoke an earlier node. Recheck every source
        # after building the graph; this is not a cross-store transaction/lease.
        for resolved in items:
            await resolver.revalidate(resolved.proof)
        digest = _closure_digest(scope, roots, topology)
        return PreparedSkillReadClosure(scope, roots, tuple(items), text_bytes, material_bytes, digest)
    except (SkillReadClosureError, SkillReadResolutionError, SkillReadMaterialError):
        raise
    except Exception:
        _fail("reference_skill_dependency_unavailable")
