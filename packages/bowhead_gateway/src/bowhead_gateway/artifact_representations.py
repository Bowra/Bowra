"""Independent, authorized-by-caller artifact representations (Results B0a-3)."""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.artifact_store import mime_to_family
from bowhead_core.results_read_model import artifact_supports_raw, derive_artifact_viewer_profile
from fastapi import HTTPException

from bowhead_gateway.dtos import FilePreviewDTO

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore

Representation = Literal["preview", "raw", "details"]
RAW_LIMIT = 512 * 1024
PREVIEW_LIMIT = 100 * 1024 * 1024  # Same maximum as file upload, not the Raw budget.
log = logging.getLogger(__name__)


class ArtifactRepresentationDTO(FilePreviewDTO):
    representation: Representation
    revision: str
    encoding: str | None = None
    total_byte_count: int | None = None
    returned_byte_count: int | None = None  # UTF-8 bytes in text, before JSON escaping.
    details: dict[str, Any] | None = None


def representation_error(kind: str) -> HTTPException:
    statuses = {"unsupported": 415, "too_large": 413, "corrupt": 422, "unavailable": 503, "scope_unavailable": 404}
    code = "artifact_scope_unavailable" if kind == "scope_unavailable" else f"artifact_representation_{kind}"
    return HTTPException(statuses[kind], detail={"error": {"code": code}}, headers={"Cache-Control": "no-store"})


def safe_metadata(art: Artifact) -> dict[str, Any]:
    # No storage path, owner identity, reference IDs or origin_query (closure side channel).
    # Storage trust is not evaluated export trust. Metadata cannot supply that
    # conclusion without inspecting evidence; callers retain the catalog projection.
    return {
        key: getattr(art, key, None)
        for key in (
            "id",
            "filename",
            "mime",
            "size",
            "source",
            "created_at",
            "kind",
            "producer_kind",
        )
    }


def _revision(art: Artifact, mode: Representation, content: bytes = b"") -> str:
    metadata = {**safe_metadata(art), "trust": art.trust}
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=True).encode())
    digest.update(mode.encode())
    digest.update(content)
    return digest.hexdigest()


async def _read_prefix(store: ArtifactStore, art: Artifact, limit: int) -> bytes:
    chunks = bytearray()
    try:
        if art.size == 0:
            # S3 rejects any Range on a zero-length object. HEAD distinguishes
            # a valid empty file from a missing blob without reading its bytes.
            head = await store.backend.head(art.path)
            if head is None or head.size != 0:
                raise OSError("empty artifact metadata mismatch")
            return b""
        async for chunk in store.iter_read_range(art, start=0, end=limit):
            if len(chunks) + len(chunk) > limit:
                raise ValueError("backend range exceeded")
            chunks.extend(chunk)
        if len(chunks) < min(limit, art.size):
            raise OSError("incomplete artifact read")
    except Exception as exc:
        # Diagnostics remain server-side; the response never contains parser/path text.
        log.warning("artifact.representation_read_failed artifact_id=%s error_type=%s", art.id, type(exc).__name__)
        raise representation_error("unavailable") from exc
    return bytes(chunks)


def _raw_text(data: bytes, *, complete: bool) -> tuple[str, str, bool]:
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise representation_error("corrupt")
    encoding, offset = "utf-8", 0
    for bom, candidate in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            encoding, offset = candidate, len(bom)
            break
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    try:
        text = decoder.decode(data[offset:RAW_LIMIT], final=complete and len(data) <= RAW_LIMIT)
        # Probe validates a split code point, but is not part of the returned input window.
        if len(data) > RAW_LIMIT:
            decoder.decode(data[RAW_LIMIT:], final=complete)
    except UnicodeError as exc:
        raise representation_error("corrupt") from exc
    encoded = text.encode("utf-8")
    clipped = len(encoded) > RAW_LIMIT
    if clipped:
        # The only ignored sequence is the last UTF-8 code point cut by our byte bound.
        text = codecs.getincrementaldecoder("utf-8")().decode(encoded[:RAW_LIMIT], final=False)
    return text, encoding, clipped


async def build_representation(store: ArtifactStore, art: Artifact, mode: Representation) -> ArtifactRepresentationDTO:
    family = mime_to_family(art.mime)
    if mode == "details":
        return ArtifactRepresentationDTO(family=family, representation=mode, revision=_revision(art, mode), details=safe_metadata(art))
    if mode == "raw":
        if not artifact_supports_raw(art.mime, art.filename):
            raise representation_error("unsupported")
        data = await _read_prefix(store, art, RAW_LIMIT + 4)
        text, encoding, clipped = _raw_text(data, complete=len(data) >= art.size)
        return ArtifactRepresentationDTO(
            family=family,
            representation=mode,
            revision=_revision(art, mode, data),
            mode="text",
            text=text,
            encoding=encoding,
            truncated=art.size > RAW_LIMIT or clipped,
            total_byte_count=art.size,
            returned_byte_count=len(text.encode("utf-8")),
        )
    # Native browser media uses the existing authorized byte endpoints, not a JS blob cache.
    normalized = art.mime.split(";", 1)[0].strip().lower()
    if derive_artifact_viewer_profile(art.mime, art.filename) == "unsupported":
        raise representation_error("unsupported")
    if normalized.startswith(("image/", "video/")) or normalized in {"text/html", "application/xhtml+xml"}:
        if normalized == "image/svg+xml":
            raise representation_error("unsupported")
        return ArtifactRepresentationDTO(family=family, representation=mode, revision=_revision(art, mode), mode="image")
    if art.size > PREVIEW_LIMIT:
        raise representation_error("too_large")
    data = await _read_prefix(store, art, min(art.size + 1, PREVIEW_LIMIT + 1))
    if len(data) > PREVIEW_LIMIT:
        raise representation_error("too_large")
    from bowhead_gateway.routes.files import build_file_preview

    try:
        preview = await build_file_preview(store, art, mode="auto", max_rows=100, data=data)
    except Exception as exc:
        log.warning("artifact.representation_parse_failed artifact_id=%s error_type=%s", art.id, type(exc).__name__)
        raise representation_error("corrupt") from exc
    if preview.error_code or preview.error:
        raise representation_error("corrupt")
    result = ArtifactRepresentationDTO(**preview.model_dump(), representation=mode, revision=_revision(art, mode, data))
    if len(result.model_dump_json().encode("utf-8")) > 8 * 1024 * 1024:
        raise representation_error("too_large")
    return result
