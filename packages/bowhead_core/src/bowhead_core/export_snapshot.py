"""Immutable Canvas ExportSnapshot v1 construction and persistence.

Candidate payloads are built outside SQLite's writer lock. ``commit_candidate``
then takes a short ``BEGIN IMMEDIATE`` transaction, revalidates only the
versioned Canvas identity, and performs an INSERT-only freeze. Asset metadata
pins join this transaction in W1-F; blob reads never do.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite
from bowhead_skill.export_naming import report_title_from_sections

from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_export_projection import (
    SURFACE_KIND_POLICIES,
    CanvasContentProjection,
    CanvasSurfaceProjection,
    build_canvas_content_projection,
    build_canvas_surface_projection,
    compute_surface_projection_token,
)
from bowhead_core.canvas_store import Canvas, Section
from bowhead_core.chart.presentation_plan import ChartPlanError
from bowhead_core.chart_style_export import (
    ChartPresentationTooLargeError,
    ChartStyleExportError,
    freeze_chart_presentation_v2,
    parse_chart_style_export_theme,
)
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_assets import (
    DesignFlattenExpectation,
    SnapshotAssetRef,
    extract_design_flatten_expectations,
    extract_snapshot_asset_refs,
    initialize_snapshot_asset_schema,
    insert_asset_pins,
    validate_asset_pins,
)
from bowhead_core.print_theme import (
    PrintThemeError,
    current_print_theme_snapshot,
    parse_print_theme_snapshot,
)
from bowhead_core.source_access_projection import (
    StaticSafeSourceMap,
    project_section_for_static_export,
)

SNAPSHOT_SCHEMA_VERSION = 1
MAX_SNAPSHOT_SECTION_COUNT = 500
MAX_SNAPSHOT_PAYLOAD_BYTES = 10 * 1024 * 1024
SNAPSHOT_RETENTION_DAYS = 7

SnapshotFormat = Literal["pdf", "pptx", "xlsx"]

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_export_snapshots (
    id                 TEXT PRIMARY KEY,
    schema_version     INTEGER NOT NULL,
    canvas_id          TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    team_id             TEXT,
    workspace_id       TEXT NOT NULL,
    canvas_owner       TEXT NOT NULL,
    requested_by       TEXT NOT NULL,
    format             TEXT NOT NULL,
    profile            TEXT NOT NULL,
    title              TEXT NOT NULL,
    locale             TEXT NOT NULL,
    timezone           TEXT NOT NULL,
    projection_token   TEXT NOT NULL,
    surface_contract_version INTEGER NOT NULL,
    surface_token      TEXT NOT NULL,
    parity_contract    TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    payload_sha256     TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL,
    section_count      INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_export_snapshots_canvas
ON canvas_export_snapshots(canvas_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_export_snapshots_immutable
BEFORE UPDATE ON canvas_export_snapshots
BEGIN
    SELECT RAISE(ABORT, 'export snapshot is immutable');
END;
"""

# Top-level payload whitelist. Nested records inside an allowed field retain
# their business keys except the exact transport-only ``_local_path`` key.
_KIND_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "eyebrow": frozenset({"text"}),
    "h1": frozenset({"text"}),
    "deck": frozenset({"markdown"}),
    "meta": frozenset({"pills"}),
    "kpi_row": frozenset({"cells"}),
    "section": frozenset({"label", "h2", "body"}),
    # Unverified list and total are **a pair**: allow both or neither (allowlists silently drop fields).
    # ⚠ Honest note: **no export renderer currently reads `unverified*`** (`compute_report_trust` has
    # only two backend call sites: `canvas.done` trust payload and IM rendering, verified in re-review).
    # Include them because snapshots are records and retaining half the data is worse than none, not because export shows wrong percentages.
    "narrative": frozenset({"markdown", "unverified_numbers", "unverified_total"}),
    "data_table": frozenset(
        {
            "caption",
            "subCaption",
            "sub_caption",
            "columns",
            "rows",
            "totalRow",
            "total_row",
            "meta",
        }
    ),
    "callout": frozenset({"label", "markdown"}),
    "next_list": frozenset({"items"}),
    "datarc_iframe": frozenset({"cardUrl", "card_url", "title", "height"}),
    "html_artifact": frozenset(
        {
            "artifact_id",
            "title",
            "height",
            "sandbox",
            "data",
            "unverified",
            "sources",
        }
    ),
    # Google Workspace cards carry `open_url`: exports link to the file, never the in-canvas preview
    # URL (which names the member's Google account via `authuser`).
    "external_iframe": frozenset({"url", "title", "height", "open_url"}),
    "image": frozenset({"artifact_id", "caption", "alt"}),
    "design": frozenset(
        {
            "title",
            "size_preset",
            "canvas_width",
            "canvas_height",
            "background",
            "layers",
            "flattened_artifact_id",
            "flattened_source_rev",
            "flattened_at",
        }
    ),
    "chart": frozenset({"spec"}),
    "spec_block": frozenset({"format", "title", "direction", "nodes", "edges"}),
    "code_block": frozenset(
        {
            "call_id",
            "lang",
            "code",
            "dur_ms",
            "stdout_preview",
            "input_artifact_ids",
            "output_artifact_ids",
            "input_artifacts",
            "output_artifacts",
            "summary_payload",
            "meta",
            "sql",
        }
    ),
    "sources": frozenset({"label", "entries"}),
    "insight_card": frozenset(
        {
            "title",
            "thesis",
            "evidence",
            "narrative",
            "impact",
            "confidence",
            # `compute_report_trust` counts insight_card and narrative **equally**,
            # but both keys were previously blocked here, losing 100% of this half's unverified data in snapshots,
            # not merely the truncation difference. Match narrative's handling (caught in re-review).
            "unverified_numbers",
            "unverified_total",
        }
    ),
    "recommendations": frozenset({"items"}),
}
if _KIND_PAYLOAD_FIELDS.keys() != SURFACE_KIND_POLICIES.keys():
    raise RuntimeError("snapshot serializer registry must cover the exact surface kind registry")

logger = logging.getLogger(__name__)


class SnapshotError(CodedError):
    """Stable error code raised before a V2 job can be queued."""


def _renderer_contract_version(theme: dict[str, Any]) -> int:
    schema_version = theme.get("schemaVersion")
    renderer_version = theme.get("rendererContractVersion")
    if schema_version == 2:
        if renderer_version != "2":
            raise SnapshotError("snapshot_renderer_contract_invalid")
        return 2
    if renderer_version is not None:
        raise SnapshotError("snapshot_renderer_contract_invalid")
    return 1


@dataclass(frozen=True, slots=True)
class ExportSnapshotRequest:
    requested_by: str
    team_id: str | None
    fmt: SnapshotFormat
    profile: str
    parity_contract: str
    locale: str
    timezone: str
    client_surface_receipt_sha256: str
    theme: dict[str, Any]
    options: dict[str, Any]
    client_visual_receipts: tuple[dict[str, Any], ...] = ()
    print_theme: dict[str, Any] = field(default_factory=current_print_theme_snapshot)


@dataclass(frozen=True, slots=True)
class ExportSnapshotCandidate:
    id: str
    schema_version: int
    canvas_id: str
    session_id: str
    team_id: str | None
    workspace_id: str
    canvas_owner: str
    requested_by: str
    fmt: SnapshotFormat
    profile: str
    title: str
    locale: str
    timezone: str
    parity_contract: str
    renderer_contract_version: int
    content_projection: CanvasContentProjection
    surface_projection: CanvasSurfaceProjection
    screen_sections: tuple[dict[str, Any], ...]
    asset_refs: tuple[SnapshotAssetRef, ...]
    design_flatten_expectations: tuple[DesignFlattenExpectation, ...]
    payload_json: str
    payload_sha256: str
    size_bytes: int
    section_count: int
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ExportSnapshot:
    id: str
    schema_version: int
    canvas_id: str
    session_id: str
    team_id: str | None
    workspace_id: str
    canvas_owner: str
    requested_by: str
    fmt: str
    profile: str
    title: str
    locale: str
    timezone: str
    projection_token: str
    surface_contract_version: int
    surface_token: str
    parity_contract: str
    payload: dict[str, Any]
    payload_sha256: str
    size_bytes: int
    section_count: int
    created_at: str
    expires_at: str


@dataclass
class ExportSnapshotStore:
    db_path: str | Path
    _path: str = field(init=False)

    def __post_init__(self) -> None:
        self._path = str(self.db_path)

    async def initialize(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self._path) as db:
            await db.executescript(_SNAPSHOT_SCHEMA)
            await initialize_snapshot_asset_schema(db)
            await db.commit()

    async def build_candidate(
        self,
        *,
        canvas_id: str,
        request: ExportSnapshotRequest,
        snapshot_id: str | None = None,
        created_at: str | None = None,
        project_sections: Callable[[Canvas, list[Section]], Awaitable[list[Section]]] | None = None,
    ) -> ExportSnapshotCandidate:
        """Read a stable Canvas state, then serialize and hash outside the read transaction."""
        canvas, sections = await self._read_stable_state(canvas_id)
        content_projection = build_canvas_content_projection(
            title=canvas.title,
            sections=sections,
        )
        current_ids = {entry.section_id for entry in content_projection.sections}
        if not current_ids:
            raise SnapshotError("snapshot_empty")
        unsupported = sorted(section.kind for section in sections if section.id in current_ids and section.kind not in SURFACE_KIND_POLICIES)
        if unsupported:
            raise SnapshotError(
                "snapshot_unsupported_section_kind",
                kinds=unsupported,
            )
        quarantined = sorted(section.id for section in sections if section.id in current_ids and section.quarantined)
        if quarantined:
            raise SnapshotError("snapshot_quarantined", section_ids=quarantined)

        surface_projection = build_canvas_surface_projection(
            content_projection=content_projection,
            sections=sections,
        )
        if not surface_projection.entries:
            raise SnapshotError("snapshot_empty")

        # Preserve canonical CAS identity while freezing the exact stable sections
        # through the requester's existing permission projection.
        has_plan = any(_has_chart_plan(section.to_report_section()) for section in sections)
        if has_plan and project_sections is not None:
            projected_sections = await project_sections(canvas, sections)
            if [(s.id, s.rev) for s in projected_sections] != [(s.id, s.rev) for s in sections]:
                raise SnapshotError("snapshot_projection_changed_identity")
            sections = projected_sections
        elif has_plan:
            raise SnapshotError("chart_plan_projection_required")

        candidate_id = snapshot_id or _new_snapshot_id()
        if not candidate_id.startswith("exs_"):
            raise SnapshotError("invalid_snapshot_id")
        frozen_at = created_at or _now()
        expires_at = _expires_at(frozen_at)
        payload, section_count, asset_refs = _build_snapshot_payload(
            snapshot_id=candidate_id,
            created_at=frozen_at,
            canvas=canvas,
            request=request,
            content_projection=content_projection,
            surface_projection=surface_projection,
            sections=sections,
        )
        try:
            payload_json = _canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise SnapshotError("snapshot_invalid_payload") from exc
        payload_bytes = payload_json.encode("utf-8")
        if len(payload_bytes) > MAX_SNAPSHOT_PAYLOAD_BYTES:
            raise SnapshotError(
                "snapshot_too_large",
                size_bytes=len(payload_bytes),
                limit_bytes=MAX_SNAPSHOT_PAYLOAD_BYTES,
            )
        payload_sha256 = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
        # Same function/source as skill export and legacy jobs (`export_naming`): filenames use the report's
        # own h1. `canvas.title` exists only after manual renaming; automatic output always leaves it NULL.
        #
        # ★ This is the **third** implementation site and the actual live path: on 116 prod 1.1.26,
        # every `canvas_export_jobs` row had `snapshot_id`, selecting snapshots; the two legacy sites
        # (`canvas_export.py:885`) were already fixed. Chain: snapshot title='Bowhead Canvas'
        # → job.filename='Bowhead Canvas.xlsx' → the user's download name,
        # despite an h1 "退款率最高的 20 个品牌" (persisted a day earlier, not a race).
        title = report_title_from_sections(sections, canvas.title) or "Bowhead Canvas"
        renderer_contract_version = _renderer_contract_version(request.theme)
        return ExportSnapshotCandidate(
            id=candidate_id,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            canvas_id=canvas.id,
            session_id=canvas.session_id,
            team_id=request.team_id,
            workspace_id=canvas.workspace_id,
            canvas_owner=canvas.user_id,
            requested_by=request.requested_by,
            fmt=request.fmt,
            profile=request.profile,
            title=title,
            locale=request.locale,
            timezone=request.timezone,
            parity_contract=request.parity_contract,
            renderer_contract_version=renderer_contract_version,
            content_projection=content_projection,
            surface_projection=surface_projection,
            screen_sections=tuple(section.to_report_section() for section in sections if section.id in current_ids),
            asset_refs=asset_refs,
            design_flatten_expectations=extract_design_flatten_expectations(
                payload["sections"],
                canvas_id=canvas.id,
            ),
            payload_json=payload_json,
            payload_sha256=payload_sha256,
            size_bytes=len(payload_bytes),
            section_count=section_count,
            created_at=frozen_at,
            expires_at=expires_at,
        )

    async def commit_candidate(
        self,
        candidate: ExportSnapshotCandidate,
        *,
        expected_projection_token: str,
        expected_surface_token: str,
    ) -> ExportSnapshot:
        """Revalidate a candidate under a short writer lock and freeze it once."""
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self.insert_candidate_in_transaction(
                    db,
                    candidate,
                    expected_projection_token=expected_projection_token,
                    expected_surface_token=expected_surface_token,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        snapshot = await self.get_snapshot(candidate.id)
        if snapshot is None:  # pragma: no cover - committed INSERT must be readable
            raise RuntimeError("committed export snapshot is missing")
        return snapshot

    async def insert_candidate_in_transaction(
        self,
        db: aiosqlite.Connection,
        candidate: ExportSnapshotCandidate,
        *,
        expected_projection_token: str,
        expected_surface_token: str,
    ) -> None:
        """Validate and insert snapshot + pins in the caller's active transaction.

        The caller owns commit/rollback. W1-G uses this to add the V2 job row to
        the same atomic unit; no blob I/O is performed here.
        """
        if not db.in_transaction:
            raise RuntimeError("snapshot insert requires an active transaction")
        if candidate.requested_by != candidate.canvas_owner:
            raise SnapshotError("canvas_not_owned")
        if expected_projection_token != candidate.content_projection.projection_token:
            raise SnapshotError("projection_conflict")
        if expected_surface_token != candidate.surface_projection.surface_token:
            raise SnapshotError("surface_projection_conflict")

        await self._validate_current_state(db, candidate)
        pins = await validate_asset_pins(
            db,
            refs=candidate.asset_refs,
            requested_by=candidate.requested_by,
            session_id=candidate.session_id,
            pinned_at=candidate.created_at,
            design_flatten_expectations=candidate.design_flatten_expectations,
        )
        async with db.execute(
            "SELECT 1 FROM canvas_export_snapshots WHERE id=?",
            (candidate.id,),
        ) as cursor:
            if await cursor.fetchone() is not None:
                raise SnapshotError("snapshot_id_conflict")
        await db.execute(
            "INSERT INTO canvas_export_snapshots("
            "id, schema_version, canvas_id, session_id, team_id, workspace_id, "
            "canvas_owner, requested_by, format, profile, title, locale, timezone, "
            "projection_token, surface_contract_version, surface_token, parity_contract, "
            "payload_json, payload_sha256, size_bytes, section_count, created_at, expires_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _candidate_row(candidate),
        )
        await insert_asset_pins(
            db,
            snapshot_id=candidate.id,
            pins=pins,
        )

    async def get_snapshot(self, snapshot_id: str) -> ExportSnapshot | None:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_export_snapshots WHERE id=?",
                (snapshot_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_snapshot(row) if row is not None else None

    async def _read_stable_state(self, canvas_id: str) -> tuple[Canvas, list[Section]]:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                async with db.execute(
                    "SELECT id, session_id, workspace_id, user_id, title, status, created_at, updated_at FROM canvases WHERE id=?",
                    (canvas_id,),
                ) as cursor:
                    canvas_row = await cursor.fetchone()
                if canvas_row is None:
                    raise SnapshotError("canvas_not_found")
                async with db.execute(
                    "SELECT id, canvas_id, seq, kind, payload, rev, created_at, updated_at, "
                    "quarantined, quarantine_reason, hidden_for_chat_mode, surface_rationale, "
                    "owner, superseded, superseded_by_turn, turn_id "
                    "FROM canvas_sections WHERE canvas_id=? ORDER BY seq, id",
                    (canvas_id,),
                ) as cursor:
                    section_rows = await cursor.fetchall()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _canvas_from_row(canvas_row), [_section_from_row(row) for row in section_rows]

    async def _validate_current_state(
        self,
        db: aiosqlite.Connection,
        candidate: ExportSnapshotCandidate,
    ) -> None:
        async with db.execute(
            "SELECT title FROM canvases WHERE id=?",
            (candidate.canvas_id,),
        ) as cursor:
            canvas_row = await cursor.fetchone()
        if canvas_row is None:
            raise SnapshotError("projection_conflict")
        async with db.execute(
            "SELECT id, canvas_id, seq, kind, rev, quarantined, hidden_for_chat_mode, "
            "superseded FROM canvas_sections WHERE canvas_id=? ORDER BY seq, id",
            (candidate.canvas_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        current_sections = [_metadata_section_from_row(row) for row in rows]
        current_content = build_canvas_content_projection(
            title=canvas_row["title"],
            sections=current_sections,
        )
        if current_content.projection_token != candidate.content_projection.projection_token:
            raise SnapshotError("projection_conflict")

        # The surface plan may depend on insight payload references. The pinned
        # candidate plan is safe to reuse after content identity matches because
        # every payload mutation must bump ``rev``. This keeps payload reads and
        # surface traversal outside the SQLite writer lock.
        current_surface_token = compute_surface_projection_token(
            surface_contract_version=candidate.surface_projection.contract_version,
            content_projection_token=current_content.projection_token,
            entries=candidate.surface_projection.entries,
            exclusions=candidate.surface_projection.exclusions,
        )
        if current_surface_token != candidate.surface_projection.surface_token:
            raise SnapshotError("surface_projection_conflict")


def _build_snapshot_payload(
    *,
    snapshot_id: str,
    created_at: str,
    canvas: Canvas,
    request: ExportSnapshotRequest,
    content_projection: CanvasContentProjection,
    surface_projection: CanvasSurfaceProjection,
    sections: list[Section],
) -> tuple[dict[str, Any], int, tuple[SnapshotAssetRef, ...]]:
    section_by_id = {section.id: section for section in sections}
    section_counter = [0]
    source_map = StaticSafeSourceMap()
    frozen_sections = [
        _freeze_top_level_section(
            section_by_id[entry.section_id],
            section_counter=section_counter,
            source_map=source_map,
        )
        for entry in surface_projection.entries
    ]
    if request.theme.get("schemaVersion") == 2:
        try:
            parsed_theme = parse_chart_style_export_theme(request.theme)
            # The print theme is the team's resolved snapshot (product default or published
            # brand); it must be well formed and bound to the chart-style theme it was frozen with.
            try:
                parsed_print_theme = parse_print_theme_snapshot(request.print_theme)
            except PrintThemeError as exc:
                raise ChartStyleExportError("print theme snapshot is invalid") from exc
            if parsed_theme["structuralThemeChecksum"] != parsed_print_theme["checksum"]:
                raise ChartStyleExportError("structural theme binding is invalid")
            added_panels = _freeze_chart_presentations(
                frozen_sections,
                theme=parsed_theme,
                locale=request.locale,
            )
            for _ in range(added_panels):
                _increment_section_count(section_counter)
        except ChartStyleExportError as exc:
            raise SnapshotError("snapshot_chart_style_invalid") from exc
    asset_refs = extract_snapshot_asset_refs(frozen_sections)
    exclusions = [asdict(exclusion) for exclusion in surface_projection.exclusions]
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "canvas": {
            "id": canvas.id,
            "session_id": canvas.session_id,
            "team_id": request.team_id,
            "workspace_id": canvas.workspace_id,
            "owner_user_id": canvas.user_id,
            # naming-exempt: records the **canvas's own** title (audit/parity metadata),
            # unused for filenames/document titles; naming uses `report_title_from_sections` above.
            "title": canvas.title or "Bowhead Canvas",
        },
        "request": {
            "requested_by": request.requested_by,
            "format": request.fmt,
            "profile": request.profile,
            "parity_contract": request.parity_contract,
            "client_surface_receipt_sha256": request.client_surface_receipt_sha256,
            "client_visual_receipts": [_sanitize_json(receipt) for receipt in request.client_visual_receipts],
            "locale": request.locale,
            "timezone": request.timezone,
            "theme": _sanitize_json(request.theme),
            "print_theme": _sanitize_json(request.print_theme),
            "options": _sanitize_json(request.options),
        },
        "projection": {
            "token": content_projection.projection_token,
            "created_at": created_at,
            "excluded": exclusions,
        },
        "surface_projection": {
            "contract_version": surface_projection.contract_version,
            "token": surface_projection.surface_token,
            "entries": [asdict(entry) for entry in surface_projection.entries],
            "exclusions": exclusions,
        },
        "sections": frozen_sections,
        "resolved_sources": [],
        "assets": [asdict(ref) for ref in asset_refs],
    }
    return payload, section_counter[0], asset_refs


def _has_chart_plan(section: dict[str, Any]) -> bool:
    if section.get("kind") == "chart" and isinstance(section.get("spec"), dict):
        return "presentation_plan" in section["spec"]
    return any(_has_chart_plan(child) for child in section.get("body", []) if isinstance(child, dict))


def _freeze_chart_presentations(
    sections: list[dict[str, Any]],
    *,
    theme: dict[str, Any],
    locale: str,
) -> int:
    """Attach server-owned presentation beside chart payloads, including nested ones."""
    added_panels = 0
    for section in sections:
        kind = section.get("kind")
        payload = section.get("payload")
        content = payload if isinstance(payload, dict) else section
        if kind == "chart":
            spec = content.get("spec")
            if not isinstance(spec, dict):
                raise SnapshotError("snapshot_invalid_payload")
            if "presentation_plan" in spec:
                from bowhead_core.chart.presentation_plan import validate_projected_notes_plan

                try:
                    validate_projected_notes_plan(spec["presentation_plan"], static=True)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise SnapshotError("snapshot_invalid_chart_plan") from exc
            if isinstance(spec.get("views"), list) and spec["views"]:
                views = spec["views"]
                minimum = 1 if spec.get("presentation_plan", {}).get("analytical_views") else 2
                if not minimum <= len(views) <= 6 or any(
                    not isinstance(view, dict) or view.get("kind") != "chart" or not isinstance(view.get("spec"), dict) or view["spec"].get("views")
                    for view in views
                ):
                    raise SnapshotError("snapshot_invalid_payload")
                from bowhead_core.chart.prepared_frames import static_frame_views

                spec["views"] = views = static_frame_views(spec, locale=locale)
                from bowhead_core.chart.annotations import annotation_views

                spec["views"] = views = annotation_views(spec)
                if spec.get("presentation_plan", {}).get("analytical_views"):
                    from bowhead_core.chart.analytical import materialize_analytical

                    spec["views"] = views = materialize_analytical(spec)["views"]
                added_panels += _freeze_chart_presentations(views, theme=theme, locale=locale)
                continue
            from bowhead_core.chart.analytical import static_analytical_panels

            panels = static_analytical_panels(spec, locale=locale)
            if panels:
                added_panels += len(panels) + _freeze_chart_presentations(panels, theme=theme, locale=locale)
                content["spec"] = {**spec, "views": panels}
                continue
            try:
                section["frozen_chart_presentation"] = freeze_chart_presentation_v2(
                    spec,
                    theme=theme,
                    locale=locale,
                )
            except ChartPresentationTooLargeError:
                # This chart cannot fit export (too many series/categories, frozen style >64KB) ⇒ **degrade only this chart**:
                # export a table with every row (footer says "共 N 行"), rather than fail the entire export.
                # Chart-creation gate (`chart/infer.py`'s `_undrawable_reason`) covers only new charts; edit-time kind changes
                # and existing charts bypass it, requiring this extra fallback at the shared boundary for all export paths.
                logger.warning(
                    "export.chart_degraded_to_table section=%s kind=%s rows=%s — 冻结样式放不下",
                    section.get("id"),
                    spec.get("kind"),
                    len(spec.get("rows") or []),
                )
                # Readers must see it too: silently replacing a canvas chart with a PDF table is another hidden change.
                note = "数据量过大，已按表格导出" if str(locale or "").lower().startswith("zh") else "Too large to chart; exported as a table"
                subtitle = str(spec.get("subtitle") or "").strip()
                # Clear `top_n` too: table footer already says "显示 1-N / 共 M", so retaining it repeats the same message
                # (same clearing when `chart/infer.py` falls back to tables).
                spec = {
                    **spec,
                    "kind": "table",
                    "top_n": None,
                    "subtitle": f"{subtitle} · {note}" if subtitle else note,
                }
                content["spec"] = spec
                section["frozen_chart_presentation"] = freeze_chart_presentation_v2(
                    spec,
                    theme=theme,
                    locale=locale,
                )
            continue
        if kind != "section":
            continue
        body = content.get("body")
        if isinstance(body, list):
            added_panels += _freeze_chart_presentations(
                [item for item in body if isinstance(item, dict)],
                theme=theme,
                locale=locale,
            )
    return added_panels


def _freeze_top_level_section(
    section: Section,
    *,
    section_counter: list[int],
    source_map: StaticSafeSourceMap,
) -> dict[str, Any]:
    _increment_section_count(section_counter)
    payload = _sanitize_section_payload(
        section.kind,
        section.payload,
        section_counter=section_counter,
        source_map=source_map,
    )
    try:
        payload, source_refs = project_section_for_static_export(
            section_id=section.id,
            section_kind=section.kind,
            payload=payload,
            source_refs=_sanitize_source_refs(section.payload.get("source_refs")),
            source_map=source_map,
        )
    except ChartPlanError as exc:
        raise SnapshotError("snapshot_invalid_chart_plan") from exc
    frozen = {
        "id": section.id,
        "seq": section.seq,
        "kind": section.kind,
        "rev": section.rev,
        "turn_id": section.turn_id,
        "payload": payload,
        "source_refs": source_refs,
    }
    return frozen


def _sanitize_section_payload(
    kind: str,
    payload: dict[str, Any],
    *,
    section_counter: list[int],
    source_map: StaticSafeSourceMap,
) -> dict[str, Any]:
    allowed = _KIND_PAYLOAD_FIELDS.get(kind)
    if allowed is None:
        raise SnapshotError("snapshot_unsupported_section_kind", kinds=[kind])
    frozen: dict[str, Any] = {}
    for key in sorted(allowed):
        if key not in payload:
            continue
        value = payload[key]
        if kind == "chart" and key == "spec" and isinstance(value, dict) and isinstance(value.get("views"), list):
            for _ in value["views"]:
                _increment_section_count(section_counter)
        if kind == "section" and key == "body":
            if not isinstance(value, list):
                raise SnapshotError("snapshot_invalid_payload")
            frozen[key] = [
                _sanitize_nested_section(
                    item,
                    section_counter=section_counter,
                    source_map=source_map,
                )
                for item in value
            ]
        else:
            frozen[key] = _sanitize_json(value)
    return frozen


def _sanitize_nested_section(
    value: Any,
    *,
    section_counter: list[int],
    source_map: StaticSafeSourceMap,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError("snapshot_invalid_payload")
    kind = value.get("kind")
    section_id = value.get("id")
    if not isinstance(kind, str) or not isinstance(section_id, str):
        raise SnapshotError("snapshot_invalid_payload")
    if kind not in SURFACE_KIND_POLICIES:
        raise SnapshotError("snapshot_unsupported_section_kind", kinds=[kind])
    _increment_section_count(section_counter)
    payload = _sanitize_section_payload(
        kind,
        value,
        section_counter=section_counter,
        source_map=source_map,
    )
    try:
        payload, refs = project_section_for_static_export(
            section_id=section_id,
            section_kind=kind,
            payload=payload,
            source_refs=_sanitize_source_refs(value.get("source_refs")),
            source_map=source_map,
        )
    except ChartPlanError as exc:
        raise SnapshotError("snapshot_invalid_chart_plan") from exc
    frozen: dict[str, Any] = {
        "id": section_id,
        "kind": kind,
        **payload,
    }
    rev = value.get("rev")
    if isinstance(rev, int) and not isinstance(rev, bool):
        frozen["rev"] = rev
    if refs:
        frozen["source_refs"] = refs
    return frozen


def _sanitize_source_refs(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [_sanitize_json(item) for item in value]


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_json(item) for key, item in value.items() if key != "_local_path"}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json(item) for item in value]
    return value


def _increment_section_count(section_counter: list[int]) -> None:
    section_counter[0] += 1
    if section_counter[0] > MAX_SNAPSHOT_SECTION_COUNT:
        raise SnapshotError(
            "snapshot_section_limit",
            section_count=section_counter[0],
            limit=MAX_SNAPSHOT_SECTION_COUNT,
        )


def _candidate_row(candidate: ExportSnapshotCandidate) -> tuple[Any, ...]:
    return (
        candidate.id,
        candidate.schema_version,
        candidate.canvas_id,
        candidate.session_id,
        candidate.team_id,
        candidate.workspace_id,
        candidate.canvas_owner,
        candidate.requested_by,
        candidate.fmt,
        candidate.profile,
        candidate.title,
        candidate.locale,
        candidate.timezone,
        candidate.content_projection.projection_token,
        candidate.surface_projection.contract_version,
        candidate.surface_projection.surface_token,
        candidate.parity_contract,
        candidate.payload_json,
        candidate.payload_sha256,
        candidate.size_bytes,
        candidate.section_count,
        candidate.created_at,
        candidate.expires_at,
    )


def _row_to_snapshot(row: aiosqlite.Row) -> ExportSnapshot:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):  # pragma: no cover - serializer always emits object
        raise RuntimeError("export snapshot payload is not an object")
    return ExportSnapshot(
        id=row["id"],
        schema_version=int(row["schema_version"]),
        canvas_id=row["canvas_id"],
        session_id=row["session_id"],
        team_id=row["team_id"],
        workspace_id=row["workspace_id"],
        canvas_owner=row["canvas_owner"],
        requested_by=row["requested_by"],
        fmt=row["format"],
        profile=row["profile"],
        title=row["title"],
        locale=row["locale"],
        timezone=row["timezone"],
        projection_token=row["projection_token"],
        surface_contract_version=int(row["surface_contract_version"]),
        surface_token=row["surface_token"],
        parity_contract=row["parity_contract"],
        payload=payload,
        payload_sha256=row["payload_sha256"],
        size_bytes=int(row["size_bytes"]),
        section_count=int(row["section_count"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _canvas_from_row(row: aiosqlite.Row) -> Canvas:
    return Canvas(
        id=row["id"],
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _section_from_row(row: aiosqlite.Row) -> Section:
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise SnapshotError("snapshot_invalid_payload")
    return Section(
        id=row["id"],
        canvas_id=row["canvas_id"],
        seq=int(row["seq"]),
        kind=row["kind"],
        payload=payload,
        rev=int(row["rev"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        quarantined=bool(row["quarantined"]),
        quarantine_reason=row["quarantine_reason"],
        hidden_for_chat_mode=bool(row["hidden_for_chat_mode"]),
        surface_rationale=row["surface_rationale"],
        owner=row["owner"],
        superseded=bool(row["superseded"]),
        superseded_by_turn=row["superseded_by_turn"],
        turn_id=row["turn_id"],
    )


def _metadata_section_from_row(row: aiosqlite.Row) -> Section:
    return Section(
        id=row["id"],
        canvas_id=row["canvas_id"],
        seq=int(row["seq"]),
        kind=row["kind"],
        payload={},
        rev=int(row["rev"]),
        created_at="",
        updated_at="",
        quarantined=bool(row["quarantined"]),
        hidden_for_chat_mode=bool(row["hidden_for_chat_mode"]),
        superseded=bool(row["superseded"]),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _new_snapshot_id() -> str:
    return f"exs_{uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _expires_at(created_at: str) -> str:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (created + timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat(timespec="seconds")


__all__ = [
    "ExportSnapshot",
    "ExportSnapshotCandidate",
    "ExportSnapshotRequest",
    "ExportSnapshotStore",
    "MAX_SNAPSHOT_PAYLOAD_BYTES",
    "MAX_SNAPSHOT_SECTION_COUNT",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotError",
]
