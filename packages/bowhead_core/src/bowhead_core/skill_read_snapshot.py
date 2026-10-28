"""Retain a read-only dependency closure in one scoped encrypted-content slot.

The descriptor is anchored to the first selected Skill node; dependencies keep
their own resource/hash identities inside the snapshot, without invented user
nodes. The original receiver must come from independent composition evidence.
Decoding proves retained integrity, never current authority or worker ownership.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_dependency_compiler import parse_exact_skill_grants
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES, ContentDescriptor
from bowhead_core.chat_references import ContentHash, Identity, SkillResourceRef, decode_reference_json
from bowhead_core.skill_read_closure import MAX_CLOSURE_SKILLS, PreparedSkillReadClosure, snapshot_prepared_skill_read_closure
from bowhead_core.skill_read_material import PreparedSkillRead
from bowhead_core.skill_read_receiver import PreparedReceiverSkillReads, SkillReadReceiverBaseline
from bowhead_core.skill_read_resolution import ResolvedSkillRead, SkillReadSourceProof

SKILL_READ_SNAPSHOT_CONTRACT = "skill-read-closure.v1"


class SkillReadSnapshotError(ValueError):
    """Finite errors without method bytes, parameter values or source metadata."""


def _fail(code: str = "reference_skill_snapshot_invalid") -> NoReturn:
    raise SkillReadSnapshotError(code) from None


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class _Proof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scope: ReferenceRequestScope
    resource: SkillResourceRef
    name: Identity
    policy_epoch: int = Field(ge=0)
    trust_basis: Identity
    source_state_digest: ContentHash

    @classmethod
    def capture(cls, proof: SkillReadSourceProof) -> _Proof:
        return cls.model_validate({key: getattr(proof, key) for key in cls.model_fields})

    def restore(self) -> SkillReadSourceProof:
        return SkillReadSourceProof(self.scope, self.resource, self.name, self.policy_epoch, self.trust_basis, self.source_state_digest)


@dataclass(frozen=True, slots=True)
class PreparedSkillReadSnapshot:
    content: bytes = field(repr=False)
    content_hash: str
    materialization_hash: str
    anchor: SkillResourceRef

    def descriptor(self, *, node_id: str, content_key: str) -> ContentDescriptor:
        """Caller must bind this anchor to its canonical selected source node."""
        try:
            return ContentDescriptor(
                schema_version=1,
                content_key=content_key,
                node_id=node_id,
                resource=self.anchor.model_dump(mode="python"),
                content_kind="skill_read_closure",
                materialization_hash=self.materialization_hash,
                content_hash=self.content_hash,
                byte_count=len(self.content),
            )
        except Exception:
            _fail()


def _receiver_snapshot(receiver: SkillReadReceiverBaseline) -> SkillReadReceiverBaseline:
    # Strict Pydantic dataclass fields require instances in Python mode. Reuse
    # the grant parser rather than accepting dicts or using JSON coercion for
    # the remaining receiver fields.
    value = receiver.model_dump(mode="python", exclude={"grants"}, warnings="error")
    value["grants"] = parse_exact_skill_grants([grant.to_canonical_dict() for grant in receiver.grants])
    return SkillReadReceiverBaseline.model_validate(value)


def prepare_skill_read_snapshot(prepared: PreparedReceiverSkillReads) -> PreparedSkillReadSnapshot:
    """Encode retained bytes/proofs; this does not persist or authorize them.

    Base64 preserves each original material byte sequence. The final 1 MiB bound
    includes this encoding and metadata, independently of closure/render budgets.
    Parameters stay in canonical source/resolutions and are not duplicated here.
    """
    try:
        closure = snapshot_prepared_skill_read_closure(prepared.closure)
        receiver = _receiver_snapshot(prepared.receiver)
        if receiver.scope.session_id is None or receiver.scope != closure.scope:
            _fail()
        for item in closure.items:
            grant = receiver.require_skill_read(item.proof.resource, scope=closure.scope)
            if grant.name != item.proof.name:
                _fail()
        detached = PreparedReceiverSkillReads(receiver, closure)
        members = [
            {
                "proof": _Proof.capture(item.proof).model_dump(mode="json"),
                "content": base64.b64encode(item.material.content).decode("ascii"),
                "content_hash": item.material.content_hash,
                "materialization_hash": item.material.materialization_hash,
            }
            for item in closure.items
        ]
        content = _json(
            {
                "schema_version": 1,
                "snapshot_contract": SKILL_READ_SNAPSHOT_CONTRACT,
                "receiver_digest": receiver.digest,
                "preparation_digest": detached.preparation_digest,
                "closure": {
                    "scope": closure.scope.model_dump(mode="json"),
                    "roots": [root.model_dump(mode="json") for root in closure.roots],
                    "items": members,
                    "text_bytes": closure.text_bytes,
                    "material_bytes": closure.material_bytes,
                    "closure_digest": closure.closure_digest,
                },
            }
        )
        if len(content) > MAX_REFERENCE_STORAGE_BYTES:
            _fail("reference_skill_snapshot_material_limit")
        materialization_hash = canonical_fingerprint(
            {
                "snapshot_contract": SKILL_READ_SNAPSHOT_CONTRACT,
                "members": [
                    {"resource": item.proof.resource.model_dump(mode="json"), "materialization_hash": item.material.materialization_hash}
                    for item in closure.items
                ],
            }
        )
        return PreparedSkillReadSnapshot(content, _hash(content), materialization_hash, closure.roots[0])
    except SkillReadSnapshotError:
        raise
    except Exception:
        _fail()


def restore_skill_read_snapshot(content: bytes, *, descriptor: ContentDescriptor, receiver: SkillReadReceiverBaseline) -> PreparedReceiverSkillReads:
    """Restore against an independently retained original receiver and descriptor.

    The caller opens the scoped cipher and checks current resource/receiver access
    separately. This function reads no files, current catalog, database or tools.
    """
    try:
        if not isinstance(content, bytes) or len(content) > MAX_REFERENCE_STORAGE_BYTES:
            _fail()
        descriptor = ContentDescriptor.model_validate(descriptor.model_dump(mode="python", warnings="error"))
        receiver = _receiver_snapshot(receiver)
        if descriptor.content_kind != "skill_read_closure" or (descriptor.byte_count, descriptor.content_hash) != (len(content), _hash(content)):
            _fail()
        value = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20)
        if not isinstance(value, dict) or set(value) != {"schema_version", "snapshot_contract", "receiver_digest", "preparation_digest", "closure"}:
            _fail()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["snapshot_contract"] != SKILL_READ_SNAPSHOT_CONTRACT:
            _fail()
        if value["receiver_digest"] != receiver.digest:
            _fail()
        saved = value["closure"]
        if not isinstance(saved, dict) or set(saved) != {"scope", "roots", "items", "text_bytes", "material_bytes", "closure_digest"}:
            _fail()
        if not isinstance(saved["items"], list) or not 1 <= len(saved["items"]) <= MAX_CLOSURE_SKILLS:
            _fail()
        items = []
        for member in saved["items"]:
            if not isinstance(member, dict) or set(member) != {"proof", "content", "content_hash", "materialization_hash"}:
                _fail()
            proof = _Proof.model_validate(member["proof"])
            if not isinstance(member["content"], str):
                _fail()
            material = PreparedSkillRead(base64.b64decode(member["content"], validate=True), member["content_hash"], member["materialization_hash"])
            items.append(ResolvedSkillRead(proof.restore(), material))
        closure = snapshot_prepared_skill_read_closure(
            PreparedSkillReadClosure(
                ReferenceRequestScope.model_validate(saved["scope"]),
                tuple(SkillResourceRef.model_validate(root) for root in saved["roots"]),
                tuple(items),
                saved["text_bytes"],
                saved["material_bytes"],
                saved["closure_digest"],
            )
        )
        restored = PreparedReceiverSkillReads(receiver, closure)
        expected = prepare_skill_read_snapshot(restored)
        if (
            value["preparation_digest"] != restored.preparation_digest
            or descriptor.resource != expected.anchor
            or descriptor.materialization_hash != expected.materialization_hash
            or content != expected.content
        ):
            _fail()
        return restored
    except SkillReadSnapshotError:
        raise
    except Exception:
        _fail()
