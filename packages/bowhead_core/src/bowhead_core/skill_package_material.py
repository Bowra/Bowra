"""Retain exact baseline package bytes without granting read or invoke access.

Unlike a user-selected read-only method, an Agent baseline may contain ordinary
Skill manifests, scripts and binary assets. Capture preserves them independently
of mandatory method input. No package is installed, executed or published here.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml
from bowhead_skill.loader import split_skill_markdown
from bowhead_skill.readonly_entry import MAX_CONTRACT_BYTES, MAX_FILE_BYTES

from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES
from bowhead_core.chat_references import SkillResourceRef, decode_reference_json
from bowhead_core.skill_read_material import PreparedSkillRead, _bundle_hash, _capture, _hash, _json, prepare_skill_read_files

SKILL_PACKAGE_CONTRACT = "skill-package.v1"
MAX_PACKAGE_FILES = 1024


class SkillPackageMaterialError(ValueError):
    """Finite material failure without file names, method text or source paths."""


@dataclass(frozen=True, slots=True)
class CapturedSkillPackage:
    """Call-local bytes under the existing package limit, before storage encoding."""

    resource: SkillResourceRef
    name: str
    entries: tuple[tuple[str, bytes], ...] = field(repr=False)

    def files(self) -> dict[str, bytes]:
        return dict(self.entries)


def capture_skill_package(root: Path, *, resource: SkillResourceRef, expected_name: str) -> CapturedSkillPackage:
    """Capture exact source bytes without imposing the reference envelope limit."""
    try:
        resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
        files = _capture(root)
        _manifest_name(files, expected_name)
        if (resource.source_kind == "builtin" and resource.id != "builtin:" + expected_name) or _bundle_hash(files) != resource.version:
            raise ValueError()
        return CapturedSkillPackage(resource, expected_name, tuple(files.items()))
    except Exception:
        raise SkillPackageMaterialError("reference_skill_package_unavailable") from None


@dataclass(frozen=True, slots=True)
class PreparedSkillPackage:
    resource: SkillResourceRef
    name: str
    content: bytes = field(repr=False)
    content_hash: str

    def files(self) -> dict[str, bytes]:
        """Return a detached exact byte map; this is not a runtime permission."""
        return _decode_package(self.content, resource=self.resource, expected_name=self.name, content_hash=self.content_hash)

    def selected_read(self) -> PreparedSkillRead:
        """Project only the declared method from this same retained revision."""
        return prepare_skill_read_files(self.files(), resource=self.resource, expected_name=self.name)


def _manifest_name(files: dict[str, bytes], expected_name: str) -> None:
    manifest = files.get("SKILL.md")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,504}", expected_name) or manifest is None or len(manifest) > MAX_FILE_BYTES or b"\0" in manifest:
        raise ValueError()
    header, _ = split_skill_markdown(manifest.decode("utf-8"))
    if len(header.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ValueError()
    metadata = yaml.safe_load(header)
    if (
        not isinstance(metadata, dict)
        or str(metadata.get("name") or "").strip() != expected_name
        or not str(metadata.get("description") or "").strip()
    ):
        raise ValueError()


def _decode_package(content: bytes, *, resource: SkillResourceRef, expected_name: str, content_hash: str) -> dict[str, bytes]:
    try:
        resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
        if type(content) is not bytes or len(content) > MAX_REFERENCE_STORAGE_BYTES or _hash(content) != content_hash:
            raise ValueError()
        value = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=10)
        if not isinstance(value, dict) or set(value) != {"schema_version", "package_contract", "resource", "name", "files"}:
            raise ValueError()
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["package_contract"] != SKILL_PACKAGE_CONTRACT
            or value["resource"] != resource.model_dump(mode="json")
            or value["name"] != expected_name
            or (resource.source_kind == "builtin" and resource.id != "builtin:" + expected_name)
            or not isinstance(value["files"], list)
            or not 1 <= len(value["files"]) <= MAX_PACKAGE_FILES
        ):
            raise ValueError()
        files = {}
        for item in value["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "content"} or type(item["path"]) is not str or type(item["content"]) is not str:
                raise ValueError()
            name = item["path"]
            path = PurePosixPath(name)
            if (
                not path.parts
                or path.is_absolute()
                or path.as_posix() != name
                or ".." in path.parts
                or "\\" in name
                or any(ord(char) < 32 for char in name)
                or len(name.encode()) > 1024
                or len(path.parts) > 17
                or name in files
            ):
                raise ValueError()
            files[name] = base64.b64decode(item["content"], validate=True)
        if any(parent.as_posix() in files for name in files for parent in PurePosixPath(name).parents):
            raise ValueError()
        # Canonical order/encoding and the *whole* original package are checked.
        # Re-hashing a mutated envelope cannot change an independently pinned ref.
        if list(files) != sorted(files, key=lambda name: PurePosixPath(name).parts) or _bundle_hash(files) != resource.version:
            raise ValueError()
        _manifest_name(files, expected_name)
        if _encode_package(files, resource=resource, name=expected_name) != content:
            raise ValueError()
        return files
    except Exception:
        raise SkillPackageMaterialError("reference_skill_package_unavailable") from None


def _encode_package(files: dict[str, bytes], *, resource: SkillResourceRef, name: str) -> bytes:
    return _json(
        {
            "schema_version": 1,
            "package_contract": SKILL_PACKAGE_CONTRACT,
            "resource": resource.model_dump(mode="json"),
            "name": name,
            "files": [
                {"path": path, "content": base64.b64encode(files[path]).decode("ascii")}
                for path in sorted(files, key=lambda p: PurePosixPath(p).parts)
            ],
        }
    )


def restore_skill_package(content: bytes, *, resource: SkillResourceRef, expected_name: str, content_hash: str) -> PreparedSkillPackage:
    """Restore against independent identity/hash, without filesystem or access."""
    _decode_package(content, resource=resource, expected_name=expected_name, content_hash=content_hash)
    return PreparedSkillPackage(SkillResourceRef.model_validate(resource.model_dump(mode="python")), expected_name, content, content_hash)


def prepare_skill_package(root: Path, *, resource: SkillResourceRef, expected_name: str) -> PreparedSkillPackage:
    """Capture in a worker thread after resolving current exact source authority."""
    try:
        resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
        content = _encode_package(_capture(root), resource=resource, name=expected_name)
        return restore_skill_package(content, resource=resource, expected_name=expected_name, content_hash=_hash(content))
    except Exception:
        raise SkillPackageMaterialError("reference_skill_package_unavailable") from None
