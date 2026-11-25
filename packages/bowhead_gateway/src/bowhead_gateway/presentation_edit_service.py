"""In-place editing of a PowerPoint result (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md §6).

The routes live with the owner Results routes; this module holds the checks, the bounded
parse calls and the version write. Every failure carries a stable code and, where the cause
cannot be shown, a diagnostic reference that is logged next to the underlying error.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from bowhead_core.artifact_store import PRESENTATION_USER_EDIT_PRODUCER, Artifact, ArtifactStore, VersionTargetRefused
from bowhead_core.documents.authoring_protocol import MAX_FILE
from bowhead_core.presentation_edit import MAX_EDITS, edited_filename, feature_enabled, is_presentation
from bowhead_core.results_read_model import artifact_kind
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_TEMPLATE_PRODUCERS = {"template_document_v1", "template_document_internal_v1"}

EditMode = Literal["version", "new_file"]


class PresentationInventoryRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=128)


class PresentationEditsRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=128)
    edits: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_EDITS)


@dataclass(frozen=True, slots=True)
class EditTarget:
    artifact: Artifact
    mode: EditMode


def edit_error(status_code: int, code: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, **details}},
        headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
    )


def _diagnostic(event: str, **fields: Any) -> str:
    reference = f"pe_{secrets.token_hex(5)}"
    logger.warning("presentation_edit.%s diagnostic=%s %s", event, reference, " ".join(f"{key}={value}" for key, value in fields.items()))
    return reference


def require_feature() -> None:
    if not feature_enabled():
        raise edit_error(status.HTTP_404_NOT_FOUND, "presentation_edit_unavailable")


async def latest_version_facts(app: Any, store: ArtifactStore, latest_id: str | None) -> dict[str, Any] | None:
    """Who saved the newer version and when, for the conflict message (design §6.3)."""
    if not latest_id:
        return None
    latest = await store.get(latest_id)
    if latest is None:
        return None
    facts: dict[str, Any] = {
        "artifact_id": latest.id,
        "version_no": latest.version_no or 1,
        "created_at": latest.created_at,
        "produced_by": "user" if (latest.producer_kind or "") == PRESENTATION_USER_EDIT_PRODUCER else "agent",
        "request_excerpt": None,
    }
    session_store = getattr(app.state, "session_store", None)
    if latest.created_by_turn and session_store is not None:
        try:
            turn = await session_store.get_turn(latest.created_by_turn)
        except Exception:  # noqa: BLE001 - the excerpt is optional context
            turn = None
        if turn is not None and turn.user_text:
            text = " ".join(turn.user_text.split())
            facts["request_excerpt"] = text[:40] + ("…" if len(text) > 40 else "")
    return facts


async def resolve_target(app: Any, store: ArtifactStore, *, session_id: str, user_id: str, artifact_id: str) -> EditTarget:
    """The latest version of a deck in the owner's Results that this person may edit."""
    versions = await store.list_result_versions(session_id=session_id, user_id=user_id, artifact_id=artifact_id)
    if not versions or not any(version.id == artifact_id for version in versions):
        raise edit_error(status.HTTP_404_NOT_FOUND, "results_scope_unavailable")
    latest = versions[0]
    if latest.id != artifact_id:
        raise edit_error(status.HTTP_409_CONFLICT, "version_target_not_latest", latest=await latest_version_facts(app, store, latest.id))
    kind = artifact_kind(latest)
    if (
        latest.created_by != user_id
        or latest.status != "ready"
        or not is_presentation(latest.mime, latest.filename)
        or kind not in {"deliverable", "user_upload"}
        or (latest.producer_kind or "") in _TEMPLATE_PRODUCERS
    ):
        raise edit_error(status.HTTP_404_NOT_FOUND, "presentation_not_editable")
    if latest.size > MAX_FILE:
        raise edit_error(status.HTTP_413_CONTENT_TOO_LARGE, "too_large", limit_bytes=MAX_FILE)
    return EditTarget(artifact=latest, mode="version" if kind == "deliverable" else "new_file")


def _deck_error(exc: Any, artifact_id: str, kind: str) -> HTTPException:
    """Map a deck read or parse failure to the route's error, logging what cannot be shown."""
    if exc.code == "presentation_unreadable":
        return edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "presentation_unreadable", reason=exc.reason)
    if exc.code == "file_unavailable":
        reference = _diagnostic("read_failed", artifact_id=artifact_id, reason=exc.reason)
        return edit_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_unavailable", diagnostic_id=reference)
    return _child_failure(exc.reason, artifact_id, kind)


def _child_failure(reason: str, artifact_id: str, kind: str) -> HTTPException:
    reference = _diagnostic("child_failed", kind=kind, artifact_id=artifact_id, reason=reason)
    return edit_error(status.HTTP_503_SERVICE_UNAVAILABLE, "parse_unavailable", reason=reason, diagnostic_id=reference)


async def edited_copies(store: ArtifactStore, *, session_id: str, user_id: str, upload_id: str) -> list[dict[str, Any]]:
    """Files this person already made by editing an upload, newest first (design §6.4).

    The editor offers them before a second save would split the person's work across files.
    Each entry is the newest live version of a file whose first version records the upload.
    A helper's revised copy of the upload records it too (pptx dogfood 2026-11-25), and its
    newest version may come from either producer.
    """
    rows, _ = await store.find_session_results(session_id=session_id, user_id=user_id, sources=["presentation_edit", "python_sandbox"], limit=50)
    copies: list[dict[str, Any]] = []
    for row in rows:
        root = row if row.lineage_root == row.id else await store.get(row.lineage_root)
        if root is not None and root.derived_from == upload_id:
            copies.append({"artifact_id": row.id, "filename": row.filename, "version_no": row.version_no or 1})
    return copies


async def inventory(store: ArtifactStore, target: EditTarget, *, session_id: str = "", user_id: str = "", language: str = "zh-CN") -> dict[str, Any]:
    from bowhead_core.presentation_deck import DeckUnavailable, deck_inventory

    art = target.artifact
    try:
        cached = await deck_inventory(store, art)
    except DeckUnavailable as exc:
        raise _deck_error(exc, art.id, "pptx_edit_inventory") from exc
    result: dict[str, Any] = {
        "artifact_id": art.id,
        "version_no": art.version_no or 1,
        "edit_mode": target.mode,
        # The editor references this exact file in chat; attachment checks compare name, type and size.
        "file": {"filename": art.filename, "mime": art.mime, "size": art.size},
        **cached,
    }
    if target.mode == "new_file":
        result["new_filename"] = edited_filename(art.filename, language)
        if session_id and user_id:
            result["edited_copies"] = await edited_copies(store, session_id=session_id, user_id=user_id, upload_id=art.id)
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
    from bowhead_core.presentation_deck import DeckUnavailable, read_deck_bytes
    from bowhead_core.untrusted_parse import run_worker_json

    base = target.artifact
    try:
        data = await read_deck_bytes(store, base)
    except DeckUnavailable as exc:
        raise _deck_error(exc, base.id, "pptx_edit_apply") from exc
    result, reason = await run_worker_json("pptx_edit_apply", {"edits": edits}, data=data, binary_result=True)
    if result is None:
        raise _child_failure(reason, base.id, "pptx_edit_apply")
    if "refused" in result:
        raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "edit_refused", edits=result["refused"])
    if "unreadable" in result:
        if result["unreadable"] == "verification_failed":
            reference = _diagnostic("verification_failed", artifact_id=base.id)
            raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "presentation_unreadable", reason="verification_failed", diagnostic_id=reference)
        raise edit_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "presentation_unreadable", reason=result["unreadable"])
    output = result.pop("_result_bytes", None)
    if not output:
        raise _child_failure("no_output", base.id, "pptx_edit_apply")
    new_file = target.mode == "new_file"
    try:
        saved = await store.put_bytes(
            content=output,
            filename=edited_filename(base.filename, language) if new_file else base.filename,
            mime=base.mime or PPTX_MIME,
            created_by=user_id,
            source="presentation_edit",
            producer_kind=PRESENTATION_USER_EDIT_PRODUCER,
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
        raise edit_error(status.HTTP_404_NOT_FOUND, "presentation_not_editable", reason=exc.reason) from exc
    # The split database layout does not pin inside put_bytes; the pin is what places a version
    # without a Turn in its session (design §6.5), so write it through the session store as well.
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
        "presentation_edit.saved artifact_id=%s base=%s version=%s edits=%d new_file=%s",
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
