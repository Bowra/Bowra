"""Startup-only builtin Skill metadata. Never an execution or materialization grant.

Use the installed builtin root, not the runtime's name-shadowing global catalog.
Rebuild on package/catalog replacement; queries only consume detached metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from bowhead_skill.loader import load_skill

from bowhead_core.skill_upload import MAX_SKILL_SIZE, bundle_sha256
from bowhead_skill.catalog import builtin_catalog_dir


@dataclass(frozen=True, slots=True)
class BuiltinSkillMetadata:
    name: str
    label: str
    summary: str
    revision: str
    parameter_schema_json: str | None
    relative_path: str = field(repr=False)
    read_only_entry_json: str | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return f"builtin:{self.name}"

    def parameter_schema(self) -> dict | None:
        return json.loads(self.parameter_schema_json) if self.parameter_schema_json is not None else None


@dataclass(frozen=True, slots=True)
class BuiltinSkillMetadataIndex:
    items: tuple[BuiltinSkillMetadata, ...]
    generation: str
    incomplete: bool


def _package_revision(root: Path) -> str:
    # Installed package data is trusted, but unsupported filesystem entries and
    # oversized packages must not be advertised. This is not the later stable
    # materializer for mutable user/team bundles.
    if root.is_symlink() or not root.is_dir():
        raise ValueError("builtin_metadata_unavailable")
    total = 0
    for count, path in enumerate(root.rglob("*"), 1):
        info = path.lstat()
        if count > 1024 or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("builtin_metadata_unavailable")
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        if total > MAX_SKILL_SIZE:
            raise ValueError("builtin_metadata_unavailable")
    return f"sha256:{bundle_sha256(root)}"


def build_builtin_skill_metadata(*, builtin_root: Path | None = None) -> BuiltinSkillMetadataIndex:
    """Build once outside discovery; a partial failure keeps other rows usable.

    The optional root is a server/test dependency, never a request parameter.
    Both revisions use the existing whole-package algorithm. An observed package
    change during parsing is rejected; runtime must still recheck exact content.
    """
    root = builtin_root if builtin_root is not None else builtin_catalog_dir()
    entries: dict[str, BuiltinSkillMetadata] = {}
    ambiguous: set[str] = set()
    incomplete = False
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("builtin_metadata_unavailable")
        for path in sorted(root.rglob("SKILL.md")):
            try:
                if path.is_symlink() or any((root / parent).is_symlink() for parent in path.relative_to(root).parents):
                    raise ValueError("builtin_metadata_unavailable")
                revision = _package_revision(path.parent)
                spec = load_skill(path.parent)
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,504}", spec.name) or _package_revision(path.parent) != revision:
                    raise ValueError("builtin_metadata_unavailable")
                if spec.name in entries or spec.name in ambiguous:
                    entries.pop(spec.name, None)
                    ambiguous.add(spec.name)
                    incomplete = True
                    continue
                from bowhead_core.chat_references import SkillResourceRef
                from bowhead_core.skill_read_material import prepare_skill_read

                material = prepare_skill_read(
                    path.parent,
                    resource=SkillResourceRef(kind="skill", source_kind="builtin", id=f"builtin:{spec.name}", version=revision),
                    expected_name=spec.name,
                )
                entry = material.document()["read_only_entry"]
                entry_json = (
                    json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if spec.read_only_entry is not None else None
                )
                schema_json = json.dumps(entry["parameter_schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                summary = spec.description[:1024]
                summary.encode("utf-8")
                label = (spec.title or spec.name)[:256]
                label.encode("utf-8")
                entries[spec.name] = BuiltinSkillMetadata(
                    name=spec.name,
                    label=label,
                    summary=summary,
                    revision=revision,
                    parameter_schema_json=schema_json,
                    relative_path=path.parent.relative_to(root).as_posix(),
                    read_only_entry_json=entry_json,
                )
            except (OSError, ValueError, TypeError):
                incomplete = True
    except OSError:
        incomplete = True
    except ValueError:
        incomplete = True
    items = tuple(entries[name] for name in sorted(entries))
    wire = [(item.id, item.label, item.summary, item.revision, item.parameter_schema_json, item.relative_path) for item in items]
    generation = hashlib.sha256(
        json.dumps(["builtin-reference-metadata.v1", incomplete, wire], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return BuiltinSkillMetadataIndex(items, generation, incomplete)
