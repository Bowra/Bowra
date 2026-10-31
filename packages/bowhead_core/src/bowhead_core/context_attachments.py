"""Source proofs for model history, independent of continuation payload limits."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ContextAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    filename: str
    mime: str
    size: int = Field(ge=0)
    content_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class AttachmentContextDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    turn_id: str = Field(min_length=1, max_length=128)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[ContextAttachment]


async def _authorize(ctx, db, source, files: list[ContextAttachment]) -> list:
    from bowhead_core.chat_attachment_access import validate_chat_attachment_access_in_connection

    store = ctx.artifact_store
    if store is None or Path(store.db_path).resolve() != Path(ctx.session_store.db_path).resolve():
        raise ValueError("context_attachment_store_unavailable")
    await validate_chat_attachment_access_in_connection(
        db,
        refs=source.attachment_refs,
        runtime_ids=source.attachment_ids,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_id=ctx.session_id,
    )
    if len({item.artifact_id for item in files}) != len(files):
        raise ValueError("context_attachment_duplicate")
    artifacts = []
    for item in files:
        if item.artifact_id in source.attachment_ids:
            art = await store.get(item.artifact_id, connection=db)
        else:
            # Session material keeps its original membership and user boundary;
            # a historical pin is not an independent permission grant.
            matches = await store.list_for_session(
                session_id=ctx.session_id, user_id=ctx.user_id, artifact_id=item.artifact_id, limit=1, connection=db
            )
            art = matches[0] if len(matches) == 1 else None
            if art is not None and art.kind == "user_upload":
                ready = await store.get_ready_uploads_for_chat([art.id], user_id=ctx.user_id, workspace_id=ctx.workspace_id, connection=db)
                art = ready.get(art.id)
        if art is None or art.status != "ready" or (art.filename, art.mime, art.size) != (item.filename, item.mime, item.size):
            raise ValueError("context_attachment_source_changed")
        artifacts.append(art)
    return artifacts


async def _validate_bytes(ctx, files: list[ContextAttachment], artifacts: list) -> None:
    for item, art in zip(files, artifacts, strict=True):
        if item.content_hash is not None:
            data = await ctx.artifact_store.backend.read_bounded(art.path, limit=item.size)
            if len(data) != item.size or "sha256:" + hashlib.sha256(data).hexdigest() != item.content_hash:
                raise ValueError("context_attachment_bytes_changed")


async def capture_attachment_dependencies(ctx, db, source) -> AttachmentContextDependencies | None:
    from bowhead_core.chat_turn_source import chat_turn_user_source_projection
    from bowhead_core.ordinary_lead_continuation import content_hash

    parsed = list(getattr(ctx, "_psi_parsed_results", []) or [])
    pins = list(getattr(ctx, "_psi_pinned_snapshot", []) or [])
    if not source.attachment_ids and not parsed and not pins:
        return None
    allowed = set(source.attachment_ids) | {item["artifact_id"] for item in pins}
    files = {}
    unavailable = set()
    for item in parsed:
        if item.artifact_id not in allowed:
            raise ValueError("context_attachment_selection_changed")
        has_body = bool(item.extracted_text or item.image_base64 or item.inventory)
        if has_body and item.content_hash is None:
            raise ValueError("context_attachment_digest_unavailable")
        # Failed pins can legitimately be represented by an unavailable notice.
        # They contribute no retained source body and must not block the Turn.
        if item.error and not has_body and item.artifact_id not in source.attachment_ids:
            unavailable.add(item.artifact_id)
            continue
        retained = ContextAttachment(
            artifact_id=item.artifact_id,
            filename=item.filename,
            mime=item.mime,
            size=item.size,
            content_hash=item.content_hash if has_body else None,
        )
        if item.image_base64 and "sha256:" + hashlib.sha256(base64.b64decode(item.image_base64, validate=True)).hexdigest() != retained.content_hash:
            raise ValueError("context_attachment_image_changed")
        files[item.artifact_id] = retained
    for item in pins:
        identity = item["artifact_id"]
        if identity not in files and identity not in unavailable:
            files[identity] = ContextAttachment(artifact_id=identity, filename=item["filename"], mime=item["mime"], size=item["size"])
    values = list(files.values())
    artifacts = await _authorize(ctx, db, source, values)
    await _validate_bytes(ctx, values, artifacts)
    return AttachmentContextDependencies(turn_id=ctx.turn_id, source_hash=content_hash(chat_turn_user_source_projection(source)), files=values)


async def validate_attachment_dependencies(ctx, db, source, proof: AttachmentContextDependencies, *, read_bytes: bool = True) -> None:
    from bowhead_core.chat_turn_source import chat_turn_user_source_projection
    from bowhead_core.ordinary_lead_continuation import content_hash

    if source is None or source.session_id != ctx.session_id or content_hash(chat_turn_user_source_projection(source)) != proof.source_hash:
        raise ValueError("context_attachment_source_changed")
    artifacts = await _authorize(ctx, db, source, proof.files)
    if read_bytes:
        await _validate_bytes(ctx, proof.files, artifacts)
