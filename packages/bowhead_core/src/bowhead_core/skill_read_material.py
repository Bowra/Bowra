"""Bounded, exact-byte Skill read preparation; never an authorization handle.

The caller must resolve the root by exact source/id under current authorization
before calling, and authorize again before storage/use. This module does not
resolve names, grant tools, visit dependencies, persist plaintext, or execute.
Run filesystem preparation in a worker thread, never on a catalog query.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NoReturn

import yaml
from bowhead_skill.loader import split_skill_markdown
from bowhead_skill.readonly_entry import MAX_CONTRACT_BYTES, MAX_ENTRY_BYTES, MAX_FILE_BYTES, ReadOnlyEntry, skill_reference_read_entry

from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES, ContentDescriptor
from bowhead_core.chat_references import SkillResourceRef, decode_reference_json
from bowhead_core.skill_upload import MAX_SKILL_SIZE

_MAX_ENTRIES = 1024
_MAX_DEPTH = 16
_CHUNK_BYTES = 64 * 1024


class SkillReadMaterialError(ValueError):
    """Finite code only, including when a parser or syscall fails."""


def _fail(code: str = "reference_skill_material_unavailable") -> NoReturn:
    raise SkillReadMaterialError(code) from None


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _signature(info: os.stat_result) -> tuple[int, ...]:
    # atime changes on reads; content, identity and directory mutations do not
    # need atime. Pinning the resulting full digest is still mandatory.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _capture(root: Path) -> dict[str, bytes]:
    """Read through directory descriptors; never follow an in-package symlink.

    Paths above the server-resolved root remain the caller's trust boundary.
    Root rename can only retain original descriptor content or fail; opening a
    later same-name package cannot change bytes already captured here.
    """
    files: dict[str, bytes] = {}
    count = 0
    total = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

    def walk(fd: int, prefix: tuple[str, ...]) -> None:
        nonlocal count, total
        if len(prefix) > _MAX_DEPTH:
            _fail("reference_skill_material_too_large")
        before = os.fstat(fd)
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    _fail("reference_skill_material_too_large")
                parts = (*prefix, entry.name)
                relative = PurePosixPath(*parts).as_posix()
                if len(relative.encode("utf-8")) > 1024 or "\\" in relative or any(ord(char) < 32 for char in relative):
                    _fail()
                observed = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                is_dir = stat.S_ISDIR(observed.st_mode)
                if not is_dir and (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1):
                    _fail()
                child = os.open(entry.name, directory_flags if is_dir else file_flags, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if _signature(observed) != _signature(opened):
                        _fail()
                    if is_dir:
                        walk(child, parts)
                        continue
                    if opened.st_size > MAX_SKILL_SIZE - total:
                        _fail("reference_skill_material_too_large")
                    content = bytearray()
                    while True:
                        chunk = os.read(child, min(_CHUNK_BYTES, MAX_SKILL_SIZE - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_SKILL_SIZE:
                            _fail("reference_skill_material_too_large")
                        content.extend(chunk)
                    if _signature(opened) != _signature(os.fstat(child)) or len(content) != opened.st_size:
                        _fail()
                    files[relative] = bytes(content)
                finally:
                    os.close(child)
        if _signature(before) != _signature(os.fstat(fd)):
            _fail()

    root_fd = os.open(root, directory_flags)
    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)
    return files


def _bundle_hash(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    # pathlib sorts by path components, not slash-joined strings (a/b < a.txt).
    # Preserve the existing bundle_sha256 algorithm, including binary/scripts.
    for relative in sorted(files, key=lambda name: PurePosixPath(name).parts):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[relative])
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedSkillRead:
    """Detached material bytes, with no source path, executable or live grant.

    content is compatible with ReferenceCipher/ContentDescriptor byte budgets;
    hashing it proves integrity, not approval or authority to read the resource.
    """

    content: bytes = field(repr=False)
    content_hash: str
    materialization_hash: str

    def document(self) -> dict:
        return json.loads(self.content)


def prepare_skill_read(root: Path, *, resource: SkillResourceRef, expected_name: str, require_declaration: bool = False) -> PreparedSkillRead:
    """Prepare only a selected, already resolved whole-package revision.

    No parameter defaults or interpolation. required_skill_refs are declarations
    for the caller to authorize/close, not recursively granted dependencies.
    A baseline without an entry declaration contributes only its SKILL.md body.
    Explicit declarations remain required for optional read-only extensions.
    """
    try:
        if not isinstance(resource, SkillResourceRef):
            _fail()
        resource = SkillResourceRef.model_validate(resource.model_dump(mode="python"))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,504}", expected_name) or (
            resource.source_kind == "builtin" and resource.id != f"builtin:{expected_name}"
        ):
            _fail()
        return prepare_skill_read_files(_capture(root), resource=resource, expected_name=expected_name, require_declaration=require_declaration)
    except SkillReadMaterialError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        _fail()


def prepare_skill_read_files(
    captured: dict[str, bytes], *, resource: SkillResourceRef, expected_name: str, require_declaration: bool = False
) -> PreparedSkillRead:
    """Derive a selected read from already captured exact package bytes.

    The caller supplies a validated original package, never paths or model text.
    The complete package revision and selected text entry are still verified.
    This pure projection does not authorize a receiver or execute package code.
    """
    try:
        if not isinstance(resource, SkillResourceRef):
            _fail()
        exact = SkillResourceRef.model_validate(resource.model_dump(mode="python"))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,504}", expected_name):
            _fail()
        if exact.source_kind == "builtin" and exact.id != f"builtin:{expected_name}":
            _fail()
        if _bundle_hash(captured) != exact.version:
            _fail("reference_skill_version_changed")
        manifest = captured.get("SKILL.md")
        if manifest is None or len(manifest) > MAX_FILE_BYTES or b"\0" in manifest:
            _fail()
        frontmatter_text, body = split_skill_markdown(manifest.decode("utf-8"))
        if len(frontmatter_text.encode("utf-8")) > MAX_CONTRACT_BYTES:
            _fail("reference_skill_material_too_large")
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            _fail()
        # Preserve the existing loader's required-name/description normalization.
        if str(frontmatter.get("name") or "").strip() != expected_name or not str(frontmatter.get("description") or "").strip():
            _fail()
        if require_declaration and "read_only_entry" not in frontmatter:
            _fail()
        entry = skill_reference_read_entry(frontmatter)
        projected = []
        total = 0
        for relative in (entry.entry_file, *entry.required_files):
            raw = captured.get(relative)
            if raw is None or len(raw) > MAX_FILE_BYTES or b"\0" in raw:
                _fail()
            total += len(raw)
            if total > MAX_ENTRY_BYTES:
                _fail("reference_skill_material_too_large")
            text = body if relative == "SKILL.md" else raw.decode("utf-8")
            if relative == entry.entry_file and not text.strip():
                _fail()
            projected.append({"path": relative, "text": text, "sha256": _hash(text.encode("utf-8"))})
        # Asset revision covers the whole original package. Materialization covers
        # the declaration and projected text; an unused script changes only the
        # former. No unselected manifest/tool metadata crosses this boundary.
        material = {"schema_version": 1, "read_only_entry": entry.to_dict(), "files": projected}
        content = _json({**material, "resource": exact.model_dump(mode="json")})
        if len(content) > MAX_REFERENCE_STORAGE_BYTES:
            _fail("reference_skill_material_too_large")
        return PreparedSkillRead(content, _hash(content), _hash(_json(material)))
    except SkillReadMaterialError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        _fail()


def validate_prepared_skill_read(material: PreparedSkillRead, *, resource: SkillResourceRef) -> PreparedSkillRead:
    """Validate retained bytes and hashes without claiming persisted authority."""
    try:
        resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
        content, content_hash, materialization_hash = material.content, material.content_hash, material.materialization_hash
        if not isinstance(content, bytes) or len(content) > MAX_REFERENCE_STORAGE_BYTES or content_hash != _hash(content):
            _fail()
        document = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20)
        if not isinstance(document, dict) or set(document) != {"schema_version", "resource", "read_only_entry", "files"}:
            _fail()
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            _fail()
        selected = SkillResourceRef.model_validate(document["resource"])
        if selected.source_kind == "builtin" and (not selected.id.startswith("builtin:") or len(selected.id) <= 8):
            _fail()
        if selected != resource:
            _fail()
        entry = ReadOnlyEntry(document["read_only_entry"])
        files = document["files"]
        paths = (entry.entry_file, *entry.required_files)
        if not isinstance(files, list) or len(files) != len(paths):
            _fail()
        total = 0
        for relative, item in zip(paths, files, strict=True):
            if not isinstance(item, dict) or set(item) != {"path", "text", "sha256"} or item["path"] != relative or type(item["text"]) is not str:
                _fail()
            raw = item["text"].encode("utf-8")
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_ENTRY_BYTES or b"\0" in raw or item["sha256"] != _hash(raw):
                _fail()
            if relative == entry.entry_file and not item["text"].strip():
                _fail()
        material = {key: document[key] for key in ("schema_version", "read_only_entry", "files")}
        if materialization_hash != _hash(_json(material)):
            _fail()
        return PreparedSkillRead(content, content_hash, materialization_hash)
    except SkillReadMaterialError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail()


def restore_skill_read(content: bytes, *, descriptor: ContentDescriptor) -> PreparedSkillRead:
    """Check decrypted material against its already scoped stored descriptor.

    Callers must use the protected store/cipher and current resource authorization.
    Successful decoding does not grant access or rebuild an execution handle.
    """
    try:
        if not isinstance(descriptor, ContentDescriptor):
            _fail()
        descriptor = ContentDescriptor.model_validate(descriptor.model_dump(mode="json"))
        if descriptor.content_kind != "skill_read" or not isinstance(content, bytes) or descriptor.byte_count != len(content):
            _fail()
        return validate_prepared_skill_read(
            PreparedSkillRead(content, descriptor.content_hash, descriptor.materialization_hash), resource=descriptor.resource
        )
    except SkillReadMaterialError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail()
