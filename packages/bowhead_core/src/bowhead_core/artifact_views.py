"""Rendered views of a stored file, kept with the file (docx inline edit design §5.2).

A file's bytes never change under its ID (a new version is a new artifact), so its PDF render is
rendered once and kept next to it: ``<id>_.view.<kind>.pdf`` and a small record
``<id>_.view.<kind>.json`` at the storage backend's own paths for that artifact. The record names
the stored object the render came from and the renderer (the worker profile and preview contract the
gateway requires, plus a generation the gateway bumps to redo kept renders), so a replaced object or a
new renderer is a miss and renders again. The renderer is known without asking the executor, so a
kept render is served with no network call.

Views are deleted with the file: the 30-day purge deletes them before completing (a failure keeps
the purge work item for retry), and account erasure adds their paths to its object inventory. They
are never artifact rows, so Results, Files, usage totals and the Lead never see them. A backend
that cannot name an artifact's paths (no ``path_for_artifact``) gets no views, because they could
not be erased.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)

VIEW_KINDS = ("pptx-pdf", "docx-pdf")
_RECORD_LIMIT = 4096


def _names(kind: str) -> tuple[str, str]:
    return f".view.{kind}.pdf", f".view.{kind}.json"


def view_paths(backend: Any, artifact_id: str) -> list[str]:
    """Every path a view of this artifact can occupy, for purge and account erasure."""
    path_for = getattr(backend, "path_for_artifact", None)
    if not callable(path_for):
        return []
    return [str(path_for(artifact_id, name)) for kind in VIEW_KINDS for name in _names(kind)]


async def read_view(backend: Any, artifact_id: str, kind: str, *, source: str, renderer: str, limit: int) -> bytes | None:
    """The kept render, or None on a miss."""
    return await _read(backend, artifact_id, kind, source=source, renderer=renderer, limit=limit)


async def read_view_for_source(backend: Any, artifact_id: str, kind: str, *, source: str, limit: int) -> bytes | None:
    """The kept render of this stored object by any renderer, or None on a miss.

    For reading what the person saw (a page reference in chat, design §7.4): any render of the same
    stored object shows the pages they referenced, so the renderer is not matched. Size and digest are.
    """
    return await _read(backend, artifact_id, kind, source=source, renderer=None, limit=limit)


async def _read(backend: Any, artifact_id: str, kind: str, *, source: str, renderer: str | None, limit: int) -> bytes | None:
    path_for = getattr(backend, "path_for_artifact", None)
    if kind not in VIEW_KINDS or not callable(path_for):
        return None
    pdf_name, record_name = _names(kind)
    try:
        record = json.loads(await backend.read_bounded(str(path_for(artifact_id, record_name)), limit=_RECORD_LIMIT))
        if not isinstance(record, dict) or record.get("source") != source:
            return None
        if renderer is not None and record.get("renderer") != renderer:
            return None
        size = record.get("size")
        if not isinstance(size, int) or not 0 < size <= limit:
            return None
        pdf = await backend.read_bounded(str(path_for(artifact_id, pdf_name)), limit=limit)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - a broken cache entry is a miss, never a failed view
        log.warning("artifact.view_read_failed", artifact_id=artifact_id, kind=kind, error_type=type(exc).__name__)
        return None
    if len(pdf) != record["size"] or hashlib.sha256(pdf).hexdigest() != record.get("sha256"):
        return None
    return pdf


async def write_view(backend: Any, artifact_id: str, kind: str, *, source: str, renderer: str, pdf: bytes) -> None:
    """Keep a render; failures only log, because the view was already served."""
    if kind not in VIEW_KINDS or not callable(getattr(backend, "path_for_artifact", None)):
        return
    pdf_name, record_name = _names(kind)
    record = {"source": source, "renderer": renderer, "size": len(pdf), "sha256": hashlib.sha256(pdf).hexdigest()}
    try:
        # The PDF first: a reader that sees the new record finds the matching PDF, and a reader that
        # sees the old record next to the new PDF fails the digest check and renders again.
        await backend.put(aid=artifact_id, content=pdf, filename=pdf_name, mime="application/pdf")
        await backend.put(aid=artifact_id, content=json.dumps(record).encode(), filename=record_name, mime="application/json")
    except Exception as exc:  # noqa: BLE001
        log.warning("artifact.view_write_failed", artifact_id=artifact_id, kind=kind, error_type=type(exc).__name__)


async def delete_views(backend: Any, artifact_id: str) -> None:
    """Delete every view of the artifact; absent paths are fine, other failures raise."""
    for path in view_paths(backend, artifact_id):
        try:
            await backend.delete(path)
        except FileNotFoundError:
            continue


__all__ = ["VIEW_KINDS", "delete_views", "read_view", "read_view_for_source", "view_paths", "write_view"]
