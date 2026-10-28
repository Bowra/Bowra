"""Shared authorization fence for typed Composer attachment references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class ChatAttachmentAccessError(ValueError):
    """Finite attachment failure shared by HTTP admission and queue replay."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _ref_value(ref: Mapping[str, Any], camel: str, snake: str) -> Any:
    return ref.get(camel, ref.get(snake))


def runtime_attachment_ids(refs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Flatten canonical bundle children and stable-dedupe runtime IDs."""
    ids: list[str] = []
    for ref in refs:
        children = _ref_value(ref, "bundleArtifactIds", "bundle_artifact_ids")
        file_id = _ref_value(ref, "fileId", "file_id")
        # The wire contract has always treated an explicitly empty bundle as
        # an ordinary single-file reference, just like an omitted bundle.
        if children is None or children == []:
            children = [file_id]
        if not isinstance(children, list) or not children or any(type(item) is not str or not item for item in children):
            raise ChatAttachmentAccessError(409, "attachment_unavailable")
        ids.extend(children)
    return list(dict.fromkeys(ids))


async def validate_chat_attachment_access(
    *,
    artifact_store: Any,
    refs: Sequence[Mapping[str, Any]],
    runtime_ids: Sequence[str],
    user_id: str,
    workspace_id: str,
    session_id: str | None,
) -> None:
    """Revalidate exact typed metadata against current artifact authority."""
    if artifact_store is None:
        raise ChatAttachmentAccessError(503, "attachment_validation_unavailable")
    if len(refs) > 32:
        raise ChatAttachmentAccessError(422, "too_many_attachments")
    if any(type(item) is not str or not item for item in runtime_ids):
        raise ChatAttachmentAccessError(409, "attachment_unavailable")
    all_ids = list(dict.fromkeys(runtime_ids))
    if len(all_ids) > 100:
        raise ChatAttachmentAccessError(422, "too_many_attachments")
    result_claimed = any(_ref_value(ref, "referenceKind", "reference_kind") == "result" for ref in refs)
    projected_ids = runtime_attachment_ids(refs)
    if projected_ids != all_ids:
        raise ChatAttachmentAccessError(
            409,
            "result_reference_unavailable" if result_claimed else "attachment_unavailable",
        )

    uploads: list[Mapping[str, Any]] = []
    results: list[Mapping[str, Any]] = []
    for ref in refs:
        reference_kind = _ref_value(ref, "referenceKind", "reference_kind")
        file_id = _ref_value(ref, "fileId", "file_id")
        filename = ref.get("filename")
        mime = ref.get("mime")
        size = ref.get("size")
        if (
            reference_kind not in {None, "upload", "result"}
            or type(file_id) is not str
            or not file_id
            or type(filename) is not str
            or not filename
            or type(mime) is not str
            or not mime
            or type(size) is not int
            or size < 0
        ):
            raise ChatAttachmentAccessError(409, "attachment_unavailable")
        (results if reference_kind == "result" else uploads).append(ref)

    resolved_uploads: dict[str, Any] = {}
    if uploads:
        if not hasattr(artifact_store, "get_ready_uploads_for_chat"):
            raise ChatAttachmentAccessError(503, "attachment_validation_unavailable")
        upload_ids = runtime_attachment_ids(uploads)
        upload_roots = [str(_ref_value(ref, "fileId", "file_id")) for ref in uploads]
        resolved_uploads = await artifact_store.get_ready_uploads_for_chat(
            list(dict.fromkeys(upload_roots + upload_ids)),
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if set(resolved_uploads) != set(upload_roots + upload_ids):
            raise ChatAttachmentAccessError(409, "attachment_unavailable")

    resolved_results: dict[str, Any] = {}
    if results:
        if not session_id or any(_ref_value(ref, "bundleArtifactIds", "bundle_artifact_ids") for ref in results):
            raise ChatAttachmentAccessError(409, "result_reference_unavailable")
        if not hasattr(artifact_store, "list_for_session"):
            raise ChatAttachmentAccessError(503, "attachment_validation_unavailable")
        session_results = await artifact_store.list_for_session(
            session_id=session_id,
            user_id=user_id,
            limit=500,
        )
        resolved_results = {artifact.id: artifact for artifact in session_results}
        if not {str(_ref_value(ref, "fileId", "file_id")) for ref in results} <= set(resolved_results):
            raise ChatAttachmentAccessError(409, "result_reference_unavailable")

    for ref in refs:
        file_id = str(_ref_value(ref, "fileId", "file_id"))
        result = _ref_value(ref, "referenceKind", "reference_kind") == "result"
        artifact = resolved_results[file_id] if result else resolved_uploads[file_id]
        if artifact.filename != ref.get("filename") or artifact.mime != ref.get("mime") or artifact.size != ref.get("size"):
            raise ChatAttachmentAccessError(
                409,
                "result_reference_unavailable" if result else "attachment_metadata_stale",
            )
        children = _ref_value(ref, "bundleArtifactIds", "bundle_artifact_ids")
        if not result and children:
            if not hasattr(artifact_store, "chat_bundle_contains") or not await artifact_store.chat_bundle_contains(
                bundle_artifact_id=file_id,
                child_artifact_ids=children,
            ):
                raise ChatAttachmentAccessError(409, "attachment_bundle_mismatch")

    if any(ref.get("selectedContext") is not None for ref in refs):
        from bowhead_core.chart.selected_context import resolve_selected_contexts

        try:
            await resolve_selected_contexts(artifact_store, refs, user_id=user_id, workspace_id=workspace_id, session_id=session_id or "")
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            raise ChatAttachmentAccessError(409, "chart_selection_unavailable") from exc
