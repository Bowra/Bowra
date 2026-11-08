"""Independent, authorized-by-caller artifact representations (Results B0a-3)."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Hashable
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

import anyio
import httpx2
from bowhead_core.artifact_store import mime_to_family
from bowhead_core.documents.authoring_executor import AuthoringExecutor
from bowhead_core.documents.authoring_protocol import MAX_FILE
from bowhead_core.documents.package_io import OFFICE_MIMES, office_upload_mime
from bowhead_core.results_read_model import artifact_supports_raw, derive_artifact_viewer_profile
from fastapi import HTTPException, Response

from bowhead_gateway.dtos import FilePreviewDTO

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore
    from bowhead_core.documents.executor import DocumentExecutor

Representation = Literal["preview", "raw", "details"]
RepresentationRequest = Literal[Representation, "pptx-pdf"]
RAW_LIMIT = 512 * 1024
PREVIEW_LIMIT = 100 * 1024 * 1024  # Same maximum as file upload, not the Raw budget.
log = logging.getLogger(__name__)
_MAX_PRESENTATION_JOBS = 4
_PRESENTATION_QUEUE_SECONDS = 30


class ArtifactRepresentationDTO(FilePreviewDTO):
    representation: Representation
    revision: str
    encoding: str | None = None
    total_byte_count: int | None = None
    returned_byte_count: int | None = None  # UTF-8 bytes in text, before JSON escaping.
    details: dict[str, Any] | None = None


def representation_error(kind: str) -> HTTPException:
    statuses = {"unsupported": 415, "too_large": 413, "corrupt": 422, "unavailable": 503, "busy": 503, "scope_unavailable": 404}
    code = "artifact_scope_unavailable" if kind == "scope_unavailable" else f"artifact_representation_{kind}"
    headers = {"Cache-Control": "no-store"}
    if kind == "busy":
        headers["Retry-After"] = "2"
    return HTTPException(statuses[kind], detail={"error": {"code": code}}, headers=headers)


class PresentationPreviewCoordinator:
    """Application-owned in-flight reads; completed PDFs are not retained."""

    def __init__(self) -> None:
        self._jobs: dict[Hashable, asyncio.Task[bytes]] = {}
        self._slot = anyio.Semaphore(1)
        self._closed = False

    async def render(self, key: Hashable, build: Callable[[], Awaitable[bytes]]) -> bytes:
        if self._closed:
            raise representation_error("unavailable")
        task = self._jobs.get(key)
        if task is None:
            if len(self._jobs) >= _MAX_PRESENTATION_JOBS:
                raise representation_error("busy")
            task = asyncio.create_task(self._render(build), name="presentation-preview")
            self._jobs[key] = task
            task.add_done_callback(partial(self._finished, key))
        # A browser detaches its read, not the execution shared with other readers.
        return await asyncio.shield(task)

    async def _render(self, build: Callable[[], Awaitable[bytes]]) -> bytes:
        acquired = False
        try:
            try:
                with anyio.fail_after(_PRESENTATION_QUEUE_SECONDS):
                    await self._slot.acquire()
                    acquired = True
            except TimeoutError:
                raise representation_error("busy") from None
            return await build()
        finally:
            if acquired:
                self._slot.release()

    def _finished(self, key: Hashable, task: asyncio.Task[bytes]) -> None:
        if self._jobs.get(key) is task:
            del self._jobs[key]
        if not task.cancelled():
            error = task.exception()
            if error is not None and not isinstance(error, HTTPException):
                log.warning("artifact.presentation_job_failed error_type=%s", type(error).__name__)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()


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


async def _presentation_source_version(store: ArtifactStore, art: Artifact) -> str | None:
    try:
        head = await store.backend.head(art.path)
    except Exception as exc:
        log.warning("artifact.presentation_head_failed artifact_id=%s error_type=%s", art.id, type(exc).__name__)
        raise representation_error("unavailable") from exc
    if head is None or head.size != art.size:
        raise representation_error("unavailable")
    return head.identity or head.etag


async def build_presentation_view(
    store: ArtifactStore,
    art: Artifact,
    executor: Callable[[], DocumentExecutor] | None,
    previews: PresentationPreviewCoordinator | None,
) -> Response:
    if office_upload_mime(art.filename, art.mime) != OFFICE_MIMES[".pptx"]:
        raise representation_error("unsupported")
    if art.size > MAX_FILE:
        raise representation_error("too_large")
    if executor is None or previews is None:
        raise representation_error("unavailable")
    try:
        document = executor()
        renderer = AuthoringExecutor(root=document.root, url=document.url, token=document.token)
        version = await _presentation_source_version(store, art)
        # Unknown backend identities remain readable, but cannot identify shared work.
        key = (
            id(store),
            art.id,
            art.path,
            art.size,
            version if version is not None else object(),
            str(document.root),
            document.url,
            renderer.profile,
        )

        async def convert() -> bytes:
            if await _presentation_source_version(store, art) != version:
                raise representation_error("unavailable")
            source = await _read_prefix(store, art, art.size + 1)
            if len(source) != art.size:
                raise representation_error("corrupt")
            return await renderer.view_presentation(source)

        pdf = await previews.render(key, convert)
        if await _presentation_source_version(store, art) != version:
            raise representation_error("unavailable")
    except (httpx2.HTTPError, ValueError, OSError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        log.warning("artifact.presentation_view_failed artifact_id=%s error_code=%s", art.id, code)
        if code in {"document_slide_limit", "file_office_package_limit", "document_preview_limit"}:
            raise representation_error("too_large") from exc
        if code in {"file_office_invalid_package", "document_pdf_invalid", "document_pdf_page_mismatch"}:
            raise representation_error("corrupt") from exc
        raise representation_error("unavailable") from exc
    return Response(content=pdf, media_type="application/pdf", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


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
