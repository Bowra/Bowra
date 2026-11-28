"""In-place editing of a Word result (docs/sprints/docx-inline-edit-2026-11-26/tech-design.md §7).

The routes live with the owner Results routes and follow the deck editor's routes: the same
owner fences, version write, refusal with the newer version's facts, upload saved as a new file
and edited-copy offer (pptx-inline-edit design §6). Every failure carries a stable code and,
where the cause cannot be shown, a diagnostic reference that is logged next to the underlying
error. Behind ``FEATURE_DOCX_INLINE_EDIT``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from bowhead_core.artifact_store import DOCUMENT_USER_EDIT_PRODUCER, ArtifactStore, VersionTargetRefused
from bowhead_core.document_edit import DOCUMENT_REFUSALS, MAX_EDITS, edited_filename, is_word_document
from bowhead_core.documents.authoring_protocol import MAX_FILE
from bowhead_core.results_read_model import artifact_kind
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.presentation_edit_service import EditTarget, edit_error, edited_copies, latest_version_facts

logger = logging.getLogger(__name__)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EDIT_SOURCE = "document_edit"
_TEMPLATE_PRODUCERS = {"template_document_v1", "template_document_internal_v1"}


class DocumentInventoryRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=128)


class DocumentEditsRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=128)
    edits: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_EDITS)


def _diagnostic(event: str, **fields: Any) -> str:
    reference = f"de_{secrets.token_hex(5)}"
    logger.warning("document_edit.%s diagnostic=%s %s", event, reference, " ".join(f"{key}={value}" for key, value in fields.items()))
    return reference


def require_enabled() -> None:
    from bowhead_core.docx_inline_edit_flag import docx_inline_edit_enabled

    if not docx_inline_edit_enabled():
        raise edit_error(status.HTTP_404_NOT_FOUND, "document_edit_disabled")


def document_editable(artifact: object) -> bool:
    """A Word file the owner may edit in place; the edit routes re-check it (docx inline edit design §7.1)."""
    if not is_word_document(getattr(artifact, "mime", None), getattr(artifact, "filename", None)):
        return False
    producer = getattr(artifact, "producer_kind", None) or ""
    return artifact_kind(artifact) in {"deliverable", "user_upload"} and producer not in _TEMPLATE_PRODUCERS


async def resolve_target(app: Any, store: ArtifactStore, *, session_id: str, user_id: str, artifact_id: str) -> EditTarget:
    """The latest version of a Word file in the owner's Results that this person may edit."""
    versions = await store.list_result_versions(session_id=session_id, user_id=user_id, artifact_id=artifact_id)
    if not versions or not any(version.id == artifact_id for version in versions):
        raise edit_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable")
    latest = versions[0]
    if latest.id != artifact_id:
        raise edit_error(status.HTTP_409_CONFLICT, "version_target_not_latest", latest=await latest_version_facts(app, store, latest.id))
    if latest.created_by != user_id or latest.status != "ready" or not document_editable(latest):
        raise edit_error(status.HTTP_404_NOT_FOUND, "document_not_editable")
    if latest.size > MAX_FILE:
        raise edit_error(status.HTTP_413_CONTENT_TOO_LARGE, "too_large", limit_bytes=MAX_FILE)
    return EditTarget(artifact=latest, mode="version" if artifact_kind(latest) == "deliverable" else "new_file")


def _unavailable(exc: Any, artifact_id: str, kind: str) -> HTTPException:
    """Map a document read or parse failure to the route's error, logging what cannot be shown."""
    if exc.code == "document_unreadable":
        return edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "document_unreadable", reason=exc.reason)
    if exc.code == "file_unavailable":
        reference = _diagnostic("read_failed", artifact_id=artifact_id, reason=exc.reason)
        return edit_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_unavailable", diagnostic_id=reference)
    return _child_failure(exc.reason, artifact_id, kind)


def _child_failure(reason: str, artifact_id: str, kind: str) -> HTTPException:
    reference = _diagnostic("child_failed", kind=kind, artifact_id=artifact_id, reason=reason)
    return edit_error(status.HTTP_503_SERVICE_UNAVAILABLE, "parse_unavailable", reason=reason, diagnostic_id=reference)


async def inventory(
    store: ArtifactStore, target: EditTarget, *, session_id: str = "", user_id: str = "", language: str = "zh-CN", app: Any = None
) -> dict[str, Any]:
    from bowhead_core.document_store_inventory import DocumentUnavailable, document_inventory

    art = target.artifact
    try:
        cached = await document_inventory(store, art)
    except DocumentUnavailable as exc:
        raise _unavailable(exc, art.id, "docx_edit_inventory") from exc
    result: dict[str, Any] = {
        "artifact_id": art.id,
        "version_no": art.version_no or 1,
        "edit_mode": target.mode,
        # The editor references this exact file in chat; attachment checks compare name, type and size.
        "file": {"filename": art.filename, "mime": art.mime, "size": art.size},
        **cached,
    }
    if target.mode == "version" and app is not None:
        # Which older versions the save will push out of the kept three (pptx-inline-edit design §13.7).
        from bowhead_gateway.file_versions_pruning import versions_dropped_by_next_save

        try:
            result["drops_on_save"] = await versions_dropped_by_next_save(app, art.id)
        except Exception:
            logger.warning("document_edit.drops_on_save_unavailable artifact=%s", art.id, exc_info=True)
    if target.mode == "new_file":
        result["new_filename"] = edited_filename(art.filename, language)
        if session_id and user_id:
            result["edited_copies"] = await edited_copies(store, session_id=session_id, user_id=user_id, upload_id=art.id, edit_source=EDIT_SOURCE)
    return result


async def save_edits(
    app: Any,
    store: ArtifactStore,
    target: EditTarget,
    *,
    session: Any,
    user_id: str,
    edits: list[dict[str, Any]],
    language: str = "zh-CN",
) -> dict[str, Any]:
    from bowhead_core.document_store_inventory import DocumentUnavailable, read_document_bytes
    from bowhead_core.untrusted_parse import run_worker_json

    base = target.artifact
    try:
        data = await read_document_bytes(store, base)
    except DocumentUnavailable as exc:
        raise _unavailable(exc, base.id, "docx_edit_apply") from exc
    result, reason = await run_worker_json("docx_edit_apply", {"edits": edits}, data=data, binary_result=True)
    if result is None:
        raise _child_failure(reason, base.id, "docx_edit_apply")
    if "refused" in result:
        refused = result["refused"]
        document = next((item for item in refused if item.get("index") is None and item.get("code") in DOCUMENT_REFUSALS), None)
        if document is not None:
            # The whole document refuses edits (change tracking on, protection, a signature): nothing to fix per edit.
            raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "document_not_editable", reason=document["code"])
        raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "edit_refused", edits=refused)
    if "unreadable" in result:
        if result["unreadable"] == "verification_failed":
            reference = _diagnostic("verification_failed", artifact_id=base.id)
            raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "document_unreadable", reason="verification_failed", diagnostic_id=reference)
        raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "document_unreadable", reason=result["unreadable"])
    output = result.pop("_result_bytes", None)
    if not output:
        raise _child_failure("no_output", base.id, "docx_edit_apply")
    if not result.get("changes"):
        # Every edit repeats the text already there; a version identical to its base is not saved.
        raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "no_changes")
    new_file = target.mode == "new_file"
    try:
        saved = await store.put_bytes(
            content=output,
            filename=edited_filename(base.filename, language) if new_file else base.filename,
            mime=base.mime or DOCX_MIME,
            created_by=user_id,
            source=EDIT_SOURCE,
            producer_kind=DOCUMENT_USER_EDIT_PRODUCER,
            kind="deliverable",
            session_id=session.id,
            workspace_id=session.workspace_id,
            created_by_turn=None,
            derived_from=base.id if new_file else None,
            version_of_target=None if new_file else base.id,
        )
    except VersionTargetRefused as exc:
        if exc.reason == "version_target_not_latest":
            raise edit_error(
                status.HTTP_409_CONFLICT, "version_target_not_latest", latest=await latest_version_facts(app, store, exc.latest_id)
            ) from exc
        raise edit_error(status.HTTP_404_NOT_FOUND, "document_not_editable", reason=exc.reason) from exc
    # A version without a Turn is placed in its session by the pin (pptx-inline-edit design §6.5).
    session_store = getattr(app.state, "session_store", None)
    if session_store is not None:
        try:
            await session_store.pin_session_attachments(session_id=session.id, artifact_ids=[saved.id])
        except Exception as exc:  # noqa: BLE001 - the version exists; report the missing pin for repair
            _diagnostic("pin_failed", artifact_id=saved.id, session_id=session.id, error_type=type(exc).__name__)
    if not new_file:
        from bowhead_gateway.file_versions_pruning import schedule_file_version_prune

        schedule_file_version_prune(app, saved.lineage_id or saved.id)
    logger.info(
        "document_edit.saved artifact_id=%s base=%s version=%s edits=%d new_file=%s",
        saved.id,
        base.id,
        saved.version_no or 1,
        len(edits),
        new_file,
    )
    return {
        "artifact_id": saved.id,
        "version_no": saved.version_no or 1,
        "filename": saved.filename,
        "new_file": new_file,
        "changes": result.get("changes", []),
    }
