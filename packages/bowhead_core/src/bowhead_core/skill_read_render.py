"""Versioned parameter data rendering for a prepared exact Skill candidate.

Selections are caller-supplied candidate nodes, not proof of user confirmation.
The production writer must bind them to immutable source/parameter resolutions;
model budgets, durable composition and current dispatch authority remain separate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn

from bowhead_skill.readonly_entry import ReadOnlyEntry, ReadOnlyEntryError

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import InputDocument, ReferenceNode, SkillResourceRef
from bowhead_core.skill_read_closure import MAX_CLOSURE_BYTES, MAX_SELECTED_SKILLS, PreparedSkillReadClosure, empty_baseline_selection
from bowhead_core.skill_read_receiver import PreparedReceiverSkillReads, PrimaryFactory, prepare_primary_baseline_skill_reads
from bowhead_core.skill_read_resolution import SkillReadSourceResolver

SKILL_READ_RENDER_CONTRACT = "skill-read-json.v1"
EMPTY_SKILL_SELECTION_DIGEST = canonical_fingerprint([])


class SkillReadRenderError(ValueError):
    """Finite reason without a parameter value, private field or method body."""


def _fail(code: str = "reference_skill_render_unavailable") -> NoReturn:
    raise SkillReadRenderError(code) from None


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _selections(nodes: Sequence[ReferenceNode]) -> tuple[ReferenceNode, ...]:
    try:
        if not 1 <= len(nodes) <= MAX_SELECTED_SKILLS:
            _fail("reference_skill_render_selection_invalid")
        # Reuse wire bounds, duplicate checks and numeric normalization. Dumping
        # and validating also detaches nested parameters before the first await.
        # JSON mode could coerce unchecked bytes/tuples into accepted wire data.
        # Serializer warnings can contain private values; turn them into the
        # same finite rejection instead of emitting them to logs.
        document = InputDocument.model_validate({"schema_version": 1, "nodes": [node.model_dump(mode="python", warnings="error") for node in nodes]})
        if any(not isinstance(node, ReferenceNode) or not isinstance(node.resource, SkillResourceRef) for node in document.nodes):
            _fail("reference_skill_render_selection_invalid")
        return tuple(document.nodes)
    except SkillReadRenderError:
        raise
    except Exception:
        _fail("reference_skill_render_selection_invalid")


@dataclass(frozen=True, slots=True)
class RenderedSkillReads:
    content: bytes = field(repr=False)
    rendered_content_hash: str
    preparation_digest: str
    selection_digest: str
    render_contract: str = SKILL_READ_RENDER_CONTRACT

    def document(self) -> dict:
        return json.loads(self.content)


def _parameterized_skills(closure: PreparedSkillReadClosure, nodes: tuple[ReferenceNode, ...]) -> tuple[list[dict], str]:
    if tuple(node.resource for node in nodes) != closure.roots:
        _fail("reference_skill_render_selection_mismatch")
    selected = {(node.resource.source_kind, node.resource.id): node for node in nodes}
    selection_digest = canonical_fingerprint([node.model_dump(mode="json", exclude={"label", "type"}) for node in nodes])
    skills = []
    for item in closure.items:
        resource = item.proof.resource
        material = item.material.document()
        node = selected.get((resource.source_kind, resource.id))
        if item.proof.scope != closure.scope or material["resource"] != resource.model_dump(mode="json"):
            _fail()
        if node is not None and node.resource != resource:
            _fail("reference_skill_render_selection_mismatch")
        entry = ReadOnlyEntry(material["read_only_entry"])
        try:
            parameters = entry.validate_parameters(node.parameters if node is not None else {})
        except ReadOnlyEntryError:
            _fail("reference_skill_parameters_invalid" if node is not None else "reference_skill_dependency_parameters_required")
        skills.append(
            {
                "resource": resource.model_dump(mode="json"),
                "node_id": node.node_id if node is not None else None,
                "entry_file": entry.entry_file,
                "parameters": parameters,
                "files": material["files"],
                "content_hash": item.material.content_hash,
                "materialization_hash": item.material.materialization_hash,
            }
        )
    if not set(selected).issubset({(item.proof.resource.source_kind, item.proof.resource.id) for item in closure.items}):
        _fail()
    return skills, selection_digest


def validate_prepared_skill_read_parameters(closure: PreparedSkillReadClosure, selections: Sequence[ReferenceNode]) -> None:
    """Validate preflight parameters; no receiver, confirmation or composition."""
    try:
        _parameterized_skills(closure, _selections(selections))
    except SkillReadRenderError:
        raise
    except Exception:
        _fail()


def render_prepared_skill_reads(prepared: PreparedReceiverSkillReads, selections: Sequence[ReferenceNode]) -> RenderedSkillReads:
    """Encode method files and schema-validated JSON data without substitution.

    A dependency receives its own explicitly selected parameters, or an empty
    object if its schema permits that. No parent values/defaults are inherited.
    This digest binds a candidate; it does not assert source admission or a lease.
    """
    try:
        if not selections and prepared.baseline is not None:
            if prepared.baseline.receiver != prepared.receiver or prepared.closure != empty_baseline_selection(prepared.receiver.scope):
                _fail("reference_skill_render_selection_mismatch")
            nodes = ()
        else:
            nodes = _selections(selections)
        closure = prepared.closure
        if closure.scope != prepared.receiver.scope or tuple(node.resource for node in nodes) != closure.roots:
            _fail("reference_skill_render_selection_mismatch")
        skills, selection_digest = _parameterized_skills(closure, nodes)
        preparation_digest = prepared.preparation_digest
        content = _json(
            {
                "schema_version": 1,
                "render_contract": SKILL_READ_RENDER_CONTRACT,
                "preparation_digest": preparation_digest,
                "selection_digest": selection_digest,
                "skills": skills,
            }
        )
        # Includes UTF-8, JSON escaping, parameters and wrappers. This storage
        # bound does not replace the actual provider's token/input-budget gate.
        if len(content) > MAX_CLOSURE_BYTES:
            _fail("reference_skill_render_material_limit")
        return RenderedSkillReads(content, "sha256:" + hashlib.sha256(content).hexdigest(), preparation_digest, selection_digest)
    except SkillReadRenderError:
        raise
    except Exception:
        _fail()


def restore_rendered_skill_reads(content: bytes, *, prepared: PreparedReceiverSkillReads, selections: Sequence[ReferenceNode]) -> RenderedSkillReads:
    """Verify against original material and independently supplied selections.

    Never trust selection/digest fields in decrypted bytes as their own evidence.
    The caller must restore the scoped material and effective parameters first;
    this pure comparison does not read today's files or validate current access.
    """
    if not isinstance(content, bytes) or len(content) > MAX_CLOSURE_BYTES:
        _fail("reference_skill_render_snapshot_mismatch")
    expected = render_prepared_skill_reads(prepared, selections)
    if content != expected.content:
        _fail("reference_skill_render_snapshot_mismatch")
    return expected


async def prepare_primary_skill_read_candidate(
    sources: SkillReadSourceResolver,
    selections: Sequence[ReferenceNode],
    *,
    scope: ReferenceRequestScope,
    turn_id: str,
    primary_factory: PrimaryFactory,
) -> tuple[PreparedReceiverSkillReads, RenderedSkillReads]:
    """Prepare and render detached candidate selections for a bound primary."""
    nodes = _selections(selections)
    prepared = await prepare_primary_baseline_skill_reads(
        sources, [node.resource for node in nodes], scope=scope, turn_id=turn_id, primary_factory=primary_factory
    )
    return prepared, render_prepared_skill_reads(prepared, nodes)
