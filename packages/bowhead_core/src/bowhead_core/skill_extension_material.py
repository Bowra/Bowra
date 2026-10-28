"""Retain optional read-only material beside the unchanged executable baseline."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES
from bowhead_core.chat_references import SkillResourceRef, decode_reference_json
from bowhead_core.skill_baseline_material import restore_skill_baseline
from bowhead_core.skill_read_closure import PreparedSkillReadClosure, snapshot_prepared_skill_read_closure
from bowhead_core.skill_read_extensions import SkillReadExtensionPolicy, skill_read_provenance, validate_extension_closure
from bowhead_core.skill_read_material import PreparedSkillRead, _hash, _json
from bowhead_core.skill_read_receiver import PreparedReceiverSkillReads, SkillReadReceiverBaseline
from bowhead_core.skill_read_resolution import ResolvedSkillRead
from bowhead_core.skill_read_snapshot import _Proof

CONTRACT = "skill-read-extension.v1"


@dataclass(frozen=True, slots=True)
class PreparedSkillExtension:
    content: bytes = field(repr=False)
    content_hash: str


def prepare_skill_extension(prepared: PreparedReceiverSkillReads) -> PreparedSkillExtension:
    try:
        receiver, closure, baseline, extension = prepared.receiver, prepared.closure, prepared.baseline, prepared.extension
        if baseline is None or extension is None:
            raise ValueError()
        validate_extension_closure(receiver, closure, extension)
        if baseline != restore_skill_baseline(baseline.content, receiver=receiver, content_hash=baseline.content_hash):
            raise ValueError()
        for item in closure.items:
            if skill_read_provenance(receiver.grants, item.proof.resource, extension.policy) == "baseline":
                package = next((p for p in baseline.packages if p.proof == item.proof), None)
                if package is None or package.material.selected_read() != item.material:
                    raise ValueError()
        content = _json(
            {
                "schema_version": 1,
                "contract": CONTRACT,
                "receiver_digest": receiver.digest,
                "preparation_digest": prepared.preparation_digest,
                "extension": extension.model_dump(mode="json"),
                "baseline": base64.b64encode(baseline.content).decode("ascii"),
                "baseline_hash": baseline.content_hash,
                "roots": [root.model_dump(mode="json") for root in closure.roots],
                "items": [
                    {
                        "proof": _Proof.capture(item.proof).model_dump(mode="json"),
                        "content": base64.b64encode(item.material.content).decode("ascii"),
                        "content_hash": item.material.content_hash,
                        "materialization_hash": item.material.materialization_hash,
                    }
                    for item in closure.items
                ],
                "text_bytes": closure.text_bytes,
                "material_bytes": closure.material_bytes,
                "closure_digest": closure.closure_digest,
            }
        )
        if len(content) > MAX_REFERENCE_STORAGE_BYTES:
            raise ValueError()
        return PreparedSkillExtension(content, _hash(content))
    except Exception:
        raise ValueError("reference_skill_extension_material_unavailable") from None


def restore_skill_extension(content: bytes, *, receiver: SkillReadReceiverBaseline, content_hash: str) -> PreparedReceiverSkillReads:
    try:
        if type(content) is not bytes or _hash(content) != content_hash:
            raise ValueError()
        value = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20)
        baseline = restore_skill_baseline(base64.b64decode(value["baseline"], validate=True), receiver=receiver, content_hash=value["baseline_hash"])
        extension = SkillReadExtensionPolicy.model_validate_json(_json(value["extension"]), strict=True)
        closure = snapshot_prepared_skill_read_closure(
            PreparedSkillReadClosure(
                receiver.scope,
                tuple(SkillResourceRef.model_validate(root) for root in value["roots"]),
                tuple(
                    ResolvedSkillRead(
                        _Proof.model_validate(item["proof"]).restore(),
                        PreparedSkillRead(base64.b64decode(item["content"], validate=True), item["content_hash"], item["materialization_hash"]),
                    )
                    for item in value["items"]
                ),
                value["text_bytes"],
                value["material_bytes"],
                value["closure_digest"],
            )
        )
        prepared = PreparedReceiverSkillReads(receiver, closure, baseline, extension)
        if prepare_skill_extension(prepared).content != content:
            raise ValueError()
        return prepared
    except Exception:
        raise ValueError("reference_skill_extension_material_unavailable") from None
