"""Bounded, read-only admission for non-macro Office document containers.

This checks the outer OPC package, not layout fidelity, embedded OLE/workbooks,
business semantics, or readiness to edit. Never extract paths or follow links.
"""

from __future__ import annotations

import hashlib
import io
import posixpath
import stat
import xml.etree.ElementTree as ET  # noqa: S405 — parser below rejects all DTDs
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

MAX_OFFICE_BYTES = 32 * 1024 * 1024
MAX_PART_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_PARTS = 4096
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PPT_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
MIME_PREFIX = "application/vnd.openxmlformats-officedocument."
XLSB_MIME = "application/vnd.ms-excel.sheet.binary.macroEnabled.12"
OFFICE_MIMES = {
    ".docx": MIME_PREFIX + "wordprocessingml.document",
    ".dotx": MIME_PREFIX + "wordprocessingml.template",
    ".pptx": MIME_PREFIX + "presentationml.presentation",
    ".potx": MIME_PREFIX + "presentationml.template",
}
GENERIC_MIMES = frozenset({"", "application/octet-stream", "application/zip", "application/x-zip-compressed"})
OFFICE_TEXT_MIMES = frozenset(OFFICE_MIMES[suffix] for suffix in (".docx", ".pptx"))


class OfficePackageError(ValueError):
    """Stable, content-free reason suitable for an API error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OfficePackageInfo:
    format: str
    mime: str
    main_part: str
    sha256: str
    size_bytes: int
    part_count: int
    expanded_bytes: int
    external_relationship_count: int
    opaque_embedding_count: int


def defer_office_content(*, filename: str, mime: str) -> bool:
    """Defer automatic body reads, not classify a file as a valid template.

    Historical uploads can have ZIP MIME or conflicting declarations. Either
    Office signal is enough to defer; only byte inspection can validate it.
    Explicit, authorized reads do not use this turn/upload context policy.
    """
    return PurePosixPath(filename).suffix.lower() in OFFICE_MIMES or mime.lower().split(";", 1)[0].strip() in OFFICE_MIMES.values()


def office_upload_mime(filename: str, declared_mime: str) -> str | None:
    """Resolve only Office declarations. Generic MIME is not proof of contents."""
    suffix = PurePosixPath(filename).suffix.lower()
    mime = declared_mime.lower().split(";", 1)[0].strip()
    expected = OFFICE_MIMES.get(suffix)
    if expected is not None:
        if mime not in GENERIC_MIMES and mime != expected:
            raise OfficePackageError("file_office_type_mismatch")
        return expected
    if mime in OFFICE_MIMES.values():
        raise OfficePackageError("file_office_type_mismatch")
    return None


@dataclass
class _XmlBudget:
    nodes: int = 0


class _BoundedTree(ET.TreeBuilder):
    def __init__(self, budget: _XmlBudget) -> None:
        super().__init__()
        self.depth = 0
        self.budget = budget

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise OfficePackageError("file_office_unsafe_xml")

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self.depth += 1
        self.budget.nodes += 1
        if self.depth > 128 or self.budget.nodes > 200_000:
            raise OfficePackageError("file_office_package_limit")
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        element = super().end(tag)
        self.depth -= 1
        return element


def _xml(content: bytes, budget: _XmlBudget) -> ET.Element:
    try:
        return ET.fromstring(content, parser=ET.XMLParser(target=_BoundedTree(budget)))  # noqa: S314 — DTDs rejected, depth/nodes bounded
    except (ET.ParseError, LookupError) as exc:
        raise OfficePackageError("file_office_invalid_package") from exc


def _safe_part(name: str) -> bool:
    return bool(name) and not (
        name.startswith("/")
        or "\\" in name
        or ":" in name
        or "%" in name
        or any(ord(c) < 32 for c in name)
        or any(segment in ("", ".", "..") for segment in name.split("/"))
    )


def inspect_office_package(content: bytes, *, filename: str, declared_mime: str) -> OfficePackageInfo:
    """Verify exact upload bytes without reserializing or altering the document."""
    expected_mime = office_upload_mime(filename, declared_mime)
    if expected_mime is None:
        raise OfficePackageError("file_type_unsupported")
    if len(content) > MAX_OFFICE_BYTES:
        raise OfficePackageError("file_office_package_limit")
    if not content.startswith(b"PK\x03\x04"):
        raise OfficePackageError("file_office_invalid_package")
    try:
        return _inspect_zip(content, expected_mime=expected_mime)
    except (BadZipFile, KeyError, RuntimeError, NotImplementedError, EOFError, UnicodeError, zlib.error) as exc:
        raise OfficePackageError("file_office_invalid_package") from exc


def _inspect_zip(content: bytes, *, expected_mime: str) -> OfficePackageInfo:
    roots: dict[str, ET.Element] = {}
    xml_budget = _XmlBudget()
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        expanded = sum(entry.file_size for entry in entries)
        if len(entries) > MAX_PARTS or expanded > MAX_EXPANDED_BYTES:
            raise OfficePackageError("file_office_package_limit")
        seen = set()
        names = set()
        for entry in entries:
            name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
            if not _safe_part(name) or entry.orig_filename != entry.filename or name.casefold() in seen:
                raise OfficePackageError("file_office_invalid_package")
            seen.add(name.casefold())
            if entry.flag_bits & 1 or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED) or stat.S_ISLNK(entry.external_attr >> 16):
                raise OfficePackageError("file_office_invalid_package")
            if entry.file_size > MAX_PART_BYTES:
                raise OfficePackageError("file_office_package_limit")
            if entry.is_dir():
                if entry.file_size:
                    raise OfficePackageError("file_office_invalid_package")
                continue
            names.add(name)
            if "vbaproject" in name.casefold() or name.casefold().startswith("_xmlsignatures/"):
                raise OfficePackageError("file_office_active_content_unsupported")
        if not {"[Content_Types].xml", "_rels/.rels"}.issubset(names):
            raise OfficePackageError("file_office_invalid_package")
        for entry in entries:
            if entry.is_dir():
                continue
            # Reading every member checks CRC/overlap and enforces actual bounds,
            # but binary embeddings remain opaque and are never executed.
            with archive.open(entry) as member:
                part = member.read(MAX_PART_BYTES + 1)
            if len(part) != entry.file_size:
                raise OfficePackageError("file_office_invalid_package")
            if entry.filename.endswith((".xml", ".rels")):
                root = _xml(part, xml_budget)
                if entry.filename.endswith(".rels") or entry.filename in {"[Content_Types].xml", "word/document.xml", "ppt/presentation.xml"}:
                    roots[entry.filename] = root

    types = roots["[Content_Types].xml"]
    if types.tag != f"{{{CT_NS}}}Types":
        raise OfficePackageError("file_office_invalid_package")
    overrides, defaults = {}, {}
    for node in types:
        content_type = node.get("ContentType", "")
        # XLSB's standard type includes "macroEnabled" even for opaque data
        # workbooks. Permit only embedded XLSB parts below; never open them.
        if content_type != XLSB_MIME and any(marker in content_type.casefold() for marker in ("macroenabled", "vbaproject", "digital-signature")):
            raise OfficePackageError("file_office_active_content_unsupported")
        if node.tag == f"{{{CT_NS}}}Override":
            path = node.get("PartName", "")
            if not path.startswith("/") or path[1:] not in names or path in overrides:
                raise OfficePackageError("file_office_invalid_package")
            overrides[path] = content_type
        elif node.tag == f"{{{CT_NS}}}Default":
            extension = node.get("Extension", "").lower()
            if not extension or extension in defaults:
                raise OfficePackageError("file_office_invalid_package")
            defaults[extension] = content_type
        else:
            raise OfficePackageError("file_office_invalid_package")
        if not content_type:
            raise OfficePackageError("file_office_invalid_package")
    resolved_types = {
        name: overrides.get("/" + name, defaults.get(name.rsplit(".", 1)[-1].lower())) for name in names if name != "[Content_Types].xml"
    }
    if any(value is None for value in resolved_types.values()):
        raise OfficePackageError("file_office_invalid_package")
    for name, mime in resolved_types.items():
        if mime == XLSB_MIME and not (name.startswith(("ppt/embeddings/", "word/embeddings/")) and name.endswith(".xlsb")):
            raise OfficePackageError("file_office_active_content_unsupported")
    main_types = {mime + ".main+xml": mime for mime in OFFICE_MIMES.values()}
    mains = [(name, main_types[mime]) for name, mime in resolved_types.items() if mime in main_types]
    if len(mains) != 1:
        raise OfficePackageError("file_office_invalid_package")
    main_part, actual_mime = mains[0]
    if actual_mime != expected_mime:
        raise OfficePackageError("file_office_type_mismatch")
    is_word = actual_mime.startswith(MIME_PREFIX + "wordprocessingml.")
    if main_part != ("word/document.xml" if is_word else "ppt/presentation.xml"):
        raise OfficePackageError("file_office_invalid_package")
    expected_root = f"{{{WORD_NS}}}document" if is_word else f"{{{PPT_NS}}}presentation"
    if roots[main_part].tag != expected_root or (is_word and roots[main_part].find(f"{{{WORD_NS}}}body") is None):
        raise OfficePackageError("file_office_invalid_package")

    external_count, office_targets = 0, []
    for path, root in roots.items():
        if not path.endswith(".rels"):
            continue
        if root.tag != f"{{{REL_NS}}}Relationships":
            raise OfficePackageError("file_office_invalid_package")
        parent = posixpath.dirname(posixpath.dirname(path))
        ids = set()
        for rel in root:
            rid, target, rel_type = rel.get("Id"), rel.get("Target", ""), rel.get("Type", "")
            if rel.tag != f"{{{REL_NS}}}Relationship" or not rid or rid in ids or not target or not rel_type:
                raise OfficePackageError("file_office_invalid_package")
            ids.add(rid)
            if any(marker in rel_type.casefold() for marker in ("vbaproject", "digital-signature")):
                raise OfficePackageError("file_office_active_content_unsupported")
            if rel.get("TargetMode") == "External":
                if rel_type == OFFICE_REL:
                    raise OfficePackageError("file_office_invalid_package")
                external_count += 1
                continue  # All external URLs are preserved, never requested.
            if rel.get("TargetMode") not in (None, "Internal"):
                raise OfficePackageError("file_office_invalid_package")
            try:
                uri = urlsplit(target)
            except ValueError as exc:
                raise OfficePackageError("file_office_invalid_package") from exc
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(parent, target))
            if uri.scheme or uri.netloc or uri.query or uri.fragment or resolved not in names or "\\" in target:
                raise OfficePackageError("file_office_invalid_package")
            if path == "_rels/.rels" and rel_type == OFFICE_REL:
                office_targets.append(resolved)
    if office_targets != [main_part]:
        raise OfficePackageError("file_office_invalid_package")
    suffix = next(ext for ext, mime in OFFICE_MIMES.items() if mime == actual_mime)
    return OfficePackageInfo(
        suffix[1:],
        actual_mime,
        main_part,
        hashlib.sha256(content).hexdigest(),
        len(content),
        len(names),
        expanded,
        external_count,
        sum("/embeddings/" in name for name in names),
    )
