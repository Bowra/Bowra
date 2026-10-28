"""Prepare the complete exact Skill baseline independently of selected methods.

Original packages and operation grants are retained together. Deriving required
read-only input does not inject other baseline packages or remove invocation
permissions. These are preparation records, not live handles or dispatch grants.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES
from bowhead_core.chat_references import SkillResourceRef, decode_reference_json
from bowhead_core.skill_package_material import restore_skill_package
from bowhead_core.skill_read_closure import empty_baseline_selection, prepare_skill_read_closure
from bowhead_core.skill_read_material import _hash, _json
from bowhead_core.skill_read_receiver import (
    PreparedReceiverSkillReads,
    PrimaryFactory,
    SkillReadReceiverBaseline,
    SkillReadReceiverError,
    primary_skill_read_receiver,
)
from bowhead_core.skill_read_resolution import (
    ResolvedSkillPackage,
    ResolvedSkillRead,
    SkillReadResolutionError,
    SkillReadSourceProof,
    SkillReadSourceResolver,
)
from bowhead_core.skill_read_snapshot import _Proof, _receiver_snapshot

SKILL_BASELINE_CONTRACT = "skill-baseline-packages.v1"


class SkillBaselineMaterialError(SkillReadResolutionError):
    """Finite baseline preparation failure without private dependency details."""


@dataclass(frozen=True, slots=True)
class PreparedSkillBaseline:
    receiver: SkillReadReceiverBaseline
    packages: tuple[ResolvedSkillPackage, ...] = field(repr=False)
    content: bytes = field(repr=False)
    content_hash: str


def _pack(receiver: SkillReadReceiverBaseline, packages: tuple[ResolvedSkillPackage, ...]) -> PreparedSkillBaseline:
    try:
        receiver = _receiver_snapshot(receiver)
        if len(packages) != len(receiver.grants):
            raise ValueError()
        members = []
        detached = []
        epoch = None
        for grant, item in zip(receiver.grants, packages, strict=True):
            proof = _Proof.capture(item.proof).restore()
            resource = SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=grant.revision)
            if proof.scope != receiver.scope or proof.resource != resource or proof.name != grant.name:
                raise ValueError()
            if epoch is not None and proof.policy_epoch != epoch:
                raise ValueError()
            epoch = proof.policy_epoch
            material = restore_skill_package(
                item.material.content, resource=resource, expected_name=grant.name, content_hash=item.material.content_hash
            )
            members.append(
                {
                    "proof": _Proof.capture(proof).model_dump(mode="json"),
                    "package": decode_reference_json(material.content, max_bytes=MAX_REFERENCE_STORAGE_BYTES),
                    "content_hash": material.content_hash,
                }
            )
            detached.append(ResolvedSkillPackage(proof, material))
        content = _json({"schema_version": 1, "baseline_contract": SKILL_BASELINE_CONTRACT, "receiver_digest": receiver.digest, "members": members})
        if len(content) > MAX_REFERENCE_STORAGE_BYTES:
            raise ValueError()
        return PreparedSkillBaseline(receiver, tuple(detached), content, _hash(content))
    except Exception:
        raise SkillBaselineMaterialError("reference_skill_baseline_material_unavailable") from None


def restore_skill_baseline(content: bytes, *, receiver: SkillReadReceiverBaseline, content_hash: str) -> PreparedSkillBaseline:
    """Verify original bytes against an independently retained receiver/hash."""
    try:
        if type(content) is not bytes or len(content) > MAX_REFERENCE_STORAGE_BYTES or _hash(content) != content_hash:
            raise ValueError()
        value = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20)
        if not isinstance(value, dict) or set(value) != {"schema_version", "baseline_contract", "receiver_digest", "members"}:
            raise ValueError()
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["baseline_contract"] != SKILL_BASELINE_CONTRACT
            or value["receiver_digest"] != receiver.digest
            or not isinstance(value["members"], list)
            or len(value["members"]) != len(receiver.grants)
        ):
            raise ValueError()
        packages = []
        for item in value["members"]:
            if not isinstance(item, dict) or set(item) != {"proof", "package", "content_hash"}:
                raise ValueError()
            proof = _Proof.model_validate(item["proof"]).restore()
            package = restore_skill_package(
                _json(item["package"]), resource=proof.resource, expected_name=proof.name, content_hash=item["content_hash"]
            )
            packages.append(ResolvedSkillPackage(proof, package))
        expected = _pack(receiver, tuple(packages))
        if expected.content != content:
            raise ValueError()
        return expected
    except Exception:
        raise SkillBaselineMaterialError("reference_skill_baseline_material_unavailable") from None


async def prepare_primary_skill_baseline(
    sources: SkillReadSourceResolver, *, scope: ReferenceRequestScope, turn_id: str, primary_factory: PrimaryFactory
) -> PreparedSkillBaseline:
    """Capture every original grant under current source/receiver authority."""
    try:
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
        before = primary_skill_read_receiver(await primary_factory(), scope, turn_id)
        packages = []
        total = 0
        for grant in before.grants:
            resource = SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=grant.revision)
            item = await sources.prepare_package(resource, scope=before.scope)
            if item.proof.name != grant.name:
                raise ValueError()
            total += len(item.material.content)
            if total > MAX_REFERENCE_STORAGE_BYTES:
                raise ValueError()
            packages.append(item)
        for item in packages:
            await sources.revalidate(item.proof)
        after = primary_skill_read_receiver(await primary_factory(), scope, turn_id)
        if after != before:
            raise ValueError()
        return _pack(before, tuple(packages))
    except SkillReadReceiverError:
        raise
    except Exception:
        raise SkillBaselineMaterialError("reference_skill_baseline_material_unavailable") from None


class _RetainedMethods:
    def __init__(self, baseline: PreparedSkillBaseline):
        self.baseline = baseline

    async def prepare(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillRead:
        self.baseline.receiver.require_skill_read(resource, scope=scope)
        package = next(item for item in self.baseline.packages if item.proof.resource == resource)
        return ResolvedSkillRead(package.proof, package.material.selected_read())

    async def revalidate(self, proof: SkillReadSourceProof) -> None:
        # Integrity against this detached preparation, never current authority.
        if proof not in [item.proof for item in self.baseline.packages]:
            raise ValueError()


async def prepare_baseline_selected_reads(baseline: PreparedSkillBaseline, resources: Sequence[SkillResourceRef]) -> PreparedReceiverSkillReads:
    """Derive only selected methods/dependencies, without re-opening packages.

    Other baseline materials stay available in the separate baseline preparation.
    Required parameters and model budgets remain the selected renderer's contract.
    """
    baseline = restore_skill_baseline(baseline.content, receiver=baseline.receiver, content_hash=baseline.content_hash)
    closure = (
        await prepare_skill_read_closure(_RetainedMethods(baseline), resources, scope=baseline.receiver.scope)
        if resources
        else empty_baseline_selection(baseline.receiver.scope)
    )
    return PreparedReceiverSkillReads(baseline.receiver, closure, baseline)
