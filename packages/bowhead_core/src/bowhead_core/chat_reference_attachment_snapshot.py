"""Original attachment material for an effect-free Lead delegation resume."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ImageRef

from bowhead_core.documents.package_io import OFFICE_TEXT_MIMES

MAX_LEAD_ATTACHMENT_FILE_BYTES = 20 * 1024 * 1024
MAX_LEAD_ATTACHMENT_TOTAL_BYTES = 32 * 1024 * 1024
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})

if TYPE_CHECKING:
    from bowhead_core.chat_reference_execution import ReferencePrimaryInput
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.tool_registry import ToolExecutionContext


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def supports_lead_attachment_mime(mime: str) -> bool:
    return (
        mime.startswith("text/")
        or mime in {"application/json", "application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        or mime in _IMAGE_MIMES
        or mime in OFFICE_TEXT_MIMES
    )


def supports_lead_attachment_refs(refs: Sequence[Mapping[str, Any]], ids: Sequence[str]) -> bool:
    """Share bounded text/PDF/XLSX previews and supported vision images."""
    if not ids:
        return not refs
    if not refs or len(refs) != len(ids) or len(refs) > 32:
        return False
    total_bytes = 0
    for ref, artifact_id in zip(refs, ids, strict=True):
        mime = ref.get("mime")
        size = ref.get("size")
        if (
            ref.get("fileId", ref.get("file_id")) != artifact_id
            or ref.get("bundleArtifactIds", ref.get("bundle_artifact_ids"))
            or not isinstance(mime, str)
            or not supports_lead_attachment_mime(mime)
            or type(size) is not int
            or size < 0
            or size > MAX_LEAD_ATTACHMENT_FILE_BYTES
        ):
            return False
        total_bytes += size
        if total_bytes > MAX_LEAD_ATTACHMENT_TOTAL_BYTES:
            return False
    return True


async def seal_attachment_snapshot(primary: ReferencePrimaryInput, ctx: ToolExecutionContext, attachment_context: str) -> dict[str, Any]:
    """Bind the exact parsed model input to bytes before recording delegation."""
    return await seal_source_attachment_snapshot(primary.source, ctx, attachment_context)


async def seal_source_attachment_snapshot(source: ChatTurnUserSource, ctx: ToolExecutionContext, attachment_context: str) -> dict[str, Any]:
    """Bind parsed bytes to a canonical source without manufacturing a primary."""
    return await seal_prepared_attachment_snapshot(source.attachment_ids, ctx, attachment_context)


async def seal_prepared_attachment_snapshot(ids: Sequence[str], ctx: ToolExecutionContext, attachment_context: str) -> dict[str, Any]:
    """Seal an already-authorized selection using the shared material limits."""
    store = ctx.artifact_store
    if store is None:
        raise ValueError("reference_attachment_material_unavailable")
    parsed = {item.artifact_id: item for item in getattr(ctx, "_psi_parsed_results", None) or [] if item.artifact_id in ids}
    if len(parsed) != len(ids):
        raise ValueError("reference_attachment_material_unavailable")
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for artifact_id in ids:
        item = parsed[artifact_id]
        total_bytes += item.size
        if (
            item.error
            or (not item.content_hash and not item.content_deferred)
            or not supports_lead_attachment_mime(item.mime)
            or item.size < 0
            or item.size > MAX_LEAD_ATTACHMENT_FILE_BYTES
            or total_bytes > MAX_LEAD_ATTACHMENT_TOTAL_BYTES
        ):
            raise ValueError("reference_attachment_material_unavailable")
        art = await store.get(artifact_id)
        if art is None or (art.filename, art.mime, art.size) != (item.filename, item.mime, item.size):
            raise ValueError("reference_attachment_material_changed")
        if item.content_deferred:
            from bowhead_core.documents.upload_confirmation import confirmed_content_hash

            if item.mime not in OFFICE_TEXT_MIMES:
                raise ValueError("reference_attachment_material_unavailable")
            digest = await confirmed_content_hash(store, art, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
        else:
            content = await store.backend.read_bounded(art.path, limit=item.size)
            digest = _digest(content)
            if len(content) != item.size or digest != item.content_hash:
                raise ValueError("reference_attachment_material_changed")
        if item.mime.startswith("image/"):
            if not item.image_base64 or base64.b64decode(item.image_base64, validate=True) != content:
                raise ValueError("reference_attachment_image_unavailable")
        files.append(
            {
                "artifact_id": artifact_id,
                "filename": item.filename,
                "mime": item.mime,
                "size": item.size,
                "content_hash": digest,
                **({"content_deferred": True} if item.content_deferred else {}),
            }
        )
    return {"schema_version": 2 if any(item.get("content_deferred") for item in files) else 1, "context": attachment_context, "files": files}


async def reopen_attachment_snapshot(
    primary: ReferencePrimaryInput, ctx: ToolExecutionContext, snapshot: dict[str, Any] | None
) -> tuple[str, list[ImageRef]]:
    """Recheck original bytes and current authority; never re-extract text."""
    if not isinstance(snapshot, dict) or not snapshot.get("context"):
        raise ValueError("reference_attachment_continuation_unavailable")
    return await reopen_source_attachment_snapshot(primary.source, ctx, snapshot)


async def reopen_source_attachment_snapshot(
    source: ChatTurnUserSource, ctx: ToolExecutionContext, snapshot: dict[str, Any] | None
) -> tuple[str, list[ImageRef]]:
    """Use the same exact-byte checks for an already authorized ordinary source."""
    return await reopen_prepared_attachment_snapshot(source.attachment_ids, ctx, snapshot)


async def reopen_prepared_attachment_snapshot(
    ids: Sequence[str], ctx: ToolExecutionContext, snapshot: dict[str, Any] | None
) -> tuple[str, list[ImageRef]]:
    """Reopen an already-authorized selection without extraction or execution."""
    store = ctx.artifact_store
    if store is None or not isinstance(snapshot, dict) or set(snapshot) != {"schema_version", "context", "files"}:
        raise ValueError("reference_attachment_continuation_unavailable")
    context, files = snapshot.get("context"), snapshot.get("files")
    if (
        snapshot.get("schema_version") not in (1, 2)
        or not isinstance(context, str)
        or not isinstance(files, list)
        or len(files) != len(ids)
        or len(files) > 32
    ):
        raise ValueError("reference_attachment_continuation_unavailable")
    from bowhead_core.subagent import _VISION_MESSAGE_MIMES

    images: list[ImageRef] = []
    total_bytes = 0
    for artifact_id, item in zip(ids, files, strict=True):
        if (
            not isinstance(item, dict)
            or set(item)
            != {"artifact_id", "filename", "mime", "size", "content_hash"}
            | ({"content_deferred"} if snapshot["schema_version"] == 2 and item.get("content_deferred") is True else set())
            or item.get("artifact_id") != artifact_id
            or not isinstance(item.get("filename"), str)
            or not isinstance(item.get("mime"), str)
            or type(item.get("size")) is not int
            or item["size"] < 0
            or item["size"] > MAX_LEAD_ATTACHMENT_FILE_BYTES
            or not isinstance(item.get("content_hash"), str)
            or len(item["content_hash"]) != 71
            or not item["content_hash"].startswith("sha256:")
        ):
            raise ValueError("reference_attachment_continuation_unavailable")
        total_bytes += item["size"]
        if total_bytes > MAX_LEAD_ATTACHMENT_TOTAL_BYTES or not supports_lead_attachment_mime(item["mime"]):
            raise ValueError("reference_attachment_continuation_unavailable")
        art = await store.get(artifact_id)
        if art is None or (art.filename, art.mime, art.size) != (item["filename"], item["mime"], item["size"]):
            raise ValueError("reference_attachment_material_changed")
        if item.get("content_deferred"):
            from bowhead_core.documents.upload_confirmation import confirmed_content_hash

            if (
                item["mime"] not in OFFICE_TEXT_MIMES
                or await confirmed_content_hash(store, art, user_id=ctx.user_id, workspace_id=ctx.workspace_id) != item["content_hash"]
            ):
                raise ValueError("reference_attachment_material_changed")
            continue
        content = await store.backend.read_bounded(art.path, limit=item["size"])
        if len(content) != item["size"] or _digest(content) != item["content_hash"]:
            raise ValueError("reference_attachment_material_changed")
        if art.mime.startswith("image/"):
            if art.mime not in _VISION_MESSAGE_MIMES:
                raise ValueError("reference_attachment_image_unavailable")
            images.append(ImageRef(base64=base64.b64encode(content).decode("ascii"), mime=art.mime))
    return context, images
