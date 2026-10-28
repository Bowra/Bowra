"""Transactional authorization fence for canonical chat attachments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import aiosqlite


class ChatAttachmentAccessError(ValueError):
    """The persisted attachment identity is no longer authorized."""


def _ref_value(ref: Mapping[str, Any], camel: str, snake: str) -> Any:
    return ref.get(camel, ref.get(snake))


def runtime_attachment_ids(refs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Flatten canonical bundle children and stable-dedupe runtime IDs."""
    ids: list[str] = []
    for ref in refs:
        children = _ref_value(ref, "bundleArtifactIds", "bundle_artifact_ids")
        file_id = _ref_value(ref, "fileId", "file_id")
        if children is None or children == []:
            children = [file_id]
        if not isinstance(children, list) or not children or any(type(item) is not str or not item for item in children):
            raise ChatAttachmentAccessError("attachment identity is invalid")
        ids.extend(children)
    return list(dict.fromkeys(ids))


async def validate_chat_attachment_access_in_connection(
    connection: aiosqlite.Connection,
    *,
    refs: Sequence[Mapping[str, Any]],
    runtime_ids: Sequence[str],
    user_id: str,
    workspace_id: str,
    session_id: str,
) -> None:
    """Recheck attachment authority inside the materialization transaction."""
    if not connection.in_transaction:
        raise RuntimeError("attachment validation requires a transaction")
    if len(refs) > 32 or len(runtime_ids) > 100 or any(type(item) is not str or not item for item in runtime_ids):
        raise ChatAttachmentAccessError("attachment identity is invalid")
    projected_ids = runtime_attachment_ids(refs)
    if projected_ids != list(dict.fromkeys(runtime_ids)):
        raise ChatAttachmentAccessError("attachment projection changed")

    uploads: list[Mapping[str, Any]] = []
    results: list[Mapping[str, Any]] = []
    for ref in refs:
        reference_kind = _ref_value(ref, "referenceKind", "reference_kind")
        file_id = _ref_value(ref, "fileId", "file_id")
        if (
            reference_kind not in {None, "upload", "result"}
            or type(file_id) is not str
            or not file_id
            or type(ref.get("filename")) is not str
            or not ref.get("filename")
            or type(ref.get("mime")) is not str
            or not ref.get("mime")
            or type(ref.get("size")) is not int
            or ref["size"] < 0
        ):
            raise ChatAttachmentAccessError("attachment metadata is invalid")
        (results if reference_kind == "result" else uploads).append(ref)

    upload_rows: dict[str, aiosqlite.Row] = {}
    if uploads:
        upload_ids = runtime_attachment_ids(uploads)
        upload_roots = [str(_ref_value(ref, "fileId", "file_id")) for ref in uploads]
        required_uploads = list(dict.fromkeys(upload_roots + upload_ids))
        placeholders = ",".join("?" for _ in required_uploads)
        async with connection.execute(
            "SELECT a.id,a.filename,a.mime,a.size FROM artifacts a "
            "JOIN artifact_upload_bindings b ON b.artifact_id=a.id "
            f"WHERE a.id IN ({placeholders}) "  # noqa: S608 - placeholders only
            "AND a.created_by=? AND a.deleted_at IS NULL AND a.status='ready' "
            "AND COALESCE(a.producer_kind,'')<>'template_document_internal_v1' "
            "AND b.user_id=? AND b.workspace_id=?",
            (*required_uploads, user_id, user_id, workspace_id),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            upload_rows = {str(row["id"]): row for row in await cursor.fetchall()}
        if set(upload_rows) != set(required_uploads):
            raise ChatAttachmentAccessError("upload authority changed")

    result_rows: dict[str, aiosqlite.Row] = {}
    if results:
        if any(_ref_value(ref, "bundleArtifactIds", "bundle_artifact_ids") for ref in results):
            raise ChatAttachmentAccessError("result bundle is invalid")
        result_ids = list(dict.fromkeys(str(_ref_value(ref, "fileId", "file_id")) for ref in results))
        placeholders = ",".join("?" for _ in result_ids)
        async with connection.execute(
            "WITH session_artifact_ids AS ("
            "SELECT ta.artifact_id FROM turn_attachments ta JOIN turns t ON t.id=ta.turn_id WHERE t.session_id=? "
            "UNION SELECT sa.artifact_id FROM session_attachments sa WHERE sa.session_id=? AND sa.detached_at IS NULL "
            "UNION SELECT a.id FROM artifacts a JOIN canvases c ON c.id=a.canvas_id WHERE c.session_id=? "
            "UNION SELECT a.id FROM artifacts a JOIN turns t ON t.id=a.created_by_turn WHERE t.session_id=?"
            ") SELECT a.id,a.filename,a.mime,a.size FROM artifacts a "
            "JOIN session_artifact_ids s ON s.artifact_id=a.id "
            f"WHERE a.id IN ({placeholders}) AND a.deleted_at IS NULL "  # noqa: S608 - placeholders only
            "AND a.created_by=? AND COALESCE(a.producer_kind,'')<>'template_document_internal_v1'",
            (session_id, session_id, session_id, session_id, *result_ids, user_id),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            result_rows = {str(row["id"]): row for row in await cursor.fetchall()}
        if set(result_rows) != set(result_ids):
            raise ChatAttachmentAccessError("result authority changed")

    for ref in refs:
        file_id = str(_ref_value(ref, "fileId", "file_id"))
        result = _ref_value(ref, "referenceKind", "reference_kind") == "result"
        row = result_rows[file_id] if result else upload_rows[file_id]
        if row["filename"] != ref.get("filename") or row["mime"] != ref.get("mime") or row["size"] != ref.get("size"):
            raise ChatAttachmentAccessError("attachment metadata changed")
        children = _ref_value(ref, "bundleArtifactIds", "bundle_artifact_ids")
        if not result and children:
            child_ids = list(dict.fromkeys(children))
            placeholders = ",".join("?" for _ in child_ids)
            async with connection.execute(
                f"SELECT COUNT(*) FROM chat_attachment_bundle_members WHERE bundle_artifact_id=? AND child_artifact_id IN ({placeholders})",  # noqa: S608 - placeholders only
                (file_id, *child_ids),
            ) as cursor:
                count_row = await cursor.fetchone()
            if count_row is None or int(count_row[0]) != len(child_ids):
                raise ChatAttachmentAccessError("attachment bundle changed")

    from bowhead_core.chart.selected_context import selected_facts_in_connection

    for ref in refs:
        if ref.get("selectedContext") is not None:
            try:
                await selected_facts_in_connection(connection, ref, user_id=user_id, workspace_id=workspace_id, session_id=session_id)
            except (ValueError, TypeError, KeyError, StopIteration) as exc:
                raise ChatAttachmentAccessError("chart_selection_unavailable") from exc
