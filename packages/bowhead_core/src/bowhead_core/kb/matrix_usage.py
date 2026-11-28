"""Usage events of the document matrix (step 1 S9).

Each function records one ``MetricsLog`` event (``metrics_event_log``) named ``kb.matrix.<action>``:

- ``user_id`` is the person who acted;
- ``attrs`` holds ``{team_id, workspace_id, matrix_id, origin}`` plus the event's own counts and kinds.

Attributes never hold content: no matrix, column or file names, questions, answers or excerpts. Counts of
files, columns and verified cells are not repeated here; they are read from the matrix tables. The
technical design (§9) shows how the discovery's success metrics are computed from both.

``origin`` names the entry point an action came from: ``knowledge`` (the knowledge base pages) today, and
``chat`` once Lead creates, runs and reads matrices (step 2). Recording is best-effort: without a log, with
an unknown origin, or when the write fails, the event is dropped with a log line and the caller continues.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol, get_args

from bowhead_core.kb.matrix_engine import SCOPE_KINDS

logger = logging.getLogger(__name__)

MatrixOrigin = Literal["knowledge", "chat"]
MATRIX_ORIGINS: frozenset[str] = frozenset(get_args(MatrixOrigin))

MATRIX_CREATED = "kb.matrix.created"
MATRIX_OPENED = "kb.matrix.opened"
MATRIX_RUN_STARTED = "kb.matrix.run_started"
MATRIX_EXPORTED = "kb.matrix.exported"
MATRIX_CELLS_VERIFIED = "kb.matrix.cells_verified"


class MatrixActor(Protocol):
    """Who acted and where: an ``AccessContext`` qualifies."""

    @property
    def user_id(self) -> str: ...

    @property
    def team_id(self) -> str: ...

    @property
    def workspace_id(self) -> str: ...


async def _record(
    metrics_log: Any | None,
    name: str,
    actor: MatrixActor,
    *,
    matrix_id: str,
    origin: str,
    details: dict[str, int | bool | str] | None = None,
) -> None:
    if metrics_log is None:
        return
    try:
        if origin not in MATRIX_ORIGINS:
            logger.warning("kb_matrix.usage_rejected name=%s origin=%r", name, origin)
            return
        attrs: dict[str, object] = {
            "team_id": actor.team_id,
            "workspace_id": actor.workspace_id,
            "matrix_id": matrix_id,
            "origin": origin,
            **(details or {}),
        }
        await metrics_log.track(name, user_id=actor.user_id, attrs=attrs)
    except Exception as exc:  # noqa: BLE001 — usage events never fail the action they describe.
        logger.warning("kb_matrix.usage_failed name=%s matrix_id=%s err=%s", name, matrix_id, exc)


async def track_matrix_created(metrics_log: Any | None, actor: MatrixActor, *, matrix_id: str, origin: MatrixOrigin = "knowledge") -> None:
    """A matrix was created (from chosen files and columns, or from a template's records)."""
    await _record(metrics_log, MATRIX_CREATED, actor, matrix_id=matrix_id, origin=origin)


async def track_matrix_opened(metrics_log: Any | None, actor: MatrixActor, *, matrix_id: str, origin: MatrixOrigin = "knowledge") -> None:
    """A matrix was opened: once per page load, never per poll."""
    await _record(metrics_log, MATRIX_OPENED, actor, matrix_id=matrix_id, origin=origin)


async def track_matrix_run_started(
    metrics_log: Any | None,
    actor: MatrixActor,
    *,
    matrix_id: str,
    scope_kind: str,
    cells: int,
    trial: bool,
    origin: MatrixOrigin = "knowledge",
) -> None:
    """A run was started: its scope kind (``single_cell`` for a one-cell rerun), cells queued, and whether
    it was a trial on a few files."""
    details: dict[str, int | bool | str] = {
        "scope_kind": scope_kind if scope_kind in SCOPE_KINDS else "other",
        "cells": cells,
        "trial": trial is True,
    }
    await _record(metrics_log, MATRIX_RUN_STARTED, actor, matrix_id=matrix_id, origin=origin, details=details)


async def track_matrix_exported(metrics_log: Any | None, actor: MatrixActor, *, matrix_id: str, origin: MatrixOrigin = "knowledge") -> None:
    """A matrix was exported to Excel."""
    await _record(metrics_log, MATRIX_EXPORTED, actor, matrix_id=matrix_id, origin=origin)


async def track_matrix_cells_verified(
    metrics_log: Any | None,
    actor: MatrixActor,
    *,
    matrix_id: str,
    count: int,
    bulk: bool,
    origin: MatrixOrigin = "knowledge",
) -> None:
    """Cells were marked verified: how many changed, and whether by bulk verify or one cell at a time.

    Undoing a verification is not recorded, and neither is a request that changed no cell.
    """
    if not isinstance(count, int) or count <= 0:
        return
    details: dict[str, int | bool | str] = {"count": count, "bulk": bulk is True}
    await _record(metrics_log, MATRIX_CELLS_VERIFIED, actor, matrix_id=matrix_id, origin=origin, details=details)
