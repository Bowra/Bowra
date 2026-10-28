"""Finite metadata graph checks for an exact receiver and selected methods.

No package reads, model calls, authorization changes or execution reservation.
Source preparation and live authorization remain mandatory at submission/use.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bowhead_skill.readonly_entry import ReadOnlyEntry, skill_reference_read_entry

from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata
from bowhead_core.skill_read_closure import MAX_CLOSURE_DEPTH, MAX_CLOSURE_SKILLS, MAX_SELECTED_SKILLS
from bowhead_core.skill_read_extensions import require_extension_member_name, require_extension_roots, skill_read_provenance


@dataclass(frozen=True, slots=True)
class ReferenceSkillMetadata:
    name: str
    entry_json: str | None
    available: bool
    strict_access: bool = False


def reference_skill_modes(
    receiver: ReferenceReceiverMetadata,
    roots: Sequence[SkillResourceRef],
    skills: Mapping[SkillResourceRef, ReferenceSkillMetadata],
) -> tuple[str, ...] | None:
    try:
        # Agent-only selection uses its ordinary system execution. Missing
        # exact grants must never become authorization for a selected Skill.
        if receiver.ordinary_system_scope:
            return None if roots else ()
        if len(roots) > MAX_SELECTED_SKILLS or len({(root.source_kind, root.id) for root in roots}) != len(roots):
            return None
        # Complete baseline availability cannot be narrowed to the selected roots.
        baseline = set()
        for grant in receiver.grants:
            resource = SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=grant.revision)
            metadata = skills.get(resource)
            baseline.add(resource)
            if metadata is None or not metadata.available or metadata.name != grant.name:
                return None
        require_extension_roots(receiver.grants, roots, receiver.policy)
        visiting = set()
        heights: dict[SkillResourceRef, int] = {}
        versions = {}
        names = {}
        modes = set()

        def visit(resource: SkillResourceRef, depth: int) -> int:
            if resource in visiting or depth > MAX_CLOSURE_DEPTH:
                raise ValueError("incompatible dependency")
            if resource in heights:
                if depth + heights[resource] - 1 > MAX_CLOSURE_DEPTH:
                    raise ValueError("dependency depth")
                return heights[resource]
            identity = (resource.source_kind, resource.id)
            if identity in versions and versions[identity] != resource.version or len(versions) >= MAX_CLOSURE_SKILLS:
                raise ValueError("dependency version or count")
            metadata = skills.get(resource)
            if metadata is None or not metadata.available or metadata.entry_json is None and resource not in baseline:
                raise ValueError("missing current method")
            entry = ReadOnlyEntry(json.loads(metadata.entry_json)) if metadata.entry_json is not None else skill_reference_read_entry({})
            if resource not in roots:
                entry.validate_parameters({})
            require_extension_member_name(receiver.grants, resource, metadata.name)
            if metadata.name in names and names[metadata.name] != resource:
                raise ValueError("ambiguous method name")
            names[metadata.name] = resource
            versions[identity] = resource.version
            modes.add(skill_read_provenance(receiver.grants, resource, receiver.policy))
            visiting.add(resource)
            height = 1 + max(
                (visit(SkillResourceRef.model_validate(child), depth + 1) for child in entry.to_dict()["required_skill_refs"]), default=0
            )
            visiting.remove(resource)
            heights[resource] = height
            return height

        for root in roots:
            visit(root, 1)
        if "readonly_extension" in modes and any(not skills[resource].strict_access for resource in baseline | heights.keys()):
            return None
        return tuple(sorted(modes))
    except (ValueError, TypeError, KeyError):
        return None
